#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIM-1b: Block-Bootstrap-CIs auf Live-Trade-Journal (tools/trade_journal.sqlite)

Companion script to sim/bootstrap.py. Unterschied:

  * Quelle ist nicht der V1-Backtest-Parquet, sondern die Live-DB.
  * Standard-Cohort: source='backtest' AND exit_reason != 'skipped_tier_x'
    (die 258 echten V6-Trades, die 182 Tier-X-Skips ausgeklammert, weil
    deren return_pct fuer ausgefuehrte Trades irrefuehrend waere).
  * Nur Trade-Level-iid-Bootstrap: Daily-Path-Bootstrap faellt weg,
    weil die Backtest-Rows kein `exit_date` tragen; eine saubere
    Tages-Equity-Rekonstruktion ist damit nicht moeglich.
  * Output: sim/_reports/bootstrap_ci_live_YYYY-MM-DD.json
  * Audit-Gate 3 (Performance-vs-CI) liest genau diese Datei als
    V6-Baseline-CI; V6.1-Live-Trades werden gegen die Untergrenze gespiegelt.

CLI:
  python3 sim/bootstrap_live.py                           # Default: backtest-real
  python3 sim/bootstrap_live.py --source backtest         # alle backtest-Rows
  python3 sim/bootstrap_live.py --source systematic       # V6.1-Live + Legacy
  python3 sim/bootstrap_live.py --since 2023-01-01
  python3 sim/bootstrap_live.py --n-boot 5000

Exit-Codes:
  0: OK, Gates gruen
  1: WARN (WR-Untergrenze < 50% oder PF-Untergrenze < 1.1)
  2: FAIL (zu wenig Trades fuer CI, n < 30)
  3: DB nicht lesbar
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Same-package-Imports aus sim/bootstrap.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
sys.path.insert(0, str(ROOT / "tools"))

from bootstrap import (  # noqa: E402
    _trade_metrics,
    _quantiles,
    trade_bootstrap,
    N_BOOT,
    SEED,
    CI_LO_PCT,
    CI_HI_PCT,
)

REPORT_DIR = ROOT / "sim" / "_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Cohort-Definitionen: halten wir hier lokal, weil das Live-Audit
# andere Semantik braucht als die trade_journal-CLI (die --include-all
# standardmaessig systematic+live zeigt, waehrend wir hier die
# statistische Mehrheit brauchen).
COHORTS = {
    "backtest-real": (
        "source='backtest' AND exit_reason != 'skipped_tier_x' "
        "AND return_pct IS NOT NULL"
    ),
    "backtest": (
        "source='backtest' AND return_pct IS NOT NULL"
    ),
    "systematic": (
        "source IN ('systematic','live') AND return_pct IS NOT NULL"
    ),
    "external": (
        "source='external' AND return_pct IS NOT NULL"
    ),
    "all-closed": (
        "exit_date IS NOT NULL AND return_pct IS NOT NULL"
    ),
}


