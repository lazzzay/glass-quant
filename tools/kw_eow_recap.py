#!/usr/bin/env python3
"""
V7.9.37 KW-EOW-RECAP-V2  (FEATURE, 2026-05-28 Do KW22).

Generischer Wochenabschluss-Recap mit 4 KPIs.  Nachfolger von
V7.9.33 kw21_eow_recap.py: Woche-agnostisch + scan_freshness als
4. KPI (aus V7.9.36 scan_freshness_monitor).

4 KPIs:
  1) Recs pro Werktag (scan_<DATE>.json: passes_threshold=True)
  2) PCR-Resilience-pct (% Werktage market_pcr.source=cboe_html_scrape
     UND tag != "unknown" UND ok=True)
  3) GDELT-Sentiment-pct (% Werktage news_sentiment.status="ok"
     UND n_live_ok > 0)
  4) Scan-Freshness-pct (% Werktage scan_freshness verdict="scan_fresh")
     via Import von scan_freshness_monitor.aggregate_range()

Read-only · keine Score-/Live-Aenderung · freeze-konform.
KW22-Bugfix-Quote NICHT angefasst (V7.9.34 hat sie Mo 25.05. verbraucht).

CLI:
    python3 tools/kw_eow_recap.py --iso-week 22
    python3 tools/kw_eow_recap.py --start-date 2026-05-25 --end-date 2026-05-29
    python3 tools/kw_eow_recap.py --selftest
    python3 tools/kw_eow_recap.py --iso-week 22 --strict

Exit-Codes (--strict):
    0 = GREEN  (alle 4 KPIs >= Schwelle)
    1 = YELLOW (1 KPI verletzt)
    2 = RED    (2 KPIs verletzt)
    3 = BLACK  (>=3 KPIs verletzt oder insufficient data)

Output:
    tools/kw_eow_recap_<START>_<END>.json
    Briefings/KW-EOW-Recap-<START>_<END>.md

"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Konstanten
# -----------------------------------------------------------------------------
GENERATOR = "kw_eow_recap/V7.9.37"

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"

ACTION_BUY_THRESHOLD = 7.0          # CLAUDE.md §3
MIN_RECS_TARGET = 4                 # CLAUDE.md §1 (KW21+ Ziel)
MIN_PCR_RESILIENCE_PCT = 0.60       # 60% Werktage PCR ok
MIN_GDELT_PCT = 0.60                # 60% Werktage GDELT ok
MIN_SCAN_FRESHNESS_PCT = 0.80       # 80% Werktage Scan fresh

REQ_PCR_SOURCE = "cboe_html_scrape"
INVALID_PCR_TAGS = {None, "", "unknown"}


# -----------------------------------------------------------------------------
# Date-Helper
# -----------------------------------------------------------------------------
def iso_week_workdays(year: int, week: int) -> List[date]:
    """Liefert die 5 Werktage (Mo-Fr) einer ISO-Kalenderwoche."""
    monday = date.fromisocalendar(year, week, 1)
    return [monday + timedelta(days=i) for i in range(5)]


def parse_iso_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def workdays_in_range(start: date, end: date) -> List[date]:
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# -----------------------------------------------------------------------------
# JSON-Read-Helper
# -----------------------------------------------------------------------------
def _safe_load_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# -----------------------------------------------------------------------------
# KPI 1: Recs/Win-Rate
# -----------------------------------------------------------------------------
def kpi_recs(workdays: List[date], tools_dir: Path) -> Dict[str, Any]:
    """Zaehlt passes_threshold=True Setups pro Werktag."""
    per_day: List[dict] = []
    total_recs = 0
    for d in workdays:
        path = tools_dir / f"scan_{d.isoformat()}.json"
        j = _safe_load_json(path)
        if j is None:
            per_day.append({"date": d.isoformat(), "recs": None, "reason": "missing"})
            continue
        top = j.get("top_setups", []) or []
        recs = 0
        for s in top:
            ps = s.get("pre_score")
            pt = s.get("passes_threshold", None)
            if pt is True or (isinstance(ps, (int, float)) and ps >= ACTION_BUY_THRESHOLD):
                recs += 1
        per_day.append({"date": d.isoformat(), "recs": recs, "reason": None})
        total_recs += recs
    n_data = sum(1 for r in per_day if r["recs"] is not None)
    avg = (total_recs / n_data) if n_data > 0 else None
    passing = total_recs >= MIN_RECS_TARGET if n_data > 0 else False
    return {
        "kpi": "recs",
        "per_day": per_day,
        "total_recs": total_recs,
        "n_workdays_with_data": n_data,
        "avg_per_workday": avg,
        "threshold": MIN_RECS_TARGET,
        "passing": passing,
    }


# -----------------------------------------------------------------------------
# KPI 2: PCR-Resilience-pct
# -----------------------------------------------------------------------------
def kpi_pcr_resilience(workdays: List[date], tools_dir: Path) -> Dict[str, Any]:
    per_day: List[dict] = []
    n_ok = 0
    n_data = 0
    for d in workdays:
        path = tools_dir / f"options_flow_{d.isoformat()}.json"
        j = _safe_load_json(path)
        if j is None:
            per_day.append({"date": d.isoformat(), "ok": None, "reason": "missing"})
            continue
        n_data += 1
        mp = j.get("market_pcr") or {}
        src = mp.get("source")
        tag = mp.get("tag")
        ok_flag = bool(mp.get("ok"))
        ok = (ok_flag and src == REQ_PCR_SOURCE and tag not in INVALID_PCR_TAGS)
        per_day.append({"date": d.isoformat(), "ok": ok,
                         "source": src, "tag": tag})
        if ok:
            n_ok += 1
    pct = (n_ok / n_data) if n_data > 0 else None
    passing = pct is not None and pct >= MIN_PCR_RESILIENCE_PCT
    return {
        "kpi": "pcr_resilience",
        "per_day": per_day,
        "n_ok": n_ok,
        "n_workdays_with_data": n_data,
        "pct": pct,
        "threshold": MIN_PCR_RESILIENCE_PCT,
        "passing": passing,
    }


# -----------------------------------------------------------------------------
# KPI 3: GDELT-Sentiment-pct
# -----------------------------------------------------------------------------
def kpi_gdelt(workdays: List[date], tools_dir: Path) -> Dict[str, Any]:
    per_day: List[dict] = []
    n_ok = 0
    n_data = 0
    for d in workdays:
        path = tools_dir / f"news_sentiment_{d.isoformat()}.json"
        j = _safe_load_json(path)
        if j is None:
            per_day.append({"date": d.isoformat(), "ok": None, "reason": "missing"})
            continue
        n_data += 1
        status = j.get("status")
        n_live = j.get("n_live_ok") or 0
        ok = (status == "ok" and isinstance(n_live, int) and n_live > 0)
        per_day.append({"date": d.isoformat(), "ok": ok,
                         "status": status, "n_live_ok": n_live})
        if ok:
            n_ok += 1
    pct = (n_ok / n_data) if n_data > 0 else None
    passing = pct is not None and pct >= MIN_GDELT_PCT
    return {
        "kpi": "gdelt_sentiment",
        "per_day": per_day,
        "n_ok": n_ok,
        "n_workdays_with_data": n_data,
        "pct": pct,
        "threshold": MIN_GDELT_PCT,
        "passing": passing,
    }


# -----------------------------------------------------------------------------
# KPI 4: Scan-Freshness-pct (NEU)
# -----------------------------------------------------------------------------
def kpi_scan_freshness(workdays: List[date], tools_dir: Path) -> Dict[str, Any]:
    """Importiert scan_freshness_monitor.aggregate_range() falls vorhanden.

    Fail-soft: bei Import-Fehler markiere KPI als 'insufficient_data'.
    """
    if not workdays:
        return {"kpi": "scan_freshness", "pct": None, "passing": False,
                "reason": "no_workdays", "per_day": []}

    start_s = workdays[0].isoformat()
    end_s = workdays[-1].isoformat()

    try:
        sys.path.insert(0, str(TOOLS_DIR))
        try:
            import scan_freshness_monitor as sfm  # type: ignore
        finally:
            try:
                sys.path.remove(str(TOOLS_DIR))
            except ValueError:
                pass
        agg = sfm.aggregate_range(start_s, end_s, tools_dir=tools_dir)
    except Exception as e:
        return {"kpi": "scan_freshness", "pct": None, "passing": False,
                "reason": f"import_failed: {type(e).__name__}: {str(e)[:80]}",
                "per_day": []}

    per_day_raw = agg.get("per_day", [])
    per_day: List[dict] = []
    n_fresh = 0
    n_data = 0
    for r in per_day_raw:
        d_val = r.get("date")
        d_str = d_val.isoformat() if hasattr(d_val, "isoformat") else str(d_val)
        verdict = r.get("verdict")
        if verdict is None:
            per_day.append({"date": d_str, "fresh": None,
                              "verdict": None, "reason": "missing"})
            continue
        n_data += 1
        fresh = (verdict == "scan_fresh")
        per_day.append({"date": d_str, "fresh": fresh, "verdict": verdict})
        if fresh:
            n_fresh += 1
    pct = (n_fresh / n_data) if n_data > 0 else None
    passing = pct is not None and pct >= MIN_SCAN_FRESHNESS_PCT
    return {
        "kpi": "scan_freshness",
        "per_day": per_day,
        "n_fresh": n_fresh,
        "n_workdays_with_data": n_data,
        "pct": pct,
        "threshold": MIN_SCAN_FRESHNESS_PCT,
        "passing": passing,
        "range_verdict": agg.get("verdict"),
    }


# -----------------------------------------------------------------------------
# Aggregate
# -----------------------------------------------------------------------------
def build_recap(start: date, end: date,
                 tools_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = tools_dir or TOOLS_DIR
    wd = workdays_in_range(start, end)
    k1 = kpi_recs(wd, base)
    k2 = kpi_pcr_resilience(wd, base)
    k3 = kpi_gdelt(wd, base)
    k4 = kpi_scan_freshness(wd, base)

    failures = sum(1 for k in (k1, k2, k3, k4) if not k.get("passing"))
    insufficient = sum(1 for k in (k1, k2, k3, k4)
                       if k.get("pct") is None and k.get("n_workdays_with_data", 0) == 0
                       and k.get("total_recs", 0) == 0)
    if insufficient >= 3:
        severity = "BLACK"
        verdict = "insufficient_data"
    elif failures == 0:
        severity, verdict = "GREEN", "week_healthy"
    elif failures == 1:
        severity, verdict = "YELLOW", "week_one_kpi_failed"
    elif failures == 2:
        severity, verdict = "RED", "week_two_kpis_failed"
    else:
        severity, verdict = "BLACK", "week_systemic_failure"

    sprint_plan = build_sprint_plan(k1, k2, k3, k4)

    return {
        "generator": GENERATOR,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "workdays": [d.isoformat() for d in wd],
        "kpis": {
            "recs": k1,
            "pcr_resilience": k2,
            "gdelt_sentiment": k3,
            "scan_freshness": k4,
        },
        "n_kpis_failing": failures,
        "severity": severity,
        "verdict": verdict,
        "sprint_plan": sprint_plan,
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


def build_sprint_plan(k1, k2, k3, k4) -> Dict[str, Any]:
    next_week_priorities: List[str] = []
    if not k1.get("passing"):
        next_week_priorities.append(
            f"Recs-Frequenz: nur {k1.get('total_recs')} Recs ({k1.get('threshold')} Ziel): "
            "Score-Pipeline + Layer-Heal pruefen (vgl. V7.9.17 layer_heal_scorecard)."
        )
    if not k2.get("passing"):
        pct = k2.get("pct")
        next_week_priorities.append(
            f"PCR-Resilience: nur {pct*100:.0f}% Werktage cboe_html_scrape ok, "
            "V7.9.27/31 Cache + Stage-4 Decommission-Verify."
            if pct is not None else
            "PCR-Resilience: keine Daten, options_flow_<date>.json Crons pruefen."
        )
    if not k3.get("passing"):
        pct = k3.get("pct")
        next_week_priorities.append(
            f"GDELT-Sentiment: nur {pct*100:.0f}% Werktage live, "
            "Mac-launchd-Cron statt Sandbox-Cron (V7.9.34/35 Diff-Tool aktuell)."
            if pct is not None else
            "GDELT-Sentiment: keine Daten, Mac-launchd-Cron Pflicht."
        )
    if not k4.get("passing"):
        pct = k4.get("pct")
        rv = k4.get("range_verdict", {}) or {}
        target = rv.get("primary_target", "mac_cron")
        next_week_priorities.append(
            f"Scan-Freshness: nur {pct*100:.0f}% Werktage fresh (primary_target={target}), "
            "Mac-launchd Scanner-Cron pruefen / heilen."
            if pct is not None else
            "Scan-Freshness: insufficient_data, scan_<date>.json fehlt fuer >=3 Tage."
        )
    if not next_week_priorities:
        next_week_priorities.append("Alle 4 KPIs GREEN, keine neuen Sprint-Items. Feature-Backlog (R3 RAG, Welle II Pydantic) priorisieren.")
    return {
        "priorities": next_week_priorities,
        "next_week_bugfix_target": next_week_priorities[0] if next_week_priorities else None,
    }


# -----------------------------------------------------------------------------
# Render
# -----------------------------------------------------------------------------
def render_md(recap: Dict[str, Any]) -> str:
    start = recap["start_date"]
    end = recap["end_date"]
    sev = recap["severity"]
    verdict = recap["verdict"]
    n_fail = recap["n_kpis_failing"]

    sev_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "BLACK": "⚫"}.get(sev, "?")

    lines = []
    lines.append(f"# KW-EOW-Recap · {start} → {end}")
    lines.append("")
    lines.append(f"**Verdict:** {sev_icon} **{sev}** · `{verdict}` · {n_fail}/4 KPIs failing")
    lines.append("")
    lines.append(f"_Generator: {recap['generator']}_")
    lines.append("")
    lines.append("## KPIs")
    lines.append("")
    lines.append("| KPI | Wert | Schwelle | Status |")
    lines.append("|---|---|---|---|")
    k1 = recap["kpis"]["recs"]
    lines.append(f"| Recs (total) | {k1.get('total_recs')} | >= {k1.get('threshold')} | "
                 f"{'PASS' if k1.get('passing') else 'FAIL'} |")
    for key, label in [("pcr_resilience", "PCR-Resilience"),
                        ("gdelt_sentiment", "GDELT-Sentiment"),
                        ("scan_freshness", "Scan-Freshness")]:
        k = recap["kpis"][key]
        pct = k.get("pct")
        pct_s = f"{pct*100:.0f}%" if pct is not None else "n/a"
        thr = f">= {int(k.get('threshold', 0)*100)}%"
        st = "PASS" if k.get("passing") else "FAIL"
        lines.append(f"| {label} | {pct_s} | {thr} | {st} |")
    lines.append("")
    lines.append("## KW-Sprint-Plan (naechste Woche)")
    lines.append("")
    for p in recap["sprint_plan"]["priorities"]:
        lines.append(f"- {p}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Read-only · keine Score-/Live-Aenderung · freeze-konform._")
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Selftest
# -----------------------------------------------------------------------------
def _write_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def selftest() -> int:
    print("=== KW-EOW-Recap-V2 selftest (V7.9.37) ===")
    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name} :: {detail}")
            failed += 1

    # T01: workdays_in_range
    s = date(2026, 5, 25)
    e = date(2026, 5, 29)
    wd = workdays_in_range(s, e)
    check("T01 workdays_in_range Mo-Fr = 5", len(wd) == 5, str(wd))

    # T02: iso_week_workdays
    wd2 = iso_week_workdays(2026, 22)
    check("T02 iso_week_workdays(2026,22) Mo=2026-05-25", wd2[0] == date(2026, 5, 25),
          str(wd2[0]))

    # T03: KPI Recs with fixtures (GREEN)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"scan_{d.isoformat()}.json",
                        {"top_setups": [{"pre_score": 7.5, "passes_threshold": True},
                                          {"pre_score": 7.1, "passes_threshold": True}]})
        k = kpi_recs(wd, td_p)
        check("T03 kpi_recs GREEN total=10", k["total_recs"] == 10 and k["passing"],
              str(k.get('total_recs')))

    # T04: KPI Recs FAIL (0 Recs)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"scan_{d.isoformat()}.json",
                        {"top_setups": [{"pre_score": 5.0, "passes_threshold": False}]})
        k = kpi_recs(wd, td_p)
        check("T04 kpi_recs FAIL total=0", k["total_recs"] == 0 and not k["passing"], "")

    # T05: KPI PCR GREEN
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"options_flow_{d.isoformat()}.json",
                        {"market_pcr": {"ok": True, "source": REQ_PCR_SOURCE,
                                        "tag": "neutral"}})
        k = kpi_pcr_resilience(wd, td_p)
        check("T05 kpi_pcr GREEN pct=1.0", k["pct"] == 1.0 and k["passing"], str(k.get('pct')))

    # T06: KPI PCR FAIL (source != cboe_html_scrape)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"options_flow_{d.isoformat()}.json",
                        {"market_pcr": {"ok": True, "source": "yfinance",
                                        "tag": "unknown"}})
        k = kpi_pcr_resilience(wd, td_p)
        check("T06 kpi_pcr FAIL pct=0.0", k["pct"] == 0.0 and not k["passing"], "")

    # T07: KPI GDELT GREEN
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"news_sentiment_{d.isoformat()}.json",
                        {"status": "ok", "n_live_ok": 250})
        k = kpi_gdelt(wd, td_p)
        check("T07 kpi_gdelt GREEN pct=1.0", k["pct"] == 1.0 and k["passing"], str(k.get('pct')))

    # T08: KPI GDELT FAIL (all sandbox-blocked)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"news_sentiment_{d.isoformat()}.json",
                        {"status": "fail_soft_sandbox_egress", "n_live_ok": 0})
        k = kpi_gdelt(wd, td_p)
        check("T08 kpi_gdelt FAIL pct=0.0", k["pct"] == 0.0 and not k["passing"], "")

    # T09: KPI Scan-Freshness via import (GREEN setup)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        # fresh scan_<date>.json fuer jeden Werktag
        for d in wd:
            _write_json(td_p / f"scan_{d.isoformat()}.json",
                        {"scan_date": d.isoformat(),
                         "top_setups": [{"pre_score": 7.5, "passes_threshold": True}]})
        k = kpi_scan_freshness(wd, td_p)
        # bei GREEN sollte pct >= 0.8 sein
        ok = k.get("pct") is not None and k.get("pct") >= 0.8
        check("T09 kpi_scan_freshness GREEN pct>=0.8",
              ok and k.get("passing"),
              f"pct={k.get('pct')} reason={k.get('reason')}")

    # T10: KPI Scan-Freshness FAIL (alle fallback)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"scan_{d.isoformat()}.json",
                        {"scan_date": (d - timedelta(days=10)).isoformat(),
                         "_fallback_from": f"scan_{(d - timedelta(days=10)).isoformat()}.json",
                         "_fallback_age_werktage": 8,
                         "top_setups": []})
        k = kpi_scan_freshness(wd, td_p)
        # bei FAIL sollte pct < 0.8 ODER None und passing=False
        ok_fail = (k.get("pct") is None or k.get("pct") < 0.8) and not k.get("passing")
        check("T10 kpi_scan_freshness FAIL",
              ok_fail, f"pct={k.get('pct')}")

    # T11: build_recap GREEN end-to-end
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"scan_{d.isoformat()}.json",
                        {"scan_date": d.isoformat(),
                         "top_setups": [{"pre_score": 7.5, "passes_threshold": True},
                                          {"pre_score": 7.1, "passes_threshold": True}]})
            _write_json(td_p / f"options_flow_{d.isoformat()}.json",
                        {"market_pcr": {"ok": True, "source": REQ_PCR_SOURCE,
                                        "tag": "neutral"}})
            _write_json(td_p / f"news_sentiment_{d.isoformat()}.json",
                        {"status": "ok", "n_live_ok": 250})
        recap = build_recap(s, e, tools_dir=td_p)
        check("T11 build_recap GREEN sev=GREEN",
              recap["severity"] == "GREEN" and recap["n_kpis_failing"] == 0,
              f"sev={recap['severity']} fail={recap['n_kpis_failing']}")

    # T12: build_recap BLACK (all KPIs fail)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        for d in wd:
            _write_json(td_p / f"scan_{d.isoformat()}.json",
                        {"scan_date": (d - timedelta(days=10)).isoformat(),
                         "_fallback_from": f"scan_{(d - timedelta(days=10)).isoformat()}.json",
                         "_fallback_age_werktage": 8,
                         "top_setups": []})
            _write_json(td_p / f"options_flow_{d.isoformat()}.json",
                        {"market_pcr": {"ok": False, "source": "yfinance",
                                        "tag": "unknown"}})
            _write_json(td_p / f"news_sentiment_{d.isoformat()}.json",
                        {"status": "fail_soft_sandbox_egress", "n_live_ok": 0})
        recap = build_recap(s, e, tools_dir=td_p)
        check("T12 build_recap BLACK sev=BLACK",
              recap["severity"] == "BLACK" and recap["n_kpis_failing"] >= 3,
              f"sev={recap['severity']} fail={recap['n_kpis_failing']}")

    # T13: render_md sanity
    md = render_md(recap)
    check("T13 render_md contains KW-EOW-Recap header",
          "KW-EOW-Recap" in md, md[:100])

    # T14: sprint_plan emits items when failing
    sp = recap["sprint_plan"]
    check("T14 sprint_plan has >=1 priority", len(sp["priorities"]) >= 1, str(sp))

    print()
    print(f"=== SUMMARY ===  {passed} passed, {failed} failed (of {passed+failed})")
    return 0 if failed == 0 else 1


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="V7.9.37 KW-EOW-Recap-V2: Generischer 4-KPI-Wochenabschluss."
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--iso-week", type=int, default=None,
                   help="ISO-Kalenderwoche (Default: aktuelle Woche)")
    g.add_argument("--start-date", type=str, default=None,
                   help="YYYY-MM-DD; muss mit --end-date kombiniert sein")
    ap.add_argument("--end-date", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--year", type=int, default=None,
                    help="Jahr fuer --iso-week (Default aktuelles Jahr)")
    ap.add_argument("--tools-dir", type=str, default=None)
    ap.add_argument("--strict", action="store_true",
                    help="Exit-Code semantics: 0=GREEN 1=YELLOW 2=RED 3=BLACK")
    ap.add_argument("--no-write", action="store_true",
                    help="Nur JSON nach stdout, kein File-Write")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    today = date.today()
    if args.start_date and args.end_date:
        start = parse_iso_date(args.start_date)
        end = parse_iso_date(args.end_date)
    else:
        year = args.year or today.year
        wk = args.iso_week or today.isocalendar()[1]
        wd = iso_week_workdays(year, wk)
        start, end = wd[0], wd[-1]

    tools_dir = Path(args.tools_dir) if args.tools_dir else TOOLS_DIR

    recap = build_recap(start, end, tools_dir=tools_dir)

    if not args.no_write:
        out_json = tools_dir / f"kw_eow_recap_{start.isoformat()}_{end.isoformat()}.json"
        out_json.write_text(json.dumps(recap, indent=2, default=str), encoding="utf-8")
        BRIEFINGS_DIR.mkdir(exist_ok=True)
        out_md = BRIEFINGS_DIR / f"KW-EOW-Recap-{start.isoformat()}_{end.isoformat()}.md"
        out_md.write_text(render_md(recap), encoding="utf-8")
        print(f"WROTE  {out_json}")
        print(f"WROTE  {out_md}")
    else:
        print(json.dumps(recap, indent=2, default=str))

    sev = recap["severity"]
    n_fail = recap["n_kpis_failing"]
    print(f"=== VERDICT ===  {sev}  ({n_fail}/4 KPIs failing)  {recap['verdict']}")
    for p in recap["sprint_plan"]["priorities"]:
        print(f"  -> {p}")

    if args.strict:
        return {"GREEN": 0, "YELLOW": 1, "RED": 2, "BLACK": 3}.get(sev, 3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
