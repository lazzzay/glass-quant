#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIM-2: Monte-Carlo-Pfad-Resampling (Welle A, Simulation-Layer V7.0)

Ziel: synthetische Equity-Kurven aus der Trade-Return-Verteilung generieren,
um die Drawdown-Verteilung und die Bandbreite plausibler Equity-Pfade sichtbar
zu machen. Ergaenzt SIM-1 (Bootstrap-CI auf Punktschaetzern) um Pfad-Perspektive.

Methodik
--------
Wir simulieren zwei unabhaengige Pfad-Ensembles:

  (a) Trade-i.i.d.-Resample
      Ziehe N gleich lange Trade-Folgen (|trades|) mit Zuruecklegen,
      komponiere Equity = Startkapital * prod(1 + pnl_pct_i).
      Reflektiert Trade-Sequenz-Unsicherheit ("hat die Strategie Glueck gehabt?").

  (b) Daily-Moving-Block-Bootstrap (5-Tage-Bloecke)
      Ziehe N Tages-Return-Pfade gleicher Laenge wie Equity-Historie,
      komponiere taegliche Equity-Kurve. Respektiert Autokorrelation,
      liefert realistische Drawdown-Profile.

Pro Pfad werden folgende Kennzahlen gespeichert:
  - Endequity (Trade-Pfad) bzw. End-CAGR (Daily-Pfad)
  - Max-Drawdown
  - Time-to-Recovery (Tage bis neues Hoch nach MaxDD, Daily-Pfad)
  - Sharpe (Daily-Pfad)
  - WR, ProfitFactor (Trade-Pfad)

Outputs
-------
  sim/_reports/monte_carlo_YYYY-MM-DD.json   Quantile + Stats
  sim/_reports/monte_carlo_YYYY-MM-DD.png    Fanchart + DD-Histogramm

Keine Paid-APIs, alle Libs pip-lokal (numpy/pandas/matplotlib).

Referenz: Validation-Simulation-Framework-2026-04-16.md §4 Welle A, SIM-2.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# matplotlib ohne GUI
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = ROOT / "backtest" / "results_v1"
EQUITY_PARQUET = BACKTEST_DIR / "equity_daily.parquet"
TRADES_PARQUET = BACKTEST_DIR / "trades.parquet"
REPORT_DIR = ROOT / "sim" / "_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

N_MC_TRADE = 10_000            # Monte-Carlo-Pfade Trade-Resample
N_MC_DAILY = 2_000             # weniger Daily-Pfade (teurer, Laenge ~7k Tage)
BLOCK_LEN = 5                   # Handelstage pro Block (analog SIM-1)
TRADING_DAYS = 252
SEED = 20260416
START_EQUITY = 100_000.0        # konsistent mit backtest

QUANTILES = [5, 25, 50, 75, 95]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _quantile_summary(arr: np.ndarray) -> dict:
    """Quantile + Kennzahlen einer Pfad-Kennzahl ueber alle MC-Draws."""
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"n": 0}
    out = {
        "n": int(arr.size),
        "mean": round(float(arr.mean()), 6),
        "median": round(float(np.median(arr)), 6),
        "std": round(float(arr.std(ddof=1)), 6),
    }
    for q in QUANTILES:
        out[f"q{q:02d}"] = round(float(np.percentile(arr, q)), 6)
    return out


def _max_dd(equity: np.ndarray) -> float:
    """Max-Drawdown (negativ, als Bruchteil)."""
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    return float(dd.min())


def _time_to_recovery(equity: np.ndarray) -> int:
    """
    Laenge der laengsten Under-Water-Periode in Kalendertagen (hier: Handelstage).
    Wenn nie ein neues Hoch erreicht, gib Pfadlaenge zurueck.
    """
    peak = np.maximum.accumulate(equity)
    under_water = equity < peak
    if not under_water.any():
        return 0
    # laengster Run von True in under_water
    max_run = cur = 0
    for flag in under_water:
        if flag:
            cur += 1
            if cur > max_run:
                max_run = cur
        else:
            cur = 0
    return int(max_run)


