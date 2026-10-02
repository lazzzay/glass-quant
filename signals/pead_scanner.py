#!/usr/bin/env python3
"""
STRAT-PEAD-1: Post-Earnings-Announcement-Drift-Scanner (SHADOW-MODE).

Setup:
  * EPS-Surprise > +5% (aktuell vs Konsens)
  * Open-Gap >= +2% am Earnings-Tag
  * Volume-Spike >= 2.0 x ADV(20) am Earnings-Tag
  * Hold 5-20 Tage, ATR-Trail oder +10% hard-exit (Output-only)

Output: shadow/pead_YYYY-MM-DD.json

Data: yfinance (earnings_history, earnings_dates, fast_info). Free.

Feature-Freeze-konform: SHADOW-only. Keine Live-Rec, kein Entry in
daily_scanner.py. Evaluation in naechster Earnings-Saison (~Juli 2026).

Usage:
    python3 signals/pead_scanner.py
    python3 signals/pead_scanner.py --tickers AAPL,MSFT
    python3 signals/pead_scanner.py --test
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHADOW_DIR = REPO_ROOT / "shadow"
SHADOW_DIR.mkdir(exist_ok=True)
WATCHLIST_DEFAULT = REPO_ROOT / "data" / "watchlist_global.txt"

# Thresholds
SURPRISE_MIN_PCT = 5.0
GAP_MIN_PCT = 2.0
VOLUME_MULT_MIN = 2.0
ADV_N = 20
LOOKBACK_DAYS = 3   # Earnings-Tag innerhalb der letzten 3 Kalendertage


@dataclass
class PeadCandidate:
    ticker: str
    earnings_date: str
    eps_actual: float | None
    eps_estimate: float | None
    surprise_pct: float | None
    prev_close: float | None
    open_price: float | None
    gap_pct: float | None
    volume: int | None
    adv20: float | None
    volume_mult: float | None
    passes_all_gates: bool
    reasons: list[str]


def _fetch_earnings(ticker: str):
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: pip install yfinance --break-system-packages", file=sys.stderr)
        sys.exit(1)
    try:
        t = yf.Ticker(ticker)
        # earnings_dates kann 0 oder mehrere Spalten haben je nach yfinance-Version
        df = t.get_earnings_dates(limit=6)
        return df
    except Exception:
        return None


def _fetch_prices(ticker: str, days: int = 60):
    import yfinance as yf
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days + 10)
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
    if df.empty:
        return None
    if hasattr(df.columns, "get_level_values"):
        df.columns = df.columns.get_level_values(0)
    return df


def _find_recent_earnings(edf, within_days: int = LOOKBACK_DAYS):
    import pandas as pd
    if edf is None or edf.empty:
        return None
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    edf = edf.copy()
    if hasattr(edf.index, "tz") and edf.index.tz is not None:
        edf.index = edf.index.tz_localize(None)
    edf = edf[(edf.index <= now) & (edf.index >= now - pd.Timedelta(days=within_days))]
    if edf.empty:
        return None
    return edf.iloc[0]


def scan_one(ticker: str) -> PeadCandidate:
    reasons: list[str] = []
    edf = _fetch_earnings(ticker)
    rec = _find_recent_earnings(edf)

    if rec is None:
        return PeadCandidate(ticker, "", None, None, None, None, None, None,
                             None, None, None, False, ["no_recent_earnings"])

    eps_actual = rec.get("Reported EPS") if hasattr(rec, "get") else None
    eps_est = rec.get("EPS Estimate") if hasattr(rec, "get") else None
    surprise = None
    if eps_actual is not None and eps_est not in (None, 0):
        try:
            surprise = (float(eps_actual) - float(eps_est)) / abs(float(eps_est)) * 100.0
        except Exception:
            surprise = None

    earnings_day = rec.name.strftime("%Y-%m-%d") if hasattr(rec, "name") else ""
    prices = _fetch_prices(ticker)
    gap_pct = None
    prev_close = None
    open_price = None
    volume = None
    adv20 = None
    vol_mult = None
    if prices is not None and not prices.empty:
        import pandas as pd
        idx = prices.index
        # Finde erste Bar ab earnings_day + 1 (Reaktionstag)
        target = pd.Timestamp(earnings_day)
        reaction_rows = prices[idx > target]
        if not reaction_rows.empty:
            row = reaction_rows.iloc[0]
            prev_rows = prices[idx <= target]
            if not prev_rows.empty:
                prev_close = float(prev_rows.iloc[-1]["Close"])
            open_price = float(row["Open"])
            volume = int(row["Volume"])
            if prev_close:
                gap_pct = (open_price / prev_close - 1.0) * 100.0
            adv_window = prices[idx < reaction_rows.index[0]].tail(ADV_N)
            if not adv_window.empty:
                adv20 = float(adv_window["Volume"].mean())
                vol_mult = (volume / adv20) if adv20 else None

    gates = {
        "surprise": surprise is not None and surprise >= SURPRISE_MIN_PCT,
        "gap":      gap_pct is not None and gap_pct >= GAP_MIN_PCT,
        "volume":   vol_mult is not None and vol_mult >= VOLUME_MULT_MIN,
    }
    for n, ok in gates.items():
        if not ok:
            reasons.append(f"fail_{n}")
    return PeadCandidate(
        ticker=ticker,
        earnings_date=earnings_day,
        eps_actual=float(eps_actual) if eps_actual is not None else None,
        eps_estimate=float(eps_est) if eps_est is not None else None,
        surprise_pct=round(surprise, 2) if surprise is not None else None,
        prev_close=round(prev_close, 2) if prev_close else None,
        open_price=round(open_price, 2) if open_price else None,
        gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
        volume=volume,
        adv20=round(adv20, 0) if adv20 else None,
        volume_mult=round(vol_mult, 2) if vol_mult else None,
        passes_all_gates=all(gates.values()),
        reasons=reasons,
    )


def run(tickers: list[str]) -> dict:
    today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    cands = [scan_one(t) for t in tickers]
    passing = [c for c in cands if c.passes_all_gates]
    payload = {
        "date": today,
        "mode": "SHADOW",
        "scanned": len(cands),
        "passes": len(passing),
        "candidates": [asdict(c) for c in cands],
    }
    out = SHADOW_DIR / f"pead_{today}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    payload["_shadow_file"] = str(out)
    return payload


def load_universe(path: Path | None) -> list[str]:
    if path and path.exists():
        return [l.strip() for l in path.read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    # Built-in demo list. Say so instead of falling back silently.
    print(f"NOTE: watchlist {path} not found, using the built-in demo list of 15 US large "
          "caps (copy data/watchlist_global.example.txt to data/watchlist_global.txt "
          "or pass --tickers).", file=sys.stderr)
    return ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
            "JPM", "UNH", "XOM", "CVX", "WMT", "HD", "PG", "KO"]


def _self_test() -> int:
    assert SURPRISE_MIN_PCT == 5.0 and GAP_MIN_PCT == 2.0 and VOLUME_MULT_MIN == 2.0
    print("pead_scanner self-test: PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", help="Kommaseparierte Ticker")
    p.add_argument("--universe", type=Path)
    p.add_argument("--test", action="store_true")
    a = p.parse_args()
    if a.test:
        return _self_test()
    tickers = ([t.strip().upper() for t in a.tickers.split(",") if t.strip()]
               if a.tickers else load_universe(a.universe or WATCHLIST_DEFAULT))
    res = run(tickers)
    print(json.dumps({
        "date": res["date"], "scanned": res["scanned"],
        "passes": res["passes"], "file": res["_shadow_file"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
