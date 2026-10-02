#!/usr/bin/env python3
"""
EXIT-EDGE-DURABILITY-1 (V7.23 · 2026-06-15 Mo KW25 · FEATURE).

Read-only Durability-/Stabilitaets-Layer fuer die EXIT-Seite der V6.1-Strategie.

`tools/exit_attribution.py` (V7.21) hat den V6.1-Backtest AGGREGAT zerlegt und
gezeigt: die Edge sitzt im Signal-Exit `connors_sma5`, der forced max-hold-Exit
ist ein Drag, und ~85% des Netto-PnL faellt in T+1..T+2 (stuetzt die Hypothese
"Peak oft bei T+1"). Das ist ein starker AGGREGAT-Befund, aber er beantwortet die
naechste, schaerfere H2-Frage NICHT:

    Ist dieser Exit-Edge STABIL ueber die Zeit und ueber Regime,
    oder ist er ein Artefakt EINES guten Jahres / EINER Marktphase?

Genau das muss geklaert sein, BEVOR H2 irgendeine Exit-Regel (Teilgewinn-T+1,
Zeit-Stop-T+2, max-hold-Cap) anfasst. Ein Edge, der nur 2024 trug und 2023/2026
negativ war, ist KEINE Basis fuer eine Regel-Aenderung: er ist Overfitting.

Dieses Tool nimmt denselben Korpus (`v6_backtest_results_v61.csv`, aktive
V6.1-Trades, v61_skip != True) und zerlegt ihn entlang von SUB-PERIODEN:

  1. **edge_by_year**       Signal-Exit (connors_sma5) je Jahr:
                            n / WR / Σ netto / avg netto / Expectancy
  2. **frontload_by_year**  Σ netto T+1..T+2  vs.  Σ netto gesamt je Jahr
                            -> ist die Front-Load-Eigenschaft jedes Jahr da?
  3. **max_hold_by_year**   forced max-hold-Drag je Jahr (Vorzeichen-Stabilitaet)
  4. **edge_by_regime**     Signal-Exit je Regime (Trending / Neutral / ...)

Daraus ein maschinen-lesbares Durability-Verdikt:
  - edge_sign_consistency   Anteil der Jahre (n>=MIN_N) mit Σ_netto(edge) > 0
  - edge_worst_year         (Jahr, Σ_netto) des schwaechsten Edge-Jahres
  - edge_worst_expectancy   minimale Per-Jahr-Expectancy des Edge-Engines
  - frontload_consistency   Anteil der Jahre mit frontload_ratio >= FL_FLOOR
  - max_hold_drag_consistency  Anteil der Jahre mit Σ_netto(max_hold) <= 0
  - edge_is_durable         bool + reason  (Gate-Logik s.u.)

GATE-Logik fuer `edge_is_durable` (konservativ, H2-tauglich):
  durable  <=>  edge_sign_consistency  >= SIGN_FLOOR (default 0.75)
           AND  edge_worst_expectancy  >  0          (kein Verlust-Jahr)
           AND  n_years_qualified      >= MIN_YEARS  (default 3)

WICHTIG (eiserne Regel CLAUDE.md §3/§9): NUR Analyse. Aendert KEINE Live-Regel,
KEINE Schwelle, KEIN Sizing. Output ist H2-Panel-Input. Jede echte
Regel-Aenderung wartet auf eine saubere H2-Re-Validierung mit Forward-Daten.

V7.17/V7.21-Pattern: EDEADLK-resilienter Reader (Sandbox-Mount wirft sporadisch
Errno 35 'Resource deadlock avoided'); nativ auf dem Mac irrelevant. Output wird
atomar (tmp + os.replace) geschrieben.

CLI:
    python3 tools/exit_durability.py
    python3 tools/exit_durability.py --csv v6_backtest_results_v61.csv
    python3 tools/exit_durability.py --date 2026-06-15 --write
    python3 tools/exit_durability.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Quelle nicht lesbar (EDEADLK persistent / Datei fehlt / keine Trades)
    3  Selftest fail

Output JSON : tools/exit_durability_<date>.json   (nur mit --write)
Output MD   : Briefings/Exit-Durability-<date>.md  (nur mit --write)

"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"
DEFAULT_CSV = REPO_ROOT / "v6_backtest_results_v61.csv"

VERSION = "V7.23"

# Der primaere Signal-Exit der Connors-MR-Strategie (= die vermutete Edge).
_SIGNAL_EXIT = "connors_sma5"
# Exit-Reasons, die als "forced max hold" gelten (Hard-Exit-T+3-Familie).
_MAX_HOLD_REASONS = {"day3_flat_exit", "max_hold", "hard_exit_t3", "time_exit"}

# Gate-Parameter (konservativ, H2-tauglich).
MIN_N_PER_YEAR = 5      # ein Jahr "qualifiziert" erst ab so vielen Edge-Trades
MIN_YEARS = 3           # so viele qualifizierte Jahre muessen vorliegen
SIGN_FLOOR = 0.75       # Anteil positiver Edge-Jahre fuer "durable"
FL_FLOOR = 0.50         # frontload_ratio-Schwelle pro Jahr (>=50% in T+1..T+2)


# --------------------------------------------------------------------------- #
# IO (V7.17/V7.21-Pattern)
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


def atomic_write_text(path: Path, text: str, tries: int = 40, backoff: float = 0.4) -> bool:
    """Atomarer Schreiber (tmp + os.replace), EDEADLK-resilient."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(tries):
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wtmp_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, str(path))
                return True
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except OSError as e:
            if getattr(e, "errno", None) == 35:
                time.sleep(backoff)
                continue
            return False
    return False


