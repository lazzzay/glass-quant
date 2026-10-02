#!/usr/bin/env python3
"""
COVERAGE-RESILIENCE-1 / DATA-ARCH-3 (2026-05-04):
Multi-Source-Orchestrator für Preisdaten.
Kaskade: yfinance → Stooq (via pandas-datareader) → leer.

Zweck:
- yfinance-Disk-I/O-Errors (FUSE, Cache-Korruption) verhindern dass Scanner durchläuft
- Stooq als Backup, free, weniger Rate-Limited als yfinance
- Symbol-Mapping zwischen Quellen (yfinance ABBV → Stooq ABBV.US)

Usage:
    from tools.data_source_orchestrator import fetch_with_fallback
    df = fetch_with_fallback("LMT", period_days=450)
    # Versucht yfinance erst, dann Stooq

CLI für Smoke-Test (Netzwerk, nicht in CI):
    python3 tools/data_source_orchestrator.py --selftest
    python3 tools/data_source_orchestrator.py --selftest LMT,SAP.DE,AAPL
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]


def _yf_to_stooq_symbol(yf_ticker: str) -> Optional[str]:
    """Mappe yfinance-Symbol auf Stooq-Format.
    Stooq-Konventionen:
    - US-Aktien: <SYMBOL>.US (z.B. LMT.US, AAPL.US)
    - DE-Aktien: <SYMBOL>.DE (gleich wie yfinance)
    - PA, L, MI, MC, AS: gleich
    - SW (Schweiz): meist <SYMBOL>.SW (gleich)
    - ST (Stockholm): gleich
    - Crypto: <SYMBOL>USD (z.B. BTCUSD statt BTC-USD)
    - Commodities: oft nicht verfügbar (CL=F, GC=F)
    - ETF/Index ohne Suffix: .US
    """
    t = yf_ticker.upper()
    if t.endswith("=F"):
        # Commodities: Stooq nicht zuverlässig
        return None
    if "-USD" in t:
        # Crypto: BTC-USD → BTCUSD
        return t.replace("-USD", "USD")
    if "." in t:
        # Schon mit Region-Suffix → meist gleich verwendbar (DE, PA, L, MI, SW, ST, MC, AS, CO)
        # Stooq hat dieselben Suffixe wie yfinance für viele Märkte
        return t
    # US-Default: kein Suffix → .US anhängen
    return f"{t}.US"


def _fetch_yfinance(ticker: str, start_date, end_date) -> Optional[pd.DataFrame]:
    """Versuche yfinance. Liefert None bei Failure."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.history(
            start=start_date,
            end=end_date,
            auto_adjust=True,
            timeout=20,
        )
        if df is not None and not df.empty and len(df) > 50:
            return df
    except Exception:
        pass
    return None


def _fetch_stooq(ticker: str, start_date, end_date) -> Optional[pd.DataFrame]:
    """Versuche Stooq via pandas-datareader. Liefert None bei Failure.

    Stooq-Spalten kommen als: Open, High, Low, Close, Volume
    (yfinance hat auch Open, High, Low, Close, Volume: kompatibel)
    """
    stooq_sym = _yf_to_stooq_symbol(ticker)
    if not stooq_sym:
        return None
    try:
        import pandas_datareader.data as pdr
        df = pdr.DataReader(stooq_sym, "stooq", start_date, end_date)
        if df is not None and not df.empty and len(df) > 50:
            # Stooq liefert sortiert descending: wir wollen ascending
            df = df.sort_index(ascending=True)
            return df
    except Exception:
        pass
    return None


def fetch_with_fallback(
    ticker: str,
    period_days: int = 450,
    prefer: str = "yfinance",
) -> tuple[Optional[pd.DataFrame], str]:
    """Hauptfunktion. Liefert (DataFrame, source_used).
    source_used in {'yfinance', 'stooq', 'none'}.
    """
    end_date = datetime.now(timezone.utc).replace(tzinfo=None).date() + timedelta(days=1)
    start_date = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=period_days)).date()

    sources = ["yfinance", "stooq"] if prefer == "yfinance" else ["stooq", "yfinance"]

    for src in sources:
        if src == "yfinance":
            df = _fetch_yfinance(ticker, start_date, end_date)
        else:
            df = _fetch_stooq(ticker, start_date, end_date)
        if df is not None:
            return df, src

    return None, "none"


def main():
    """Smoke-Test CLI."""
    import argparse
    ap = argparse.ArgumentParser()
    _default_tickers = "LMT,SAP.DE,AAPL,NVDA,HNR1.DE"
    ap.add_argument("--selftest", nargs="?", const=_default_tickers, default=_default_tickers,
                    help="Network smoke test (yfinance, Stooq). Optional comma-separated "
                         f"tickers, default {_default_tickers}")
    ap.add_argument("--prefer", default="yfinance",
                    choices=["yfinance", "stooq"])
    args = ap.parse_args()

    tickers = [t.strip() for t in args.selftest.split(",") if t.strip()]
    print(f"\n=== DATA-SOURCE-ORCHESTRATOR Smoke-Test ===")
    print(f"Tickers: {tickers}")
    print(f"Prefer: {args.prefer}")
    print()

    yf_count = 0
    stooq_count = 0
    none_count = 0

    for t in tickers:
        df, src = fetch_with_fallback(t, period_days=90, prefer=args.prefer)
        if src == "yfinance":
            yf_count += 1
        elif src == "stooq":
            stooq_count += 1
        else:
            none_count += 1
        if df is not None:
            print(f"  {t:<10} → {src:<10} {len(df):>4} rows, last close={df['Close'].iloc[-1]:.2f}")
        else:
            print(f"  {t:<10} → NONE (yfinance + Stooq beide failed)")

    print()
    print(f"Summary: yfinance={yf_count}, stooq={stooq_count}, none={none_count}")
    print(f"Hit-Rate: {(yf_count+stooq_count)/len(tickers)*100:.0f}%")


if __name__ == "__main__":
    main()
