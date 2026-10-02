#!/usr/bin/env python3
"""
SCAN-FRESHNESS-MONITOR-1 (V7.9.36 · 2026-05-27 Mi KW22 · FEATURE).

Maschinen-lesbarer Diagnose-Layer fuer den taeglichen `tools/scan_<date>.json`.
Trennt drei Wurzeln, die heute alle gleich aussehen ("Empfehlung verwendet
2026-05-22-Daten obwohl heute der 27. ist"):

  1. **scan_fresh**           Heutiges Scan-Artefakt existiert UND `_fallback_from`
                              ist NICHT gesetzt → Mac-Cron heute live durchgelaufen.
  2. **scan_stale_short**     `_fallback_from` gesetzt, `_fallback_age_werktage` 1-2 →
                              Mac-Cron 1-2 Werktage spaet, akzeptabel laut CLAUDE.md §6
                              Cache-Persistenz-Fallback (max 5 Werktage).
  3. **scan_stale_long**      `_fallback_age_werktage` >=3 → Mac-Cron strukturell
                              ausgefallen, Empfehlung greift auf ein 3+ Werktage
                              altes Universum zurueck (Score-Drift inakzeptabel).
  4. **scan_missing**         Weder Live noch Fallback-Pfad → kein scan_<date>.json
                              gefunden, Empfehlung kann nicht laufen.
  5. **scan_corrupt**         Datei vorhanden, aber JSON-parse failt oder Pflichtfeld
                              `top_setups` fehlt.

CLAUDE.md §6 erlaubt Cache-Persistenz max 5 Werktage. Die Schwelle wird im
Tool als Konstante `_STALE_LONG_THRESHOLD` (Default 3) konfigurierbar.

V7.9.36 ist analog zu V7.9.34/35 (news_sentiment_sandbox_diff) und V7.9.29
(market_pcr_resilience_verify) ein Read-Only Diagnose-Tool mit Single-Day +
Range-Mode, 5-stufiger Severity-Ampel und maschinen-lesbarem Decision-Tree.

Workflow (KW22-EOW-Recap-Vorbereitung Fr 29.05.):

    python3 tools/scan_freshness_monitor.py
    python3 tools/scan_freshness_monitor.py --date 2026-05-27
    python3 tools/scan_freshness_monitor.py --start-date 2026-05-18 --end-date 2026-05-27 --strict
    python3 tools/scan_freshness_monitor.py --selftest

CLI Exit-Codes (Strict-Mode --strict):
    0  GREEN   scan_fresh ODER range_recovered / range_fresh
    1  YELLOW  scan_stale_short / range_mixed
    2  RED     scan_stale_long / range_chronic_stale / range_persistent_stale
    3  BLACK   scan_missing / scan_corrupt / range_chronic_missing
    4  INSUFFICIENT_DATA  range mit <2 Werktagen Daten

Output JSON:
  Single: tools/scan_freshness_<date>.json
  Range : tools/scan_freshness_range_<start>_<end>.json

Output MD:
  Single: Briefings/Scan-Freshness-<date>.md
  Range : Briefings/Scan-Freshness-Range-<start>_<end>.md

"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, date as _date, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"

VERSION = "V7.9.36"

# Schwelle in Werktagen, ab der ein _fallback_age als strukturell-stale gilt.
# CLAUDE.md §6 Cache-Persistenz-Fallback: max 5 Werktage. Tool ist strenger:
# ab 3 Werktagen wird auf RED gekippt (Empfehlungs-Drift inakzeptabel).
_STALE_LONG_THRESHOLD = 3

# Mapping Single-Day-Verdict -> Severity-Label
_SINGLE_VERDICTS = {
    "scan_fresh":        "GREEN",
    "scan_stale_short":  "YELLOW",
    "scan_stale_long":   "RED",
    "scan_missing":      "BLACK",
    "scan_corrupt":      "BLACK",
}

# Mapping Range-Verdict -> Severity-Label
_RANGE_VERDICTS = {
    "range_fresh":               "GREEN",   # >=80% Werktage GREEN
    "range_recovered":           "GREEN",   # letzter Werktag GREEN nach non-GREEN
    "range_chronic_stale":       "RED",     # >=3 Werktage in Folge scan_stale_long
    "range_chronic_missing":     "BLACK",   # >=3 Werktage in Folge scan_missing
    "range_persistent_stale":    "RED",     # 100% Werktage non-GREEN ohne 3er-Streak
    "range_mixed":               "YELLOW",  # uneindeutiges Bild
    "range_insufficient_data":   "BLACK",   # < 2 Werktage Daten
}


# ---------------------------------------------------------------------------
# Single-Day-Diagnose
# ---------------------------------------------------------------------------

def load_scan_artifact(date_str: str,
                       tools_dir: Optional[Path] = None) -> dict:
    """Lade tools/scan_<date_str>.json und liefere ein robustes Diagnose-Dict.

    Returns:
        {
          "exists":   bool,
          "parsed":   bool,
          "data":     dict|None,
          "size_b":   int|None,
          "error":    str|None,
        }
    """
    base = tools_dir or TOOLS_DIR
    path = base / f"scan_{date_str}.json"
    if not path.exists():
        return {"exists": False, "parsed": False, "data": None,
                "size_b": None, "error": "file_not_found"}
    try:
        size_b = path.stat().st_size
    except OSError as e:
        size_b = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"exists": True, "parsed": True, "data": data,
                "size_b": size_b, "error": None}
    except Exception as e:
        return {"exists": True, "parsed": False, "data": None,
                "size_b": size_b, "error": f"json_parse_error: {type(e).__name__}"}


def classify_single(date_str: str,
                    artifact: dict,
                    stale_long_threshold: int = _STALE_LONG_THRESHOLD) -> dict:
    """Klassifiziere ein einzelnes scan_<date>.json zu einem Single-Day-Verdict.

    Returns:
        {
          "date": date_str,
          "verdict": "scan_fresh"|"scan_stale_short"|"scan_stale_long"
                     |"scan_missing"|"scan_corrupt",
          "severity": "GREEN"|"YELLOW"|"RED"|"BLACK",
          "rationale": str,
          "fallback_from": str|None,
          "fallback_age_werktage": int|None,
          "valid_results": int|None,
          "universe_size": int|None,
          "primary_target": str|None,
        }
    """
    # 1. Datei fehlt komplett
    if not artifact.get("exists"):
        return {
            "date": date_str,
            "verdict": "scan_missing",
            "severity": _SINGLE_VERDICTS["scan_missing"],
            "rationale": "kein scan_<date>.json (weder live noch fallback)",
            "fallback_from": None,
            "fallback_age_werktage": None,
            "valid_results": None,
            "universe_size": None,
            "primary_target": "morning_cron_unavailable",
        }

    # 2. Datei kaputt
    if not artifact.get("parsed"):
        return {
            "date": date_str,
            "verdict": "scan_corrupt",
            "severity": _SINGLE_VERDICTS["scan_corrupt"],
            "rationale": f"scan_<date>.json existiert, aber {artifact.get('error')}",
            "fallback_from": None,
            "fallback_age_werktage": None,
            "valid_results": None,
            "universe_size": None,
            "primary_target": "scan_artifact_corrupt",
        }

    data = artifact["data"] or {}
    fb_from = data.get("_fallback_from")
    fb_age_raw = data.get("_fallback_age_werktage")
    valid = data.get("valid_results")
    usize = data.get("universe_size")

    # Pflichtfeld top_setups MUSS vorhanden sein (auch leer ist ok)
    if "top_setups" not in data:
        return {
            "date": date_str,
            "verdict": "scan_corrupt",
            "severity": _SINGLE_VERDICTS["scan_corrupt"],
            "rationale": "scan_<date>.json parsed, aber Pflichtfeld 'top_setups' fehlt",
            "fallback_from": fb_from,
            "fallback_age_werktage": fb_age_raw,
            "valid_results": valid,
            "universe_size": usize,
            "primary_target": "scan_schema_drift",
        }

    # 3. Kein Fallback → frisch
    if fb_from is None:
        return {
            "date": date_str,
            "verdict": "scan_fresh",
            "severity": _SINGLE_VERDICTS["scan_fresh"],
            "rationale": (
                f"scan heute live generiert, valid_results={valid}, "
                f"universe_size={usize}"
            ),
            "fallback_from": None,
            "fallback_age_werktage": 0,
            "valid_results": valid,
            "universe_size": usize,
            "primary_target": None,
        }

    # 4. Fallback aktiv → age pruefen
    try:
        fb_age = int(fb_age_raw) if fb_age_raw is not None else None
    except (TypeError, ValueError):
        fb_age = None

    if fb_age is None:
        return {
            "date": date_str,
            "verdict": "scan_corrupt",
            "severity": _SINGLE_VERDICTS["scan_corrupt"],
            "rationale": (
                f"_fallback_from={fb_from} aber _fallback_age_werktage fehlt/ungueltig"
            ),
            "fallback_from": fb_from,
            "fallback_age_werktage": fb_age_raw,
            "valid_results": valid,
            "universe_size": usize,
            "primary_target": "scan_schema_drift",
        }

    if fb_age >= stale_long_threshold:
        return {
            "date": date_str,
            "verdict": "scan_stale_long",
            "severity": _SINGLE_VERDICTS["scan_stale_long"],
            "rationale": (
                f"Kandidatenliste greift auf {fb_age}-Werktage altes scan_<date>.json "
                f"({fb_from}) zurueck: Mac-Cron strukturell ausgefallen"
            ),
            "fallback_from": fb_from,
            "fallback_age_werktage": fb_age,
            "valid_results": valid,
            "universe_size": usize,
            "primary_target": "mac_cron_outage",
        }

    return {
        "date": date_str,
        "verdict": "scan_stale_short",
        "severity": _SINGLE_VERDICTS["scan_stale_short"],
        "rationale": (
            f"Kandidatenliste greift auf {fb_age}-Werktag(e) altes scan_<date>.json "
            f"({fb_from}) zurueck: innerhalb CLAUDE.md §6 Cache-Toleranz"
        ),
        "fallback_from": fb_from,
        "fallback_age_werktage": fb_age,
        "valid_results": valid,
        "universe_size": usize,
        "primary_target": "mac_cron_delay",
    }


def render_single_json(verdict: dict, artifact: dict) -> dict:
    """Stable JSON-Output fuer Single-Day-Mode."""
    return {
        "tool": "scan_freshness_monitor",
        "mode": "single",
        "version": VERSION,
        "date": verdict["date"],
        "verdict": {
            "verdict": verdict["verdict"],
            "severity": verdict["severity"],
            "rationale": verdict["rationale"],
            "primary_target": verdict.get("primary_target"),
        },
        "details": {
            "exists": artifact.get("exists"),
            "parsed": artifact.get("parsed"),
            "size_b": artifact.get("size_b"),
            "error": artifact.get("error"),
            "fallback_from": verdict.get("fallback_from"),
            "fallback_age_werktage": verdict.get("fallback_age_werktage"),
            "valid_results": verdict.get("valid_results"),
            "universe_size": verdict.get("universe_size"),
        },
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


def render_single_markdown(payload: dict) -> str:
    v = payload["verdict"]
    sev = v["severity"]
    emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "BLACK": "⚫"}.get(sev, "⚪")
    d = payload["details"]
    lines: list[str] = []
    lines.append(f"# Scan-Freshness-Monitor · {payload['date']}")
    lines.append("")
    lines.append(f"**Verdict:** {emoji} `{v['verdict']}` ({sev})")
    lines.append(f"**Rationale:** {v['rationale']}")
    if v.get("primary_target"):
        lines.append(f"**Primary Target:** `{v['primary_target']}`")
    lines.append("")
    lines.append("## Artefakt-Details")
    lines.append(f"- exists: {d.get('exists')}")
    lines.append(f"- parsed: {d.get('parsed')}")
    lines.append(f"- size_b: {d.get('size_b')}")
    if d.get("error"):
        lines.append(f"- error: `{d.get('error')}`")
    lines.append(f"- fallback_from: `{d.get('fallback_from')}`")
    lines.append(f"- fallback_age_werktage: {d.get('fallback_age_werktage')}")
    lines.append(f"- valid_results: {d.get('valid_results')}")
    lines.append(f"- universe_size: {d.get('universe_size')}")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated by `tools/scan_freshness_monitor.py` ({VERSION})._")
    return "\n".join(lines) + "\n"


def severity_to_exit_code(severity: str) -> int:
    return {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3}.get(severity, 3)


# ---------------------------------------------------------------------------
# Range-Mode
# ---------------------------------------------------------------------------

def _iter_workdays(start_date: str, end_date: str) -> list[str]:
    """Iteriere Werktage Mo-Fr inkl. als ISO-Strings. Sa/So uebersprungen."""
    d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
    d_end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if d_start > d_end:
        return []
    days: list[str] = []
    cur = d_start
    while cur <= d_end:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y-%m-%d"))
        cur = _date.fromordinal(cur.toordinal() + 1)
    return days


def aggregate_range(start_date: str, end_date: str,
                    tools_dir: Optional[Path] = None,
                    stale_long_threshold: int = _STALE_LONG_THRESHOLD,
                    min_workdays_for_chronic: int = 3) -> dict:
    """Aggregiere Single-Day-Verdicts ueber Werktage zu einem Range-Verdict.

    Liest direkt die scan_<date>.json's (kein Lookup auf zwischengespeicherte
    scan_freshness_<date>.json's: das ist read-only Diagnose).
    """
    workdays = _iter_workdays(start_date, end_date)
    per_day: list[dict] = []
    base = tools_dir or TOOLS_DIR
    for d in workdays:
        art = load_scan_artifact(d, tools_dir=base)
        v = classify_single(d, art, stale_long_threshold=stale_long_threshold)
        per_day.append({
            "date": d,
            "verdict": v["verdict"],
            "severity": v["severity"],
            "fallback_age_werktage": v.get("fallback_age_werktage"),
            "primary_target": v.get("primary_target"),
            "available": True,  # auch scan_missing zaehlt als "available data point"
            "exists": art.get("exists"),
            "parsed": art.get("parsed"),
        })

    n_total = len(workdays)
    n_avail = sum(1 for r in per_day if r["available"])

    counts: dict[str, int] = {}
    sev_counts: dict[str, int] = {"GREEN": 0, "YELLOW": 0, "RED": 0, "BLACK": 0}
    for r in per_day:
        if not r["available"]:
            continue
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        sev = r["severity"] or "BLACK"
        if sev in sev_counts:
            sev_counts[sev] += 1

    # Longest contiguous streak je Verdict-Key
    longest_streak = {"verdict_key": None, "length": 0}
    cur_key = None
    cur_len = 0
    for r in per_day:
        if not r["available"]:
            cur_key = None
            cur_len = 0
            continue
        vk = r["verdict"]
        if vk == cur_key:
            cur_len += 1
        else:
            cur_key = vk
            cur_len = 1
        if cur_len > longest_streak["length"]:
            longest_streak = {"verdict_key": cur_key, "length": cur_len}

    range_verdict = _build_range_verdict(per_day, counts, sev_counts,
                                          longest_streak, n_avail,
                                          min_workdays_for_chronic)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "workdays_total": n_total,
        "workdays_with_data": n_avail,
        "per_day": per_day,
        "counts": counts,
        "severity_counts": sev_counts,
        "longest_streak": longest_streak,
        "verdict": range_verdict,
    }


def _build_range_verdict(per_day: list[dict], counts: dict, sev_counts: dict,
                          longest_streak: dict, n_avail: int,
                          min_workdays_for_chronic: int) -> dict:
    """Map aggregated stats auf einen der 7 Range-Verdikte."""
    # 1. zu wenig Daten
    if n_avail < 2:
        return _wrap_range_verdict(
            "range_insufficient_data",
            f"nur {n_avail} Werktag(e) mit Daten verfuegbar (Min=2)",
            None,
        )

    # 2. chronic streaks
    ls_key = longest_streak.get("verdict_key")
    ls_len = int(longest_streak.get("length") or 0)
    if ls_key and ls_len >= min_workdays_for_chronic:
        chronic_map = {
            "scan_stale_long":   ("range_chronic_stale",   "mac_cron_outage"),
            "scan_missing":      ("range_chronic_missing", "morning_cron_unavailable"),
        }
        if ls_key in chronic_map:
            verdict_key, target = chronic_map[ls_key]
            return _wrap_range_verdict(
                verdict_key,
                f"{ls_len} Werktage in Folge {ls_key}",
                target,
            )

    # 3. recovered: letzter Werktag mit Daten ist GREEN (scan_fresh) UND vorher non-GREEN
    last_avail = next((r for r in reversed(per_day) if r["available"]), None)
    if last_avail and last_avail.get("severity") == "GREEN":
        prior = [r for r in per_day[:-1] if r["available"]]
        if any(r.get("severity") in ("RED", "YELLOW", "BLACK") for r in prior):
            return _wrap_range_verdict(
                "range_recovered",
                (
                    f"letzter Werktag {last_avail['date']} = "
                    f"{last_avail['verdict']} GREEN, davor non-GREEN"
                ),
                None,
            )

    # 4. fresh: dominant GREEN (>=80%)
    if n_avail > 0 and sev_counts["GREEN"] / n_avail >= 0.8:
        return _wrap_range_verdict(
            "range_fresh",
            f"{sev_counts['GREEN']}/{n_avail} Werktage GREEN (>=80%)",
            None,
        )

    # 5. persistent_stale: 100% non-GREEN, aber Wurzel/Severity wandert
    if n_avail >= 2 and sev_counts["GREEN"] == 0:
        # primary_target = haeufigster non-GREEN Verdict
        target = None
        non_green = {k: v for k, v in counts.items() if k != "scan_fresh"}
        if non_green:
            top_key = max(non_green, key=non_green.get)
            per_day_match = [r for r in per_day
                              if r["verdict"] == top_key and r["available"]]
            if per_day_match:
                target = per_day_match[0].get("primary_target")
        return _wrap_range_verdict(
            "range_persistent_stale",
            (
                f"{n_avail}/{n_avail} Werktage non-GREEN "
                f"(counts: {dict(counts)})"
            ),
            target,
        )

    # 6. fallback: mixed
    dominant_target = None
    if counts:
        non_green = {k: v for k, v in counts.items() if k != "scan_fresh"}
        if non_green:
            top_verdict = max(non_green, key=non_green.get)
            per_day_match = [r for r in per_day
                              if r["verdict"] == top_verdict and r["available"]]
            if per_day_match:
                dominant_target = per_day_match[0].get("primary_target")
    return _wrap_range_verdict(
        "range_mixed",
        f"counts={counts} severity={sev_counts} longest_streak={ls_key}@{ls_len}",
        dominant_target,
    )


def _wrap_range_verdict(verdict_key: str, rationale: str,
                         primary_target: Optional[str]) -> dict:
    return {
        "verdict": verdict_key,
        "severity": _RANGE_VERDICTS.get(verdict_key, "BLACK"),
        "rationale": rationale,
        "primary_target": primary_target,
    }


def render_range_json(agg: dict) -> dict:
    """Stable JSON-Output fuer Range-Mode."""
    return {
        "tool": "scan_freshness_monitor",
        "mode": "range",
        "version": VERSION,
        "start_date": agg["start_date"],
        "end_date": agg["end_date"],
        "verdict": agg["verdict"],
        "workdays_total": agg["workdays_total"],
        "workdays_with_data": agg["workdays_with_data"],
        "counts": agg["counts"],
        "severity_counts": agg["severity_counts"],
        "longest_streak": agg["longest_streak"],
        "per_day": agg["per_day"],
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


def render_range_markdown(payload: dict) -> str:
    v = payload["verdict"]
    sev = v["severity"]
    emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "BLACK": "⚫"}.get(sev, "⚪")
    lines: list[str] = []
    lines.append(
        f"# Scan-Freshness-Monitor · Range "
        f"{payload['start_date']} → {payload['end_date']}"
    )
    lines.append("")
    lines.append(f"**Range-Verdict:** {emoji} `{v['verdict']}` ({sev})")
    lines.append(f"**Rationale:** {v['rationale']}")
    if v.get("primary_target"):
        lines.append(f"**Primary Target:** `{v['primary_target']}`")
    lines.append("")
    lines.append(
        f"**Werktage:** {payload['workdays_with_data']} mit Daten / "
        f"{payload['workdays_total']} insgesamt"
    )
    ls = payload.get("longest_streak") or {}
    if ls.get("verdict_key"):
        lines.append(
            f"**Längste Streak:** `{ls['verdict_key']}` × {ls['length']} Werktag(e)"
        )
    lines.append("")
    lines.append("## Severity-Verteilung")
    sc = payload.get("severity_counts") or {}
    lines.append(
        f"- GREEN={sc.get('GREEN',0)} · YELLOW={sc.get('YELLOW',0)} · "
        f"RED={sc.get('RED',0)} · BLACK={sc.get('BLACK',0)}"
    )
    lines.append("")
    lines.append("## Per-Day-Tabelle")
    lines.append("")
    lines.append("| Datum | Verdict | Severity | fallback_age | primary_target |")
    lines.append("|---|---|---|---|---|")
    for r in payload["per_day"]:
        lines.append(
            f"| {r['date']} | `{r['verdict']}` | {r['severity']} | "
            f"{r.get('fallback_age_werktage')} | "
            f"`{r.get('primary_target') or '-'}` |"
        )
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated by `tools/scan_freshness_monitor.py` ({VERSION})._")
    return "\n".join(lines) + "\n"


def range_severity_to_exit_code(severity: str, verdict_key: str) -> int:
    if verdict_key == "range_insufficient_data":
        return 4
    return {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3}.get(severity, 3)


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """Inline-Selftest T01..T15. Liefert Exit-Code 0 bei voll grun, sonst 1."""
    fails = []

    # T01: classify_single mit fehlender Datei -> scan_missing/BLACK
    art = {"exists": False, "parsed": False, "data": None,
           "size_b": None, "error": "file_not_found"}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_missing" and v["severity"] == "BLACK":
        print("T01 OK: classify_single missing file -> scan_missing/BLACK")
    else:
        fails.append(f"T01 FAIL: {v}")

    # T02: classify_single mit corrupt JSON -> scan_corrupt/BLACK
    art = {"exists": True, "parsed": False, "data": None,
           "size_b": 100, "error": "json_parse_error: ValueError"}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_corrupt" and v["severity"] == "BLACK":
        print("T02 OK: classify_single corrupt JSON -> scan_corrupt/BLACK")
    else:
        fails.append(f"T02 FAIL: {v}")

    # T03: classify_single ohne top_setups -> scan_corrupt
    art = {"exists": True, "parsed": True,
           "data": {"valid_results": 30, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_corrupt":
        print("T03 OK: classify_single missing top_setups -> scan_corrupt")
    else:
        fails.append(f"T03 FAIL: {v}")

    # T04: classify_single mit live-Scan -> scan_fresh/GREEN
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "valid_results": 60, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_fresh" and v["severity"] == "GREEN":
        print("T04 OK: classify_single live scan -> scan_fresh/GREEN")
    else:
        fails.append(f"T04 FAIL: {v}")

    # T05: classify_single mit fallback age=1 -> scan_stale_short/YELLOW
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-26.json",
                    "_fallback_age_werktage": 1,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_stale_short" and v["severity"] == "YELLOW":
        print("T05 OK: classify_single fallback age=1 -> scan_stale_short/YELLOW")
    else:
        fails.append(f"T05 FAIL: {v}")

    # T06: classify_single mit fallback age=3 -> scan_stale_long/RED
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-22.json",
                    "_fallback_age_werktage": 3,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_stale_long" and v["severity"] == "RED":
        print("T06 OK: classify_single fallback age=3 -> scan_stale_long/RED")
    else:
        fails.append(f"T06 FAIL: {v}")

    # T07: classify_single mit fallback age=5 (CLAUDE.md §6 Cap) -> scan_stale_long
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-20.json",
                    "_fallback_age_werktage": 5,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_stale_long" and v["severity"] == "RED":
        print("T07 OK: classify_single fallback age=5 -> scan_stale_long")
    else:
        fails.append(f"T07 FAIL: {v}")

    # T08: classify_single mit fallback_from gesetzt aber age fehlt -> scan_corrupt
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-22.json",
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = classify_single("2026-05-27", art)
    if v["verdict"] == "scan_corrupt":
        print("T08 OK: classify_single fallback_from without age -> scan_corrupt")
    else:
        fails.append(f"T08 FAIL: {v}")

    # T09: _iter_workdays ueberspringt Sa/So
    days = _iter_workdays("2026-05-22", "2026-05-26")  # Fr/Sa/So/Mo/Di
    expected = ["2026-05-22", "2026-05-25", "2026-05-26"]
    if days == expected:
        print("T09 OK: _iter_workdays skips weekends")
    else:
        fails.append(f"T09 FAIL: got {days}")

    # T10: severity_to_exit_code
    if (severity_to_exit_code("GREEN") == 0 and
        severity_to_exit_code("YELLOW") == 1 and
        severity_to_exit_code("RED") == 2 and
        severity_to_exit_code("BLACK") == 3):
        print("T10 OK: severity_to_exit_code mapping")
    else:
        fails.append("T10 FAIL: severity_to_exit_code")

    # T11: aggregate_range range_insufficient_data wenn keine Daten
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        agg = aggregate_range("2026-05-25", "2026-05-27", tools_dir=tdir)
        # Mo-Mi (3 Werktage), aber kein File da -> alle scan_missing/BLACK
        # n_avail=3 (jede Klassifikation ist available), nicht insufficient_data
        # Stattdessen: 3x scan_missing -> chronic_missing
        if agg["verdict"]["verdict"] == "range_chronic_missing":
            print("T11 OK: aggregate_range chronic_missing wenn 3+ Werktage ohne Datei")
        else:
            fails.append(f"T11 FAIL: {agg['verdict']}")

    # T12: aggregate_range range_fresh wenn alle Werktage live
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        for d in ["2026-05-25", "2026-05-26", "2026-05-27"]:
            (tdir / f"scan_{d}.json").write_text(json.dumps({
                "top_setups": [], "valid_results": 60, "universe_size": 60,
            }))
        agg = aggregate_range("2026-05-25", "2026-05-27", tools_dir=tdir)
        if agg["verdict"]["verdict"] == "range_fresh":
            print("T12 OK: aggregate_range range_fresh wenn alle live")
        else:
            fails.append(f"T12 FAIL: {agg['verdict']}")

    # T13: aggregate_range range_recovered wenn letzter Werktag fresh, davor stale
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / "scan_2026-05-25.json").write_text(json.dumps({
            "top_setups": [], "_fallback_from": "scan_2026-05-22.json",
            "_fallback_age_werktage": 3,
            "valid_results": 59, "universe_size": 60,
        }))
        (tdir / "scan_2026-05-26.json").write_text(json.dumps({
            "top_setups": [], "_fallback_from": "scan_2026-05-22.json",
            "_fallback_age_werktage": 4,
            "valid_results": 59, "universe_size": 60,
        }))
        (tdir / "scan_2026-05-27.json").write_text(json.dumps({
            "top_setups": [], "valid_results": 60, "universe_size": 60,
        }))
        agg = aggregate_range("2026-05-25", "2026-05-27", tools_dir=tdir)
        if agg["verdict"]["verdict"] == "range_recovered":
            print("T13 OK: aggregate_range range_recovered wenn letzter Tag fresh nach stale")
        else:
            fails.append(f"T13 FAIL: {agg['verdict']}")

    # T14: aggregate_range range_chronic_stale wenn 3x stale_long in Folge
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        for i, d in enumerate(["2026-05-25", "2026-05-26", "2026-05-27"]):
            (tdir / f"scan_{d}.json").write_text(json.dumps({
                "top_setups": [], "_fallback_from": "scan_2026-05-20.json",
                "_fallback_age_werktage": 3 + i,
                "valid_results": 59, "universe_size": 60,
            }))
        agg = aggregate_range("2026-05-25", "2026-05-27", tools_dir=tdir)
        if (agg["verdict"]["verdict"] == "range_chronic_stale" and
            agg["verdict"]["severity"] == "RED" and
            agg["verdict"]["primary_target"] == "mac_cron_outage"):
            print("T14 OK: aggregate_range range_chronic_stale 3x stale_long")
        else:
            fails.append(f"T14 FAIL: {agg['verdict']}")

    # T15: aggregate_range range_persistent_stale wenn 100% non-GREEN ohne 3-Streak
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        # 25.05.: stale_long (RED)
        (tdir / "scan_2026-05-25.json").write_text(json.dumps({
            "top_setups": [], "_fallback_from": "scan_2026-05-20.json",
            "_fallback_age_werktage": 3,
            "valid_results": 59, "universe_size": 60,
        }))
        # 26.05.: stale_short (YELLOW)
        (tdir / "scan_2026-05-26.json").write_text(json.dumps({
            "top_setups": [], "_fallback_from": "scan_2026-05-25.json",
            "_fallback_age_werktage": 1,
            "valid_results": 59, "universe_size": 60,
        }))
        # 27.05.: stale_long (RED, wandernde Wurzel: age=4 zu age=3)
        (tdir / "scan_2026-05-27.json").write_text(json.dumps({
            "top_setups": [], "_fallback_from": "scan_2026-05-20.json",
            "_fallback_age_werktage": 4,
            "valid_results": 59, "universe_size": 60,
        }))
        agg = aggregate_range("2026-05-25", "2026-05-27", tools_dir=tdir,
                              min_workdays_for_chronic=3)
        if agg["verdict"]["verdict"] == "range_persistent_stale":
            print("T15 OK: aggregate_range range_persistent_stale 100% non-GREEN ohne 3-Streak")
        else:
            fails.append(f"T15 FAIL: {agg['verdict']}")

    if fails:
        print(f"\n❌ Selftest FAIL: {len(fails)} Test(s) failed")
        for f in fails:
            print(f"   {f}")
        return 1
    print(f"\n✅ Selftest PASS: 15/15 Tests OK")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scan-Freshness-Monitor (V7.9.36): Read-only Diagnose.")
    ap.add_argument("--date",
                    help="Single-Day-Mode: ISO YYYY-MM-DD (default: heute)")
    ap.add_argument("--start-date",
                    help="Range-Mode Start (ISO YYYY-MM-DD)")
    ap.add_argument("--end-date",
                    help="Range-Mode Ende (ISO YYYY-MM-DD)")
    ap.add_argument("--stale-long-threshold", type=int,
                    default=_STALE_LONG_THRESHOLD,
                    help=f"Werktage ab denen ein fallback_age als 'stale_long' gilt "
                         f"(default {_STALE_LONG_THRESHOLD})")
    ap.add_argument("--min-workdays-for-chronic", type=int, default=3,
                    help="Range-Mode: ab wievielen Werktagen in Folge ein "
                         "chronic-Verdict greift (default 3)")
    ap.add_argument("--strict", action="store_true",
                    help="Set exit-code zu severity-Mapping (0/1/2/3, range: +4)")
    ap.add_argument("--selftest", action="store_true",
                    help="Inline-Selftest T01..T15 laufen lassen.")
    ap.add_argument("--no-write", action="store_true",
                    help="JSON/MD nicht auf Disk schreiben (nur stdout).")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    # Range-Mode wenn beide --start-date/--end-date gesetzt
    if args.start_date and args.end_date:
        agg = aggregate_range(args.start_date, args.end_date,
                              stale_long_threshold=args.stale_long_threshold,
                              min_workdays_for_chronic=args.min_workdays_for_chronic)
        payload = render_range_json(agg)
        md = render_range_markdown(payload)

        if not args.no_write:
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
            json_path = TOOLS_DIR / (
                f"scan_freshness_range_{args.start_date}_{args.end_date}.json"
            )
            md_path = BRIEFINGS_DIR / (
                f"Scan-Freshness-Range-{args.start_date}_{args.end_date}.md"
            )
            json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            md_path.write_text(md, encoding="utf-8")
            print(f"[OK] wrote {json_path}")
            print(f"[OK] wrote {md_path}")

        v = payload["verdict"]
        print(f"=== RANGE-VERDICT === {v['severity']} · {v['verdict']}")
        print(f"rationale: {v['rationale']}")
        if v.get("primary_target"):
            print(f"primary_target: {v['primary_target']}")

        if args.strict:
            return range_severity_to_exit_code(v["severity"], v["verdict"])
        return 0

    # Single-Day-Mode
    date_str = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    art = load_scan_artifact(date_str)
    verdict = classify_single(date_str, art,
                               stale_long_threshold=args.stale_long_threshold)
    payload = render_single_json(verdict, art)
    md = render_single_markdown(payload)

    if not args.no_write:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
        json_path = TOOLS_DIR / f"scan_freshness_{date_str}.json"
        md_path = BRIEFINGS_DIR / f"Scan-Freshness-{date_str}.md"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        md_path.write_text(md, encoding="utf-8")
        print(f"[OK] wrote {json_path}")
        print(f"[OK] wrote {md_path}")

    v = payload["verdict"]
    print(f"=== VERDICT === {v['severity']} · {v['verdict']}")
    print(f"rationale: {v['rationale']}")
    if v.get("primary_target"):
        print(f"primary_target: {v['primary_target']}")

    if args.strict:
        return severity_to_exit_code(v["severity"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
