#!/usr/bin/env python3
"""
Phase C-2: Survivorship-Bias-Check mit Index-Membership

Ehrliche Einschraenkung: Wir haben nur die 66 heutigen Large-Caps. Die
echten Index-Dropouts der letzten 30 Jahre (Lehman, Bear Stearns, GM, ...)
sind nicht im Datenbestand. Eine vollstaendige Bias-Fix ist nicht moeglich
ohne Neu-Download. Was wir aber testen koennen:

  Variante A: "Date-added-Filter": fuer jeden Ticker erst dann handeln,
              wenn er tatsaechlich in den S&P 500 aufgenommen wurde.
              Verhindert Backtest-Trades, die retrospektiv ueberhaupt nicht
              moeglich waren (z.B. AAPL war nicht 1995 schon SP500-Member).

  Variante B: Baseline-Delta messen
              Vergleich: CAGR/Sharpe mit vs ohne Date-Filter.

Interpretation:
  Kleine Delta (<10%) -> bias ist moderat, Ergebnisse bleiben vertretbar
  Grosse Delta (>30%) -> viele Trades waeren vor SP500-Eintritt erfolgt
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_survivorship"
OUT.mkdir(parents=True, exist_ok=True)

SMA_LONG = 200; SMA_EXIT = 5; RSI_N = 2; RSI_ENTRY = 25.0
ATR_N = 10; ATR_STOP_MULT = 2.5
TIME_STOP_DAYS = 10; RISK_PER_TRADE = 0.01
MAX_POSITIONS = 10; STARTING_CAPITAL = 100_000.0; COMMISSION = 1.0
INCLUDE_GROUPS = {"stocks_us_large"}
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


def load_membership_dates():
    df = pd.read_parquet(LAKE / "index_membership" / "sp500_current.parquet")
    # Normalize date - ignore obvious bad values
    df["added"] = pd.to_datetime(df["Date added"], errors="coerce")
    # Fallback: 1957 (SP500-Gruendung) fuer Werte die so alt oder aelter sind
    df.loc[df["added"].isna(), "added"] = pd.Timestamp("1957-01-01")
    return dict(zip(df["Symbol"], df["added"]))


def load_universe(add_dates):
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
            # Ticker-Symbol aus manifest-id ableiten (id ist z.B. "AAPL" oder "BRK_B")
            ticker = s["id"].replace("_", ".")
            df["sp500_add"] = add_dates.get(ticker, pd.Timestamp("1957-01-01"))
            uni[s["id"]] = df
    return uni


def run(uni, enforce_membership):
    sd = pd.Timestamp(START); ed = pd.Timestamp(END)
    all_dates = sorted(set().union(*[df.index for df in uni.values()]))
    all_dates = [d for d in all_dates if sd <= d <= ed]

    signals = {tk: ((df["adj_close"] > df["sma200"]) &
                    (df["rsi2"] < RSI_ENTRY) &
                    (df["ret_1d"] > -0.05)).fillna(False)
               for tk, df in uni.items()}

    cash = STARTING_CAPITAL; positions = {}; equity_log = []
    trades = 0; trades_blocked = 0

    for d in all_dates:
        to_close = []
        for tk, pos in positions.items():
            df = uni[tk]
            if d not in df.index: continue
            row = df.loc[d]
            pos["days_held"] += 1
            exit_price = None
            if row["low"] <= pos["stop"]: exit_price = pos["stop"]
            elif row["adj_close"] > row["sma5"]: exit_price = row["adj_close"]
            elif pos["days_held"] >= TIME_STOP_DAYS: exit_price = row["adj_close"]
            if exit_price:
                cash += exit_price * pos["shares"] - COMMISSION
                to_close.append(tk); trades += 1
        for tk in to_close: del positions[tk]

        if len(positions) < MAX_POSITIONS:
            cands = []
            for tk, sig in signals.items():
                if tk in positions: continue
                if d not in sig.index or not sig.loc[d]: continue
                df = uni[tk]
                if d not in df.index: continue
                if enforce_membership and d < df["sp500_add"].iloc[0]:
                    trades_blocked += 1
                    continue
                cands.append((tk, df.loc[d]["rsi2"]))
            cands.sort(key=lambda x: x[1])
            for tk, _ in cands:
                if len(positions) >= MAX_POSITIONS: break
                df = uni[tk]; row = df.loc[d]
                ep = row["adj_close"]; av = row["atr10"]
                if pd.isna(av) or av <= 0: continue
                stop = ep - ATR_STOP_MULT * av
                rps = ep - stop
                dr = STARTING_CAPITAL * RISK_PER_TRADE
                sh = int(dr / rps) if rps > 0 else 0
                if sh <= 0: continue
                cost = sh * ep + COMMISSION
                if cost > cash: continue
                cash -= cost
                positions[tk] = {"entry_price": ep, "shares": sh, "stop": stop, "days_held": 0}

        mtm = sum(uni[tk].loc[d]["adj_close"] * p["shares"]
                  for tk, p in positions.items() if d in uni[tk].index)
        equity_log.append({"date": d, "equity": cash+mtm})

    eq = pd.DataFrame(equity_log)
    return eq, trades, trades_blocked


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
    print("[+] Loading SP500 membership dates ...")
    add_dates = load_membership_dates()
    print(f"    {len(add_dates)} add-dates loaded")
    sample = [(k, str(v.date())) for k,v in list(add_dates.items())[:5]]
    print(f"    Sample: {sample}")

    print("[+] Loading universe ...")
    uni = load_universe(add_dates)
    print(f"    {len(uni)} tickers")

    print("\n[+] Run A: OHNE Membership-Filter (status quo) ...")
    eq_a, tr_a, _ = run(uni, enforce_membership=False)
    m_a = metrics(eq_a)
    print(f"    CAGR={m_a['cagr']:.2%}  Sharpe={m_a['sharpe']}  DD={m_a['max_dd']:.2%}  Trades={tr_a}")

    print("\n[+] Run B: MIT Membership-Filter (nur nach SP500-Add-Date handelbar) ...")
    eq_b, tr_b, blocked = run(uni, enforce_membership=True)
    m_b = metrics(eq_b)
    print(f"    CAGR={m_b['cagr']:.2%}  Sharpe={m_b['sharpe']}  DD={m_b['max_dd']:.2%}  "
          f"Trades={tr_b}  Blocked={blocked}")

    delta_cagr = m_b["cagr"] - m_a["cagr"]
    delta_sharpe = m_b["sharpe"] - m_a["sharpe"]

    summary = {
        "note": ("Eingeschraenkter Bias-Fix: nur Date-Filter auf aktuelle 66 "
                 "Ticker. Vollstaendiger Fix braucht Downloads delisted "
                 "Tickers (Lehman, Bear Stearns, GM, ...)."),
        "period": f"{START} .. {END}",
        "without_filter": m_a,
        "with_filter": m_b,
        "delta_cagr": round(float(delta_cagr), 4),
        "delta_sharpe": round(float(delta_sharpe), 3),
        "trades_blocked_by_filter": int(blocked),
        "interpretation": (
            "Delta CAGR < 0.5% = Bias klein, 0.5-2% = moderat, >2% = relevant. "
            "Hinweis: echter Survivorship-Bias (delisted-tickers fehlen) ist "
            "strukturell weiterhin praesent, aber die 66 Werte sind robuste "
            "Large-Caps (Apple, MSFT, JPM, etc.) mit niedrigem Dropout-Risiko."
        )
    }
    (OUT / "survivorship_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(f"\n[+] Delta: CAGR {delta_cagr:+.2%}  Sharpe {delta_sharpe:+.3f}")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
