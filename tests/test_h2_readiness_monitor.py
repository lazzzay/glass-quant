"""Tests fuer V7.34 h2_readiness_monitor (read-only Observability)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools"))

import h2_readiness_monitor as m  # noqa: E402


def test_selftest_green():
    assert m.selftest() == 0


def test_project_days_to_gate():
    assert m.project_days_to_gate(0, 15.88) == 0
    assert m.project_days_to_gate(38, 0) is None
    assert m.project_days_to_gate(38, None) is None
    assert m.project_days_to_gate(52, 52) == 365
    # higher cadence -> fewer days
    assert m.project_days_to_gate(40, 104) < m.project_days_to_gate(40, 26)


def test_baseline_verdict_frozen_cadence_bound():
    rep = m.compose_h2_readiness(m._fake_assess(), m._fake_edge_unblock(),
                                 as_of="2026-07-01")
    assert rep["verdict"] == "H2_FROZEN_CADENCE_BOUND"
    assert rep["best_single_view_usable_n"] == 2
    assert rep["remaining_to_gate"] == 38
    assert "regime_wiring_approval_pending" in rep["limiting_factor"]


def test_regime_legacy_vs_extended():
    rep = m.compose_h2_readiness(m._fake_assess(), m._fake_edge_unblock(legacy=0, extended=4),
                                 as_of="2026-07-01")
    er = rep["regime_extended_view"]
    assert er["legacy_usable_n"] == 0
    assert er["extended_usable_n"] == 4
    assert er["unblock_delta"] == 4
    # extended view must NOT count as ready (not wired): the whole safety point
    assert er["wiring_status"] == "PENDING_APPROVAL"
    assert er["ready"] is False
    reg_row = [c for c in rep["candidates"] if c["candidate"] == "regime"][0]
    assert reg_row["usable_n"] == 0
    assert reg_row["view"] == "legacy_binary"


def test_gate_met_partial_ready():
    rep = m.compose_h2_readiness(m._fake_assess(atr_usable=40), m._fake_edge_unblock(),
                                 as_of="2026-07-01")
    assert rep["verdict"] == "H2_LIVE_FORWARD_PARTIAL_READY"
    assert rep["remaining_to_gate"] == 0


def test_not_accumulating():
    rep = m.compose_h2_readiness(
        m._fake_assess(atr_usable=0, rsi_usable=0, regime_usable=0),
        m._fake_edge_unblock(legacy=0, extended=0), as_of="2026-07-01")
    assert rep["verdict"] == "H2_NOT_ACCUMULATING"


def test_no_edge_unblock_graceful():
    rep = m.compose_h2_readiness(m._fake_assess(), None, as_of="2026-07-01")
    assert rep["regime_extended_view"] is None
    assert rep["operator_lever_illustration"] is None
    assert "regime_wiring" not in rep["limiting_factor"]


def test_operator_lever_math():
    rep = m.compose_h2_readiness(m._fake_assess(), m._fake_edge_unblock(),
                                 as_of="2026-07-01")
    ll = rep["operator_lever_illustration"]
    assert ll["best_usable_n_if_extended_adopted"] == 4
    assert ll["remaining_n_if_extended_adopted"] == 36


def test_markdown_renders():
    rep = m.compose_h2_readiness(m._fake_assess(), m._fake_edge_unblock(),
                                 as_of="2026-07-01")
    md = m.render_md(rep)
    assert md.startswith("# H2-Readiness-Monitor")
    assert "Legacy vs. Erweitert" in md
    assert "PENDING_APPROVAL" in md


def test_readonly_no_live_rule_keys():
    # Guard: report must never emit anything that looks like a sizing/threshold change
    rep = m.compose_h2_readiness(m._fake_assess(), m._fake_edge_unblock(),
                                 as_of="2026-07-01")
    assert rep["scope"] == "READ_ONLY_OBSERVABILITY"
    banned = {"position_size", "kelly", "score_threshold", "hebel", "sizing"}
    assert not (banned & set(rep.keys()))
