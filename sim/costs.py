#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIM-3: Slippage + Spread Cost-Model (Welle A, Simulation-Layer V7.0)

Ziel: statt Pauschale 1 EUR/Trade ein assetklassen-basiertes Kosten-Modell an
den V1-Backtest anlegen und den Delta-Effekt auf WR/Sharpe/CAGR/MaxDD messen.
Liefert damit die ehrliche Netto-Performance und eine Basis fuer spaetere
Per-Trade-Size-Entscheidungen.

Kosten-Buckets (Half-Spread in Basispunkten des Preises; Round-Trip = 2x)
-------------------------------------------------------------------------
  LargeCap  (us_large, dax, eu_large, asia_adr, commodity*)    5 bps
  SmallCap  (us_mid, us_small, mdax)                          25 bps
  ETF       (etf_broad, etf_sector, etf_thematic, etf_commod) 3 bps
  Crypto    (crypto)                                          25 bps
  Hebel     (KO/OS/Faktor, nicht in V1-Universum, in Spec)  15 bps

Commodity (Futures-Proxies) werden im V1-Universum wie LargeCap behandelt;
im V2-Backtest kann man sie separieren.

Zusaetzlich (konservativ):
  Slippage_floor_abs = 0.50 EUR pro Seite (fuer winzige Tickets): ersetzt
  die bisherige 1-EUR-Pauschale.

Methodik
--------
1. Lade trades.parquet + universe.csv, joine asset_class.
2. Berechne pro Trade realistische Kosten:
     cost_per_side = max(shares * entry_price * half_bps/1e4,
                         slippage_floor_abs)
     round_trip    = cost_per_side(entry) + cost_per_side(exit)
3. Recompute pnl_net = pnl_gross - round_trip   (pnl im Parquet ist schon
   nach alter 1-EUR-Pauschale; fuer Vergleich rechnen wir "Pauschale zurueck"
   mit +2 EUR und ziehen dann neue Kosten ab).
4. Recompute WR, PF, Expectancy, CAGR (approx) per Asset-Class und gesamt.
5. Delta-Report vs. altes Kosten-Modell.

Output: sim/_reports/costs_YYYY-MM-DD.json

Keine Paid-APIs. Kein Netz-Call.
Referenz: Validation-Simulation-Framework-2026-04-16.md §4 Welle A, SIM-3.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = ROOT / "backtest" / "results_v1"
TRADES_PARQUET = BACKTEST_DIR / "trades.parquet"
EQUITY_PARQUET = BACKTEST_DIR / "equity_daily.parquet"
UNIVERSE_CSV = ROOT / "tools" / "universe.csv"
REPORT_DIR = ROOT / "sim" / "_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Half-Spread in bps (Basispunkten des Preises), pro Seite
BUCKET_HALF_BPS = {
    "large":   5.0,
    "small":  25.0,
    "etf":     3.0,
    "crypto": 25.0,
    "hebel":  15.0,   # Spec-Only, nicht im V1-Universum
}

# Asset-Class -> Bucket-Mapping
ASSET_CLASS_TO_BUCKET = {
    "us_large":       "large",
    "dax":            "large",
    "eu_large":       "large",
    "asia_adr":       "large",
    "commodity":      "large",
    "us_mid":         "small",
    "us_small":       "small",
    "mdax":           "small",
    "etf_broad":      "etf",
    "etf_sector":     "etf",
    "etf_thematic":   "etf",
    "etf_commodity":  "etf",
    "crypto":         "crypto",
}

SLIPPAGE_FLOOR_ABS = 0.50        # EUR, pro Seite, Mindestkosten
OLD_FLAT_COST_PER_TRADE = 2.0     # alte Pauschale: 1 EUR Entry + 1 EUR Exit

TRADING_DAYS = 252
START_EQUITY = 100_000.0


