#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIM-1: Block-Bootstrap-Konfidenzintervalle (Welle A, Simulation-Layer V7.0)

Liefert 95%-Konfidenzintervalle fuer alle Headline-Kennzahlen des Strategie-
Backtests. Unterschied zur Phase-B-Version (backtest/bootstrap_and_regime.py):

  1. Block-Bootstrap (5-Tages-Bloecke) statt i.i.d.-Resample
     -> respektiert Autokorrelation, besonders wichtig fuer path-dependent
        Kennzahlen (MaxDD, Calmar).
  2. Zwei parallele Bootstrap-Verfahren:
       (a) Daily-Returns-Block-Bootstrap (Sharpe/Sortino/CAGR/MaxDD/Calmar)
       (b) Trade-Level-i.i.d.-Bootstrap       (WR/ProfitFactor/AvgR/Expectancy)
  3. N = 10.000 Resamples (statt 1.000) -> engere CIs, runtime <15s lokal.
  4. Output: sim/_reports/bootstrap_ci_YYYY-MM-DD.json (fuer Dashboard-Karte
     SIM-CI und Gate im Data-Health-Report).

Datenquellen (aktuell, bis Gold-Views in V7.1 existieren):
  - backtest/results_v1/equity_daily.parquet  (Tagesequity)
  - backtest/results_v1/trades.parquet        (Trade-Liste)

Keine Paid-APIs, alle Libs pip-lokal (numpy/pandas/scipy optional).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = ROOT / "backtest" / "results_v1"
EQUITY_PARQUET = BACKTEST_DIR / "equity_daily.parquet"
TRADES_PARQUET = BACKTEST_DIR / "trades.parquet"
REPORT_DIR = ROOT / "sim" / "_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

N_BOOT = 10_000
BLOCK_LEN = 5                 # Handelstage pro Bootstrap-Block
TRADING_DAYS = 252
SEED = 20260416               # deterministisch fuer Reproduzierbarkeit
RNG = np.random.default_rng(SEED)

CI_LO_PCT = 2.5
CI_HI_PCT = 97.5


# ----------------------------------------------------------------------------
# Kennzahlen-Berechnung
# ----------------------------------------------------------------------------
def _path_metrics(returns: np.ndarray) -> dict:
    """Pfad-basierte Kennzahlen auf einer Tages-Return-Serie."""
    if returns.size < TRADING_DAYS:
        return {"cagr": np.nan, "sharpe": np.nan, "sortino": np.nan,
                "max_dd": np.nan, "calmar": np.nan}

    eq = np.cumprod(1.0 + returns)
    n_years = returns.size / TRADING_DAYS
    cagr = eq[-1] ** (1.0 / n_years) - 1.0

    mu = returns.mean()
    sd = returns.std(ddof=1)
    sharpe = mu / sd * np.sqrt(TRADING_DAYS) if sd > 0 else np.nan

    downside = returns[returns < 0.0]
    dd_std = downside.std(ddof=1) if downside.size > 1 else np.nan
    sortino = mu / dd_std * np.sqrt(TRADING_DAYS) if dd_std and dd_std > 0 else np.nan

    roll_max = np.maximum.accumulate(eq)
    drawdown = eq / roll_max - 1.0
    max_dd = drawdown.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan

    return {"cagr": cagr, "sharpe": sharpe, "sortino": sortino,
            "max_dd": max_dd, "calmar": calmar}


def _trade_metrics(trade_returns_pct: np.ndarray) -> dict:
    """Trade-Level-Kennzahlen auf prozentualen Trade-Returns."""
    if trade_returns_pct.size == 0:
        return {"win_rate": np.nan, "profit_factor": np.nan,
                "avg_win_pct": np.nan, "avg_loss_pct": np.nan,
                "expectancy_pct": np.nan}
    wins = trade_returns_pct[trade_returns_pct > 0]
    losses = trade_returns_pct[trade_returns_pct <= 0]
    wr = wins.size / trade_returns_pct.size
    gross_win = wins.sum() if wins.size else 0.0
    gross_loss = -losses.sum() if losses.size else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else np.inf
    avg_win = wins.mean() if wins.size else np.nan
    avg_loss = losses.mean() if losses.size else np.nan
    expectancy = trade_returns_pct.mean()
    return {"win_rate": wr, "profit_factor": pf,
            "avg_win_pct": float(avg_win) if wins.size else np.nan,
            "avg_loss_pct": float(avg_loss) if losses.size else np.nan,
            "expectancy_pct": float(expectancy)}


