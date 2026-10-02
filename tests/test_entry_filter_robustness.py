#!/usr/bin/env python3
"""Tests fuer V7.27 ENTRY-FILTER-ROBUSTNESS-1 (tools/entry_filter_robustness.py).

Read-only H2-Robustheits-Gate: die Tests pruefen: zweiseitiger Student-t
p-Wert (regularisierte inkompl. Beta), Benjamini-Hochberg FDR, das geteilte
v61_skip-Filterkontrakt, die temporale Sign-Konsistenz, Bootstrap-Stabilitaet
(inkl. Determinismus), die gerichtete regime-Konsistenz und die Verdikt-/
Re-Klassifikations-Logik. Keine Live-Regel-Aenderung.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import entry_filter_robustness as efr  # noqa: E402


# --------------------------------------------------------------------------- #
# Student-t p-Wert
# --------------------------------------------------------------------------- #
def test_t_pvalue_zero_is_one():
    assert abs(efr.t_two_sided_p(0.0, 10) - 1.0) < 1e-9


def test_t_pvalue_large_is_zero():
    assert efr.t_two_sided_p(50.0, 100) < 1e-6


def test_t_pvalue_known_reference():
    # df=10, t=2.228 -> zweiseitig ~ 0.05 (Standard-t-Tabelle)
    p = efr.t_two_sided_p(2.228, 10)
    assert 0.045 < p < 0.055


def test_t_pvalue_symmetry():
    assert abs(efr.t_two_sided_p(2.0, 30) - efr.t_two_sided_p(-2.0, 30)) < 1e-12


def test_spearman_p_none_safe():
    assert efr.spearman_p(None, 100) is None
    assert efr.spearman_p(0.3, 2) is None


def test_spearman_p_monotone_in_ic():
    # bei gleichem n: groessere |ic| -> kleinerer p-Wert
    p_small = efr.spearman_p(0.05, 200)
    p_large = efr.spearman_p(0.30, 200)
    assert p_large < p_small


# --------------------------------------------------------------------------- #
# Benjamini-Hochberg FDR
# --------------------------------------------------------------------------- #
def test_bh_strong_survives_nulls_dont():
    bh = efr.benjamini_hochberg(
        {"strong": 0.0001, "n1": 0.4, "n2": 0.6, "n3": 0.8, "n4": 0.95}, 0.10)
    assert bh["strong"]["survives"] is True
    assert all(bh[k]["survives"] is False for k in ("n1", "n2", "n3", "n4"))


def test_bh_padj_ge_p():
    bh = efr.benjamini_hochberg({"a": 0.01, "b": 0.02, "c": 0.2}, 0.10)
    for k, d in bh.items():
        assert d["p_adj"] >= d["p"] - 1e-12


def test_bh_all_none():
    bh = efr.benjamini_hochberg({"a": None, "b": None}, 0.10)
    assert all(d["survives"] is None for d in bh.values())


def test_bh_higher_q_admits_more():
    pv = {"a": 0.01, "b": 0.04, "c": 0.06, "d": 0.5}
    low = efr.benjamini_hochberg(pv, 0.05)
    high = efr.benjamini_hochberg(pv, 0.20)
    n_low = sum(1 for d in low.values() if d["survives"])
    n_high = sum(1 for d in high.values() if d["survives"])
    assert n_high >= n_low


# --------------------------------------------------------------------------- #
# Skip-Filter-Kontrakt (geteilt mit V7.26) + Synthetik
# --------------------------------------------------------------------------- #
def test_skip_filter_excludes_v61_skip():
    act = efr.load_trades(efr._syn_make())
    assert len(act) == 160  # 160 aktiv, 1 v61_skip=True faellt raus


# --------------------------------------------------------------------------- #
# Temporale Konsistenz
# --------------------------------------------------------------------------- #
def test_temporal_consistency_consistent_feature():
    act = efr.load_trades(efr._syn_make())
    ic, _ = efr._ic_return(act, "rsi2")
    tc = efr.temporal_consistency(act, "rsi2", ic)
    assert tc["sign_consistency"] == 1.0
    assert tc["n_years"] == 4


def test_temporal_consistency_flip_feature():
    act = efr.load_trades(efr._syn_fragile())
    ic, _ = efr._ic_return(act, "atr_pct")
    tc = efr.temporal_consistency(act, "atr_pct", ic)
    # 2 von 4 Jahren mit Gegen-Vorzeichen -> 0.5
    assert tc["sign_consistency"] == 0.5


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_strong_feature_stable():
    act = efr.load_trades(efr._syn_make())
    ic, _ = efr._ic_return(act, "rsi2")
    b = efr.bootstrap_stability(act, "rsi2", ic, B=300, seed=1)
    assert b["sign_stability"] >= 0.90
    assert b["ci_excludes_zero"] is True


def test_bootstrap_deterministic_with_seed():
    act = efr.load_trades(efr._syn_make())
    ic, _ = efr._ic_return(act, "rsi2")
    b1 = efr.bootstrap_stability(act, "rsi2", ic, B=200, seed=42)
    b2 = efr.bootstrap_stability(act, "rsi2", ic, B=200, seed=42)
    assert b1 == b2


# --------------------------------------------------------------------------- #
# Regime gerichtete Konsistenz
# --------------------------------------------------------------------------- #
def test_regime_directional_consistency():
    act = efr.load_trades(efr._syn_make())
    rc = efr.regime_directional_consistency(act, "GoodReg", "BadReg")
    assert rc["directional_consistency"] == 1.0
    assert rc["n_years_counted"] == 4


# --------------------------------------------------------------------------- #
# Orchestrierung / Verdikt
# --------------------------------------------------------------------------- #
def test_analyze_rsi2_robust():
    act = efr.load_trades(efr._syn_make())
    rep = efr.analyze(act, buckets=4, B=300, seed=1)
    cmap = {c["feature"]: c for c in rep["candidates"]}
    assert cmap["rsi2"]["is_v726_candidate"] is True
    assert cmap["rsi2"]["edge_is_robust"] is True


def test_analyze_atr_fragile_candidate():
    # atr_pct ist V7.26-Kandidat, faellt aber an der temporalen Sign-Konsistenz
    act = efr.load_trades(efr._syn_fragile())
    rep = efr.analyze(act, buckets=4, B=300, seed=1)
    cmap = {c["feature"]: c for c in rep["candidates"]}
    assert cmap["atr_pct"]["is_v726_candidate"] is True
    assert cmap["atr_pct"]["edge_is_robust"] is False


def test_analyze_regime_robust():
    act = efr.load_trades(efr._syn_make())
    rep = efr.analyze(act, buckets=4, B=200, seed=1)
    assert rep["regime"] is not None
    assert rep["regime"]["edge_is_robust"] is True


def test_analyze_verdict_and_reclass():
    act = efr.load_trades(efr._syn_make())
    rep = efr.analyze(act, buckets=4, B=200, seed=1)
    assert rep["verdict"]["any_robust_edge"] is True
    assert len(rep["verdict"]["reclassification"]) >= 2
    # FDR-Familie deckt alle 5 numerischen Features ab
    assert set(rep["numeric_family_fdr"].keys()) == set(efr._NUMERIC_FAMILY)


def test_render_md_sections():
    act = efr.load_trades(efr._syn_make())
    rep = efr.analyze(act, buckets=4, B=150, seed=1)
    md = efr.render_md({**rep, "date": "test", "source_csv": "synthetic"})
    assert "Verdikt" in md and "FDR" in md and "Re-Klassifikation" in md


# --------------------------------------------------------------------------- #
# Live-Korpus-Smoke (nur wenn CSV vorhanden)
# --------------------------------------------------------------------------- #
def test_live_corpus_smoke():
    csv = ROOT / "v6_backtest_results_v61.csv"
    if not csv.exists():
        pytest.skip("Backtest-CSV nicht vorhanden")
    rep = efr.build_report(csv, buckets=4, fdr_q=0.10, B=200, seed=1,
                           run_date="test")
    assert "error" not in rep
    assert rep["n_active"] > 200
    assert "any_robust_edge" in rep["verdict"]
    # FDR-Familie immer komplett
    assert len(rep["numeric_family_fdr"]) == len(efr._NUMERIC_FAMILY)


def test_selftest_passes():
    assert efr.selftest() == 0
