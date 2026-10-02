#!/usr/bin/env python3
"""
Hebel D: Cohort-Kelly per Setup × Regime (REC-RECOVERY-1 Mega-Audit-Befund).

Berechnet Kelly-Fraction pro setup_fk × regime_at_entry-Cohort aus trade_journal.
Hauptzweck: Cohorts mit negativer Kelly-Edge (z. B. ein Setup in einem ungeeigneten
Regime) erkennen und als SKIP markieren.

Output: tools/cohort_kelly_YYYY-MM-DD.json
Format:
{
  "cohorts": {
    "<setup>|<regime>": {"n": 120, "wr": 0.55, "avg_win_eur": 10.0, "avg_loss_eur": -8.0, "kelly_full": 0.19, "kelly_half": 0.095, "verdict": "TRADE"},
    "<setup>|<regime2>": {"n": 40, "wr": 0.40, ..., "kelly_full": -0.08, "kelly_half": 0.0, "verdict": "SKIP"},
    ...
  },
  "fallback_half_kelly": 0.16,  // global default
  "min_cohort_n": 30
}

CLI:
    python3 tools/cohort_kelly.py              # full SQL run
    python3 tools/cohort_kelly.py --setup mr_v61 --regime Trending  # lookup
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "trade_journal.sqlite"
TOOLS_DIR = REPO_ROOT / "tools"

# Fallback cohort data used when the journal DB is missing or the SQL query fails.
# Intentionally EMPTY: without real data every lookup falls back to
# FALLBACK_HALF_KELLY (verdict NO_DATA). Shape, for illustration only (example
# numbers, not real results):
#   FALLBACK_COHORTS = {
#       "mr_v61|Trending": {"n": 100, "wins": 55, "avg_win_eur": 10.0, "avg_loss_eur": -8.0},
#   }
FALLBACK_COHORTS: dict = {}

MIN_COHORT_N = 30  # statistische Signifikanz-Schwelle
FALLBACK_HALF_KELLY = 0.16  # globale Default-Sizing


def kelly_fraction(wr: float, avg_win: float, avg_loss: float) -> float:
    """Kelly-Formel: f = (p × b − q) / b, wobei b = avg_win / |avg_loss|.
    Returnt 0 wenn Kelly negativ (= setup verlierst). Cap auf 0.5 (Half-Kelly-Max).
    """
    if not avg_win or not avg_loss or avg_win <= 0 or avg_loss >= 0:
        return 0.0
    b = avg_win / abs(avg_loss)
    q = 1.0 - wr
    if b <= 0:
        return 0.0
    f_full = (wr * b - q) / b
    return f_full


def cohort_to_kelly_dict(setup: str, regime: str, n: int, wins: int,
                         avg_win_eur: float, avg_loss_eur: float) -> dict:
    """Wandelt Cohort-Raw-Daten in Kelly-Verdict um."""
    wr = wins / n if n > 0 else 0.0
    k_full = kelly_fraction(wr, avg_win_eur or 0, avg_loss_eur or 0)
    k_half = max(0.0, min(0.5, k_full * 0.5))  # Half-Kelly mit 0.5-Cap

    if n < MIN_COHORT_N:
        verdict = "FALLBACK_HALF_KELLY"  # zu wenig Daten, Default 0.16
        k_half_apply = FALLBACK_HALF_KELLY
    elif k_full <= 0:
        verdict = "SKIP"  # negative Edge, kein Trade
        k_half_apply = 0.0
    elif k_full < 0.05:
        verdict = "MINIMAL"  # tiny edge
        k_half_apply = k_half
    else:
        verdict = "TRADE"
        k_half_apply = k_half

    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "wr": round(wr, 3),
        "avg_win_eur": round(avg_win_eur or 0, 2),
        "avg_loss_eur": round(avg_loss_eur or 0, 2),
        "kelly_full": round(k_full, 3),
        "kelly_half": round(k_half, 3),
        "kelly_half_apply": round(k_half_apply, 3),
        "verdict": verdict,
    }


def fetch_cohorts_from_db() -> dict:
    """SQL-Query auf trade_journal. Fallback bei DB-Errors."""
    if not DB_PATH.exists():
        print(f"[cohort_kelly] DB nicht gefunden: {DB_PATH}", file=sys.stderr)
        return _use_fallback()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute("""
            SELECT setup_fk, regime_at_entry,
                   COUNT(*) as n,
                   SUM(CASE WHEN brutto_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   AVG(CASE WHEN brutto_pnl > 0 THEN brutto_pnl END) as avg_win,
                   AVG(CASE WHEN brutto_pnl < 0 THEN brutto_pnl END) as avg_loss
            FROM trades
            WHERE source IN ('backtest','live','systematic')
              AND brutto_pnl IS NOT NULL
              AND setup_fk IS NOT NULL
              AND regime_at_entry IS NOT NULL
            GROUP BY setup_fk, regime_at_entry
            HAVING n >= 3
            ORDER BY n DESC
        """)
        cohorts = {}
        for row in c.fetchall():
            setup, regime, n, wins, avg_win, avg_loss = row
            key = f"{setup}|{regime}"
            cohorts[key] = cohort_to_kelly_dict(setup, regime, n, wins, avg_win, avg_loss)
        conn.close()
        return cohorts
    except sqlite3.OperationalError as e:
        print(f"[cohort_kelly] DB-Error: {e} → Fallback-Daten", file=sys.stderr)
        return _use_fallback()


def _use_fallback() -> dict:
    cohorts = {}
    for key, raw in FALLBACK_COHORTS.items():
        setup, regime = key.split("|")
        cohorts[key] = cohort_to_kelly_dict(
            setup, regime, raw["n"], raw["wins"],
            raw["avg_win_eur"], raw["avg_loss_eur"],
        )
    return cohorts


def lookup_kelly(setup: str, regime: str, cohorts: dict | None = None) -> dict:
    """Lookup Cohort-Kelly für gegebenes setup×regime."""
    if cohorts is None:
        cohorts = fetch_cohorts_from_db()
    key = f"{setup}|{regime}"
    if key in cohorts:
        return cohorts[key]
    # Fallback: nur setup ohne regime (alle regimes gemittelt)
    setup_only = [v for k, v in cohorts.items() if k.startswith(setup + "|")]
    if setup_only:
        # gewichteter Durchschnitt der kelly_half
        total_n = sum(c["n"] for c in setup_only)
        weighted_k = sum(c["kelly_half"] * c["n"] for c in setup_only) / total_n if total_n > 0 else 0
        return {
            "n": total_n,
            "wr": sum(c["wr"] * c["n"] for c in setup_only) / total_n if total_n > 0 else 0,
            "kelly_half_apply": round(weighted_k, 3),
            "verdict": "WEIGHTED_AVG_FALLBACK",
            "note": f"Kein exaktes match für {key}, gewichteter Avg über {len(setup_only)} setup-cohorts",
        }
    return {
        "verdict": "NO_DATA",
        "kelly_half_apply": FALLBACK_HALF_KELLY,
        "note": f"Keine Cohort-Daten für {key} oder setup {setup}, Default-Half-Kelly",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", default=None, help="Setup-FK lookup (z.B. mr_v61)")
    ap.add_argument("--regime", default=None, help="Regime lookup (z.B. Trending)")
    ap.add_argument("--date", default=None, help="Override Datum (YYYY-MM-DD)")
    args = ap.parse_args()

    cohorts = fetch_cohorts_from_db()

    # Lookup-Modus
    if args.setup and args.regime:
        result = lookup_kelly(args.setup, args.regime, cohorts)
        print(json.dumps(result, indent=2))
        return

    # Full-Output-Modus
    scan_date = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    out = {
        "date": scan_date,
        "version": "cohort-kelly-v1",
        "min_cohort_n": MIN_COHORT_N,
        "fallback_half_kelly": FALLBACK_HALF_KELLY,
        "n_cohorts": len(cohorts),
        "cohorts": cohorts,
    }
    out_path = TOOLS_DIR / f"cohort_kelly_{scan_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"[cohort_kelly] saved: {out_path}", file=sys.stderr)
    print()
    print(f"{'Cohort':<35} {'N':<5} {'WR':<7} {'AvgW':<7} {'AvgL':<7} {'KellyH':<8} {'Verdict':<22}")
    print("-" * 100)
    for key in sorted(cohorts.keys(), key=lambda k: -cohorts[k]["n"]):
        c = cohorts[key]
        print(f"{key:<35} {c['n']:<5} {c['wr']*100:<6.1f}% {c.get('avg_win_eur',0):<7.2f} {c.get('avg_loss_eur',0):<7.2f} {c.get('kelly_half_apply',0):<8.3f} {c.get('verdict',''):<22}")


if __name__ == "__main__":
    main()