# ----------------------------------------------------------------------------
# Block-Bootstrap (moving-block) auf Tages-Returns
# ----------------------------------------------------------------------------
def _block_indices(n: int, block_len: int, rng: np.random.Generator) -> np.ndarray:
    """
    Zieht Bloecke der Laenge block_len, bis gesamte Laenge n erreicht;
    Startpunkt = uniform aus [0, n-block_len]. Wrap-around wird bewusst
    vermieden (sonst ueberschaetzt man CI-Breite an Raendern).
    """
    max_start = n - block_len
    if max_start <= 0:
        return rng.integers(0, n, size=n)
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, max_start + 1, size=n_blocks)
    idx = np.concatenate([np.arange(s, s + block_len) for s in starts])
    return idx[:n]


def block_bootstrap_daily(returns: np.ndarray, n_boot: int = N_BOOT,
                          block_len: int = BLOCK_LEN,
                          rng: np.random.Generator | None = None) -> dict:
    if rng is None:
        rng = np.random.default_rng(SEED)
    buckets: dict[str, list[float]] = {k: [] for k in
                                       ("cagr", "sharpe", "sortino", "max_dd", "calmar")}
    n = returns.size
    for _ in range(n_boot):
        idx = _block_indices(n, block_len, rng)
        m = _path_metrics(returns[idx])
        for k, v in m.items():
            if not np.isnan(v) and not np.isinf(v):
                buckets[k].append(float(v))
    return {k: _quantiles(v) for k, v in buckets.items()}


def trade_bootstrap(trade_returns_pct: np.ndarray, n_boot: int = N_BOOT,
                    rng: np.random.Generator | None = None) -> dict:
    if rng is None:
        rng = np.random.default_rng(SEED + 1)
    buckets: dict[str, list[float]] = {k: [] for k in
                                       ("win_rate", "profit_factor",
                                        "avg_win_pct", "avg_loss_pct",
                                        "expectancy_pct")}
    n = trade_returns_pct.size
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        m = _trade_metrics(trade_returns_pct[idx])
        for k, v in m.items():
            if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
                continue
            buckets[k].append(float(v))
    return {k: _quantiles(v) for k, v in buckets.items()}


def _quantiles(vals: list[float]) -> dict:
    if not vals:
        return {"n_valid": 0, "mean": None, "median": None,
                "ci95_lo": None, "ci95_hi": None}
    arr = np.asarray(vals, dtype=float)
    return {
        "n_valid": int(arr.size),
        "mean":    round(float(arr.mean()), 6),
        "median":  round(float(np.median(arr)), 6),
        "std":     round(float(arr.std(ddof=1)), 6),
        "ci95_lo": round(float(np.percentile(arr, CI_LO_PCT)), 6),
        "ci95_hi": round(float(np.percentile(arr, CI_HI_PCT)), 6),
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    if not EQUITY_PARQUET.exists() or not TRADES_PARQUET.exists():
        print(f"[SIM-1] FATAL: Eingabe-Parquets nicht gefunden "
              f"({EQUITY_PARQUET.name} / {TRADES_PARQUET.name}).", file=sys.stderr)
        return 2

    eq = pd.read_parquet(EQUITY_PARQUET)
    trades = pd.read_parquet(TRADES_PARQUET)

    # Tagesrenditen aus Equity-Kurve
    eq = eq.sort_values("date").reset_index(drop=True)
    returns = eq["equity"].pct_change().dropna().to_numpy()
    point_path = _path_metrics(returns)

    # Trade-Returns in Prozent (pnl_pct ist bereits Dezimal, z. B. 0.03793 = 3,79 %)
    trade_ret = trades["pnl_pct"].dropna().to_numpy()
    point_trade = _trade_metrics(trade_ret)

    print(f"[SIM-1] Daily-Returns: n={returns.size:,d}")
    print(f"[SIM-1] Trades:        n={trade_ret.size:,d}")
    print(f"[SIM-1] N_BOOT={N_BOOT:,d}, BLOCK_LEN={BLOCK_LEN}, SEED={SEED}")

    t0 = datetime.now()
    ci_path = block_bootstrap_daily(returns)
    t1 = datetime.now()
    ci_trade = trade_bootstrap(trade_ret)
    t2 = datetime.now()
    print(f"[SIM-1] Block-Bootstrap daily:  {(t1 - t0).total_seconds():.2f}s")
    print(f"[SIM-1] Trade-Bootstrap i.i.d.: {(t2 - t1).total_seconds():.2f}s")

    today = datetime.now().strftime("%Y-%m-%d")
    out = {
        "schema_version": "sim.bootstrap.v1",
        "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_date_local": today,
        "source": {
            "equity_parquet": str(EQUITY_PARQUET.relative_to(ROOT)),
            "trades_parquet": str(TRADES_PARQUET.relative_to(ROOT)),
            "n_days":    int(returns.size),
            "n_trades":  int(trade_ret.size),
            "date_start": str(eq["date"].iloc[0].date()) if hasattr(eq["date"].iloc[0], "date") else str(eq["date"].iloc[0]),
            "date_end":   str(eq["date"].iloc[-1].date()) if hasattr(eq["date"].iloc[-1], "date") else str(eq["date"].iloc[-1]),
        },
        "config": {
            "n_bootstrap":    N_BOOT,
            "block_len_days": BLOCK_LEN,
            "seed":           SEED,
            "ci_lo_pct":      CI_LO_PCT,
            "ci_hi_pct":      CI_HI_PCT,
        },
        "point_estimates": {
            "path":  {k: (round(v, 6) if isinstance(v, float) and not np.isnan(v) else None)
                      for k, v in point_path.items()},
            "trade": {k: (round(v, 6) if isinstance(v, float) and not np.isnan(v) else None)
                      for k, v in point_trade.items()},
        },
        "bootstrap_ci": {
            "path_block_bootstrap":  ci_path,
            "trade_iid_bootstrap":   ci_trade,
        },
        "gate": _gate(ci_path, ci_trade),
    }

    out_path = REPORT_DIR / f"bootstrap_ci_{today}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[SIM-1] Report geschrieben: {out_path.relative_to(ROOT)}")

    # Kurzer Screen-Print
    _pretty(out)
    return 0


