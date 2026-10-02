#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests fuer tools/adjudication_gate.py (ADJUDICATION-DATA-QUALITY-GATE-1, V7.36)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import adjudication_gate as ag  # noqa: E402

D = "2026-07-02"


# --------------------------------------------------------------------------
# Fixtures / Helpers
# --------------------------------------------------------------------------
@pytest.fixture()
def base(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "signals").mkdir()
    return str(tmp_path)


def _w(base_dir, rel, payload):
    p = os.path.join(base_dir, rel)
    with open(p, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)


def _scan(date=D, uni=639, valid=620, **extra):
    d = {"scan_date": date, "universe_size": uni, "valid_results": valid, "top_setups": []}
    d.update(extra)
    return d


def _dh(status="OK", h_codes=None, date=D):
    return {"date": date, "overall_status": status,
            "findings": [{"severity": "H", "source": "scan", "code": c} for c in (h_codes or [])]}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_healthy_scan_and_health_allowed(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=615))
    _w(base, "tools/data_health_%s.json" % D, _dh("OK"))
    r = ag.evaluate(D, base)
    assert r["verdict"] == ag.VERDICT_ALLOWED
    assert r["rc"] == ag.RC_OK
    assert r["no_trade_reason"] is None
    assert any("✅" in x for x in r["briefing_source_rows"])


def test_fill_rate_computed(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=615))
    _w(base, "tools/data_health_%s.json" % D, _dh("OK"))
    r = ag.evaluate(D, base)
    assert abs(r["fill_rate"] - 615 / 639.0) < 1e-3  # Feld ist auf 4 Dezimalen gerundet


def test_floor_boundary_exact_pass(base):
    # exakt am Floor (>=) muss PASSieren: konsistent zu V7.32 valid_fill_guard
    _w(base, "tools/scan_%s.json" % D, _scan(uni=100, valid=80))
    _w(base, "tools/data_health_%s.json" % D, _dh("OK"))
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_OK


# --------------------------------------------------------------------------
# Gate 3: SCAN_FILL, das 07-01/07-02-Loch
# --------------------------------------------------------------------------
def test_subfloor_66pct_blocks_like_0702(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=422))  # 66,0 %
    _w(base, "tools/data_health_%s.json" % D, _dh("CRITICAL", ["SCAN_FILL_LT_80"]))
    r = ag.evaluate(D, base)
    assert r["verdict"] == ag.VERDICT_NO_TRADE
    assert r["no_trade_reason"] == "SCAN_FILL_BELOW_FLOOR"
    assert r["rc"] == ag.RC_SCAN_FILL


def test_subfloor_368pct_blocks_like_0701(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=235))  # 36,8 %
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_SCAN_FILL
    assert any("SUB-FLOOR" in x for x in r["briefing_source_rows"])


def test_custom_floor_respected(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=422))
    _w(base, "tools/data_health_%s.json" % D, _dh("OK"))
    r = ag.evaluate(D, base, fill_floor=0.50)  # 66 % >= 50 %
    assert r["rc"] == ag.RC_OK


def test_missing_universe_size_fail_safe(base):
    _w(base, "tools/scan_%s.json" % D, {"scan_date": D, "top_setups": []})
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_SCAN_FILL  # nicht pruefbar => fail-safe NO_TRADE


# --------------------------------------------------------------------------
# Gate 1: SCAN_PRESENT
# --------------------------------------------------------------------------
def test_scan_missing_blocks(base):
    r = ag.evaluate(D, base)
    assert r["verdict"] == ag.VERDICT_NO_TRADE
    assert r["rc"] == ag.RC_SCAN_MISSING


def test_scan_missing_with_abort_context(base):
    _w(base, "tools/scan_abort_%s.json" % D, {"reason": "VALID_FILL_FLOOR"})
    r = ag.evaluate(D, base)
    sp = [c for c in r["checks"] if c["id"] == "SCAN_PRESENT"][0]
    assert sp["detail"]["scan_abort_present"] is True
    assert sp["detail"]["scan_abort_reason"] == "VALID_FILL_FLOOR"