# ----------------------------------------------------------------------------
# Kosten-Rechnung
# ----------------------------------------------------------------------------
def _bucket_for_ticker(ticker: str, ticker_to_class: dict[str, str]) -> str:
    ac = ticker_to_class.get(ticker)
    if ac is None:
        return "large"   # konservativer Default (5 bps) statt KeyError
    return ASSET_CLASS_TO_BUCKET.get(ac, "large")


def _round_trip_cost(shares: float, entry_price: float, exit_price: float,
                     bucket: str) -> float:
    half_bps = BUCKET_HALF_BPS[bucket]
    entry_cost = max(shares * entry_price * half_bps / 1e4, SLIPPAGE_FLOOR_ABS)
    exit_cost  = max(shares * exit_price  * half_bps / 1e4, SLIPPAGE_FLOOR_ABS)
    return entry_cost + exit_cost


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
def _summary(trades: pd.DataFrame, pnl_col: str) -> dict:
    pnl = trades[pnl_col].to_numpy()
    wins = pnl > 0
    n = pnl.size
    wr = float(wins.sum() / n) if n else float("nan")
    gross_win = float(pnl[wins].sum()) if wins.any() else 0.0
    gross_loss = float(-pnl[~wins].sum()) if (~wins).any() else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_pnl = float(pnl.sum())
    avg = float(pnl.mean()) if n else float("nan")
    avg_win = float(pnl[wins].mean()) if wins.any() else float("nan")
    avg_loss = float(pnl[~wins].mean()) if (~wins).any() else float("nan")
    return {
        "n_trades": int(n),
        "win_rate": round(wr, 4),
        "profit_factor": round(pf, 3) if np.isfinite(pf) else None,
        "gross_pnl_eur": round(total_pnl, 2),
        "avg_pnl_eur": round(avg, 2),
        "avg_win_eur": round(avg_win, 2) if wins.any() else None,
        "avg_loss_eur": round(avg_loss, 2) if (~wins).any() else None,
    }


def _per_bucket_summary(trades: pd.DataFrame, pnl_col: str) -> dict:
    out = {}
    for bucket, sub in trades.groupby("cost_bucket"):
        out[bucket] = _summary(sub, pnl_col)
    return out


# ----------------------------------------------------------------------------
# CAGR- und MaxDD-Approximation ueber Rebuild-Equity
# ----------------------------------------------------------------------------
def _rebuild_equity(trades: pd.DataFrame, pnl_col: str,
                    equity_dates: pd.Series) -> np.ndarray:
    """
    Bauen der Equity-Kurve aus Trades neu: taegliche Equity = START +
    kumulierte PnL aller Trades mit exit_date <= t. Ignoriert MTM-Effekte
    offener Positionen (approximativ). Gut genug fuer Delta-Vergleich.
    """
    t = trades[["exit_date", pnl_col]].dropna().copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"]).dt.normalize()
    daily_pnl = t.groupby("exit_date")[pnl_col].sum()

    eq = pd.Series(0.0, index=pd.to_datetime(equity_dates).dt.normalize().values)
    eq = eq.add(daily_pnl, fill_value=0.0)
    eq = eq.cumsum() + START_EQUITY
    return eq.to_numpy()