def _block_indices(n: int, block_len: int, rng: np.random.Generator) -> np.ndarray:
    """Moving-Block-Bootstrap-Indizes (ohne Wrap, analog SIM-1)."""
    max_start = n - block_len
    if max_start <= 0:
        return rng.integers(0, n, size=n)
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, max_start + 1, size=n_blocks)
    idx = np.concatenate([np.arange(s, s + block_len) for s in starts])
    return idx[:n]


# ----------------------------------------------------------------------------
# Monte-Carlo (a): Trade-i.i.d.-Resample
# ----------------------------------------------------------------------------
def mc_trade_paths(trade_returns: np.ndarray, n_paths: int,
                   rng: np.random.Generator) -> dict:
    """
    Trade-Level-MC: i.i.d.-Resample der Trade-Returns liefert n_paths synthetische
    Trade-Sequenzen. Wir aggregieren NICHT zu einer Equity-Kurve (das waere wegen
    paralleler Positions-Struktur im Live-System irrefuehrend), sondern messen die
    skalen-invarianten Edge-Kennzahlen pro Pfad:

      - WR, ProfitFactor, Expectancy, AvgWin, AvgLoss

    Das beantwortet die Frage: "Wie robust ist der Trade-Edge ueber plausible
    Re-Orderings der Historie?"

    Streaming in Chunks, damit kein 10k x 14k-Array materialisiert wird.
    """
    n_trades = trade_returns.size
    wr = np.empty(n_paths)
    pf = np.empty(n_paths)
    expectancy = np.empty(n_paths)
    avg_win = np.empty(n_paths)
    avg_loss = np.empty(n_paths)

    chunk = 500
    for start in range(0, n_paths, chunk):
        end = min(start + chunk, n_paths)
        k = end - start
        idx = rng.integers(0, n_trades, size=(k, n_trades))
        rets = trade_returns[idx]

        wins_mask = rets > 0.0
        wins_ct = wins_mask.sum(axis=1)
        wr[start:end] = wins_ct / n_trades

        gross_win = np.where(wins_mask, rets, 0.0).sum(axis=1)
        gross_loss = -np.where(~wins_mask, rets, 0.0).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            pf[start:end] = np.where(gross_loss > 0, gross_win / gross_loss, np.nan)

        expectancy[start:end] = rets.mean(axis=1)

        # avg_win / avg_loss pro Pfad
        rets_pos = np.where(wins_mask, rets, np.nan)
        rets_neg = np.where(~wins_mask, rets, np.nan)
        with np.errstate(invalid="ignore"):
            avg_win[start:end] = np.nanmean(rets_pos, axis=1)
            avg_loss[start:end] = np.nanmean(rets_neg, axis=1)

    return {
        "win_rate": wr,
        "profit_factor": pf,
        "expectancy": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


# ----------------------------------------------------------------------------
# Monte-Carlo (b): Daily-Moving-Block-Bootstrap
# ----------------------------------------------------------------------------
def mc_daily_paths(returns: np.ndarray, n_paths: int,
                   block_len: int, rng: np.random.Generator) -> dict:
    """Synthetisiere n_paths tagesbasierte Equity-Kurven via Block-Bootstrap."""
    n_days = returns.size
    equity = np.empty((n_paths, n_days))
    max_dd = np.empty(n_paths)
    ttr = np.empty(n_paths, dtype=int)
    sharpe = np.empty(n_paths)
    cagr = np.empty(n_paths)

    n_years = n_days / TRADING_DAYS
    for i in range(n_paths):
        idx = _block_indices(n_days, block_len, rng)
        r = returns[idx]
        eq = START_EQUITY * np.cumprod(1.0 + r)
        equity[i] = eq
        max_dd[i] = _max_dd(eq)
        ttr[i] = _time_to_recovery(eq)
        mu = r.mean()
        sd = r.std(ddof=1)
        sharpe[i] = mu / sd * np.sqrt(TRADING_DAYS) if sd > 0 else np.nan
        cagr[i] = (eq[-1] / START_EQUITY) ** (1.0 / n_years) - 1.0

    return {
        "paths": equity,
        "max_dd": max_dd,
        "time_to_recovery": ttr,
        "sharpe": sharpe,
        "cagr": cagr,
    }


# ----------------------------------------------------------------------------
# Plot
# ----------------------------------------------------------------------------
def plot_fanchart(daily_eq: np.ndarray, dates: pd.Series,
                  point_equity: np.ndarray, out_path: Path,
                  mc_daily: dict) -> None:
    """Fanchart (5/25/50/75/95-Quantile) + Daily-MC MaxDD-Histogramm."""
    q = np.percentile(daily_eq, QUANTILES, axis=0)     # (5, n_days)
    fig, axes = plt.subplots(2, 1, figsize=(10, 8),
                             gridspec_kw={"height_ratios": [2, 1]})

    ax = axes[0]
    ax.fill_between(dates, q[0], q[4], alpha=0.15, color="steelblue",
                    label="5: 95 % Band")
    ax.fill_between(dates, q[1], q[3], alpha=0.30, color="steelblue",
                    label="25: 75 % Band")
    ax.plot(dates, q[2], color="steelblue", lw=1.2, label="Median-Pfad")
    ax.plot(dates, point_equity, color="black", lw=1.2,
            label="Realisierter Pfad (V1-Backtest)")
    ax.set_yscale("log")
    ax.set_ylabel("Equity (log)")
    ax.set_title("SIM-2  Monte-Carlo-Fanchart (Daily-Block-Bootstrap)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, which="both", alpha=0.25)

    ax2 = axes[1]
    ax2.hist(mc_daily["max_dd"] * 100.0, bins=60, color="firebrick", alpha=0.7,
             edgecolor="white", label="MaxDD-Verteilung (Daily-MC)")
    ax2.axvline(np.percentile(mc_daily["max_dd"] * 100.0, 5),
                color="black", lw=1.0, ls="--", label="5 %-Quantil")
    ax2.set_xlabel("Max-Drawdown (%)")
    ax2.set_ylabel("Haeufigkeit")
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.25)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    if not EQUITY_PARQUET.exists() or not TRADES_PARQUET.exists():
        print(f"[SIM-2] FATAL: Eingabe-Parquets nicht gefunden "
              f"({EQUITY_PARQUET.name} / {TRADES_PARQUET.name}).", file=sys.stderr)
        return 2

    eq = pd.read_parquet(EQUITY_PARQUET).sort_values("date").reset_index(drop=True)
    trades = pd.read_parquet(TRADES_PARQUET)

    daily_returns = eq["equity"].pct_change().dropna().to_numpy()
    trade_returns = trades["pnl_pct"].dropna().to_numpy()

    print(f"[SIM-2] Daily-Returns: n={daily_returns.size:,d}")
    print(f"[SIM-2] Trades:        n={trade_returns.size:,d}")
    print(f"[SIM-2] MC-Trade-Paths: {N_MC_TRADE:,d}, "
          f"MC-Daily-Paths: {N_MC_DAILY:,d}, BLOCK_LEN={BLOCK_LEN}, SEED={SEED}")

    rng_t = np.random.default_rng(SEED)
    rng_d = np.random.default_rng(SEED + 1)

    t0 = datetime.now()
    mc_trade = mc_trade_paths(trade_returns, N_MC_TRADE, rng_t)
    t1 = datetime.now()
    mc_daily = mc_daily_paths(daily_returns, N_MC_DAILY, BLOCK_LEN, rng_d)
    t2 = datetime.now()
    print(f"[SIM-2] Trade-MC   : {(t1 - t0).total_seconds():.2f}s")
    print(f"[SIM-2] Daily-MC   : {(t2 - t1).total_seconds():.2f}s")

    today = datetime.now().strftime("%Y-%m-%d")

    # Realer (point) Pfad
    point_equity = eq["equity"].to_numpy()
    point_stats = {
        "final_eq": float(point_equity[-1]),
        "max_dd": _max_dd(point_equity),
        "time_to_recovery_days": _time_to_recovery(point_equity),
    }

    # JSON-Report (ohne grosse Pfad-Arrays!)
    out = {
        "schema_version": "sim.monte_carlo.v1",
        "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_date_local": today,
        "source": {
            "equity_parquet": str(EQUITY_PARQUET.relative_to(ROOT)),
            "trades_parquet": str(TRADES_PARQUET.relative_to(ROOT)),
            "n_days": int(daily_returns.size),
            "n_trades": int(trade_returns.size),
            "date_start": str(eq["date"].iloc[0].date()),
            "date_end": str(eq["date"].iloc[-1].date()),
            "start_equity": START_EQUITY,
        },
        "config": {
            "n_mc_trade": N_MC_TRADE,
            "n_mc_daily": N_MC_DAILY,
            "block_len_days": BLOCK_LEN,
            "seed": SEED,
            "quantiles": QUANTILES,
        },
        "point_realized": point_stats,
        "trade_mc": {
            "win_rate":       _quantile_summary(mc_trade["win_rate"]),
            "profit_factor":  _quantile_summary(mc_trade["profit_factor"]),
            "expectancy":     _quantile_summary(mc_trade["expectancy"]),
            "avg_win":        _quantile_summary(mc_trade["avg_win"]),
            "avg_loss":       _quantile_summary(mc_trade["avg_loss"]),
        },
        "daily_mc": {
            "cagr":              _quantile_summary(mc_daily["cagr"]),
            "sharpe":            _quantile_summary(mc_daily["sharpe"]),
            "max_dd":            _quantile_summary(mc_daily["max_dd"]),
            "time_to_recovery":  _quantile_summary(mc_daily["time_to_recovery"].astype(float)),
        },
        "gate": _gate(mc_trade, mc_daily),
    }

    out_json = REPORT_DIR / f"monte_carlo_{today}.json"
    out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[SIM-2] JSON  geschrieben: {out_json.relative_to(ROOT)}")

    # Plot: Dates & realer Equity-Pfad auf gleiche Laenge bringen wie MC-Pfade
    # (pct_change() dropnt Tag 0, MC-Pfade haben n_days-1 Elemente)
    plot_dates = eq["date"].iloc[1:].reset_index(drop=True)
    plot_realized = point_equity[1:]
    out_png = REPORT_DIR / f"monte_carlo_{today}.png"
    plot_fanchart(mc_daily["paths"], plot_dates, plot_realized, out_png, mc_daily)
    print(f"[SIM-2] Plot  geschrieben: {out_png.relative_to(ROOT)}")

    _pretty(out)
    return 0


def _gate(mc_trade: dict, mc_daily: dict) -> dict:
    """
    Ampel (Daily-MC liefert Pfad-Kennzahlen, Trade-MC liefert Edge-Kennzahlen):
      - Daily-MaxDD-5%-Quantil < -25 % -> AMBER, < -35 % -> RED
      - Daily-Sharpe-5%-Quantil  < 0.5 -> AMBER
      - Trade-WR-5%-Quantil       < 50 % -> AMBER (Edge fragil)
      - Trade-PF-5%-Quantil       < 1.0 -> RED (Edge nicht robust)
    """
    flags = []
    status = "GREEN"

    dd_q5 = np.percentile(mc_daily["max_dd"], 5)
    if dd_q5 < -0.35:
        status = "RED"
        flags.append(f"daily_max_dd_q5={dd_q5:.2%} < -35% (Tail-Verlust)")
    elif dd_q5 < -0.25:
        status = "AMBER"
        flags.append(f"daily_max_dd_q5={dd_q5:.2%} < -25% (Tail-Risk)")

    sharpe = mc_daily["sharpe"][np.isfinite(mc_daily["sharpe"])]
    if sharpe.size:
        sharpe_q5 = np.percentile(sharpe, 5)
        if sharpe_q5 < 0.5:
            if status == "GREEN":
                status = "AMBER"
            flags.append(f"daily_sharpe_q5={sharpe_q5:.2f} < 0.50 (schwache Evidenz)")

    wr_q5 = np.percentile(mc_trade["win_rate"], 5)
    if wr_q5 < 0.50:
        if status == "GREEN":
            status = "AMBER"
        flags.append(f"trade_wr_q5={wr_q5:.2%} < 50% (Edge fragil)")

    pf_clean = mc_trade["profit_factor"][np.isfinite(mc_trade["profit_factor"])]
    if pf_clean.size:
        pf_q5 = np.percentile(pf_clean, 5)
        if pf_q5 < 1.0:
            status = "RED"
            flags.append(f"trade_pf_q5={pf_q5:.2f} < 1.0 (kein Edge im Tail)")

    return {"status": status, "flags": flags}


def _pretty(out: dict) -> None:
    p = out["point_realized"]
    t = out["trade_mc"]
    d = out["daily_mc"]
    print()
    print("=" * 72)
    print(f"SIM-2  Monte-Carlo  Laufdatum {out['run_date_local']}")
    print("=" * 72)
    print(f"Fenster: {out['source']['date_start']}  ->  {out['source']['date_end']}")
    print(f"Tage:    {out['source']['n_days']:,d}    Trades: {out['source']['n_trades']:,d}")
    print()
    print("Realer Pfad:")
    print(f"  Endequity        {p['final_eq']:>12,.0f}  (Start {START_EQUITY:,.0f})")
    print(f"  MaxDD            {p['max_dd']:>12.2%}")
    print(f"  Time-to-Recover  {p['time_to_recovery_days']:>12d}  Tage")
    print()
    print("Daily-MC Endequity-Quantile (Start 100k):")
    # Re-Aggregation aus CAGR: final = start * (1 + cagr)^n_years
    n_years = out["source"]["n_days"] / TRADING_DAYS
    for q in QUANTILES:
        cagr_q = d["cagr"][f"q{q:02d}"]
        fin = START_EQUITY * (1.0 + cagr_q) ** n_years
        print(f"  q{q:02d}             {fin:>14,.0f}     (CAGR {cagr_q:>7.2%})")
    print()
    print("Daily-MC MaxDD-Quantile:")
    for q in QUANTILES:
        print(f"  q{q:02d}             {d['max_dd'][f'q{q:02d}']:>14.2%}")
    print()
    print("Daily-MC Sharpe-Quantile:")
    for q in QUANTILES:
        print(f"  q{q:02d}             {d['sharpe'][f'q{q:02d}']:>14.3f}")
    print()
    print("Daily-MC Time-to-Recovery-Quantile (Handelstage):")
    for q in QUANTILES:
        v = d['time_to_recovery'][f'q{q:02d}']
        print(f"  q{q:02d}             {v:>14.0f}")
    print()
    print("Trade-MC WR-Quantile:")
    for q in QUANTILES:
        print(f"  q{q:02d}             {t['win_rate'][f'q{q:02d}']:>14.2%}")
    print()
    print("Trade-MC ProfitFactor-Quantile:")
    for q in QUANTILES:
        print(f"  q{q:02d}             {t['profit_factor'][f'q{q:02d}']:>14.3f}")
    print()
    g = out["gate"]
    print(f"Gate: {g['status']}")
    for f in g["flags"]:
        print(f"  - {f}")


if __name__ == "__main__":
    sys.exit(main())