def load_trade_returns(cohort: str, since: str | None, until: str | None) -> tuple[np.ndarray, dict]:
    """Liest pro cohort die return_pct-Serie aus trade_journal.sqlite."""
    from trade_journal import connect

    where = COHORTS.get(cohort)
    if where is None:
        raise SystemExit(f"Unbekannte cohort '{cohort}'. Moeglich: {list(COHORTS)}")

    params: list = []
    # --since/--until als SQL-Parameter (keine String-Interpolation von CLI-Input)
    if since:
        where += " AND entry_date >= ?"
        params.append(since)
    if until:
        where += " AND entry_date <= ?"
        params.append(until)

    sql_ret = f"SELECT return_pct, is_win FROM trades WHERE {where}"
    sql_meta = (
        f"SELECT MIN(entry_date), MAX(entry_date), COUNT(*), "
        f"AVG(CAST(is_win AS REAL)) FROM trades WHERE {where}"
    )

    conn = connect()
    try:
        rows = conn.execute(sql_ret, params).fetchall()
        meta = conn.execute(sql_meta, params).fetchone()
    finally:
        conn.close()

    vals = np.array([r[0] for r in rows if r[0] is not None], dtype=float)
    is_win_vals = np.array([r[1] for r in rows if r[0] is not None], dtype=float)

    # WR-Definitions-Audit: Die Bootstrap-CI basiert auf return_pct>0
    # (via _trade_metrics in sim/bootstrap.py). Die is_win-Spalte kann
    # abweichen (cost-adjusted im Backtest-Writer). Beide Zahlen ins
    # Meta packen, damit das Audit-Gate 3 die Diskrepanz sieht.
    wr_return_gt0 = float((vals > 0).mean()) if vals.size else None
    wr_is_win = float(is_win_vals.mean()) if is_win_vals.size else None
    n_discrepant = int(((is_win_vals == 0) & (vals > 0)).sum())

    return vals, {
        "cohort":         cohort,
        "where_clause":   where,
        "date_start":     meta[0],
        "date_end":       meta[1],
        "n_trades":       int(meta[2]),
        "wr_point":       (float(meta[3]) if meta[3] is not None else None),
        "wr_defs": {
            "return_pct_gt_0":   wr_return_gt0,   # Basis der Bootstrap-CI
            "is_win_column":     wr_is_win,        # Backtest-Writer-Flag (cost-adjusted?)
            "n_is_win0_ret_gt0": n_discrepant,     # is_win=0 & return>0: cost-Threshold-Hinweis
            "note": ("CI-win_rate basiert auf return_pct>0. is_win-Spalte kann "
                     "cost-adjusted abweichen: fuer Gate 3 return_pct>0 nutzen."),
        },
    }


