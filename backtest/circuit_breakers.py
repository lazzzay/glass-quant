#!/usr/bin/env python3
"""
Phase C-4: Risk-Management-Overlays als CIRCUIT-BREAKER (nicht Alpha)

Weg 1 hat gezeigt: Overlays als Alpha-Filter funktionieren nicht (−0,01 bis
−1,71% CAGR). Frage jetzt: Schuetzen sie wenigstens den TAIL-RISK in
Crash-Jahren? Dafuer testen wir sie nur als HARTER Ausschalter bei extremen
Schwellwerten, mit dem Ziel, den Max-DD zu reduzieren.

Getestete Circuit-Breaker:
  CB1: VIX_PANIC  VIX > 40 UND SPY < SMA200
       Aktion: alle Positionen schliessen, kein Entry bis Trigger off
  CB2: HY_CRISIS  HY-OAS > 800 bps UND steigt
       Aktion: Positionsgroesse halbieren, kein Neuentry
  CB3: PORTFOLIO_DD  Equity-DD > -12%
       Aktion: Paper-Modus (keine Neu-Entries) bis DD < -5%

Messung: MaxDD, Sharpe, CAGR, Calmar, und vor allem die Crash-Jahre
(2008, 2020, 2022) im Detail.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_circuit_breakers"
OUT.mkdir(parents=True, exist_ok=True)

SMA_LONG = 200; SMA_EXIT = 5
RSI_N = 2; RSI_ENTRY = 25.0
ATR_N = 10; ATR_STOP_MULT = 2.5
TIME_STOP_DAYS = 10
RISK_PER_TRADE = 0.01; MAX_POSITIONS = 10
STARTING_CAPITAL = 100_000.0; COMMISSION = 1.0
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


def load_macro():
    vix = pd.read_parquet(LAKE / "macro_fred" / "VIXCLS.parquet")
    vix["date"] = pd.to_datetime(vix["date"])
    vix = vix.set_index("date").sort_index()["value"].astype(float)
    hy = pd.read_parquet(LAKE / "macro_fred" / "HY_OAS.parquet")
    hy["date"] = pd.to_datetime(hy["date"])
    hy = hy.set_index("date").sort_index()["value"].astype(float)
    spy = pd.read_parquet(LAKE / "prices" / "daily" / "SPY.parquet")
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy.set_index("date").sort_index()["adj_close"]
    spy_sma200 = spy.rolling(200).mean()
    return vix, hy, spy, spy_sma200


def run(uni, macro, config):
    vix, hy, spy, spy_sma = macro
    sd = pd.Timestamp(START); ed = pd.Timestamp(END)
    all_dates = sorted(set().union(*[df.index for df in uni.values()]))
    all_dates = [d for d in all_dates if sd <= d <= ed]

    idx = pd.DatetimeIndex(all_dates)
    vix_ff = vix.reindex(idx, method="ffill")
    hy_ff = hy.reindex(idx, method="ffill")
    hy_5d = hy_ff.shift(5)
    spy_ff = spy.reindex(idx, method="ffill")
    spy_sma_ff = spy_sma.reindex(idx, method="ffill")

    signals = {tk: ((df["adj_close"] > df["sma200"]) &
                    (df["rsi2"] < RSI_ENTRY) &
                    (df["ret_1d"] > -0.05)).fillna(False)
               for tk, df in uni.items()}

    cash = STARTING_CAPITAL; positions = {}; equity_log = []
    paper_mode_active = False

    for d in all_dates:
        # Circuit-Breaker evaluation
        vix_now = vix_ff.get(d, 20.0)
        hy_now = hy_ff.get(d, 4.0)
        hy_prev = hy_5d.get(d, hy_now)
        spy_now = spy_ff.get(d, np.nan)
        spy_sma_now = spy_sma_ff.get(d, np.nan)

        cb_panic = (config.get("CB1_PANIC") and
                    vix_now > 40 and spy_now < spy_sma_now)
        cb_crisis = (config.get("CB2_HY") and
                     hy_now > 8.0 and hy_now > hy_prev)
        cb_ddguard = False
        if config.get("CB3_DD") and equity_log:
            peak = max(e["equity"] for e in equity_log)
            current = equity_log[-1]["equity"]
            current_dd = (current / peak - 1) if peak > 0 else 0
            if not paper_mode_active and current_dd < -0.12:
                paper_mode_active = True
            elif paper_mode_active and current_dd > -0.05:
                paper_mode_active = False
            cb_ddguard = paper_mode_active

        # CB1 hard exit all
        if cb_panic and positions:
            for tk, pos in list(positions.items()):
                df = uni[tk]
                if d not in df.index: continue
                exit_price = df.loc[d]["adj_close"]
                cash += exit_price * pos["shares"] - COMMISSION
                del positions[tk]

        # Regular exits
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
                to_close.append(tk)
        for tk in to_close: del positions[tk]

        # Entry-Block
        entries_allowed = not (cb_panic or cb_crisis or cb_ddguard)
        size_factor = 0.5 if (cb_crisis and not cb_panic) else 1.0

        if entries_allowed and len(positions) < MAX_POSITIONS:
            cands = []
            for tk, sig in signals.items():
                if tk in positions: continue
                if d not in sig.index or not sig.loc[d]: continue
                df = uni[tk]
                if d not in df.index: continue
                cands.append((tk, df.loc[d]["rsi2"]))
            cands.sort(key=lambda x: x[1])
            for tk, _ in cands:
                if len(positions) >= MAX_POSITIONS: break
                df = uni[tk]; row = df.loc[d]
                ep = row["adj_close"]; av = row["atr10"]
                if pd.isna(av) or av <= 0: continue
                stop = ep - ATR_STOP_MULT * av
                rps = ep - stop
                dr = STARTING_CAPITAL * RISK_PER_TRADE * size_factor
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
    return eq


def metrics(eq):
    if len(eq) < 50: return {}
    ret = eq["equity"].pct_change().dropna()
    n_years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (eq["equity"].iloc[-1] / eq["equity"].iloc[0]) ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    dd = eq["equity"] / eq["equity"].cummax() - 1
    calmar = cagr / abs(dd.min()) if dd.min() < 0 else np.nan
    return {"cagr": round(float(cagr),4), "sharpe": round(float(sharpe),3),
            "max_dd": round(float(dd.min()),4), "calmar": round(float(calmar),3)}


def yearly_returns(eq):
    eq2 = eq.copy()
    eq2["date"] = pd.to_datetime(eq2["date"])
    eq2 = eq2.set_index("date")
    yearly = eq2["equity"].resample("Y").last().pct_change()
    return {str(d.year): round(float(v), 4) for d, v in yearly.items() if not pd.isna(v)}


def main():
    print("[+] Loading universe + macro ...")
    uni = load_universe()
    macro = load_macro()

    configs = {
        "BASELINE": {},
        "CB1_PANIC": {"CB1_PANIC": True},
        "CB2_HY": {"CB2_HY": True},
        "CB3_DD": {"CB3_DD": True},
        "CB1+CB2": {"CB1_PANIC": True, "CB2_HY": True},
        "ALL_CBS": {"CB1_PANIC": True, "CB2_HY": True, "CB3_DD": True},
    }

    results = {}
    for name, cfg in configs.items():
        print(f"\n[+] Running {name} ...")
        eq = run(uni, macro, cfg)
        m = metrics(eq)
        yr = yearly_returns(eq)
        results[name] = {**m, "yearly": yr}
        # Crash-Jahre extra betrachten
        crashes = {y: yr.get(y) for y in ["2000", "2001", "2002", "2008", "2020", "2022"]}
        print(f"    CAGR={m['cagr']:.2%}  Sharpe={m['sharpe']}  "
              f"DD={m['max_dd']:.2%}  Calmar={m['calmar']}")
        print(f"    Crash-Jahre: {crashes}")

    (OUT / "circuit_breakers_summary.json").write_text(
        json.dumps(results, indent=2, default=str))

    # Delta-Table
    base = results["BASELINE"]
    print("\n" + "=" * 80)
    print(f"{'Config':12s} {'CAGR':>8s} {'Sharpe':>8s} {'MaxDD':>8s} "
          f"{'Calmar':>8s} {'ΔCAGR':>8s} {'ΔDD':>8s}")
    print("=" * 80)
    for name, m in results.items():
        dc = m["cagr"] - base["cagr"]
        dd = m["max_dd"] - base["max_dd"]
        print(f"{name:12s} {m['cagr']:>7.2%} {m['sharpe']:>8} "
              f"{m['max_dd']:>7.2%} {m['calmar']:>8} "
              f"{dc:>+7.2%} {dd:>+7.2%}")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
