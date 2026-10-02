#!/usr/bin/env python3
"""Tests fuer REGIME-CELL-EDGE-MAP-1 (V7.33).

Hermetisch: synthetisches Panel + In-Memory-Journal (connector-Injektion) +
injiziertes Regime-Archiv. Kein Zugriff auf echtes trade_journal.sqlite / Netz /
v6_backtest_results_v61.csv.

Korrigiert/validiert die These REGIME-TAG-GRANULARITY-1: 'Trending' ist eine
echte, distinkte, n-grosse Regime-Klasse; der Unblock kommt aus der Per-Zell-Map,
nicht aus Re-Tagging.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import regime_cell_edge_map as rcem  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def panel():
    return rcem.parse_panel_rows(rcem._SYN_CSV)


@pytest.fixture()
def edge_map(panel):
    return rcem.build_edge_map(panel, cutoff="2025-01-01", min_cell_n=4)


@pytest.fixture()
def archive():
    return {"2099-04-16": "Trending", "2099-05-12": "Trending",
            "2099-06-01": "Trending", "2099-06-04": "Trending"}


_LIVE_ROWS = [
    ("AAA", "external", "2099-04-16", None, "Trending", 0.5),
    ("BBB", "live", "2099-05-12", "Trending", None, 8.0),
    ("CCC", "live", "2099-06-01", "?", None, 4.0),
    ("DDD", "live", "2099-06-04", "?", None, -0.4),
]


# --------------------------------------------------------------------------- #
# Panel-Parsing
# --------------------------------------------------------------------------- #
def test_panel_parse_count(panel):
    assert len(panel) == 14


def test_panel_skips_unknown_regime():
    csv_q = "date,ticker,regime,return_pct,year\n2023-01-01,X,?,1.0,2023\n2023-01-02,Y,,1.0,2023\n"
    assert rcem.parse_panel_rows(csv_q) == []


def test_panel_column_case_insensitive():
    csv_uc = "DATE,Ticker,REGIME,Return_Pct,Year\n2023-01-01,X,Trending,1.0,2023\n"
    rows = rcem.parse_panel_rows(csv_uc)
    assert len(rows) == 1 and rows[0]["regime"] == "Trending"


# --------------------------------------------------------------------------- #
# Edge-Map
# --------------------------------------------------------------------------- #
def test_seven_cells_signs(edge_map):
    by = edge_map["by_regime"]
    assert by["Trending"]["expected_sign"] == 1
    assert by["Trending-Divergent"]["expected_sign"] == 1
    assert by["Bullish-Confirmed"]["expected_sign"] == -1


def test_confidence_floor(edge_map):
    by = edge_map["by_regime"]
    assert by["Trending"]["confidence"] == "HIGH"        # n=4 >= 4
    assert by["Neutral"]["confidence"] == "LOW"          # n=2 < 4


def test_edge_rank_ordering(edge_map):
    by = edge_map["by_regime"]
    # besseres erwartetes Regime -> hoeherer rank
    assert by["Bullish-Confirmed"]["edge_rank"] < by["Trending"]["edge_rank"]
    assert by["Trending"]["edge_rank"] <= by["Trending-Divergent"]["edge_rank"]


def test_oos_sign_consistency(edge_map):
    by = edge_map["by_regime"]
    assert by["Trending"]["oos_sign_consistent"] is True
    assert by["Bullish-Confirmed"]["oos_sign_consistent"] is True


def test_legacy_edge_cell_flag(edge_map):
    by = edge_map["by_regime"]
    assert by["Trending-Divergent"]["legacy_edge_cell"] is True
    assert by["Bullish-Confirmed"]["legacy_edge_cell"] is True
    assert by["Trending"]["legacy_edge_cell"] is False


# --------------------------------------------------------------------------- #
# Archiv-first Aufloesung
# --------------------------------------------------------------------------- #
def test_archive_first_recovery(archive):
    r = rcem.resolve_live_regime(
        {"entry_date": "2099-06-01", "regime": "?", "regime_at_entry": None}, archive)
    assert r["regime_cell"] == "Trending"
    assert r["regime_source"] == "archive"
    assert r["recovered_from_archive"] is True


def test_archive_wins_and_flags_divergence(archive):
    r = rcem.resolve_live_regime(
        {"entry_date": "2099-06-01", "regime": "Neutral", "regime_at_entry": None}, archive)
    assert r["regime_cell"] == "Trending"          # Archiv autoritativ
    assert r["journal_divergence"] is True


def test_journal_fallback_without_archive(archive):
    r = rcem.resolve_live_regime(
        {"entry_date": "2099-01-01", "regime": "Trending-Divergent"}, archive)
    assert r["regime_cell"] == "Trending-Divergent"
    assert r["regime_source"] == "journal"


def test_unknown_when_nothing(archive):
    r = rcem.resolve_live_regime(
        {"entry_date": "2099-01-01", "regime": "?", "regime_at_entry": "?"}, archive)
    assert r["regime_cell"] is None
    assert r["regime_source"] == "unknown"


# --------------------------------------------------------------------------- #
# Unblock (Kern-Befund: 0 legacy -> 4 extended)
# --------------------------------------------------------------------------- #
def _load(rows):
    trades, err = rcem.load_journal_trades(
        "ignored", connector=rcem._mk_inmemory_journal(rows))
    assert err is None
    return trades


def test_unblock_zero_to_four(edge_map, archive):
    trades = _load(_LIVE_ROWS)
    ub = rcem.assess_unblock(trades, archive, edge_map, min_cell_n=4)
    assert ub["legacy_usable_n"] == 0
    assert ub["extended_usable_n"] == 4
    assert ub["unblock_delta"] == 4


def test_unblock_recovery_count(edge_map, archive):
    trades = _load(_LIVE_ROWS)
    ub = rcem.assess_unblock(trades, archive, edge_map, min_cell_n=4)
    # CCC + DDD hatten Journal '?' -> aus Archiv gerettet
    assert ub["n_recovered_from_archive"] == 2
    assert ub["n_journal_divergence"] == 0


def test_unblock_directional_agreement(edge_map, archive):
    trades = _load(_LIVE_ROWS)
    ub = rcem.assess_unblock(trades, archive, edge_map, min_cell_n=4)
    # BBB(+) CCC(+) AAA(+0.5) agree with Trending(+); DDD(-) disagrees -> 3/4
    assert ub["directional_evaluated_n"] == 4
    assert ub["directional_agree_n"] == 3


def test_legacy_cell_trade_counts_legacy(edge_map, archive):
    # Ein synthetischer Live-Trade, der ECHT in Trending-Divergent liegt
    rows = [("ZZZ", "live", "2099-02-02", "Trending-Divergent", None, 2.0)]
    trades = _load(rows)
    ub = rcem.assess_unblock(trades, archive, edge_map, min_cell_n=4)
    assert ub["legacy_usable_n"] == 1
    assert ub["extended_usable_n"] == 1


def test_low_conf_cell_not_extended_usable(edge_map, archive):
    # Neutral ist LOW-conf (n<4) -> NICHT extended_usable trotz Return
    arch2 = {"2099-03-03": "Neutral"}
    rows = [("QQQ", "live", "2099-03-03", "Neutral", None, 1.0)]
    trades = _load(rows)
    ub = rcem.assess_unblock(trades, arch2, edge_map, min_cell_n=4)
    assert ub["extended_usable_n"] == 0


# --------------------------------------------------------------------------- #
# build_report + Verdikt + Render
# --------------------------------------------------------------------------- #
def test_build_report_verdict(tmp_path, archive):
    csv_p = tmp_path / "panel.csv"
    csv_p.write_text(rcem._SYN_CSV, encoding="utf-8")
    rep = rcem.build_report(
        csv_p, "ignored", cutoff="2025-01-01", min_cell_n=4, run_date="2026-06-30",
        journal_connector=rcem._mk_inmemory_journal(_LIVE_ROWS), regime_archive=archive)
    assert rep["verdict"] == "UNBLOCKED_BY_EXTENDED_MAP"
    assert rep["live_unblock"]["extended_usable_n"] == 4
    assert rep["edge_map"]["n_cells"] == 4


def test_build_report_no_trades(tmp_path, archive):
    csv_p = tmp_path / "panel.csv"
    csv_p.write_text(rcem._SYN_CSV, encoding="utf-8")
    rep = rcem.build_report(
        csv_p, "ignored", min_cell_n=4, run_date="2026-06-30",
        journal_connector=rcem._mk_inmemory_journal([]), regime_archive=archive)
    assert rep["verdict"] == "NO_LIVE_TRADES"


def test_build_report_missing_csv(tmp_path, archive):
    rep = rcem.build_report(
        tmp_path / "nope.csv", "ignored", run_date="2026-06-30",
        journal_connector=rcem._mk_inmemory_journal(_LIVE_ROWS), regime_archive=archive)
    assert "error" in rep


def test_render_md_smoke(tmp_path, archive):
    csv_p = tmp_path / "panel.csv"
    csv_p.write_text(rcem._SYN_CSV, encoding="utf-8")
    rep = rcem.build_report(
        csv_p, "ignored", cutoff="2025-01-01", min_cell_n=4, run_date="2026-06-30",
        journal_connector=rcem._mk_inmemory_journal(_LIVE_ROWS), regime_archive=archive)
    md = rcem.render_md(rep)
    assert "Regime-Cell-Edge-Map" in md
    assert "Live-Unblock" in md
    assert "Diagnose-Korrektur" in md


# --------------------------------------------------------------------------- #
# Selftest als pytest
# --------------------------------------------------------------------------- #
def test_selftest_passes():
    assert rcem.selftest() == 0
