#!/usr/bin/env python3
"""
Pre-Market Gap-Scanner: Parallel-Wrapper
==========================================

Identische Logik zu premarket_scanner.py, aber mit ThreadPoolExecutor
(yfinance ist I/O-bound → Threads geben ~6-8x Speedup).

Aufruf:
    python3 premarket_scanner_parallel.py --tier A,B --min-gap 3.0
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_artifacts import write_immutable, ImmutableArtifactError  # noqa: E402
from premarket_scanner import (  # noqa: E402
    PREMARKET_VERSION,
    load_universe,
    fetch_premarket_snapshot,
)

import pandas as pd  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--tier", default="A,B")
    ap.add_argument("--min-gap", type=float, default=3.0)
    ap.add_argument("--out", default="premarket")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rescan", action="store_true")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    universe_path = script_dir / args.universe
    df = load_universe(universe_path)
    tiers = [t.strip() for t in args.tier.split(",")]
    df = df[df["tier"].isin(tiers)]
    tickers = df["ticker"].tolist()

    print(f"[Pre-Market Scanner Parallel] {len(tickers)} US Tier-{','.join(tiers)} "
          f"Aktien, workers={args.workers} ...", file=sys.stderr)

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_premarket_snapshot, t): t for t in tickers}
        for fut in as_completed(futs):
            ticker = futs[fut]
            try:
                snap = fut.result()
            except Exception as e:
                snap = {"ticker": ticker, "status": f"ERROR: {type(e).__name__}: {e}"}
            row = df[df["ticker"] == ticker].iloc[0]
            snap["tier"] = row["tier"]
            snap["sector"] = row["sector"]
            results.append(snap)
            done += 1
            if done % 40 == 0:
                print(f"  ... {done}/{len(tickers)}", file=sys.stderr)

    # Sortieren wie Original (nach Reihenfolge im Universe)
    order = {t: i for i, t in enumerate(tickers)}
    results.sort(key=lambda r: order.get(r["ticker"], 9999))

    alerts = [
        r for r in results
        if r.get("status") == "OK"
        and abs(r.get("gap_pct", 0)) >= args.min_gap
        and r.get("setup_type") in ("gap_up_fade", "gap_down_bounce")
    ]
    alerts.sort(key=lambda r: abs(r.get("gap_pct", 0)), reverse=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    csv_out = script_dir / f"{args.out}_{today}.csv"
    json_out = script_dir / f"{args.out}_{today}.json"

    fields = ["ticker", "tier", "sector", "setup_type", "gap_pct",
              "prev_close", "premarket_price", "ema20",
              "ema20_distance_pct", "target_ema20_move_pct",
              "volume_premarket", "status"]

    def _write_csv(p):
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in alerts:
                w.writerow(r)

    payload = {
        "date": today,
        "system_version": "V6.4",
        "scanner": "premarket_gap",
        "min_gap_pct": args.min_gap,
        "total_scanned": len(results),
        "ok_count": sum(1 for r in results if r.get("status") == "OK"),
        "alert_count": len(alerts),
        "alerts": alerts,
        "all_results": results,
    }

    def _write_json(p):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)

    try:
        csv_actual = write_immutable(
            csv_out, _write_csv,
            writer="premarket_scanner_parallel.py",
            writer_version=PREMARKET_VERSION,
            force=args.force, rescan=args.rescan,
            metadata={"date": today, "alert_count": len(alerts)},
        )
        json_actual = write_immutable(
            json_out, _write_json,
            writer="premarket_scanner_parallel.py",
            writer_version=PREMARKET_VERSION,
            force=args.force, rescan=args.rescan,
            metadata={"date": today, "alert_count": len(alerts),
                      "min_gap_pct": args.min_gap},
        )
    except ImmutableArtifactError as e:
        print(f"\n[SCAN-IMMUTABILITY-1] {e}", file=sys.stderr)
        sys.exit(3)

    print(f"\n[Pre-Market] {len(alerts)} Gap-Alerts (>={args.min_gap} %) geschrieben:",
          file=sys.stderr)
    for a in alerts[:10]:
        print(f"  {a['ticker']:6} {a['setup_type']:18} gap={a['gap_pct']:+6.2f}% "
              f"ema20_dist={a.get('ema20_distance_pct', 0):+6.2f}% "
              f"target_move={a.get('target_ema20_move_pct', 0):+6.2f}%",
              file=sys.stderr)

    print(f"  CSV:  {csv_actual}", file=sys.stderr)
    print(f"  JSON: {json_actual}", file=sys.stderr)


if __name__ == "__main__":
    main()
