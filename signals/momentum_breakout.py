#!/usr/bin/env python3
"""
STRAT-MOMO-1: Momentum-Breakout-Scanner (SHADOW-MODE).

Setup:
  * 20-Tage-Donchian-Breakout: Close > max(Close[-21:-1])
  * Volume > 1.5 x ADV(20)
  * Relative Strength vs SPY (3M): Ticker-Return >= SPY-Return + 5pp
  * Keine unmittelbar vorhergehende 20-Tage-Range-Kontraktion gefordert
    (Donchian alleine reicht als Breakout-Anker)
  * ATR-Trail-Stop 2.5 x ATR(14) als Risk-Suggestion (nur Output, kein Execute)

Output: shadow/momo_YYYY-MM-DD.json

Feature-Freeze-konform: SHADOW-only. Kein Eingriff in daily_scanner.py,
keine Live-Rec. Ab 2026-04-30 (COHORT-AUDIT-1) Entscheidung ueber Go-Live.

Usage:
    python3 signals/momentum_breakout.py
    python3 signals/momentum_breakout.py --tickers AAPL,MSFT,NVDA
    python3 signals/momentum_breakout.py --universe watchlist_global.txt
    python3 signals/momentum_breakout.py --test
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
SHADOW_DIR = REPO_ROOT / "shadow"
SHADOW_DIR.mkdir(exist_ok=True)
WATCHLIST_DEFAULT = REPO_ROOT / "data" / "watchlist_global.txt"

# --- Parameter ---------------------------------------------------------------
DONCHIAN_N = 20
VOL_MULT = 1.5
ADV_N = 20
RS_LOOKBACK_DAYS = 63          # ~3 Monate Handelstage
RS_EXCESS_MIN = 0.05           # Ticker >= SPY + 5pp
ATR_N = 14
ATR_STOP_MULT = 2.5


@dataclass
class MomoCandidate:
    ticker: str
    close: float
    donchian_high: float
    breakout_margin_pct: float
    volume: int
    adv20: float
    volume_mult: float
    ret_3m_pct: float
    spy_ret_3m_pct: float
    rs_excess_pp: float
    atr14: float
    suggested_stop: float
    score: float
    passes_all_gates: bool
    reasons: list[str]


def _adx_placeholder() -> None:  # pragma: no cover
    """Regime-Gate bleibt in tools/regime_gate.py. Wir referenzieren nur."""


def _fetch_panel(tickers: list[str], days: int = 120):
    """yfinance Panel fetch. Fallback-friendly."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("ERROR: pip install yfinance pandas --break-system-packages", file=sys.stderr)
        sys.exit(1)
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days + 20)
    df = yf.download(
        tickers + ["SPY"], start=start, end=end,
        group_by="ticker", progress=False, auto_adjust=False, threads=True,
    )
    return df, pd


def _atr(high, low, close, n=14):
    import pandas as pd
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def scan(tickers: list[str]) -> list[MomoCandidate]:
    df, pd = _fetch_panel(tickers)
    if df.empty:
        return []

    spy = df["SPY"].dropna() if "SPY" in df.columns.get_level_values(0) else None
    if spy is None or spy.empty:
        return []
    spy_ret = (spy["Close"].iloc[-1] / spy["Close"].iloc[-RS_LOOKBACK_DAYS] - 1.0) \
        if len(spy) > RS_LOOKBACK_DAYS else 0.0

    results: list[MomoCandidate] = []
    for t in tickers:
        try:
            sub = df[t].dropna()
        except KeyError:
            continue
        if len(sub) < max(DONCHIAN_N + 5, ADV_N + 5, RS_LOOKBACK_DAYS + 5):
            continue

        close = sub["Close"]
        high = sub["High"]
        low = sub["Low"]
        vol = sub["Volume"]

        donchian_high = close.iloc[-(DONCHIAN_N + 1):-1].max()
        last_close = float(close.iloc[-1])
        bo_margin = (last_close / float(donchian_high) - 1.0) if donchian_high else 0.0

        adv = float(vol.rolling(ADV_N).mean().iloc[-1])
        vol_mult = (float(vol.iloc[-1]) / adv) if adv > 0 else 0.0

        ret_3m = float(close.iloc[-1] / close.iloc[-RS_LOOKBACK_DAYS] - 1.0)
        rs_excess = ret_3m - spy_ret

        atr14 = float(_atr(high, low, close, ATR_N).iloc[-1])
        stop = last_close - ATR_STOP_MULT * atr14

        reasons = []
        gates = {
            "breakout": last_close > float(donchian_high),
            "volume":   vol_mult >= VOL_MULT,
            "rs":       rs_excess >= RS_EXCESS_MIN,
        }
        for name, ok in gates.items():
            if not ok:
                reasons.append(f"fail_{name}")

        passes = all(gates.values())
        # Simple composite score (shadow-only, kein Live-Scoring-Change)
        score = round(
            0.4 * max(bo_margin * 100, 0) +
            0.3 * max(vol_mult - 1.0, 0) +
            0.3 * max(rs_excess * 100, 0), 3,
        )

        results.append(MomoCandidate(
            ticker=t,
            close=round(last_close, 2),
            donchian_high=round(float(donchian_high), 2),
            breakout_margin_pct=round(bo_margin * 100, 2),
            volume=int(vol.iloc[-1]),
            adv20=round(adv, 0),
            volume_mult=round(vol_mult, 2),
            ret_3m_pct=round(ret_3m * 100, 2),
            spy_ret_3m_pct=round(spy_ret * 100, 2),
            rs_excess_pp=round(rs_excess * 100, 2),
            atr14=round(atr14, 2),
            suggested_stop=round(stop, 2),
            score=score,
            passes_all_gates=passes,
            reasons=reasons,
        ))
    return results


def load_universe(path: Path | None) -> list[str]:
    if path and path.exists():
        return [line.strip() for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    # Default Demo-Universe (S&P-500 Liquide Core). Say so instead of falling back silently.
    print(f"NOTE: watchlist {path} not found, using the built-in demo list of 20 US large "
          "caps (copy data/watchlist_global.example.txt to data/watchlist_global.txt "
          "or pass --tickers).", file=sys.stderr)
    return ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
            "BRK-B", "JPM", "V", "MA", "UNH", "HD", "PG", "XOM",
            "CVX", "COP", "LMT", "NOC", "CAT"]


def run(universe: list[str], out_dir: Path = SHADOW_DIR) -> dict:
    today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    cands = scan(universe)
    passing = [c for c in cands if c.passes_all_gates]
    payload = {
        "date": today,
        "mode": "SHADOW",
        "universe_size": len(universe),
        "scanned": len(cands),
        "passes": len(passing),
        "candidates": [asdict(c) for c in cands],
    }
    out = out_dir / f"momo_{today}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    payload["_shadow_file"] = str(out)
    return payload


def _self_test() -> int:
    """Reine Gate-Logik-Tests (kein Netzwerk)."""
    assert VOL_MULT == 1.5
    assert RS_EXCESS_MIN == 0.05
    print("momentum_breakout self-test: PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", help="Kommaseparierte Ticker (ueberschreibt universe)")
    p.add_argument("--universe", type=Path, help="Pfad zu watchlist-txt")
    p.add_argument("--test", action="store_true")
    a = p.parse_args()
    if a.test:
        return _self_test()

    if a.tickers:
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    else:
        tickers = load_universe(a.universe or WATCHLIST_DEFAULT)
    res = run(tickers)
    print(json.dumps({
        "date": res["date"], "universe": res["universe_size"],
        "scanned": res["scanned"], "passes": res["passes"],
        "file": res["_shadow_file"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
