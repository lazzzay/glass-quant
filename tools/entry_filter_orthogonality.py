#!/usr/bin/env python3
"""
ENTRY-FILTER-ORTHOGONALITY-1 (V7.26 · 2026-06-18 Do KW25 · FEATURE).

Read-only Edge-Diagnostik fuer die ENTRY-Seite: der naechste Schritt nach
V7.25 ENTRY-SCORE-EDGE. V7.25 hat sauber gezeigt: der Entry-Score (v6_score) ist
ein GATE, kein Ranker (rank_ic 0,027 ~ 0; Bucket-Expectancy non-monoton). Die
explizite V7.25-Konsequenz fuer H2 lautete woertlich:

    "Schwellen-Feintuning auf Score allein ist unbegruendet, H2 sollte
     ORTHOGONALE Filter pruefen."

Genau das macht dieses Tool. Kernfrage (H2):

    **Unter den Trades, die das Score-Gate passieren: welche ORTHOGONALEN
     Entry-Features (am Entry bekannt, NICHT der Score selbst) trennen
     Gewinner von Verlierern?**

Wenn der Score nicht ranked (V7.25), muss die Rendite-Streuung an etwas anderem
haengen. Dieses Tool vermisst die zum Entry verfuegbaren orthogonalen Features
auf dem V6.1-aktiven Backtest-Korpus (`v6_backtest_results_v61.csv`, exakt die
gleiche `v61_skip`-Filterung wie exit_attribution / entry_score_edge) und liefert
maschinen-lesbare H2-FORWARD-TEST-KANDIDATEN, keine Live-Regel.

Vermessene Features
  numerisch  : rsi2, rsi5, rsi14, triple_rsi, atr_pct  (+ v6_score als REFERENZ,
               damit man sieht, ob ein orthogonales Feature die eigene
               Ordnungs-Kraft des Scores schlaegt)
  kategorial : regime, sector   (tier near-konstant 254/4 -> geflaggt, nicht
               gegatet; year = temporal, nur Robustheits-Kontext, kein Filter)

Pro NUMERISCHEM Feature
  rank_ic        Spearman-Rang-Korrelation Feature <-> return_pct (primaer)
                 + Sekundaer gegen v61_netto_pnl. Der ehrliche Information-
                 Coefficient des orthogonalen Signals.
  quartile_lift  Feature in N Quantil-Buckets -> je n / WR / avg return_pct /
                 Expectancy (avg v61_netto_pnl). Steigt/faellt die Expectancy
                 monoton mit dem Feature?
  monotonie      Spearman(bucket_idx, expectancy).
  has_edge       TRUE nur wenn |rank_ic|>=IC_GATE UND |Top-Bottom-Expectancy|
                 sinnvoll UND Buckets monoton-ish. Mit Richtung (high_better /
                 low_better) und advisorischem Schnitt-Hinweis.

Pro KATEGORIALEM Feature
  per-Zelle n / WR / avg return_pct / Expectancy (nur Zellen n>=MIN_CELL).
  spread_pp      bestes minus schlechtestes avg_return_pct ueber eligible Zellen.
  has_separation TRUE wenn spread_pp>=SPREAD_GATE_PP und >=2 eligible Zellen.
  best/worst     z.B. Regime Neutral vs Bullish-Confirmed (cross-validiert die
                 bekannte Skip-Cohort mr_v61 x Bullish-Confirmed, vgl. V7.8).

Maschinen-lesbares Verdikt
  orthogonal_edge_found  TRUE wenn >=1 ORTHOGONALES Feature (ohne v6_score) sein
                         Gate reisst -> H2 hat reale Filter-Kandidaten.
  candidate_filters      rangsortierte Liste (Richtung + advisorischer Schnitt).
  score_reference_ic     rank_ic von v6_score selbst (Erwartung ~0,03, V7.25):
                         der Vergleichsmassstab.

STATISTISCHE EHRLICHKEIT (Anti-Pattern "Vibe ohne Tests"):
  ~7 Features werden getestet -> die IC/Spread-Gates sind NICHT multiple-testing-
  korrigiert. Treffer sind H2-HYPOTHESEN fuer den Forward-Test, KEINE bestaetigten
  Edges. n pro Zelle/Bucket wird immer mitgeliefert.

WICHTIG (eiserne Regel CLAUDE.md §3/§8): NUR Analyse. Aendert KEINE Live-Regel,
KEINE Schwelle, KEIN Sizing. Size/Hebel NIE vor H2. Output ist H2-Panel-Input.

V7.17-Pattern: EDEADLK-resilienter Reader (Sandbox-Mount wirft sporadisch
Errno 35 'Resource deadlock avoided'); nativ auf dem Mac irrelevant.

CLI:
    python3 tools/entry_filter_orthogonality.py
    python3 tools/entry_filter_orthogonality.py --csv v6_backtest_results_v61.csv
    python3 tools/entry_filter_orthogonality.py --buckets 4
    python3 tools/entry_filter_orthogonality.py --date 2026-06-18 --write
    python3 tools/entry_filter_orthogonality.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Quelle nicht lesbar (EDEADLK persistent / Datei fehlt / keine aktiven Trades)
    3  Selftest fail

Output JSON : tools/entry_filter_orthogonality_<date>.json
Output MD   : Briefings/Entry-Filter-Orthogonality-<date>.md

"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import date as _date
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CSV = _ROOT / "v6_backtest_results_v61.csv"
_DEFAULT_BUCKETS = 4

_IC_GATE = 0.10          # gleich wie V7.25 entry_score_edge -> direkt vergleichbar
_MIN_N = 20              # Mindest-Trades, damit ein numerisches Feature bewertet wird
_MIN_CELL = 15           # Mindest-Trades je kategorialer Zelle (sonst nicht eligible)
_SPREAD_GATE_PP = 1.5    # avg_return_pct-Spread (Prozentpunkte) fuer kategoriale Separation

# Orthogonale Entry-Features (am Entry bekannt). v6_score ist NUR Referenz.
_NUMERIC_FEATURES = ["rsi2", "rsi5", "rsi14", "triple_rsi", "atr_pct"]
_SCORE_FEATURE = "v6_score"
_CATEGORICAL_FEATURES = ["regime", "sector"]
_CONTEXT_CATEGORICAL = ["tier", "year"]   # geflaggt, nicht gegatet


# --------------------------------------------------------------------------- #
# Robuste Reader / Helfer (V7.17-Pattern, identisch zu entry_score_edge)
# --------------------------------------------------------------------------- #
def read_text_resilient(path: Path, retries: int = 5, backoff: float = 0.2) -> Optional[str]:
    """Liest eine Datei und ueberlebt sporadische Errno 35 (EDEADLK) im Sandbox-Mount."""
    for _ in range(retries):
        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                chunks = []
                while True:
                    b = os.read(fd, 1 << 20)
                    if not b:
                        break
                    chunks.append(b)
                return b"".join(chunks).decode("utf-8", "replace")
            finally:
                os.close(fd)
        except OSError as e:
            if e.errno == 35:  # EDEADLK -> kurz warten, retry
                time.sleep(backoff)
                continue
            if not path.exists():
                return None
            time.sleep(backoff)
    return None


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Daten
# --------------------------------------------------------------------------- #
def load_trades(csv_text: str) -> list[dict]:
    """Parst den Backtest-CSV und filtert auf aktive V6.1-Trades.

    Identische Logik zu tools/exit_attribution.py + tools/entry_score_edge.py:
    v61_skip == True -> Trade ist im V6.1-Regime ausgeschlossen -> faellt raus.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    active = []
    for r in reader:
        if str(r.get("v61_skip", "")).strip().lower() == "true":
            continue
        active.append(r)
    return active


