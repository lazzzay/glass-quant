#!/usr/bin/env python3
"""
EXIT-ATTRIBUTION-1 (V7.21 · 2026-06-12 Fr KW24 · FEATURE).

Read-only Attributions-Layer fuer die EXIT-Seite der V6.1-Strategie.

Bisher deckt `tools/layer_ablation.py` nur die SCORING-Seite ab (welcher
Boost-Layer traegt zum Entry-Score bei). Die Frage "welcher EXIT-Mechanismus
verdient eigentlich die Rendite, und ist der Hard-Exit-T+3 / max-hold-Bein ein
Drag?" war bis V7.20 unbeantwortet. Die Hypothese "der Peak liegt oft bei T+1"
hat sie scharf gestellt (H2-Panel-Frage "Zeit-/Teilgewinn-Exit?").

Dieses Tool zerlegt den V6.1-Backtest-Korpus (`v6_backtest_results_v61.csv`,
optional auch das Live-Journal) nach drei Achsen und liefert je Bucket
n / Win-Rate / avg return_pct / sum netto_pnl / avg holding_days / Expectancy:

  1. **by_exit_reason**   connors_sma5 / atr_take_profit / atr_stop_loss /
                          day3_flat_exit  -> wo sitzt die Edge, was blutet
  2. **by_regime**        Trending / Trending-Divergent / Neutral / ...
  3. **by_holding_days**  T+1 / T+2 / T+3 / T+4  -> ist die Edge front-loaded?

Daraus wird ein maschinen-lesbares H2-Verdikt abgeleitet:
  - edge_engine        Exit-Reason mit dem groessten positiven sum_netto
  - max_hold_drag      sum_netto + WR des day3_flat_exit-/max_hold-Buckets
  - frontload_ratio    (sum_netto T+1..T+2) / (sum_netto aller Hold-Tage)
  - day4plus_bleed     sum_netto der Trades mit holding_days >= 4

WICHTIG (eiserne Regel CLAUDE.md): Dieses Tool ist NUR Analyse. Es aendert KEINE
Live-Regel, KEINE Schwelle, KEIN Sizing. Output ist ein H2-Panel-Input; jede
echte Regel-Aenderung wartet auf eine saubere H2-Re-Validierung mit Forward-Daten.

V7.17-Pattern: EDEADLK-resilienter Reader (Sandbox-Mount wirft sporadisch
Errno 35 'Resource deadlock avoided'); nativ auf dem Mac irrelevant.

CLI:
    python3 tools/exit_attribution.py
    python3 tools/exit_attribution.py --csv v6_backtest_results_v61.csv
    python3 tools/exit_attribution.py --date 2026-06-12 --write
    python3 tools/exit_attribution.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Quelle nicht lesbar (EDEADLK persistent / Datei fehlt)
    3  Selftest fail

Output JSON : tools/exit_attribution_<date>.json
Output MD   : Briefings/Exit-Attribution-<date>.md

"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"
DEFAULT_CSV = REPO_ROOT / "v6_backtest_results_v61.csv"

VERSION = "V7.21"

# Exit-Reasons, die als "forced max hold" gelten (Hard-Exit-T+3-Familie).
_MAX_HOLD_REASONS = {"day3_flat_exit", "max_hold", "hard_exit_t3", "time_exit"}
# Der primaere Signal-Exit der Connors-MR-Strategie.
_SIGNAL_EXIT = "connors_sma5"


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def edeadlk_read_text(path: Path, tries: int = 40, backoff: float = 0.4) -> Optional[str]:
    """Liest eine Datei robust gegen Sandbox-Mount-EDEADLK (Errno 35)."""
    for _ in range(tries):
        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                chunks = []
                while True:
                    b = os.read(fd, 1 << 20)
                    if not b:
                        break
                    chunks.append(b)
                return b"".join(chunks).decode("utf-8", errors="replace")
            finally:
                os.close(fd)
        except FileNotFoundError:
            return None
        except OSError as e:
            if getattr(e, "errno", None) == 35:
                time.sleep(backoff)
                continue
            return None
    return None


def _to_float(x) -> Optional[float]:
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def load_trades(csv_text: str) -> list[dict]:
    """Parst den Backtest-CSV und filtert auf aktive V6.1-Trades."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    active = []
    for r in rows:
        # v61_skip == True -> Trade ist im V6.1-Regime ausgeschlossen (Tier-X etc.)
        if str(r.get("v61_skip", "")).strip().lower() == "true":
            continue
        active.append(r)
    return active