def _path_metrics(equity: np.ndarray, n_days: int) -> dict:
    if equity.size < TRADING_DAYS:
        return {"cagr": None, "max_dd": None, "calmar": None}
    returns = np.diff(equity) / equity[:-1]
    returns = returns[np.isfinite(returns)]

    n_years = equity.size / TRADING_DAYS
    cagr = (equity[-1] / equity[0]) ** (1.0 / n_years) - 1.0 if equity[0] > 0 else None

    peak = np.maximum.accumulate(equity)
    dd = (equity / peak - 1.0)
    max_dd = float(dd.min())

    calmar = (cagr / abs(max_dd)) if (cagr is not None and max_dd < 0) else None
    return {
        "cagr": round(float(cagr), 6) if cagr is not None else None,
        "max_dd": round(max_dd, 6),
        "calmar": round(float(calmar), 3) if calmar is not None else None,
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    for p in (TRADES_PARQUET, EQUITY_PARQUET, UNIVERSE_CSV):
        if not p.exists():
            print(f"[SIM-3] FATAL: {p} fehlt.", file=sys.stderr)
            return 2

    trades = pd.read_parquet(TRADES_PARQUET)
    eq = pd.read_parquet(EQUITY_PARQUET).sort_values("date").reset_index(drop=True)
    uni = pd.read_csv(UNIVERSE_CSV)

    ticker_to_class = dict(zip(uni["ticker"], uni["asset_class"]))

    trades["asset_class"] = trades["ticker"].map(ticker_to_class).fillna("unknown")
    trades["cost_bucket"] = trades["ticker"].apply(
        lambda tk: _bucket_for_ticker(tk, ticker_to_class))

    # Neue Kosten berechnen
    trades["new_round_trip_cost_eur"] = [
        _round_trip_cost(s, e, x, b)
        for s, e, x, b in zip(trades["shares"], trades["entry_price"],
                              trades["exit_price"], trades["cost_bucket"])
    ]

    # pnl_gross = pnl_old + 2 EUR (alte Pauschale zurueck)
    trades["pnl_gross_eur"] = trades["pnl"] + OLD_FLAT_COST_PER_TRADE
    # pnl_new = pnl_gross - new_round_trip
    trades["pnl_new_eur"] = trades["pnl_gross_eur"] - trades["new_round_trip_cost_eur"]
    # pnl_old (fuer Baseline-Vergleich) = trades["pnl"]

    print(f"[SIM-3] Trades        : {len(trades):,d}")
    print(f"[SIM-3] Unique tickers: {trades['ticker'].nunique()}")
    print(f"[SIM-3] Bucket-Counts :")
    for b, c in trades["cost_bucket"].value_counts().items():
        print(f"        {b:<8s} {c:>6,d}")

    # Aggregate
    summary_gross = _summary(trades, "pnl_gross_eur")
    summary_old   = _summary(trades, "pnl")
    summary_new   = _summary(trades, "pnl_new_eur")

    by_bucket_new = _per_bucket_summary(trades, "pnl_new_eur")
    by_bucket_old = _per_bucket_summary(trades, "pnl")

    # Pfad-Rebuild (approximativ) + Metrik
    eq_old = _rebuild_equity(trades, "pnl", eq["date"])
    eq_new = _rebuild_equity(trades, "pnl_new_eur", eq["date"])
    eq_gross = _rebuild_equity(trades, "pnl_gross_eur", eq["date"])
    path_old   = _path_metrics(eq_old,   len(eq))
    path_new   = _path_metrics(eq_new,   len(eq))
    path_gross = _path_metrics(eq_gross, len(eq))

    # Cost-Aggregate
    total_cost_old = len(trades) * OLD_FLAT_COST_PER_TRADE
    total_cost_new = float(trades["new_round_trip_cost_eur"].sum())
    avg_cost_new   = total_cost_new / len(trades)
    delta_cost_eur = total_cost_new - total_cost_old

    today = datetime.now().strftime("%Y-%m-%d")
    out = {
        "schema_version": "sim.costs.v1",
        "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_date_local": today,
        "source": {
            "trades_parquet": str(TRADES_PARQUET.relative_to(ROOT)),
            "universe_csv":   str(UNIVERSE_CSV.relative_to(ROOT)),
            "n_trades": int(len(trades)),
            "date_start": str(trades["entry_date"].min().date()),
            "date_end":   str(trades["exit_date"].max().date()),
        },
        "config": {
            "bucket_half_bps":         BUCKET_HALF_BPS,
            "slippage_floor_abs_eur":  SLIPPAGE_FLOOR_ABS,
            "old_flat_cost_per_trade": OLD_FLAT_COST_PER_TRADE,
        },
        "cost_aggregate": {
            "total_cost_old_eur":  round(total_cost_old, 2),
            "total_cost_new_eur":  round(total_cost_new, 2),
            "avg_cost_new_eur":    round(avg_cost_new, 3),
            "delta_cost_eur":      round(delta_cost_eur, 2),
            "delta_cost_pct_of_gross_pnl":
                round(delta_cost_eur / summary_gross["gross_pnl_eur"] * 100.0, 3)
                if summary_gross["gross_pnl_eur"] else None,
        },
        "trade_summary": {
            "gross_no_cost": summary_gross,
            "old_flat_cost": summary_old,
            "new_bps_cost":  summary_new,
        },
        "path_metrics": {
            "gross_no_cost": path_gross,
            "old_flat_cost": path_old,
            "new_bps_cost":  path_new,
        },
        "bucket_breakdown_new": by_bucket_new,
        "bucket_breakdown_old": by_bucket_old,
        "delta_new_vs_old": {
            "win_rate":       round(summary_new["win_rate"] - summary_old["win_rate"], 4),
            "profit_factor":  (round(summary_new["profit_factor"] - summary_old["profit_factor"], 3)
                               if summary_new["profit_factor"] is not None
                               and summary_old["profit_factor"] is not None else None),
            "gross_pnl_eur":  round(summary_new["gross_pnl_eur"] - summary_old["gross_pnl_eur"], 2),
            "avg_pnl_eur":    round(summary_new["avg_pnl_eur"] - summary_old["avg_pnl_eur"], 3),
            "cagr":           (round(path_new["cagr"] - path_old["cagr"], 6)
                               if path_new["cagr"] is not None and path_old["cagr"] is not None else None),
            "max_dd":         (round(path_new["max_dd"] - path_old["max_dd"], 6)
                               if path_new["max_dd"] is not None and path_old["max_dd"] is not None else None),
        },
        "gate": _gate(summary_old, summary_new, path_old, path_new),
    }

    out_path = REPORT_DIR / f"costs_{today}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[SIM-3] Report geschrieben: {out_path.relative_to(ROOT)}")

    _pretty(out)
    return 0


