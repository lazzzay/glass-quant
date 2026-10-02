#!/usr/bin/env python3
"""
Phase B Step 2: Bootstrap Monte-Carlo + Regime-Split

Liest backtest/results_v1/equity_daily.parquet + trades.parquet
und liefert:
  1. Bootstrap (N=1000) Konfidenzintervalle fuer CAGR / Sharpe / MaxDD / Calmar
  2. Jahres-Aufteilung: Performance pro Jahr + Regime-Label (Bull/Bear/Sideways)
  3. Zwei Plots: Equity-Curve + Bootstrap-CI-Fan-Chart
  4. regime_attribution.json + yearly_performance.parquet
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
RES = ROOT / "results_v1"
LAKE = ROOT.parent / "data_lake"

N_BOOTSTRAP = 1000
RNG = np.random.default_rng(42)


def compute_metrics(returns: np.ndarray, starting: float = 100_000.0) -> dict:
    if len(returns) < 10:
        return {"cagr": np.nan, "sharpe": np.nan, "max_dd": np.nan, "calmar": np.nan}
    eq = starting * np.cumprod(1 + returns)
    n_years = len(returns) / 252
    cagr = (eq[-1] / starting) ** (1 / n_years) - 1
    sharpe = returns.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else 0
    dd = eq / np.maximum.accumulate(eq) - 1
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan
    return {"cagr": cagr, "sharpe": sharpe, "max_dd": max_dd, "calmar": calmar}


def bootstrap_ci(returns: np.ndarray, n_boot: int = N_BOOTSTRAP) -> dict:
    metrics = {"cagr": [], "sharpe": [], "max_dd": [], "calmar": []}
    n = len(returns)
    for _ in range(n_boot):
        idx = RNG.integers(0, n, size=n)
        m = compute_metrics(returns[idx])
        for k, v in m.items():
            metrics[k].append(v)
    out = {}
    for k, vals in metrics.items():
        arr = np.array(vals, dtype=float)
        arr = arr[~np.isnan(arr)]
        if len(arr) == 0:
            out[k] = {"mean": None, "ci95_lo": None, "ci95_hi": None}
        else:
            out[k] = {
                "mean": round(float(arr.mean()), 4),
                "median": round(float(np.median(arr)), 4),
                "ci95_lo": round(float(np.percentile(arr, 2.5)), 4),
                "ci95_hi": round(float(np.percentile(arr, 97.5)), 4),
            }
    return out


def classify_regime(spx_ret_series: pd.Series, window: int = 252) -> str:
    """Grober Regime-Label pro Jahr:
       Bull  = Jahr-Return > +10%
       Bear  = Jahr-Return < -10%
       Sideways sonst."""
    yr_ret = (1 + spx_ret_series).prod() - 1
    if yr_ret > 0.10: return "BULL"
    if yr_ret < -0.10: return "BEAR"
    return "SIDEWAYS"


def main():
    eq = pd.read_parquet(RES / "equity_daily.parquet")
    tr = pd.read_parquet(RES / "trades.parquet")
    eq["date"] = pd.to_datetime(eq["date"])
    eq = eq.sort_values("date").reset_index(drop=True)
    eq["ret"] = eq["equity"].pct_change()
    returns = eq["ret"].dropna().values

    print(f"[+] Bootstrap N={N_BOOTSTRAP} auf {len(returns)} Tages-Returns ...")
    boot = bootstrap_ci(returns)
    for k, v in boot.items():
        print(f"    {k:8s}  mean={v['mean']}  CI95=[{v['ci95_lo']}, {v['ci95_hi']}]")

    # SPX-Benchmark laden
    spx_path = LAKE / "prices" / "daily" / "SPY.parquet"
    spx = pd.read_parquet(spx_path)
    spx["date"] = pd.to_datetime(spx["date"])
    spx = spx.set_index("date").sort_index()
    spx["ret"] = spx["adj_close"].pct_change()

    # Jahres-Performance + Regime-Label
    eq_y = eq.set_index("date")
    years = eq_y.index.year.unique()
    yearly = []
    for y in years:
        y_eq = eq_y[eq_y.index.year == y]
        y_spx = spx[spx.index.year == y]
        if len(y_eq) < 20:
            continue
        strat_ret = (1 + y_eq["ret"]).prod() - 1
        spx_ret = (1 + y_spx["ret"]).prod() - 1
        regime = classify_regime(y_spx["ret"])
        yearly.append({
            "year": int(y),
            "strat_return": round(float(strat_ret), 4),
            "spx_return": round(float(spx_ret), 4),
            "outperf": round(float(strat_ret - spx_ret), 4),
            "regime": regime,
        })
    yp = pd.DataFrame(yearly)
    yp.to_parquet(RES / "yearly_performance.parquet", index=False)

    # Regime-Attribution
    reg_agg = yp.groupby("regime").agg(
        n_years=("year", "count"),
        avg_strat=("strat_return", "mean"),
        avg_spx=("spx_return", "mean"),
        avg_outperf=("outperf", "mean"),
    ).round(4).reset_index()
    reg_agg.to_parquet(RES / "regime_attribution.parquet", index=False)

    # Kombi-Report
    report = {
        "run_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        "n_bootstrap": N_BOOTSTRAP,
        "bootstrap_ci": boot,
        "yearly_performance": yp.to_dict(orient="records"),
        "regime_attribution": reg_agg.to_dict(orient="records"),
    }
    (RES / "bootstrap_regime_report.json").write_text(json.dumps(report, indent=2, default=str))

    # === Plots ===
    fig, axes = plt.subplots(2, 1, figsize=(11, 9))

    # Panel 1: Equity-Curve vs SPY
    ax = axes[0]
    ax.plot(eq["date"], eq["equity"], label="V6-Baseline", color="#2E75B6", lw=1.3)
    # SPY normalisiert
    spx_cut = spx.loc[eq["date"].iloc[0]:eq["date"].iloc[-1]].copy()
    spx_cut["equity_norm"] = (spx_cut["adj_close"] / spx_cut["adj_close"].iloc[0]) * 100_000
    ax.plot(spx_cut.index, spx_cut["equity_norm"], label="SPY Buy&Hold", color="#C55A11", lw=1.0, alpha=0.75)
    ax.set_title("Phase B V1: Baseline-Strategie vs. SPY (1997-2026)")
    ax.set_ylabel("Equity (USD)"); ax.legend(); ax.grid(alpha=0.3)
    ax.set_yscale("log")

    # Panel 2: Drawdown
    ax = axes[1]
    eq["dd"] = eq["equity"] / eq["equity"].cummax() - 1
    ax.fill_between(eq["date"], eq["dd"], 0, color="#C00000", alpha=0.55)
    ax.set_title(f"Drawdown: Max: {eq['dd'].min():.2%}")
    ax.set_ylabel("Drawdown"); ax.grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig(RES / "equity_and_dd.png", dpi=110)
    plt.close()

    # Panel: Bootstrap-Histogramme
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    titles = {"cagr": "CAGR", "sharpe": "Sharpe", "max_dd": "Max Drawdown", "calmar": "Calmar"}
    for ax, (k, title) in zip(axes.flat, titles.items()):
        vals = []
        for _ in range(N_BOOTSTRAP):
            idx = RNG.integers(0, len(returns), size=len(returns))
            m = compute_metrics(returns[idx])
            if not np.isnan(m[k]): vals.append(m[k])
        ax.hist(vals, bins=50, color="#2E75B6", alpha=0.7, edgecolor="white")
        ax.axvline(np.percentile(vals, 2.5), color="red", ls="--", lw=1, label="95% CI")
        ax.axvline(np.percentile(vals, 97.5), color="red", ls="--", lw=1)
        ax.axvline(np.median(vals), color="black", ls="-", lw=1.2, label="Median")
        ax.set_title(f"{title}  (Bootstrap N={N_BOOTSTRAP})")
        ax.legend(fontsize=8); ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(RES / "bootstrap_histograms.png", dpi=110)
    plt.close()

    print(f"\n[+] Yearly Performance ({len(yp)} Jahre):")
    print(yp.to_string(index=False))
    print(f"\n[+] Regime-Attribution:")
    print(reg_agg.to_string(index=False))
    print(f"\n[+] Plots: equity_and_dd.png, bootstrap_histograms.png")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