def _pick(r: dict, *keys: str):
    """Erstes vorhandenes, nicht-leeres Feld aus mehreren Spaltennamen."""
    for k in keys:
        if k in r and str(r[k]).strip() != "":
            return r[k]
    return None


def _bucket_stats(group: list[dict]) -> dict:
    n = len(group)
    nettos = [_to_float(_pick(g, "v61_netto_pnl", "netto_pnl")) or 0.0 for g in group]
    rets = [_to_float(g.get("return_pct")) for g in group]
    rets = [x for x in rets if x is not None]
    holds = [_to_float(g.get("holding_days")) for g in group]
    holds = [x for x in holds if x is not None]
    wins = sum(1 for x in nettos if x > 0)
    return {
        "n": n,
        "win_rate_pct": round(100.0 * wins / n, 1) if n else 0.0,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "sum_netto": round(sum(nettos), 2),
        "avg_netto": round(sum(nettos) / n, 2) if n else 0.0,
        "avg_holding_days": round(sum(holds) / len(holds), 2) if holds else None,
    }


def attribute(active: list[dict]) -> dict:
    """Baut die drei Attributions-Achsen + das H2-Verdikt."""
    def by(keyfn) -> dict:
        buckets = defaultdict(list)
        for r in active:
            buckets[keyfn(r)].append(r)
        out = {str(k): _bucket_stats(v) for k, v in buckets.items()}
        # nach sum_netto absteigend sortiert zurueckgeben
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["sum_netto"]))

    by_exit = by(lambda r: _pick(r, "v61_exit_reason", "exit_reason") or "?")
    by_regime = by(lambda r: r.get("regime") or "?")
    by_hold = by(lambda r: str(int(float(r["holding_days"]))) if _to_float(r.get("holding_days")) is not None else "?")

    # ---- H2-Verdikt ----
    # edge_engine = Exit-Reason mit groesstem positivem sum_netto
    edge_engine = max(by_exit.items(), key=lambda kv: kv[1]["sum_netto"], default=(None, {}))
    # max_hold_drag = aggregiert ueber alle forced-max-hold-Reasons
    mh = [r for r in active if (_pick(r, "v61_exit_reason", "exit_reason") or "") in _MAX_HOLD_REASONS]
    max_hold_stats = _bucket_stats(mh) if mh else None

    total_netto = sum(b["sum_netto"] for b in by_hold.values())
    front = sum(b["sum_netto"] for k, b in by_hold.items() if k in ("1", "2"))
    frontload_ratio = round(front / total_netto, 3) if total_netto else None
    day4plus = sum(
        b["sum_netto"] for k, b in by_hold.items()
        if k.isdigit() and int(k) >= 4
    )

    verdict = {
        "edge_engine": edge_engine[0],
        "edge_engine_sum_netto": edge_engine[1].get("sum_netto"),
        "edge_engine_win_rate_pct": edge_engine[1].get("win_rate_pct"),
        "max_hold_drag_sum_netto": max_hold_stats["sum_netto"] if max_hold_stats else None,
        "max_hold_drag_win_rate_pct": max_hold_stats["win_rate_pct"] if max_hold_stats else None,
        "frontload_ratio_t1_t2": frontload_ratio,
        "day4plus_bleed_sum_netto": round(day4plus, 2),
        "total_netto": round(total_netto, 2),
        "n_active": len(active),
    }
    # menschliche Kurz-Lesart
    notes = []
    if verdict["edge_engine"] == _SIGNAL_EXIT:
        notes.append(
            f"Die Edge sitzt im Signal-Exit '{_SIGNAL_EXIT}' "
            f"({verdict['edge_engine_sum_netto']:+.2f} EUR, "
            f"WR {verdict['edge_engine_win_rate_pct']}%)."
        )
    if max_hold_stats and max_hold_stats["sum_netto"] <= 0:
        notes.append(
            f"Der forced max-hold-Exit ist netto ein Drag "
            f"({max_hold_stats['sum_netto']:+.2f} EUR, WR {max_hold_stats['win_rate_pct']}%) "
            f"-> Kandidaten ohne Signal-Trigger bis T+3 zahlen mehrheitlich nicht."
        )
    if day4plus < 0:
        notes.append(
            f"Trades, die T+4+ erreichen, bluten ({day4plus:+.2f} EUR) "
            f"-> stuetzt die Hypothese 'Peak oft bei T+1' und den Hard-Exit-T+3."
        )
    if frontload_ratio is not None and frontload_ratio >= 0.6:
        notes.append(
            f"Edge ist front-loaded: {frontload_ratio:.0%} des Netto-PnL faellt in T+1..T+2."
        )
    verdict["notes"] = notes

    return {
        "by_exit_reason": by_exit,
        "by_regime": by_regime,
        "by_holding_days": by_hold,
        "h2_verdict": verdict,
    }