def _gate(summary_old: dict, summary_new: dict,
          path_old: dict, path_new: dict) -> dict:
    """
    Ampel:
      - WR-Delta < -1.5 pp          -> AMBER (Spread frisst Edge)
      - PF_new < 1.25               -> AMBER (Edge nach Kosten schwach)
      - PF_new < 1.10               -> RED   (Edge nach Kosten marginal)
      - CAGR-Delta < -0.5 pp        -> AMBER (Kosten kosten 50 bps/Jahr)
    """
    flags = []
    status = "GREEN"

    wr_delta = summary_new["win_rate"] - summary_old["win_rate"]
    if wr_delta < -0.015:
        status = "AMBER"
        flags.append(f"wr_delta={wr_delta:.2%} < -1.5pp (Spread frisst Edge)")

    pf_new = summary_new["profit_factor"]
    if pf_new is not None:
        if pf_new < 1.10:
            status = "RED"
            flags.append(f"pf_new={pf_new:.2f} < 1.10 (Edge marginal)")
        elif pf_new < 1.25:
            if status == "GREEN":
                status = "AMBER"
            flags.append(f"pf_new={pf_new:.2f} < 1.25 (Edge nach Kosten schwach)")

    if path_new["cagr"] is not None and path_old["cagr"] is not None:
        cagr_delta = path_new["cagr"] - path_old["cagr"]
        if cagr_delta < -0.005:
            if status == "GREEN":
                status = "AMBER"
            flags.append(f"cagr_delta={cagr_delta:.3%} < -0.5pp/Jahr")

    return {"status": status, "flags": flags}


