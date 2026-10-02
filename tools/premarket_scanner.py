#!/usr/bin/env python3
"""
Pre-Market Gap-Scanner (V6.4 Hebel E4)
=======================================

Zweck:
    Findet US-Aktien (Tier A/B) mit Pre-Market-Gap > +/- 3 % gegen Vortag-Close.
    Gap-Fill Statistik: ~70 % der Gaps bis 5 % werden innerhalb derselben Session
    zur 20d-EMA geschlossen (Mean-Reversion-Setup parallel zum Daily-Connors).

Aufruf:
    python3 premarket_scanner.py                    # Default: alle US Tier A/B
    python3 premarket_scanner.py --min-gap 2.5      # nur Gaps >= 2.5 %
    python3 premarket_scanner.py --tier A           # nur Tier A
    python3 premarket_scanner.py --out premarket    # Outfile-Prefix

Output:
    premarket_YYYY-MM-DD.csv + .json
    Spalten: ticker, tier, prev_close, premarket_price, gap_pct,
             ema20, gap_direction, setup_type, ema20_distance_pct,
             target_ema20_move_pct, volume_premarket

Setup-Typen:
    - gap_up_fade:    Pre-Market Lücke nach OBEN, Short-Setup (Gap-Fill zu EMA20 unten)
    - gap_down_bounce: Pre-Market Lücke nach UNTEN, Long-Setup (Gap-Fill zu EMA20 oben)
    - gap_extension:  Gap in Richtung EMA20 (wenig Mean-Reversion-Potenzial → filtern)

Hinweis:
    yfinance Pre-Market-Quotes sind best-effort. Bei fehlenden Daten
    wird der aktuelle 1m-Bar als Proxy genutzt. Call am besten um 08:45 CET
    (= 02:45 ET, vor regulärem US-Pre-Market ab 04:00 ET, aber mit gefüllten
    Futures/Fair-Value-Daten) oder 14:30 CET (= 08:30 ET, 1h vor US-Open).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# SCAN-IMMUTABILITY-1: Write-once Helper (2026-04-19)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_artifacts import write_immutable, ImmutableArtifactError  # noqa: E402

PREMARKET_VERSION = "V6.4"

try:
    import yfinance as yf
    import pandas as pd
except ImportError as e:
    if __name__ == "__main__":
        print(f"Fehler: {e}. Installiere via: pip install yfinance pandas --break-system-packages",
              file=sys.stderr)
        sys.exit(1)
    raise  # FIX 2026-06-01: Import-safe (kein Prozess-Kill bei fehlendem Dep)


def load_universe(universe_path: Path) -> pd.DataFrame:
    df = pd.read_csv(universe_path)
    # Nur US-Aktien (haben keine Punkt-Suffixe wie .DE .L .PA .SW .AS .HK .T etc.
    # und asset_class startet mit 'us_')
    df = df[df["asset_class"].str.startswith("us_", na=False)]
    return df.reset_index(drop=True)


def fetch_premarket_snapshot(ticker: str) -> dict:
    """Holt Pre-Market-Daten via yfinance fast_info + 1m-Bar-Fallback."""
    try:
        t = yf.Ticker(ticker)
        fi = getattr(t, "fast_info", None) or {}

        prev_close = None
        last_price = None
        pm_price = None
        pm_volume = None

        # fast_info Felder (yfinance >= 0.2.x)
        for key in ("previous_close", "previousClose", "regular_market_previous_close"):
            try:
                val = fi[key] if hasattr(fi, "__getitem__") else getattr(fi, key, None)
                if val:
                    prev_close = float(val)
                    break
            except (KeyError, TypeError, AttributeError):
                continue

        for key in ("last_price", "lastPrice", "regular_market_price"):
            try:
                val = fi[key] if hasattr(fi, "__getitem__") else getattr(fi, key, None)
                if val:
                    last_price = float(val)
                    break
            except (KeyError, TypeError, AttributeError):
                continue

        # Pre/Post-Market via info (langsamer, aber zuverlässiger für PM)
        try:
            info = t.info or {}
            pm_price = info.get("preMarketPrice") or info.get("postMarketPrice")
            if pm_price:
                pm_price = float(pm_price)
        except Exception:
            pm_price = None

        # Fallback: aktuellster 1m-Bar
        if pm_price is None:
            try:
                hist_1m = t.history(period="1d", interval="1m", prepost=True)
                if not hist_1m.empty:
                    pm_price = float(hist_1m["Close"].iloc[-1])
                    pm_volume = int(hist_1m["Volume"].sum())
            except Exception:
                pass

        # Fallback Fallback: last_price
        if pm_price is None and last_price:
            pm_price = last_price

        # EMA20 aus Daily
        ema20 = None
        try:
            daily = t.history(period="60d", interval="1d")
            if len(daily) >= 20:
                ema20 = float(daily["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
                if prev_close is None:
                    prev_close = float(daily["Close"].iloc[-1])
        except Exception:
            pass

        if not prev_close or not pm_price:
            return {"ticker": ticker, "status": "NO_DATA"}

        gap_pct = (pm_price - prev_close) / prev_close * 100.0
        ema20_dist_pct = None
        target_move_pct = None
        if ema20:
            ema20_dist_pct = (pm_price - ema20) / ema20 * 100.0
            target_move_pct = (ema20 - pm_price) / pm_price * 100.0

        # Setup-Typ bestimmen
        setup_type = "neutral"
        if abs(gap_pct) < 1.0:
            setup_type = "no_gap"
        elif ema20 and pm_price > ema20 and gap_pct > 0:
            setup_type = "gap_up_fade"        # Gap nach oben, EMA20 unten → Short/Fade-Setup
        elif ema20 and pm_price < ema20 and gap_pct < 0:
            setup_type = "gap_down_bounce"    # Gap nach unten, EMA20 oben → Long/Bounce-Setup
        elif ema20:
            setup_type = "gap_extension"      # Gap in Richtung EMA20 → Trend, nicht Reversal

        return {
            "ticker": ticker,
            "status": "OK",
            "prev_close": round(prev_close, 4),
            "premarket_price": round(pm_price, 4),
            "gap_pct": round(gap_pct, 3),
            "ema20": round(ema20, 4) if ema20 else None,
            "ema20_distance_pct": round(ema20_dist_pct, 3) if ema20_dist_pct is not None else None,
            "target_ema20_move_pct": round(target_move_pct, 3) if target_move_pct is not None else None,
            "setup_type": setup_type,
            "volume_premarket": pm_volume,
        }

    except Exception as e:
        return {"ticker": ticker, "status": f"ERROR: {type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--tier", default="A,B", help="comma-sep, default A,B")
    ap.add_argument("--min-gap", type=float, default=3.0, help="Min abs Gap %% für Alert")
    ap.add_argument("--out", default="premarket", help="Output-Prefix")
    # SCAN-IMMUTABILITY-1 (2026-04-19): Write-once-Schutz
    ap.add_argument("--force", action="store_true",
                    help="Existierendes Pre-Market-Artefakt ueberschreiben (Notfall)")
    ap.add_argument("--rescan", action="store_true",
                    help="In separaten __RESCAN_HHMMSS-Namespace schreiben")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    universe_path = script_dir / args.universe
    if not universe_path.exists():
        print(f"Universe-Datei nicht gefunden: {universe_path}", file=sys.stderr)
        sys.exit(1)

    df = load_universe(universe_path)
    tiers = [t.strip() for t in args.tier.split(",")]
    df = df[df["tier"].isin(tiers)]
    tickers = df["ticker"].tolist()

    print(f"[Pre-Market Scanner] {len(tickers)} US Tier-{','.join(tiers)} Aktien...",
          file=sys.stderr)

    results = []
    for i, ticker in enumerate(tickers, 1):
        snap = fetch_premarket_snapshot(ticker)
        # Meta mergen
        row = df[df["ticker"] == ticker].iloc[0]
        snap["tier"] = row["tier"]
        snap["sector"] = row["sector"]
        results.append(snap)
        if i % 20 == 0:
            print(f"  ... {i}/{len(tickers)}", file=sys.stderr)

    # Gap-Alerts filtern
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

    # CSV: nur Alerts + top 20 absolute Gaps für Kontext
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
            writer="premarket_scanner.py",
            writer_version=PREMARKET_VERSION,
            force=args.force, rescan=args.rescan,
            metadata={"date": today, "alert_count": len(alerts)},
        )
        json_actual = write_immutable(
            json_out, _write_json,
            writer="premarket_scanner.py",
            writer_version=PREMARKET_VERSION,
            force=args.force, rescan=args.rescan,
            metadata={"date": today, "alert_count": len(alerts),
                      "min_gap_pct": args.min_gap},
        )
    except ImmutableArtifactError as e:
        print(f"\n[SCAN-IMMUTABILITY-1] {e}", file=sys.stderr)
        sys.exit(3)
    csv_out, json_out = csv_actual, json_actual

    print(f"\n[Pre-Market] {len(alerts)} Gap-Alerts (>={args.min_gap} %) geschrieben:", file=sys.stderr)
    for a in alerts[:10]:
        print(f"  {a['ticker']:6} {a['setup_type']:18} gap={a['gap_pct']:+6.2f}% "
              f"ema20_dist={a.get('ema20_distance_pct', 0):+6.2f}% "
              f"target_move={a.get('target_ema20_move_pct', 0):+6.2f}%", file=sys.stderr)

    print(f"  CSV:  {csv_out}", file=sys.stderr)
    print(f"  JSON: {json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
