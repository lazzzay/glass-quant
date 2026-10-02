#!/usr/bin/env python3
"""Tests fuer V7.38 TIER-RULE-COUNTERFACTUAL-1 (tools/tier_rule_counterfactual.py).

Read-only H2-Diagnostik: quantifiziert die Counterfactual-Erwartung der von der
regime_hard_tier-Regel AUSGESCHLOSSENEN Tiers pro verengtem Regime.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import tier_rule_counterfactual as trc  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetisches Panel: TD-B positiv (TOO_STRICT), HVK-B negativ (PROTECTIVE),
# BD nur A (INSUFFICIENT), Trending unverengt (NO_RESTRICTION).
# --------------------------------------------------------------------------- #
def _panel():
    return trc.parse_panel_rows(trc._syn_csv())


# ---- SSoT / Regel-Import -------------------------------------------------- #
def test_hard_rules_ssot_present():
    assert "Trending-Divergent" in trc._HARD_RULES
    assert trc._HARD_RULES["Trending-Divergent"]["hard_tiers"] == ["A"]
    assert trc._HARD_RULES["Extreme-Fear-Thrust"]["hard_tiers"] == ["A", "B"]


def test_hard_rules_source_is_authoritative_or_fallback():
    # In gesunder Umgebung == SSoT-Import; sonst dokumentierter Fallback.
    assert trc._HARD_RULES_SOURCE in (
        "rec_gating_attribution.REGIME_HARD_RULES", "fallback (import failed)")


@pytest.mark.parametrize("hard,expected", [
    (["A"], ["B", "C"]),
    (["A", "B"], ["C"]),
    (["A", "B", "C"], []),
    (None, []),
    ([], []),
])
def test_excluded_tiers(hard, expected):
    assert trc._excluded_tiers(hard) == expected


# ---- Panel-Parsing -------------------------------------------------------- #
def test_parse_panel_has_tier_and_regime():
    p = _panel()
    assert len(p) == 21
    assert all(r["tier"] in ("A", "B", "C", "?") for r in p)
    assert all(r["regime"] for r in p)


def test_parse_panel_skips_unknown_regime():
    txt = "date,ticker,tier,regime,return_pct,year\n2023-01-01,X,A,?,1.0,2023\n"
    assert trc.parse_panel_rows(txt) == []


# ---- Per-Regime-Verdikte -------------------------------------------------- #
def test_verdict_rule_too_strict():
    a = trc.assess_regime_counterfactual(
        "Trending-Divergent", trc._HARD_RULES["Trending-Divergent"], _panel(), min_n=4)
    assert a["verdict"] == "RULE_TOO_STRICT"
    assert a["excluded_tiers"] == ["B", "C"]
    assert a["excluded_pool"]["n"] == 4
    assert a["excluded_pool"]["mean_return_pct"] > 0
    assert a["excluded_minus_allowed_mean_ret"] is not None


def test_verdict_rule_protective():
    a = trc.assess_regime_counterfactual(
        "High-Vol-Krise", trc._HARD_RULES["High-Vol-Krise"], _panel(), min_n=4)
    assert a["verdict"] == "RULE_PROTECTIVE"
    assert a["excluded_pool"]["mean_return_pct"] < 0
    assert a["excluded_year_sign_consistent"] is True


def test_verdict_insufficient_data_when_excluded_empty():
    a = trc.assess_regime_counterfactual(
        "Bullish-Divergent", trc._HARD_RULES["Bullish-Divergent"], _panel(), min_n=4)
    assert a["verdict"] == "INSUFFICIENT_DATA"
    assert a["excluded_pool"]["n"] == 0


def test_verdict_insufficient_data_when_below_floor():
    #  Floor hoeher als vorhandene excluded-n -> INSUFFICIENT trotz Daten
    a = trc.assess_regime_counterfactual(
        "High-Vol-Krise", trc._HARD_RULES["High-Vol-Krise"], _panel(), min_n=999)
    assert a["verdict"] == "INSUFFICIENT_DATA"


def test_verdict_no_restriction_for_open_regime():
    a = trc.assess_regime_counterfactual(
        "Trending", trc._HARD_RULES["Trending"], _panel(), min_n=4)
    assert a["verdict"] == "NO_RESTRICTION"
    assert a["excluded_tiers"] == []


# ---- Report / Gesamt-Verdikt ---------------------------------------------- #
def test_report_overall_mixed():
    rep = trc.build_report(Path("ignored"), min_n=4, run_date="2026-07-07",
                           csv_text=trc._syn_csv(), today_regime="Trending-Divergent")
    assert rep["overall_verdict"] == "MIXED"
    assert rep["summary_counts"]["too_strict"] == 1
    assert rep["summary_counts"]["protective"] == 1


def test_report_overall_data_starved_with_high_floor():
    rep = trc.build_report(Path("ignored"), min_n=999, run_date="2026-07-07",
                           csv_text=trc._syn_csv(), today_regime="Trending")
    assert rep["overall_verdict"] == "DATA_STARVED_UNANSWERABLE"
    assert rep["summary_counts"]["measurable"] == 0


def test_report_error_on_unreadable_csv():
    rep = trc.build_report(Path("/nonexistent/does_not_exist.csv"), min_n=4)
    assert rep.get("error")


# ---- Live-Bindung --------------------------------------------------------- #
def test_live_binding_true_for_restricted_regime():
    rep = trc.build_report(Path("ignored"), min_n=4, csv_text=trc._syn_csv(),
                           today_regime="Trending-Divergent")
    lb = rep["live_binding"]
    assert lb["rule_binding_today"] is True
    assert lb["excluded_tiers_today"] == ["B", "C"]


def test_live_binding_false_for_open_regime():
    rep = trc.build_report(Path("ignored"), min_n=4, csv_text=trc._syn_csv(),
                           today_regime="Trending")
    assert rep["live_binding"]["rule_binding_today"] is False


def test_live_binding_none_when_no_regime():
    rep = trc.build_report(Path("ignored"), min_n=4, csv_text=trc._syn_csv(),
                           today_regime=None)
    assert rep["live_binding"]["rule_binding_today"] is None


# ---- Global premise + matrix ---------------------------------------------- #
def test_global_premise_and_matrix():
    p = _panel()
    g = trc.global_tier_expectancy(p)
    assert g["premise_A_is_core"] in (True, False)
    mtx = trc.build_tier_matrix(p)
    assert mtx["Trending-Divergent"]["B"]["n"] == 4


# ---- Render --------------------------------------------------------------- #
def test_render_md_contains_sections():
    rep = trc.build_report(Path("ignored"), min_n=4, csv_text=trc._syn_csv(),
                           today_regime="Trending-Divergent")
    md = trc.render_md(rep)
    assert "Tier-Rule-Counterfactual" in md
    assert "Per-Regime-Counterfactual" in md
    assert "Live-Bindung" in md


def test_render_md_error_path():
    md = trc.render_md({"date": "2026-07-07", "error": "boom"})
    assert "FEHLER" in md


# ---- Selftest ------------------------------------------------------------- #
def test_selftest_passes():
    assert trc.selftest() == 0


# ---- Integration mit echtem Panel (falls vorhanden) ----------------------- #
@pytest.mark.skipif(not (ROOT / "v6_backtest_results_v61.csv").exists(),
                    reason="Panel-CSV nicht vorhanden")
def test_real_panel_is_tier_a_dominated_and_data_starved():
    rep = trc.build_report(ROOT / "v6_backtest_results_v61.csv", min_n=10,
                           run_date="2026-07-07")
    # Historischer Kern-Befund: Panel tier-A-dominiert -> in den verengten
    # Regimes kein messbarer excluded-Pool -> DATA_STARVED_UNANSWERABLE.
    assert rep["panel_n"] > 300
    assert rep["overall_verdict"] == "DATA_STARVED_UNANSWERABLE"
    g = rep["global_tier_expectancy"]
    assert g["premise_A_is_core"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
