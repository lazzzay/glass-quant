#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V7.9.26 DIAGNOSTIC-ROLLUP-RANGE-1
=================================

Erweitert V7.9.25 DIAGNOSTIC-ROLLUP-1 um einen RANGE-Mode, der per-Day-Rollups
ueber ein Werktage-Fenster aggregiert.

Single-Day-Mode (unveraendert seit V7.9.25):
  python3 tools/diagnostic_rollup.py --date 2026-05-13

Range-Mode (NEU, V7.9.26):
  python3 tools/diagnostic_rollup.py \
      --start-date 2026-05-07 --end-date 2026-05-13

Range-Synth-Layer (ueber alle Werktage zwischen start/end inkl.):
  consensus_status_history   - Liste {date, consensus_status, primary_target}
  worst_consensus_in_window  - schlechteste Severity in der Range
  worst_severity_streak      - laengster zusammenhaengender BLACK-Streak (Werktage)
  red_or_black_streak        - laengster RED-oder-BLACK-Streak
  vote_persistence_pct       - % Tage mit gleichem primary_target_consensus
                                wie der dominante Target
  dominant_primary_target    - haeufigster primary_target_consensus (None bei
                                INSUFFICIENT_DATA-Tagen)
  per_tool                   - pro Tool: severity_distribution,
                                longest_black_streak, availability_pct
  range_recommendation       - Heuristik: BLACK-Streak ≥ 3 + Konsens-Persistence
                                ≥ 60% ⇒ Sprint-Trigger; sonst Watchdog.

Read-only. Freeze-konform. Keine Score-Aenderung. KW20-Bugfix-Quote NICHT
angefasst (ging Mo 11.05. an V7.9.24).

Single-Day-Quellen pro Tool (unveraendert):
  V7.9.15 trade_funnel_diagnostic          → report.severity, bottleneck_stage
  V7.9.17 layer_heal_scorecard             → scorecard.overall_severity, layer_stats
  V7.9.18 of_data_quality                  → severity, silent_decommission
  V7.9.20 oi_heal_telemetry                → severity, heal_success_pct
  V7.9.21 threshold_passer_coverage        → severity, pass_rate_pct
  V7.9.22 score_component_effectiveness    → overall_severity, primary_dead_layer
  V7.9.23 fix_session_verify (V7.9.24)     → overall_severity, fixes[]

Ausgabe Single-Day:
  - tools/diagnostic_rollup_<DATE>.json
  - Briefings/Diagnostic-Rollup-<DATE>.md (sofern --no-md nicht gesetzt)

Ausgabe Range:
  - tools/diagnostic_rollup_range_<START>_<END>.json
  - Briefings/Diagnostic-Rollup-Range-<START>_<END>.md

Exit-Codes (mit --strict):
  Single-Day:
    0  GREEN/YELLOW
    1  RED
    2  BLACK
    3  INSUFFICIENT_DATA (weniger als min_tools verfuegbar)
  Range:
    0  worst_consensus_in_window in {GREEN, YELLOW}
    1  worst_consensus_in_window == RED
    2  worst_consensus_in_window == BLACK
    3  alle Tage INSUFFICIENT_DATA

Mode: FEATURE (V7.9.26). Read-only. Freeze-konform. Keine Score-Aenderung,
keine Live-Pipeline-Aenderung. KW20-Bugfix-Quote NICHT angefasst (ging
Mo 11.05. an V7.9.24).
"""

import argparse
import datetime as _dt
import json
import os
import sys
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

VERSION = "V7.9.26"
TOOL_NAME = "diagnostic_rollup"

# V7.9.24-kompatible Retry-Logik gegen FUSE-EDEADLK
TRANSIENT_OS_ERRNOS = {
    11,  # EAGAIN
    16,  # EBUSY
    35,  # EDEADLK (FUSE auf macOS)
}
TRANSIENT_RETRIES = 5
TRANSIENT_DELAY_S = 0.25

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"

SEVERITY_RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3, "UNAVAILABLE": -1}
SEVERITY_FROM_RANK = {0: "GREEN", 1: "YELLOW", 2: "RED", 3: "BLACK"}

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MIN_TOOLS = 4  # weniger als 4/7 verfuegbar = INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# Safe-IO (FUSE-EDEADLK tolerant)
# ---------------------------------------------------------------------------


def safe_load_json(
    path: Path,
    retries: int = TRANSIENT_RETRIES,
    delay_s: float = TRANSIENT_DELAY_S,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Returns (payload, info).

    info-Felder:
      file_missing  True iff FileNotFoundError
      parse_error   True iff json.JSONDecodeError
      transient_io  True iff alle Versuche an transientem OSError gescheitert
      attempts      Anzahl Lese-Versuche
      last_error    letzte Exception als String oder None
    """
    info: Dict[str, Any] = {
        "file_missing": False,
        "parse_error": False,
        "transient_io": False,
        "attempts": 0,
        "last_error": None,
    }
    last_err: Optional[str] = None
    n = max(1, int(retries))
    for i in range(n):
        info["attempts"] = i + 1
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh), info
        except FileNotFoundError as e:
            info["file_missing"] = True
            info["last_error"] = str(e)
            return None, info
        except json.JSONDecodeError as e:
            info["parse_error"] = True
            info["last_error"] = str(e)
            return None, info
        except OSError as e:
            errno = getattr(e, "errno", None)
            last_err = f"OSError errno={errno}: {e}"
            if errno in TRANSIENT_OS_ERRNOS and (i + 1) < n:
                _time.sleep(delay_s)
                continue
            info["transient_io"] = errno in TRANSIENT_OS_ERRNOS
            info["last_error"] = last_err
            return None, info
    info["transient_io"] = True
    info["last_error"] = last_err
    return None, info


