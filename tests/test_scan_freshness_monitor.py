"""Pytest-Suite fuer tools/scan_freshness_monitor.py (V7.9.36 · 2026-05-27)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import scan_freshness_monitor as sfm  # noqa: E402


# ---------------------------------------------------------------------------
# Single-Day-Tests
# ---------------------------------------------------------------------------

def test_t01_classify_missing_file():
    art = {"exists": False, "parsed": False, "data": None,
           "size_b": None, "error": "file_not_found"}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_missing"
    assert v["severity"] == "BLACK"
    assert v["primary_target"] == "morning_cron_unavailable"


def test_t02_classify_corrupt_json():
    art = {"exists": True, "parsed": False, "data": None,
           "size_b": 100, "error": "json_parse_error: ValueError"}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_corrupt"
    assert v["severity"] == "BLACK"


def test_t03_classify_missing_top_setups():
    art = {"exists": True, "parsed": True,
           "data": {"valid_results": 30, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_corrupt"
    assert v["primary_target"] == "scan_schema_drift"


def test_t04_classify_fresh():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [{"ticker": "AAPL"}],
                    "valid_results": 60, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_fresh"
    assert v["severity"] == "GREEN"
    assert v["fallback_age_werktage"] == 0


def test_t05_classify_stale_short_age_1():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-26.json",
                    "_fallback_age_werktage": 1,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_stale_short"
    assert v["severity"] == "YELLOW"


def test_t06_classify_stale_short_age_2():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-25.json",
                    "_fallback_age_werktage": 2,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_stale_short"
    assert v["severity"] == "YELLOW"


def test_t07_classify_stale_long_age_3():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-22.json",
                    "_fallback_age_werktage": 3,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_stale_long"
    assert v["severity"] == "RED"
    assert v["primary_target"] == "mac_cron_outage"


def test_t08_classify_stale_long_age_5_capped():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-20.json",
                    "_fallback_age_werktage": 5,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_stale_long"


def test_t09_classify_fallback_without_age():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-22.json",
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_corrupt"


def test_t10_classify_fallback_age_invalid_string():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-22.json",
                    "_fallback_age_werktage": "not-an-int",
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    assert v["verdict"] == "scan_corrupt"


def test_t11_classify_custom_threshold():
    """stale_long_threshold konfigurierbar machen: age=2 mit threshold=2 -> long."""
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "_fallback_from": "scan_2026-05-25.json",
                    "_fallback_age_werktage": 2,
                    "valid_results": 59, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art, stale_long_threshold=2)
    assert v["verdict"] == "scan_stale_long"


# ---------------------------------------------------------------------------
# Range-Mode-Tests
# ---------------------------------------------------------------------------

def test_t12_iter_workdays_skips_weekends():
    days = sfm._iter_workdays("2026-05-22", "2026-05-26")
    assert days == ["2026-05-22", "2026-05-25", "2026-05-26"]


def test_t13_iter_workdays_empty_when_reversed():
    days = sfm._iter_workdays("2026-05-27", "2026-05-25")
    assert days == []


def test_t14_aggregate_range_insufficient_data(tmp_path):
    # nur 1 Werktag -> insufficient_data
    (tmp_path / "scan_2026-05-25.json").write_text(json.dumps({
        "top_setups": [], "valid_results": 60, "universe_size": 60,
    }))
    agg = sfm.aggregate_range("2026-05-25", "2026-05-25", tools_dir=tmp_path)
    assert agg["verdict"]["verdict"] == "range_insufficient_data"
    assert agg["verdict"]["severity"] == "BLACK"


def test_t15_aggregate_range_fresh(tmp_path):
    for d in ["2026-05-25", "2026-05-26", "2026-05-27"]:
        (tmp_path / f"scan_{d}.json").write_text(json.dumps({
            "top_setups": [], "valid_results": 60, "universe_size": 60,
        }))
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    assert agg["verdict"]["verdict"] == "range_fresh"
    assert agg["verdict"]["severity"] == "GREEN"


def test_t16_aggregate_range_recovered(tmp_path):
    (tmp_path / "scan_2026-05-25.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-22.json",
        "_fallback_age_werktage": 3, "valid_results": 59, "universe_size": 60,
    }))
    (tmp_path / "scan_2026-05-26.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-22.json",
        "_fallback_age_werktage": 4, "valid_results": 59, "universe_size": 60,
    }))
    (tmp_path / "scan_2026-05-27.json").write_text(json.dumps({
        "top_setups": [], "valid_results": 60, "universe_size": 60,
    }))
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    assert agg["verdict"]["verdict"] == "range_recovered"
    assert agg["verdict"]["severity"] == "GREEN"


def test_t17_aggregate_range_chronic_stale(tmp_path):
    for i, d in enumerate(["2026-05-25", "2026-05-26", "2026-05-27"]):
        (tmp_path / f"scan_{d}.json").write_text(json.dumps({
            "top_setups": [], "_fallback_from": "scan_2026-05-20.json",
            "_fallback_age_werktage": 3 + i,
            "valid_results": 59, "universe_size": 60,
        }))
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    assert agg["verdict"]["verdict"] == "range_chronic_stale"
    assert agg["verdict"]["severity"] == "RED"
    assert agg["verdict"]["primary_target"] == "mac_cron_outage"


def test_t18_aggregate_range_chronic_missing(tmp_path):
    # 3 Werktage ohne irgendeine Datei
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    assert agg["verdict"]["verdict"] == "range_chronic_missing"
    assert agg["verdict"]["severity"] == "BLACK"


def test_t19_aggregate_range_persistent_stale_mixed_severity(tmp_path):
    # 100% non-GREEN: 1x stale_short (YELLOW) + 2x stale_long (RED) ohne 3-Streak
    # Reihenfolge: long, short, long → keine 3er-Folge eines Verdicts
    (tmp_path / "scan_2026-05-25.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-20.json",
        "_fallback_age_werktage": 3, "valid_results": 59, "universe_size": 60,
    }))
    (tmp_path / "scan_2026-05-26.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-25.json",
        "_fallback_age_werktage": 1, "valid_results": 59, "universe_size": 60,
    }))
    (tmp_path / "scan_2026-05-27.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-20.json",
        "_fallback_age_werktage": 4, "valid_results": 59, "universe_size": 60,
    }))
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    assert agg["verdict"]["verdict"] == "range_persistent_stale"
    assert agg["verdict"]["severity"] == "RED"


def test_t20_aggregate_range_mixed(tmp_path):
    # 1 GREEN + 2 non-GREEN ohne 80% GREEN und ohne 100% non-GREEN
    (tmp_path / "scan_2026-05-25.json").write_text(json.dumps({
        "top_setups": [], "valid_results": 60, "universe_size": 60,
    }))  # GREEN
    (tmp_path / "scan_2026-05-26.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-22.json",
        "_fallback_age_werktage": 3, "valid_results": 59, "universe_size": 60,
    }))  # RED
    (tmp_path / "scan_2026-05-27.json").write_text(json.dumps({
        "top_setups": [], "_fallback_from": "scan_2026-05-22.json",
        "_fallback_age_werktage": 4, "valid_results": 59, "universe_size": 60,
    }))  # RED
    # Wichtig: 1 GREEN am Anfang -> nicht recovered (letzter Tag ist RED)
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    # 2/3 = 66.7% RED -> nicht chronic (Streak=2 <3) -> mixed (nicht 100% non-GREEN
    # weil Tag 1 GREEN)
    assert agg["verdict"]["verdict"] == "range_mixed"
    assert agg["verdict"]["severity"] == "YELLOW"


def test_t21_render_single_json_schema():
    art = {"exists": True, "parsed": True,
           "data": {"top_setups": [], "valid_results": 60, "universe_size": 60},
           "size_b": 1000, "error": None}
    v = sfm.classify_single("2026-05-27", art)
    payload = sfm.render_single_json(v, art)
    assert payload["tool"] == "scan_freshness_monitor"
    assert payload["mode"] == "single"
    assert payload["version"] in {"V7.9.36", "V7.9.37"}  # forward-compat
    assert payload["date"] == "2026-05-27"
    assert payload["verdict"]["severity"] == "GREEN"


def test_t22_render_range_markdown(tmp_path):
    for d in ["2026-05-25", "2026-05-26", "2026-05-27"]:
        (tmp_path / f"scan_{d}.json").write_text(json.dumps({
            "top_setups": [], "valid_results": 60, "universe_size": 60,
        }))
    agg = sfm.aggregate_range("2026-05-25", "2026-05-27", tools_dir=tmp_path)
    payload = sfm.render_range_json(agg)
    md = sfm.render_range_markdown(payload)
    assert "Scan-Freshness-Monitor" in md
    assert "range_fresh" in md
    assert "Per-Day-Tabelle" in md


def test_t23_range_exit_codes():
    assert sfm.range_severity_to_exit_code("GREEN", "range_fresh") == 0
    assert sfm.range_severity_to_exit_code("YELLOW", "range_mixed") == 1
    assert sfm.range_severity_to_exit_code("RED", "range_chronic_stale") == 2
    assert sfm.range_severity_to_exit_code("BLACK", "range_chronic_missing") == 3
    assert sfm.range_severity_to_exit_code("BLACK", "range_insufficient_data") == 4