# --------------------------------------------------------------------------- #
# Statistik (stdlib-only, identisch zu entry_score_edge)
# --------------------------------------------------------------------------- #
def _ranks(values: list[float]) -> list[float]:
    """Durchschnitts-Raenge (1-basiert), Ties gemittelt."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _round_opt(x: Optional[float], nd: int = 4) -> Optional[float]:
    """round(), aber None-sicher (Spearman/Pearson liefern None bei Null-Varianz)."""
    return None if x is None else round(x, nd)


def _wr(rows: list[dict]) -> Optional[float]:
    if not rows:
        return None
    w = sum(1 for r in rows if str(r.get("is_win", "")).strip().lower() == "true")
    return w / len(rows)


def _avg(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _group_stats(rows: list[dict]) -> dict:
    """n / WR / avg return_pct / Expectancy (avg v61_netto_pnl) fuer eine Gruppe."""
    rp = [_f(r.get("return_pct")) for r in rows]
    rp = [v for v in rp if v is not None]
    npnl = [_f(r.get("v61_netto_pnl")) for r in rows]
    npnl = [v for v in npnl if v is not None]
    return {
        "n": len(rows),
        "win_rate": round(_wr(rows), 4) if rows else None,
        "avg_return_pct": round(_avg(rp), 4) if rp else None,
        "expectancy": round(_avg(npnl), 4) if npnl else None,
        "sum_netto": round(sum(npnl), 2) if npnl else None,
    }


# --------------------------------------------------------------------------- #
# Numerische Feature-Analyse
# --------------------------------------------------------------------------- #
def analyze_numeric(active: list[dict], feature: str, n_buckets: int,
                    is_score_ref: bool = False) -> dict:
    pairs = [(_f(r.get(feature)), r) for r in active if _f(r.get(feature)) is not None]
    n = len(pairs)
    out: dict = {
        "feature": feature,
        "kind": "numeric",
        "is_orthogonal": not is_score_ref,
        "n": n,
        "rank_ic_return": None,
        "rank_ic_netto": None,
        "value_min": None,
        "value_max": None,
        "buckets": [],
        "bucket_monotonicity": None,
        "top_minus_bottom_expectancy": None,
        "direction": None,
        "has_edge": False,
        "notes": [],
    }
    if n < _MIN_N:
        out["notes"].append(f"n<{_MIN_N}, nicht belastbar")
        return out

    fvals = [p[0] for p in pairs]
    rets = [_f(p[1].get("return_pct")) for p in pairs]
    nettos = [_f(p[1].get("v61_netto_pnl")) for p in pairs]
    out["value_min"] = round(min(fvals), 4)
    out["value_max"] = round(max(fvals), 4)

    # rank_ic nur ueber Trades mit beidem vorhanden
    fr = [(fvals[i], rets[i]) for i in range(n) if rets[i] is not None]
    if len(fr) >= 3:
        out["rank_ic_return"] = _round_opt(_spearman([a for a, _ in fr], [b for _, b in fr]))
    fn = [(fvals[i], nettos[i]) for i in range(n) if nettos[i] is not None]
    if len(fn) >= 3:
        out["rank_ic_netto"] = _round_opt(_spearman([a for a, _ in fn], [b for _, b in fn]))

    # Quantil-Buckets aufsteigend nach Feature
    spairs = sorted(pairs, key=lambda p: p[0])
    buckets = []
    for b in range(n_buckets):
        lo = (b * n) // n_buckets
        hi = ((b + 1) * n) // n_buckets
        seg = spairs[lo:hi]
        if not seg:
            continue
        gs = _group_stats([p[1] for p in seg])
        gs["bucket"] = b + 1
        gs["value_lo"] = round(seg[0][0], 4)
        gs["value_hi"] = round(seg[-1][0], 4)
        buckets.append(gs)
    out["buckets"] = buckets

    exps = [(bk["bucket"], bk["expectancy"]) for bk in buckets if bk["expectancy"] is not None]
    if len(exps) >= 3:
        out["bucket_monotonicity"] = _round_opt(
            _spearman([float(i) for i, _ in exps], [e for _, e in exps])
        )
    if len(exps) >= 2:
        tmb = exps[-1][1] - exps[0][1]
        out["top_minus_bottom_expectancy"] = round(tmb, 4)

    ic = out["rank_ic_return"]
    mono = out["bucket_monotonicity"]
    tmb = out["top_minus_bottom_expectancy"]
    if ic is not None:
        out["direction"] = "high_better" if ic > 0 else "low_better"
    # Edge-Gate: |IC|>=Gate UND Buckets ziehen in dieselbe Richtung (mono-Vorzeichen
    # == IC-Vorzeichen) UND Top/Bottom-Expectancy-Differenz hat dasselbe Vorzeichen.
    if ic is not None and abs(ic) >= _IC_GATE and mono is not None and tmb is not None:
        consistent = (ic > 0) == (mono > 0) and (ic > 0) == (tmb > 0)
        out["has_edge"] = bool(consistent)
        if not consistent:
            out["notes"].append("IC>=Gate, aber Buckets nicht konsistent monoton: verworfen")
    elif ic is not None and abs(ic) < _IC_GATE:
        out["notes"].append(f"|rank_ic|<{_IC_GATE}")
    return out


# --------------------------------------------------------------------------- #
# Kategoriale Feature-Analyse
# --------------------------------------------------------------------------- #
def analyze_categorical(active: list[dict], feature: str, gated: bool = True) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in active:
        key = str(r.get(feature, "")).strip() or "(leer)"
        groups.setdefault(key, []).append(r)

    cells = []
    for key, rows in groups.items():
        gs = _group_stats(rows)
        gs["value"] = key
        gs["eligible"] = gs["n"] >= _MIN_CELL
        cells.append(gs)
    cells.sort(key=lambda c: (c["expectancy"] is None, -(c["expectancy"] or -1e9)))

    eligible = [c for c in cells if c["eligible"] and c["avg_return_pct"] is not None]
    spread_pp = None
    best = worst = None
    has_sep = False
    if len(eligible) >= 2:
        ret_sorted = sorted(eligible, key=lambda c: c["avg_return_pct"])
        worst, best = ret_sorted[0], ret_sorted[-1]
        spread_pp = round(best["avg_return_pct"] - worst["avg_return_pct"], 4)
        if gated:
            has_sep = spread_pp >= _SPREAD_GATE_PP
    return {
        "feature": feature,
        "kind": "categorical",
        "is_orthogonal": gated,
        "gated": gated,
        "n_cells": len(cells),
        "n_eligible": len(eligible),
        "cells": cells,
        "spread_return_pp": spread_pp,
        "best_cell": best["value"] if best else None,
        "best_cell_return_pp": best["avg_return_pct"] if best else None,
        "worst_cell": worst["value"] if worst else None,
        "worst_cell_return_pp": worst["avg_return_pct"] if worst else None,
        "has_separation": bool(has_sep),
    }


# --------------------------------------------------------------------------- #
# Orchestrierung
# --------------------------------------------------------------------------- #
def analyze(active: list[dict], n_buckets: int = _DEFAULT_BUCKETS) -> dict:
    score_ref = analyze_numeric(active, _SCORE_FEATURE, n_buckets, is_score_ref=True)
    numeric = [analyze_numeric(active, feat, n_buckets) for feat in _NUMERIC_FEATURES]
    categorical = [analyze_categorical(active, feat, gated=True) for feat in _CATEGORICAL_FEATURES]
    context = [analyze_categorical(active, feat, gated=False) for feat in _CONTEXT_CATEGORICAL]

    # Kandidaten-Ranking: orthogonale Features mit Edge/Separation, sortiert nach Staerke.
    candidates = []
    for r in numeric:
        if r["has_edge"]:
            candidates.append({
                "feature": r["feature"],
                "kind": "numeric",
                "strength": abs(r["rank_ic_return"] or 0.0),
                "metric": "rank_ic",
                "value": r["rank_ic_return"],
                "direction": r["direction"],
                "hint": _numeric_hint(r),
            })
    for r in categorical:
        if r["has_separation"]:
            candidates.append({
                "feature": r["feature"],
                "kind": "categorical",
                "strength": (r["spread_return_pp"] or 0.0) / 100.0,  # grobe Normierung -> vergleichbar mit IC-Skala
                "metric": "spread_return_pp",
                "value": r["spread_return_pp"],
                "direction": f"favor:{r['best_cell']} / avoid:{r['worst_cell']}",
                "hint": (f"bevorzuge {r['best_cell']} ({r['best_cell_return_pp']}pp), "
                         f"meide {r['worst_cell']} ({r['worst_cell_return_pp']}pp)"),
            })
    candidates.sort(key=lambda c: -c["strength"])

    orthogonal_edge_found = len(candidates) > 0
    score_ic = score_ref.get("rank_ic_return")
    best_orth_ic = max(
        (abs(r["rank_ic_return"]) for r in numeric if r["rank_ic_return"] is not None),
        default=None,
    )

    return {
        "n_active": len(active),
        "n_buckets": n_buckets,
        "ic_gate": _IC_GATE,
        "min_n": _MIN_N,
        "min_cell": _MIN_CELL,
        "spread_gate_pp": _SPREAD_GATE_PP,
        "score_reference": score_ref,
        "numeric": numeric,
        "categorical": categorical,
        "context_categorical": context,
        "candidate_filters": candidates,
        "verdict": {
            "orthogonal_edge_found": orthogonal_edge_found,
            "n_candidates": len(candidates),
            "score_reference_ic": score_ic,
            "best_orthogonal_abs_ic": round(best_orth_ic, 4) if best_orth_ic is not None else None,
            "orthogonal_beats_score": bool(
                best_orth_ic is not None and score_ic is not None
                and best_orth_ic > abs(score_ic)
            ),
            "headline": _headline(orthogonal_edge_found, candidates, score_ic, best_orth_ic),
        },
    }


def _numeric_hint(r: dict) -> str:
    buckets = r.get("buckets") or []
    if not buckets:
        return ""
    if r["direction"] == "high_better":
        top = buckets[-1]
        return f"hoehere {r['feature']} besser; Top-Bucket >= {top.get('value_lo')} (Exp {top.get('expectancy')})"
    bot = buckets[0]
    return f"niedrigere {r['feature']} besser; Bottom-Bucket <= {bot.get('value_hi')} (Exp {bot.get('expectancy')})"


def _headline(found: bool, candidates: list[dict], score_ic, best_orth_ic) -> str:
    if not found:
        return (f"KEIN orthogonaler Entry-Filter reisst sein Gate (best |IC|={best_orth_ic}, "
                f"Score-Referenz-IC={score_ic}). Rendite-Streuung sitzt nicht in den getesteten "
                f"Entry-Features: H2-Fokus zurueck auf die Exit-Seite (V7.21/23).")
    names = ", ".join(c["feature"] for c in candidates[:3])
    return (f"{len(candidates)} orthogonale(r) Filter-Kandidat(en) fuer H2: {names}. "
            f"Best orthogonal |IC|={best_orth_ic} vs Score-Referenz-IC={score_ic} "
            f"-> orthogonales Signal {'SCHLAEGT' if (best_orth_ic or 0) > abs(score_ic or 0) else 'schlaegt NICHT'} "
            f"die Ordnungs-Kraft des Scores. NUR Forward-Test-Hypothesen, KEINE Live-Regel.")


# --------------------------------------------------------------------------- #
# Report-Bau
# --------------------------------------------------------------------------- #
def build_report(csv_path: Path, n_buckets: int, run_date: str) -> dict:
    text = read_text_resilient(csv_path)
    if text is None:
        return {"error": "source_unreadable", "path": str(csv_path)}
    active = load_trades(text)
    if not active:
        return {"error": "no_active_trades", "path": str(csv_path)}
    body = analyze(active, n_buckets=n_buckets)
    return {
        "tool": "entry_filter_orthogonality",
        "version": "V7.26",
        "date": run_date,
        "source_csv": str(csv_path.name),
        **body,
    }


def render_md(rep: dict) -> str:
    L: list[str] = []
    L.append(f"# Entry-Filter-Orthogonality (V7.26): {rep.get('date')}")
    L.append("")
    L.append(f"**Quelle:** `{rep['source_csv']}` · aktive V6.1-Trades: **{rep['n_active']}** "
             f"· Buckets: {rep['n_buckets']} · IC-Gate: {rep['ic_gate']} · Spread-Gate: {rep['spread_gate_pp']}pp")
    L.append("")
    v = rep["verdict"]
    flag = "✅ EDGE-KANDIDATEN" if v["orthogonal_edge_found"] else "⚪️ KEIN ORTHOGONALER EDGE"
    L.append(f"## Verdikt: {flag}")
    L.append("")
    L.append(f"> {v['headline']}")
    L.append("")
    L.append(f"- **orthogonal_edge_found:** {v['orthogonal_edge_found']}  ·  Kandidaten: {v['n_candidates']}")
    L.append(f"- **Score-Referenz rank_ic:** {v['score_reference_ic']}  (V7.25-Befund: Score ist Gate, kein Ranker)")
    L.append(f"- **bestes orthogonales |rank_ic|:** {v['best_orthogonal_abs_ic']}  ·  "
             f"schlaegt Score: **{v['orthogonal_beats_score']}**")
    L.append("")

    if rep["candidate_filters"]:
        L.append("## H2-Forward-Test-Kandidaten (rangsortiert)")
        L.append("")
        L.append("| # | Feature | Art | Metrik | Wert | Richtung | Hinweis |")
        L.append("|---|---|---|---|---|---|---|")
        for i, c in enumerate(rep["candidate_filters"], 1):
            L.append(f"| {i} | {c['feature']} | {c['kind']} | {c['metric']} | {c['value']} "
                     f"| {c['direction']} | {c['hint']} |")
        L.append("")
        L.append("> ⚠️ Multiple-Testing: ~7 Features getestet, Gates NICHT Bonferroni-korrigiert. "
                 "Das sind **Hypothesen** fuer den H2-Forward-Test, KEINE bestaetigten Edges, "
                 "KEINE Live-Regel (eiserne Regel: Size/Hebel/Schwelle NIE vor H2).")
        L.append("")

    L.append("## Numerische Features")
    L.append("")
    L.append("| Feature | n | rank_ic(ret) | rank_ic(netto) | Mono | Top-Bottom-Exp | Richtung | Edge |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in [rep["score_reference"]] + rep["numeric"]:
        tag = " (REF)" if not r["is_orthogonal"] else ""
        L.append(f"| {r['feature']}{tag} | {r['n']} | {r['rank_ic_return']} | {r['rank_ic_netto']} "
                 f"| {r['bucket_monotonicity']} | {r['top_minus_bottom_expectancy']} "
                 f"| {r['direction']} | {'✅' if r['has_edge'] else '-'} |")
    L.append("")

    # Bucket-Detail nur fuer Edge-Features + Score-Referenz (Transparenz)
    detail_feats = [rep["score_reference"]] + [r for r in rep["numeric"] if r["has_edge"]]
    for r in detail_feats:
        if not r["buckets"]:
            continue
        tag = " (Score-Referenz)" if not r["is_orthogonal"] else ""
        L.append(f"### Buckets: {r['feature']}{tag}")
        L.append("")
        L.append("| Bucket | n | Wert-Range | WR | avg ret% | Expectancy € |")
        L.append("|---|---|---|---|---|---|")
        for b in r["buckets"]:
            L.append(f"| {b['bucket']} | {b['n']} | {b['value_lo']}-{b['value_hi']} "
                     f"| {b['win_rate']} | {b['avg_return_pct']} | {b['expectancy']} |")
        L.append("")

    L.append("## Kategoriale Features")
    L.append("")
    for r in rep["categorical"]:
        L.append(f"### {r['feature']}  ·  Separation: {'✅' if r['has_separation'] else '-'} "
                 f"(spread {r['spread_return_pp']}pp)")
        L.append("")
        L.append("| Wert | n | eligible | WR | avg ret% | Expectancy € |")
        L.append("|---|---|---|---|---|---|")
        for c in r["cells"]:
            L.append(f"| {c['value']} | {c['n']} | {'✓' if c['eligible'] else '·'} "
                     f"| {c['win_rate']} | {c['avg_return_pct']} | {c['expectancy']} |")
        L.append("")

    L.append("## Kontext (nicht gegatet)")
    L.append("")
    for r in rep["context_categorical"]:
        note = "near-konstant" if r["feature"] == "tier" else "temporal, kein Filter"
        L.append(f"### {r['feature']} ({note})")
        L.append("")
        L.append("| Wert | n | WR | avg ret% | Expectancy € |")
        L.append("|---|---|---|---|---|")
        for c in r["cells"]:
            L.append(f"| {c['value']} | {c['n']} | {c['win_rate']} | {c['avg_return_pct']} | {c['expectancy']} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append("_Read-only H2-Diagnostik (V7.26). Kein Live-Regel-/Schwellen-/Sizing-Change._")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def write_outputs(rep: dict, run_date: str) -> tuple[Path, Path]:
    json_path = _ROOT / "tools" / f"entry_filter_orthogonality_{run_date}.json"
    md_dir = _ROOT / "Briefings"
    md_dir.mkdir(exist_ok=True)
    md_path = md_dir / f"Entry-Filter-Orthogonality-{run_date}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(rep), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
_SYN_HEADER = ("date,ticker,regime,sector,tier,year,v6_score,rsi2,rsi5,rsi14,triple_rsi,"
               "atr_pct,return_pct,is_win,v61_skip,v61_exit_reason,v61_netto_pnl")


def _syn_row(i, regime, sector, rsi2, ret, netto, skip=False):
    win = "True" if ret > 0 else "False"
    return (f"2025-01-{(i % 27) + 1:02d},T{i},{regime},{sector},A,2025,6.5,{rsi2},"
            f"30,45,6,2.5,{ret},{win},{str(skip)},connors_sma5,{netto}")


def _make_synth() -> str:
    """Synthetik so gebaut, dass:
       - rsi2 ECHTE Edge hat (niedrig -> hoher Return; rank_ic stark negativ, low_better),
       - regime separiert (GoodReg >> BadReg),
       - es eine v61_skip=True-Zeile gibt, die rausfliegen MUSS,
       - der Score konstant 6.5 ist (kein Score-Rank moeglich -> Referenz ~None/0).
    """
    rows = [_SYN_HEADER]
    n = 60
    for i in range(n):
        # rsi2 von 1..95 monoton; Return invers (niedrige rsi2 = oversold = besserer Bounce)
        rsi2 = 1 + (i * 94) / (n - 1)
        ret = round((50 - rsi2) / 10.0, 3)           # rsi2 klein -> ret gross
        netto = round(ret * 10.0, 2)
        regime = "GoodReg" if i % 2 == 0 else "BadReg"
        # regime-Effekt zusaetzlich aufpraegen
        if regime == "GoodReg":
            ret2 = ret + 2.0
        else:
            ret2 = ret - 2.0
        netto2 = round(ret2 * 10.0, 2)
        rows.append(_syn_row(i, regime, "SecX", round(rsi2, 2), round(ret2, 3), netto2))
    # eine Zeile, die rausgefiltert werden MUSS
    rows.append(_syn_row(999, "GoodReg", "SecX", 1.0, 9.9, 99.0, skip=True))
    return "\n".join(rows) + "\n"


def selftest() -> int:
    fails: list[str] = []

    # 1) Spearman-Sanity
    if round(_spearman([1, 2, 3, 4], [10, 20, 30, 40]) or 0, 3) != 1.0:
        fails.append("spearman perfect-monotone != 1.0")
    if round(_spearman([1, 2, 3, 4], [40, 30, 20, 10]) or 0, 3) != -1.0:
        fails.append("spearman perfect-inverse != -1.0")

    # 2) v61_skip-Filter
    act = load_trades(_make_synth())
    if len(act) != 60:
        fails.append(f"skip-filter: expected 60 active, got {len(act)}")

    rep = analyze(act, n_buckets=4)

    # 3) rsi2 muss als Edge erkannt werden (low_better, IC stark negativ)
    rsi2 = next((r for r in rep["numeric"] if r["feature"] == "rsi2"), None)
    if not rsi2 or not rsi2["has_edge"]:
        fails.append(f"rsi2 edge not detected: {rsi2 and rsi2.get('rank_ic_return')}")
    elif rsi2["direction"] != "low_better":
        fails.append(f"rsi2 direction wrong: {rsi2['direction']}")

    # 4) regime muss separieren (GoodReg vs BadReg)
    reg = next((r for r in rep["categorical"] if r["feature"] == "regime"), None)
    if not reg or not reg["has_separation"]:
        fails.append(f"regime separation not detected: {reg and reg.get('spread_return_pp')}")
    elif reg["best_cell"] != "GoodReg" or reg["worst_cell"] != "BadReg":
        fails.append(f"regime best/worst wrong: {reg.get('best_cell')}/{reg.get('worst_cell')}")

    # 5) Verdikt + Kandidaten
    if not rep["verdict"]["orthogonal_edge_found"]:
        fails.append("verdict should find orthogonal edge on synthetic data")
    if rep["verdict"]["n_candidates"] < 2:
        fails.append(f"expected >=2 candidates, got {rep['verdict']['n_candidates']}")

    # 6) Score-Referenz: konstanter Score -> kein/instabiler Rank -> KEIN has_edge
    if rep["score_reference"]["has_edge"]:
        fails.append("constant score wrongly flagged as edge")

    # 7) Render darf nicht crashen
    try:
        md = render_md({**rep, "date": "selftest", "source_csv": "synthetic", "n_active": len(act)})
        if "Verdikt" not in md:
            fails.append("render_md missing Verdikt section")
    except Exception as e:  # noqa: BLE001
        fails.append(f"render_md crashed: {e}")

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  -", f)
        return 3
    print("SELFTEST PASS (7 Gruppen): spearman, skip-filter, rsi2-edge, "
          "regime-separation, verdict, score-ref-no-edge, render")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="ENTRY-FILTER-ORTHOGONALITY-1 (V7.26): H2 Entry-Filter-Diagnostik")
    ap.add_argument("--csv", default=str(_DEFAULT_CSV), help="Backtest-CSV (Default v6_backtest_results_v61.csv)")
    ap.add_argument("--buckets", type=int, default=_DEFAULT_BUCKETS, help="Quantil-Buckets je numerischem Feature")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="JSON + MD schreiben")
    ap.add_argument("--selftest", action="store_true", help="Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rep = build_report(Path(args.csv), args.buckets, args.date)
    if rep.get("error"):
        print(json.dumps(rep, ensure_ascii=False))
        return 2

    if args.write:
        jp, mp = write_outputs(rep, args.date)
        print(f"geschrieben: {jp}")
        print(f"geschrieben: {mp}")
    else:
        print(json.dumps(rep["verdict"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
