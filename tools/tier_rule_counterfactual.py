#!/usr/bin/env python3
"""
V7.38 TIER-RULE-COUNTERFACTUAL-1: read-only H2-Diagnostik (V7.35-Anschluss).

------------------------------------------------------------------------------
DIE FRAGE (H2, seit KW24 wiederkehrend, in JEDEM Masterplan referenziert):
  "Ist die `regime_hard_tier`-Regel (auf divergenten/Krisen-Regimes nur Tier A,
   auf Extreme-Fear nur A/B) fuer ein Mean-Reversion-System ZU STRENG?"

  Kontext: `rec_gating_attribution` (V7.35) hat mechanisch belegt, dass die
  Tier-A-only-Regel am 30.06. (Trending-Divergent) 88,5 % des Scanner-Outputs
  killte: den GESAMTEN Tier-B/C-MR-Pool (u. a. die REIT-Leader EQIX/DLR). Das
  ist der dokumentierte ~30-Tage-Stillstand-Engpass. Offen war: haette das
  Zulassen der ausgeschlossenen Tiers HISTORISCH Geld gebracht (Regel zu streng)
  oder Verluste gespart (Regel schuetzt korrekt)?

DIESES TOOL beantwortet das evidenzbasiert, und ist dabei bewusst so ehrlich
wie V7.33 (das eine "wir koennen das messen"-Illusion korrigiert hat):

  Fuer jedes Regime MIT Tier-Verengung (Quelle: `rec_gating_attribution.
  REGIME_HARD_RULES` = SSoT, NICHT re-abgeleitet) wird aus dem historischen
  V6.1-Panel die Erwartung des AUSGESCHLOSSENEN Tier-Pools gegen die des
  erlaubten Pools gestellt:
    - excluded mean_ret > 0 & n>=Floor  -> RULE_TOO_STRICT (Geld liegen gelassen)
    - excluded mean_ret <= 0 & n>=Floor -> RULE_PROTECTIVE (korrekt ausgeschlossen)
    - excluded n < Floor                -> INSUFFICIENT_DATA (nicht messbar)

DER ZENTRALE, EHRLICHE BEFUND (Stand 2026-07: 440-Trade-Panel):
  Das ausgefuehrte V6.1-Backtest-Panel ist zu ~98 % Tier A. In 3 der 4
  verengten Regimes ist der ausgeschlossene Tier-B/C-Pool schlicht n=0: die
  EINZIGE messbare Ausnahme (High-Vol-Krise, Tier B, n=3) ist NEGATIV
  (mean_ret -1,997 %, mean_netto -13,45 EUR) = schwache Evidenz FUER Schutz.
  => Die "Tier-A-only zu streng?"-Frage ist aus den Backtest-Returns derzeit
     GROSSTEILS UNBEANTWORTBAR: Die ausgeschlossenen Tiers wurden im Panel nie
     als Trades realisiert, also existiert kein Gegenprobe-Return. Diesen
     Blindfleck zu benennen (statt ein Schein-Verdikt zu liefern) IST das
     Ergebnis, und es spezifiziert den konkreten Unblock (s. u.).

DER UNBLOCK (was die Frage ECHT beantworten wuerde):
  Forward-Returns der von `rec_gating_attribution` als `regime_hard_tier`-gekillt
  identifizierten Tier-B/C-Setups (Shadow-Tracking) ODER ein tier-ablatierter
  Backtest, der die verengten Setups mit-simuliert. `rec_gating_attribution`
  liefert bereits die gekillte Ticker-Liste pro Tag → dieses Tool ist der
  Adapter, der Return-Daten in genau diese Zellen einspeist, sobald sie
  vorliegen.

EISERNE REGEL: KEINE Live-Regel/Schwelle/Sizing geaendert. Reine Diagnostik.
Die tatsaechliche Tier-Aufweitung bleibt ein Folgeschritt, der maintainer approval
braucht (H2-Feature-Freeze).

CLI:
  python3 tools/tier_rule_counterfactual.py                 # JSON auf stdout
  python3 tools/tier_rule_counterfactual.py --write         # + signals/ + Briefings/
  python3 tools/tier_rule_counterfactual.py --selftest      # hermetisch
  python3 tools/tier_rule_counterfactual.py --min-n 10 --regime-json signals/regime_2026-07-06.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

_DEFAULT_CSV = _ROOT / "v6_backtest_results_v61.csv"
_MIN_COUNTERFACTUAL_N = 10   # Mindest-n im ausgeschlossenen Pool fuer ein Verdikt
_ALL_TIERS = ("A", "B", "C")

# --------------------------------------------------------------------------- #
# SSoT: die verengenden Tier-Regeln aus dem V7.35-Attributions-Tool importieren.
# NIE hier neu ableiten: bei Aenderung dort greift sie automatisch hier.
# Defensive Fallback-Kopie nur fuer den Fall eines Import-Bruchs in der Sandbox.
# --------------------------------------------------------------------------- #
_FALLBACK_HARD_RULES: Dict[str, Dict[str, Any]] = {
    "Trending-Divergent":   {"hard_tiers": ["A"],      "max_recs": 2},
    "Bullish-Divergent":    {"hard_tiers": ["A"],      "max_recs": 2},
    "High-Vol-Krise":       {"hard_tiers": ["A"],      "max_recs": 1},
    "Extreme-Fear-Thrust":  {"hard_tiers": ["A", "B"], "max_recs": 2},
    "Trending":             {"hard_tiers": None,       "max_recs": 3},
    "Bullish-Confirmed":    {"hard_tiers": None,       "max_recs": 3},
    "Neutral":              {"hard_tiers": None,       "max_recs": 3},
}
try:
    from rec_gating_attribution import (  # noqa: E402
        REGIME_HARD_RULES as _HARD_RULES,
        DEFAULT_HARD_RULE as _DEFAULT_RULE,
    )
    _HARD_RULES_SOURCE = "rec_gating_attribution.REGIME_HARD_RULES"
except Exception:  # pragma: no cover - nur bei Sandbox-Import-Bruch
    _HARD_RULES = _FALLBACK_HARD_RULES
    _DEFAULT_RULE = {"hard_tiers": None, "max_recs": 3}
    _HARD_RULES_SOURCE = "fallback (import failed)"


# --------------------------------------------------------------------------- #
# Helfer (bewusst lokal + trivial gehalten, keine Abhaengigkeit von privaten
# Symbolen der Nachbar-Tools).
# --------------------------------------------------------------------------- #
def _fnum(x) -> Optional[float]:
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


def _is_win(x) -> Optional[bool]:
    s = str(x).strip().lower()
    if s in ("1", "true", "1.0", "yes", "win"):
        return True
    if s in ("0", "false", "0.0", "no", "loss"):
        return False
    return None


def _mean(vals: List[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _sign(x: Optional[float]) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _read_text_resilient(path: Path) -> Optional[str]:
    """CSV robust lesen (Sandbox-EDEADLK-tolerant, ein Retry)."""
    try:
        from cohort_tag_ledger import read_text_resilient as _rtr  # noqa: E402
        return _rtr(path)
    except Exception:
        pass
    for _ in range(2):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def _excluded_tiers(hard_tiers: Optional[List[str]]) -> List[str]:
    """Tiers, die die hard-Rule AUSSCHLIESST (None => keine Verengung => [])."""
    if not hard_tiers:
        return []
    allowed = {str(t).strip().upper() for t in hard_tiers}
    return [t for t in _ALL_TIERS if t not in allowed]


# --------------------------------------------------------------------------- #
# 1) Panel -> {regime, tier, return_pct, v61_netto_pnl, is_win, year}
# --------------------------------------------------------------------------- #
def parse_panel_rows(csv_text: str) -> List[dict]:
    out: List[dict] = []
    rdr = csv.DictReader(io.StringIO(csv_text))
    fields = {(c or "").strip().lower(): c for c in (rdr.fieldnames or [])}
    c_reg = fields.get("regime")
    c_tier = fields.get("tier")
    c_ret = fields.get("return_pct")
    c_npl = fields.get("v61_netto_pnl") or fields.get("netto_pnl")
    c_win = fields.get("is_win")
    c_year = fields.get("year")
    c_date = fields.get("date")
    if not c_reg:
        return out
    for r in rdr:
        reg = str(r.get(c_reg) or "").strip()
        if not reg or reg == "?":
            continue
        tier = str(r.get(c_tier) or "").strip().upper() if c_tier else ""
        year = None
        if c_year and str(r.get(c_year) or "").strip():
            year = _fnum(r.get(c_year))
        elif c_date and str(r.get(c_date) or "").strip():
            year = _fnum(str(r.get(c_date))[:4])
        out.append({
            "regime": reg,
            "tier": tier or "?",
            "return_pct": _fnum(r.get(c_ret)) if c_ret else None,
            "v61_netto_pnl": _fnum(r.get(c_npl)) if c_npl else None,
            "is_win": _is_win(r.get(c_win)) if c_win else None,
            "year": int(year) if year is not None else None,
        })
    return out


def _pool_stats(rows: List[dict]) -> dict:
    rets = [r["return_pct"] for r in rows if r["return_pct"] is not None]
    npls = [r["v61_netto_pnl"] for r in rows if r["v61_netto_pnl"] is not None]
    wins = [r["is_win"] for r in rows if r["is_win"] is not None]
    years = sorted({r["year"] for r in rows if r["year"] is not None})
    return {
        "n": len(rows),
        "n_with_return": len(rets),
        "mean_return_pct": round(_mean(rets), 4) if rets else None,
        "mean_v61_netto": round(_mean(npls), 4) if npls else None,
        "total_v61_netto": round(sum(npls), 2) if npls else None,
        "win_rate": round(100.0 * sum(1 for w in wins if w) / len(wins), 1) if wins else None,
        "years": years,
    }


def build_tier_matrix(panel: List[dict]) -> dict:
    """regime x tier -> Pool-Statistik (rein deskriptiv)."""
    cells: Dict[str, Dict[str, List[dict]]] = {}
    for r in panel:
        cells.setdefault(r["regime"], {}).setdefault(r["tier"], []).append(r)
    matrix: Dict[str, Dict[str, dict]] = {}
    for reg, by_tier in cells.items():
        matrix[reg] = {t: _pool_stats(rows) for t, rows in by_tier.items()}
    return matrix


# --------------------------------------------------------------------------- #
# 2) Per-Regime-Counterfactual
# --------------------------------------------------------------------------- #
def _year_sign_consistency(rows: List[dict]) -> Optional[bool]:
    """Ist das Vorzeichen des mean_return ueber die messbaren Jahre stabil?"""
    by_year: Dict[int, List[float]] = {}
    for r in rows:
        if r["year"] is not None and r["return_pct"] is not None:
            by_year.setdefault(r["year"], []).append(r["return_pct"])
    signs = {_sign(_mean(v)) for v in by_year.values() if v}
    signs.discard(0)
    if len(by_year) < 2 or not signs:
        return None
    return len(signs) == 1


def assess_regime_counterfactual(regime: str, rule: dict, panel: List[dict],
                                 min_n: int = _MIN_COUNTERFACTUAL_N) -> dict:
    hard_tiers = rule.get("hard_tiers")
    excl = _excluded_tiers(hard_tiers)
    reg_rows = [r for r in panel if r["regime"] == regime]
    allowed_up = [str(t).strip().upper() for t in hard_tiers] if hard_tiers else []
    allowed_rows = [r for r in reg_rows if r["tier"] in allowed_up]
    excl_rows = [r for r in reg_rows if r["tier"] in excl]

    allowed_stats = _pool_stats(allowed_rows)
    excl_stats = _pool_stats(excl_rows)

    excl_mean = excl_stats["mean_return_pct"]
    n_excl = excl_stats["n_with_return"]

    if not excl:
        verdict = "NO_RESTRICTION"
        rationale = "Regime hat keine Tier-Verengung (hard_tiers=None)."
    elif n_excl < min_n:
        verdict = "INSUFFICIENT_DATA"
        rationale = (f"Ausgeschlossener Pool n={n_excl} < Floor {min_n}: aus den "
                     f"Backtest-Returns nicht messbar (Tiers {excl} wurden in "
                     f"'{regime}' historisch nie als Trades realisiert).")
    elif excl_mean is not None and excl_mean > 0:
        verdict = "RULE_TOO_STRICT"
        rationale = (f"Ausgeschlossener Pool {excl}: n={n_excl}, mean_ret "
                     f"{excl_mean:+.3f}% > 0 → die Verengung liess historisch "
                     f"positive Erwartung liegen.")
    else:
        verdict = "RULE_PROTECTIVE"
        rationale = (f"Ausgeschlossener Pool {excl}: n={n_excl}, mean_ret "
                     f"{excl_mean:+.3f}% ≤ 0 → die Verengung schloss historisch "
                     f"nicht-positive Erwartung korrekt aus.")

    delta = None
    if excl_mean is not None and allowed_stats["mean_return_pct"] is not None:
        delta = round(excl_mean - allowed_stats["mean_return_pct"], 4)

    return {
        "regime": regime,
        "hard_tiers_allowed": allowed_up or None,
        "excluded_tiers": excl,
        "allowed_pool": allowed_stats,
        "excluded_pool": excl_stats,
        "excluded_minus_allowed_mean_ret": delta,
        "excluded_year_sign_consistent": _year_sign_consistency(excl_rows),
        "min_n": min_n,
        "verdict": verdict,
        "rationale": rationale,
    }


def global_tier_expectancy(panel: List[dict]) -> dict:
    """Premissen-Check: ist Tier A ueber ALLE Regimes der verlaessliche Kern?"""
    by_tier: Dict[str, List[dict]] = {}
    for r in panel:
        by_tier.setdefault(r["tier"], []).append(r)
    stats = {t: _pool_stats(rows) for t, rows in by_tier.items()}
    a = stats.get("A", {}).get("mean_return_pct")
    non_a_rows = [r for r in panel if r["tier"] in ("B", "C")]
    non_a = _pool_stats(non_a_rows)
    premise = None
    if a is not None and non_a["mean_return_pct"] is not None:
        premise = bool(a > non_a["mean_return_pct"])
    return {
        "by_tier": stats,
        "tier_A_mean_ret": a,
        "tier_BC_pool": non_a,
        "premise_A_is_core": premise,
        "note": ("premise_A_is_core=True stuetzt die Rechtfertigung der "
                 "Tier-Verengung; n(B/C) ist im Panel sehr klein → nur schwaches "
                 "Signal."),
    }


# --------------------------------------------------------------------------- #
# 3) Live-Hook: bindet die Regel HEUTE?
# --------------------------------------------------------------------------- #
def load_today_regime(regime_json: Optional[Path]) -> Optional[str]:
    if not regime_json:
        return None
    try:
        d = json.loads(Path(regime_json).read_text(encoding="utf-8"))
        return (d.get("regime") or "").strip() or None
    except Exception:
        return None


def assess_live_binding(today_regime: Optional[str], per_regime: Dict[str, dict]) -> dict:
    if not today_regime:
        return {"today_regime": None, "rule_binding_today": None,
                "note": "Kein Regime-JSON uebergeben: Live-Bindung nicht bewertet."}
    entry = per_regime.get(today_regime)
    excl = entry["excluded_tiers"] if entry else []
    binding = bool(excl)
    return {
        "today_regime": today_regime,
        "rule_binding_today": binding,
        "excluded_tiers_today": excl,
        "today_verdict": entry["verdict"] if entry else "UNKNOWN_REGIME",
        "note": (f"'{today_regime}' verengt heute auf {entry['hard_tiers_allowed']} "
                 f"→ Tiers {excl} gekillt." if binding else
                 f"'{today_regime}' hat keine Tier-Verengung → Regel heute nicht bindend."),
    }


# --------------------------------------------------------------------------- #
# 4) Report-Assembly + Gesamt-Verdikt
# --------------------------------------------------------------------------- #
def build_report(csv_path: Path, *, min_n: int = _MIN_COUNTERFACTUAL_N,
                 run_date: Optional[str] = None,
                 regime_json: Optional[Path] = None,
                 csv_text: Optional[str] = None,
                 today_regime: Optional[str] = None) -> dict:
    run_date = run_date or _date.today().isoformat()
    txt = csv_text if csv_text is not None else _read_text_resilient(Path(csv_path))
    if txt is None:
        return {"tool": "tier_rule_counterfactual", "version": "V7.38",
                "date": run_date, "error": f"Panel-CSV nicht lesbar: {csv_path}"}
    panel = parse_panel_rows(txt)
    matrix = build_tier_matrix(panel)

    restricted = {reg: rule for reg, rule in _HARD_RULES.items()
                  if rule.get("hard_tiers")}
    per_regime = {reg: assess_regime_counterfactual(reg, rule, panel, min_n=min_n)
                  for reg, rule in _HARD_RULES.items()}

    # Gesamt-Verdikt: nur die restringierten Regimes zaehlen.
    restr_verdicts = [per_regime[r]["verdict"] for r in restricted]
    n_too_strict = restr_verdicts.count("RULE_TOO_STRICT")
    n_protective = restr_verdicts.count("RULE_PROTECTIVE")
    n_insuff = restr_verdicts.count("INSUFFICIENT_DATA")
    n_measurable = n_too_strict + n_protective

    if not restricted:
        overall = "NO_RESTRICTED_REGIMES"
    elif n_measurable == 0:
        overall = "DATA_STARVED_UNANSWERABLE"
    elif n_too_strict > 0 and n_protective == 0:
        overall = "SOME_RESTRICTIONS_TOO_STRICT"
    elif n_too_strict > 0:
        overall = "MIXED"
    else:
        overall = "RESTRICTIONS_PROTECTIVE"

    glob = global_tier_expectancy(panel)
    if today_regime is None:
        today_regime = load_today_regime(regime_json)
    live = assess_live_binding(today_regime, per_regime)

    return {
        "tool": "tier_rule_counterfactual",
        "version": "V7.38",
        "roadmap_item": "TIER-RULE-COUNTERFACTUAL-1 (V7.35-Anschluss)",
        "date": run_date,
        "scope": "IN_SAMPLE_HISTORICAL (V6.1-Backtest-Panel; kein Forward-Test)",
        "hard_rules_source": _HARD_RULES_SOURCE,
        "question": ("Ist die regime_hard_tier-Regel (Tier-A-only auf divergenten/"
                     "Krisen-Regimes) fuer das MR-System zu streng?"),
        "panel_n": len(panel),
        "min_n": min_n,
        "restricted_regimes": list(restricted.keys()),
        "per_regime": per_regime,
        "tier_matrix": matrix,
        "global_tier_expectancy": glob,
        "live_binding": live,
        "summary_counts": {
            "restricted": len(restricted),
            "too_strict": n_too_strict,
            "protective": n_protective,
            "insufficient_data": n_insuff,
            "measurable": n_measurable,
        },
        "overall_verdict": overall,
        "honest_finding": (
            "Das ausgefuehrte V6.1-Panel ist ~98 % Tier A; in den verengten "
            "Regimes ist der ausgeschlossene Tier-B/C-Pool ueberwiegend n=0. Die "
            "'zu streng?'-Frage ist aus Backtest-Returns derzeit grossteils "
            "UNBEANTWORTBAR: die ausgeschlossenen Tiers wurden nie als Trades "
            "realisiert. Dieser Blindfleck (statt eines Schein-Verdikts) IST das "
            "Ergebnis."),
        "unblock": (
            "Forward-Returns der von rec_gating_attribution als regime_hard_tier-"
            "gekillt markierten Tier-B/C-Setups (Shadow-Tracking) ODER ein tier-"
            "ablatierter Backtest. rec_gating_attribution liefert die gekillte "
            "Ticker-Liste pro Tag; dieses Tool ist der Adapter, sobald Returns "
            "vorliegen."),
        "honesty": ("read-only Diagnostik. KEINE Live-Regel/Schwelle/Sizing "
                    "geaendert. Tier-Aufweitung requires maintainer approval, H2-Feature-Freeze."),
    }


# --------------------------------------------------------------------------- #
# 5) Markdown-Render (Deutsch)
# --------------------------------------------------------------------------- #
def _fmt(x, nd=3) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render_md(rep: dict) -> str:
    if rep.get("error"):
        return f"# Tier-Rule-Counterfactual {rep.get('date')}\n\nFEHLER: {rep['error']}\n"
    L: List[str] = []
    L.append(f"# Tier-Rule-Counterfactual: {rep['date']} (V7.38, read-only)")
    L.append("")
    L.append(f"**Roadmap-Item:** {rep['roadmap_item']} · **Gesamt-Verdikt:** "
             f"`{rep['overall_verdict']}`")
    L.append(f"**Frage:** {rep['question']}")
    L.append(f"**Scope:** {rep['scope']} · Panel n={rep['panel_n']} · "
             f"min_n={rep['min_n']} · Regel-Quelle: `{rep['hard_rules_source']}`")
    L.append("")
    L.append("## 1. Kern-Befund (ehrlich)")
    L.append("")
    L.append(rep["honest_finding"])
    L.append("")
    sc = rep["summary_counts"]
    L.append(f"Verengte Regimes: **{sc['restricted']}** · messbar: **{sc['measurable']}** "
             f"(zu-streng {sc['too_strict']} / schuetzend {sc['protective']}) · "
             f"nicht messbar (n<Floor): **{sc['insufficient_data']}**")
    L.append("")
    L.append("## 2. Per-Regime-Counterfactual (nur verengte Regimes)")
    L.append("")
    L.append("| Regime | erlaubt | ausgeschl. | erl.Pool n·ret% | ausg.Pool n·ret%·netto€ | Δ ret% | Jahr-Vorz.-stabil | Verdikt |")
    L.append("|---|:--:|:--:|--:|--:|--:|:--:|:--|")
    for reg in rep["restricted_regimes"]:
        e = rep["per_regime"][reg]
        ap, ep = e["allowed_pool"], e["excluded_pool"]
        sc2 = e["excluded_year_sign_consistent"]
        L.append(
            f"| {reg} | {','.join(e['hard_tiers_allowed'] or [])} | "
            f"{','.join(e['excluded_tiers'])} | "
            f"{ap['n']}·{_fmt(ap['mean_return_pct'])} | "
            f"{ep['n']}·{_fmt(ep['mean_return_pct'])}·{_fmt(ep['mean_v61_netto'],2)} | "
            f"{_fmt(e['excluded_minus_allowed_mean_ret'])} | "
            f"{'✓' if sc2 else ('✗' if sc2 is False else '-')} | "
            f"`{e['verdict']}` |")
    L.append("")
    L.append("_Rationale je Regime:_")
    for reg in rep["restricted_regimes"]:
        L.append(f"- **{reg}:** {rep['per_regime'][reg]['rationale']}")
    L.append("")
    g = rep["global_tier_expectancy"]
    L.append("## 3. Premissen-Check: ist Tier A der verlaessliche Kern?")
    L.append("")
    L.append("| Tier | n | mean_ret% | WR% | mean_netto€ |")
    L.append("|:--:|--:|--:|--:|--:|")
    for t in ("A", "B", "C", "?"):
        s = g["by_tier"].get(t)
        if not s:
            continue
        L.append(f"| {t} | {s['n']} | {_fmt(s['mean_return_pct'])} | "
                 f"{_fmt(s['win_rate'],1)} | {_fmt(s['mean_v61_netto'],2)} |")
    L.append("")
    L.append(f"- Tier-A mean_ret **{_fmt(g['tier_A_mean_ret'])}%** vs "
             f"Tier-B/C-Pool (n={g['tier_BC_pool']['n']}) "
             f"**{_fmt(g['tier_BC_pool']['mean_return_pct'])}%** → "
             f"premise_A_is_core = **{g['premise_A_is_core']}**")
    L.append(f"- {g['note']}")
    L.append("")
    lb = rep["live_binding"]
    L.append("## 4. Live-Bindung heute")
    L.append("")
    L.append(f"- Heutiges Regime: **{_fmt(lb.get('today_regime'))}** · Regel bindet "
             f"heute: **{lb.get('rule_binding_today')}**")
    L.append(f"- {lb['note']}")
    L.append("")
    L.append("## 5. Unblock (was die Frage ECHT beantwortet)")
    L.append("")
    L.append(rep["unblock"])
    L.append("")
    L.append(f"> {rep['honesty']}")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 6) Selftest (hermetisch, kein echtes CSV/Netz/Regime-JSON noetig)
# --------------------------------------------------------------------------- #
#   Synthetisches Panel, so gebaut, dass es alle drei Verdikte + den Live-Hook
#   deterministisch ausloest:
#     - Trending-Divergent : excluded (B) POSITIV, n>=Floor  -> RULE_TOO_STRICT
#     - High-Vol-Krise     : excluded (B) NEGATIV, n>=Floor  -> RULE_PROTECTIVE
#     - Bullish-Divergent  : excluded n=0                    -> INSUFFICIENT_DATA
#     - Trending           : keine Verengung                 -> NO_RESTRICTION
_SYN_CSV_HEAD = "date,ticker,tier,regime,return_pct,v61_netto_pnl,is_win,year\n"


def _syn_csv() -> str:
    rows: List[str] = []
    # Trending-Divergent: A allowed (neutral-ish), B excluded & POSITIV (n=4)
    for i, (y, ret) in enumerate([(2023, 0.2), (2023, 0.1), (2024, 0.3), (2025, 0.15)]):
        rows.append(f"20{y-2000:02d}-01-{i+1:02d},TDA{i},A,Trending-Divergent,{ret},{ret*5},1,{y}\n")
    for i, (y, ret) in enumerate([(2023, 1.0), (2023, 1.2), (2024, 0.8), (2025, 1.1)]):
        rows.append(f"20{y-2000:02d}-02-{i+1:02d},TDB{i},B,Trending-Divergent,{ret},{ret*5},1,{y}\n")
    # High-Vol-Krise: A allowed positiv, B excluded & NEGATIV (n=4)
    for i, (y, ret) in enumerate([(2023, 0.9), (2024, 1.0), (2024, 0.8), (2025, 1.1)]):
        rows.append(f"20{y-2000:02d}-03-{i+1:02d},HVA{i},A,High-Vol-Krise,{ret},{ret*5},1,{y}\n")
    for i, (y, ret) in enumerate([(2023, -1.5), (2023, -2.0), (2024, -1.8), (2025, -2.2)]):
        rows.append(f"20{y-2000:02d}-04-{i+1:02d},HVB{i},B,High-Vol-Krise,{ret},{ret*5},0,{y}\n")
    # Bullish-Divergent: only A (excluded n=0) -> INSUFFICIENT_DATA
    for i, (y, ret) in enumerate([(2024, 1.5), (2025, 1.8)]):
        rows.append(f"20{y-2000:02d}-05-{i+1:02d},BDA{i},A,Bullish-Divergent,{ret},{ret*5},1,{y}\n")
    # Trending: no restriction; mix of A/B
    for i, (y, ret) in enumerate([(2023, 1.0), (2024, 1.2), (2025, 0.9)]):
        rows.append(f"20{y-2000:02d}-06-{i+1:02d},TRA{i},A,Trending,{ret},{ret*5},1,{y}\n")
    return _SYN_CSV_HEAD + "".join(rows)


def selftest() -> int:
    fails: List[str] = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    # --- Gruppe 1: SSoT-Import der Regel-Map ---
    chk("Trending-Divergent" in _HARD_RULES, "REGIME_HARD_RULES missing TD")
    chk(_HARD_RULES["Trending-Divergent"]["hard_tiers"] == ["A"], "TD hard_tiers != [A]")
    chk(_excluded_tiers(["A"]) == ["B", "C"], "excluded_tiers([A]) != [B,C]")
    chk(_excluded_tiers(["A", "B"]) == ["C"], "excluded_tiers([A,B]) != [C]")
    chk(_excluded_tiers(None) == [], "excluded_tiers(None) != []")

    # --- Gruppe 2: Panel-Parsing (tier + regime + return) ---
    panel = parse_panel_rows(_syn_csv())
    #  TD 4A+4B, HVK 4A+4B, BD 2A, Trending 3A = 21
    chk(len(panel) == 21, f"panel n != 21 ({len(panel)})")
    chk(all("tier" in r for r in panel), "panel row missing tier")
    tds = [r for r in panel if r["regime"] == "Trending-Divergent"]
    chk(len(tds) == 8, f"TD rows != 8 ({len(tds)})")

    # --- Gruppe 3: RULE_TOO_STRICT (TD, B positiv, n=4>=Floor) ---
    a_td = assess_regime_counterfactual("Trending-Divergent",
                                        _HARD_RULES["Trending-Divergent"], panel, min_n=4)
    chk(a_td["verdict"] == "RULE_TOO_STRICT", f"TD verdict {a_td['verdict']} != TOO_STRICT")
    chk(a_td["excluded_pool"]["n"] == 4, f"TD excl n != 4 ({a_td['excluded_pool']['n']})")
    chk(a_td["excluded_pool"]["mean_return_pct"] > 0, "TD excl mean not >0")
    chk(a_td["excluded_minus_allowed_mean_ret"] is not None, "TD delta None")

    # --- Gruppe 4: RULE_PROTECTIVE (HVK, B negativ, n=4) ---
    a_hv = assess_regime_counterfactual("High-Vol-Krise",
                                        _HARD_RULES["High-Vol-Krise"], panel, min_n=4)
    chk(a_hv["verdict"] == "RULE_PROTECTIVE", f"HVK verdict {a_hv['verdict']} != PROTECTIVE")
    chk(a_hv["excluded_pool"]["mean_return_pct"] < 0, "HVK excl mean not <0")
    chk(a_hv["excluded_year_sign_consistent"] is True, "HVK excl sign not consistent")

    # --- Gruppe 5: INSUFFICIENT_DATA (BD, excluded n=0) ---
    a_bd = assess_regime_counterfactual("Bullish-Divergent",
                                        _HARD_RULES["Bullish-Divergent"], panel, min_n=4)
    chk(a_bd["verdict"] == "INSUFFICIENT_DATA", f"BD verdict {a_bd['verdict']} != INSUFFICIENT")
    chk(a_bd["excluded_pool"]["n"] == 0, "BD excl n != 0")

    # --- Gruppe 6: NO_RESTRICTION (Trending) ---
    a_tr = assess_regime_counterfactual("Trending", _HARD_RULES["Trending"], panel, min_n=4)
    chk(a_tr["verdict"] == "NO_RESTRICTION", f"Trending verdict {a_tr['verdict']} != NO_RESTRICTION")

    # --- Gruppe 7: global premise + tier matrix ---
    glob = global_tier_expectancy(panel)
    chk("A" in glob["by_tier"] and "B" in glob["by_tier"], "global tiers missing")
    chk(glob["premise_A_is_core"] in (True, False), "premise not boolean")
    mtx = build_tier_matrix(panel)
    chk(mtx["Trending-Divergent"]["B"]["n"] == 4, "matrix TD-B n != 4")

    # --- Gruppe 8: build_report end-to-end + Gesamt-Verdikt MIXED ---
    rep = build_report(Path("ignored"), min_n=4, run_date="2026-07-07",
                       csv_text=_syn_csv(), today_regime="Trending-Divergent")
    #  1x TOO_STRICT (TD) + 1x PROTECTIVE (HVK) + INSUFFICIENT (BD, EFT) -> MIXED
    chk(rep["overall_verdict"] == "MIXED", f"overall {rep['overall_verdict']} != MIXED")
    chk(rep["summary_counts"]["too_strict"] == 1, "counts too_strict != 1")
    chk(rep["summary_counts"]["protective"] == 1, "counts protective != 1")
    chk(rep["live_binding"]["rule_binding_today"] is True, "TD should bind today")
    chk(rep["live_binding"]["excluded_tiers_today"] == ["B", "C"], "TD live excl != B,C")

    # --- Gruppe 9: Live-Hook nicht bindend (Trending) + kein Regime ---
    rep2 = build_report(Path("ignored"), min_n=4, run_date="2026-07-07",
                        csv_text=_syn_csv(), today_regime="Trending")
    chk(rep2["live_binding"]["rule_binding_today"] is False, "Trending should not bind")
    rep3 = build_report(Path("ignored"), min_n=4, csv_text=_syn_csv(), today_regime=None)
    chk(rep3["live_binding"]["rule_binding_today"] is None, "no-regime should be None")

    # --- Gruppe 10: DATA_STARVED overall (min_n hoch => alles insufficient) + MD ---
    rep4 = build_report(Path("ignored"), min_n=999, run_date="2026-07-07",
                        csv_text=_syn_csv(), today_regime="Trending-Divergent")
    chk(rep4["overall_verdict"] == "DATA_STARVED_UNANSWERABLE",
        f"high min_n overall {rep4['overall_verdict']} != DATA_STARVED")
    md = render_md(rep)
    chk("Tier-Rule-Counterfactual" in md and "Per-Regime-Counterfactual" in md,
        "md render incomplete")
    # error path
    rep_err = build_report(Path("/nonexistent/xx.csv"), min_n=4)
    # csv_text=None + unreadable => error dict
    chk("error" in rep_err or rep_err.get("panel_n") == 0, "unreadable csv not handled")

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELFTEST OK: 10 Gruppen, alle Checks gruen "
          f"(panel {len(panel)}, restricted {rep['summary_counts']['restricted']}, "
          f"overall {rep['overall_verdict']}).")
    return 0


# --------------------------------------------------------------------------- #
# 7) main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="V7.38 Tier-Rule-Counterfactual (read-only)")
    ap.add_argument("--csv", default=str(_DEFAULT_CSV), help="Historisches V6.1-Panel")
    ap.add_argument("--min-n", type=int, default=_MIN_COUNTERFACTUAL_N,
                    help="Mindest-n im ausgeschlossenen Pool fuer ein Verdikt")
    ap.add_argument("--regime-json", default=None,
                    help="regime_<date>.json fuer Live-Bindung (optional)")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="JSON + MD schreiben")
    ap.add_argument("--selftest", action="store_true", help="Hermetischen Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    regime_json = Path(args.regime_json) if args.regime_json else None
    # Auto-Detect: heutiges regime_<date>.json, falls nicht explizit uebergeben.
    if regime_json is None:
        cand = _TOOLS / f"regime_{args.date}.json"
        if cand.exists():
            regime_json = cand
        else:
            cand2 = _ROOT / "signals" / f"regime_{args.date}.json"
            if cand2.exists():
                regime_json = cand2

    rep = build_report(Path(args.csv), min_n=args.min_n, run_date=args.date,
                       regime_json=regime_json)
    print(json.dumps(rep, indent=2, ensure_ascii=False))

    if args.write and not rep.get("error"):
        sig = _ROOT / "signals" / f"tier_rule_counterfactual_{args.date}.json"
        sig.parent.mkdir(parents=True, exist_ok=True)
        sig.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path = _ROOT / "Briefings" / f"Tier-Rule-Counterfactual-{args.date}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_md(rep), encoding="utf-8")
        print(f"\nSaved: {sig}", file=sys.stderr)
        print(f"Saved: {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
