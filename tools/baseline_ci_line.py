#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baseline-CI-Snippet: liest das neueste sim/_reports/bootstrap_ci_live_*.json
und gibt eine formatierte Zusammenfassung aus fuer:

  (a) Copy-Paste ins Morgen-Briefing (Markdown-Zeile)
  (b) JSON fuer das Dashboard-Widget `/baseline_ci` (DASH-2)

CLI:
    python3 tools/baseline_ci_line.py                   # Markdown
    python3 tools/baseline_ci_line.py --format json     # JSON
    python3 tools/baseline_ci_line.py --format short    # Einzeiler

Exit 0 bei Erfolg, 2 wenn kein Report gefunden.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "sim" / "_reports"


def load_latest_live_ci() -> tuple[Path | None, dict | None]:
    """Returns (path, parsed_json) oder (None, None) wenn nix vorhanden."""
    files = sorted(REPORTS.glob("bootstrap_ci_live_*.json"))
    if not files:
        return None, None
    latest = files[-1]
    try:
        return latest, json.loads(latest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return latest, None


def _extract_ci(doc: dict) -> dict:
    """Extrahiert WR/PF/Expectancy Lo/Hi/Median aus dem CI-Report."""
    ci = (doc.get("bootstrap_ci") or {}).get("trade_iid_bootstrap") or {}
    src = doc.get("source") or {}

    def _triplet(key):
        q = ci.get(key, {}) or {}
        return {
            "lo":     q.get("ci95_lo"),
            "hi":     q.get("ci95_hi"),
            "median": q.get("median"),
        }

    return {
        "cohort":        src.get("cohort"),
        "n_trades":      src.get("n_trades"),
        "date_range":    [src.get("date_start"), src.get("date_end")],
        "run_date":      doc.get("run_date_local"),
        "win_rate":      _triplet("win_rate"),
        "profit_factor": _triplet("profit_factor"),
        "expectancy":    _triplet("expectancy_pct"),
        "gate":          doc.get("gate", {}).get("level"),
        "wr_defs":       src.get("wr_defs"),
    }


def format_markdown(snap: dict) -> str:
    """Markdown-Block fuers Briefing (mehrzeilig, lesbar).

    Wichtig: win_rate ist eine Rate (0.69 = 69%), also * 100.
             expectancy_pct ist bereits in Prozent-Einheit (1.0 = 1.0%), also kein * 100.
    """
    wr = snap["win_rate"]
    pf = snap["profit_factor"]
    ex = snap["expectancy"]

    def _rate_pct(v):
        """Fuer Rates im [0,1]-Intervall (Win-Rate) -> anzeigen als %."""
        return f"{v*100:+.1f}%" if v is not None else "n/a"

    def _already_pct(v):
        """Fuer Werte, die schon Prozent-Einheit sind (Expectancy-% pro Trade)."""
        return f"{v:+.2f}%" if v is not None else "n/a"

    def _num(v, prec=2):
        return f"{v:+.{prec}f}" if v is not None else "n/a"

    gate_icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(snap["gate"], "⏳")
    wd = snap.get("wr_defs") or {}
    defs_note = ""
    if wd.get("return_pct_gt_0") is not None and wd.get("is_win_column") is not None:
        defs_note = (f"\n  > _WR-Defs: return>0 = {wd['return_pct_gt_0']*100:.1f}% · "
                     f"is_win-Spalte = {wd['is_win_column']*100:.1f}% · "
                     f"CI basiert auf return>0._")

    return (
        f"**Backtest-Baseline {gate_icon} (Gate {snap['gate'] or 'n/a'})**: "
        f"Cohort `{snap['cohort']}`, n={snap['n_trades']} Trades "
        f"({snap['date_range'][0]}: {snap['date_range'][1]}), "
        f"Run {snap['run_date']}\n"
        f"- **Win-Rate:** 95%-CI {_rate_pct(wr['lo'])} … {_rate_pct(wr['hi'])} · median {_rate_pct(wr['median'])}\n"
        f"- **Profit-Factor:** 95%-CI {_num(pf['lo'])} … {_num(pf['hi'])} · median {_num(pf['median'])}\n"
        f"- **Expectancy/Trade:** 95%-CI {_already_pct(ex['lo'])} … {_already_pct(ex['hi'])} · median {_already_pct(ex['median'])}"
        f"{defs_note}"
    )


def format_short(snap: dict) -> str:
    """Einzeiler fuer die Briefing-Kopfzeile."""
    wr = snap["win_rate"]
    pf = snap["profit_factor"]
    if wr["lo"] is None or pf["lo"] is None:
        return "Baseline-CI: n/a"
    return (
        f"Baseline-CI n={snap['n_trades']} · WR95 [{wr['lo']*100:.1f}%-{wr['hi']*100:.1f}%] · "
        f"PF95 [{pf['lo']:.2f}-{pf['hi']:.2f}] · Gate {snap['gate']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--format", choices=("markdown", "json", "short"), default="markdown")
    ap.add_argument("--file", default=None,
                    help="Explicit path to bootstrap_ci_live_*.json (default: latest)")
    args = ap.parse_args()

    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            print(f"[baseline_ci_line] Datei nicht gefunden: {fp}", file=sys.stderr)
            return 2
        doc = json.loads(fp.read_text(encoding="utf-8"))
    else:
        fp, doc = load_latest_live_ci()
        if not doc:
            print("[baseline_ci_line] Kein bootstrap_ci_live_*.json gefunden. "
                  "Erst `python3 sim/bootstrap_live.py` laufen lassen.", file=sys.stderr)
            return 2

    snap = _extract_ci(doc)

    if args.format == "json":
        print(json.dumps(snap, indent=2, ensure_ascii=False))
    elif args.format == "short":
        print(format_short(snap))
    else:
        print(format_markdown(snap))

    return 0


if __name__ == "__main__":
    sys.exit(main())