def test_corrupt_scan_json_fail_safe_no_crash(base):
    _w(base, "tools/scan_%s.json" % D, "{broken")
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_SCAN_MISSING
    assert "PARSE_ERROR" in str(r["checks"][0]["detail"]["error"])


# --------------------------------------------------------------------------
# Gate 2: SCAN_DATE_MATCH
# --------------------------------------------------------------------------
def test_stale_scan_date_blocks(base):
    _w(base, "tools/scan_%s.json" % D, _scan(date="2026-07-01", valid=615))
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_STALE_SCAN
    assert r["no_trade_reason"] == "STALE_SCAN"


# --------------------------------------------------------------------------
# Gate 4: DATA_HEALTH
# --------------------------------------------------------------------------
def test_data_health_critical_blocks_even_with_good_fill(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=615))
    _w(base, "tools/data_health_%s.json" % D, _dh("CRITICAL", ["PIPELINE_NOT_RUN"]))
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_DATA_HEALTH
    assert r["no_trade_reason"] == "DATA_HEALTH_CRITICAL"
    assert any("PIPELINE_NOT_RUN" in x for x in r["briefing_source_rows"])


def test_data_health_absent_warns_but_allows(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=615))
    r = ag.evaluate(D, base)
    assert r["verdict"] == ag.VERDICT_ALLOWED_WARN
    assert r["rc"] == ag.RC_OK
    assert r["warnings"]


def test_data_health_nonok_noncritical_warns(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=615))
    _w(base, "tools/data_health_%s.json" % D, _dh("DEGRADED"))
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_OK
    assert r["verdict"] == ag.VERDICT_ALLOWED_WARN


def test_first_violation_priority_fill_before_health(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=235))
    _w(base, "tools/data_health_%s.json" % D, _dh("CRITICAL", ["SCAN_FILL_LT_80"]))
    r = ag.evaluate(D, base)
    assert r["rc"] == ag.RC_SCAN_FILL  # Fill (timing-unabhaengig) hat Prioritaet


# --------------------------------------------------------------------------
# Artefakt / CLI / rc-Vertrag
# --------------------------------------------------------------------------
def test_write_artifact_roundtrip(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=422))
    r = ag.evaluate(D, base)
    out = ag.write_artifact(r, base)
    assert os.path.exists(out)
    with open(out, encoding="utf-8") as fh:
        back = json.load(fh)
    assert back["rc"] == ag.RC_SCAN_FILL
    assert back["verdict"] == ag.VERDICT_NO_TRADE
    assert back["briefing_source_rows"]


def test_rc_contract_values_stable():
    # rc-Vertrag ist API: Aenderung waere Breaking Change fuer den Workflow
    assert (ag.RC_OK, ag.RC_TOOL_ERROR, ag.RC_SCAN_MISSING,
            ag.RC_SCAN_FILL, ag.RC_DATA_HEALTH, ag.RC_STALE_SCAN) == (0, 1, 2, 3, 4, 5)


def test_render_stdout_contains_verdict_and_rows(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=422))
    r = ag.evaluate(D, base)
    txt = ag.render_stdout(r)
    assert "VERDIKT: NO_TRADE" in txt
    assert "SUB-FLOOR" in txt


def test_main_invalid_date_tool_error():
    assert ag.main(["--date", "02.07.2026"]) == ag.RC_TOOL_ERROR


def test_evaluate_is_read_only(base):
    _w(base, "tools/scan_%s.json" % D, _scan(valid=615))
    _w(base, "tools/data_health_%s.json" % D, _dh("OK"))
    before = sorted(os.listdir(os.path.join(base, "tools"))) + sorted(os.listdir(os.path.join(base, "signals")))
    ag.evaluate(D, base)
    after = sorted(os.listdir(os.path.join(base, "tools"))) + sorted(os.listdir(os.path.join(base, "signals")))
    assert before == after
