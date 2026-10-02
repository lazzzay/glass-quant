#!/usr/bin/env python3
"""Tests fuer tools/exit_attribution.py (V7.21 EXIT-ATTRIBUTION-1)."""
import importlib.util
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "exit_attribution.py"
_spec = importlib.util.spec_from_file_location("exit_attribution", _TOOL)
ea = importlib.util.module_from_spec(_spec)
sys.modules["exit_attribution"] = ea
_spec.loader.exec_module(ea)


CSV = """date,ticker,regime,return_pct,netto_pnl,holding_days,exit_reason,v61_skip,v61_exit_reason,v61_netto_pnl
2024-01-01,AAA,Trending,3.0,30,1,connors_sma5,False,connors_sma5,30
2024-01-02,BBB,Trending,2.0,20,2,connors_sma5,False,connors_sma5,20
2024-01-03,CCC,Neutral,-1.0,-10,3,day3_flat_exit,False,day3_flat_exit,-10
2024-01-04,DDD,Neutral,-4.0,-40,4,atr_stop_loss,False,atr_stop_loss,-40
2024-01-05,EEE,Trending,5.0,50,1,connors_sma5,True,skipped_tier_x,0
2024-01-06,FFF,Trending,1.0,12,2,atr_take_profit,False,atr_take_profit,12
"""


def test_selftest_passes():
    assert ea._selftest() == 0


def test_v61_skip_filtered():
    active = ea.load_trades(CSV)
    # 6 rows, 1 with v61_skip True -> 5 active
    assert len(active) == 5
    assert all(str(r.get("v61_skip")).lower() != "true" for r in active)


def test_pick_prefers_v61_columns():
    active = ea.load_trades(CSV)
    rep = ea.attribute(active)
    # netto comes from v61_netto_pnl, not netto_pnl
    assert rep["by_exit_reason"]["connors_sma5"]["sum_netto"] == 50.0


def test_to_float_edge_cases():
    assert ea._to_float("abc") is None
    assert ea._to_float("") is None
    assert ea._to_float("3.14") == 3.14
    assert ea._to_float(None) is None


def test_bucket_stats_win_rate():
    active = ea.load_trades(CSV)
    rep = ea.attribute(active)
    sl = rep["by_exit_reason"]["atr_stop_loss"]
    assert sl["n"] == 1
    assert sl["win_rate_pct"] == 0.0
    assert sl["sum_netto"] == -40.0


def test_edge_engine_is_signal_exit():
    active = ea.load_trades(CSV)
    rep = ea.attribute(active)
    v = rep["h2_verdict"]
    assert v["edge_engine"] == "connors_sma5"
    assert v["edge_engine_sum_netto"] == 50.0


def test_max_hold_drag_aggregation():
    active = ea.load_trades(CSV)
    rep = ea.attribute(active)
    v = rep["h2_verdict"]
    # only day3_flat_exit qualifies as forced max hold -> -10
    assert v["max_hold_drag_sum_netto"] == -10.0
    assert v["max_hold_drag_win_rate_pct"] == 0.0


def test_holding_day_axis_and_day4plus():
    active = ea.load_trades(CSV)
    rep = ea.attribute(active)
    bh = rep["by_holding_days"]
    # T1 = AAA(30)+? only AAA at hold1 active (EEE skipped) -> 30
    assert bh["1"]["sum_netto"] == 30.0
    # T2 = BBB(20)+FFF(12) = 32
    assert bh["2"]["sum_netto"] == 32.0
    v = rep["h2_verdict"]
    assert v["day4plus_bleed_sum_netto"] == -40.0


def test_frontload_ratio_guard_zero_total():
    # craft a corpus whose total nets to 0 -> ratio must be None, not crash
    csv = """date,ticker,regime,return_pct,netto_pnl,holding_days,exit_reason,v61_skip,v61_exit_reason,v61_netto_pnl
2024-01-01,AAA,Trending,1,50,1,connors_sma5,False,connors_sma5,50
2024-01-02,BBB,Neutral,-1,-50,4,atr_stop_loss,False,atr_stop_loss,-50
"""
    rep = ea.attribute(ea.load_trades(csv))
    assert rep["h2_verdict"]["total_netto"] == 0.0
    assert rep["h2_verdict"]["frontload_ratio_t1_t2"] is None


def test_render_md_no_raise():
    active = ea.load_trades(CSV)
    rep = ea.attribute(active)
    md = ea.render_md({"version": "t/test", "date": "2024-01-06",
                       "source_csv": "fixture", **rep})
    assert "Exit-Attribution" in md
    assert "H2-Verdikt" in md
    assert "connors_sma5" in md


def test_build_report_missing_file():
    rep = ea.build_report(Path("/nonexistent/does_not_exist.csv"), ea._date(2026, 6, 12))
    assert rep.get("error") == "source_unreadable"


def test_empty_active_returns_error(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("date,ticker,v61_skip\n2024-01-01,AAA,True\n")
    rep = ea.build_report(p, ea._date(2026, 6, 12))
    assert rep.get("error") == "no_active_trades"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
