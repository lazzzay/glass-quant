#!/usr/bin/env python3
"""
V6.5 Journal-Metrics: Sharpe / Sortino / Max-DD / Calmar / Profit-Factor / R-Multiple

Reads trade_journal.sqlite (managed by trade_journal.py) and computes risk-adjusted
KPIs over a configurable window and source (live | backtest | both).

Usage:
    python3 journal_metrics.py                        # live + backtest, all-time
    python3 journal_metrics.py --source live          # nur Live-Trades
    python3 journal_metrics.py --from 2026-01-01      # seit Jahresanfang
    python3 journal_metrics.py --out metrics          # zusätzlicher Markdown-Report
    python3 journal_metrics.py --cohorts              # Breakdown nach regime/tier/exit_reason/gics_sector

Metriken:
    - Trade-Count, Win-Rate, Avg-PnL, Avg-Win, Avg-Loss, Payoff-Ratio
    - Profit-Factor = SUM(wins) / abs(SUM(losses))
    - Total-PnL, Total-Return-% (on accumulated equity)
    - Sharpe (annualisiert): per-trade returns, risk-free = 0
    - Sortino (annualisiert): downside-deviation only
    - Max-Drawdown (% + absolut), Max-DD-Dauer in Trades
    - Calmar = annualized return / max drawdown
    - Recovery-Factor = total_pnl / max_dd_abs
    - R-Multiple-Distribution (wenn r_multiple verfügbar)
    - Per-Cohort-Breakdown (optional via --cohorts)

Hinweis: "Sharpe per-trade" skaliert mit sqrt(252 / avg_days_per_trade) grob auf Jahresbasis.
Das ist eine Faustformel, keine exakte Annualization. Für eine exakte Annualization
bräuchte es daily_equity-Snapshots → erfolgt wenn genug Live-Tage da sind.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "trade_journal.sqlite"


def fetch_trades(conn, source: str | None, date_from: str | None, date_to: str | None):
    sql = """
        SELECT id, ticker, tier, sector, gics_sector, sector_rs_score, sector_regime_tag,
               regime, entry_date, entry_price, exit_date, exit_price, holding_days,
               position_size, brutto_pnl, netto_pnl, return_pct, exit_reason,
               is_win, r_multiple, connors_quality, cum_rsi2_3d, pullback_strength_3d,
               source, system_version
        FROM trades
        WHERE netto_pnl IS NOT NULL
    """
    params = []
    if source and source != "both":
        sql += " AND source = ?"
        params.append(source)
    if date_from:
        sql += " AND entry_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND entry_date <= ?"
        params.append(date_to)
    sql += " ORDER BY entry_date ASC, id ASC"

    rows = conn.execute(sql, params).fetchall()
    cols = [d[0] for d in conn.execute(sql, params).description]
    return [dict(zip(cols, r)) for r in rows]


def compute_core_metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trade_count": 0}

    # Win-Definition: netto_pnl > 0 (konsistent, unabhängig von is_win aus Legacy-CSV).
    pnls = [t["netto_pnl"] for t in trades if t["netto_pnl"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    returns_pct = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    holding_days = [t["holding_days"] for t in trades if t["holding_days"]]

    total_pnl = sum(pnls)
    total_wins = sum(wins) if wins else 0
    total_losses = sum(losses) if losses else 0

    win_rate = len(wins) / n if n else 0
    avg_pnl = total_pnl / n if n else 0
    avg_win = (total_wins / len(wins)) if wins else 0
    avg_loss = (total_losses / len(losses)) if losses else 0
    payoff = (avg_win / abs(avg_loss)) if avg_loss else None
    profit_factor = (total_wins / abs(total_losses)) if losses and total_losses != 0 else None
    avg_days = (sum(holding_days) / len(holding_days)) if holding_days else None

    # Sharpe + Sortino per-trade, dann auf Jahr hochskalieren
    sharpe = sortino = None
    if len(returns_pct) >= 5:
        mean_r = sum(returns_pct) / len(returns_pct)
        var = sum((r - mean_r) ** 2 for r in returns_pct) / len(returns_pct)
        sd = math.sqrt(var)
        down = [r for r in returns_pct if r < 0]
        down_var = sum(r ** 2 for r in down) / len(returns_pct) if down else 0
        down_sd = math.sqrt(down_var)
        trades_per_year = 252 / avg_days if avg_days and avg_days > 0 else 252
        if sd > 0:
            sharpe = (mean_r / sd) * math.sqrt(trades_per_year)
        if down_sd > 0:
            sortino = (mean_r / down_sd) * math.sqrt(trades_per_year)

    # Equity-Curve aus Trade-PnL (nicht aus daily_equity: das kommt später)
    equity = []
    cum = 0.0
    for p in pnls:
        cum += p
        equity.append(cum)
    peak = -math.inf
    max_dd_abs = 0
    max_dd_duration = 0
    cur_dd_start_idx = None
    current_dd = 0
    for i, e in enumerate(equity):
        if e > peak:
            peak = e
            cur_dd_start_idx = None
        else:
            dd = peak - e
            if dd > max_dd_abs:
                max_dd_abs = dd
            if cur_dd_start_idx is None:
                cur_dd_start_idx = i
            max_dd_duration = max(max_dd_duration, i - cur_dd_start_idx + 1)
    max_dd_pct = (max_dd_abs / peak * 100) if peak > 0 else None

    # Calmar: annual return / max-DD%
    calmar = None
    if max_dd_pct and max_dd_pct > 0 and avg_days:
        years = (n * avg_days) / 252
        if years > 0 and peak > 0:
            total_return_pct = (equity[-1] / 1000) * 100 if equity else 0
            annualized = total_return_pct / years
            calmar = annualized / max_dd_pct

    recovery_factor = (total_pnl / max_dd_abs) if max_dd_abs > 0 else None

    # R-Multiple Verteilung
    r_mults = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    r_summary = None
    if r_mults:
        r_summary = {
            "count": len(r_mults),
            "avg_r": round(sum(r_mults) / len(r_mults), 3),
            "p50": round(sorted(r_mults)[len(r_mults) // 2], 3),
            "p25": round(sorted(r_mults)[max(0, len(r_mults) // 4)], 3),
            "p75": round(sorted(r_mults)[min(len(r_mults) - 1, 3 * len(r_mults) // 4)], 3),
            "r_gt_1": sum(1 for r in r_mults if r > 1),
            "r_lt_neg1": sum(1 for r in r_mults if r < -1),
        }

    return {
        "trade_count": n,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate * 100, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(payoff, 3) if payoff else None,
        "profit_factor": round(profit_factor, 3) if profit_factor else None,
        "avg_holding_days": round(avg_days, 2) if avg_days else None,
        "sharpe_ann": round(sharpe, 3) if sharpe is not None else None,
        "sortino_ann": round(sortino, 3) if sortino is not None else None,
        "max_dd_abs": round(max_dd_abs, 2),
        "max_dd_pct": round(max_dd_pct, 2) if max_dd_pct is not None else None,
        "max_dd_duration_trades": max_dd_duration,
        "calmar": round(calmar, 3) if calmar else None,
        "recovery_factor": round(recovery_factor, 3) if recovery_factor else None,
        "r_multiple": r_summary,
    }


def compute_cohort(trades: list[dict], key: str) -> list[dict]:
    buckets = {}
    for t in trades:
        k = t.get(key) or "n/a"
        buckets.setdefault(k, []).append(t)
    out = []
    for k, subset in sorted(buckets.items()):
        m = compute_core_metrics(subset)
        m[key] = k
        out.append(m)
    # Sort: sinnvolle default-Sortierung
    if key in ("exit_reason", "regime", "tier", "gics_sector", "sector_regime_tag"):
        out.sort(key=lambda r: -(r.get("total_pnl") or 0))
    return out


def build_markdown(summary: dict, cohorts: dict | None, args) -> str:
    lines = []
    lines.append(f"# Trade-Journal Metrics Report: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"**Source:** {args.source}  |  **Range:** {args.date_from or 'all'} → {args.date_to or 'today'}")
    lines.append("")
    lines.append("## Core-Metriken")
    lines.append("")
    c = summary
    if c["trade_count"] == 0:
        lines.append("_Keine Trades im gewählten Zeitraum/Source._")
        return "\n".join(lines)

    lines.append(f"- Trade-Count: **{c['trade_count']}** ({c['win_count']}W / {c['loss_count']}L)")
    lines.append(f"- Win-Rate: **{c['win_rate']} %**")
    lines.append(f"- Total-PnL: **{c['total_pnl']:+.2f} €**")
    lines.append(f"- Avg-PnL: {c['avg_pnl']:+.2f} € | Avg-Win: {c['avg_win']:+.2f} € | Avg-Loss: {c['avg_loss']:+.2f} €")
    lines.append(f"- Payoff-Ratio: {c['payoff_ratio']} | Profit-Factor: **{c['profit_factor']}**")
    if c.get("avg_holding_days"):
        lines.append(f"- Avg-Holding: {c['avg_holding_days']} Tage")
    lines.append("")
    lines.append("## Risiko-Metriken")
    lines.append("")
    lines.append(f"- Sharpe (ann): **{c['sharpe_ann']}**")
    lines.append(f"- Sortino (ann): **{c['sortino_ann']}**")
    lines.append(f"- Max-Drawdown: **{c['max_dd_abs']:.2f} €** ({c['max_dd_pct']} %)")
    lines.append(f"- Max-DD-Dauer: {c['max_dd_duration_trades']} Trades")
    lines.append(f"- Calmar: {c['calmar']}")
    lines.append(f"- Recovery-Factor: {c['recovery_factor']}")
    if c.get("r_multiple"):
        r = c["r_multiple"]
        lines.append("")
        lines.append("## R-Multiple-Verteilung")
        lines.append("")
        lines.append(f"- Count: {r['count']} | Avg-R: **{r['avg_r']}**")
        lines.append(f"- Quartile: P25={r['p25']}, P50={r['p50']}, P75={r['p75']}")
        lines.append(f"- Big Wins (R > 1): {r['r_gt_1']} | Big Losses (R < -1): {r['r_lt_neg1']}")

    if cohorts:
        for key, rows in cohorts.items():
            lines.append("")
            lines.append(f"## Cohort: {key}")
            lines.append("")
            lines.append("| Cohort | N | WR | Total-PnL | Avg | PF | Sharpe |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in rows[:15]:
                lines.append(f"| {r.get(key)} | {r['trade_count']} | {r['win_rate']}% | "
                             f"{r['total_pnl']:+.2f} € | {r['avg_pnl']:+.2f} € | "
                             f"{r.get('profit_factor') or '-'} | {r.get('sharpe_ann') or '-'} |")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--source", default="both", choices=["live", "backtest", "both"])
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--to", dest="date_to", default=None)
    ap.add_argument("--cohorts", action="store_true", help="Breakdown nach exit_reason/regime/tier/gics_sector/sector_regime_tag")
    ap.add_argument("--out", default=None, help="Markdown-Output-Prefix (optional)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"DB nicht gefunden: {args.db}", file=sys.stderr)
        print("Initialisiere zuerst: python3 trade_journal.py init", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    trades = fetch_trades(conn, args.source, args.date_from, args.date_to)

    summary = compute_core_metrics(trades)

    cohorts = None
    if args.cohorts and summary["trade_count"] > 0:
        cohorts = {
            "exit_reason": compute_cohort(trades, "exit_reason"),
            "regime": compute_cohort(trades, "regime"),
            "tier": compute_cohort(trades, "tier"),
            "gics_sector": compute_cohort(trades, "gics_sector"),
            "sector_regime_tag": compute_cohort(trades, "sector_regime_tag"),
        }

    # Console
    print(json.dumps({"summary": summary, "cohorts_available": bool(cohorts)}, indent=2, default=str))

    # Optional Markdown
    if args.out:
        md = build_markdown(summary, cohorts, args)
        today = datetime.now().strftime("%Y-%m-%d")
        parent = Path(__file__).resolve().parent.parent
        md_path = parent / f"{args.out}_{today}.md"
        json_path = parent / f"{args.out}_{today}.json"
        with open(md_path, "w") as f:
            f.write(md)
        with open(json_path, "w") as f:
            json.dump({"summary": summary, "cohorts": cohorts}, f, indent=2, default=str)
        print(f"\nSaved Markdown: {md_path}", file=sys.stderr)
        print(f"Saved JSON:     {json_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
