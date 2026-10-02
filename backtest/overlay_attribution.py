#!/usr/bin/env python3
"""
Weg 1: Overlay-Attribution

Laeuft Baseline + jedes Overlay einzeln, misst ΔCAGR / ΔSharpe / ΔMaxDD.
Identifiziert welche Overlays echtes Alpha bringen und welche redundant sind.

Overlays getestet:
  O3_LIQUIDITY:  Fed-NetLiq (WALCL - WTREGEN - RRPONTSYD) steigend → erlaubt,
                 fallend → Positionsgroesse halbieren
  O4_CREDIT:     HY-OAS < 500bps + nicht steigend → erlaubt, sonst blockieren
  O4_CREDIT_SOFT: bei HY-OAS > 700bps Stops verdoppeln (Crash-Modus)
  E1_EARNINGS:   kein Entry im ±2-Tage-Fenster um Earnings-Datum
  E8_WEEKLY:     zusaetzlich zu Close>SMA200 auch Close>40-Wochen-SMA
  O4+O3 kombo:   beide zusammen

Outputs:
  backtest/results_overlay/metrics.parquet
  backtest/results_overlay/delta_chart.png
  backtest/results_overlay/attribution_report.md
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_overlay"
OUT.mkdir(parents=True, exist_ok=True)

# === Strategie-Parameter identisch zu Baseline ===
SMA_LONG = 200
SMA_EXIT = 5
RSI_N = 2
RSI_ENTRY = 25.0
ATR_N = 10
ATR_STOP_MULT = 2.5
TIME_STOP_DAYS = 10
RISK_PER_TRADE = 0.01
MAX_POSITIONS = 10
STARTING_CAPITAL = 100_000.0
COMMISSION = 1.0
INCLUDE_GROUPS = {"stocks_us_large"}
START_OOS = "1997-01-01"


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
            # Weekly-Trend fuer E8
            weekly_close = df["adj_close"].resample("W-FRI").last()
            weekly_sma40 = weekly_close.rolling(40).mean()
            df["wk_sma40"] = weekly_sma40.reindex(df.index, method="ffill")
            uni[s["id"]] = df
    return uni


def load_macro():
    """Laedt WALCL/WTREGEN/RRPONTSYD fuer NetLiq + HY-OAS."""
    walcl = pd.read_parquet(LAKE / "macro_fred" / "WALCL.parquet")
    tga = pd.read_parquet(LAKE / "macro_fred" / "WTREGEN.parquet")
    rrp = pd.read_parquet(LAKE / "macro_fred" / "RRPONTSYD.parquet")
    hy = pd.read_parquet(LAKE / "macro_fred" / "HY_OAS.parquet")
    for d in (walcl, tga, rrp, hy):
        d["date"] = pd.to_datetime(d["date"])
    macro = walcl.merge(tga, on="date", suffixes=("_walcl", "_tga"))
    macro = macro.merge(rrp, on="date"); macro = macro.rename(columns={"value": "rrp"})
    macro["value_walcl"] = macro["value_walcl"].astype(float)
    macro["value_tga"] = macro["value_tga"].astype(float)
    macro["rrp"] = macro["rrp"].astype(float)
    # Net-Liquidity in Milliarden
    macro["netliq"] = macro["value_walcl"] - macro["value_tga"] - macro["rrp"]
    macro["netliq_4w_change"] = macro["netliq"].diff(4)  # weekly data, 4 weeks = 1 month
    macro = macro[["date", "netliq", "netliq_4w_change"]].set_index("date").sort_index()

    hy = hy.rename(columns={"value": "hy_oas"}).set_index("date").sort_index()
    return macro, hy


def load_earnings():
    p = LAKE / "fundamentals_yf" / "earnings_history.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["earnings_date"] = pd.to_datetime(df["earnings_date"])
    # Build a lookup: ticker -> set of blackout dates
    lookup = {}
    for tk, g in df.groupby("ticker"):
        dates = set()
        for d in g["earnings_date"]:
            for offset in range(-2, 3):
                dates.add((d + pd.Timedelta(days=offset)).normalize())
        lookup[tk] = dates
    return lookup


def generate_signals(df: pd.DataFrame, config: dict) -> pd.Series:
    base = (
        (df["adj_close"] > df["sma200"])
        & (df["rsi2"] < RSI_ENTRY)
        & (df["ret_1d"] > -0.05)
    )
    if config.get("E8_WEEKLY"):
        base &= (df["adj_close"] > df["wk_sma40"])
    return base.fillna(False)


def run_backtest(uni, macro_env, hy, earnings_bk, config):
    all_dates = sorted(set().union(*[df.index for df in uni.values()]))
    all_dates = [d for d in all_dates if d >= pd.Timestamp(START_OOS)]

    signals = {tk: generate_signals(df, config) for tk, df in uni.items()}

    cash = STARTING_CAPITAL
    positions = {}
    equity_log = []
    trades_log = []

    # Macro-Lookup vorbereiten
    netliq_series = macro_env["netliq_4w_change"].reindex(pd.DatetimeIndex(all_dates), method="ffill")
    hy_series = hy["hy_oas"].reindex(pd.DatetimeIndex(all_dates), method="ffill")

    for d in all_dates:
        # Macro-Gates
        netliq_pos = True
        if config.get("O3_LIQUIDITY"):
            val = netliq_series.get(d, 0)
            netliq_pos = (val > 0)
        credit_allow = True
        credit_crash = False
        if config.get("O4_CREDIT"):
            hy_val = hy_series.get(d, 4.0)
            # Blockieren wenn HY > 500bps und steigt
            hy_prev = hy_series.shift(5).get(d, hy_val)
            rising = hy_val > hy_prev
            credit_allow = not (hy_val > 5.0 and rising)
            credit_crash = (hy_val > 7.0)

        # --- Exits ---
        to_close = []
        for tk, pos in positions.items():
            df = uni[tk]
            if d not in df.index: continue
            row = df.loc[d]
            pos["days_held"] += 1
            # Crash-Modus: Stops verdoppeln (naeher am Preis = frueher raus)
            stop_mult = 0.5 if credit_crash else 1.0
            effective_stop = pos["entry_price"] - stop_mult * (pos["entry_price"] - pos["stop"])
            exit_reason = None; exit_price = None
            if row["low"] <= effective_stop:
                exit_price = effective_stop; exit_reason = "ATR_STOP"
            elif row["adj_close"] > row["sma5"]:
                exit_price = row["adj_close"]; exit_reason = "MEAN_REVERSION"
            elif pos["days_held"] >= TIME_STOP_DAYS:
                exit_price = row["adj_close"]; exit_reason = "TIME_STOP"
            if exit_reason:
                pnl = (exit_price - pos["entry_price"]) * pos["shares"] - 2*COMMISSION
                cash += exit_price * pos["shares"] - COMMISSION
                trades_log.append({"ticker": tk, "entry_date": pos["entry_date"],
                                   "entry_price": pos["entry_price"], "exit_date": d,
                                   "exit_price": exit_price, "shares": pos["shares"],
                                   "pnl": pnl, "pnl_pct": (exit_price/pos["entry_price"])-1,
                                   "days_held": pos["days_held"], "reason": exit_reason})
                to_close.append(tk)
        for tk in to_close: del positions[tk]

        # --- Entries ---
        if credit_allow and len(positions) < MAX_POSITIONS:
            # Sizing-Tilt fuer O3
            size_factor = 1.0 if netliq_pos else 0.5
            candidates = []
            for tk, sig in signals.items():
                if tk in positions: continue
                if d not in sig.index or not sig.loc[d]: continue
                df = uni[tk]
                if d not in df.index: continue
                # Earnings-Blackout
                if config.get("E1_EARNINGS") and earnings_bk:
                    if tk in earnings_bk and d.normalize() in earnings_bk[tk]:
                        continue
                candidates.append((tk, df.loc[d]["rsi2"]))
            candidates.sort(key=lambda x: x[1])
            for tk, _ in candidates:
                if len(positions) >= MAX_POSITIONS: break
                df = uni[tk]
                row = df.loc[d]
                entry_price = row["adj_close"]
                atr_val = row["atr10"]
                if pd.isna(atr_val) or atr_val <= 0: continue
                stop_price = entry_price - ATR_STOP_MULT * atr_val
                risk_per_share = entry_price - stop_price
                dollar_risk = STARTING_CAPITAL * RISK_PER_TRADE * size_factor
                shares = int(dollar_risk / risk_per_share) if risk_per_share > 0 else 0
                if shares <= 0: continue
                cost = shares * entry_price + COMMISSION
                if cost > cash: continue
                cash -= cost
                positions[tk] = {"entry_date": d, "entry_price": entry_price,
                                "shares": shares, "stop": stop_price, "days_held": 0}

        # --- Equity MTM ---
        mtm = sum(uni[tk].loc[d]["adj_close"] * pos["shares"]
                  for tk, pos in positions.items() if d in uni[tk].index)
        equity_log.append({"date": d, "cash": cash, "mtm": mtm, "equity": cash+mtm,
                          "n_positions": len(positions)})

    return pd.DataFrame(equity_log), pd.DataFrame(trades_log)


def summarize(eq, tr):
    ret = eq["equity"].pct_change().dropna()
    n_years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (eq["equity"].iloc[-1] / eq["equity"].iloc[0]) ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    dd = eq["equity"] / eq["equity"].cummax() - 1
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan
    wr = (tr["pnl"] > 0).mean() if len(tr) else 0
    return {"cagr": round(float(cagr),4), "sharpe": round(float(sharpe),3),
            "max_dd": round(float(max_dd),4), "calmar": round(float(calmar),3),
            "n_trades": int(len(tr)), "win_rate": round(float(wr),3)}


def main():
    print("[+] Loading universe + macro ...")
    uni = load_universe()
    macro, hy = load_macro()
    earnings_bk = load_earnings()
    print(f"    {len(uni)} tickers, Macro NetLiq+HY ready, {len(earnings_bk) if earnings_bk else 0} earnings-lookups")

    configs = {
        "BASELINE":          {},
        "O3_LIQUIDITY":      {"O3_LIQUIDITY": True},
        "O4_CREDIT":         {"O4_CREDIT": True},
        "E1_EARNINGS":       {"E1_EARNINGS": True},
        "E8_WEEKLY":         {"E8_WEEKLY": True},
        "O3+O4":             {"O3_LIQUIDITY": True, "O4_CREDIT": True},
        "O3+O4+E1":          {"O3_LIQUIDITY": True, "O4_CREDIT": True, "E1_EARNINGS": True},
        "O3+O4+E1+E8":       {"O3_LIQUIDITY": True, "O4_CREDIT": True, "E1_EARNINGS": True, "E8_WEEKLY": True},
    }

    results = []
    equity_curves = {}
    for name, cfg in configs.items():
        print(f"[+] Running {name} ...")
        eq, tr = run_backtest(uni, macro, hy, earnings_bk, cfg)
        m = summarize(eq, tr)
        results.append({"overlay": name, **m})
        equity_curves[name] = eq.set_index("date")["equity"]
        print(f"    CAGR={m['cagr']:.2%}  Sharpe={m['sharpe']}  MaxDD={m['max_dd']:.2%}  Trades={m['n_trades']}")

    df = pd.DataFrame(results)
    df.to_parquet(OUT / "metrics.parquet", index=False)

    # Delta vs Baseline
    base = df[df["overlay"] == "BASELINE"].iloc[0]
    df["d_cagr"] = (df["cagr"] - base["cagr"]).round(4)
    df["d_sharpe"] = (df["sharpe"] - base["sharpe"]).round(3)
    df["d_maxdd"] = (df["max_dd"] - base["max_dd"]).round(4)
    df["d_trades"] = (df["n_trades"] - base["n_trades"]).astype(int)

    print("\n" + "="*90)
    print("OVERLAY ATTRIBUTION  (Δ vs BASELINE)")
    print("="*90)
    print(df[["overlay","cagr","sharpe","max_dd","calmar","n_trades","win_rate",
             "d_cagr","d_sharpe","d_maxdd","d_trades"]].to_string(index=False))

    # Chart
    fig, ax = plt.subplots(figsize=(12, 7))
    for name, eq in equity_curves.items():
        lw = 2.0 if name == "BASELINE" else 1.2
        alpha = 1.0 if name == "BASELINE" else 0.85
        ax.plot(eq.index, eq.values, label=name, lw=lw, alpha=alpha)
    ax.set_title("Overlay-Attribution: Equity-Curves 1997-2026")
    ax.set_ylabel("Equity (USD)"); ax.set_yscale("log")
    ax.legend(fontsize=9, loc="upper left"); ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(OUT / "delta_chart.png", dpi=110)
    plt.close()

    # Report
    lines = [
        "# Overlay-Attribution Report",
        "",
        "## Delta-Table vs Baseline",
        "",
        "| Overlay | CAGR | Sharpe | MaxDD | Calmar | Trades | WR | Δ CAGR | Δ Sharpe | Δ MaxDD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in df.iterrows():
        lines.append(f"| {r['overlay']} | {r['cagr']:.2%} | {r['sharpe']} | {r['max_dd']:.2%} | "
                     f"{r['calmar']} | {r['n_trades']} | {r['win_rate']:.1%} | "
                     f"{r['d_cagr']:+.2%} | {r['d_sharpe']:+.2f} | {r['d_maxdd']:+.2%} |")
    (OUT / "attribution_report.md").write_text("\n".join(lines))
    print(f"\n[+] Report: {OUT / 'attribution_report.md'}")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
