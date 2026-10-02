#!/usr/bin/env python3
"""Tests fuer V7.26 ENTRY-FILTER-ORTHOGONALITY-1 (tools/entry_filter_orthogonality.py).

Read-only Diagnostik: die Tests pruefen Statistik-Korrektheit, das v61_skip-
Filterkontrakt, die Edge-/Separations-Gates und die Verdikt-Logik. Keine
Live-Regel-Aenderung.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import entry_filter_orthogonality as efo  # noqa: E402


# --------------------------------------------------------------------------- #
# Statistik
# --------------------------------------------------------------------------- #
def test_spearman_monotone():
    assert round(efo._spearman([1, 2, 3, 4], [10, 20, 30, 40]), 6) == 1.0


def test_spearman_inverse():
    assert round(efo._spearman([1, 2, 3, 4], [40, 30, 20, 10]), 6) == -1.0


def test_spearman_too_few():
    assert efo._spearman([1, 2], [3, 4]) is None


def test_f_helper():
    assert efo._f("3.5") == 3.5
    assert efo._f("nan") is None
    assert efo._f(None) is None
    assert efo._f("x") is None


def test_ranks_ties_averaged():
    # zwei gleiche kleinste Werte -> Rang (1+2)/2 = 1.5
    r = efo._ranks([5.0, 5.0, 9.0])
    assert r[0] == 1.5 and r[1] == 1.5 and r[2] == 3.0


# --------------------------------------------------------------------------- #
# Daten-Kontrakt
# --------------------------------------------------------------------------- #
def test_skip_filter_drops_v61_skip_true():
    act = efo.load_trades(efo._make_synth())
    assert len(act) == 60  # 60 aktiv + 1 skip=True rausgefiltert


def test_group_stats_basic():
    rows = [
        {"return_pct": "2.0", "v61_netto_pnl": "20", "is_win": "True"},
        {"return_pct": "-1.0", "v61_netto_pnl": "-10", "is_win": "False"},
    ]
    gs = efo._group_stats(rows)
    assert gs["n"] == 2
    assert gs["win_rate"] == 0.5
    assert gs["avg_return_pct"] == 0.5
    assert gs["expectancy"] == 5.0


# --------------------------------------------------------------------------- #
# Numerische Analyse
# --------------------------------------------------------------------------- #
def test_numeric_detects_edge_low_better():
    act = efo.load_trades(efo._make_synth())
    r = efo.analyze_numeric(act, "rsi2", 4)
    assert r["has_edge"] is True
    assert r["direction"] == "low_better"
    assert r["rank_ic_return"] is not None and r["rank_ic_return"] < 0


def test_numeric_constant_score_no_edge():
    act = efo.load_trades(efo._make_synth())
    r = efo.analyze_numeric(act, "v6_score", 4, is_score_ref=True)
    assert r["has_edge"] is False
    assert r["is_orthogonal"] is False


def test_numeric_min_n_guard():
    tiny = efo.load_trades(efo._make_synth())[:5]
    r = efo.analyze_numeric(tiny, "rsi2", 4)
    assert r["has_edge"] is False
    assert any("n<" in n for n in r["notes"])


# --------------------------------------------------------------------------- #
# Kategoriale Analyse
# --------------------------------------------------------------------------- #
def test_categorical_separation():
    act = efo.load_trades(efo._make_synth())
    r = efo.analyze_categorical(act, "regime", gated=True)
    assert r["has_separation"] is True
    assert r["best_cell"] == "GoodReg"
    assert r["worst_cell"] == "BadReg"
    assert r["spread_return_pp"] >= efo._SPREAD_GATE_PP


def test_categorical_eligibility_min_cell():
    # Zelle mit < MIN_CELL Trades ist nicht eligible
    rows = [{"regime": "Rare", "return_pct": "5", "v61_netto_pnl": "50", "is_win": "True"}] * 3
    rows += [{"regime": "Common", "return_pct": "1", "v61_netto_pnl": "10", "is_win": "True"}] * 30
    r = efo.analyze_categorical(rows, "regime", gated=True)
    rare = next(c for c in r["cells"] if c["value"] == "Rare")
    common = next(c for c in r["cells"] if c["value"] == "Common")
    assert rare["eligible"] is False
    assert common["eligible"] is True


def test_context_categorical_not_gated():
    act = efo.load_trades(efo._make_synth())
    r = efo.analyze_categorical(act, "year", gated=False)
    assert r["gated"] is False
    assert r["has_separation"] is False  # ungated -> nie Separation-Flag


# --------------------------------------------------------------------------- #
# Verdikt + Orchestrierung
# --------------------------------------------------------------------------- #
def test_analyze_verdict_finds_orthogonal_edge():
    act = efo.load_trades(efo._make_synth())
    rep = efo.analyze(act, n_buckets=4)
    v = rep["verdict"]
    assert v["orthogonal_edge_found"] is True
    assert v["n_candidates"] >= 2
    feats = {c["feature"] for c in rep["candidate_filters"]}
    assert "rsi2" in feats and "regime" in feats


def test_candidates_sorted_by_strength():
    act = efo.load_trades(efo._make_synth())
    rep = efo.analyze(act, n_buckets=4)
    strengths = [c["strength"] for c in rep["candidate_filters"]]
    assert strengths == sorted(strengths, reverse=True)


def test_render_md_runs():
    act = efo.load_trades(efo._make_synth())
    rep = efo.analyze(act, n_buckets=4)
    md = efo.render_md({**rep, "date": "test", "source_csv": "syn", "n_active": len(act)})
    assert "Verdikt" in md and "Numerische Features" in md


# --------------------------------------------------------------------------- #
# Live-Korpus (falls vorhanden)
# --------------------------------------------------------------------------- #
def test_live_corpus_runs_if_present():
    if not efo._DEFAULT_CSV.exists():
        pytest.skip("kein Live-Korpus")
    rep = efo.build_report(efo._DEFAULT_CSV, 4, "2026-06-18")
    assert rep.get("error") is None
    assert rep["n_active"] > 50
    assert "orthogonal_edge_found" in rep["verdict"]
    # Score-Referenz muss schwach sein (V7.25-Konsistenz): |IC| klein
    sic = rep["verdict"]["score_reference_ic"]
    assert sic is None or abs(sic) < 0.2


def test_selftest_passes():
    assert efo.selftest() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