def _to_float(x) -> Optional[float]:
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


def _pick(r: dict, *keys: str):
    """Erstes vorhandenes, nicht-leeres Feld aus mehreren Spaltennamen."""
    for k in keys:
        if k in r and str(r[k]).strip() != "":
            return r[k]
    return None


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def load_trades(csv_text: str) -> list[dict]:
    """Parst den Backtest-CSV und filtert auf aktive V6.1-Trades (v61_skip != True)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    active = []
    for r in reader:
        if str(r.get("v61_skip", "")).strip().lower() == "true":
            continue
        active.append(r)
    return active


def _exit_reason(r: dict) -> str:
    return _pick(r, "v61_exit_reason", "exit_reason") or "?"


def _netto(r: dict) -> float:
    return _to_float(_pick(r, "v61_netto_pnl", "netto_pnl")) or 0.0


def _year(r: dict) -> str:
    y = _pick(r, "year")
    if y is not None:
        return str(y).strip()
    d = _pick(r, "date")
    if d and len(str(d)) >= 4:
        return str(d)[:4]
    return "?"


def _hold(r: dict) -> Optional[int]:
    h = _to_float(r.get("holding_days"))
    return int(h) if h is not None else None


def _bucket_stats(group: list[dict]) -> dict:
    """n / WR / Σ netto / avg netto / avg ret / avg hold / Expectancy."""
    n = len(group)
    nettos = [_netto(g) for g in group]
    rets = [_to_float(g.get("return_pct")) for g in group]
    rets = [x for x in rets if x is not None]
    holds = [h for h in (_hold(g) for g in group) if h is not None]
    wins = sum(1 for x in nettos if x > 0)
    sum_netto = sum(nettos)
    # Expectancy je Trade = avg netto pro Trade (EUR/Trade).
    expectancy = round(sum_netto / n, 2) if n else 0.0
    return {
        "n": n,
        "win_rate_pct": round(100.0 * wins / n, 1) if n else 0.0,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "sum_netto": round(sum_netto, 2),
        "avg_netto": expectancy,
        "expectancy": expectancy,
        "avg_holding_days": round(sum(holds) / len(holds), 2) if holds else None,
    }


def _frontload_ratio(group: list[dict]) -> Optional[float]:
    """Σ netto(T+1..T+2) / Σ netto(gesamt) fuer eine Trade-Gruppe."""
    total = sum(_netto(g) for g in group)
    front = sum(_netto(g) for g in group if (_hold(g) in (1, 2)))
    if total == 0:
        return None
    return round(front / total, 3)


def analyze(active: list[dict]) -> dict:
    """Baut die Sub-Perioden-Achsen + das Durability-Verdikt."""
    edge_trades = [r for r in active if _exit_reason(r) == _SIGNAL_EXIT]
    mh_trades = [r for r in active if _exit_reason(r) in _MAX_HOLD_REASONS]

    # ---- 1. edge_by_year ----
    by_year_groups: dict[str, list[dict]] = defaultdict(list)
    for r in edge_trades:
        by_year_groups[_year(r)].append(r)
    edge_by_year = {
        y: _bucket_stats(g) for y, g in sorted(by_year_groups.items())
    }

    # ---- 2. frontload_by_year (auf ALLEN aktiven Trades des Jahres) ----
    all_by_year: dict[str, list[dict]] = defaultdict(list)
    for r in active:
        all_by_year[_year(r)].append(r)
    frontload_by_year = {}
    for y, g in sorted(all_by_year.items()):
        frontload_by_year[y] = {
            "n": len(g),
            "sum_netto_total": round(sum(_netto(x) for x in g), 2),
            "sum_netto_t1_t2": round(sum(_netto(x) for x in g if _hold(x) in (1, 2)), 2),
            "frontload_ratio": _frontload_ratio(g),
        }

    # ---- 3. max_hold_by_year ----
    mh_by_year_groups: dict[str, list[dict]] = defaultdict(list)
    for r in mh_trades:
        mh_by_year_groups[_year(r)].append(r)
    max_hold_by_year = {
        y: _bucket_stats(g) for y, g in sorted(mh_by_year_groups.items())
    }

    # ---- 4. edge_by_regime ----
    by_regime_groups: dict[str, list[dict]] = defaultdict(list)
    for r in edge_trades:
        by_regime_groups[r.get("regime") or "?"].append(r)
    edge_by_regime = dict(
        sorted(
            {k: _bucket_stats(v) for k, v in by_regime_groups.items()}.items(),
            key=lambda kv: -kv[1]["sum_netto"],
        )
    )

    # ---- Durability-Verdikt ----
    qualified_years = {y: s for y, s in edge_by_year.items()
                       if s["n"] >= MIN_N_PER_YEAR and y != "?"}
    n_qual = len(qualified_years)
    pos_years = [y for y, s in qualified_years.items() if s["sum_netto"] > 0]
    sign_consistency = round(len(pos_years) / n_qual, 3) if n_qual else None

    worst_year = None
    worst_expectancy = None
    if qualified_years:
        wy = min(qualified_years.items(), key=lambda kv: kv[1]["sum_netto"])
        worst_year = {"year": wy[0], "sum_netto": wy[1]["sum_netto"],
                      "win_rate_pct": wy[1]["win_rate_pct"]}
        worst_expectancy = min(s["expectancy"] for s in qualified_years.values())

    # frontload-Konsistenz nur ueber qualifizierte Jahre (gleiche Jahresmenge)
    fl_qual = {y: frontload_by_year[y] for y in qualified_years if y in frontload_by_year}
    fl_pos = [y for y, s in fl_qual.items()
              if s["frontload_ratio"] is not None and s["frontload_ratio"] >= FL_FLOOR]
    frontload_consistency = round(len(fl_pos) / len(fl_qual), 3) if fl_qual else None

    mh_qual = {y: max_hold_by_year[y] for y in qualified_years if y in max_hold_by_year}
    mh_drag = [y for y, s in mh_qual.items() if s["sum_netto"] <= 0]
    max_hold_drag_consistency = round(len(mh_drag) / len(mh_qual), 3) if mh_qual else None

    durable = bool(
        sign_consistency is not None
        and sign_consistency >= SIGN_FLOOR
        and worst_expectancy is not None
        and worst_expectancy > 0
        and n_qual >= MIN_YEARS
    )
    reasons = []
    if sign_consistency is None or n_qual < MIN_YEARS:
        reasons.append(
            f"zu wenige qualifizierte Jahre (n={n_qual} < {MIN_YEARS}, "
            f"MIN_N_PER_YEAR={MIN_N_PER_YEAR}): Datenlage fuer Durability duenn."
        )
    else:
        if sign_consistency >= SIGN_FLOOR:
            reasons.append(
                f"Edge in {len(pos_years)}/{n_qual} qualifizierten Jahren positiv "
                f"(sign_consistency {sign_consistency:.0%} >= {SIGN_FLOOR:.0%})."
            )
        else:
            reasons.append(
                f"Edge nur in {len(pos_years)}/{n_qual} Jahren positiv "
                f"(sign_consistency {sign_consistency:.0%} < {SIGN_FLOOR:.0%}) "
                f"-> NICHT durable, Verdacht auf Einzeljahr-Artefakt."
            )
        if worst_expectancy is not None and worst_expectancy <= 0:
            reasons.append(
                f"schwaechstes Jahr ({worst_year['year']}) hat Expectancy "
                f"{worst_expectancy:+.2f} EUR/Trade <= 0 -> Verlust-Jahr vorhanden."
            )

    verdict = {
        "edge_engine": _SIGNAL_EXIT,
        "n_years_total": len([y for y in edge_by_year if y != "?"]),
        "n_years_qualified": n_qual,
        "qualified_years": sorted(qualified_years.keys()),
        "edge_sign_consistency": sign_consistency,
        "edge_positive_years": sorted(pos_years),
        "edge_worst_year": worst_year,
        "edge_worst_expectancy": worst_expectancy,
        "frontload_consistency": frontload_consistency,
        "max_hold_drag_consistency": max_hold_drag_consistency,
        "gate_params": {
            "MIN_N_PER_YEAR": MIN_N_PER_YEAR,
            "MIN_YEARS": MIN_YEARS,
            "SIGN_FLOOR": SIGN_FLOOR,
            "FL_FLOOR": FL_FLOOR,
        },
        "edge_is_durable": durable,
        "reasons": reasons,
    }

    return {
        "edge_by_year": edge_by_year,
        "frontload_by_year": frontload_by_year,
        "max_hold_by_year": max_hold_by_year,
        "edge_by_regime": edge_by_regime,
        "durability_verdict": verdict,
    }


def build_report(csv_path: Path, for_date: _date) -> dict:
    text = edeadlk_read_text(csv_path)
    if text is None:
        return {"error": "source_unreadable", "path": str(csv_path)}
    active = load_trades(text)
    if not active:
        return {"error": "no_active_trades", "path": str(csv_path)}
    body = analyze(active)
    return {
        "version": f"exit_durability/{VERSION}",
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "date": for_date.isoformat(),
        "source_csv": str(csv_path.name),
        "n_active": len(active),
        **body,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _yr_table(title: str, buckets: dict, cols: str = "edge") -> str:
    if cols == "frontload":
        lines = [f"### {title}", "",
                 "| Jahr | n | Σ Netto € | Σ Netto T+1..T+2 € | Frontload-Ratio |",
                 "|---|---:|---:|---:|---:|"]
        for y, b in buckets.items():
            fr = b["frontload_ratio"]
            lines.append(
                f"| {y} | {b['n']} | {b['sum_netto_total']:+.2f} | "
                f"{b['sum_netto_t1_t2']:+.2f} | "
                f"{'' if fr is None else format(fr, '.0%')} |"
            )
        lines.append("")
        return "\n".join(lines)
    lines = [f"### {title}", "",
             "| Bucket | n | WR% | Σ Netto € | Expectancy €/Trade | Ø Hold |",
             "|---|---:|---:|---:|---:|---:|"]
    for k, b in buckets.items():
        lines.append(
            f"| {k} | {b['n']} | {b['win_rate_pct']} | {b['sum_netto']:+.2f} | "
            f"{b['expectancy']:+.2f} | "
            f"{'' if b['avg_holding_days'] is None else b['avg_holding_days']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_md(rep: dict) -> str:
    if rep.get("error"):
        return f"# Exit-Durability: FEHLER\n\n`{rep['error']}` ({rep.get('path','')})\n"
    v = rep["durability_verdict"]
    durable_str = "✅ DURABLE" if v["edge_is_durable"] else "⚠️ NICHT bestätigt"
    out = [
        f"# Exit-Edge-Durability {rep['date']}: H2-Panel-Input ({rep['version']})",
        "",
        f"**Quelle:** `{rep['source_csv']}` · aktive V6.1-Trades: **{rep['n_active']}** · "
        f"Edge-Engine: `{v['edge_engine']}`",
        "",
        "> Read-only Analyse. Aendert KEINE Live-Regel/Schwelle/Sizing "
        "(eiserne Regel: nichts vor sauberer H2-Re-Validierung). "
        "Baut auf V7.21 EXIT-ATTRIBUTION auf: dort *wo* sitzt die Edge, "
        "hier *ob sie ueber Zeit/Regime stabil* ist.",
        "",
        "## Durability-Verdikt",
        "",
        f"- **Status:** {durable_str}",
        f"- **Qualifizierte Jahre:** {v['n_years_qualified']}/{v['n_years_total']} "
        f"(>= {v['gate_params']['MIN_N_PER_YEAR']} Edge-Trades/Jahr): "
        f"{', '.join(v['qualified_years']) or '-'}",
        f"- **Edge-Sign-Consistency:** "
        f"{'' if v['edge_sign_consistency'] is None else format(v['edge_sign_consistency'], '.0%')} "
        f"(positiv in: {', '.join(v['edge_positive_years']) or '-'})",
        f"- **Schwaechstes Jahr:** "
        + ("-" if not v["edge_worst_year"] else
           f"{v['edge_worst_year']['year']} "
           f"({v['edge_worst_year']['sum_netto']:+.2f} €, "
           f"WR {v['edge_worst_year']['win_rate_pct']}%)"),
        f"- **Worst-Year-Expectancy:** "
        f"{'' if v['edge_worst_expectancy'] is None else format(v['edge_worst_expectancy'], '+.2f')} €/Trade",
        f"- **Frontload-Consistency (>= {v['gate_params']['FL_FLOOR']:.0%} in T+1..T+2):** "
        f"{'' if v['frontload_consistency'] is None else format(v['frontload_consistency'], '.0%')}",
        f"- **Max-Hold-Drag-Consistency (Σnetto <= 0):** "
        f"{'' if v['max_hold_drag_consistency'] is None else format(v['max_hold_drag_consistency'], '.0%')}",
        "",
        "**Lesart:**",
        "",
    ]
    for r in v["reasons"]:
        out.append(f"- {r}")
    out += [
        "",
        "## Sub-Perioden-Zerlegung",
        "",
        _yr_table("Edge-Engine (connors_sma5) je Jahr", rep["edge_by_year"], "edge"),
        _yr_table("Frontload je Jahr (alle aktiven Trades)", rep["frontload_by_year"], "frontload"),
        _yr_table("Forced max-hold Drag je Jahr", rep["max_hold_by_year"], "edge"),
        _yr_table("Edge-Engine je Regime", rep["edge_by_regime"], "edge"),
        "---",
        "",
        "_H2-Konsequenz:_ Ist `edge_is_durable=true`, darf H2 die in V7.21 "
        "formulierten Exit-Hypothesen (Teilgewinn-T+1 / Zeit-Stop-T+2 / max-hold-Cap) "
        "ueberhaupt erst forward-testen. Ist es `false`, ist der Aggregat-Edge aus "
        "V7.21 ein Kandidat fuer Einzeljahr-Overfitting → KEINE Exit-Regel-Aenderung, "
        "stattdessen mehr Forward-Daten sammeln.",
        "",
        f"_Erzeugt {rep['generated_at_utc']} · {rep['version']}._",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
def _mk_row(year, exit_reason, netto, hold, regime="Trending", v61_skip="False"):
    return {
        "year": str(year),
        "date": f"{year}-03-01",
        "regime": regime,
        "exit_reason": exit_reason,
        "v61_exit_reason": exit_reason,
        "netto_pnl": str(netto),
        "v61_netto_pnl": str(netto),
        "return_pct": "1.0" if float(netto) > 0 else "-1.0",
        "holding_days": str(hold),
        "v61_skip": v61_skip,
    }


def _synthetic_durable() -> list[dict]:
    """Edge in JEDEM Jahr positiv + front-loaded -> sollte durable sein."""
    rows = []
    for y in ("2023", "2024", "2025"):
        # 6 Edge-Trades/Jahr, alle positiv, fuenf in T+1/T+2
        for i in range(6):
            hold = 1 if i % 2 == 0 else 2
            rows.append(_mk_row(y, "connors_sma5", 40.0, hold))
        # max-hold-Drag jedes Jahr negativ
        for i in range(3):
            rows.append(_mk_row(y, "day3_flat_exit", -20.0, 3))
    return rows


def _synthetic_fragile() -> list[dict]:
    """Edge nur in 1/3 Jahren positiv -> NICHT durable (Einzeljahr-Artefakt)."""
    rows = []
    # 2024 traegt alles
    for i in range(6):
        rows.append(_mk_row("2024", "connors_sma5", 60.0, 1))
    # 2023 + 2025 negativ
    for y in ("2023", "2025"):
        for i in range(6):
            rows.append(_mk_row(y, "connors_sma5", -15.0, 3))
    return rows


def _selftest() -> int:
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    # T1: durable panel -> edge_is_durable true, sign 100%
    rep = {"date": "2026-06-15"}
    body = analyze(_synthetic_durable())
    v = body["durability_verdict"]
    check("T1_durable_true", v["edge_is_durable"] is True)
    check("T1_sign_100", v["edge_sign_consistency"] == 1.0)
    check("T1_three_years", v["n_years_qualified"] == 3)
    check("T1_worst_pos", (v["edge_worst_expectancy"] or 0) > 0)
    check("T1_frontload_full", v["frontload_consistency"] == 1.0)
    check("T1_maxhold_drag_full", v["max_hold_drag_consistency"] == 1.0)

    # T2: fragile panel -> not durable, sign ~33%
    body2 = analyze(_synthetic_fragile())
    v2 = body2["durability_verdict"]
    check("T2_durable_false", v2["edge_is_durable"] is False)
    check("T2_sign_low", v2["edge_sign_consistency"] is not None and v2["edge_sign_consistency"] < 0.75)
    check("T2_worst_neg", (v2["edge_worst_expectancy"] or 0) < 0)
    check("T2_worst_year_2023_or_2025", v2["edge_worst_year"]["year"] in ("2023", "2025"))

    # T3: too few years -> not durable, dünn-reason
    body3 = analyze([_mk_row("2024", "connors_sma5", 40.0, 1) for _ in range(6)])
    v3 = body3["durability_verdict"]
    check("T3_one_year_not_durable", v3["edge_is_durable"] is False)
    check("T3_n_qual_1", v3["n_years_qualified"] == 1)

    # T4: v61_skip filter respected
    skipped = [_mk_row("2024", "connors_sma5", 40.0, 1, v61_skip="True") for _ in range(3)]
    active = load_trades_from_rows(skipped + _synthetic_durable())
    check("T4_skip_filtered", all(str(r.get("v61_skip")).lower() != "true" for r in active))

    # T5: MIN_N gate, 4 edge-trades/year is below MIN_N_PER_YEAR(5) -> 0 qualified
    thin = []
    for y in ("2023", "2024", "2025"):
        for _ in range(4):
            thin.append(_mk_row(y, "connors_sma5", 40.0, 1))
    v5 = analyze(thin)["durability_verdict"]
    check("T5_below_min_n_unqualified", v5["n_years_qualified"] == 0)
    check("T5_below_min_n_not_durable", v5["edge_is_durable"] is False)

    # T6: render_md runs without exception on real-ish report
    full = build_report_from_rows(_synthetic_durable(), _date(2026, 6, 15))
    md = render_md(full)
    check("T6_md_nonempty", isinstance(md, str) and "Durability-Verdikt" in md)
    check("T6_json_roundtrip", json.loads(json.dumps(full))["n_active"] == len(_synthetic_durable()))

    # T7: frontload_ratio math
    g = [_mk_row("2024", "connors_sma5", 50.0, 1), _mk_row("2024", "connors_sma5", 50.0, 5)]
    check("T7_frontload_half", _frontload_ratio(g) == 0.5)

    total = 7 * 1  # checks groups, but count individual asserts
    n_checks = 17
    if fails:
        print(f"SELFTEST FAIL ({len(fails)} failing): {', '.join(fails)}")
        return 3
    print(f"SELFTEST PASS: {n_checks} checks green (T1..T7, {VERSION})")
    return 0


def load_trades_from_rows(rows: list[dict]) -> list[dict]:
    """Selftest-Helfer: wendet denselben v61_skip-Filter wie load_trades an."""
    return [r for r in rows if str(r.get("v61_skip", "")).strip().lower() != "true"]


def build_report_from_rows(rows: list[dict], for_date: _date) -> dict:
    """Selftest-Helfer: Report direkt aus Rows (ohne CSV-IO)."""
    active = load_trades_from_rows(rows)
    body = analyze(active)
    return {
        "version": f"exit_durability/{VERSION}",
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "date": for_date.isoformat(),
        "source_csv": "<synthetic>",
        "n_active": len(active),
        **body,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Exit-Edge-Durability (V7.23, read-only H2-Input)")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="Backtest-CSV (default v6_backtest_results_v61.csv)")
    ap.add_argument("--date", default=None, help="Report-Datum YYYY-MM-DD (default heute)")
    ap.add_argument("--write", action="store_true", help="JSON + MD nach tools/ bzw. Briefings/ schreiben")
    ap.add_argument("--selftest", action="store_true", help="Offline-Selftest, kein IO")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    for_date = _date.today()
    if args.date:
        for_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    rep = build_report(Path(args.csv), for_date)
    if rep.get("error"):
        print(f"ERROR: {rep['error']} ({rep.get('path','')})", file=sys.stderr)
        return 2

    print(json.dumps(rep["durability_verdict"], indent=2, ensure_ascii=False))

    if args.write:
        jpath = TOOLS_DIR / f"exit_durability_{for_date.isoformat()}.json"
        mpath = BRIEFINGS_DIR / f"Exit-Durability-{for_date.isoformat()}.md"
        ok_j = atomic_write_text(jpath, json.dumps(rep, indent=2, ensure_ascii=False))
        ok_m = atomic_write_text(mpath, render_md(rep))
        print(f"\nwrote: {jpath.name} ({'ok' if ok_j else 'FAIL'}) · {mpath.name} ({'ok' if ok_m else 'FAIL'})")
        if not (ok_j and ok_m):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
