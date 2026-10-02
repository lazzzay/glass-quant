#!/usr/bin/env python3
"""
Weg 3: Trend-Following-Modul (komplementaer zur Mean-Reversion-Baseline)

Logik: Fange Bull-Market-Durchmaersche, die die Baseline verpasst.
  Entry LONG:
    1. Close > SMA200 (absolut Trend)
    2. Close > SMA50 (mittelfristig aufwaerts)
    3. 12M-Momentum > +5% (Relative Staerke)
    4. Nicht ueber-extended: Close < 1.15 * SMA50 (nicht am Top kaufen)
  Exit LONG:
    A. Close < SMA100 (Trend-Bruch)
    B. Absolute Loss > 15%
    C. Keine Zeit-Stop: Trends laufen

Position-Sizing:
  - 5 Positionen gleichgewichtet (20% Account je Position)
  - Ranking: hoechstes 12M-Momentum zuerst

Output: backtest/results_trend/
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_trend"
OUT.mkdir(parents=True, exist_ok=True)

SMA_LONG = 200
SMA_MID = 50
SMA_EXIT = 100
MOM_N = 252  # 12M
MOM_THRESHOLD = 0.05
MAX_EXTENSION = 1.15  # Close < 1.15 * SMA50
STOP_LOSS = 0.15
N_POSITIONS = 5
STARTING_CAPITAL = 100_000.0
COMMISSION = 1.0
INCLUDE_GROUPS = {"stocks_us_large", "sector_etfs", "broad_etfs"}
START_OOS = "1997-01-01"


def load_universe():
    m = json.loads((LAKE / "manifest.json").read_text())
    uni = {}
    for s in m["series"]:
        if s.get("source") == "yfinance" and s.get("group") in INCLUDE_GROUPS:
            p = LAKE / s["path"]
            if not p.exists(): continue
            df = pd.read_parquet(p)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            df["sma200"] = df["adj_close"].rolling(SMA_LONG).mean()
            df["sma50"] = df["adj_close"].rolling(SMA_MID).mean()
            df["sma100"] = df["adj_close"].rolling(SMA_EXIT).mean()
            df["mom12m"] = df["adj_close"] / df["adj_close"].shift(MOM_N) - 1
            uni[s["id"]] = df
    return uni


def generate_signals(df: pd.DataFrame) -> pd.Series:
    cond = (
        (df["adj_close"] > df["sma200"])
        & (df["adj_close"] > df["sma50"])
        & (df["mom12m"] > MOM_THRESHOLD)
        & (df["adj_close"] < MAX_EXTENSION * df["sma50"])
    )
    return cond.fillna(False)


def run_trend_backtest(uni):
    all_dates = sorted(set().union(*[df.index for df in uni.values()]))
    all_dates = [d for d in all_dates if d >= pd.Timestamp(START_OOS)]

    signals = {tk: generate_signals(df) for tk, df in uni.items()}

    cash = STARTING_CAPITAL
    positions = {}
    equity_log = []
    trades_log = []

    for i, d in enumerate(all_dates):
        # Exits
        to_close = []
        for tk, pos in positions.items():
            df = uni[tk]
            if d not in df.index: continue
            row = df.loc[d]
            pos["days_held"] += 1
            exit_reason = None; exit_price = None
            loss_pct = row["adj_close"] / pos["entry_price"] - 1
            if row["adj_close"] < row["sma100"]:
                exit_price = row["adj_close"]; exit_reason = "SMA100_BREAK"
            elif loss_pct < -STOP_LOSS:
                exit_price = row["adj_close"]; exit_reason = "STOP_LOSS"
            if exit_reason:
                pnl = (exit_price - pos["entry_price"]) * pos["shares"] - 2*COMMISSION
                cash += exit_price * pos["shares"] - COMMISSION
                trades_log.append({"ticker": tk, "entry_date": pos["entry_date"],
                                   "entry_price": pos["entry_price"], "exit_date": d,
                                   "exit_price": exit_price, "shares": pos["shares"],
                                   "pnl": pnl, "pnl_pct": loss_pct,
                                   "days_held": pos["days_held"], "reason": exit_reason})
                to_close.append(tk)
        for tk in to_close: del positions[tk]

        # Entries - nur woechentlich bewerten (Montag) fuer Trend-Logik
        if d.weekday() == 0 and len(positions) < N_POSITIONS:
            candidates = []
            for tk, sig in signals.items():
                if tk in positions: continue
                if d not in sig.index or not sig.loc[d]: continue
                df = uni[tk]
                if d not in df.index: continue
                candidates.append((tk, df.loc[d]["mom12m"]))
            # hoechstes Momentum zuerst
            candidates.sort(key=lambda x: -x[1])
            for tk, _ in candidates:
                if len(positions) >= N_POSITIONS: break
                df = uni[tk]
                row = df.loc[d]
                entry_price = row["adj_close"]
                # Equal-Weight: 1/5 des aktuellen Equity
                current_equity = cash + sum(uni[t].loc[d]["adj_close"] * p["shares"]
                                           for t, p in positions.items() if d in uni[t].index)
                target_value = current_equity / N_POSITIONS
                shares = int(target_value / entry_price)
                if shares <= 0: continue
                cost = shares * entry_price + COMMISSION
                if cost > cash: continue
                cash -= cost
                positions[tk] = {"entry_date": d, "entry_price": entry_price,
                                "shares": shares, "days_held": 0}

        # Equity
        mtm = sum(uni[tk].loc[d]["adj_close"] * pos["shares"]
                  for tk, pos in positions.items() if d in uni[tk].index)
        equity_log.append({"date": d, "cash": cash, "mtm": mtm,
                          "equity": cash+mtm, "n_positions": len(positions)})

    return pd.DataFrame(equity_log), pd.DataFrame(trades_log)


def summarize(eq, tr):
    ret = eq["equity"].pct_change().dropna()
    n_years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (eq["equity"].iloc[-1] / eq["equity"].iloc[0]) ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    downside = ret[ret < 0].std()
    sortino = ret.mean() / downside * np.sqrt(252) if downside > 0 else np.nan
    dd = eq["equity"] / eq["equity"].cummax() - 1
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan
    wr = (tr["pnl"] > 0).mean() if len(tr) else 0
    avg_hold = tr["days_held"].mean() if len(tr) else 0
    return {"cagr": round(float(cagr),4), "sharpe": round(float(sharpe),3),
            "sortino": round(float(sortino),3),
            "max_dd": round(float(max_dd),4), "calmar": round(float(calmar),3),
            "n_trades": int(len(tr)), "win_rate": round(float(wr),3),
            "avg_days_held": round(float(avg_hold),1)}


def main():
    print("[+] Loading universe ...")
    uni = load_universe()
    print(f"    {len(uni)} tickers (stocks + sector-ETFs + broad-ETFs)")
    print(f"[+] Running Trend-Following backtest {START_OOS} -> today ...")
    eq, tr = run_trend_backtest(uni)
    s = summarize(eq, tr)
    print(f"[+] Trend-Module Results:")
    for k, v in s.items(): print(f"    {k:18s}: {v}")

    eq.to_parquet(OUT / "equity_daily.parquet", index=False)
    tr.to_parquet(OUT / "trades.parquet", index=False)
    (OUT / "summary.json").write_text(json.dumps(s, indent=2, default=str))

    # Plot
    fig, axes = plt.subplots(2, 1, figsize=(11, 9))
    spy = pd.read_parquet(LAKE / "prices" / "daily" / "SPY.parquet")
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy.set_index("date").sort_index()
    spy_cut = spy.loc[eq["date"].iloc[0]:eq["date"].iloc[-1]].copy()
    spy_cut["eq_norm"] = spy_cut["adj_close"] / spy_cut["adj_close"].iloc[0] * STARTING_CAPITAL
    axes[0].plot(eq["date"], eq["equity"], label="Trend-Module", color="#548235", lw=1.3)
    axes[0].plot(spy_cut.index, spy_cut["eq_norm"], label="SPY B&H", color="#C55A11", lw=1.0, alpha=0.75)
    axes[0].set_yscale("log"); axes[0].legend(); axes[0].grid(alpha=0.3)
    axes[0].set_title("Trend-Following-Module vs SPY")
    dd = eq["equity"] / eq["equity"].cummax() - 1
    axes[1].fill_between(eq["date"], dd, 0, color="#C00000", alpha=0.55)
    axes[1].set_title(f"Drawdown: Max: {dd.min():.2%}")
    axes[1].grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(OUT / "trend_equity.png", dpi=110)
    plt.close()


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