def _gate(ci_path: dict, ci_trade: dict) -> dict:
    """Ampel-Logik fuer Data-Health: triggert bei negativer Sharpe-Untergrenze,
    MaxDD-Untergrenze jenseits -25 % oder WR-Untergrenze unter 50 %."""
    flags = []
    sharpe_lo = ci_path.get("sharpe", {}).get("ci95_lo")
    if sharpe_lo is not None and sharpe_lo < 0.5:
        flags.append(f"sharpe_ci95_lo={sharpe_lo:.2f} < 0.50 (schwache Evidenz)")
    dd_lo = ci_path.get("max_dd", {}).get("ci95_lo")
    if dd_lo is not None and dd_lo < -0.25:
        flags.append(f"max_dd_ci95_lo={dd_lo:.2%} < -25% (Tail-Risk-Warn)")
    wr_lo = ci_trade.get("win_rate", {}).get("ci95_lo")
    if wr_lo is not None and wr_lo < 0.50:
        flags.append(f"win_rate_ci95_lo={wr_lo:.2%} < 50% (edge fragil)")
    return {"status": "GREEN" if not flags else "AMBER", "flags": flags}


def _pretty(out: dict) -> None:
    p = out["point_estimates"]["path"]
    t = out["point_estimates"]["trade"]
    cp = out["bootstrap_ci"]["path_block_bootstrap"]
    ct = out["bootstrap_ci"]["trade_iid_bootstrap"]
    print()
    print("=" * 72)
    print(f"SIM-1  Bootstrap-CI   Laufdatum {out['run_date_local']}")
    print("=" * 72)
    print(f"Fenster: {out['source']['date_start']}  ->  {out['source']['date_end']}")
    print(f"Tage:    {out['source']['n_days']:,d}    Trades: {out['source']['n_trades']:,d}")
    print()
    print(f"{'Metrik':<14} {'Point':>10}   {'CI95 lo':>10}   {'CI95 hi':>10}")
    print("-" * 50)
    for k in ("cagr", "sharpe", "sortino", "max_dd", "calmar"):
        pv, lo, hi = p.get(k), cp[k]["ci95_lo"], cp[k]["ci95_hi"]
        pct = k in ("cagr", "max_dd")
        fmt = (lambda x: f"{x:>10.2%}") if pct else (lambda x: f"{x:>10.3f}")
        print(f"{k:<14} {fmt(pv)}   {fmt(lo)}   {fmt(hi)}")
    print()
    for k in ("win_rate", "profit_factor", "expectancy_pct"):
        pv, lo, hi = t.get(k), ct[k]["ci95_lo"], ct[k]["ci95_hi"]
        pct = k in ("win_rate", "expectancy_pct")
        fmt = (lambda x: f"{x:>10.2%}") if pct else (lambda x: f"{x:>10.3f}")
        print(f"{k:<14} {fmt(pv)}   {fmt(lo)}   {fmt(hi)}")
    print()
    g = out["gate"]
    print(f"Gate: {g['status']}")
    for f in g["flags"]:
        print(f"  - {f}")


if __name__ == "__main__":
    sys.exit(main())
