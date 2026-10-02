#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests fuer tools/rec_gating_attribution.py (REC-GATING-ATTRIBUTION-1, V7.35)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import rec_gating_attribution as rga  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures / Helpers
# --------------------------------------------------------------------------
def _write_universe(tmp):
    with open(os.path.join(tmp, "universe.csv"), "w", encoding="utf-8") as fh:
        fh.write("ticker,name,tier,asset_class,region,sector,atr_multi_sl,atr_multi_tp,score_threshold\n")
        fh.write("AAA,AlphaA,A,us_large,US,Tech,1.7,2.55,7.5\n")
        fh.write("BBB,BetaB,B,eu_large,DE,Materials,1.7,2.55,7.5\n")
        fh.write("CCC,GammaC,C,us_small,US,Growth,1.7,2.55,7.5\n")


def _row(t, tier, score, trad="direct", passes=True):
    return {"ticker": t, "tier": tier, "pre_score": score,
            "tradability": trad, "passes_threshold": passes}


def _write_scan(tmp, date, rows, universe=100, valid=90):
    with open(os.path.join(tmp, f"scan_{date}.json"), "w", encoding="utf-8") as fh:
        json.dump({"scan_date": date, "universe_size": universe,
                   "valid_results": valid, "top_setups": rows}, fh)


def _write_regime(tmp, date, regime, allowed):
    with open(os.path.join(tmp, f"regime_{date}.json"), "w", encoding="utf-8") as fh:
        json.dump({"date": date, "regime": regime, "allowed_tiers": allowed}, fh)


@pytest.fixture
def env(tmp_path):
    tmp = str(tmp_path)
    _write_universe(tmp)
    return tmp


# --------------------------------------------------------------------------
# Kern-Tests
# --------------------------------------------------------------------------
def test_selftest_green():
    assert rga._selftest() == 0


def test_trending_divergent_tier_a_only_kills_b_and_c(env):
    d = "2099-02-01"
    # 3 Tier-B + 1 Tier-C + 1 Tier-A -> hard-tier is the clear bottleneck (drops 3 B),
    # allowed-tiers drops only 1 C (mirrors real 06-30: 23 B vs 4 C).
    _write_scan(env, d, [_row("AAA", "A", 9.0), _row("BBB", "B", 9.5),
                         _row("B2", "B", 9.4), _row("B3", "B", 9.1),
                         _row("CCC", "C", 8.0)])
    _write_regime(env, d, "Trending-Divergent", ["A", "B"])
    r = rga.diagnose_day(d, tools_dir=env)
    assert r.n_passes == 5
    # allowed A/B drops C (1); hard-rule A drops the 3 B -> only AAA
    assert r.mechanical_eligible_n == 1
    assert r.bottleneck_gate == "regime_hard_tier"


def test_all_tier_b_gives_zero_eligible_gated_by_regime(env):
    d = "2099-02-02"
    _write_scan(env, d, [_row("BBB", "B", 9.5), _row("B2", "B", 8.8)])
    _write_regime(env, d, "Trending-Divergent", ["A", "B"])
    r = rga.diagnose_day(d, tools_dir=env)
    assert r.mechanical_eligible_n == 0
    assert r.verdict == "GATED_BY_REGIME_TIER"


def test_max_recs_cap_keeps_highest_scores(env):
    d = "2099-02-03"
    rows = [_row(f"T{i}", "A", 9.9 - i * 0.1) for i in range(6)]
    _write_scan(env, d, rows)
    _write_regime(env, d, "Trending", ["A", "B", "C"])
    r = rga.diagnose_day(d, tools_dir=env)
    assert r.mechanical_eligible_n == 3
    assert r.bottleneck_gate == "regime_max_recs"
    assert [x["ticker"] for x in r.eligible_sample] == ["T0", "T1", "T2"]


def test_tradability_gate_drops_hebel_only(env):
    d = "2099-02-04"
    _write_scan(env, d, [_row("AAA", "A", 9.0, trad="hebel_only"),
                         _row("A2", "A", 8.0, trad="direct")])
    _write_regime(env, d, "Neutral", ["A", "B", "C"])
    r = rga.diagnose_day(d, tools_dir=env)
    step = next(s for s in r.steps if s.name == "tradability_direct")
    assert step.dropped == 1
    assert "AAA" in step.killed_tickers


def test_blacklist_gate(env, monkeypatch):
    # Die User-Blacklist ist per Default leer -> synthetische Test-Blacklist injizieren.
    monkeypatch.setattr(rga, "PERSONAL_BLACKLIST", frozenset({"EEE"}))
    d = "2099-02-05"
    _write_scan(env, d, [_row("EEE", "A", 9.9), _row("AAA", "A", 8.0)])
    _write_regime(env, d, "Neutral", ["A", "B", "C"])
    r = rga.diagnose_day(d, tools_dir=env)
    step = next(s for s in r.steps if s.name == "blacklist")
    assert step.dropped == 1
    assert "EEE" in step.killed_tickers


