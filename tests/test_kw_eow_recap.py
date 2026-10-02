"""Pytest fuer tools/kw_eow_recap.py (V7.9.37, 2026-05-28).

Spiegelt die wichtigsten Selftest-Cases als pytest-Variante.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kw_eow_recap as kwer  # noqa: E402


def _write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _wd(monday: date):
    return [monday + timedelta(days=i) for i in range(5)]


def test_T01_workdays_in_range_5():
    wd = kwer.workdays_in_range(date(2026, 5, 25), date(2026, 5, 29))
    assert len(wd) == 5


def test_T02_iso_week_workdays_kw22_monday():
    wd = kwer.iso_week_workdays(2026, 22)
    assert wd[0] == date(2026, 5, 25)
    assert wd[-1] == date(2026, 5, 29)


def test_T03_kpi_recs_green(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"top_setups": [{"pre_score": 7.5, "passes_threshold": True},
                                       {"pre_score": 7.1, "passes_threshold": True}]})
    k = kwer.kpi_recs(wd, tmp_path)
    assert k["total_recs"] == 10
    assert k["passing"] is True


def test_T04_kpi_recs_fail(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"top_setups": [{"pre_score": 5.0, "passes_threshold": False}]})
    k = kwer.kpi_recs(wd, tmp_path)
    assert k["total_recs"] == 0
    assert k["passing"] is False


def test_T05_kpi_pcr_green(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"options_flow_{d.isoformat()}.json",
                    {"market_pcr": {"ok": True,
                                     "source": kwer.REQ_PCR_SOURCE,
                                     "tag": "neutral"}})
    k = kwer.kpi_pcr_resilience(wd, tmp_path)
    assert k["pct"] == 1.0
    assert k["passing"] is True


def test_T06_kpi_pcr_fail_wrong_source(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"options_flow_{d.isoformat()}.json",
                    {"market_pcr": {"ok": True, "source": "yfinance",
                                     "tag": "unknown"}})
    k = kwer.kpi_pcr_resilience(wd, tmp_path)
    assert k["pct"] == 0.0
    assert k["passing"] is False


def test_T07_kpi_gdelt_green(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"news_sentiment_{d.isoformat()}.json",
                    {"status": "ok", "n_live_ok": 250})
    k = kwer.kpi_gdelt(wd, tmp_path)
    assert k["pct"] == 1.0
    assert k["passing"] is True


def test_T08_kpi_gdelt_fail_sandbox(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"news_sentiment_{d.isoformat()}.json",
                    {"status": "fail_soft_sandbox_egress", "n_live_ok": 0})
    k = kwer.kpi_gdelt(wd, tmp_path)
    assert k["pct"] == 0.0
    assert k["passing"] is False


def test_T09_kpi_scan_freshness_green(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"scan_date": d.isoformat(),
                     "top_setups": [{"pre_score": 7.5, "passes_threshold": True}]})
    k = kwer.kpi_scan_freshness(wd, tmp_path)
    assert k["pct"] is not None
    assert k["pct"] >= 0.8
    assert k["passing"] is True


def test_T10_kpi_scan_freshness_fail_stale(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        old_iso = (d - timedelta(days=10)).isoformat()
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"scan_date": old_iso,
                     "_fallback_from": f"scan_{old_iso}.json",
                     "_fallback_age_werktage": 8,
                     "top_setups": []})
    k = kwer.kpi_scan_freshness(wd, tmp_path)
    assert k["passing"] is False
    assert k["pct"] is None or k["pct"] < 0.8


def test_T11_build_recap_green_end_to_end(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"scan_date": d.isoformat(),
                     "top_setups": [{"pre_score": 7.5, "passes_threshold": True},
                                       {"pre_score": 7.1, "passes_threshold": True}]})
        _write_json(tmp_path / f"options_flow_{d.isoformat()}.json",
                    {"market_pcr": {"ok": True, "source": kwer.REQ_PCR_SOURCE,
                                     "tag": "neutral"}})
        _write_json(tmp_path / f"news_sentiment_{d.isoformat()}.json",
                    {"status": "ok", "n_live_ok": 250})
    recap = kwer.build_recap(date(2026, 5, 25), date(2026, 5, 29),
                              tools_dir=tmp_path)
    assert recap["severity"] == "GREEN"
    assert recap["n_kpis_failing"] == 0
    assert recap["verdict"] == "week_healthy"


def test_T12_build_recap_black_systemic_failure(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        old_iso = (d - timedelta(days=10)).isoformat()
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"scan_date": old_iso,
                     "_fallback_from": f"scan_{old_iso}.json",
                     "_fallback_age_werktage": 8,
                     "top_setups": []})
        _write_json(tmp_path / f"options_flow_{d.isoformat()}.json",
                    {"market_pcr": {"ok": False, "source": "yfinance",
                                     "tag": "unknown"}})
        _write_json(tmp_path / f"news_sentiment_{d.isoformat()}.json",
                    {"status": "fail_soft_sandbox_egress", "n_live_ok": 0})
    recap = kwer.build_recap(date(2026, 5, 25), date(2026, 5, 29),
                              tools_dir=tmp_path)
    assert recap["severity"] == "BLACK"
    assert recap["n_kpis_failing"] >= 3


def test_T13_render_md_contains_header(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"scan_date": d.isoformat(),
                     "top_setups": [{"pre_score": 7.5, "passes_threshold": True}]})
    recap = kwer.build_recap(date(2026, 5, 25), date(2026, 5, 29),
                              tools_dir=tmp_path)
    md = kwer.render_md(recap)
    assert "KW-EOW-Recap" in md
    assert "KPIs" in md


def test_T14_sprint_plan_emits_priorities(tmp_path):
    wd = _wd(date(2026, 5, 25))
    for d in wd:
        _write_json(tmp_path / f"scan_{d.isoformat()}.json",
                    {"top_setups": []})
    recap = kwer.build_recap(date(2026, 5, 25), date(2026, 5, 29),
                              tools_dir=tmp_path)
    assert len(recap["sprint_plan"]["priorities"]) >= 1


def test_T15_pcr_partial_pct(tmp_path):
    """3/5 Werktage ok = pct=0.6 (auf der Schwelle)."""
    wd = _wd(date(2026, 5, 25))
    for i, d in enumerate(wd):
        if i < 3:
            _write_json(tmp_path / f"options_flow_{d.isoformat()}.json",
                        {"market_pcr": {"ok": True,
                                         "source": kwer.REQ_PCR_SOURCE,
                                         "tag": "neutral"}})
        else:
            _write_json(tmp_path / f"options_flow_{d.isoformat()}.json",
                        {"market_pcr": {"ok": False, "source": "yfinance",
                                         "tag": "unknown"}})
    k = kwer.kpi_pcr_resilience(wd, tmp_path)
    assert k["pct"] == 0.6
    assert k["passing"] is True


def test_T16_workdays_skip_weekend():
    """KW22 startet Mo 25.05., letzter WT Fr 29.05.; Sa/So nicht enthalten."""
    wd = kwer.workdays_in_range(date(2026, 5, 25), date(2026, 5, 31))
    assert all(d.weekday() < 5 for d in wd)
    assert len(wd) == 5
