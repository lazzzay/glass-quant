#!/usr/bin/env python3
"""
ENTRY-FILTER-ROBUSTNESS-1 (V7.27 · 2026-06-19 Fr KW25 · FEATURE).

Read-only Robustheits-Gate fuer die ENTRY-Seite: der direkte, von V7.26
geforderte naechste Schritt. V7.26 ENTRY-FILTER-ORTHOGONALITY fand drei
orthogonale Filter-Kandidaten, die die (Null-)Ordnungskraft des Scores
schlagen (atr_pct +0,197 high_better; rsi14 -0,132 low_better; regime
spread 2,60pp), schrieb aber selbst die entscheidende Warnung:

    "Multiple-Testing NICHT korrigiert -> die Kandidaten sind
     H2-FORWARD-TEST-Hypothesen, KEINE bestaetigten Edges."

Dieses Tool schliesst genau diese Luecke. Es ist das ENTRY-seitige Gegenstueck
zu V7.23 EXIT-EDGE-DURABILITY (das die Exit-Edge connors_sma5 ueber Jahre auf
Stabilitaet prueft). Kernfrage (H2):

    **Sind die V7.26-Kandidaten ROBUST oder sind sie In-Sample-Artefakte aus
     dem gleichzeitigen Testen von ~5 Features?**

Drei orthogonale Robustheits-Pruefungen pro Kandidat:

  1. MULTIPLE-TESTING (Benjamini-Hochberg FDR).
     Spearman-rank_ic jedes der 5 numerischen orthogonalen Features bekommt
     einen zweiseitigen p-Wert (t-Approximation t = ic*sqrt((n-2)/(1-ic^2)),
     df=n-2, exakte regularisierte unvollstaendige Beta: stdlib-only). BH-FDR
     bei q (default 0,10) liefert je Feature `survives_fdr` + BH-adjustierten
     p-Wert. Beantwortet: ueberlebt die Edge die Korrektur fuer 5 Tests?

  2. TEMPORALE SUB-PERIODEN-KONSISTENZ (Analog V7.23).
     rank_ic pro Jahr (Jahre mit n>=MIN_YEAR_N) neu gerechnet -> Anteil der
     Jahre mit gleichem Vorzeichen wie die Voll-Sample-IC = `sign_consistency`.
     Plus schlechtestes-Jahr-Vorzeichen. Beantwortet: ist die Edge ein
     Einzeljahr-Artefakt oder zieht sie sich durch?

  3. BOOTSTRAP-VORZEICHEN-STABILITAET.
     B (default 1000) Resamples mit Zuruecklegen (fester Seed) -> rank_ic je
     Resample -> Anteil mit Voll-Sample-Vorzeichen = `bootstrap_sign_stability`
     + Perzentil-CI [2,5 ; 97,5]. Beantwortet: wie zufaellig ist das
     Vorzeichen? CI, das die 0 nicht einschliesst, ist starkes Signal.

Pro kategorialem Kandidaten (regime) eine analoge gerichtete Konsistenz:
  pro Jahr Expectancy der V7.26-bevorzugten vs. gemiedenen Zelle ->
  Anteil Jahre, in denen die bevorzugte >= gemiedene Zelle ist
  (`directional_consistency`): cross-validiert die Skip-Cohort ueber Zeit.

Maschinen-lesbares Verdikt
  edge_is_robust (je Kandidat)  TRUE nur wenn survives_fdr UND
                                sign_consistency >= TEMPORAL_GATE UND
                                bootstrap_sign_stability >= BOOTSTRAP_GATE.
  any_robust_edge               TRUE wenn >=1 Kandidat alle drei Gates reisst.
  reclassification              je V7.26-Kandidat: ROBUST | FRAGILE + Grund.

EHRLICHKEITS-GRENZE (zentral, nicht weglassen): auch "robust" heisst hier
IN-SAMPLE-robust: FDR + temporale CV + Bootstrap auf DEMSELBEN historischen
258-Trade-Korpus. Das ist KEIN Forward-Test (der braucht frische Forward-Daten).
Der Nutzen: Multiple-Testing-Artefakte werden ausgesiebt, damit der knappe
H2-Forward-Test-Slot nicht an Rauschen verschwendet wird. ROBUST hier =
"verdient den Forward-Test", NICHT "bestaetigte Live-Edge".

WICHTIG (eiserne Regel CLAUDE.md §3/§8): NUR Analyse. Aendert KEINE Live-Regel,
KEINE Schwelle, KEIN Sizing. Size/Hebel NIE vor H2.

V7.17-Pattern: EDEADLK-resilienter Reader (geteilt mit V7.26).

CLI:
    python3 tools/entry_filter_robustness.py
    python3 tools/entry_filter_robustness.py --bootstrap 2000 --fdr-q 0.05
    python3 tools/entry_filter_robustness.py --date 2026-06-19 --write
    python3 tools/entry_filter_robustness.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Quelle nicht lesbar (EDEADLK persistent / Datei fehlt / keine aktiven Trades)
    3  Selftest fail

Output JSON : tools/entry_filter_robustness_<date>.json
Output MD   : Briefings/Entry-Filter-Robustness-<date>.md

"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date as _date
from pathlib import Path
from typing import Optional

# Geteilte Primitive aus V7.26: Single-Source-of-Truth fuer Reader, Skip-Filter,
# Spearman und die Orthogonalitaets-Analyse (Konsistenz statt Copy-Paste).
import entry_filter_orthogonality as efo  # noqa: E402  (gleiches tools/-Verzeichnis)
from entry_filter_orthogonality import (  # noqa: E402
    _f,
    _spearman,
    _group_stats,
    load_trades,
    read_text_resilient,
)

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CSV = _ROOT / "v6_backtest_results_v61.csv"

# Familie der numerischen orthogonalen Features fuer die FDR-Korrektur
# (identisch zu V7.26 _NUMERIC_FEATURES; v6_score ist NUR Referenz, NICHT in der Familie).
_NUMERIC_FAMILY = list(efo._NUMERIC_FEATURES)

_FDR_Q = 0.10            # Benjamini-Hochberg False-Discovery-Rate
_TEMPORAL_GATE = 0.75    # Mindest-Anteil Jahre mit gleichem IC-Vorzeichen
_BOOTSTRAP_GATE = 0.90   # Mindest-Anteil Bootstrap-Resamples mit Voll-Sample-Vorzeichen
_BOOTSTRAP_B = 1000      # Anzahl Bootstrap-Resamples
_BOOTSTRAP_SEED = 20260619
_MIN_YEAR_N = 15         # Mindest-Trades, damit ein Jahr in die temporale CV eingeht
_MIN_N = efo._MIN_N      # Mindest-Trades fuer ein numerisches Feature ueberhaupt


# --------------------------------------------------------------------------- #
# Statistik: zweiseitiger Student-t p-Wert (stdlib, regularisierte inkompl. Beta)
# --------------------------------------------------------------------------- #
def _betacf(a: float, b: float, x: float) -> float:
    """Kettenbruch fuer die unvollstaendige Beta-Funktion (Numerical Recipes)."""
    MAXIT = 200
    EPS = 3.0e-12
    FPMIN = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularisierte unvollstaendige Beta-Funktion I_x(a,b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> Optional[float]:
    """Zweiseitiger p-Wert einer t-Statistik: p = I_{df/(df+t^2)}(df/2, 1/2)."""
    if df <= 0:
        return None
    x = df / (df + t * t)
    return _betai(df / 2.0, 0.5, x)


def spearman_p(ic: Optional[float], n: int) -> Optional[float]:
    """Zweiseitiger p-Wert fuer eine Spearman-rank_ic via t-Approximation."""
    if ic is None or n < 4:
        return None
    if abs(ic) >= 1.0:
        return 0.0
    t = ic * math.sqrt((n - 2) / (1.0 - ic * ic))
    return t_two_sided_p(t, n - 2)


# --------------------------------------------------------------------------- #
# Benjamini-Hochberg FDR
# --------------------------------------------------------------------------- #
def benjamini_hochberg(pvals: dict[str, Optional[float]], q: float) -> dict[str, dict]:
    """BH-FDR ueber benannte p-Werte. Liefert je Schluessel:
       {p, p_adj, survives}. None-p-Werte werden ausgenommen (survives=None)."""
    valid = {k: v for k, v in pvals.items() if v is not None}
    m = len(valid)
    out: dict[str, dict] = {k: {"p": pvals[k], "p_adj": None, "survives": None}
                            for k in pvals}
    if m == 0:
        return out
    ordered = sorted(valid.items(), key=lambda kv: kv[1])  # aufsteigend nach p
    # groesstes k mit p(k) <= (k/m)*q -> alle <= k werden rejected
    max_k = 0
    for i, (_, p) in enumerate(ordered, start=1):
        if p <= (i / m) * q:
            max_k = i
    rejected = {ordered[i][0] for i in range(max_k)}
    # BH-adjustierte p-Werte (monoton von hinten)
    adj_running = 1.0
    adj: dict[str, float] = {}
    for i in range(m, 0, -1):
        k, p = ordered[i - 1]
        cand = min(1.0, p * m / i)
        adj_running = min(adj_running, cand)
        adj[k] = adj_running
    for k in valid:
        out[k]["p_adj"] = round(adj[k], 6)
        out[k]["survives"] = k in rejected
    return out


# --------------------------------------------------------------------------- #
# Numerische Robustheit: IC, temporale CV, Bootstrap
# --------------------------------------------------------------------------- #
def _ic_return(rows: list[dict], feature: str) -> tuple[Optional[float], int]:
    """Spearman rank_ic Feature <-> return_pct + n der nutzbaren Paare."""
    pairs = [(_f(r.get(feature)), _f(r.get("return_pct"))) for r in rows]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(pairs) < 3:
        return None, len(pairs)
    return _spearman([a for a, _ in pairs], [b for _, b in pairs]), len(pairs)


def temporal_consistency(active: list[dict], feature: str,
                         full_ic: Optional[float]) -> dict:
    """rank_ic pro Jahr; Anteil Jahre mit gleichem Vorzeichen wie full_ic."""
    years: dict[str, list[dict]] = {}
    for r in active:
        y = str(r.get("year", "")).strip() or "?"
        years.setdefault(y, []).append(r)
    per_year = []
    for y in sorted(years):
        ic, n = _ic_return(years[y], feature)
        if n >= _MIN_YEAR_N and ic is not None:
            per_year.append({"year": y, "n": n, "rank_ic": round(ic, 4)})
    sign = (1 if (full_ic or 0) > 0 else -1) if full_ic is not None else 0
    same = [p for p in per_year if (1 if p["rank_ic"] > 0 else -1) == sign]
    consistency = (len(same) / len(per_year)) if per_year else None
    worst = min((p["rank_ic"] for p in per_year), default=None) if sign > 0 else \
        max((p["rank_ic"] for p in per_year), default=None)
    return {
        "per_year": per_year,
        "n_years": len(per_year),
        "sign_consistency": round(consistency, 3) if consistency is not None else None,
        "worst_year_ic": round(worst, 4) if worst is not None else None,
        "full_sign": sign,
    }


def bootstrap_stability(active: list[dict], feature: str, full_ic: Optional[float],
                        B: int, seed: int) -> dict:
    """Bootstrap-Resampling der rank_ic -> Vorzeichen-Stabilitaet + Perzentil-CI."""
    pairs = [(_f(r.get(feature)), _f(r.get("return_pct"))) for r in active]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    if n < _MIN_N or full_ic is None:
        return {"n": n, "sign_stability": None, "ci_low": None, "ci_high": None,
                "n_resamples": 0}
    rng = random.Random(seed)
    sign = 1 if full_ic > 0 else -1
    ics: list[float] = []
    for _ in range(B):
        xs = []
        ys = []
        for _ in range(n):
            a, b = pairs[rng.randrange(n)]
            xs.append(a)
            ys.append(b)
        ic = _spearman(xs, ys)
        if ic is not None:
            ics.append(ic)
    if not ics:
        return {"n": n, "sign_stability": None, "ci_low": None, "ci_high": None,
                "n_resamples": 0}
    same = sum(1 for v in ics if (1 if v > 0 else -1) == sign)
    ics.sort()
    lo = ics[max(0, int(0.025 * len(ics)) - 1)]
    hi = ics[min(len(ics) - 1, int(0.975 * len(ics)))]
    ci_excludes_zero = (lo > 0 and hi > 0) or (lo < 0 and hi < 0)
    return {
        "n": n,
        "n_resamples": len(ics),
        "sign_stability": round(same / len(ics), 3),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "ci_excludes_zero": bool(ci_excludes_zero),
    }


# --------------------------------------------------------------------------- #
# Kategoriale Robustheit (regime): gerichtete Konsistenz ueber Jahre
# --------------------------------------------------------------------------- #
def regime_directional_consistency(active: list[dict], favor: str, avoid: str) -> dict:
    """Pro Jahr: ist Expectancy(favor) >= Expectancy(avoid)?  Anteil = Konsistenz."""
    years: dict[str, list[dict]] = {}
    for r in active:
        y = str(r.get("year", "")).strip() or "?"
        years.setdefault(y, []).append(r)
    per_year = []
    holds = 0
    counted = 0
    for y in sorted(years):
        rows = years[y]
        fav = [r for r in rows if str(r.get("regime", "")).strip() == favor]
        avd = [r for r in rows if str(r.get("regime", "")).strip() == avoid]
        if len(fav) < 3 or len(avd) < 3:
            per_year.append({"year": y, "favor_n": len(fav), "avoid_n": len(avd),
                             "favor_exp": None, "avoid_exp": None, "holds": None})
            continue
        fe = _group_stats(fav)["expectancy"]
        ae = _group_stats(avd)["expectancy"]
        h = (fe is not None and ae is not None and fe >= ae)
        counted += 1
        holds += 1 if h else 0
        per_year.append({"year": y, "favor_n": len(fav), "avoid_n": len(avd),
                         "favor_exp": fe, "avoid_exp": ae, "holds": bool(h)})
    return {
        "favor": favor,
        "avoid": avoid,
        "per_year": per_year,
        "n_years_counted": counted,
        "directional_consistency": round(holds / counted, 3) if counted else None,
    }


# --------------------------------------------------------------------------- #
# Orchestrierung
# --------------------------------------------------------------------------- #
def analyze(active: list[dict], *, buckets: int = efo._DEFAULT_BUCKETS,
            fdr_q: float = _FDR_Q, B: int = _BOOTSTRAP_B,
            seed: int = _BOOTSTRAP_SEED) -> dict:
    # 1) V7.26-Orthogonalitaet als Ausgangspunkt (gleiche Engine).
    base = efo.analyze(active, n_buckets=buckets)
    base_numeric = {r["feature"]: r for r in base["numeric"]}

    # 2) FDR ueber die numerische Familie (Voll-Sample IC -> p-Wert).
    family_ic = {}
    family_n = {}
    family_p = {}
    for feat in _NUMERIC_FAMILY:
        ic, n = _ic_return(active, feat)
        family_ic[feat] = round(ic, 4) if ic is not None else None
        family_n[feat] = n
        family_p[feat] = spearman_p(ic, n)
    fdr = benjamini_hochberg(family_p, fdr_q)

    # 3) Pro V7.26-Kandidat (numerisch has_edge) die volle Robustheit.
    numeric_candidates = [r["feature"] for r in base["numeric"] if r["has_edge"]]
    cand_reports = []
    for feat in _NUMERIC_FAMILY:
        ic = family_ic[feat]
        is_cand = feat in numeric_candidates
        temporal = temporal_consistency(active, feat, ic) if is_cand else None
        boot = bootstrap_stability(active, feat, ic, B, seed) if is_cand else None
        survives_fdr = fdr[feat]["survives"]
        robust = None
        reasons = []
        if is_cand:
            tc = temporal["sign_consistency"] if temporal else None
            bs = boot["sign_stability"] if boot else None
            passes_fdr = bool(survives_fdr)
            passes_temporal = bool(tc is not None and tc >= _TEMPORAL_GATE)
            passes_boot = bool(bs is not None and bs >= _BOOTSTRAP_GATE)
            robust = passes_fdr and passes_temporal and passes_boot
            if not passes_fdr:
                reasons.append(f"faellt FDR (p_adj={fdr[feat]['p_adj']} > q={fdr_q})")
            if not passes_temporal:
                reasons.append(f"temporal inkonsistent ({tc} < {_TEMPORAL_GATE})")
            if not passes_boot:
                reasons.append(f"Bootstrap-Vorzeichen instabil ({bs} < {_BOOTSTRAP_GATE})")
            if robust:
                reasons.append("alle 3 Gates bestanden")
        cand_reports.append({
            "feature": feat,
            "is_v726_candidate": is_cand,
            "full_rank_ic": ic,
            "n": family_n[feat],
            "direction": base_numeric.get(feat, {}).get("direction"),
            "fdr": fdr[feat],
            "temporal": temporal,
            "bootstrap": boot,
            "edge_is_robust": robust,
            "reasons": reasons,
        })

    # 4) regime-Kandidat (falls V7.26 Separation fand): gerichtete Konsistenz.
    regime_report = None
    regime_base = next((r for r in base["categorical"]
                        if r["feature"] == "regime"), None)
    if regime_base and regime_base.get("has_separation"):
        favor = regime_base["best_cell"]
        avoid = regime_base["worst_cell"]
        rc = regime_directional_consistency(active, favor, avoid)
        dc = rc["directional_consistency"]
        rc["edge_is_robust"] = bool(dc is not None and dc >= _TEMPORAL_GATE)
        rc["is_v726_candidate"] = True
        rc["full_spread_pp"] = regime_base["spread_return_pp"]
        regime_report = rc

    # 5) Verdikt
    robust_numeric = [c["feature"] for c in cand_reports if c["edge_is_robust"]]
    robust_regime = bool(regime_report and regime_report["edge_is_robust"])
    any_robust = bool(robust_numeric or robust_regime)

    reclass = []
    for c in cand_reports:
        if c["is_v726_candidate"]:
            reclass.append({
                "feature": c["feature"],
                "class": "ROBUST" if c["edge_is_robust"] else "FRAGILE",
                "why": "; ".join(c["reasons"]),
            })
    if regime_report:
        reclass.append({
            "feature": "regime",
            "class": "ROBUST" if regime_report["edge_is_robust"] else "FRAGILE",
            "why": (f"gerichtete Jahres-Konsistenz {regime_report['directional_consistency']} "
                    f"({'>=' if robust_regime else '<'} {_TEMPORAL_GATE}); "
                    f"favor {regime_report['favor']} / avoid {regime_report['avoid']}"),
        })

    return {
        "n_active": len(active),
        "params": {
            "fdr_q": fdr_q, "temporal_gate": _TEMPORAL_GATE,
            "bootstrap_gate": _BOOTSTRAP_GATE, "bootstrap_B": B,
            "bootstrap_seed": seed, "min_year_n": _MIN_YEAR_N,
        },
        "numeric_family_fdr": {f: {"rank_ic": family_ic[f], "n": family_n[f],
                                   **fdr[f]} for f in _NUMERIC_FAMILY},
        "candidates": cand_reports,
        "regime": regime_report,
        "verdict": {
            "any_robust_edge": any_robust,
            "robust_numeric": robust_numeric,
            "robust_regime": robust_regime,
            "reclassification": reclass,
            "headline": _headline(any_robust, robust_numeric, robust_regime, reclass),
        },
    }


def _headline(any_robust, robust_numeric, robust_regime, reclass) -> str:
    n_robust = len(robust_numeric) + (1 if robust_regime else 0)
    n_total = len(reclass)
    if not any_robust:
        return (f"KEIN V7.26-Kandidat ueberlebt FDR + temporale CV + Bootstrap "
                f"(0/{n_total} robust): die Orthogonalitaets-Treffer sind unter "
                f"Mehrfachtest-Korrektur In-Sample-Artefakte. KEIN Forward-Test-Slot "
                f"verbrennen. Eiserne Regel: kein Live-Change vor H2.")
    names = ", ".join(robust_numeric + (["regime"] if robust_regime else []))
    return (f"{n_robust}/{n_total} V7.26-Kandidat(en) IN-SAMPLE-robust: {names}. "
            f"Diese verdienen den knappen H2-FORWARD-TEST-Slot (FDR + Jahres-Konsistenz "
            f"+ Bootstrap bestanden). ROBUST != bestaetigte Live-Edge: der Forward-Test "
            f"auf frischen Daten bleibt Pflicht vor jeder Regel/Schwelle/Sizing.")


# --------------------------------------------------------------------------- #
# Report-Bau
# --------------------------------------------------------------------------- #
def build_report(csv_path: Path, *, buckets: int, fdr_q: float, B: int,
                 seed: int, run_date: str) -> dict:
    text = read_text_resilient(csv_path)
    if text is None:
        return {"error": "source_unreadable", "path": str(csv_path)}
    active = load_trades(text)
    if not active:
        return {"error": "no_active_trades", "path": str(csv_path)}
    body = analyze(active, buckets=buckets, fdr_q=fdr_q, B=B, seed=seed)
    return {
        "tool": "entry_filter_robustness",
        "version": "V7.27",
        "date": run_date,
        "source_csv": str(csv_path.name),
        **body,
    }


def render_md(rep: dict) -> str:
    L: list[str] = []
    p = rep["params"]
    L.append(f"# Entry-Filter-Robustness (V7.27): {rep.get('date')}")
    L.append("")
    L.append(f"**Quelle:** `{rep['source_csv']}` · aktive V6.1-Trades: **{rep['n_active']}** "
             f"· FDR q={p['fdr_q']} · temporal-Gate={p['temporal_gate']} "
             f"· Bootstrap-Gate={p['bootstrap_gate']} (B={p['bootstrap_B']}, seed={p['bootstrap_seed']})")
    L.append("")
    v = rep["verdict"]
    flag = "✅ ROBUSTE KANDIDATEN" if v["any_robust_edge"] else "⚪️ ALLE KANDIDATEN FRAGIL"
    L.append(f"## Verdikt: {flag}")
    L.append("")
    L.append(f"> {v['headline']}")
    L.append("")

    L.append("### Re-Klassifikation der V7.26-Kandidaten")
    L.append("")
    L.append("| Feature | Klasse | Begruendung |")
    L.append("|---|---|---|")
    for rc in v["reclassification"]:
        L.append(f"| {rc['feature']} | **{rc['class']}** | {rc['why']} |")
    L.append("")

    L.append("## 1) Multiple-Testing: Benjamini-Hochberg FDR (numerische Familie)")
    L.append("")
    L.append("| Feature | n | rank_ic | p | p_adj (BH) | survives FDR |")
    L.append("|---|---|---|---|---|---|")
    for f, d in rep["numeric_family_fdr"].items():
        sv = "✅" if d.get("survives") else ("-" if d.get("survives") is not None else "n/a")
        L.append(f"| {f} | {d['n']} | {d['rank_ic']} | "
                 f"{_fmt(d.get('p'))} | {_fmt(d.get('p_adj'))} | {sv} |")
    L.append("")
    L.append(f"> FDR korrigiert fuer {len(rep['numeric_family_fdr'])} gleichzeitig getestete "
             f"Features: genau die von V7.26 offengelassene Luecke.")
    L.append("")

    L.append("## 2) + 3) Temporale Konsistenz & Bootstrap (nur V7.26-Kandidaten)")
    L.append("")
    L.append("| Feature | Richtung | full IC | survives FDR | Jahres-Sign-Konsistenz | worst-Jahr IC | Bootstrap-Sign | CI [2,5;97,5] | robust |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for c in rep["candidates"]:
        if not c["is_v726_candidate"]:
            continue
        t = c["temporal"] or {}
        b = c["bootstrap"] or {}
        ci = f"[{b.get('ci_low')};{b.get('ci_high')}]" if b.get("ci_low") is not None else "-"
        L.append(f"| {c['feature']} | {c['direction']} | {c['full_rank_ic']} "
                 f"| {'✅' if c['fdr'].get('survives') else '-'} "
                 f"| {t.get('sign_consistency')} ({t.get('n_years')}J) "
                 f"| {t.get('worst_year_ic')} | {b.get('sign_stability')} | {ci} "
                 f"| {'✅' if c['edge_is_robust'] else '❌'} |")
    L.append("")

    # Jahres-Detail je Kandidat
    for c in rep["candidates"]:
        if not c["is_v726_candidate"] or not c["temporal"]:
            continue
        L.append(f"### {c['feature']}: rank_ic pro Jahr")
        L.append("")
        L.append("| Jahr | n | rank_ic |")
        L.append("|---|---|---|")
        for py in c["temporal"]["per_year"]:
            L.append(f"| {py['year']} | {py['n']} | {py['rank_ic']} |")
        L.append("")

    if rep.get("regime"):
        r = rep["regime"]
        L.append("## regime: gerichtete Jahres-Konsistenz (Skip-Cohort-Cross-Validation)")
        L.append("")
        L.append(f"favor **{r['favor']}** vs avoid **{r['avoid']}** · "
                 f"directional_consistency = **{r['directional_consistency']}** "
                 f"· robust: {'✅' if r['edge_is_robust'] else '❌'}")
        L.append("")
        L.append("| Jahr | favor n | avoid n | favor Exp € | avoid Exp € | favor>=avoid |")
        L.append("|---|---|---|---|---|---|")
        for py in r["per_year"]:
            h = "✓" if py["holds"] else ("·" if py["holds"] is None else "✗")
            L.append(f"| {py['year']} | {py['favor_n']} | {py['avoid_n']} "
                     f"| {py['favor_exp']} | {py['avoid_exp']} | {h} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append("> **Ehrlichkeits-Grenze:** „robust\" heisst hier IN-SAMPLE-robust "
             "(FDR + temporale CV + Bootstrap auf demselben 258-Trade-Korpus). Das ist "
             "KEIN Forward-Test. Nutzen: Mehrfachtest-Artefakte aussieben, damit der "
             "H2-Forward-Test-Slot nicht an Rauschen verschwendet wird. "
             "ROBUST = „verdient den Forward-Test\", NICHT „bestaetigte Live-Edge\".")
    L.append("")
    L.append("_Read-only H2-Diagnostik (V7.27). Kein Live-Regel-/Schwellen-/Sizing-Change._")
    return "\n".join(L)


def _fmt(x) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float) and x < 1e-4 and x > 0:
        return f"{x:.2e}"
    return str(x)


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def write_outputs(rep: dict, run_date: str) -> tuple[Path, Path]:
    json_path = _ROOT / "tools" / f"entry_filter_robustness_{run_date}.json"
    md_dir = _ROOT / "Briefings"
    md_dir.mkdir(exist_ok=True)
    md_path = md_dir / f"Entry-Filter-Robustness-{run_date}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(rep), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
_SYN_HEADER = ("date,ticker,regime,sector,tier,year,v6_score,rsi2,rsi5,rsi14,triple_rsi,"
               "atr_pct,return_pct,is_win,v61_skip,v61_exit_reason,v61_netto_pnl")


def _syn_make() -> str:
    """Synthetik mit zwei ROBUSTEN Faellen:
       - rsi2: ECHTE, ueber alle Jahre konsistente, starke Edge (low_better) -> ROBUST.
       - regime: GoodReg >> BadReg jedes Jahr -> ROBUST.
       atr/rsi5/rsi14/triple_rsi sind reines Rauschen (kein Effekt) -> fallen
       korrekt aus FDR + Kandidaten-Set. v6_score konstant (kein Rank).
       Eine v61_skip=True-Zeile muss rausfallen.
    """
    import random as _r
    rng = _r.Random(7)
    rows = [_SYN_HEADER]
    years = ["2023", "2024", "2025", "2026"]
    idx = 0
    for y in years:
        for k in range(40):  # 160 Trades, 40/Jahr
            # rsi2: niedrig -> hoher Return, jedes Jahr gleiche Richtung (robuste Edge)
            rsi2 = 1 + (k * 94) / 39.0
            base = (50.0 - rsi2) / 8.0
            atr = round(rng.uniform(1.0, 6.0), 3)        # reines Rauschen
            regime = "GoodReg" if k % 2 == 0 else "BadReg"
            reg_effect = 1.5 if regime == "GoodReg" else -1.5
            noise = rng.uniform(-0.3, 0.3)
            ret = round(base + reg_effect + noise, 3)
            netto = round(ret * 10.0, 2)
            win = "True" if ret > 0 else "False"
            rows.append(f"2025-01-01,T{idx},{regime},SecX,A,{y},6.5,{round(rsi2,2)},"
                        f"30,45,6,{atr},{ret},{win},False,connors_sma5,{netto}")
            idx += 1
    # Skip-Zeile
    rows.append(f"2025-01-01,SKIP,GoodReg,SecX,A,2025,6.5,1,30,45,6,2.0,99.0,True,True,connors_sma5,990.0")
    return "\n".join(rows) + "\n"


def _syn_fragile() -> str:
    """Synthetik fuer den FRAGILE-Kandidaten-Pfad: atr_pct ist der DOMINANTE
    Treiber (kein rsi2-Wettbewerb) -> full-sample-IC reisst das V7.26-Gate
    (high_better, monotone Buckets) -> wird Kandidat. ABER das Jahres-Vorzeichen
    FLIPPT (2023/25 positiv, 2024/26 negativ) -> sign_consistency 0.5 < 0.75 ->
    edge_is_robust = False. Genau der Pfad „Kandidat, aber nicht robust"."""
    import random as _r
    rng = _r.Random(11)
    rows = [_SYN_HEADER]
    years = ["2023", "2024", "2025", "2026"]
    # netto stark positiv (full-IC reisst Gate), 2 von 4 Jahren mit Gegen-Vorzeichen
    ycoef = {"2023": 2.5, "2024": -1.0, "2025": 2.5, "2026": -1.0}
    idx = 0
    for y in years:
        for k in range(40):
            atr = round(rng.uniform(1.0, 6.0), 3)
            ret = round(ycoef[y] * (atr - 3.5) + rng.uniform(-0.2, 0.2), 3)
            netto = round(ret * 10.0, 2)
            win = "True" if ret > 0 else "False"
            # rsi2/regime flach -> keine konkurrierende Edge
            rows.append(f"2025-01-01,F{idx},Flat,SecX,A,{y},6.5,50,"
                        f"50,50,50,{atr},{ret},{win},False,connors_sma5,{netto}")
            idx += 1
    return "\n".join(rows) + "\n"


