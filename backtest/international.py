#!/usr/bin/env python3
"""
Phase C-3: International Diversification

Frage: Funktioniert die MR-Baseline auch auf DAX/Nikkei/FTSE (als Index oder
als Laender-ETF)? Wenn ja, wie korreliert sind die Returns mit der US-Version?

Wir testen auf 3 Niveaus:
  (1) Index-Ebene: IDX_GDAXI, IDX_N225, IDX_FTSE als 1-Asset-System
      MR-Baseline kauft den Index bei RSI2<25 und verkauft bei Close>SMA5
  (2) ETF-Ebene: EWG (DE), EWJ (JP), EWU (UK) als handelbare Assets
      (same mechanism, tradable as retail ETFs)
  (3) Global-Dual-Book: Korrelation US-Baseline-Returns vs INT-Returns
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_international"
OUT.mkdir(parents=True, exist_ok=True)

SMA_LONG = 200; SMA_EXIT = 5
RSI_N = 2; RSI_ENTRY = 25.0
ATR_N = 10; ATR_STOP_MULT = 2.5
TIME_STOP_DAYS = 10
RISK_PER_TRADE = 0.01
STARTING_CAPITAL = 100_000.0; COMMISSION = 1.0
START = "1997-01-01"; END = "2026-04-15"


def rsi(s, n):
    d = s.diff(); u = d.clip(lower=0); dn = -d.clip(upper=0)
    ru = u.ewm(alpha=1/n, adjust=False).mean()
    rd = dn.ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + ru/rd.replace(0, np.nan))


def atr(df, n):
    h,l,c = df["high"], df["low"], df["close"]; pc = c.shift(1)
    tr = pd.concat([(h-l),(h-pc).abs(),(l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def load(ticker):
    df = pd.read_parquet(LAKE / "prices" / "daily" / f"{ticker}.parquet")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["sma200"] = df["adj_close"].rolling(SMA_LONG).mean()
    df["sma5"] = df["adj_close"].rolling(SMA_EXIT).mean()
    df["rsi2"] = rsi(df["adj_close"], RSI_N)
    df["atr10"] = atr(df, ATR_N)
    df["ret_1d"] = df["adj_close"].pct_change()
    return df


def backtest_single(df, start=START, end=END):
    """Single-Asset Backtest"""
    sd = pd.Timestamp(start); ed = pd.Timestamp(end)
    df = df.loc[sd:ed]
    sig = ((df["adj_close"] > df["sma200"]) &
           (df["rsi2"] < RSI_ENTRY) &
           (df["ret_1d"] > -0.05)).fillna(False)

    cash = STARTING_CAPITAL; pos = None
    equity_log = []
    for d, row in df.iterrows():
        # Exits
        if pos:
            pos["days_held"] += 1
            exit_price = None
            if row["low"] <= pos["stop"]: exit_price = pos["stop"]
            elif row["adj_close"] > row["sma5"]: exit_price = row["adj_close"]
            elif pos["days_held"] >= TIME_STOP_DAYS: exit_price = row["adj_close"]
            if exit_price:
                cash += exit_price * pos["shares"] - COMMISSION
                pos = None
        # Entries
        if pos is None and sig.loc[d]:
            ep = row["adj_close"]; av = row["atr10"]
            if not pd.isna(av) and av > 0:
                stop = ep - ATR_STOP_MULT * av
                rps = ep - stop
                dr = STARTING_CAPITAL * RISK_PER_TRADE
                sh = int(dr / rps) if rps > 0 else 0
                if sh > 0 and sh * ep + COMMISSION <= cash:
                    cash -= sh * ep + COMMISSION
                    pos = {"entry_price": ep, "shares": sh, "stop": stop, "days_held": 0}
        mtm = pos["shares"] * row["adj_close"] if pos else 0
        equity_log.append({"date": d, "equity": cash+mtm})
    return pd.DataFrame(equity_log)


def metrics(eq):
    if len(eq) < 50: return {}
    ret = eq["equity"].pct_change().dropna()
    n_years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (eq["equity"].iloc[-1] / eq["equity"].iloc[0]) ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    dd = eq["equity"] / eq["equity"].cummax() - 1
    return {"cagr": round(float(cagr),4), "sharpe": round(float(sharpe),3),
            "max_dd": round(float(dd.min()),4)}


def main():
    # US-Baseline-Returns
    base_eq = pd.read_parquet(ROOT / "results_v1" / "equity_daily.parquet")
    base_eq["date"] = pd.to_datetime(base_eq["date"])
    base_eq = base_eq.set_index("date").sort_index()
    r_us = base_eq["equity"].pct_change()

    results = {}

    print("=== INDEX-LEVEL (nicht direkt handelbar, Referenz) ===")
    for tk in ["IDX_GDAXI", "IDX_N225", "IDX_FTSE"]:
        df = load(tk)
        eq = backtest_single(df)
        m = metrics(eq)
        # Korrelation mit US
        ret_i = eq.set_index("date")["equity"].pct_change()
        corr = r_us.reindex(ret_i.index).corr(ret_i)
        m["corr_us"] = round(float(corr), 3)
        results[f"idx_{tk}"] = m
        print(f"  {tk:12s}  CAGR={m['cagr']:.2%}  Sharpe={m['sharpe']}  "
              f"DD={m['max_dd']:.2%}  corr_US={m['corr_us']}")

    print("\n=== ETF-LEVEL (retail-tradable, EUR/USD-denominiert) ===")
    for tk in ["EWG", "EWJ", "EWU"]:
        df = load(tk)
        eq = backtest_single(df)
        m = metrics(eq)
        ret_i = eq.set_index("date")["equity"].pct_change()
        corr = r_us.reindex(ret_i.index).corr(ret_i)
        m["corr_us"] = round(float(corr), 3)
        results[f"etf_{tk}"] = m
        print(f"  {tk:12s}  CAGR={m['cagr']:.2%}  Sharpe={m['sharpe']}  "
              f"DD={m['max_dd']:.2%}  corr_US={m['corr_us']}")

    print("\n=== EQUAL-WEIGHT GLOBAL-MR-PORTFOLIO (US + DE + JP + UK) ===")
    # Laufe einzeln, dann gewichte 25% je
    us_ret = r_us
    etf_rets = {}
    for tk in ["EWG", "EWJ", "EWU"]:
        df = load(tk)
        eq = backtest_single(df)
        etf_rets[tk] = eq.set_index("date")["equity"].pct_change()

    # Gemeinsamer Zeitraum
    common = us_ret.index
    for r in etf_rets.values():
        common = common.intersection(r.index)
    us = us_ret.loc[common]
    de = etf_rets["EWG"].loc[common]
    jp = etf_rets["EWJ"].loc[common]
    uk = etf_rets["EWU"].loc[common]

    global_ret = (us + de + jp + uk) / 4.0
    eq_global = (1 + global_ret.fillna(0)).cumprod() * 100_000
    eq_global_df = pd.DataFrame({"date": eq_global.index, "equity": eq_global.values})
    m_global = metrics(eq_global_df)
    results["global_equal_weight"] = m_global
    print(f"  25US/25DE/25JP/25UK   CAGR={m_global['cagr']:.2%}  "
          f"Sharpe={m_global['sharpe']}  DD={m_global['max_dd']:.2%}")

    # Ref: Nur US
    us_eq_df = pd.DataFrame({"date": us.index, "equity": (1+us.fillna(0)).cumprod()*100_000})
    m_us = metrics(us_eq_df)
    print(f"  100% US (Ref)         CAGR={m_us['cagr']:.2%}  "
          f"Sharpe={m_us['sharpe']}  DD={m_us['max_dd']:.2%}")

    results["us_only_reference"] = m_us

    (OUT / "international_summary.json").write_text(
        json.dumps(results, indent=2, default=str))
    print(f"\n[+] Summary: {OUT}/international_summary.json")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