# ---------------------------------------------------------------------------
# Source-Definitionen
# ---------------------------------------------------------------------------


def _norm_severity(s: Any) -> str:
    if not isinstance(s, str):
        return "UNAVAILABLE"
    up = s.upper().strip()
    return up if up in SEVERITY_RANK else "UNAVAILABLE"


def _extract_trade_funnel(payload: Dict[str, Any]) -> Dict[str, Any]:
    report = payload.get("report") or {}
    severity = _norm_severity(report.get("severity"))
    bottleneck = report.get("bottleneck_stage")
    dead = report.get("layers_dead") or []
    top_score = report.get("top_score")
    gap = report.get("top_score_gap_to_threshold")
    summary_bits = []
    if bottleneck:
        summary_bits.append(f"bottleneck={bottleneck}")
    if top_score is not None:
        summary_bits.append(f"top_score={top_score}")
    if gap is not None:
        summary_bits.append(f"gap_to_thr={gap}")
    if dead:
        summary_bits.append(f"layers_dead={','.join(dead)}")
    return {
        "severity": severity,
        "primary_target": bottleneck or (dead[0] if dead else None),
        "summary": " · ".join(summary_bits) if summary_bits else None,
        "notes": list(report.get("severity_reasons") or []),
    }


def _extract_layer_heal(payload: Dict[str, Any]) -> Dict[str, Any]:
    sc = payload.get("scorecard") or {}
    severity = _norm_severity(sc.get("overall_severity"))
    primary = None
    layer_stats = sc.get("layer_stats") or []
    worst_rank = -1
    for ls in layer_stats:
        ls_sev = _norm_severity(ls.get("severity"))
        r = SEVERITY_RANK.get(ls_sev, -1)
        if r > worst_rank:
            worst_rank = r
            primary = ls.get("layer")
    summary_bits = []
    if isinstance(layer_stats, list) and layer_stats:
        for ls in layer_stats:
            summary_bits.append(
                f"{ls.get('layer')}:{_norm_severity(ls.get('severity'))[:1]}"
            )
    return {
        "severity": severity,
        "primary_target": primary,
        "summary": " · ".join(summary_bits) if summary_bits else None,
        "notes": [sc.get("note")] if sc.get("note") else [],
    }


def _extract_of_data_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
    severity = _norm_severity(payload.get("severity"))
    bits = []
    cboe = payload.get("cboe_pcr_status")
    if cboe:
        bits.append(f"cboe={cboe[:40]}")
    yf = payload.get("yf_status")
    if yf:
        bits.append(f"yf={yf[:30]}")
    oi = payload.get("oi_coverage_pct")
    if oi is not None:
        bits.append(f"oi_cov={oi}%")
    silent = payload.get("silent_decommission")
    primary = "cboe_pcr" if silent or (severity in {"RED", "BLACK"}) else None
    return {
        "severity": severity,
        "primary_target": primary,
        "summary": " · ".join(bits) if bits else None,
        "notes": list(payload.get("notes") or []),
    }


def _extract_oi_heal(payload: Dict[str, Any]) -> Dict[str, Any]:
    severity = _norm_severity(payload.get("severity"))
    heal = payload.get("heal_success_pct")
    n_cand = payload.get("n_candidates")
    bits = []
    if heal is not None:
        bits.append(f"heal_success={heal}%")
    if n_cand is not None:
        bits.append(f"candidates={n_cand}")
    if payload.get("market_pcr_source"):
        bits.append(f"pcr_src={payload.get('market_pcr_source')}")
    primary = None
    if severity in {"RED", "BLACK"}:
        primary = "yfinance_oi"
    return {
        "severity": severity,
        "primary_target": primary,
        "summary": " · ".join(bits) if bits else None,
        "notes": list(payload.get("notes") or []),
    }


def _extract_threshold_passer(payload: Dict[str, Any]) -> Dict[str, Any]:
    severity = _norm_severity(payload.get("severity"))
    pass_rate = payload.get("pass_rate_pct")
    of_cov = payload.get("of_coverage_pct")
    bits = []
    if pass_rate is not None:
        bits.append(f"pass_rate={pass_rate}%")
    if of_cov is not None:
        bits.append(f"of_cov={of_cov}%")
    notes = list(payload.get("notes") or [])
    primary = None
    for n in notes:
        if "upstream" in n.lower():
            primary = "upstream_score_layer"
            break
        if "of_handover" in n.lower():
            primary = "options_flow_adjust"
            break
    if primary is None and severity in {"RED", "BLACK"}:
        primary = "upstream_score_layer"
    return {
        "severity": severity,
        "primary_target": primary,
        "summary": " · ".join(bits) if bits else None,
        "notes": notes,
    }


def _extract_score_component(payload: Dict[str, Any]) -> Dict[str, Any]:
    severity = _norm_severity(payload.get("overall_severity"))
    primary = payload.get("primary_dead_layer")
    comps = payload.get("components") or []
    bits = []
    for c in comps:
        bits.append(f"{c.get('field')}:{_norm_severity(c.get('severity'))[:1]}")
    return {
        "severity": severity,
        "primary_target": primary,
        "summary": " · ".join(bits) if bits else None,
        "notes": [],
    }


def _extract_fix_session(payload: Dict[str, Any]) -> Dict[str, Any]:
    severity = _norm_severity(payload.get("overall_severity"))
    fixes = payload.get("fixes") or []
    bits = []
    primary = None
    worst_rank = -1
    for fx in fixes:
        sev = _norm_severity(fx.get("severity"))
        bits.append(f"{fx.get('fix')}:{sev[:1]}")
        r = SEVERITY_RANK.get(sev, -1)
        if r > worst_rank:
            worst_rank = r
            primary = fx.get("fix")
    return {
        "severity": severity,
        "primary_target": primary,
        "summary": " · ".join(bits) if bits else None,
        "notes": [],
    }


