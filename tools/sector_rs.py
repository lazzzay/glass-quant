#!/usr/bin/env python3
"""
Sektor-RS-Matrix (V6.4 Hebel E5)
=================================

Zweck:
    Berechnet Relative-Strength (RS) der 11 GICS-Sektoren vs SPY (S&P 500).
    Maps custom-Sector-Labels aus universe.csv auf GICS-Standard-Sektoren.
    Liefert Score-Bonus/Malus für Mean-Reversion-Setups:
        - Long-Setup in Sektor mit schwacher RS → Malus (Falling-Knife-Risiko)
        - Long-Setup in Sektor mit starker RS → Bonus (Rückenwind)

Metriken:
    rs_1m  = ETF_return_20d / SPY_return_20d
    rs_3m  = ETF_return_60d / SPY_return_60d
    rs_slope = lin-reg slope of (ETF/SPY ratio) über 20d → Trendrichtung der RS
    sector_rs_score = -2 (sehr schwach) bis +2 (sehr stark), diskrete Skala

Output:
    sector_rs_YYYY-MM-DD.json
    - pro Sektor: rs_1m, rs_3m, rs_slope, sector_rs_score, regime_tag
    - ticker_map: pro Universe-Ticker → (gics_sector, sector_rs_score)

Aufruf:
    python3 sector_rs.py
    python3 sector_rs.py --out sector_rs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    import yfinance as yf
except ImportError as e:
    if __name__ == "__main__":
        print(f"Fehler: {e}. Installiere via: pip install yfinance pandas numpy --break-system-packages",
              file=sys.stderr)
        sys.exit(1)
    raise  # FIX 2026-06-01: Import-safe (kein Prozess-Kill bei fehlendem Dep)


# GICS Sektoren + SPDR Sector-ETFs
GICS_ETFS = {
    "Technology":            "XLK",
    "Healthcare":            "XLV",
    "Financials":            "XLF",
    "Consumer Discretionary":"XLY",
    "Consumer Staples":      "XLP",
    "Industrials":           "XLI",
    "Energy":                "XLE",
    "Materials":             "XLB",
    "Utilities":             "XLU",
    "Real Estate":           "XLRE",
    "Communication Services":"XLC",
}

# Prefix-basiertes Mapping (robust für alle Universe-Labels der Form "Prefix/Sub")
# Reihenfolge matters: spezifischere Keys zuerst.
PREFIX_TO_GICS = [
    # Specials mit Sub-Suffix, die von Default-Prefix abweichen
    ("Consumer/Staples",   "Consumer Staples"),
    ("Consumer/Food",      "Consumer Staples"),
    ("Consumer/Beverage",  "Consumer Staples"),
    ("Consumer/Tobacco",   "Consumer Staples"),
    ("Consumer/Cosmetics", "Consumer Staples"),
    ("Consumer/Household", "Consumer Staples"),
    ("Consumer/Staple",    "Consumer Staples"),
    ("Consumer/Chemicals", "Consumer Staples"),
    ("Energy/Utilities",   "Utilities"),
    ("Energy/Solar",       "Utilities"),
    ("Energy/Wind",        "Utilities"),
    ("Energy/Nuclear",     "Utilities"),
    ("Energy/FuelCell",    "Utilities"),
    ("Energy/Hydrogen",    "Utilities"),
    ("Energy/BioFuel",     "Utilities"),
    ("Industrial/Energy",  "Utilities"),
    ("Utility",            "Utilities"),
    ("Utilities",          "Utilities"),
    # Default Prefixe
    ("Tech",               "Technology"),
    ("Healthcare",         "Healthcare"),
    ("Financial",          "Financials"),
    ("Services/HR",        "Industrials"),
    ("Industrial",         "Industrials"),
    ("Aerospace",          "Industrials"),
    ("Defense",            "Industrials"),
    ("Transport",          "Industrials"),
    ("Construction",       "Industrials"),
    ("Consumer",           "Consumer Discretionary"),
    ("Auto",               "Consumer Discretionary"),
    ("Energy",             "Energy"),
    ("Materials",          "Materials"),
    ("REIT",               "Real Estate"),
    ("Real Estate",        "Real Estate"),
    ("Real-Estate",        "Real Estate"),
    ("Media",              "Communication Services"),
    ("Telecom",            "Communication Services"),
    # Bewusst ohne GICS-Mapping
    ("ETF",                None),
    ("Commodity",          None),
    ("Crypto",             None),
]


def map_custom_to_gics(custom: str):
    """Mappe Universe-Sector auf GICS via Prefix. Returns gics or None."""
    if not custom:
        return None
    # Exakte Matches zuerst (z.B. "Utility" oder "Utilities")
    for prefix, gics in PREFIX_TO_GICS:
        if custom == prefix:
            return gics
    # Prefix-Matches (z.B. "Tech/AI" → "Technology")
    for prefix, gics in PREFIX_TO_GICS:
        if custom.startswith(prefix + "/") or custom.startswith(prefix + "-"):
            return gics
    return None

BENCHMARK = "SPY"


def fetch_close(ticker: str, period: str = "90d") -> pd.Series | None:
    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d")
        if df.empty or len(df) < 25:
            return None
        return df["Close"]
    except Exception as e:
        print(f"[sector_rs] Fetch {ticker}: {e}", file=sys.stderr)
        return None


def compute_rs(etf_close: pd.Series, bench_close: pd.Series) -> dict:
    etf_close, bench_close = etf_close.align(bench_close, join="inner")
    if len(etf_close) < 25:
        return {"rs_1m": None, "rs_3m": None, "rs_slope": None, "sector_rs_score": 0}

    # 20d (~1M) Return-Ratio
    etf_ret_20 = etf_close.iloc[-1] / etf_close.iloc[-21] - 1
    bench_ret_20 = bench_close.iloc[-1] / bench_close.iloc[-21] - 1
    rs_1m = etf_ret_20 - bench_ret_20  # Outperformance in Prozentpunkten

    # 60d (~3M) Return-Ratio (falls genug Historie)
    if len(etf_close) >= 61:
        etf_ret_60 = etf_close.iloc[-1] / etf_close.iloc[-61] - 1
        bench_ret_60 = bench_close.iloc[-1] / bench_close.iloc[-61] - 1
        rs_3m = etf_ret_60 - bench_ret_60
    else:
        rs_3m = None

    # RS-Slope: lin-reg von (etf/bench)-Ratio über letzte 20 Tage
    ratio = (etf_close / bench_close).iloc[-20:]
    x = np.arange(len(ratio))
    slope, _ = np.polyfit(x, ratio.values, 1)
    rs_slope = float(slope) / float(ratio.iloc[0]) * 100  # slope normalisiert in % der Start-Ratio

    # Score-Mapping: -2 bis +2
    # rs_1m in Prozentpunkten (z.B. 0.03 = +3 % Outperformance vs SPY 20d)
    score = 0
    if rs_1m is not None:
        if rs_1m > 0.04: score += 1
        elif rs_1m > 0.02: score += 0.5
        elif rs_1m < -0.04: score -= 1
        elif rs_1m < -0.02: score -= 0.5
    if rs_slope is not None:
        if rs_slope > 0.2: score += 0.5
        elif rs_slope > 0.1: score += 0.25
        elif rs_slope < -0.2: score -= 0.5
        elif rs_slope < -0.1: score -= 0.25
    if rs_3m is not None:
        if rs_3m > 0.06: score += 0.5
        elif rs_3m < -0.06: score -= 0.5

    # Clamp auf [-2, +2]
    score = max(-2.0, min(2.0, score))

    # Regime-Tag
    if score >= 1.5: tag = "leader"
    elif score >= 0.5: tag = "strong"
    elif score <= -1.5: tag = "laggard"
    elif score <= -0.5: tag = "weak"
    else: tag = "neutral"

    return {
        "rs_1m": round(float(rs_1m), 4) if rs_1m is not None else None,
        "rs_3m": round(float(rs_3m), 4) if rs_3m is not None else None,
        "rs_slope": round(float(rs_slope), 4) if rs_slope is not None else None,
        "sector_rs_score": round(score, 2),
        "regime_tag": tag,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--out", default="sector_rs")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    universe_path = script_dir / args.universe

    # 1. SPY Benchmark
    print("[sector_rs] Lade SPY-Benchmark...", file=sys.stderr)
    spy_close = fetch_close(BENCHMARK)
    if spy_close is None:
        print("FATAL: SPY-Daten nicht verfügbar", file=sys.stderr)
        sys.exit(1)

    # 2. Pro Sektor ETF laden und RS berechnen
    sector_results = {}
    print(f"[sector_rs] Berechne RS für {len(GICS_ETFS)} GICS-Sektoren...", file=sys.stderr)
    for sector, etf in GICS_ETFS.items():
        close = fetch_close(etf)
        if close is None:
            sector_results[sector] = {"etf": etf, "error": "no_data",
                                      "sector_rs_score": 0, "regime_tag": "unknown"}
            continue
        metrics = compute_rs(close, spy_close)
        metrics["etf"] = etf
        sector_results[sector] = metrics

    # 3. Ranking
    ranking = sorted(
        [(s, v.get("sector_rs_score", 0)) for s, v in sector_results.items()],
        key=lambda x: x[1], reverse=True
    )

    # 4. Ticker-Mapping aus Universe
    ticker_map = {}
    unmapped = []
    if universe_path.exists():
        df = pd.read_csv(universe_path)
        for _, row in df.iterrows():
            custom = row.get("sector", "")
            gics = map_custom_to_gics(custom)
            if gics is None:
                unmapped.append(custom)
                continue
            score = sector_results.get(gics, {}).get("sector_rs_score", 0)
            tag = sector_results.get(gics, {}).get("regime_tag", "unknown")
            ticker_map[row["ticker"]] = {
                "custom_sector": custom,
                "gics_sector": gics,
                "sector_rs_score": score,
                "regime_tag": tag,
            }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = script_dir / f"{args.out}_{today}.json"

    output = {
        "date": today,
        "system_version": "V6.4",
        "benchmark": BENCHMARK,
        "sectors": sector_results,
        "ranking": [{"sector": s, "score": sc} for s, sc in ranking],
        "ticker_map": ticker_map,
        "mapped_tickers": len(ticker_map),
        "unmapped_sectors": sorted(set(unmapped)) if unmapped else [],
    }
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Console-Summary
    print("\n=== Sektor-RS-Ranking (vs SPY) ===", file=sys.stderr)
    for sector, score in ranking:
        tag = sector_results.get(sector, {}).get("regime_tag", "?")
        rs1 = sector_results.get(sector, {}).get("rs_1m", 0) or 0
        print(f"  {sector:24} score={score:+.2f}  1m-RS={rs1:+.2%}  [{tag}]", file=sys.stderr)

    print(f"\nMapped Tickers: {len(ticker_map)} | Unmapped Sector-Labels: {len(set(unmapped))}",
          file=sys.stderr)
    if unmapped:
        print(f"  Unmapped: {sorted(set(unmapped))[:8]}{'...' if len(set(unmapped)) > 8 else ''}",
              file=sys.stderr)
    print(f"Output: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
