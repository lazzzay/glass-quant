#!/usr/bin/env python3
"""
Scientific Addendum: schliesst die 2 grossen Methodik-Luecken des V1-Backtests
(methodology addendum):

  (G2) BENCHMARK: Buy-&-Hold SPY (Total Return via adj_close) ueber dasselbe
       Fenster -> Alpha (annualisiert), Beta, Korrelation, Information Ratio,
       Tracking Error. Bisher war alle Performance ABSOLUT, nicht als Excess.
  (G3) KOSTEN-/SLIPPAGE-SENSITIVITAET: Post-hoc Kosten-Overlay auf dem realisierten
       Trade-Ledger (round-trip bps zusaetzlich zur 1 EUR Kommission) -> wie weit
       traegt der Edge? Bisher: nur 1 EUR Kommission, 0 Slippage.

Methode bewusst transparent: liest die bereits erzeugten results_v1-Artefakte
(trades.parquet, equity_daily.parquet) + den Daten-Lake (SPY/^GSPC), rechnet
nichts neu im Strategie-Pfad nach -> reproduzierbar, ehrlich, erklaerbar.

Output: backtest/results_v1/scientific_addendum.json  (+ Konsolen-Report)
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data_lake"
RES = Path(__file__).resolve().parent / "results_v1"
TRADES = RES / "trades.parquet"
EQUITY = RES / "equity_daily.parquet"
SPY = LAKE / "prices" / "daily" / "SPY.parquet"
GSPC = LAKE / "prices" / "daily" / "IDX_GSPC.parquet"
SUMMARY = RES / "summary.json"

TDAYS = 252.0
RF_ANNUAL = 0.0  # konservativ: excess vs 0 (separat Sharpe-vs-RF moeglich)


def rd(path: Path) -> pd.DataFrame:
    return duckdb.sql(f"SELECT * FROM '{path.as_posix()}'").df()


def pick(cols, *cands):
    low = {c.lower(): c for c in cols}
    for c in cands:
        if c in low:
            return low[c]
    return None


def perf(equity: pd.Series, periods_per_year=TDAYS):
    """CAGR/Sharpe/Sortino/MaxDD/Calmar aus einer (datierten) Equity-Reihe."""
    equity = equity.dropna().astype(float)
    rets = equity.pct_change().dropna()
    n = len(equity)
    years = n / periods_per_year
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
    mu, sd = rets.mean(), rets.std(ddof=1)
    sharpe = (mu / sd) * np.sqrt(periods_per_year) if sd > 0 else np.nan
    downside = rets[rets < 0].std(ddof=1)
    sortino = (mu / downside) * np.sqrt(periods_per_year) if downside and downside > 0 else np.nan
    cummax = equity.cummax()
    maxdd = (equity / cummax - 1).min()
    calmar = cagr / abs(maxdd) if maxdd < 0 else np.nan
    return dict(cagr=cagr, sharpe=sharpe, sortino=sortino, max_dd=maxdd,
                calmar=calmar, ann_vol=sd * np.sqrt(periods_per_year),
                total_return=equity.iloc[-1] / equity.iloc[0] - 1, n_obs=n, years=years)


def datecol(df):
    c = pick(df.columns, "date", "dt", "day", "timestamp")
    if c is None:
        for col in df.columns:
            if np.issubdtype(df[col].dtype, np.datetime64):
                return col
    return c


def main():
    out = {"meta": {}, "strategy": {}, "benchmark_spy": {}, "benchmark_gspc": {},
           "relative": {}, "cost_sensitivity": [], "notes": []}

    # ---------- 0) Strategie-Equity laden + Kennzahlen reproduzieren ----------
    eq = rd(EQUITY)
    edc = datecol(eq)
    eqcol = pick(eq.columns, "equity", "equity_eur", "nav", "capital", "value")
    if eqcol is None:
        num = [c for c in eq.columns if c != edc and np.issubdtype(eq[c].dtype, np.number)]
        eqcol = num[0]
    eq[edc] = pd.to_datetime(eq[edc])
    eq = eq.sort_values(edc).set_index(edc)
    strat_equity = eq[eqcol].astype(float)
    out["meta"]["equity_cols"] = list(eq.columns)
    out["meta"]["window"] = [str(strat_equity.index[0].date()), str(strat_equity.index[-1].date())]
    out["strategy"] = perf(strat_equity)

    summ = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    out["meta"]["summary_json"] = summ

    # ---------- 1) SPY Buy-&-Hold (Total Return) als Benchmark ----------
    start, end = strat_equity.index[0], strat_equity.index[-1]

    def bench(path):
        df = rd(path)
        dc = datecol(df)
        px = pick(df.columns, "adj_close", "adjclose", "close")
        df[dc] = pd.to_datetime(df[dc])
        df = df.sort_values(dc).set_index(dc)
        s = df[px].astype(float)
        s = s[(s.index >= start) & (s.index <= end)].dropna()
        e0 = float(strat_equity.iloc[0])
        return (s / s.iloc[0]) * e0, px

    spy_eq, spy_px = bench(SPY)
    out["benchmark_spy"] = perf(spy_eq)
    out["benchmark_spy"]["price_col_used"] = spy_px
    try:
        gspc_eq, gspc_px = bench(GSPC)
        out["benchmark_gspc"] = perf(gspc_eq)
        out["benchmark_gspc"]["price_col_used"] = gspc_px
    except Exception as e:  # noqa
        out["benchmark_gspc"] = {"error": str(e)}

    # ---------- 2) Relativ: Alpha/Beta/IR/Korrelation vs SPY ----------
    sr = strat_equity.pct_change().rename("strat")
    br = spy_eq.pct_change().rename("spy")
    joined = pd.concat([sr, br], axis=1).dropna()
    x = joined["spy"].values
    y = joined["strat"].values
    beta, alpha_daily = np.polyfit(x, y, 1)
    yhat = beta * x + alpha_daily
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    corr = float(np.corrcoef(x, y)[0, 1])
    excess = joined["strat"] - joined["spy"]
    ir = (excess.mean() / excess.std(ddof=1)) * np.sqrt(TDAYS) if excess.std(ddof=1) > 0 else np.nan
    te = excess.std(ddof=1) * np.sqrt(TDAYS)
    out["relative"] = dict(
        beta=float(beta), alpha_annual=float((1 + alpha_daily) ** TDAYS - 1),
        alpha_daily=float(alpha_daily), r2=float(r2), correlation=corr,
        information_ratio=float(ir), tracking_error_annual=float(te),
        cagr_spread=out["strategy"]["cagr"] - out["benchmark_spy"]["cagr"],
        sharpe_spread=out["strategy"]["sharpe"] - out["benchmark_spy"]["sharpe"],
        time_in_market_proxy=float((sr.abs() > 1e-9).mean()),
    )

    # ---------- 3) Kosten-/Slippage-Sensitivitaet auf dem Trade-Ledger ----------
    # Schema: ticker, entry_date, entry_price, exit_date, exit_price, shares, pnl, pnl_pct, days_held, reason
    tr = rd(TRADES)
    out["meta"]["trade_cols"] = list(tr.columns)
    tr["exit_date"] = pd.to_datetime(tr["exit_date"])
    notional = (tr["shares"].astype(float).abs() * tr["entry_price"].astype(float).abs())
    pnl_pct = tr["pnl_pct"].astype(float)         # Rendite auf Notional je Trade
    pnl_eur = tr["pnl"].astype(float)
    total_notional = float(notional.sum())
    gross_pnl = float(pnl_eur.sum())

    out["meta"]["n_trades_ledger"] = int(len(tr))
    out["meta"]["total_notional_traded"] = total_notional
    out["meta"]["avg_notional_per_trade"] = float(notional.mean())

    # Break-even Round-Trip-Kosten: (a) gleichgewichtet pro Trade, (b) kapital-(EUR-)gewichtet
    be_equal = float(pnl_pct.mean() * 1e4)
    be_dollar = float(gross_pnl / total_notional * 1e4)
    avg_eq = float(strat_equity.mean())
    out["cost_breakeven"] = dict(
        gross_expectancy_per_trade_pct=float(pnl_pct.mean()),
        median_per_trade_pct=float(pnl_pct.median()),
        win_rate=float((pnl_pct > 0).mean()),
        breakeven_roundtrip_bps_equalweight=be_equal,
        breakeven_roundtrip_bps_dollarweight=be_dollar,
        gross_pnl_eur=gross_pnl, total_notional_traded=total_notional,
        annual_notional_turnover_x=float(total_notional / out["strategy"]["years"] / avg_eq),
    )

    # Equity zum jeweiligen Exit-Tag (asof/ffill) -> Kosten-Drag im RENDITERAUM (kompoundiert, physikalisch valide)
    exit_eq = strat_equity.reindex(tr["exit_date"].values, method="ffill").values
    exit_eq = np.where(np.isfinite(exit_eq) & (exit_eq > 0), exit_eq, avg_eq)
    gross_ret = strat_equity.pct_change().fillna(0.0)

    for rt_bps in [0, 5, 10, 15, 20, 25, 30, 40, 60, 100]:
        cost_eur = notional.values * (rt_bps / 1e4)          # round-trip auf Notional
        cost_frac = cost_eur / exit_eq                        # als Equity-Anteil am Exit-Tag
        drag = pd.Series(cost_frac, index=tr["exit_date"].values).groupby(level=0).sum()
        drag = drag.reindex(strat_equity.index).fillna(0.0)
        net_ret = gross_ret - drag
        net_equity = float(strat_equity.iloc[0]) * (1.0 + net_ret).cumprod()
        p = perf(net_equity)
        out["cost_sensitivity"].append(dict(
            round_trip_bps=rt_bps,
            total_extra_cost_eur=float(cost_eur.sum()),
            end_equity=float(net_equity.iloc[-1]),
            cagr=p["cagr"], sharpe=p["sharpe"], sortino=p["sortino"],
            max_dd=p["max_dd"], calmar=p["calmar"],
            cagr_vs_spy=p["cagr"] - out["benchmark_spy"]["cagr"],
            beats_spy_riskadj=bool(p["sharpe"] > out["benchmark_spy"]["sharpe"]),
        ))

    out["notes"] = [
        "Benchmark SPY nutzt adj_close (Total Return, inkl. Dividenden). ^GSPC ist Preis-Index (ohne Div).",
        "Kosten-Overlay ist first-order: Zusatzkosten werden am Exit-Tag von der Equity abgezogen; "
        "Compounding-Rueckkopplung (kleinere Folgepositionen) ist NICHT modelliert -> minimal konservativ in EUR.",
        "round_trip_bps = Slippage+Spread zusaetzlich zur bereits enthaltenen 1 EUR/Trade Kommission.",
        "Information Ratio/Alpha vs SPY-Tagesrenditen; Strategie ist meist nur teil-investiert "
        "(time_in_market_proxy), daher niedriges Beta erwartbar (Krisen-Hedge-Profil).",
        "Bekannte Rest-Verzerrung (separat): V1-Engine fuellt same-day-close statt next-open (im Code dokumentiert).",
    ]

    (RES / "scientific_addendum.json").write_text(json.dumps(out, indent=2, default=float))

    # ---------- Konsolen-Report ----------
    s, b = out["strategy"], out["benchmark_spy"]
    print("\n" + "=" * 64)
    print("SCIENTIFIC ADDENDUM:", *out["meta"]["window"])
    print("=" * 64)
    print(f"{'Kennzahl':<16}{'Strategie':>14}{'SPY B&H':>14}{'Spread':>12}")
    for k, lbl in [("cagr", "CAGR"), ("sharpe", "Sharpe"), ("sortino", "Sortino"),
                   ("max_dd", "MaxDD"), ("calmar", "Calmar"), ("ann_vol", "Vol p.a.")]:
        sp = s[k] - b[k]
        print(f"{lbl:<16}{s[k]:>14.4f}{b[k]:>14.4f}{sp:>12.4f}")
    r = out["relative"]
    print("-" * 64)
    print(f"Alpha p.a.: {r['alpha_annual']*100:6.2f}%   Beta: {r['beta']:.3f}   "
          f"Korr: {r['correlation']:.3f}   R2: {r['r2']:.3f}")
    print(f"Information Ratio: {r['information_ratio']:.3f}   "
          f"Tracking Error p.a.: {r['tracking_error_annual']*100:.2f}%")
    be = out["cost_breakeven"]
    print("-" * 64)
    print(f"Brutto-Erwartungswert/Trade: {be['gross_expectancy_per_trade_pct']*100:.3f}%  "
          f"(Median {be['median_per_trade_pct']*100:.3f}%, WR {be['win_rate']*100:.1f}%)")
    print(f"Break-even Round-Trip: {be['breakeven_roundtrip_bps_equalweight']:.1f} bps (pro Trade) / "
          f"{be['breakeven_roundtrip_bps_dollarweight']:.1f} bps (kapitalgewichtet)")
    print(f"Jaehrl. Notional-Umschlag: {be['annual_notional_turnover_x']:.1f}x der Equity")
    print("-" * 64)
    print("KOSTEN-SENSITIVITAET (round-trip bps zusaetzlich zu 1 EUR Kommission; kompoundiert)")
    print(f"{'bps':>5}{'CAGR':>10}{'Sharpe':>10}{'Calmar':>10}{'vs SPY CAGR':>14}{'>SPY risk-adj':>14}")
    for c in out["cost_sensitivity"]:
        print(f"{c['round_trip_bps']:>5}{c['cagr']*100:>9.2f}%{c['sharpe']:>10.3f}"
              f"{c['calmar']:>10.3f}{c['cagr_vs_spy']*100:>13.2f}%{str(c['beats_spy_riskadj']):>14}")
    print("=" * 64)
    print("geschrieben ->", (RES / "scientific_addendum.json").as_posix())


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