# Eine Source = (key, prefix, extractor, label)
SOURCES: List[Tuple[str, str, Any, str]] = [
    ("trade_funnel", "trade_funnel_", _extract_trade_funnel, "V7.9.15"),
    ("layer_heal", "layer_heal_", _extract_layer_heal, "V7.9.17"),
    ("of_data_quality", "of_data_quality_", _extract_of_data_quality, "V7.9.18"),
    ("oi_heal_telemetry", "oi_heal_telemetry_", _extract_oi_heal, "V7.9.20"),
    (
        "threshold_passer_coverage",
        "threshold_passer_coverage_",
        _extract_threshold_passer,
        "V7.9.21",
    ),
    (
        "score_component_effectiveness",
        "score_component_effectiveness_",
        _extract_score_component,
        "V7.9.22",
    ),
    ("fix_session_verify", "fix_session_verify_", _extract_fix_session, "V7.9.23"),
]


# ---------------------------------------------------------------------------
# Date-Resolution
# ---------------------------------------------------------------------------


def find_latest_single_day_file(
    prefix: str,
    target_date: str,
    lookback_days: int,
    tools_dir: Path,
) -> Optional[Path]:
    """Sucht in tools_dir nach <prefix><DATE>.json, mit target_date als Startpunkt
    und Lookback bis lookback_days Werktage zurueck.

    "Range"-Files (<prefix>range_<...>) werden ignoriert.
    """
    target = _dt.date.fromisoformat(target_date)
    candidates: List[_dt.date] = []
    cur = target
    while len(candidates) <= lookback_days:
        candidates.append(cur)
        cur = cur - _dt.timedelta(days=1)
    for d in candidates:
        path = tools_dir / f"{prefix}{d.isoformat()}.json"
        # Range-Files explizit ausschliessen (defensive)
        if "range" in path.name:
            continue
        if path.exists():
            return path
    return None


# ---------------------------------------------------------------------------
# Build-Rollup
# ---------------------------------------------------------------------------


def build_source_row(
    key: str,
    prefix: str,
    extractor: Any,
    label: str,
    target_date: str,
    lookback_days: int,
    tools_dir: Path,
) -> Dict[str, Any]:
    """Findet jueengste Single-Day-Datei, laedt, normalisiert."""
    path = find_latest_single_day_file(prefix, target_date, lookback_days, tools_dir)
    row: Dict[str, Any] = {
        "tool": key,
        "version": label,
        "file": None,
        "latest_date": None,
        "age_days": None,
        "available": False,
        "severity": "UNAVAILABLE",
        "primary_target": None,
        "summary": None,
        "notes": [],
        "unavailable_reason": None,
    }
    if path is None:
        row["unavailable_reason"] = f"no_file_within_{lookback_days}d"
        return row
    payload, info = safe_load_json(path)
    if payload is None:
        if info.get("file_missing"):
            row["unavailable_reason"] = "file_missing_after_resolve"
        elif info.get("parse_error"):
            row["unavailable_reason"] = "parse_error"
        elif info.get("transient_io"):
            row["unavailable_reason"] = "transient_io_after_retries"
        else:
            row["unavailable_reason"] = "io_error"
        row["io_attempts"] = info.get("attempts")
        row["last_error"] = info.get("last_error")
        try:
            row["file"] = str(path.relative_to(REPO_ROOT))
        except ValueError:
            row["file"] = str(path)
        return row
    # Datum aus Dateiname extrahieren (10 Zeichen am Ende vor .json)
    fname = path.name
    iso = fname[len(prefix) : len(prefix) + 10]
    try:
        latest = _dt.date.fromisoformat(iso)
        age = (_dt.date.fromisoformat(target_date) - latest).days
    except ValueError:
        latest = None
        age = None
    try:
        row["file"] = str(path.relative_to(REPO_ROOT))
    except ValueError:
        row["file"] = str(path)
    row["latest_date"] = latest.isoformat() if latest else None
    row["age_days"] = age
    row["available"] = True
    extracted = extractor(payload) or {}
    row.update(
        {
            "severity": extracted.get("severity", "UNAVAILABLE"),
            "primary_target": extracted.get("primary_target"),
            "summary": extracted.get("summary"),
            "notes": list(extracted.get("notes") or []),
        }
    )
    return row


