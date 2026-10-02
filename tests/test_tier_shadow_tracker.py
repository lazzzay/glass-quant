#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_tier_shadow_tracker.py: V7.39 TIER-SHADOW-TRACKER-1

Deckt die read-only Producer-Logik ab: gate-Extraktion, Capture-Snapshots,
immutabler Upsert, Handelstags-Forward-Mapping, Return-Rechnung, Backfill,
Readiness-Pane und den Counterfactual-Adapter. Kein Netz, kein echtes Journal.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import tier_shadow_tracker as tst  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _row(t, tier, score, close, atr=2.0, trad="direct"):
    return {"ticker": t, "tier": tier, "pre_score": score, "close": close,
            "atr_pct": atr, "tradability": trad, "sector": "X",
            "passes_threshold": True}


def _write_scan(d, date, rows):
    with open(os.path.join(d, f"scan_{date}.json"), "w", encoding="utf-8") as fh:
        json.dump({"scan_date": date, "universe_size": 100, "valid_results": 90,
                   "top_setups": rows}, fh)


def _write_recgate(d, date, regime, killed):
    sig = os.path.join(d, "signals")
    os.makedirs(sig, exist_ok=True)
    with open(os.path.join(sig, f"rec_gating_attribution_{date}.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"date": date, "ok": True, "regime": regime,
                   "steps": [{"name": "regime_allowed_tiers", "killed_tickers": []},
                             {"name": "regime_hard_tier", "killed_tickers": killed}]}, fh)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolierte SIGNALS_DIR/LEDGER_PATH im tmp; Loader lesen aus tmp."""
    sig = tmp_path / "signals"
    sig.mkdir()
    monkeypatch.setattr(tst, "SIGNALS_DIR", str(sig))
    monkeypatch.setattr(tst, "LEDGER_PATH", str(sig / "tier_shadow_ledger.json"))
    return str(tmp_path)


# --------------------------------------------------------------------------- #
# gate-Extraktion
# --------------------------------------------------------------------------- #
def test_killed_at_gate_picks_right_step():
    rg = {"steps": [{"name": "regime_allowed_tiers", "killed_tickers": ["X"]},
                    {"name": "regime_hard_tier", "killed_tickers": ["BBB", "CCC"]}]}
    assert tst.killed_at_gate(rg) == ["BBB", "CCC"]


def test_killed_at_gate_missing_gate():
    assert tst.killed_at_gate({"steps": [{"name": "score_action_buy",
                                          "killed_tickers": ["Z"]}]}) == []
    assert tst.killed_at_gate({}) == []


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
def test_capture_day_full_snapshot(sandbox):
    _write_scan(sandbox, "2099-03-01",
                [_row("AAA", "A", 9.0, 100.0), _row("BBB", "B", 9.5, 50.0),
                 _row("CCC", "C", 8.2, 20.0)])
    _write_recgate(sandbox, "2099-03-01", "Trending-Divergent", ["BBB", "CCC"])
    ents, meta = tst.capture_day("2099-03-01", tools_dir=sandbox)
    assert len(ents) == 2
    assert meta["regime"] == "Trending-Divergent"
    assert meta["gate_drop"] == 2
    bbb = [e for e in ents if e.ticker == "BBB"][0]
    assert bbb.tier == "B"
    assert bbb.entry_close == 50.0
    assert bbb.pre_score == 9.5
    assert bbb.killed_by_gate == "regime_hard_tier"
    assert bbb.forward_status == "PENDING"


def test_capture_zero_kill_day(sandbox):
    _write_scan(sandbox, "2099-03-02", [_row("AAA", "A", 9.0, 100.0)])
    _write_recgate(sandbox, "2099-03-02", "Trending", [])
    ents, meta = tst.capture_day("2099-03-02", tools_dir=sandbox)
    assert ents == []
    assert meta["gate_drop"] == 0


def test_capture_missing_scan_degrades(sandbox):
    _write_recgate(sandbox, "2099-03-03", "High-Vol-Krise", ["ZZZ"])
    ents, meta = tst.capture_day("2099-03-03", tools_dir=sandbox)
    assert len(ents) == 1
    assert ents[0].entry_close is None
    assert ents[0].forward_status == "NO_ENTRY_PRICE"


# --------------------------------------------------------------------------- #
# Upsert immutability
# --------------------------------------------------------------------------- #
def test_upsert_is_idempotent_and_immutable():
    led = {"version": "V7.39", "entries": []}
    e1 = tst.ShadowEntry(entry_date="2099-03-01", ticker="BBB", tier="B",
                         regime="Trending-Divergent", killed_by_gate="regime_hard_tier",
                         pre_score=9.5, entry_close=50.0, atr_pct=2.0)
    led, add, skip = tst.upsert_entries(led, [e1])
    assert (add, skip) == (1, 0)
    # Re-Upsert mit veraendertem Snapshot: darf Entry NICHT ueberschreiben
    e_mut = tst.ShadowEntry(entry_date="2099-03-01", ticker="BBB", tier="B",
                            regime="Trending-Divergent", killed_by_gate="regime_hard_tier",
                            pre_score=1.0, entry_close=999.0, atr_pct=9.9)
    led, add2, skip2 = tst.upsert_entries(led, [e_mut])
    assert (add2, skip2) == (0, 1)
    assert led["entries"][0]["entry_close"] == 50.0
    assert led["entries"][0]["pre_score"] == 9.5


# --------------------------------------------------------------------------- #
# Forward-Return-Rechnung
# --------------------------------------------------------------------------- #
def test_forward_trading_day_mapping_skips_weekend():
    e = tst.ShadowEntry(entry_date="2099-01-05", ticker="B", tier="B", regime="R",
                        killed_by_gate="regime_hard_tier", pre_score=9.0,
                        entry_close=50.0, atr_pct=2.0)
    closes = {"2099-01-05": 50.0, "2099-01-06": 55.0, "2099-01-07": 51.0,
              "2099-01-08": 52.5, "2099-01-11": 58.0, "2099-01-12": 60.0}
    fwd, st = tst.compute_forward(e, closes)
    assert st == "FILLED"
    assert fwd["h1"]["return_pct"] == 10.0        # 55/50 - 1
    assert fwd["h3"]["date"] == "2099-01-08"      # 09/10 = Wochenende uebersprungen
    assert fwd["h5"]["return_pct"] == 20.0        # 60/50 - 1


def test_forward_partial_when_window_immature():
    e = tst.ShadowEntry(entry_date="2099-01-05", ticker="B", tier="B", regime="R",
                        killed_by_gate="regime_hard_tier", pre_score=9.0,
                        entry_close=50.0, atr_pct=2.0)
    closes = {"2099-01-05": 50.0, "2099-01-06": 55.0, "2099-01-07": 51.0}  # nur h1,h2
    fwd, st = tst.compute_forward(e, closes)
    assert st == "PARTIAL"
    assert fwd["h1"]["return_pct"] == 10.0
    assert fwd["h5"]["return_pct"] is None


def test_forward_no_entry_price():
    e = tst.ShadowEntry(entry_date="2099-01-05", ticker="B", tier="B", regime="R",
                        killed_by_gate="regime_hard_tier", pre_score=9.0,
                        entry_close=None, atr_pct=2.0)
    fwd, st = tst.compute_forward(e, {"2099-01-06": 55.0})
    assert st == "NO_ENTRY_PRICE"
    assert fwd == {}


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #
def test_backfill_injected_fetcher_fills():
    led = {"version": "V7.39", "entries": [
        tst.asdict(tst.ShadowEntry(entry_date="2099-01-05", ticker="BBB", tier="B",
                                   regime="Trending-Divergent",
                                   killed_by_gate="regime_hard_tier", pre_score=9.5,
                                   entry_close=50.0, atr_pct=2.0))]}
    series = {"BBB": {"2099-01-05": 50.0, "2099-01-06": 55.0, "2099-01-07": 51.0,
                      "2099-01-08": 52.5, "2099-01-11": 58.0, "2099-01-12": 60.0}}
    led, n = tst.backfill(led, lambda tk, s: series.get(tk, {}))
    assert n == 1
    assert led["entries"][0]["forward_status"] == "FILLED"
    assert led["entries"][0]["forward"]["h1"]["return_pct"] == 10.0


def test_backfill_empty_feed_keeps_pending_no_fabrication():
    led = {"version": "V7.39", "entries": [
        tst.asdict(tst.ShadowEntry(entry_date="2099-01-05", ticker="BBB", tier="B",
                                   regime="R", killed_by_gate="regime_hard_tier",
                                   pre_score=9.5, entry_close=50.0, atr_pct=2.0))]}
    led, n = tst.backfill(led, lambda tk, s: {})
    assert n == 0
    assert led["entries"][0]["forward_status"] == "PENDING"
    assert led["entries"][0]["forward"] == {}


def test_backfill_fetcher_exception_is_swallowed():
    led = {"version": "V7.39", "entries": [
        tst.asdict(tst.ShadowEntry(entry_date="2099-01-05", ticker="BBB", tier="B",
                                   regime="R", killed_by_gate="regime_hard_tier",
                                   pre_score=9.5, entry_close=50.0, atr_pct=2.0))]}

    def boom(tk, s):
        raise RuntimeError("network down")

    led, n = tst.backfill(led, boom)
    assert n == 0
    assert led["entries"][0]["forward_status"] == "PENDING"


# --------------------------------------------------------------------------- #
# Readiness + Adapter
# --------------------------------------------------------------------------- #
def _filled_entry(date, tk, tier, regime, ret):
    return tst.asdict(tst.ShadowEntry(
        entry_date=date, ticker=tk, tier=tier, regime=regime,
        killed_by_gate="regime_hard_tier", pre_score=9.0, entry_close=100.0,
        atr_pct=2.0, forward={"h3": {"date": "x", "close": 100 * (1 + ret / 100),
                                     "return_pct": ret}}, forward_status="FILLED"))


def test_readiness_below_floor_not_answerable():
    led = {"version": "V7.39", "entries": [
        _filled_entry("2099-01-01", "A", "B", "Trending-Divergent", 1.0),
        _filled_entry("2099-01-02", "B", "B", "Trending-Divergent", -2.0)]}
    rd = tst.counterfactual_readiness(led)
    assert rd["total_usable"] == 2
    assert rd["answerable"] is False
    cell = [c for c in rd["cells"] if c["tier"] == "B"][0]
    assert cell["remaining_to_floor"] == tst.COUNTERFACTUAL_FLOOR - 2
    assert cell["mean_return_pct_h3"] == pytest.approx(-0.5)


def test_readiness_at_floor_answerable():
    led = {"version": "V7.39", "entries": [
        _filled_entry(f"2099-02-{i+1:02d}", f"T{i}", "B", "Trending-Divergent", 2.0)
        for i in range(tst.COUNTERFACTUAL_FLOOR)]}
    rd = tst.counterfactual_readiness(led)
    assert rd["answerable"] is True
    assert rd["cells_at_floor"] == 1


def test_to_counterfactual_rows_shape_and_filter():
    led = {"version": "V7.39", "entries": [
        _filled_entry("2099-01-01", "A", "B", "Trending-Divergent", 3.0),
        tst.asdict(tst.ShadowEntry(entry_date="2099-01-02", ticker="P", tier="C",
                                   regime="Trending-Divergent",
                                   killed_by_gate="regime_hard_tier", pre_score=8.0,
                                   entry_close=10.0, atr_pct=2.0))]}  # PENDING -> excluded
    rows = tst.to_counterfactual_rows(led)
    assert len(rows) == 1
    r = rows[0]
    assert r["regime"] == "Trending-Divergent"
    assert r["tier"] == "B"
    assert r["return_pct"] == 3.0
    assert r["is_win"] is True
    assert r["year"] == 2099
    assert r["v61_netto_pnl"] is None
    assert r["source"] == "tier_shadow_tracker"


# --------------------------------------------------------------------------- #
# Ledger round-trip
# --------------------------------------------------------------------------- #
def test_ledger_save_load_roundtrip(sandbox):
    led = tst.load_ledger(tst.LEDGER_PATH)
    e = tst.ShadowEntry(entry_date="2099-04-01", ticker="BBB", tier="B",
                        regime="Trending-Divergent", killed_by_gate="regime_hard_tier",
                        pre_score=9.5, entry_close=50.0, atr_pct=2.0)
    led, _, _ = tst.upsert_entries(led, [e])
    p = tst.save_ledger(led, tst.LEDGER_PATH)
    led2 = tst.load_ledger(p)
    assert len(led2["entries"]) == 1
    typed = tst._entries_from_ledger(led2)
    assert typed and isinstance(typed[0], tst.ShadowEntry)
    assert typed[0].ticker == "BBB"


def test_selftest_passes():
    assert tst.selftest() == 0