def test_score_gate_drops_below_action_buy(env):
    d = "2099-02-06"
    _write_scan(env, d, [_row("AAA", "A", 6.9), _row("A2", "A", 7.1)])
    _write_regime(env, d, "Neutral", ["A", "B", "C"])
    r = rga.diagnose_day(d, tools_dir=env)
    step = next(s for s in r.steps if s.name == "score_action_buy")
    assert step.dropped == 1


def test_missing_scan_returns_no_scan(env):
    r = rga.diagnose_day("2099-12-31", tools_dir=env)
    assert not r.ok
    assert r.verdict == "NO_SCAN"


def test_gate_order_preserved(env):
    d = "2099-02-07"
    _write_scan(env, d, [_row("AAA", "A", 9.0)])
    _write_regime(env, d, "Neutral", ["A", "B", "C"])
    r = rga.diagnose_day(d, tools_dir=env)
    assert [s.name for s in r.steps] == rga.GATE_ORDER


def test_tier_taken_from_universe_when_missing_in_scan(env):
    d = "2099-02-08"
    # top_setups without 'tier' -> resolved via universe.csv (BBB=B)
    row = {"ticker": "BBB", "pre_score": 9.0, "tradability": "direct",
           "passes_threshold": True}
    _write_scan(env, d, [row])
    _write_regime(env, d, "Trending-Divergent", ["A", "B"])
    r = rga.diagnose_day(d, tools_dir=env)
    # BBB is Tier B -> killed by Tier-A-only hard rule
    assert r.mechanical_eligible_n == 0
    assert r.tier_dist_in.get("B") == 1


def test_only_passes_threshold_rows_enter(env):
    d = "2099-02-09"
    _write_scan(env, d, [_row("AAA", "A", 9.0, passes=True),
                         _row("A2", "A", 9.5, passes=False)])
    _write_regime(env, d, "Neutral", ["A", "B", "C"])
    r = rga.diagnose_day(d, tools_dir=env)
    assert r.n_passes == 1


def test_live_rec_from_md_parse(tmp_path, monkeypatch):
    root = str(tmp_path)
    with open(os.path.join(root, "Trading-Empfehlung-V6.1-2099-03-01.md"), "w", encoding="utf-8") as fh:
        fh.write("# head\n**Verdikt:** **2 Empfehlungen: AAA, BBB.** Hebel AUS.\n")
    assert rga.load_live_rec_from_md("2099-03-01", root=root) == 2


def test_live_rec_from_md_no_trade(tmp_path):
    root = str(tmp_path)
    with open(os.path.join(root, "Trading-Empfehlung-V6.1-2099-03-02.md"), "w", encoding="utf-8") as fh:
        fh.write("**Verdikt:** **NO-TRADE.** 0 offene Positionen.\n")
    assert rga.load_live_rec_from_md("2099-03-02", root=root) == 0


def test_discipline_residual_verdict(env, monkeypatch):
    d = "2099-02-10"
    _write_scan(env, d, [_row("AAA", "A", 9.0), _row("A2", "A", 8.6)])
    _write_regime(env, d, "Neutral", ["A", "B", "C"])
    # force live_rec_count = 1 via monkeypatch of loader
    monkeypatch.setattr(rga, "load_live_rec_count", lambda ds, tools_dir=env: 1)
    r = rga.diagnose_day(d, tools_dir=env)
    assert r.mechanical_eligible_n == 2
    assert r.live_rec_n == 1
    assert r.qualitative_residual == 1
    assert r.verdict == "ELIGIBLE_DISCIPLINE_RESIDUAL"


def test_render_md_smoke(env):
    d = "2099-02-11"
    _write_scan(env, d, [_row("AAA", "A", 9.0), _row("BBB", "B", 9.5)])
    _write_regime(env, d, "Trending-Divergent", ["A", "B"])
    r = rga.diagnose_day(d, tools_dir=env)
    md = rga.render_md(r)
    assert "Gate-Kette" in md
    assert "regime_hard_tier" in md


def test_range_smoke(env):
    for i, d in enumerate(["2099-04-01", "2099-04-02"]):
        _write_scan(env, d, [_row("AAA", "A", 9.0)])
        _write_regime(env, d, "Neutral", ["A", "B", "C"])
    reps = [rga.diagnose_day(x, tools_dir=env) for x in ["2099-04-01", "2099-04-02"]]
    rmd = rga.render_range_md(reps)
    assert "Range" in rmd
    assert len(reps) == 2