def _pretty(out: dict) -> None:
    s_g = out["trade_summary"]["gross_no_cost"]
    s_o = out["trade_summary"]["old_flat_cost"]
    s_n = out["trade_summary"]["new_bps_cost"]
    p_o = out["path_metrics"]["old_flat_cost"]
    p_n = out["path_metrics"]["new_bps_cost"]
    ca  = out["cost_aggregate"]
    d   = out["delta_new_vs_old"]

    print()
    print("=" * 80)
    print(f"SIM-3  Slippage/Spread Cost-Model  Laufdatum {out['run_date_local']}")
    print("=" * 80)
    print(f"Fenster: {out['source']['date_start']}  ->  {out['source']['date_end']}")
    print(f"Trades:  {out['source']['n_trades']:,d}")
    print()
    print("Kosten-Aggregate:")
    print(f"  Alt (1 EUR/Trade Flat):  {ca['total_cost_old_eur']:>14,.2f} EUR gesamt")
    print(f"  Neu (bps + 0.50 EUR Floor): {ca['total_cost_new_eur']:>11,.2f} EUR gesamt")
    print(f"  Avg pro Trade neu:        {ca['avg_cost_new_eur']:>12.3f} EUR")
    print(f"  Delta:                    {ca['delta_cost_eur']:>+14,.2f} EUR "
          f"({ca['delta_cost_pct_of_gross_pnl']:+.2f} % vom Brutto-PnL)")
    print()
    print(f"{'Metrik':<18} {'Brutto':>14} {'Alt (Flat)':>14} {'Neu (bps)':>14} {'Delta':>12}")
    print("-" * 74)
    print(f"{'WR':<18} {s_g['win_rate']:>14.2%} {s_o['win_rate']:>14.2%} {s_n['win_rate']:>14.2%} {d['win_rate']:>+12.2%}")
    pf_g = s_g['profit_factor'] if s_g['profit_factor'] is not None else float('nan')
    pf_o = s_o['profit_factor'] if s_o['profit_factor'] is not None else float('nan')
    pf_n = s_n['profit_factor'] if s_n['profit_factor'] is not None else float('nan')
    print(f"{'PF':<18} {pf_g:>14.3f} {pf_o:>14.3f} {pf_n:>14.3f} {d['profit_factor']:>+12.3f}")
    print(f"{'Gross-PnL EUR':<18} {s_g['gross_pnl_eur']:>14,.0f} {s_o['gross_pnl_eur']:>14,.0f} {s_n['gross_pnl_eur']:>14,.0f} {d['gross_pnl_eur']:>+12,.0f}")
    print(f"{'Avg-PnL EUR':<18} {s_g['avg_pnl_eur']:>14.2f} {s_o['avg_pnl_eur']:>14.2f} {s_n['avg_pnl_eur']:>14.2f} {d['avg_pnl_eur']:>+12.2f}")
    print()
    print(f"{'Pfad-Metrik':<18} {'Alt (Flat)':>14} {'Neu (bps)':>14} {'Delta':>12}")
    print("-" * 60)
    if p_o.get("cagr") is not None:
        print(f"{'CAGR':<18} {p_o['cagr']:>14.2%} {p_n['cagr']:>14.2%} {d['cagr']:>+12.3%}")
    if p_o.get("max_dd") is not None:
        print(f"{'MaxDD':<18} {p_o['max_dd']:>14.2%} {p_n['max_dd']:>14.2%} {d['max_dd']:>+12.3%}")
    print()
    print("Per-Bucket (Neu):")
    for b, s in out["bucket_breakdown_new"].items():
        pf = s['profit_factor'] if s['profit_factor'] is not None else float('nan')
        print(f"  {b:<8s}  n={s['n_trades']:>5d}  WR={s['win_rate']:>6.2%}  "
              f"PF={pf:>6.3f}  AvgPnL={s['avg_pnl_eur']:>+7.2f} EUR")
    print()
    g = out["gate"]
    print(f"Gate: {g['status']}")
    for f in g["flags"]:
        print(f"  - {f}")


if __name__ == "__main__":
    sys.exit(main())