def build_report(csv_path: Path, for_date: _date) -> dict:
    text = edeadlk_read_text(csv_path)
    if text is None:
        return {"error": "source_unreadable", "path": str(csv_path)}
    active = load_trades(text)
    if not active:
        return {"error": "no_active_trades", "path": str(csv_path)}
    body = attribute(active)
    return {
        "version": f"exit_attribution/{VERSION}",
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "date": for_date.isoformat(),
        "source_csv": str(csv_path.name),
        **body,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _md_table(title: str, buckets: dict) -> str:
    lines = [f"### {title}", "",
             "| Bucket | n | WR% | Ø Ret% | Σ Netto € | Ø Netto € | Ø Hold |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for k, b in buckets.items():
        lines.append(
            f"| {k} | {b['n']} | {b['win_rate_pct']} | "
            f"{'' if b['avg_return_pct'] is None else b['avg_return_pct']} | "
            f"{b['sum_netto']:+.2f} | {b['avg_netto']:+.2f} | "
            f"{'' if b['avg_holding_days'] is None else b['avg_holding_days']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_md(rep: dict) -> str:
    v = rep["h2_verdict"]
    out = [
        f"# Exit-Attribution {rep['date']}: H2-Panel-Input ({rep['version']})",
        "",
        f"**Quelle:** `{rep['source_csv']}` · aktive V6.1-Trades: **{v['n_active']}**",
        "",
        "> Read-only Analyse. Aendert KEINE Live-Regel/Schwelle/Sizing "
        "(eiserne Regel: nichts vor sauberer H2-Re-Validierung).",
        "",
        "## H2-Verdikt",
        "",
        f"- **Edge-Engine:** `{v['edge_engine']}` "
        f"({v['edge_engine_sum_netto']:+.2f} €, WR {v['edge_engine_win_rate_pct']}%)",
        f"- **Forced max-hold Drag:** {v['max_hold_drag_sum_netto']:+.2f} € "
        f"(WR {v['max_hold_drag_win_rate_pct']}%)" if v["max_hold_drag_sum_netto"] is not None else
        "- **Forced max-hold Drag:** n/a",
        f"- **Frontload-Ratio T+1..T+2:** "
        f"{'' if v['frontload_ratio_t1_t2'] is None else format(v['frontload_ratio_t1_t2'], '.0%')}",
        f"- **T+4+ Bleed:** {v['day4plus_bleed_sum_netto']:+.2f} €",
        f"- **Gesamt-Netto (Backtest):** {v['total_netto']:+.2f} €",
        "",
    ]
    if v["notes"]:
        out.append("**Lesart:**")
        out.append("")
        for n in v["notes"]:
            out.append(f"- {n}")
        out.append("")
    out.append(_md_table("Nach Exit-Reason", rep["by_exit_reason"]))
    out.append(_md_table("Nach Regime", rep["by_regime"]))
    out.append(_md_table("Nach Holding-Days", rep["by_holding_days"]))
    out.append("---")
    out.append("")
    out.append("*Free-Stack. Keine Order, keine Regel-Aenderung.*")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
_FIXTURE = """date,ticker,regime,return_pct,netto_pnl,holding_days,exit_reason,v61_skip,v61_exit_reason,v61_netto_pnl
2024-01-01,AAA,Trending,3.0,30,1,connors_sma5,False,connors_sma5,30
2024-01-02,BBB,Trending,2.0,20,2,connors_sma5,False,connors_sma5,20
2024-01-03,CCC,Neutral,-1.0,-10,3,day3_flat_exit,False,day3_flat_exit,-10
2024-01-04,DDD,Neutral,-4.0,-40,4,atr_stop_loss,False,atr_stop_loss,-40
2024-01-05,EEE,Trending,5.0,50,1,connors_sma5,True,skipped_tier_x,0
"""


def _selftest() -> int:
    active = load_trades(_FIXTURE)
    assert len(active) == 4, f"expected 4 active, got {len(active)} (v61_skip filter)"
    rep = attribute(active)
    be = rep["by_exit_reason"]
    assert be["connors_sma5"]["n"] == 2, be["connors_sma5"]
    assert be["connors_sma5"]["sum_netto"] == 50.0, be["connors_sma5"]
    assert be["connors_sma5"]["win_rate_pct"] == 100.0
    assert be["atr_stop_loss"]["sum_netto"] == -40.0
    v = rep["h2_verdict"]
    assert v["edge_engine"] == "connors_sma5", v["edge_engine"]
    assert v["edge_engine_sum_netto"] == 50.0
    # forced max hold = day3_flat_exit bucket = -10, WR 0
    assert v["max_hold_drag_sum_netto"] == -10.0, v["max_hold_drag_sum_netto"]
    # holding-days totals: T1=30, T2=20, T3=-10, T4=-40 -> total 0; front=50
    assert v["total_netto"] == 0.0, v["total_netto"]
    assert v["day4plus_bleed_sum_netto"] == -40.0, v["day4plus_bleed_sum_netto"]
    # frontload_ratio = 50/0 -> None (guard gegen Division durch 0)
    assert v["frontload_ratio_t1_t2"] is None
    # render must not raise
    md = render_md({
        "version": "exit_attribution/test", "date": "2024-01-05",
        "source_csv": "fixture", **rep,
    })
    assert "Edge-Engine" in md and "connors_sma5" in md
    # _to_float edge cases
    assert _to_float("x") is None and _to_float("1.5") == 1.5
    print("exit_attribution selftest: 11/11 PASS")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Exit-Attribution V6.1 (H2-Panel-Input).")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="Backtest-CSV (default v6_backtest_results_v61.csv)")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Report-Datum (YYYY-MM-DD)")
    ap.add_argument("--write", action="store_true", help="JSON + MD ins Repo schreiben")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        try:
            return _selftest()
        except AssertionError as e:
            print(f"exit_attribution selftest FAIL: {e}", file=sys.stderr)
            return 3

    for_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    rep = build_report(Path(args.csv), for_date)
    if rep.get("error"):
        print(f"ERROR: {rep['error']} ({rep.get('path')})", file=sys.stderr)
        return 2

    print(json.dumps(rep["h2_verdict"], ensure_ascii=False, indent=2))
    if args.write:
        TOOLS_DIR.mkdir(exist_ok=True)
        BRIEFINGS_DIR.mkdir(exist_ok=True)
        jp = TOOLS_DIR / f"exit_attribution_{for_date.isoformat()}.json"
        mp = BRIEFINGS_DIR / f"Exit-Attribution-{for_date.isoformat()}.md"
        jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        mp.write_text(render_md(rep), encoding="utf-8")
        print(f"\nwrote {jp.name} + {mp.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
