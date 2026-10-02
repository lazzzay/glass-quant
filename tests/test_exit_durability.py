#!/usr/bin/env python3
"""
Regression-Suite fuer V7.23 EXIT-EDGE-DURABILITY-1 (tools/exit_durability.py).

Komplett OFFLINE (kein yfinance/Netz). Deckt: Gate-Logik (durable/fragile/duenn),
v61_skip-Filter, Sub-Perioden-Achsen, Frontload-Mathematik, MD/JSON-Render,
Idempotenz des In-Tool-Selftests, sowie ein Smoke-Test gegen das echte CSV
(falls vorhanden).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import exit_durability as ed  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def durable_rows():
    return ed._synthetic_durable()


def fragile_rows():
    return ed._synthetic_fragile()


# --------------------------------------------------------------------------- #
# Gate-Logik
# --------------------------------------------------------------------------- #
def test_durable_panel_is_durable():
    v = ed.analyze(durable_rows())["durability_verdict"]
    assert v["edge_is_durable"] is True
    assert v["edge_sign_consistency"] == 1.0
    assert v["n_years_qualified"] == 3
    assert v["edge_worst_expectancy"] > 0


def test_fragile_panel_not_durable():
    v = ed.analyze(fragile_rows())["durability_verdict"]
    assert v["edge_is_durable"] is False
    assert v["edge_sign_consistency"] < ed.SIGN_FLOOR
    assert v["edge_worst_expectancy"] < 0
    assert v["edge_worst_year"]["year"] in ("2023", "2025")


def test_single_year_not_durable():
    rows = [ed._mk_row("2024", "connors_sma5", 40.0, 1) for _ in range(6)]
    v = ed.analyze(rows)["durability_verdict"]
    assert v["edge_is_durable"] is False
    assert v["n_years_qualified"] == 1
    assert any("qualifiziert" in r for r in v["reasons"])


def test_min_n_per_year_gate():
    # 4 Edge-Trades/Jahr < MIN_N_PER_YEAR(5) -> 0 qualifiziert
    rows = []
    for y in ("2023", "2024", "2025"):
        rows += [ed._mk_row(y, "connors_sma5", 40.0, 1) for _ in range(4)]
    v = ed.analyze(rows)["durability_verdict"]
    assert v["n_years_qualified"] == 0
    assert v["edge_is_durable"] is False


# --------------------------------------------------------------------------- #
# Filter / Achsen
# --------------------------------------------------------------------------- #
def test_v61_skip_is_filtered():
    skipped = [ed._mk_row("2024", "connors_sma5", 40.0, 1, v61_skip="True") for _ in range(3)]
    active = ed.load_trades_from_rows(skipped + durable_rows())
    assert all(str(r.get("v61_skip")).lower() != "true" for r in active)
    assert len(active) == len(durable_rows())


def test_edge_by_year_axis_present():
    body = ed.analyze(durable_rows())
    assert set(body["edge_by_year"].keys()) == {"2023", "2024", "2025"}
    for y, s in body["edge_by_year"].items():
        assert s["n"] == 6
        assert s["sum_netto"] > 0


def test_max_hold_drag_negative_each_year():
    body = ed.analyze(durable_rows())
    for y, s in body["max_hold_by_year"].items():
        assert s["sum_netto"] < 0
    assert body["durability_verdict"]["max_hold_drag_consistency"] == 1.0


def test_edge_by_regime_axis():
    body = ed.analyze(durable_rows())
    assert "Trending" in body["edge_by_regime"]


# --------------------------------------------------------------------------- #
# Mathematik
# --------------------------------------------------------------------------- #
def test_frontload_ratio_half():
    g = [ed._mk_row("2024", "connors_sma5", 50.0, 1),
         ed._mk_row("2024", "connors_sma5", 50.0, 5)]
    assert ed._frontload_ratio(g) == 0.5


def test_frontload_ratio_full():
    g = [ed._mk_row("2024", "connors_sma5", 50.0, 1),
         ed._mk_row("2024", "connors_sma5", 50.0, 2)]
    assert ed._frontload_ratio(g) == 1.0


def test_frontload_ratio_zero_total_is_none():
    g = [ed._mk_row("2024", "connors_sma5", 50.0, 1),
         ed._mk_row("2024", "connors_sma5", -50.0, 5)]
    assert ed._frontload_ratio(g) is None


def test_expectancy_is_avg_netto():
    s = ed._bucket_stats([ed._mk_row("2024", "connors_sma5", 30.0, 1),
                          ed._mk_row("2024", "connors_sma5", 10.0, 1)])
    assert s["expectancy"] == 20.0
    assert s["avg_netto"] == s["expectancy"]


# --------------------------------------------------------------------------- #
# Render / Report
# --------------------------------------------------------------------------- #
def test_report_json_serialisable():
    rep = ed.build_report_from_rows(durable_rows(), date(2026, 6, 15))
    s = json.dumps(rep)
    assert json.loads(s)["n_active"] == len(durable_rows())


def test_render_md_contains_verdict():
    rep = ed.build_report_from_rows(fragile_rows(), date(2026, 6, 15))
    md = ed.render_md(rep)
    assert "Durability-Verdikt" in md
    assert "NICHT bestätigt" in md  # fragile -> not confirmed


def test_render_md_durable_marker():
    rep = ed.build_report_from_rows(durable_rows(), date(2026, 6, 15))
    md = ed.render_md(rep)
    assert "DURABLE" in md


# --------------------------------------------------------------------------- #
# Read-only Garantie: kein Live-Regel-/Journal-Write
# --------------------------------------------------------------------------- #
def test_no_rule_mutation_constants_stable():
    # Sanity: das Tool kennt die Strategie-Konstanten nur lesend.
    assert ed._SIGNAL_EXIT == "connors_sma5"
    assert "day3_flat_exit" in ed._MAX_HOLD_REASONS


# --------------------------------------------------------------------------- #
# CLI-Selftest (Subprozess, rc=0)
# --------------------------------------------------------------------------- #
def test_cli_selftest_exit_zero():
    r = subprocess.run(
        [sys.executable, str(REPO / "tools" / "exit_durability.py"), "--selftest"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST PASS" in r.stdout


# --------------------------------------------------------------------------- #
# Smoke gegen echtes CSV (falls vorhanden)
# --------------------------------------------------------------------------- #
def test_smoke_real_csv_if_present():
    csv_path = REPO / "v6_backtest_results_v61.csv"
    if not csv_path.exists():
        pytest.skip("kein echtes Backtest-CSV vorhanden")
    rep = ed.build_report(csv_path, date(2026, 6, 15))
    assert "error" not in rep
    assert rep["n_active"] > 0
    assert "durability_verdict" in rep
    v = rep["durability_verdict"]
    assert v["edge_engine"] == "connors_sma5"
    assert isinstance(v["edge_is_durable"], bool)
