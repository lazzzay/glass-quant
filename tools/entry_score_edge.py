#!/usr/bin/env python3
"""
ENTRY-SCORE-EDGE-1 (V7.25 · 2026-06-17 Mi KW25 · FEATURE).

Read-only Edge-Diagnostik fuer die ENTRY-Seite der V6.1-Strategie: das
Gegenstueck zu V7.21 EXIT-ATTRIBUTION (wo sitzt die Exit-Edge) und V7.23
EXIT-DURABILITY (ist sie stabil). Die Exit-Seite ist damit gut vermessen; die
ENTRY-Seite war es nicht.

Kernfrage (H2): **Verdient der Entry-Score (v6_score) seine Rendite, sagt ein
hoeherer Score wirklich ein besseres Ergebnis voraus, oder ist der Score nur ein
Gate (pass/fail), aber KEIN Ranker?**

Die System-Tiefenanalyse (01.06.) berichtete Score<->Return r=0,04 (praktisch
keine lineare Korrelation). Korrelation ist aber nicht die ganze Geschichte:
es koennte einen Schwellen-/Decile-Effekt geben. Dieses Tool quantifiziert das
sauber auf dem V6.1-aktiven Backtest-Korpus (`v6_backtest_results_v61.csv`,
gleiche `v61_skip`-Filterung wie exit_attribution):

  1. rank_ic        Spearman-Rang-Korrelation v6_score <-> return_pct
                    (+ Sekundaer gegen v61_netto_pnl). Der ehrliche
                    "Information-Coefficient" der Score-Edge.
  2. pearson_r      lineare Korrelation (Cross-Check der gemeldeten r=0,04).
  3. bucket_lift    Score in N Quantil-Buckets -> je n / WR / avg return_pct /
                    sum+avg v61_netto_pnl / Expectancy. Steigt die Expectancy
                    mit dem Score (Lift) oder ist sie flach?
  4. threshold_sweep  fuer Live-Schwellen-Kandidaten [6.5,7.0,7.5,8.0,8.5]:
                    Teilmenge score>=t -> n / WR / avg return_pct / Expectancy.
                    Deckt die "Score-Pass-Plateau"-Frage auf (V7.9.15/16): die
                    Live-Schwellen 7,0/8,5 werden im Korpus kaum/nie erreicht.
  5. reachability   Anteil aktiver Trades >= 7,0 bzw. >= 8,5 (Action-Buy /
                    High-Conviction). Headline-Metrik fuer die Threshold-Lage.

Maschinen-lesbares H2-Verdikt:
  - score_is_predictive   TRUE nur wenn |rank_ic|>=IC_GATE UND Top-Bucket-
                          Expectancy > Bottom-Bucket UND Bucket-Expectancy
                          monoton-ish (Spearman(bucket_idx, expectancy)>0).
                          Sonst FALSE = "Score ist Gate, kein Ranker".
  - best_threshold_by_expectancy  rein advisorisch (nur Schwellen mit n>=MIN_N).
  - live_threshold_reachability   reach@7.0 / reach@8.5.

WICHTIG (eiserne Regel CLAUDE.md): Dieses Tool ist NUR Analyse. Es aendert KEINE
Live-Regel, KEINE Schwelle, KEIN Sizing. Output ist H2-Panel-Input; jede echte
Regel-/Schwellen-Aenderung wartet auf die saubere H2-Re-Validierung mit
Forward-Daten (Feature-Freeze).

V7.17-Pattern: EDEADLK-resilienter Reader (Sandbox-Mount wirft sporadisch
Errno 35 'Resource deadlock avoided'); nativ auf dem Mac irrelevant.

CLI:
    python3 tools/entry_score_edge.py
    python3 tools/entry_score_edge.py --csv v6_backtest_results_v61.csv
    python3 tools/entry_score_edge.py --buckets 10
    python3 tools/entry_score_edge.py --date 2026-06-17 --write
    python3 tools/entry_score_edge.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Quelle nicht lesbar (EDEADLK persistent / Datei fehlt / keine aktiven Trades)
    3  Selftest fail

Output JSON : tools/entry_score_edge_<date>.json
Output MD   : Briefings/Entry-Score-Edge-<date>.md

"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import date as _date, datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"
DEFAULT_CSV = REPO_ROOT / "v6_backtest_results_v61.csv"

VERSION = "V7.25"

# Live-Schwellen-Kandidaten (CLAUDE.md §3: Action-Buy 7,0 / High-Conviction 8,5).
_THRESHOLD_CANDIDATES = [6.5, 7.0, 7.5, 8.0, 8.5]
# Gate fuer "Score sagt etwas voraus": Information-Coefficient-Mindestbetrag.
_IC_GATE = 0.10
# Mindest-n, damit eine Schwellen-Teilmenge als belastbar gilt.
_MIN_N = 20
_DEFAULT_BUCKETS = 5  # Quintile bei n~258 -> ~51/Bucket


# --------------------------------------------------------------------------- #
# IO (EDEADLK-resilient, V7.17-Pattern)
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

    Gleiche Logik wie tools/exit_attribution.py: v61_skip == True -> Trade ist
    im V6.1-Regime ausgeschlossen (Tier-X etc.) und faellt raus.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    active = []
    for r in reader:
        if str(r.get("v61_skip", "")).strip().lower() == "true":
            continue
        active.append(r)
    return active


# --------------------------------------------------------------------------- #
# Statistik (stdlib-only)
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
        avg = (i + j) / 2.0 + 1.0  # 1-basiert, Mittel der Tie-Positionen
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


def _bucketize(pairs: list[tuple[float, dict]], n_buckets: int) -> list[list[dict]]:
    """Sortiert nach Score aufsteigend und teilt in ~gleich grosse Buckets."""
    pairs = sorted(pairs, key=lambda p: p[0])
    total = len(pairs)
    buckets: list[list[dict]] = []
    for b in range(n_buckets):
        lo = (b * total) // n_buckets
        hi = ((b + 1) * total) // n_buckets
        buckets.append([r for _, r in pairs[lo:hi]])
    return buckets


def _bucket_stats(rows: list[dict]) -> dict:
    sc = [_f(r["v6_score"]) for r in rows if _f(r.get("v6_score")) is not None]
    rp = [_f(r["return_pct"]) for r in rows if _f(r.get("return_pct")) is not None]
    npnl = [_f(r.get("v61_netto_pnl")) for r in rows if _f(r.get("v61_netto_pnl")) is not None]
    wins = sum(1 for r in rows if str(r.get("is_win", "")).strip().lower() == "true")
    n = len(rows)
    return {
        "n": n,
        "score_min": round(min(sc), 2) if sc else None,
        "score_max": round(max(sc), 2) if sc else None,
        "score_avg": round(sum(sc) / len(sc), 3) if sc else None,
        "win_rate": round(wins / n, 4) if n else None,
        "avg_return_pct": round(sum(rp) / len(rp), 4) if rp else None,
        "sum_netto": round(sum(npnl), 2) if npnl else None,
        "expectancy": round(sum(npnl) / len(npnl), 4) if npnl else None,  # avg netto/Trade
    }


# --------------------------------------------------------------------------- #
# Analyse
# --------------------------------------------------------------------------- #
def analyze(active: list[dict], n_buckets: int = _DEFAULT_BUCKETS) -> dict:
    pairs = [(_f(r["v6_score"]), r) for r in active if _f(r.get("v6_score")) is not None]
    scores = [p[0] for p in pairs]
    rets = [_f(p[1].get("return_pct")) for p in pairs]
    nettos = [_f(p[1].get("v61_netto_pnl")) for p in pairs]

    # rank_ic / pearson nur ueber Trades mit beidem vorhanden
    sc_r = [s for s, rr in zip(scores, rets) if rr is not None]
    rr_r = [rr for rr in rets if rr is not None]
    rank_ic = _spearman(sc_r, rr_r)
    pearson_r = _pearson(sc_r, rr_r)

    sc_n = [s for s, nn in zip(scores, nettos) if nn is not None]
    nn_n = [nn for nn in nettos if nn is not None]
    rank_ic_netto = _spearman(sc_n, nn_n)

    # Bucket-Lift
    buckets = _bucketize(pairs, n_buckets)
    bucket_lift = []
    for idx, brows in enumerate(buckets):
        st = _bucket_stats(brows)
        st["bucket"] = idx + 1
        bucket_lift.append(st)

    # Monotonie der Expectancy ueber Bucket-Index
    bx = [b["bucket"] for b in bucket_lift if b.get("expectancy") is not None]
    by = [b["expectancy"] for b in bucket_lift if b.get("expectancy") is not None]
    mono_ic = _spearman([float(x) for x in bx], by) if len(bx) >= 3 else None
    top = bucket_lift[-1].get("expectancy")
    bot = bucket_lift[0].get("expectancy")
    top_minus_bottom = (round(top - bot, 4) if (top is not None and bot is not None) else None)

    # Threshold-Sweep
    threshold_sweep = []
    n_total = len(active)
    for t in _THRESHOLD_CANDIDATES:
        subset = [p[1] for p in pairs if p[0] >= t]
        st = _bucket_stats(subset)
        st["threshold"] = t
        st["reachability"] = round(len(subset) / n_total, 4) if n_total else None
        threshold_sweep.append(st)

    reach_70 = next((s["reachability"] for s in threshold_sweep if s["threshold"] == 7.0), None)
    reach_85 = next((s["reachability"] for s in threshold_sweep if s["threshold"] == 8.5), None)

    # best threshold by expectancy (nur belastbare Teilmengen)
    eligible = [s for s in threshold_sweep if (s["n"] or 0) >= _MIN_N and s.get("expectancy") is not None]
    best = max(eligible, key=lambda s: s["expectancy"]) if eligible else None

    # Verdikt
    ic_ok = rank_ic is not None and abs(rank_ic) >= _IC_GATE
    lift_ok = top_minus_bottom is not None and top_minus_bottom > 0
    mono_ok = mono_ic is not None and mono_ic > 0
    score_is_predictive = bool(ic_ok and lift_ok and mono_ok)

    if score_is_predictive:
        verdict_text = (
            "Score ist ein RANKER: hoeherer Score korreliert mit hoeherer "
            "Expectancy (rank_ic>=Gate, Top>Bottom, monoton)."
        )
    else:
        reasons = []
        if not ic_ok:
            reasons.append(f"|rank_ic|<{_IC_GATE}")
        if not lift_ok:
            reasons.append("Top-Bucket-Expectancy nicht > Bottom")
        if not mono_ok:
            reasons.append("Expectancy nicht monoton im Score")
        verdict_text = (
            "Score ist ein GATE, KEIN Ranker (" + "; ".join(reasons) + "). "
            "Innerhalb des passierenden Bandes traegt ein hoeherer Score "
            "keine zusaetzliche Edge -> Schwellen-Feintuning auf Score allein "
            "ist unbegruendet; H2 sollte orthogonale Filter pruefen."
        )

    return {
        "n_active": len(active),
        "n_buckets": n_buckets,
        "rank_ic_return": round(rank_ic, 4) if rank_ic is not None else None,
        "rank_ic_netto": round(rank_ic_netto, 4) if rank_ic_netto is not None else None,
        "pearson_r_return": round(pearson_r, 4) if pearson_r is not None else None,
        "bucket_lift": bucket_lift,
        "bucket_expectancy_monotonicity_ic": round(mono_ic, 4) if mono_ic is not None else None,
        "top_minus_bottom_expectancy": top_minus_bottom,
        "threshold_sweep": threshold_sweep,
        "live_threshold_reachability": {"reach_at_7_0": reach_70, "reach_at_8_5": reach_85},
        "best_threshold_by_expectancy": (
            {"threshold": best["threshold"], "expectancy": best["expectancy"], "n": best["n"]}
            if best else None
        ),
        "score_is_predictive": score_is_predictive,
        "verdict_text": verdict_text,
        "ic_gate": _IC_GATE,
        "min_n": _MIN_N,
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report(csv_path: Path, n_buckets: int) -> dict:
    text = edeadlk_read_text(csv_path)
    if text is None:
        return {"error": "source_unreadable", "path": str(csv_path)}
    active = load_trades(text)
    if not active:
        return {"error": "no_active_trades", "path": str(csv_path)}
    body = analyze(active, n_buckets=n_buckets)
    return {
        "tool": "entry_score_edge",
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_csv": str(csv_path.name),
        **body,
    }


def to_markdown(rep: dict) -> str:
    if "error" in rep:
        return f"# Entry-Score-Edge: FEHLER\n\n`{rep['error']}` ({rep.get('path','')})\n"
    L = []
    L.append(f"# Entry-Score-Edge {rep.get('generated_at','')[:10]} ({rep['version']})")
    L.append("")
    L.append(f"**Quelle:** `{rep['source_csv']}` · aktive V6.1-Trades: **{rep['n_active']}** · Buckets: {rep['n_buckets']}")
    L.append("")
    L.append("> Read-only H2-Analyse. KEINE Live-Regel/Schwelle/Sizing-Aenderung (eiserne Regel).")
    L.append("")
    L.append("## Verdikt")
    L.append("")
    pred = "✅ PREDICTIVE (Ranker)" if rep["score_is_predictive"] else "⚠️ GATE (kein Ranker)"
    L.append(f"- **score_is_predictive:** {pred}")
    L.append(f"- **rank_ic (Score↔return_pct):** {rep['rank_ic_return']}  ·  rank_ic (Score↔netto): {rep['rank_ic_netto']}  ·  pearson_r: {rep['pearson_r_return']}")
    L.append(f"- **Bucket-Expectancy-Monotonie-IC:** {rep['bucket_expectancy_monotonicity_ic']}  ·  Top−Bottom-Expectancy: {rep['top_minus_bottom_expectancy']} €/Trade")
    r = rep["live_threshold_reachability"]
    L.append(f"- **Live-Schwellen-Reichweite:** ≥7,0 = {r['reach_at_7_0']}  ·  ≥8,5 = {r['reach_at_8_5']}")
    bt = rep["best_threshold_by_expectancy"]
    L.append(f"- **best_threshold_by_expectancy (advisorisch):** {bt}")
    L.append("")
    L.append(f"_{rep['verdict_text']}_")
    L.append("")
    L.append("## Bucket-Lift (Score aufsteigend)")
    L.append("")
    L.append("| Bucket | n | Score-Range | WR | avg ret% | sum netto € | Expectancy €/Trade |")
    L.append("|---|---|---|---|---|---|---|")
    for b in rep["bucket_lift"]:
        L.append(f"| {b['bucket']} | {b['n']} | {b['score_min']}-{b['score_max']} | {b['win_rate']} | {b['avg_return_pct']} | {b['sum_netto']} | {b['expectancy']} |")
    L.append("")
    L.append("## Threshold-Sweep (Teilmenge Score ≥ t)")
    L.append("")
    L.append("| Schwelle | n | reach | WR | avg ret% | Expectancy €/Trade |")
    L.append("|---|---|---|---|---|---|")
    for s in rep["threshold_sweep"]:
        L.append(f"| {s['threshold']} | {s['n']} | {s['reachability']} | {s['win_rate']} | {s['avg_return_pct']} | {s['expectancy']} |")
    L.append("")
    L.append("> Anti-Pattern-Hinweis: Schwellen mit n < MIN_N sind statistisch nicht belastbar; ein leerer Bucket (n=0) ist selbst ein Befund (Score-Pass-Plateau).")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
_HEADER = "date,ticker,regime,v6_score,return_pct,netto_pnl,holding_days,exit_reason,is_win,v61_skip,v61_exit_reason,v61_netto_pnl"


def _mk_row(score, ret, netto, win, skip="False"):
    return f"2024-01-01,AAA,Trending,{score},{ret},{netto},2,connors_sma5,{win},{skip},connors_sma5,{netto}"


def _fixture_monotone() -> str:
    """Score steigt -> netto/return steigt streng monoton (predictive)."""
    rows = [_HEADER]
    # 40 Trades, 4 Score-Stufen, klar steigende Expectancy
    for i in range(10):
        rows.append(_mk_row(5.6, -1.0, -10, "False"))
    for i in range(10):
        rows.append(_mk_row(6.0, 0.5, 5, "True"))
    for i in range(10):
        rows.append(_mk_row(6.6, 1.5, 15, "True"))
    for i in range(10):
        rows.append(_mk_row(7.2, 3.0, 30, "True"))
    # eine v61_skip=True-Zeile, die rausgefiltert werden MUSS
    rows.append(_mk_row(9.9, 99.0, 999, "True", skip="True"))
    return "\n".join(rows)


def _fixture_flat() -> str:
    """Score variiert, Ergebnis ist Score-unabhaengig (Gate, kein Ranker)."""
    rows = [_HEADER]
    pat = [(-1.0, -10, "False"), (2.0, 20, "True")]
    scores = [5.6, 6.1, 6.6, 7.1]
    for s in scores:
        for k in range(10):
            ret, netto, win = pat[k % 2]
            rows.append(_mk_row(s, ret, netto, win))
    return "\n".join(rows)


def selftest() -> int:
    fails = []

    # 1) v61_skip-Filter
    act = load_trades(_fixture_monotone())
    if len(act) != 40:
        fails.append(f"skip-filter: expected 40 active, got {len(act)}")

    # 2) Spearman-Sanity (fp-Toleranz: sqrt(n)*sqrt(n) ist nicht exakt n)
    sp = _spearman([1, 2, 3, 4], [10, 20, 30, 40])
    if sp is None or abs(sp - 1.0) > 1e-9:
        fails.append(f"spearman perfect-monotone != 1.0: {sp}")
    sm = _spearman([1, 2, 3, 4], [40, 30, 20, 10])
    if sm is None or abs(sm + 1.0) > 1e-9:
        fails.append(f"spearman perfect-inverse wrong: {sm}")

    # 3) Pearson-Sanity
    pr = _pearson([1, 2, 3], [2, 4, 6])
    if pr is None or abs(pr - 1.0) > 1e-9:
        fails.append(f"pearson perfect != 1.0: {pr}")

    # 4) Ranks mit Ties
    rk = _ranks([10, 10, 20])
    if rk != [1.5, 1.5, 3.0]:
        fails.append(f"ranks ties wrong: {rk}")

    # 5) monotone Fixture -> predictive TRUE, positive IC
    rep = build_report_from_text(_fixture_monotone(), 4)
    if not rep.get("score_is_predictive"):
        fails.append(f"monotone fixture not flagged predictive: ic={rep.get('rank_ic_return')} tmb={rep.get('top_minus_bottom_expectancy')}")
    if (rep.get("rank_ic_return") or 0) <= 0:
        fails.append(f"monotone fixture rank_ic not positive: {rep.get('rank_ic_return')}")
    # Top-Bucket-Expectancy > Bottom
    bl = rep["bucket_lift"]
    if not (bl[-1]["expectancy"] > bl[0]["expectancy"]):
        fails.append("monotone fixture top<=bottom expectancy")

    # 6) flache Fixture -> predictive FALSE
    repf = build_report_from_text(_fixture_flat(), 4)
    if repf.get("score_is_predictive"):
        fails.append(f"flat fixture wrongly predictive: ic={repf.get('rank_ic_return')}")

    # 7) threshold_sweep enthaelt alle Kandidaten + reachability in [0,1]
    for s in rep["threshold_sweep"]:
        if s["threshold"] not in _THRESHOLD_CANDIDATES:
            fails.append("unexpected threshold in sweep")
        if s["reachability"] is not None and not (0.0 <= s["reachability"] <= 1.0):
            fails.append(f"reachability out of range: {s['reachability']}")

    # 8) MD rendert ohne Exception
    _ = to_markdown(rep)

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  -", f)
        return 3
    print(f"SELFTEST PASS ({VERSION}): 8 Gruppen, monotone+flat-Fixtures, Spearman/Pearson/Ranks, sweep, MD.")
    return 0


def build_report_from_text(csv_text: str, n_buckets: int) -> dict:
    active = load_trades(csv_text)
    if not active:
        return {"error": "no_active_trades"}
    return {"tool": "entry_score_edge", "version": VERSION, "source_csv": "<text>", **analyze(active, n_buckets)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Entry-Score-Edge: H2 Entry-Side-Diagnostik (read-only).")
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--buckets", type=int, default=_DEFAULT_BUCKETS)
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--write", action="store_true", help="JSON + MD persistieren")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.buckets < 2:
        print("--buckets muss >= 2 sein", file=sys.stderr)
        return 2

    rep = build_report(Path(args.csv), args.buckets)
    if "error" in rep:
        print(f"FEHLER: {rep['error']} ({rep.get('path','')})", file=sys.stderr)
        return 2

    print(json.dumps(rep, ensure_ascii=False, indent=2))

    if args.write:
        TOOLS_DIR.mkdir(exist_ok=True)
        BRIEFINGS_DIR.mkdir(exist_ok=True)
        jpath = TOOLS_DIR / f"entry_score_edge_{args.date}.json"
        mpath = BRIEFINGS_DIR / f"Entry-Score-Edge-{args.date}.md"
        jpath.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        mpath.write_text(to_markdown(rep), encoding="utf-8")
        print(f"\nGeschrieben: {jpath}", file=sys.stderr)
        print(f"Geschrieben: {mpath}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