def gate(ci_trade: dict) -> dict:
    """Einfache Ampel fuer Audit-Gate 3.

    GREEN: WR-Untergrenze >= 50% und PF-Untergrenze >= 1.10
    YELLOW: eine der beiden darunter
    RED: beide darunter oder PF-Untergrenze < 1.00 (negative Expectancy)
    """
    wr_lo = ci_trade.get("win_rate", {}).get("ci95_lo")
    pf_lo = ci_trade.get("profit_factor", {}).get("ci95_lo")
    ex_lo = ci_trade.get("expectancy_pct", {}).get("ci95_lo")

    lvl = "GREEN"
    reasons = []
    if wr_lo is None or wr_lo < 0.50:
        lvl = "YELLOW"
        reasons.append(f"WR lo95={wr_lo}")
    if pf_lo is None or pf_lo < 1.10:
        lvl = "YELLOW" if lvl != "RED" else "RED"
        reasons.append(f"PF lo95={pf_lo}")
    if pf_lo is not None and pf_lo < 1.00:
        lvl = "RED"
    if ex_lo is not None and ex_lo <= 0:
        lvl = "RED"
        reasons.append(f"Expectancy lo95={ex_lo}")

    return {
        "level":   lvl,
        "reasons": reasons,
        "wr_lo95":  wr_lo,
        "pf_lo95":  pf_lo,
        "ex_lo95":  ex_lo,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="backtest-real",
                    choices=list(COHORTS.keys()),
                    help="Cohort-Filter (default backtest-real = 258 echte V6-Trades)")
    ap.add_argument("--since", default=None,
                    help="Entry-Date untere Grenze, YYYY-MM-DD")
    ap.add_argument("--until", default=None,
                    help="Entry-Date obere Grenze, YYYY-MM-DD")
    ap.add_argument("--n-boot", type=int, default=N_BOOT,
                    help=f"Bootstrap-Resamples (default {N_BOOT})")
    ap.add_argument("--min-n", type=int, default=30,
                    help="Mindest-n fuer CI-Berechnung; darunter FAIL (default 30)")
    ap.add_argument("--out", default=None,
                    help="Alternatives Output-File (Default sim/_reports/bootstrap_ci_live_YYYY-MM-DD.json)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    vals, meta = load_trade_returns(args.source, args.since, args.until)

    if vals.size < args.min_n:
        if not args.quiet:
            print(f"[SIM-1b] FAIL: nur {vals.size} Trades in cohort '{args.source}' "
                  f"(min={args.min_n}), kein CI-Run.", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"[SIM-1b] Cohort='{args.source}'  n={vals.size}  "
              f"range=[{meta['date_start']} .. {meta['date_end']}]")
        print(f"[SIM-1b] N_BOOT={args.n_boot:,d}, SEED={SEED}")

    point = _trade_metrics(vals)

    # Ueber trade_bootstrap mit einstellbarer n_boot-Param
    rng = np.random.default_rng(SEED + 1)
    ci_trade = trade_bootstrap(vals, n_boot=args.n_boot, rng=rng)

    today = datetime.now().strftime("%Y-%m-%d")
    out = {
        "schema_version": "sim.bootstrap_live.v1",
        "run_utc":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_date_local": today,
        "source":         meta,
        "config": {
            "n_bootstrap":    args.n_boot,
            "seed":           SEED,
            "ci_lo_pct":      CI_LO_PCT,
            "ci_hi_pct":      CI_HI_PCT,
        },
        "point_estimates": {
            k: (round(v, 6) if isinstance(v, float) and not np.isnan(v) else None)
            for k, v in point.items()
        },
        "bootstrap_ci": {
            "trade_iid_bootstrap": ci_trade,
        },
        "gate": gate(ci_trade),
    }

    out_path = Path(args.out) if args.out else REPORT_DIR / f"bootstrap_ci_live_{today}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    if not args.quiet:
        try:
            _shown = out_path.relative_to(ROOT)
        except ValueError:
            _shown = out_path
        print(f"[SIM-1b] Report: {_shown}")
        _pretty(out)

    lvl = out["gate"]["level"]
    # Exit-Codes: 0=GREEN, 1=YELLOW (CI-Grenze unterschritten, aber kein harter Fail),
    # 3=RED (Expectancy<=0 oder PF<1.00). n<min_n ist bereits oben mit return 2 abgedeckt.
    return {"GREEN": 0, "YELLOW": 1, "RED": 3}.get(lvl, 1)


def _pretty(out: dict) -> None:
    src = out["source"]
    ci = out["bootstrap_ci"]["trade_iid_bootstrap"]
    gt = out["gate"]
    wd = src.get("wr_defs", {}) or {}
    wr_ret = wd.get("return_pct_gt_0")
    wr_iw = wd.get("is_win_column")
    n_disc = wd.get("n_is_win0_ret_gt0", 0)
    print(f"\n  Cohort: {src['cohort']}  n={src['n_trades']}")
    if wr_ret is not None and wr_iw is not None:
        print(f"  WR-Defs: return>0={wr_ret*100:.1f}%  is_win={wr_iw*100:.1f}%  "
              f"discrepant={n_disc}  (CI basiert auf return>0)")
    def _fmt(k, unit="%", scale=1.0):
        q = ci.get(k, {})
        lo = q.get("ci95_lo")
        md = q.get("median")
        hi = q.get("ci95_hi")
        if lo is None:
            print(f"  {k:20s}: CI nicht verfuegbar")
            return
        print(f"  {k:20s}: CI95 [{lo*scale:+7.3f}{unit} .. {hi*scale:+7.3f}{unit}]  median={md*scale:+7.3f}{unit}")
    _fmt("win_rate",       unit="%", scale=100.0)
    _fmt("profit_factor",  unit="")
    _fmt("avg_win_pct",    unit="%")
    _fmt("avg_loss_pct",   unit="%")
    _fmt("expectancy_pct", unit="%")
    print(f"\n  Gate: {gt['level']}  reasons={gt['reasons'] or '[-]'}")


if __name__ == "__main__":
    sys.exit(main())
