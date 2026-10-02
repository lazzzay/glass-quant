#!/usr/bin/env python3
"""Tests fuer tools/entry_score_edge.py (V7.25 ENTRY-SCORE-EDGE-1).

Read-only Analyse-Tool; Tests pruefen Statistik-Korrektheit, das
predictive/gate-Verdikt, den v61_skip-Filter und Output-Robustheit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import entry_score_edge as ese  # noqa: E402


# --------------------------------------------------------------------------- #
# Statistik-Primitive
# --------------------------------------------------------------------------- #
def test_ranks_average_ties():
    assert ese._ranks([10, 10, 20]) == [1.5, 1.5, 3.0]
    assert ese._ranks([5, 1, 3]) == [3.0, 1.0, 2.0]


def test_pearson_perfect():
    assert abs(ese._pearson([1, 2, 3], [2, 4, 6]) - 1.0) < 1e-9
    assert abs(ese._pearson([1, 2, 3], [6, 4, 2]) + 1.0) < 1e-9


def test_pearson_degenerate_returns_none():
    assert ese._pearson([1, 1, 1], [2, 4, 6]) is None  # keine Score-Varianz
    assert ese._pearson([1.0], [2.0]) is None           # zu wenig n


def test_spearman_monotone_and_inverse():
    assert abs(ese._spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    assert abs(ese._spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-9


def test_spearman_handles_nonlinear_monotone():
    # streng monoton, nicht-linear -> Spearman 1.0, Pearson < 1
    xs = [1, 2, 3, 4]
    ys = [1, 4, 9, 16]
    assert abs(ese._spearman(xs, ys) - 1.0) < 1e-9
    assert ese._pearson(xs, ys) < 1.0


# --------------------------------------------------------------------------- #
# v61_skip-Filter
# --------------------------------------------------------------------------- #
def test_skip_filter_excludes_v61_skip_true():
    active = ese.load_trades(ese._fixture_monotone())
    assert len(active) == 40  # 41 Zeilen, 1x v61_skip=True raus
    assert all(str(r.get("v61_skip")).lower() != "true" for r in active)


# --------------------------------------------------------------------------- #
# Verdikt: predictive vs gate
# --------------------------------------------------------------------------- #
def test_monotone_fixture_is_predictive():
    rep = ese.build_report_from_text(ese._fixture_monotone(), 4)
    assert rep["score_is_predictive"] is True
    assert rep["rank_ic_return"] > 0
    bl = rep["bucket_lift"]
    assert bl[-1]["expectancy"] > bl[0]["expectancy"]
    assert rep["top_minus_bottom_expectancy"] > 0


def test_flat_fixture_is_gate_not_ranker():
    rep = ese.build_report_from_text(ese._fixture_flat(), 4)
    assert rep["score_is_predictive"] is False
    assert "GATE" in rep["verdict_text"]


# --------------------------------------------------------------------------- #
# Threshold-Sweep / Reachability
# --------------------------------------------------------------------------- #
def test_threshold_sweep_complete_and_bounded():
    rep = ese.build_report_from_text(ese._fixture_monotone(), 4)
    sweep = rep["threshold_sweep"]
    assert [s["threshold"] for s in sweep] == ese._THRESHOLD_CANDIDATES
    for s in sweep:
        if s["reachability"] is not None:
            assert 0.0 <= s["reachability"] <= 1.0
    # Score-Max der Fixture = 7.2 -> Schwelle 8.5 muss n=0 liefern (Plateau-Befund)
    s85 = next(s for s in sweep if s["threshold"] == 8.5)
    assert s85["n"] == 0


def test_reachability_monotone_nonincreasing_in_threshold():
    rep = ese.build_report_from_text(ese._fixture_monotone(), 4)
    reach = [s["reachability"] for s in rep["threshold_sweep"]]
    for a, b in zip(reach, reach[1:]):
        assert b <= a  # hoehere Schwelle -> nie mehr Trades


# --------------------------------------------------------------------------- #
# Robustheit
# --------------------------------------------------------------------------- #
def test_no_active_trades_returns_error():
    only_skips = ese._HEADER + "\n" + ese._mk_row(7.0, 1.0, 10, "True", skip="True")
    rep = ese.build_report_from_text(only_skips, 4)
    assert rep.get("error") == "no_active_trades"


def test_markdown_renders_without_exception():
    rep = ese.build_report_from_text(ese._fixture_monotone(), 4)
    md = ese.to_markdown(rep)
    assert "Entry-Score-Edge" in md
    assert "Bucket-Lift" in md
    assert "Threshold-Sweep" in md


def test_selftest_passes():
    assert ese.selftest() == 0
