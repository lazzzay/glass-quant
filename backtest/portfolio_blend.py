#!/usr/bin/env python3
"""
Weg 2: Portfolio-Blending Baseline + SPY

Kombiniert die V6-Mean-Reversion-Baseline mit SPY-Buy&Hold in verschiedenen
Mischungsverhaeltnissen und Rebalancing-Regimes. Ziel: Pareto-Front der
Return-vs-Risk-Trade-offs (research comparison only, no recommendation).

Inputs:
  backtest/results_v1/equity_daily.parquet  (Baseline)
  data_lake/prices/daily/SPY.parquet        (SPY)

Outputs:
  backtest/results_blend/metrics.parquet
  backtest/results_blend/pareto_front.png
  backtest/results_blend/blend_equity_curves.png
  backtest/results_blend/blend_summary.json
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
BASE = ROOT / "results_v1"
LAKE = ROOT.parent / "data_lake"
OUT = ROOT / "results_blend"
OUT.mkdir(parents=True, exist_ok=True)

WEIGHTS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]  # Gewicht fuer Strategie
REBAL = {
    "none": None,         # keine
    "monthly": "M",
    "quarterly": "Q",
    "yearly": "Y",
}


def metrics(ret: pd.Series) -> dict:
    ret = ret.dropna()
    if len(ret) < 100:
        return {}
    n_years = len(ret) / 252
    eq = (1 + ret).cumprod()
    cagr = eq.iloc[-1] ** (1/n_years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    downside = ret[ret < 0].std()
    sortino = ret.mean() / downside * np.sqrt(252) if downside > 0 else np.nan
    dd = eq / eq.cummax() - 1
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan
    return {
        "cagr": round(float(cagr), 4),
        "sharpe": round(float(sharpe), 3),
        "sortino": round(float(sortino), 3),
        "max_dd": round(float(max_dd), 4),
        "calmar": round(float(calmar), 3),
        "vol_ann": round(float(ret.std() * np.sqrt(252)), 4),
    }


def blend_returns(r_strat: pd.Series, r_spy: pd.Series, w_strat: float, rebal: str | None) -> pd.Series:
    """Misch-Returns mit periodischem Rebalancing auf w_strat / (1-w_strat)."""
    df = pd.concat([r_strat, r_spy], axis=1, keys=["s", "y"]).dropna()
    if rebal is None:
        # Kein Rebal: Gewichte driften
        eq_s = (1 + df["s"]).cumprod()
        eq_y = (1 + df["y"]).cumprod()
        eq_tot = w_strat * eq_s + (1 - w_strat) * eq_y
        return eq_tot.pct_change().dropna()
    # Periodisches Rebal: jeden Period-Start Weight reset
    df["period"] = df.index.to_period(rebal)
    out = []
    for per, g in df.groupby("period"):
        eq_s = (1 + g["s"]).cumprod()
        eq_y = (1 + g["y"]).cumprod()
        eq_tot = w_strat * eq_s + (1 - w_strat) * eq_y
        out.append(eq_tot.pct_change().dropna())
    return pd.concat(out).sort_index()


def main():
    eq_base = pd.read_parquet(BASE / "equity_daily.parquet")
    eq_base["date"] = pd.to_datetime(eq_base["date"])
    eq_base = eq_base.set_index("date").sort_index()
    eq_base["ret"] = eq_base["equity"].pct_change()
    r_strat = eq_base["ret"]

    spy = pd.read_parquet(LAKE / "prices" / "daily" / "SPY.parquet")
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy.set_index("date").sort_index()
    spy["ret"] = spy["adj_close"].pct_change()
    r_spy = spy["ret"]

    # Auf gemeinsamen Zeitraum
    common = r_strat.index.intersection(r_spy.index)
    r_strat = r_strat.loc[common]
    r_spy = r_spy.loc[common]
    print(f"[+] Common period: {common.min().date()} -> {common.max().date()}  ({len(common)} days)")

    rows = []
    curves = {}
    for w in WEIGHTS:
        for reb_name, reb in REBAL.items():
            r = blend_returns(r_strat, r_spy, w, reb)
            m = metrics(r)
            row = {"w_strat": w, "w_spy": round(1-w, 2), "rebal": reb_name, **m}
            rows.append(row)
            if reb_name == "quarterly":  # save curve
                eq = (1 + r).cumprod() * 100_000
                curves[f"{int(w*100)}/{int((1-w)*100)}"] = eq

    df = pd.DataFrame(rows)
    df.to_parquet(OUT / "metrics.parquet", index=False)

    # === Pareto: CAGR vs MaxDD ===
    fig, ax = plt.subplots(figsize=(11, 7))
    color_map = {"none": "#888", "monthly": "#2E75B6", "quarterly": "#C55A11", "yearly": "#548235"}
    marker_map = {"none": "o", "monthly": "^", "quarterly": "s", "yearly": "D"}
    for reb_name in REBAL:
        sub = df[df["rebal"] == reb_name]
        ax.scatter(sub["max_dd"].abs()*100, sub["cagr"]*100,
                  c=color_map[reb_name], marker=marker_map[reb_name],
                  s=70, alpha=0.85, label=f"Rebal={reb_name}", edgecolor="white")
        for _, r in sub.iterrows():
            label = f"{int(r['w_strat']*100)}/{int(r['w_spy']*100)}"
            ax.annotate(label, (abs(r["max_dd"])*100, r["cagr"]*100),
                       fontsize=7, alpha=0.7, xytext=(4, 3), textcoords="offset points")
    ax.set_xlabel("Max Drawdown (%)"); ax.set_ylabel("CAGR (%)")
    ax.set_title("Pareto-Front: Baseline-Strategie / SPY-Blend  (1997-2026)")
    ax.legend(title="Rebalancing"); ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(OUT / "pareto_front.png", dpi=110)
    plt.close()

    # === Equity-Curves der Quarterly-Rebal Blends ===
    fig, ax = plt.subplots(figsize=(11, 7))
    selected = ["0/100", "30/70", "50/50", "70/30", "100/0"]
    colors = ["#C55A11", "#ED7D31", "#2E75B6", "#548235", "#7030A0"]
    for lab, col in zip(selected, colors):
        if lab in curves:
            ax.plot(curves[lab].index, curves[lab].values, label=lab + " (strat/SPY)", lw=1.2, color=col)
    ax.set_title("Equity-Curves Baseline/SPY-Blends (Quarterly-Rebal, Start 100k)")
    ax.set_ylabel("Equity (USD)"); ax.set_yscale("log"); ax.legend(); ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(OUT / "blend_equity_curves.png", dpi=110)
    plt.close()

    # === Beste Kombi pro Kriterium ===
    best = {
        "highest_sharpe": df.loc[df["sharpe"].idxmax()].to_dict(),
        "lowest_dd": df.loc[df["max_dd"].idxmax()].to_dict(),  # max = kleinster negativer = flachster DD
        "highest_calmar": df.loc[df["calmar"].idxmax()].to_dict(),
        "highest_cagr": df.loc[df["cagr"].idxmax()].to_dict(),
    }
    # Metrik-basierte Ranglisten (reiner Forschungsvergleich, keine Empfehlung)
    # low_dd: niedrigster DD, aber CAGR > 5%
    cons = df[df["cagr"] > 0.05].sort_values("max_dd", ascending=False).head(3)
    mod = df.sort_values("sharpe", ascending=False).head(5)
    agg = df[df["max_dd"] > -0.30].sort_values("cagr", ascending=False).head(3)

    (OUT / "blend_summary.json").write_text(json.dumps({
        "universe": "Baseline-Strategie + SPY-Buy&Hold",
        "period": f"{common.min().date()} -> {common.max().date()}",
        "best_per_metric": best,
        "low_dd_top3": cons.to_dict(orient="records"),
        "top_sharpe_top5": mod.to_dict(orient="records"),
        "high_cagr_top3": agg.to_dict(orient="records"),
    }, indent=2, default=str))

    print("\n[+] Best per Metric:")
    for k, v in best.items():
        print(f"    {k:20s}  {v['w_strat']:.0%}/{v['w_spy']:.0%} ({v['rebal']}) -> "
              f"CAGR={v['cagr']:.2%}  Sharpe={v['sharpe']}  MaxDD={v['max_dd']:.2%}")

    print("\n[+] low_dd_top3 (niedrigster DD bei CAGR>5%):")
    print(cons[["w_strat","w_spy","rebal","cagr","sharpe","max_dd","calmar"]].to_string(index=False))
    print("\n[+] top_sharpe_top5 (hoechster Sharpe):")
    print(mod[["w_strat","w_spy","rebal","cagr","sharpe","max_dd","calmar"]].to_string(index=False))
    print("\n[+] high_cagr_top3 (hoechster CAGR bei MaxDD>-30%):")
    print(agg[["w_strat","w_spy","rebal","cagr","sharpe","max_dd","calmar"]].to_string(index=False))


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