def selftest() -> int:
    fails: list[str] = []

    # 1) t-p-Wert Sanity: t=0 -> p=1; grosses t -> p~0; bekannte Referenz df=10,t=2.228~0.05
    if not (abs((t_two_sided_p(0.0, 10) or -1) - 1.0) < 1e-6):
        fails.append("t_two_sided_p(0,df) != 1.0")
    pbig = t_two_sided_p(50.0, 100) or 1.0
    if pbig > 1e-6:
        fails.append(f"t_two_sided_p(50,100) not ~0: {pbig}")
    pref = t_two_sided_p(2.228, 10) or 0.0
    if not (0.045 < pref < 0.055):
        fails.append(f"t-ref df=10 t=2.228 expected ~0.05, got {pref}")

    # 2) BH-FDR: 1 starkes Signal + 4 Null-Signale -> nur das starke survived
    pv = {"strong": 0.0001, "n1": 0.4, "n2": 0.6, "n3": 0.8, "n4": 0.95}
    bh = benjamini_hochberg(pv, 0.10)
    if not bh["strong"]["survives"]:
        fails.append("BH: strong signal should survive")
    if any(bh[k]["survives"] for k in ("n1", "n2", "n3", "n4")):
        fails.append("BH: null signals should not survive")
    # monotone p_adj >= p
    if bh["strong"]["p_adj"] < bh["strong"]["p"]:
        fails.append("BH: p_adj must be >= p")

    # 3) Korpus laden + analyze auf Synthetik
    act = load_trades(_syn_make())
    if len(act) != 160:
        fails.append(f"skip-filter: expected 160 active, got {len(act)}")
    rep = analyze(act, buckets=4, B=300, seed=1)

    # 4a) rsi2 muss ROBUST sein (konsistente Edge ueber alle Jahre)
    cmap = {c["feature"]: c for c in rep["candidates"]}
    if "rsi2" in cmap and cmap["rsi2"]["is_v726_candidate"]:
        if not cmap["rsi2"]["edge_is_robust"]:
            fails.append(f"rsi2 should be robust: {cmap['rsi2']['reasons']}")
    else:
        fails.append("rsi2 was not flagged as V7.26 candidate on synth")

    # 4b) FRAGILE-Pfad: atr_pct ist Kandidat, aber Sign-Flip ueber Jahre -> NICHT robust
    rep_f = analyze(load_trades(_syn_fragile()), buckets=4, B=300, seed=1)
    cf = {c["feature"]: c for c in rep_f["candidates"]}
    atr = cf.get("atr_pct")
    if not atr or not atr["is_v726_candidate"]:
        fails.append("atr_pct should be a V7.26 candidate on fragile synth")
    elif atr["edge_is_robust"]:
        tc = (atr["temporal"] or {}).get("sign_consistency")
        fails.append(f"atr_pct should be FRAGILE (sign-flip), consistency={tc}")

    # 5) regime robust (GoodReg jedes Jahr > BadReg)
    if rep.get("regime") is not None and not rep["regime"]["edge_is_robust"]:
        fails.append(f"regime should be robust: {rep['regime']['directional_consistency']}")

    # 6) Verdikt + Re-Klassifikation vorhanden
    if not rep["verdict"]["any_robust_edge"]:
        fails.append("verdict should find >=1 robust edge on synth")
    if not rep["verdict"]["reclassification"]:
        fails.append("reclassification empty")

    # 7) spearman_p None-sicher + render crasht nicht
    if spearman_p(None, 100) is not None:
        fails.append("spearman_p(None) should be None")
    try:
        md = render_md({**rep, "date": "selftest", "source_csv": "synthetic"})
        if "Verdikt" not in md or "FDR" not in md:
            fails.append("render_md missing key sections")
    except Exception as e:  # noqa: BLE001
        fails.append(f"render_md crashed: {e}")

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  -", f)
        return 3
    print("SELFTEST PASS (7 Gruppen): t-pvalue, BH-FDR, skip-filter, "
          "rsi2-robust+atr-fragile, regime-robust, verdict, render")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="ENTRY-FILTER-ROBUSTNESS-1 (V7.27): H2 Robustheits-Gate (FDR + temporale CV + Bootstrap)")
    ap.add_argument("--csv", default=str(_DEFAULT_CSV), help="Backtest-CSV (Default v6_backtest_results_v61.csv)")
    ap.add_argument("--buckets", type=int, default=efo._DEFAULT_BUCKETS, help="Quantil-Buckets (an V7.26 weitergereicht)")
    ap.add_argument("--fdr-q", type=float, default=_FDR_Q, help="Benjamini-Hochberg FDR q (Default 0.10)")
    ap.add_argument("--bootstrap", type=int, default=_BOOTSTRAP_B, help="Anzahl Bootstrap-Resamples")
    ap.add_argument("--seed", type=int, default=_BOOTSTRAP_SEED, help="Bootstrap-Seed (Reproduzierbarkeit)")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="JSON + MD schreiben")
    ap.add_argument("--selftest", action="store_true", help="Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rep = build_report(Path(args.csv), buckets=args.buckets, fdr_q=args.fdr_q,
                       B=args.bootstrap, seed=args.seed, run_date=args.date)
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
