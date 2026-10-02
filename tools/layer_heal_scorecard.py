"""
V7.9.17 LAYER-HEAL-SCORECARD-1 (FEATURE, KW18 Do 2026-04-30)
=============================================================

Read-only Layer-Heal-Scorecard als direkter Anschluss an V7.9.15
TRADE-FUNNEL-DIAGNOSTIC und V7.9.16 THRESHOLD-SENSITIVITY-ANALYZER.

Workshop-Kontext (29.04. 20:30 CET):
  V7.9.15 hat gezeigt: Bottleneck ist `score_pass` mit Top-Score-Plateau
  5.89-6.77 vs Threshold 7.5.
  V7.9.16 hat gezeigt: + MTF + 3 Layer = 0 bei JEDEM Threshold (auch 5.0).
  Maschinelle Workshop-Empfehlung: **Bahn B (Layer-Heal) statt Bahn A
  (Threshold-Senken)**.

  Damit ist die naechste Workshop-/Engineering-Frage:
       "Wenn Layer-Heal die Empfehlung ist, WELCHEN Layer fixen wir
        zuerst, und welcher Fix hat den groessten Unblock-Hebel?"

  V7.9.17 beantwortet exakt diese Frage maschinenlesbar.

Was dieses Tool NICHT macht:
  - Es schreibt KEINE Recommendations.
  - Es aendert KEINEN Scoring-Code.
  - Es ist read-only und freeze-konform.
  - Es verbraucht KEINE KW18-Bugfix-Quote (FEATURE).

Was es macht:
  - Pro Tag aus scan_<DATE>.json:
      * Coverage_pct pro Layer (insider / options_flow / sector_rs)
      * Layer-Distribution (layers_full=3 / layers_2 / layers_1 / layers_0)
      * MTF-Good-Count (bullish|primed, bullish|neutral, neutral|primed)
      * Severity-Ampel pro Layer (GREEN/YELLOW/RED/BLACK)
  - Range:
      * Median Coverage pro Layer
      * Days_since_last_clean pro Layer (laengster Streak >= 50% Coverage)
      * Unblock_potential pro Layer:
          "Wenn dieser Layer heute 100% waere statt 0%, wieviele
           Setups haetten + MTF + 3 Layer = 1 statt = 0?"
        Das ist der direkte Hebel-Score fuer Sprint-Prio.
      * Empfohlene Fix-Reihenfolge (1=sofort, 2=naechste Welle, 3=spaeter)

Output:
  Single-Day:
    - tools/layer_heal_<DATE>.json
    - Briefings/Layer-Heal-Scorecard-<DATE>.md
  Range:
    - tools/layer_heal_range_<START>_<END>.json
    - Briefings/Layer-Heal-Range-<START>_<END>.md

CLI:
  python3 tools/layer_heal_scorecard.py
  python3 tools/layer_heal_scorecard.py --date 2026-04-29
  python3 tools/layer_heal_scorecard.py --range 2026-04-15:2026-04-30
  python3 tools/layer_heal_scorecard.py --strict
  python3 tools/layer_heal_scorecard.py --no-md

Exit-Codes:
   0  default
   2  --strict + LAYER_BLACK in irgendeinem Layer

Severity-Ampel (Layer-Coverage = % der Top-Setups mit Score != 0):
   GREEN    coverage >= 80%
   YELLOW   coverage 40..79%
   RED      coverage 10..39%
   BLACK    coverage <  10%

Reine stdlib (csv, json, argparse, datetime, pathlib, statistics).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VERSION = "1.0"

# Drei Confluence-Layer V6.1, in Reihenfolge ihrer Auftauch-Reihenfolge im Score
LAYERS = ("insider", "options_flow", "sector_rs")

# Map auf scan_<DATE>.json-Felder
LAYER_FIELDS: Dict[str, str] = {
    "insider": "insider_score",
    "options_flow": "options_flow_score",
    "sector_rs": "sector_rs_score",
}

# MTF-Tags die als "bestanden" zaehlen (siehe V7.9.16 / V7.9.15)
MTF_GOOD = {"bullish|primed", "bullish|neutral", "neutral|primed"}

# Severity-Schwellen fuer Layer-Coverage (% der Top-Setups mit nicht-null Layer-Score)
SEV_GREEN = 0.80
SEV_YELLOW = 0.40
SEV_RED = 0.10

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -------------------------------------------------------------------------- #
# Helper                                                                     #
# -------------------------------------------------------------------------- #

def _parse_iso(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5  # Mo..Fr


def _expand_range(start: date, end: date) -> List[date]:
    out: List[date] = []
    cur = start
    while cur <= end:
        if _is_weekday(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _safe_score(setup: Dict[str, Any], field_name: str) -> float:
    v = setup.get(field_name)
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _layer_active(setup: Dict[str, Any], layer: str) -> bool:
    """True wenn Layer-Score != 0.0."""
    val = _safe_score(setup, LAYER_FIELDS[layer])
    return abs(val) > 1e-9


def _mtf_good(setup: Dict[str, Any]) -> bool:
    return setup.get("mtf_tag") in MTF_GOOD


# -------------------------------------------------------------------------- #
# Severity                                                                   #
# -------------------------------------------------------------------------- #

def severity_for_coverage(coverage_pct: float) -> str:
    """coverage_pct: 0.0..1.0 -> GREEN/YELLOW/RED/BLACK."""
    if coverage_pct >= SEV_GREEN:
        return "GREEN"
    if coverage_pct >= SEV_YELLOW:
        return "YELLOW"
    if coverage_pct >= SEV_RED:
        return "RED"
    return "BLACK"


def severity_rank(sev: str) -> int:
    """Hoeher = schlimmer."""
    return {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3}.get(sev, 0)


# -------------------------------------------------------------------------- #
# Single-Day Analyse                                                         #
# -------------------------------------------------------------------------- #

@dataclass
class LayerStat:
    layer: str
    coverage_pct: float
    non_zero: int
    total: int
    severity: str


@dataclass
class DayScorecard:
    date: str
    available: bool
    universe_size: int
    valid_results: int
    top_setups_count: int
    layer_stats: List[LayerStat] = field(default_factory=list)
    layers_full: int = 0
    layers_2: int = 0
    layers_1: int = 0
    layers_0: int = 0
    mtf_good_count: int = 0
    overall_severity: str = "GREEN"
    note: Optional[str] = None


def analyze_day_scan(scan: Dict[str, Any]) -> DayScorecard:
    """Aus einem scan_<DATE>.json-Dict eine DayScorecard berechnen."""
    scan_date = scan.get("scan_date") or ""
    universe_size = int(scan.get("universe_size") or 0)
    valid_results = int(scan.get("valid_results") or 0)
    top_setups = scan.get("top_setups") or []
    n = len(top_setups)

    if n == 0:
        return DayScorecard(
            date=scan_date,
            available=False,
            universe_size=universe_size,
            valid_results=valid_results,
            top_setups_count=0,
            note="no_top_setups",
        )

    layer_stats: List[LayerStat] = []
    for layer in LAYERS:
        non_zero = sum(1 for s in top_setups if _layer_active(s, layer))
        coverage = non_zero / n if n else 0.0
        layer_stats.append(
            LayerStat(
                layer=layer,
                coverage_pct=round(coverage, 4),
                non_zero=non_zero,
                total=n,
                severity=severity_for_coverage(coverage),
            )
        )

    layers_full = layers_2 = layers_1 = layers_0 = 0
    mtf_good_count = 0
    for s in top_setups:
        active = sum(1 for layer in LAYERS if _layer_active(s, layer))
        if active == 3:
            layers_full += 1
        elif active == 2:
            layers_2 += 1
        elif active == 1:
            layers_1 += 1
        else:
            layers_0 += 1
        if _mtf_good(s):
            mtf_good_count += 1

    # Overall = schlimmster Layer-Status
    overall = max(layer_stats, key=lambda ls: severity_rank(ls.severity)).severity

    return DayScorecard(
        date=scan_date,
        available=True,
        universe_size=universe_size,
        valid_results=valid_results,
        top_setups_count=n,
        layer_stats=layer_stats,
        layers_full=layers_full,
        layers_2=layers_2,
        layers_1=layers_1,
        layers_0=layers_0,
        mtf_good_count=mtf_good_count,
        overall_severity=overall,
    )


# -------------------------------------------------------------------------- #
# Unblock-Potential                                                          #
# -------------------------------------------------------------------------- #

def compute_unblock_potential(scan: Dict[str, Any]) -> Dict[str, int]:
    """
    Pro Layer: wieviele Setups wuerden + MTF + 3 Layer = 1 ERREICHEN, wenn
    dieser Layer 100%-aktiv waere und alle anderen Layer-/MTF-Stati gleich
    blieben?

    Konkret: zaehle Setups, die aktuell:
      - mtf_good == True
      - die anderen beiden Layer sind aktiv
      - dieser eine Layer ist NICHT aktiv
    Wenn dieser Layer aktiv waere, wuerden sie das + MTF + 3 Layer-Filter
    bestehen.

    Zusaetzlich: lockerere Variante (`*_loose`) zaehlt Setups, die nur
    diesen einen Layer brauchen, um auf "alle 3 Layer aktiv" zu kommen,
    OHNE MTF-Constraint. Das gibt einen oberen Hebel-Bound.
    """
    top = scan.get("top_setups") or []
    out: Dict[str, int] = {}
    for layer in LAYERS:
        n_strict = 0
        n_loose = 0
        others = [l for l in LAYERS if l != layer]
        for s in top:
            this_active = _layer_active(s, layer)
            others_active = all(_layer_active(s, o) for o in others)
            if (not this_active) and others_active:
                n_loose += 1
                if _mtf_good(s):
                    n_strict += 1
        out[layer + "_strict"] = n_strict
        out[layer + "_loose"] = n_loose
    return out


# -------------------------------------------------------------------------- #
# Range-Aggregat                                                             #
# -------------------------------------------------------------------------- #

@dataclass
class RangeLayerSummary:
    layer: str
    median_coverage_pct: float
    mean_coverage_pct: float
    days_above_50: int
    days_below_10: int
    longest_black_streak: int
    severity_aggregate: str
    unblock_potential_strict_total: int
    unblock_potential_loose_total: int


@dataclass
class RangeScorecard:
    start: str
    end: str
    days_total: int
    days_with_data: int
    days_missing: List[str]
    layer_summaries: List[RangeLayerSummary]
    median_layers_full: float
    median_layers_0: float
    median_mtf_good: float
    overall_severity: str
    recommendation: Dict[str, Any]


def _streak_max(seq: List[bool]) -> int:
    cur = mx = 0
    for v in seq:
        if v:
            cur += 1
            if cur > mx:
                mx = cur
        else:
            cur = 0
    return mx


def aggregate_range(daily: List[DayScorecard], scans: Dict[str, Dict[str, Any]]) -> RangeScorecard:
    """
    daily: liste DayScorecard (auch fuer fehlende Tage als available=False)
    scans: dict date_iso -> scan-dict (nur fuer available=True relevant)
    """
    start = daily[0].date if daily else ""
    end = daily[-1].date if daily else ""

    days_total = len(daily)
    days_missing = [d.date for d in daily if not d.available]
    avail = [d for d in daily if d.available]
    days_with_data = len(avail)

    layer_summaries: List[RangeLayerSummary] = []
    for layer in LAYERS:
        coverages = [
            ls.coverage_pct
            for d in avail
            for ls in d.layer_stats
            if ls.layer == layer
        ]
        if not coverages:
            layer_summaries.append(
                RangeLayerSummary(
                    layer=layer,
                    median_coverage_pct=0.0,
                    mean_coverage_pct=0.0,
                    days_above_50=0,
                    days_below_10=0,
                    longest_black_streak=0,
                    severity_aggregate="BLACK",
                    unblock_potential_strict_total=0,
                    unblock_potential_loose_total=0,
                )
            )
            continue
        median_cov = statistics.median(coverages)
        mean_cov = statistics.mean(coverages)
        days_above_50 = sum(1 for c in coverages if c >= 0.5)
        days_below_10 = sum(1 for c in coverages if c < 0.10)
        # Streak von "BLACK" Tagen
        is_black = [c < SEV_RED for c in coverages]
        longest_black = _streak_max(is_black)
        sev_agg = severity_for_coverage(median_cov)

        # Unblock-Potential aus jedem verfuegbaren Tag aufsummieren
        unblock_strict = 0
        unblock_loose = 0
        for d in avail:
            scan = scans.get(d.date)
            if not scan:
                continue
            up = compute_unblock_potential(scan)
            unblock_strict += up.get(layer + "_strict", 0)
            unblock_loose += up.get(layer + "_loose", 0)

        layer_summaries.append(
            RangeLayerSummary(
                layer=layer,
                median_coverage_pct=round(median_cov, 4),
                mean_coverage_pct=round(mean_cov, 4),
                days_above_50=days_above_50,
                days_below_10=days_below_10,
                longest_black_streak=longest_black,
                severity_aggregate=sev_agg,
                unblock_potential_strict_total=unblock_strict,
                unblock_potential_loose_total=unblock_loose,
            )
        )

    # Median Layer-Distribution
    layers_full_list = [d.layers_full for d in avail]
    layers_0_list = [d.layers_0 for d in avail]
    mtf_good_list = [d.mtf_good_count for d in avail]
    median_layers_full = statistics.median(layers_full_list) if layers_full_list else 0.0
    median_layers_0 = statistics.median(layers_0_list) if layers_0_list else 0.0
    median_mtf_good = statistics.median(mtf_good_list) if mtf_good_list else 0.0

    overall_severity = (
        max(layer_summaries, key=lambda ls: severity_rank(ls.severity_aggregate)).severity_aggregate
        if layer_summaries
        else "GREEN"
    )

    recommendation = build_recommendation(layer_summaries, median_layers_full)

    return RangeScorecard(
        start=start,
        end=end,
        days_total=days_total,
        days_with_data=days_with_data,
        days_missing=days_missing,
        layer_summaries=layer_summaries,
        median_layers_full=median_layers_full,
        median_layers_0=median_layers_0,
        median_mtf_good=median_mtf_good,
        overall_severity=overall_severity,
        recommendation=recommendation,
    )


# -------------------------------------------------------------------------- #
# Recommendation                                                             #
# -------------------------------------------------------------------------- #

def build_recommendation(
    layer_summaries: List[RangeLayerSummary],
    median_layers_full: float,
) -> Dict[str, Any]:
    """
    Sprint-Prio:
      Score = severity_rank(sev) * 10
            + min(longest_black_streak, 14)            # bis 14d
            + min(unblock_potential_loose_total, 50)   # cap
    Hoeher = mehr Prioritaet.

    Zusaetzlich: wenn ALLE Layer GREEN und median_layers_full = 0, dann
    ist die Layer-Logik (AND-Filter) der Bottleneck, nicht ein einzelner
    Layer. Empfehlung: "review_layer_combination" (Bahn B' statt Bahn B).
    """
    if not layer_summaries:
        return {
            "priority_order": [],
            "primary_target": None,
            "rationale": "no_data",
        }

    all_green = all(ls.severity_aggregate == "GREEN" for ls in layer_summaries)
    if all_green and median_layers_full == 0:
        return {
            "priority_order": [ls.layer for ls in layer_summaries],
            "primary_target": "review_layer_combination",
            "rationale": (
                "Alle Layer sind GREEN, aber kein Setup hat alle 3 Layer "
                "gleichzeitig aktiv. Bottleneck ist nicht ein einzelner "
                "Layer, sondern die UND-Verknuepfung. Workshop-Empfehlung: "
                "Score-Layer-Kombi-Logik review (z.B. OR statt AND, oder "
                "Layer-Ablation)."
            ),
        }

    def score_for(ls: RangeLayerSummary) -> int:
        return (
            severity_rank(ls.severity_aggregate) * 10
            + min(ls.longest_black_streak, 14)
            + min(ls.unblock_potential_loose_total, 50)
        )

    ranked = sorted(layer_summaries, key=score_for, reverse=True)
    primary = ranked[0]

    rationale_parts = []
    rationale_parts.append(
        f"Layer '{primary.layer}' ist {primary.severity_aggregate} "
        f"(median coverage {primary.median_coverage_pct*100:.1f}%, "
        f"longest BLACK-streak {primary.longest_black_streak} Tage)."
    )
    rationale_parts.append(
        f"Unblock-Potential (loose): {primary.unblock_potential_loose_total} "
        f"Setups wuerden + 3 Layer = 1 erreichen, wenn dieser Layer "
        f"100% aktiv waere."
    )
    if primary.unblock_potential_strict_total > 0:
        rationale_parts.append(
            f"Davon mit MTF-Good: {primary.unblock_potential_strict_total}."
        )

    return {
        "priority_order": [ls.layer for ls in ranked],
        "primary_target": primary.layer,
        "rationale": " ".join(rationale_parts),
        "scores": {ls.layer: score_for(ls) for ls in ranked},
    }


# -------------------------------------------------------------------------- #
# IO                                                                         #
# -------------------------------------------------------------------------- #

def load_scan(scan_date: str, root: Path = PROJECT_ROOT) -> Optional[Dict[str, Any]]:
    p = root / "tools" / f"scan_{scan_date}.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)


# -------------------------------------------------------------------------- #
# Markdown-Brief                                                             #
# -------------------------------------------------------------------------- #

def render_day_md(day: DayScorecard, scan: Optional[Dict[str, Any]]) -> str:
    out = []
    out.append(f"# Layer-Heal-Scorecard {day.date}")
    out.append("")
    out.append(f"**Overall:** {day.overall_severity} · "
               f"Top-Setups: {day.top_setups_count} · "
               f"Universe: {day.universe_size} · "
               f"Valid: {day.valid_results}")
    out.append("")
    out.append("## Layer-Coverage")
    out.append("")
    out.append("| Layer | Coverage | non-zero | Severity |")
    out.append("|---|---:|---:|:--:|")
    for ls in day.layer_stats:
        out.append(
            f"| `{ls.layer}` | {ls.coverage_pct*100:.1f}% | "
            f"{ls.non_zero}/{ls.total} | {ls.severity} |"
        )
    out.append("")
    out.append("## Layer-Distribution")
    out.append("")
    out.append(f"- layers_full (3/3): **{day.layers_full}**")
    out.append(f"- layers_2:        {day.layers_2}")
    out.append(f"- layers_1:        {day.layers_1}")
    out.append(f"- layers_0:        {day.layers_0}")
    out.append(f"- mtf_good:        {day.mtf_good_count}")
    out.append("")
    if scan is not None:
        up = compute_unblock_potential(scan)
        out.append("## Unblock-Potential (Single-Day)")
        out.append("")
        out.append("Falls dieser Layer heute 100% aktiv waere, wuerden so viele Setups "
                   "die `+ MTF + 3 Layer`-Schwelle erreichen:")
        out.append("")
        out.append("| Layer | strict (mit MTF) | loose (ohne MTF) |")
        out.append("|---|---:|---:|")
        for layer in LAYERS:
            out.append(
                f"| `{layer}` | {up.get(layer+'_strict', 0)} | "
                f"{up.get(layer+'_loose', 0)} |"
            )
        out.append("")
    out.append("---")
    out.append(f"*V7.9.17 LAYER-HEAL-SCORECARD-1 · read-only · {datetime.now(timezone.utc).isoformat()}*")
    return "\n".join(out) + "\n"


def render_range_md(rg: RangeScorecard) -> str:
    out = []
    out.append(f"# Layer-Heal-Range {rg.start} → {rg.end}")
    out.append("")
    out.append(f"**Overall:** {rg.overall_severity} · "
               f"Days total {rg.days_total}, with data {rg.days_with_data}, "
               f"missing {len(rg.days_missing)}")
    if rg.days_missing:
        out.append(f"Missing days: {', '.join(rg.days_missing)}")
    out.append("")
    out.append("## Layer-Aggregate")
    out.append("")
    out.append("| Layer | median Cov | days≥50% | days<10% | longest BLACK | unblock_strict | unblock_loose | Severity |")
    out.append("|---|---:|---:|---:|---:|---:|---:|:--:|")
    for ls in rg.layer_summaries:
        out.append(
            f"| `{ls.layer}` | {ls.median_coverage_pct*100:.1f}% | "
            f"{ls.days_above_50} | {ls.days_below_10} | "
            f"{ls.longest_black_streak} | "
            f"{ls.unblock_potential_strict_total} | "
            f"{ls.unblock_potential_loose_total} | "
            f"{ls.severity_aggregate} |"
        )
    out.append("")
    out.append("## Layer-Distribution-Median")
    out.append("")
    out.append(f"- median layers_full: **{rg.median_layers_full:.1f}**")
    out.append(f"- median layers_0:    {rg.median_layers_0:.1f}")
    out.append(f"- median mtf_good:    {rg.median_mtf_good:.1f}")
    out.append("")
    out.append("## Sprint-Prio (Layer-Heal)")
    out.append("")
    rec = rg.recommendation
    if rec.get("primary_target") == "review_layer_combination":
        out.append("**Empfehlung:** `review_layer_combination`")
        out.append("")
        out.append(rec.get("rationale", ""))
    else:
        out.append(f"**Primaerer Fix:** `{rec.get('primary_target')}`")
        out.append("")
        out.append(f"**Reihenfolge:** {' → '.join(rec.get('priority_order') or [])}")
        out.append("")
        out.append(rec.get("rationale", ""))
        scores = rec.get("scores") or {}
        if scores:
            out.append("")
            out.append("**Sprint-Score pro Layer:**")
            for k, v in scores.items():
                out.append(f"- `{k}`: {v}")
    out.append("")
    out.append("---")
    out.append(f"*V7.9.17 LAYER-HEAL-SCORECARD-1 · read-only · {datetime.now(timezone.utc).isoformat()}*")
    return "\n".join(out) + "\n"


# -------------------------------------------------------------------------- #
# CLI                                                                        #
# -------------------------------------------------------------------------- #

def cli_single_day(target: date, root: Path, write_md: bool) -> Tuple[Dict[str, Any], DayScorecard]:
    iso = target.isoformat()
    scan = load_scan(iso, root)
    if scan is None:
        day = DayScorecard(
            date=iso,
            available=False,
            universe_size=0,
            valid_results=0,
            top_setups_count=0,
            note="scan_not_found",
        )
        payload = {
            "version": VERSION,
            "mode": "single_day",
            "date": iso,
            "scorecard": _to_jsonable(day),
        }
        out_json = root / "tools" / f"layer_heal_{iso}.json"
        write_json(out_json, payload)
        if write_md:
            out_md = root / "Briefings" / f"Layer-Heal-Scorecard-{iso}.md"
            md = render_day_md(day, None)
            out_md.parent.mkdir(parents=True, exist_ok=True)
            out_md.write_text(md, encoding="utf-8")
        return payload, day

    day = analyze_day_scan(scan)
    unblock = compute_unblock_potential(scan)
    payload = {
        "version": VERSION,
        "mode": "single_day",
        "date": iso,
        "scorecard": _to_jsonable(day),
        "unblock_potential": unblock,
    }
    out_json = root / "tools" / f"layer_heal_{iso}.json"
    write_json(out_json, payload)
    if write_md:
        out_md = root / "Briefings" / f"Layer-Heal-Scorecard-{iso}.md"
        md = render_day_md(day, scan)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
    return payload, day


def cli_range(start: date, end: date, root: Path, write_md: bool) -> Tuple[Dict[str, Any], RangeScorecard]:
    days = _expand_range(start, end)
    daily: List[DayScorecard] = []
    scans: Dict[str, Dict[str, Any]] = {}
    for d in days:
        iso = d.isoformat()
        scan = load_scan(iso, root)
        if scan is None:
            daily.append(
                DayScorecard(
                    date=iso,
                    available=False,
                    universe_size=0,
                    valid_results=0,
                    top_setups_count=0,
                    note="scan_not_found",
                )
            )
        else:
            scans[iso] = scan
            daily.append(analyze_day_scan(scan))

    rg = aggregate_range(daily, scans)
    payload = {
        "version": VERSION,
        "mode": "range",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "daily": [_to_jsonable(d) for d in daily],
        "range": _to_jsonable(rg),
    }
    out_json = root / "tools" / f"layer_heal_range_{start.isoformat()}_{end.isoformat()}.json"
    write_json(out_json, payload)
    if write_md:
        out_md = root / "Briefings" / f"Layer-Heal-Range-{start.isoformat()}_{end.isoformat()}.md"
        md = render_range_md(rg)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
    return payload, rg


def _to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "__dict__") or hasattr(obj, "__dataclass_fields__"):
        try:
            return asdict(obj)
        except TypeError:
            pass
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "V7.9.17 Layer-Heal-Scorecard. Read-only, freeze-konform. "
            "Beantwortet 'Welchen Layer fixen wir zuerst?' nach V7.9.16."
        )
    )
    p.add_argument("--date", default=None, help="Single-day mode: YYYY-MM-DD (Default: heute).")
    p.add_argument("--range", default=None, help="Range mode: YYYY-MM-DD:YYYY-MM-DD.")
    p.add_argument("--strict", action="store_true",
                   help="Exit-Code 2 wenn irgendein Layer Severity BLACK ist.")
    p.add_argument("--no-md", action="store_true", help="JSON-only (kein Briefing-MD).")
    p.add_argument("--root", default=str(PROJECT_ROOT), help="Project-Root (default: parent of tools/).")
    args = p.parse_args(argv)

    root = Path(args.root).resolve()

    if args.range:
        try:
            s, e = args.range.split(":", 1)
            start = _parse_iso(s)
            end = _parse_iso(e)
        except ValueError:
            print("ERROR: --range expects YYYY-MM-DD:YYYY-MM-DD", file=sys.stderr)
            return 1
        payload, rg = cli_range(start, end, root, write_md=not args.no_md)
        print(f"layer_heal range {rg.start}..{rg.end} overall={rg.overall_severity}")
        for ls in rg.layer_summaries:
            print(f"  {ls.layer:14s} sev={ls.severity_aggregate} "
                  f"median_cov={ls.median_coverage_pct*100:.1f}% "
                  f"unblock_loose={ls.unblock_potential_loose_total}")
        primary = rg.recommendation.get("primary_target")
        print(f"  primary_target={primary}")
        if args.strict and any(ls.severity_aggregate == "BLACK" for ls in rg.layer_summaries):
            return 2
        return 0

    target = _parse_iso(args.date) if args.date else date.today()
    payload, day = cli_single_day(target, root, write_md=not args.no_md)
    print(f"layer_heal {day.date} overall={day.overall_severity} "
          f"layers_full={day.layers_full} layers_0={day.layers_0} "
          f"mtf_good={day.mtf_good_count} top_setups={day.top_setups_count}")
    for ls in day.layer_stats:
        print(f"  {ls.layer:14s} sev={ls.severity} cov={ls.coverage_pct*100:.1f}% "
              f"({ls.non_zero}/{ls.total})")
    if args.strict and any(ls.severity == "BLACK" for ls in day.layer_stats):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
