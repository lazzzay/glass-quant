#!/usr/bin/env python3
"""
Weg 3 Teil 2: Dual-Book Portfolio (Mean-Reversion + Trend-Following)

Kombiniert die beiden Module und vergleicht mit allen Einzel-Komponenten
plus SPY-Buy&Hold. Misst auch verschiedene Mix-Verhaeltnisse + Rebalancing.
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
OUT = ROOT / "results_dualbook"
OUT.mkdir(parents=True, exist_ok=True)


def metrics(ret: pd.Series, start: float = 100_000) -> dict:
    ret = ret.dropna()
    if len(ret) < 50: return {}
    eq = start * (1 + ret).cumprod()
    n_years = len(ret) / 252
    cagr = (eq.iloc[-1] / start) ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    down = ret[ret < 0].std()
    sortino = ret.mean() / down * np.sqrt(252) if down > 0 else np.nan
    dd = eq / eq.cummax() - 1
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan
    return {"cagr": round(float(cagr),4), "sharpe": round(float(sharpe),3),
            "sortino": round(float(sortino),3), "max_dd": round(float(max_dd),4),
            "calmar": round(float(calmar),3), "vol_ann": round(float(ret.std()*np.sqrt(252)),4)}


def blend(r_a: pd.Series, r_b: pd.Series, w_a: float, rebal: str | None) -> pd.Series:
    df = pd.concat([r_a, r_b], axis=1, keys=["a", "b"]).dropna()
    if rebal is None:
        eq_a = (1 + df["a"]).cumprod()
        eq_b = (1 + df["b"]).cumprod()
        eq_tot = w_a * eq_a + (1 - w_a) * eq_b
        return eq_tot.pct_change().dropna()
    df["period"] = df.index.to_period(rebal)
    out = []
    for _, g in df.groupby("period"):
        eq_a = (1 + g["a"]).cumprod()
        eq_b = (1 + g["b"]).cumprod()
        eq_tot = w_a * eq_a + (1 - w_a) * eq_b
        out.append(eq_tot.pct_change().dropna())
    return pd.concat(out).sort_index()


def load_returns(path: Path, col: str = "equity") -> pd.Series:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[col].pct_change()


def main():
    r_mr = load_returns(ROOT / "results_v1" / "equity_daily.parquet")
    r_tr = load_returns(ROOT / "results_trend" / "equity_daily.parquet")

    spy = pd.read_parquet(LAKE / "prices" / "daily" / "SPY.parquet")
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy.set_index("date").sort_index()
    r_spy = spy["adj_close"].pct_change()

    # Gemeinsamer Zeitraum
    common = r_mr.index.intersection(r_tr.index).intersection(r_spy.index)
    r_mr, r_tr, r_spy = r_mr.loc[common], r_tr.loc[common], r_spy.loc[common]
    print(f"[+] Common: {common.min().date()} -> {common.max().date()}  ({len(common)} Tage)")

    # === Einzelkomponenten ===
    print("\n=== EINZELKOMPONENTEN ===")
    for lab, r in [("MR-Baseline", r_mr), ("Trend-Module", r_tr), ("SPY-B&H", r_spy)]:
        m = metrics(r)
        print(f"{lab:15s}  CAGR={m['cagr']:.2%}  Sharpe={m['sharpe']}  Sortino={m['sortino']}  "
              f"MaxDD={m['max_dd']:.2%}  Calmar={m['calmar']}")

    # === Dual-Book MR+Trend Mischungen ===
    print("\n=== DUAL-BOOK BLENDS (MR / Trend, quarterly rebal) ===")
    dualbook_rows = []
    curves = {}
    REBAL_MAP = {"quarterly": "Q", "monthly": "M", "yearly": "Y"}
    for w_mr in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        for reb_name, reb_code in [*REBAL_MAP.items(), ("none", None)]:
            r_blend = blend(r_mr, r_tr, w_mr, reb_code)
            m = metrics(r_blend)
            row = {"w_mr": w_mr, "w_trend": round(1-w_mr, 2), "rebal": reb_name, **m}
            dualbook_rows.append(row)
            if reb_name == "quarterly":
                curves[f"{int(w_mr*100)}MR/{int((1-w_mr)*100)}TR"] = (1 + r_blend).cumprod() * 100_000

    dual = pd.DataFrame(dualbook_rows).sort_values("sharpe", ascending=False)
    print(dual[["w_mr","w_trend","rebal","cagr","sharpe","sortino","max_dd","calmar"]].head(10).to_string(index=False))

    dual.to_parquet(OUT / "dualbook_metrics.parquet", index=False)

    # === Triple-Book: MR + Trend + SPY ===
    print("\n=== TRIPLE-BLENDS (MR / Trend / SPY, quarterly) ===")
    triple_rows = []
    for w_mr in [0.2, 0.3, 0.4, 0.5]:
        for w_tr in [0.2, 0.3, 0.4]:
            w_spy = round(1 - w_mr - w_tr, 2)
            if w_spy < 0 or w_spy > 0.8: continue
            # Synthese durch rekursives Blending
            r_mrtr = blend(r_mr, r_tr, w_mr/(w_mr+w_tr), "Q")
            w_alt = w_mr + w_tr
            r_triple = blend(r_mrtr, r_spy, w_alt, "Q")
            m = metrics(r_triple)
            triple_rows.append({"w_mr": w_mr, "w_trend": w_tr, "w_spy": w_spy, **m})
    triple = pd.DataFrame(triple_rows).sort_values("sharpe", ascending=False)
    print(triple.head(10).to_string(index=False))
    triple.to_parquet(OUT / "triple_metrics.parquet", index=False)

    # === Plot: Dual-Book-Equity-Curves ===
    fig, axes = plt.subplots(2, 1, figsize=(12, 10))
    # Einzelkomponenten
    ax = axes[0]
    eq_mr = (1 + r_mr).cumprod() * 100_000
    eq_tr = (1 + r_tr).cumprod() * 100_000
    eq_spy = (1 + r_spy).cumprod() * 100_000
    ax.plot(eq_mr.index, eq_mr.values, label="MR-Baseline", color="#2E75B6", lw=1.3)
    ax.plot(eq_tr.index, eq_tr.values, label="Trend-Module", color="#548235", lw=1.3)
    ax.plot(eq_spy.index, eq_spy.values, label="SPY B&H", color="#C55A11", lw=1.0, alpha=0.75)
    # Top-Sharpe-Blend
    top_blend = dual.iloc[0]
    REBAL_CODE = {"quarterly": "Q", "monthly": "M", "yearly": "Y", "none": None}
    r_top = blend(r_mr, r_tr, top_blend["w_mr"], REBAL_CODE.get(top_blend["rebal"]))
    eq_top = (1 + r_top).cumprod() * 100_000
    ax.plot(eq_top.index, eq_top.values,
            label=f"Best-Dual {int(top_blend['w_mr']*100)}MR/{int(top_blend['w_trend']*100)}TR ({top_blend['rebal']})",
            color="#7030A0", lw=2.0)
    ax.set_yscale("log"); ax.legend(); ax.grid(alpha=0.3)
    ax.set_title("Einzelkomponenten + Best-Dual-Book-Blend")

    # Drawdown-Vergleich
    ax = axes[1]
    for lab, eq, col in [("MR", eq_mr, "#2E75B6"), ("Trend", eq_tr, "#548235"),
                        ("SPY", eq_spy, "#C55A11"), ("Best-Dual", eq_top, "#7030A0")]:
        dd = eq / eq.cummax() - 1
        ax.plot(dd.index, dd.values, label=f"{lab} (max {dd.min():.1%})", lw=1.0, color=col)
    ax.legend(); ax.grid(alpha=0.3); ax.set_title("Drawdown-Vergleich")
    plt.tight_layout()
    plt.savefig(OUT / "dualbook_curves.png", dpi=110)
    plt.close()

    # Summary JSON
    (OUT / "dualbook_summary.json").write_text(json.dumps({
        "components": {
            "MR_Baseline": metrics(r_mr),
            "Trend_Module": metrics(r_tr),
            "SPY_BuyHold": metrics(r_spy),
        },
        "best_dualbook_sharpe": dual.head(3).to_dict(orient="records"),
        "best_dualbook_calmar": dual.sort_values("calmar", ascending=False).head(3).to_dict(orient="records"),
        "best_triple_sharpe": triple.head(3).to_dict(orient="records"),
    }, indent=2, default=str))


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
