#!/usr/bin/env python3
"""
universe_coverage.py: Fill-Rate-Tracker für das Scanner-Universe

Zweck
=====
Löst das Ops-Ticket "Universe-Fill 97.9 %" strukturell: statt die 13 Missing-
Ticker nur einmalig zu listen, trackt dieses Tool die Fill-Rate über Zeit und
erkennt automatisch Delistings, Rename-Events oder schleichende Datenquellen-
Degradation.

Ansatz
------
Quelle der Wahrheit für das Universe: `tools/universe.csv` (610 Tickers).
Quelle der tatsächlich gescanten Tickers: `tools/scan_YYYY-MM-DD.json`
`ticker_map` (597).

Per Tag wird ein kleiner Report geschrieben:
    tools/universe_coverage_YYYY-MM-DD.json
    {
        "date": "...",
        "universe_total": 610,
        "scanned_total": 597,
        "fill_rate": 0.9787,
        "missing": ["TKR1", "TKR2", ...],
        "newly_missing_vs_prev": ["..."],   # neu hinzugekommene Missings
        "newly_recovered_vs_prev": ["..."]  # wiederaufgetauchte
    }

Thresholds
----------
    fill_rate <  0.95  → WARN  (Exit 1)
    fill_rate <  0.90  → CRIT  (Exit 2)
    newly_missing ≥ 3   → WARN  (Exit 1)

Usage
-----
    python3 tools/universe_coverage.py
    python3 tools/universe_coverage.py --date 2026-04-17
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"

WARN_FILL_RATE = 0.95
CRIT_FILL_RATE = 0.90
WARN_NEW_MISSING = 3


def load_universe(universe_csv: Path) -> set[str]:
    """Liest tools/universe.csv: erwartet Spalte 'ticker' oder 'symbol'."""
    with universe_csv.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        tickers = set()
        for row in reader:
            t = (row.get("ticker") or row.get("symbol") or "").strip()
            if t:
                tickers.add(t)
        return tickers


def load_scanned(scan_json: Path) -> set[str]:
    """Holt die gescannten Tickers aus ticker_map im scan_*.json."""
    doc = json.loads(scan_json.read_text(encoding="utf-8"))
    tmap = doc.get("ticker_map") or {}
    return set(tmap.keys())


def load_previous_missing(date: str) -> set[str] | None:
    """Sucht den zuletzt geschriebenen Coverage-Report vor `date`."""
    reports = sorted(TOOLS_DIR.glob("universe_coverage_*.json"), reverse=True)
    for r in reports:
        stem = r.stem  # universe_coverage_2026-04-17
        d = stem.replace("universe_coverage_", "")
        if d < date:
            try:
                return set(json.loads(r.read_text(encoding="utf-8")).get("missing") or [])
            except Exception:
                continue
    return None


def analyze(date: str) -> dict:
    universe_csv = TOOLS_DIR / "universe.csv"
    scan_json = TOOLS_DIR / f"scan_{date}.json"

    if not universe_csv.exists():
        return {"date": date, "status": "MISSING_UNIVERSE_CSV", "error": str(universe_csv)}
    if not scan_json.exists():
        return {"date": date, "status": "MISSING_SCAN", "error": str(scan_json)}

    universe = load_universe(universe_csv)
    scanned = load_scanned(scan_json)
    missing = sorted(universe - scanned)
    fill_rate = (len(scanned) / len(universe)) if universe else 0.0

    prev_missing = load_previous_missing(date)
    newly_missing = []
    newly_recovered = []
    if prev_missing is not None:
        newly_missing = sorted(set(missing) - prev_missing)
        newly_recovered = sorted(prev_missing - set(missing))

    findings = []
    if fill_rate < CRIT_FILL_RATE:
        findings.append(("CRIT", "FILL_RATE_CRIT", f"Fill-Rate {fill_rate:.3%} < {CRIT_FILL_RATE:.0%}"))
    elif fill_rate < WARN_FILL_RATE:
        findings.append(("WARN", "FILL_RATE_WARN", f"Fill-Rate {fill_rate:.3%} < {WARN_FILL_RATE:.0%}"))
    if len(newly_missing) >= WARN_NEW_MISSING:
        findings.append(("WARN", "NEW_MISSING_WARN",
                         f"{len(newly_missing)} neue Missing-Ticker vs. Vortag: {', '.join(newly_missing[:8])}"))

    return {
        "date": date,
        "status": "OK",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "universe_total": len(universe),
        "scanned_total": len(scanned),
        "fill_rate": round(fill_rate, 5),
        "missing_count": len(missing),
        "missing": missing,
        "newly_missing_vs_prev": newly_missing,
        "newly_recovered_vs_prev": newly_recovered,
        "findings": [{"severity": s, "code": c, "msg": m} for s, c, m in findings],
    }


def print_report(r: dict) -> None:
    if r.get("status") != "OK":
        print(f"  UNIVERSE-COVERAGE {r.get('date')}: {r.get('status')}: {r.get('error')}")
        return

    print(f"\n╔══ UNIVERSE-COVERAGE {r['date']} ══╗")
    print(f"  Total: {r['universe_total']} · gescannt: {r['scanned_total']} · Fill: {r['fill_rate']:.3%}")
    print(f"  Missing: {r['missing_count']}")
    if r["missing"]:
        chunks = [r["missing"][i:i+8] for i in range(0, len(r["missing"]), 8)]
        for c in chunks:
            print("    " + "  ".join(f"{t:<8}" for t in c))
    if r["newly_missing_vs_prev"]:
        print(f"\n  NEU missing (vs. Vortag): {', '.join(r['newly_missing_vs_prev'])}")
    if r["newly_recovered_vs_prev"]:
        print(f"  Recovered (vs. Vortag): {', '.join(r['newly_recovered_vs_prev'])}")
    print()
    for f in r["findings"]:
        print(f"  [{f['severity']}] {f['code']}: {f['msg']}")
    if not r["findings"]:
        print("  ✓ Keine Findings.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Universe-Coverage-Tracker")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    r = analyze(args.date)

    out_path = TOOLS_DIR / f"universe_coverage_{args.date}.json"
    out_path.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet:
        print_report(r)
        print(f"Report: {out_path.relative_to(PROJECT_ROOT)}")

    if r.get("status") != "OK":
        return 3
    severities = {f["severity"] for f in r.get("findings", [])}
    if "CRIT" in severities:
        return 2
    if "WARN" in severities:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