def aggregate_consensus(
    rows: List[Dict[str, Any]],
    min_tools: int = DEFAULT_MIN_TOOLS,
) -> Dict[str, Any]:
    """Berechnet Synth-Layer (severity_histogram, worst, vote, status, recommendation)."""
    histogram = {"GREEN": 0, "YELLOW": 0, "RED": 0, "BLACK": 0, "UNAVAILABLE": 0}
    available_rows: List[Dict[str, Any]] = []
    for r in rows:
        sev = r.get("severity", "UNAVAILABLE")
        if sev not in histogram:
            sev = "UNAVAILABLE"
        histogram[sev] += 1
        if r.get("available"):
            available_rows.append(r)

    n_available = len(available_rows)
    insufficient = n_available < min_tools

    # worst severity
    worst_rank = -1
    worst_sev = "UNAVAILABLE"
    for r in available_rows:
        sev = r.get("severity", "UNAVAILABLE")
        rk = SEVERITY_RANK.get(sev, -1)
        if rk > worst_rank:
            worst_rank = rk
            worst_sev = sev
    if worst_rank < 0:
        worst_sev = "UNAVAILABLE"

    # primary_target_vote: BLACK=2 Punkte, RED=1
    votes: Dict[str, int] = {}
    for r in available_rows:
        sev = r.get("severity")
        tgt = r.get("primary_target")
        if not tgt:
            continue
        if sev == "BLACK":
            votes[tgt] = votes.get(tgt, 0) + 2
        elif sev == "RED":
            votes[tgt] = votes.get(tgt, 0) + 1
    primary_consensus = None
    if votes:
        primary_consensus = max(votes.items(), key=lambda x: (x[1], x[0]))[0]

    if insufficient:
        consensus_status = "INSUFFICIENT_DATA"
    else:
        consensus_status = worst_sev if worst_sev in SEVERITY_RANK else "UNAVAILABLE"

    recommendation = _build_recommendation(
        consensus_status, available_rows, primary_consensus, votes
    )

    return {
        "n_sources_total": len(rows),
        "n_sources_available": n_available,
        "min_tools_required": min_tools,
        "insufficient_data": insufficient,
        "severity_histogram": histogram,
        "worst_severity": worst_sev,
        "primary_target_vote": votes,
        "primary_target_consensus": primary_consensus,
        "consensus_status": consensus_status,
        "sprint_recommendation": recommendation,
    }


def _build_recommendation(
    status: str,
    rows: List[Dict[str, Any]],
    primary: Optional[str],
    votes: Dict[str, int],
) -> Dict[str, Any]:
    """Heuristische, datengetriebene Sprint-Empfehlung."""
    if status == "INSUFFICIENT_DATA":
        return {
            "headline": "Zu wenige Diagnose-Quellen: Sprint-Empfehlung deferred",
            "rationale": "min_tools nicht erreicht; erst Diagnose-Cron-Health pruefen.",
            "levers": [],
        }
    if status == "GREEN":
        return {
            "headline": "Pipeline-Gesundheit GREEN, kein Sprint-Trigger",
            "rationale": "Alle verfuegbaren Diagnose-Tools im gruenen Bereich.",
            "levers": [],
        }
    if status == "YELLOW":
        return {
            "headline": "Leichte Degradation: Beobachtung, kein Notfall-Sprint",
            "rationale": "Keine BLACK-Quelle, Layer haelt insgesamt.",
            "levers": ["watchdog_continue"],
        }
    # RED oder BLACK → primary anschauen
    headline_map = {
        "options_flow_adjust": "Wurzel CBOE-403 + yfinance-OI · Layer options_flow_adjust",
        "market_pcr_adjust": "Wurzel CBOE-403 + yfinance-OI · Layer market_pcr_adjust",
        "options_flow": "Wurzel CBOE/yfinance · Layer options_flow",
        "insider_adjust": "Wurzel insider_feed · Tier-Filter / openinsider",
        "insider": "Wurzel insider_feed · Tier-Filter / openinsider",
        "cboe_pcr": "Wurzel CBOE-403 · Decommission-Hypothese",
        "yfinance_oi": "Wurzel yfinance-OI · Payload-Heal greift, Daten dahinter tot",
        "upstream_score_layer": "Score-Layer upstream · Threshold-Senkung unwirksam (V7.9.16)",
        "score_pass": "Funnel-Bottleneck = score_pass · Top-Score < Threshold",
    }
    headline = headline_map.get(
        primary or "",
        f"Bottleneck = {primary or 'unbekannt'} · Mehrstufige Diagnose noetig",
    )
    levers: List[str] = []
    if primary in {"options_flow_adjust", "market_pcr_adjust", "cboe_pcr", "yfinance_oi"}:
        levers.append("KW21-Bugfix-Tag: CBOE-Source-Decommission + yfinance-OI-Replacement")
        levers.append("Optional: Barchart-Scraping V6.8.2 wenn yfinance OI weiter stumm")
    if primary in {"insider_adjust", "insider"}:
        levers.append("INSIDER-CRON-VERIFY-1: Tier-Filter + openinsider-Source")
    if primary in {"upstream_score_layer", "score_pass"}:
        levers.append("Score-Layer pruefen, NICHT Threshold senken (V7.9.16 negativ)")
    if not levers:
        levers.append("Diagnose pro Tool durchgehen, primary nicht eindeutig identifiziert")
    return {
        "headline": headline,
        "rationale": _build_rationale(rows, primary, votes),
        "levers": levers,
    }


def _build_rationale(
    rows: List[Dict[str, Any]],
    primary: Optional[str],
    votes: Dict[str, int],
) -> str:
    """Kurze Begruendung in 1-2 Saetzen."""
    black_tools = [r["tool"] for r in rows if r.get("severity") == "BLACK"]
    red_tools = [r["tool"] for r in rows if r.get("severity") == "RED"]
    bits = []
    if black_tools:
        bits.append(f"BLACK: {', '.join(black_tools)}")
    if red_tools:
        bits.append(f"RED: {', '.join(red_tools)}")
    if primary:
        bits.append(f"primary_target={primary}")
    if votes:
        top_vote = max(votes.items(), key=lambda x: (x[1], x[0]))
        bits.append(f"vote-top={top_vote[0]}({top_vote[1]}p)")
    return " · ".join(bits) if bits else "Keine BLACK/RED-Quelle identifiziert."


# ---------------------------------------------------------------------------
# Run / IO
# ---------------------------------------------------------------------------


