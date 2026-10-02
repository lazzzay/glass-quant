#!/usr/bin/env python3
"""
Phase C-1: Walk-Forward-Parametertuning (MR-Baseline)

Ziel: Messen, ob die Baseline-Parameter (RSI_ENTRY, ATR_STOP_MULT) auf
historischen Daten ueberangepasst sind oder robust.

Methode:
  IS  = 1997-01-01 .. 2012-12-31  (16 Jahre)
  OOS = 2013-01-01 .. 2026-04-15  (13 Jahre)

  Param-Grid:
    RSI_ENTRY   in {20, 23, 25, 27, 30}
    ATR_MULT    in {2.0, 2.5, 3.0}
  = 15 Kombis

  Vorgehen:
    1. Fuer jede Kombi: Backtest auf IS, Sharpe messen
    2. Best-IS-Kombi -> auf OOS anwenden, Sharpe messen
    3. Vergleich: IS-Sharpe vs OOS-Sharpe -> Degradation-Ratio
       Ratio > 0.7 = robust, < 0.5 = ueberangepasst

Output: backtest/results_walkforward/
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_walkforward"
OUT.mkdir(parents=True, exist_ok=True)

SMA_LONG = 200
SMA_EXIT = 5
RSI_N = 2
ATR_N = 10
TIME_STOP_DAYS = 10
RISK_PER_TRADE = 0.01
MAX_POSITIONS = 10
STARTING_CAPITAL = 100_000.0
COMMISSION = 1.0
INCLUDE_GROUPS = {"stocks_us_large"}

IS_START = "1997-01-01"
IS_END = "2012-12-31"
OOS_START = "2013-01-01"
OOS_END = "2026-04-15"

GRID_RSI = [20, 23, 25, 27, 30]
GRID_ATR = [2.0, 2.5, 3.0]


def rsi(series, n):
    delta = series.diff()
    up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    ru = up.ewm(alpha=1/n, adjust=False).mean()
    rd = dn.ewm(alpha=1/n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100/(1+rs)


def atr(df, n):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h-l), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


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
            df["sma5"] = df["adj_close"].rolling(SMA_EXIT).mean()
            df["rsi2"] = rsi(df["adj_close"], RSI_N)
            df["atr10"] = atr(df, ATR_N)
            df["ret_1d"] = df["adj_close"].pct_change()
            uni[s["id"]] = df
    return uni


def run_backtest(uni, start, end, rsi_entry, atr_mult):
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    all_dates = sorted(set().union(*[df.index for df in uni.values()]))
    all_dates = [d for d in all_dates if start_ts <= d <= end_ts]

    signals = {tk: ((df["adj_close"] > df["sma200"]) &
                    (df["rsi2"] < rsi_entry) &
                    (df["ret_1d"] > -0.05)).fillna(False)
               for tk, df in uni.items()}

    cash = STARTING_CAPITAL; positions = {}; equity_log = []
    for d in all_dates:
        # Exits
        to_close = []
        for tk, pos in positions.items():
            df = uni[tk]
            if d not in df.index: continue
            row = df.loc[d]
            pos["days_held"] += 1
            exit_price = None
            if row["low"] <= pos["stop"]:
                exit_price = pos["stop"]
            elif row["adj_close"] > row["sma5"]:
                exit_price = row["adj_close"]
            elif pos["days_held"] >= TIME_STOP_DAYS:
                exit_price = row["adj_close"]
            if exit_price:
                cash += exit_price * pos["shares"] - COMMISSION
                to_close.append(tk)
        for tk in to_close: del positions[tk]

        # Entries
        if len(positions) < MAX_POSITIONS:
            candidates = []
            for tk, sig in signals.items():
                if tk in positions: continue
                if d not in sig.index or not sig.loc[d]: continue
                df = uni[tk]
                if d not in df.index: continue
                candidates.append((tk, df.loc[d]["rsi2"]))
            candidates.sort(key=lambda x: x[1])
            for tk, _ in candidates:
                if len(positions) >= MAX_POSITIONS: break
                df = uni[tk]; row = df.loc[d]
                entry_price = row["adj_close"]; atr_val = row["atr10"]
                if pd.isna(atr_val) or atr_val <= 0: continue
                stop_price = entry_price - atr_mult * atr_val
                risk_per_share = entry_price - stop_price
                dollar_risk = STARTING_CAPITAL * RISK_PER_TRADE
                shares = int(dollar_risk / risk_per_share) if risk_per_share > 0 else 0
                if shares <= 0: continue
                cost = shares * entry_price + COMMISSION
                if cost > cash: continue
                cash -= cost
                positions[tk] = {"entry_price": entry_price, "shares": shares,
                                "stop": stop_price, "days_held": 0}

        mtm = sum(uni[tk].loc[d]["adj_close"] * pos["shares"]
                  for tk, pos in positions.items() if d in uni[tk].index)
        equity_log.append({"date": d, "equity": cash+mtm})

    eq = pd.DataFrame(equity_log)
    return eq


def metrics(eq):
    if len(eq) < 50:
        return {"cagr": np.nan, "sharpe": np.nan, "max_dd": np.nan}
    ret = eq["equity"].pct_change().dropna()
    n_years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (eq["equity"].iloc[-1] / eq["equity"].iloc[0]) ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    dd = eq["equity"] / eq["equity"].cummax() - 1
    max_dd = dd.min()
    return {"cagr": round(float(cagr),4), "sharpe": round(float(sharpe),3),
            "max_dd": round(float(max_dd),4)}


def main():
    print("[+] Loading universe ...")
    uni = load_universe()
    print(f"    {len(uni)} tickers")

    is_rows = []
    print(f"\n[+] IS-Grid-Search ({IS_START} -> {IS_END}) ...")
    for rsi_entry in GRID_RSI:
        for atr_mult in GRID_ATR:
            eq = run_backtest(uni, IS_START, IS_END, rsi_entry, atr_mult)
            m = metrics(eq)
            is_rows.append({"rsi": rsi_entry, "atr": atr_mult, **m})
            print(f"    RSI={rsi_entry} ATR={atr_mult} -> "
                  f"CAGR={m['cagr']:.2%} Sharpe={m['sharpe']} DD={m['max_dd']:.2%}")

    is_df = pd.DataFrame(is_rows).sort_values("sharpe", ascending=False)
    is_df.to_parquet(OUT / "is_grid.parquet", index=False)

    # Best-IS
    best_is = is_df.iloc[0]
    print(f"\n[+] Best IS: RSI={best_is['rsi']} ATR={best_is['atr']} "
          f"Sharpe={best_is['sharpe']}")

    # Baseline-Kombi (RSI=25, ATR=2.5) zur Referenz
    baseline_is = is_df[(is_df["rsi"]==25) & (is_df["atr"]==2.5)].iloc[0]
    print(f"    Baseline (25, 2.5) IS Sharpe={baseline_is['sharpe']}")

    print(f"\n[+] OOS-Validierung ({OOS_START} -> {OOS_END}) ...")
    # Best-IS auf OOS
    eq_best = run_backtest(uni, OOS_START, OOS_END, int(best_is["rsi"]), float(best_is["atr"]))
    m_best = metrics(eq_best)
    print(f"    Best-IS auf OOS: CAGR={m_best['cagr']:.2%} "
          f"Sharpe={m_best['sharpe']} DD={m_best['max_dd']:.2%}")

    # Baseline auf OOS
    eq_base = run_backtest(uni, OOS_START, OOS_END, 25, 2.5)
    m_base = metrics(eq_base)
    print(f"    Baseline(25,2.5) OOS: CAGR={m_base['cagr']:.2%} "
          f"Sharpe={m_base['sharpe']} DD={m_base['max_dd']:.2%}")

    # Degradation-Ratio
    ratio_best = m_best["sharpe"] / best_is["sharpe"] if best_is["sharpe"] > 0 else np.nan
    ratio_base = m_base["sharpe"] / baseline_is["sharpe"] if baseline_is["sharpe"] > 0 else np.nan

    summary = {
        "IS_period": f"{IS_START} .. {IS_END}",
        "OOS_period": f"{OOS_START} .. {OOS_END}",
        "best_is": {"rsi": float(best_is["rsi"]), "atr": float(best_is["atr"]),
                    "is_sharpe": float(best_is["sharpe"]), "is_cagr": float(best_is["cagr"]),
                    "oos_sharpe": m_best["sharpe"], "oos_cagr": m_best["cagr"],
                    "oos_dd": m_best["max_dd"],
                    "degradation_ratio": round(float(ratio_best), 3)},
        "baseline_25_2.5": {"is_sharpe": float(baseline_is["sharpe"]),
                            "is_cagr": float(baseline_is["cagr"]),
                            "oos_sharpe": m_base["sharpe"],
                            "oos_cagr": m_base["cagr"],
                            "oos_dd": m_base["max_dd"],
                            "degradation_ratio": round(float(ratio_base), 3)},
    }
    (OUT / "walkforward_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(f"\n[+] Degradation-Ratio (OOS/IS Sharpe):")
    print(f"    Best-IS:   {ratio_best:.2f}  (>0.7 = robust, <0.5 = ueberangepasst)")
    print(f"    Baseline:  {ratio_base:.2f}")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
