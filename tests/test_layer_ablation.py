#!/usr/bin/env python3
"""Tests fuer tools/layer_ablation.py (H2-Baustein, 2026-06-01). Stdlib only, netzfrei.

Run ohne pytest:  python3 tests/test_layer_ablation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import layer_ablation as la  # noqa: E402
import build_ablation_panel as bap  # noqa: E402


def test_pearson_basic():
    xs = list(range(20))
    assert la.pearson(xs, xs) > 0.999          # perfekt positiv
    assert la.pearson(xs, [-x for x in xs]) < -0.999
    assert la.pearson([1, 2], [1, 2]) is None  # < 10 Punkte -> None


def test_selftest_identifies_signal_vs_noise():
    # Kern-Eigenschaft: echtes Signal (sector_rs) = KEEP, Rauschen != KEEP
    panel = la.make_synthetic(n=600, seed=7)
    rep = la.run_ablation(panel, top_pct=20.0)
    v = rep["verdicts"]
    assert v["sector_rs"]["verdict"] == "KEEP", v["sector_rs"]
    assert v["insider"]["verdict"] != "KEEP", v["insider"]
    assert v["news"]["verdict"] != "KEEP", v["news"]


def test_full_beats_base_on_topk():
    panel = la.make_synthetic(n=600, seed=7)
    rep = la.run_ablation(panel, top_pct=20.0)
    assert rep["configs"]["FULL"]["topk_avg_ret"] > rep["configs"]["BASE"]["topk_avg_ret"]


def test_threshold_sweep_monotone_count():
    panel = la.make_synthetic(n=300, seed=3)
    sweep = la.threshold_sweep(panel, lo=6.0, hi=9.0, step=0.5)
    counts = [s["n"] for s in sweep]
    assert counts == sorted(counts, reverse=True)   # hoehere Schwelle -> nie mehr Trades


def test_selftest_returns_zero():
    assert la.selftest() == 0


def test_extract_rows_from_scan():
    scan = {"scan_date": "2026-05-29", "ticker_map": {
        "AAA": {"ticker": "AAA", "date": "2026-05-29", "pre_score_raw": 6.80,
                 "sector_rs_adjust": 0.30, "insider_adjust": 0.10, "options_flow_adjust": 0.0,
                 "market_pcr_adjust": -0.10, "news_adjust": 0.0, "mtf_adjust": 0.15,
                 "weekly_trend": "bullish", "tier": "A", "region": "US"}}}
    rows = bap.extract_rows(scan)
    assert len(rows) == 1
    r = rows[0]
    assert r["base_score"] == 6.80
    assert r["adjusts"]["sector_rs"] == 0.30 and r["adjusts"]["mtf"] == 0.15
    assert r["adjusts"]["news"] == 0.0
    assert r["regime_ok"] is True
    assert r["tier"] == "A"
    assert r["forward_return"] is None


def test_compute_forward_join_and_horizon():
    rows = [{"ticker": "AAA", "date": "2026-01-05", "forward_return": None},
            {"ticker": "AAA", "date": "2026-01-12", "forward_return": None},  # zu nah am Ende
            {"ticker": "ZZZ", "date": "2026-01-05", "forward_return": None}]  # keine Kurse
    series = [("2026-01-05", 100.0), ("2026-01-06", 101.0), ("2026-01-07", 102.0),
              ("2026-01-08", 103.0), ("2026-01-09", 104.0), ("2026-01-12", 110.0)]
    filled = bap.compute_forward(rows, {"AAA": series}, horizon=5)
    assert filled == 1
    assert rows[0]["forward_return"] == round(110.0 / 100.0 - 1.0, 5)  # +0.10 nach 5 Tagen
    assert rows[1]["forward_return"] is None   # j out of range
    assert rows[2]["forward_return"] is None   # kein Kurs


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa
            fails += 1; print(f"FAIL  {t.__name__} -> {e}"); traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    raise SystemExit(1 if fails else 0)