def run_rollup(
    target_date: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_tools: int = DEFAULT_MIN_TOOLS,
    tools_dir: Path = TOOLS_DIR,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for key, prefix, extractor, label in SOURCES:
        row = build_source_row(
            key, prefix, extractor, label, target_date, lookback_days, tools_dir
        )
        rows.append(row)
    synth = aggregate_consensus(rows, min_tools=min_tools)
    return {
        "tool": TOOL_NAME,
        "version": VERSION,
        "target_date": target_date,
        "lookback_days": lookback_days,
        "min_tools_required": min_tools,
        "sources": rows,
        "synth": synth,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
    }


def render_markdown(rollup: Dict[str, Any]) -> str:
    target = rollup.get("target_date", "?")
    synth = rollup.get("synth", {})
    rows = rollup.get("sources", [])
    consensus = synth.get("consensus_status", "?")
    primary = synth.get("primary_target_consensus") or "-"
    hist = synth.get("severity_histogram", {})
    rec = synth.get("sprint_recommendation", {})

    lines: List[str] = []
    lines.append(f"# Diagnostic-Rollup: {target}")
    lines.append("")
    lines.append(
        f"**Tool:** {TOOL_NAME} {VERSION} · "
        f"Lookback: {rollup.get('lookback_days', '?')} Werktage · "
        f"min_tools={rollup.get('min_tools_required', '?')}"
    )
    lines.append("")
    lines.append(f"## Konsens: **{consensus}**")
    lines.append("")
    lines.append(
        "| Severity | GREEN | YELLOW | RED | BLACK | UNAVAILABLE |"
    )
    lines.append("|---|---|---|---|---|---|")
    lines.append(
        f"| Anzahl | {hist.get('GREEN', 0)} | {hist.get('YELLOW', 0)} | "
        f"{hist.get('RED', 0)} | {hist.get('BLACK', 0)} | "
        f"{hist.get('UNAVAILABLE', 0)} |"
    )
    lines.append("")
    lines.append(f"**Primary-Target-Consensus:** `{primary}`")
    votes = synth.get("primary_target_vote") or {}
    if votes:
        vote_bits = ", ".join(f"{k}={v}" for k, v in sorted(votes.items(), key=lambda x: -x[1]))
        lines.append("")
        lines.append(f"**Vote-Detail:** {vote_bits}")
    lines.append("")
    lines.append("## Sprint-Recommendation")
    lines.append("")
    lines.append(f"- **Headline:** {rec.get('headline', '-')}")
    lines.append(f"- **Begruendung:** {rec.get('rationale', '-')}")
    levers = rec.get("levers") or []
    if levers:
        lines.append("- **Hebel:**")
        for lv in levers:
            lines.append(f"  - {lv}")
    lines.append("")
    lines.append("## Quellen-Detail")
    lines.append("")
    lines.append(
        "| Tool | Version | latest | age (d) | Severity | primary_target | summary |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        latest = r.get("latest_date") or "-"
        age = r.get("age_days")
        age_str = "-" if age is None else str(age)
        sev = r.get("severity", "UNAVAILABLE")
        if not r.get("available"):
            sev = f"UNAVAILABLE ({r.get('unavailable_reason', '?')})"
        pt = r.get("primary_target") or "-"
        smry = (r.get("summary") or "-").replace("|", "\\|")
        if len(smry) > 90:
            smry = smry[:87] + "..."
        lines.append(
            f"| `{r.get('tool', '?')}` | {r.get('version', '?')} | "
            f"{latest} | {age_str} | {sev} | `{pt}` | {smry} |"
        )
    lines.append("")
    lines.append("---")
    lines.append(
        f"_Generated {rollup.get('generated_utc', '?')} · "
        f"FEATURE V7.9.25 · read-only · freeze-konform_"
    )
    lines.append("")
    return "\n".join(lines)


def _strict_exit_code(consensus_status: str) -> int:
    if consensus_status == "INSUFFICIENT_DATA":
        return 3
    if consensus_status == "BLACK":
        return 2
    if consensus_status == "RED":
        return 1
    return 0


# ---------------------------------------------------------------------------
# V7.9.26 Range-Mode (NEU)
# ---------------------------------------------------------------------------


def trading_days_between(start_date: str, end_date: str) -> List[str]:
    """Liefert sortierte Liste aller Werktage (Mo-Fr) zwischen start_date und
    end_date (jeweils inklusiv) als ISO-Strings. Wenn start > end → leere Liste.
    """
    try:
        s = _dt.date.fromisoformat(start_date)
        e = _dt.date.fromisoformat(end_date)
    except ValueError:
        return []
    if s > e:
        return []
    out: List[str] = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


def _longest_consecutive(flags: List[bool]) -> int:
    """Laengster zusammenhaengender True-Block in flags."""
    longest = 0
    cur = 0
    for f in flags:
        if f:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    return longest


def aggregate_range(
    per_day: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregiert per-Day-Rollups (Liste der run_rollup()-Outputs) zu einer
    Range-Synthese.

    per_day muss in chronologischer Reihenfolge sein (aufsteigendes Datum).
    """
    n_days = len(per_day)

    consensus_history: List[Dict[str, Any]] = []
    worst_rank = -1
    worst_consensus = "UNAVAILABLE"
    primary_target_counts: Dict[str, int] = {}
    n_insufficient = 0
    n_with_consensus = 0

    # Pro-Tool-Sammler
    tool_severities: Dict[str, List[str]] = {}

    consensus_flags_black: List[bool] = []
    consensus_flags_red_or_black: List[bool] = []

    for day in per_day:
        date = day.get("target_date", "?")
        synth = day.get("synth", {}) or {}
        status = synth.get("consensus_status", "UNAVAILABLE")
        primary = synth.get("primary_target_consensus")
        consensus_history.append({
            "date": date,
            "consensus_status": status,
            "primary_target_consensus": primary,
        })

        if status == "INSUFFICIENT_DATA":
            n_insufficient += 1
            consensus_flags_black.append(False)
            consensus_flags_red_or_black.append(False)
        else:
            n_with_consensus += 1
            rk = SEVERITY_RANK.get(status, -1)
            if rk > worst_rank:
                worst_rank = rk
                worst_consensus = status
            if primary:
                primary_target_counts[primary] = (
                    primary_target_counts.get(primary, 0) + 1
                )
            consensus_flags_black.append(status == "BLACK")
            consensus_flags_red_or_black.append(status in ("RED", "BLACK"))

        # per-Tool-Severities sammeln
        for src in day.get("sources", []) or []:
            tool = src.get("tool")
            if not tool:
                continue
            sev = src.get("severity", "UNAVAILABLE")
            if not src.get("available", False):
                sev = "UNAVAILABLE"
            tool_severities.setdefault(tool, []).append(sev)

    if worst_rank < 0:
        worst_consensus = "INSUFFICIENT_DATA" if n_days > 0 else "UNAVAILABLE"

    worst_severity_streak = _longest_consecutive(consensus_flags_black)
    red_or_black_streak = _longest_consecutive(consensus_flags_red_or_black)

    # Dominant primary_target + vote_persistence
    dominant_primary_target: Optional[str] = None
    if primary_target_counts:
        dominant_primary_target = max(
            primary_target_counts.items(),
            key=lambda x: (x[1], x[0]),
        )[0]
    if n_with_consensus > 0 and dominant_primary_target:
        dom_count = primary_target_counts.get(dominant_primary_target, 0)
        vote_persistence_pct = round(100.0 * dom_count / n_with_consensus, 1)
    else:
        vote_persistence_pct = 0.0

    # Per-Tool-Aggregate
    per_tool: List[Dict[str, Any]] = []
    for tool, sevs in tool_severities.items():
        hist = {"GREEN": 0, "YELLOW": 0, "RED": 0, "BLACK": 0, "UNAVAILABLE": 0}
        for s in sevs:
            if s in hist:
                hist[s] += 1
            else:
                hist["UNAVAILABLE"] += 1
        black_flags = [s == "BLACK" for s in sevs]
        avail = sum(1 for s in sevs if s != "UNAVAILABLE")
        per_tool.append({
            "tool": tool,
            "n_days": len(sevs),
            "severity_distribution": hist,
            "longest_black_streak": _longest_consecutive(black_flags),
            "availability_pct": round(100.0 * avail / len(sevs), 1) if sevs else 0.0,
        })
    per_tool.sort(key=lambda r: r["tool"])

    range_rec = _build_range_recommendation(
        worst_consensus=worst_consensus,
        worst_severity_streak=worst_severity_streak,
        red_or_black_streak=red_or_black_streak,
        vote_persistence_pct=vote_persistence_pct,
        dominant_primary_target=dominant_primary_target,
        n_days=n_days,
        n_with_consensus=n_with_consensus,
    )

    return {
        "n_days": n_days,
        "n_with_consensus": n_with_consensus,
        "n_insufficient_data": n_insufficient,
        "consensus_status_history": consensus_history,
        "worst_consensus_in_window": worst_consensus,
        "worst_severity_streak": worst_severity_streak,
        "red_or_black_streak": red_or_black_streak,
        "primary_target_counts": primary_target_counts,
        "dominant_primary_target": dominant_primary_target,
        "vote_persistence_pct": vote_persistence_pct,
        "per_tool": per_tool,
        "range_recommendation": range_rec,
    }


def _build_range_recommendation(
    worst_consensus: str,
    worst_severity_streak: int,
    red_or_black_streak: int,
    vote_persistence_pct: float,
    dominant_primary_target: Optional[str],
    n_days: int,
    n_with_consensus: int,
) -> Dict[str, Any]:
    """Range-Heuristik:

    - Wenn ueberhaupt keine Konsens-Tage: deferred
    - BLACK-Streak >= 3 UND vote_persistence_pct >= 60 → sprint_trigger
    - sonst RED_OR_BLACK_Streak >= 3 → watchdog mit Hinweis
    - sonst keep_observing
    """
    if n_with_consensus == 0:
        return {
            "verdict": "deferred",
            "headline": "Range ohne Konsens-Tage: Diagnose-Cron-Health pruefen",
            "rationale": (
                "Alle Tage INSUFFICIENT_DATA: min_tools wurde an keinem "
                "Tag erreicht."
            ),
            "levers": ["diagnostic_cron_health_check"],
            "sprint_trigger": False,
        }

    if worst_severity_streak >= 3 and vote_persistence_pct >= 60.0:
        return {
            "verdict": "sprint_trigger",
            "headline": (
                f"BLACK-Streak {worst_severity_streak}d + "
                f"Konsens {vote_persistence_pct}% → "
                f"strukturell, KW21-Sprint scharf"
            ),
            "rationale": (
                f"Dominant primary_target='{dominant_primary_target}' an "
                f"{vote_persistence_pct}% der Konsens-Tage, BLACK-Streak "
                f"{worst_severity_streak} Werktage in Folge."
            ),
            "levers": _levers_for_primary(dominant_primary_target),
            "sprint_trigger": True,
        }

    if red_or_black_streak >= 3:
        return {
            "verdict": "watchdog_elevated",
            "headline": (
                f"RED-oder-BLACK-Streak {red_or_black_streak}d: "
                f"Beobachtung verschaerft"
            ),
            "rationale": (
                f"Worst {worst_consensus}; dominanter primary_target="
                f"'{dominant_primary_target}', Persistence "
                f"{vote_persistence_pct}%."
            ),
            "levers": ["watchdog_continue"] + _levers_for_primary(
                dominant_primary_target
            ),
            "sprint_trigger": False,
        }

    if worst_consensus == "BLACK":
        return {
            "verdict": "isolated_black",
            "headline": "Einzelner BLACK-Tag ohne strukturellen Streak",
            "rationale": (
                f"BLACK-Streak nur {worst_severity_streak}d: wirkt wie "
                f"Tagesrauschen. Konsens-Persistence {vote_persistence_pct}%."
            ),
            "levers": ["root_cause_check_one_day", "no_sprint_yet"],
            "sprint_trigger": False,
        }

    if worst_consensus == "RED":
        return {
            "verdict": "isolated_red",
            "headline": "Leichte Degradation in der Range",
            "rationale": (
                f"Worst RED, kein BLACK; Streak {red_or_black_streak}d."
            ),
            "levers": ["watchdog_continue"],
            "sprint_trigger": False,
        }

    return {
        "verdict": "stable",
        "headline": "Range GREEN/YELLOW: Pipeline gesund",
        "rationale": (
            f"Worst {worst_consensus}, keine BLACK-Tage in der Range."
        ),
        "levers": [],
        "sprint_trigger": False,
    }


def _levers_for_primary(primary: Optional[str]) -> List[str]:
    if not primary:
        return ["diagnose_primary_target_unbekannt"]
    if primary in {"options_flow_adjust", "market_pcr_adjust", "cboe_pcr", "yfinance_oi"}:
        return [
            "KW21-Bugfix-Tag: CBOE-Source-Decommission + yfinance-OI-Replacement",
            "Optional: Barchart-Scraping V6.8.2 wenn yfinance OI weiter stumm",
        ]
    if primary in {"insider_adjust", "insider"}:
        return ["INSIDER-CRON-VERIFY-1: Tier-Filter + openinsider-Source"]
    if primary in {"upstream_score_layer", "score_pass"}:
        return ["Score-Layer pruefen, NICHT Threshold senken (V7.9.16 negativ)"]
    if primary in {"FIX-A", "FIX-B", "FIX-C", "FIX-D"}:
        return [f"heal_{primary.lower().replace('-', '_')}"]
    return [f"primary={primary}: Hebel-Mapping unbekannt"]


def run_range_rollup(
    start_date: str,
    end_date: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_tools: int = DEFAULT_MIN_TOOLS,
    tools_dir: Path = TOOLS_DIR,
) -> Dict[str, Any]:
    days = trading_days_between(start_date, end_date)
    per_day: List[Dict[str, Any]] = []
    for d in days:
        per_day.append(
            run_rollup(
                d,
                lookback_days=lookback_days,
                min_tools=min_tools,
                tools_dir=tools_dir,
            )
        )
    range_synth = aggregate_range(per_day)
    return {
        "tool": TOOL_NAME,
        "version": VERSION,
        "mode": "range",
        "start_date": start_date,
        "end_date": end_date,
        "lookback_days": lookback_days,
        "min_tools_required": min_tools,
        "n_trading_days": len(days),
        "trading_days": days,
        "per_day": per_day,
        "range_synth": range_synth,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
    }


def render_markdown_range(range_rollup: Dict[str, Any]) -> str:
    s = range_rollup.get("start_date", "?")
    e = range_rollup.get("end_date", "?")
    rs = range_rollup.get("range_synth", {}) or {}
    days = range_rollup.get("trading_days", []) or []

    lines: List[str] = []
    lines.append(f"# Diagnostic-Rollup Range: {s} bis {e}")
    lines.append("")
    lines.append(
        f"**Tool:** {TOOL_NAME} {VERSION} · "
        f"Werktage in Range: **{len(days)}** · "
        f"Lookback/Tag: {range_rollup.get('lookback_days', '?')} · "
        f"min_tools={range_rollup.get('min_tools_required', '?')}"
    )
    lines.append("")
    lines.append(f"## Range-Verdikt: **{rs.get('worst_consensus_in_window', '?')}**")
    lines.append("")
    rec = rs.get("range_recommendation", {}) or {}
    lines.append(f"- **Verdikt:** `{rec.get('verdict', '-')}`")
    lines.append(f"- **Headline:** {rec.get('headline', '-')}")
    lines.append(f"- **Begruendung:** {rec.get('rationale', '-')}")
    lines.append(
        f"- **Sprint-Trigger:** "
        f"{'JA' if rec.get('sprint_trigger') else 'nein'}"
    )
    levers = rec.get("levers") or []
    if levers:
        lines.append("- **Hebel:**")
        for lv in levers:
            lines.append(f"  - {lv}")
    lines.append("")
    lines.append("## Range-Kennzahlen")
    lines.append("")
    lines.append(
        f"- **Tage gesamt / mit Konsens / INSUFFICIENT_DATA:** "
        f"{rs.get('n_days', 0)} / "
        f"{rs.get('n_with_consensus', 0)} / "
        f"{rs.get('n_insufficient_data', 0)}"
    )
    lines.append(
        f"- **BLACK-Streak (laengste):** "
        f"{rs.get('worst_severity_streak', 0)} Werktage"
    )
    lines.append(
        f"- **RED-oder-BLACK-Streak (laengste):** "
        f"{rs.get('red_or_black_streak', 0)} Werktage"
    )
    lines.append(
        f"- **Dominant primary_target:** "
        f"`{rs.get('dominant_primary_target') or '-'}` "
        f"(Persistence {rs.get('vote_persistence_pct', 0.0)}%)"
    )
    counts = rs.get("primary_target_counts") or {}
    if counts:
        bits = ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])
        )
        lines.append(f"- **Primary-Target-Counts:** {bits}")
    lines.append("")
    lines.append("## Tagesverlauf Konsens")
    lines.append("")
    lines.append("| Datum | Konsens | primary_target |")
    lines.append("|---|---|---|")
    for h in rs.get("consensus_status_history", []) or []:
        lines.append(
            f"| {h.get('date', '?')} | "
            f"{h.get('consensus_status', '?')} | "
            f"`{h.get('primary_target_consensus') or '-'}` |"
        )
    lines.append("")
    lines.append("## Per-Tool-Aggregate")
    lines.append("")
    lines.append(
        "| Tool | n_days | G | Y | R | B | UNAVAIL | longest_BLACK_streak | availability_% |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for pt in rs.get("per_tool", []) or []:
        hist = pt.get("severity_distribution", {}) or {}
        lines.append(
            f"| `{pt.get('tool', '?')}` | "
            f"{pt.get('n_days', 0)} | "
            f"{hist.get('GREEN', 0)} | "
            f"{hist.get('YELLOW', 0)} | "
            f"{hist.get('RED', 0)} | "
            f"{hist.get('BLACK', 0)} | "
            f"{hist.get('UNAVAILABLE', 0)} | "
            f"{pt.get('longest_black_streak', 0)} | "
            f"{pt.get('availability_pct', 0.0)}% |"
        )
    lines.append("")
    lines.append("---")
    lines.append(
        f"_Generated {range_rollup.get('generated_utc', '?')} · "
        f"FEATURE V7.9.26 · read-only · freeze-konform_"
    )
    lines.append("")
    return "\n".join(lines)


def _range_strict_exit_code(range_synth: Dict[str, Any]) -> int:
    worst = range_synth.get("worst_consensus_in_window")
    if worst == "BLACK":
        return 2
    if worst == "RED":
        return 1
    if worst in ("INSUFFICIENT_DATA", "UNAVAILABLE", None):
        # Wenn alle Tage INSUFFICIENT → 3
        if range_synth.get("n_with_consensus", 0) == 0:
            return 3
        return 0
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"{TOOL_NAME} {VERSION} - Diagnostic Rollup",
    )
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument(
        "--start-date",
        default=None,
        help="Range-Mode (V7.9.26): Beginn (inklusiv) der Werktage-Range.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help=(
            "Range-Mode (V7.9.26): Ende (inklusiv) der Werktage-Range. "
            "Defaults zu --date wenn nur --start-date gegeben ist."
        ),
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS
    )
    parser.add_argument("--min-tools", type=int, default=DEFAULT_MIN_TOOLS)
    parser.add_argument("--tools-dir", default=str(TOOLS_DIR))
    parser.add_argument("--out-dir", default=str(TOOLS_DIR))
    parser.add_argument("--briefings-dir", default=str(BRIEFINGS_DIR))
    parser.add_argument("--no-md", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Single-Day Exit-Codes 0 GREEN/YELLOW, 1 RED, 2 BLACK, "
            "3 INSUFFICIENT_DATA. Range-Mode mappt worst_consensus analog."
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    tools_dir = Path(args.tools_dir)
    out_dir = Path(args.out_dir)
    briefings_dir = Path(args.briefings_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    briefings_dir.mkdir(parents=True, exist_ok=True)

    # Range-Mode aktiviert, sobald --start-date gesetzt ist
    range_mode = bool(args.start_date)

    if range_mode:
        start = args.start_date
        end = args.end_date or args.date
        range_rollup = run_range_rollup(
            start,
            end,
            lookback_days=args.lookback_days,
            min_tools=args.min_tools,
            tools_dir=tools_dir,
        )
        range_synth = range_rollup.get("range_synth", {}) or {}
        out_json = out_dir / f"{TOOL_NAME}_range_{start}_{end}.json"
        with out_json.open("w", encoding="utf-8") as fh:
            json.dump(range_rollup, fh, indent=2, ensure_ascii=False)
        if not args.quiet:
            print(f"[{TOOL_NAME}] wrote {out_json}")

        if not args.no_md:
            md = render_markdown_range(range_rollup)
            md_path = briefings_dir / f"Diagnostic-Rollup-Range-{start}_{end}.md"
            with md_path.open("w", encoding="utf-8") as fh:
                fh.write(md)
            if not args.quiet:
                print(f"[{TOOL_NAME}] wrote {md_path}")

        if not args.quiet:
            worst = range_synth.get("worst_consensus_in_window", "?")
            verdict = (range_synth.get("range_recommendation") or {}).get(
                "verdict", "?"
            )
            print(
                f"[{TOOL_NAME}] range worst={worst} · verdict={verdict} · "
                f"days={range_rollup.get('n_trading_days', 0)}"
            )

        if args.strict:
            return _range_strict_exit_code(range_synth)
        return 0

    # Single-Day-Pfad (V7.9.25-Verhalten)
    rollup = run_rollup(
        args.date,
        lookback_days=args.lookback_days,
        min_tools=args.min_tools,
        tools_dir=tools_dir,
    )
    consensus_status = rollup["synth"]["consensus_status"]

    out_json = out_dir / f"{TOOL_NAME}_{args.date}.json"
    with out_json.open("w", encoding="utf-8") as fh:
        json.dump(rollup, fh, indent=2, ensure_ascii=False)
    if not args.quiet:
        print(f"[{TOOL_NAME}] wrote {out_json}")

    if not args.no_md:
        md = render_markdown(rollup)
        md_path = briefings_dir / f"Diagnostic-Rollup-{args.date}.md"
        with md_path.open("w", encoding="utf-8") as fh:
            fh.write(md)
        if not args.quiet:
            print(f"[{TOOL_NAME}] wrote {md_path}")

    if not args.quiet:
        print(f"[{TOOL_NAME}] consensus={consensus_status}")

    if args.strict:
        return _strict_exit_code(consensus_status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
