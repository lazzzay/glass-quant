#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_tier_shadow_archive.py: V7.39 Scan-Archiv-Forward-Feed

Der Default-Backfill-Feed liest Forward-Closes aus dem eigenen Scan-Archiv
(scan_<date>.json -> ticker_map). Getestet: load_scan_closes, Archiv-Bau,
make_archive_fetcher-Closure, end-to-end Backfill ueber das Archiv.
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


def _write_scan(d, date, ticker_map, top=None):
    with open(os.path.join(d, f"scan_{date}.json"), "w", encoding="utf-8") as fh:
        json.dump({"scan_date": date, "ticker_map": ticker_map,
                   "top_setups": top or []}, fh)


def test_load_scan_closes_from_ticker_map(tmp_path):
    _write_scan(str(tmp_path), "2099-05-01",
                {"BBB": {"close": 50.0}, "AAA": {"close": 10.0}})
    cl = tst.load_scan_closes("2099-05-01", tools_dir=str(tmp_path))
    assert cl["BBB"] == 50.0
    assert cl["AAA"] == 10.0


def test_load_scan_closes_top_setups_fallback(tmp_path):
    # kein ticker_map -> top_setups als Fallback
    with open(os.path.join(str(tmp_path), "scan_2099-05-02.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"scan_date": "2099-05-02",
                   "top_setups": [{"ticker": "ZZZ", "close": 7.5}]}, fh)
    cl = tst.load_scan_closes("2099-05-02", tools_dir=str(tmp_path))
    assert cl["ZZZ"] == 7.5


def test_build_scan_archive_explicit_dates(tmp_path):
    _write_scan(str(tmp_path), "2099-05-01", {"BBB": {"close": 50.0}})
    _write_scan(str(tmp_path), "2099-05-04", {"BBB": {"close": 55.0}})
    arch = tst.build_scan_archive(tools_dir=str(tmp_path),
                                  dates=["2099-05-01", "2099-05-04"])
    assert arch["2099-05-01"]["BBB"] == 50.0
    assert arch["2099-05-04"]["BBB"] == 55.0


def test_make_archive_fetcher_returns_forward_only():
    archive = {"2099-05-01": {"BBB": 50.0}, "2099-05-04": {"BBB": 55.0},
               "2099-05-05": {"BBB": 60.0}, "2099-04-30": {"BBB": 44.0}}
    fetch = tst.make_archive_fetcher(archive)
    got = fetch("BBB", "2099-05-01")
    # enthaelt entry-Tag + alle spaeteren, NICHT den frueheren 04-30
    assert got["2099-05-04"] == 55.0
    assert got["2099-05-05"] == 60.0
    assert "2099-04-30" not in got


def test_archive_backfill_end_to_end():
    archive = {"2099-05-01": {"BBB": 50.0}, "2099-05-04": {"BBB": 55.0},
               "2099-05-05": {"BBB": 60.0}}
    fetch = tst.make_archive_fetcher(archive)
    led = {"version": "V7.39", "entries": [
        tst.asdict(tst.ShadowEntry(entry_date="2099-05-01", ticker="BBB", tier="B",
                                   regime="Trending-Divergent",
                                   killed_by_gate="regime_hard_tier", pre_score=9.0,
                                   entry_close=50.0, atr_pct=2.0))]}
    led, n = tst.backfill(led, fetch, horizons=(1, 2))
    assert n == 1
    e = led["entries"][0]
    assert e["forward"]["h1"]["return_pct"] == 10.0     # 55/50-1
    assert e["forward"]["h2"]["return_pct"] == 20.0     # 60/50-1
    assert e["forward_status"] == "FILLED"


def test_archive_fetcher_missing_ticker_yields_empty():
    fetch = tst.make_archive_fetcher({"2099-05-04": {"AAA": 10.0}})
    assert fetch("BBB", "2099-05-01") == {}
