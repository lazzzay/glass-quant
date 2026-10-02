#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests fuer V7.9.25 DIAGNOSTIC-ROLLUP-1 (tools/diagnostic_rollup.py).

Mode: FEATURE-Test (V7.9.25). Read-only Tool, daher reine Library/IO-Tests.

Test-IDs:
  T01 _norm_severity normalisiert korrekt
  T02 _extract_trade_funnel liefert plausibles Mapping
  T03 _extract_layer_heal nimmt worst-Layer als primary_target
  T04 _extract_of_data_quality erkennt cboe_pcr als primary bei BLACK
  T05 _extract_oi_heal liefert primary=yfinance_oi bei RED/BLACK
  T06 _extract_threshold_passer erkennt upstream_score_layer aus notes
  T07 _extract_score_component nutzt primary_dead_layer
  T08 _extract_fix_session waehlt worst FIX als primary
  T09 find_latest_single_day_file findet exakt das Zieldatum
  T10 find_latest_single_day_file faellt auf juengste vorherige zurueck
  T11 find_latest_single_day_file Range-File wird ignoriert
  T12 build_source_row liefert UNAVAILABLE wenn nichts da
  T13 build_source_row laedt vorhandene Datei korrekt
  T14 build_source_row parse_error → unavailable_reason=parse_error
  T15 aggregate_consensus zaehlt severity_histogram
  T16 aggregate_consensus worst_severity = max(rank)
  T17 aggregate_consensus primary_target_vote gewichtet BLACK=2 RED=1
  T18 aggregate_consensus INSUFFICIENT_DATA bei n<min_tools
  T19 aggregate_consensus consensus_status = worst wenn enough tools
  T20 _build_recommendation INSUFFICIENT_DATA-Branch
  T21 _build_recommendation GREEN-Branch
  T22 _build_recommendation BLACK + options_flow_adjust → CBOE-Lever
  T23 _build_recommendation BLACK + upstream_score_layer → Score-Layer-Lever
  T24 _strict_exit_code Mapping korrekt
  T25 safe_load_json: transient OSError fuehrt zu retry
  T26 safe_load_json: file_missing sofort propagiert
  T27 safe_load_json: parse_error sofort propagiert
  T28 run_rollup end-to-end mit Sample-Fixtures
  T29 render_markdown enthaelt headline-Felder
  T30 main CLI strict exit-code GREEN/YELLOW=0
  T31 main CLI strict BLACK=2
  T32 main CLI strict INSUFFICIENT_DATA=3
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import diagnostic_rollup as dr  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (Realistische Auszuege aus echten 2026-05-x Reports)
# ---------------------------------------------------------------------------


def fx_trade_funnel(severity="BLACK", bottleneck="score_pass", top_score=6.45):
    return {
        "generated_utc": "2026-04-29T12:09:49+00:00",
        "report": {
            "severity": severity,
            "bottleneck_stage": bottleneck,
            "top_score": top_score,
            "top_score_gap_to_threshold": 1.05,
            "layers_dead": ["insider", "options_flow", "market_pcr"],
            "severity_reasons": ["top_score_gap=1.05"],
        },
        "version": "1.0",
    }


def fx_layer_heal():
    return {
        "version": "1.0",
        "mode": "single_day",
        "date": "2026-04-30",
        "scorecard": {
            "overall_severity": "BLACK",
            "layer_stats": [
                {"layer": "insider", "severity": "BLACK"},
                {"layer": "options_flow", "severity": "BLACK"},
                {"layer": "sector_rs", "severity": "GREEN"},
            ],
            "note": None,
        },
    }


def fx_of_data_quality(severity="YELLOW", silent=False):
    return {
        "date": "2026-05-01",
        "severity": severity,
        "silent_decommission": silent,
        "cboe_pcr_status": "skipped (chronic 403; backup-cron mode)",
        "yf_status": "ok (40/40, batch-merged)",
        "oi_coverage_pct": 50.0,
        "notes": ["cboe_pcr_unavailable"],
    }


def fx_oi_heal(severity="GREEN"):
    return {
        "date": "2026-05-06",
        "severity": severity,
        "heal_success_pct": 100.0,
        "n_candidates": 5,
        "market_pcr_source": "yfinance_fallback",
        "notes": ["heal_loop_recovers_yfinance_oi"],
    }


def fx_threshold_passer(severity="RED"):
    return {
        "date": "2026-05-06",
        "severity": severity,
        "pass_rate_pct": 26.67,
        "of_coverage_pct": 87.5,
        "notes": ["very_low_passer_count_upstream_priority"],
    }


def fx_score_component(severity="BLACK", primary="options_flow_adjust"):
    return {
        "date": "2026-05-07",
        "overall_severity": severity,
        "primary_dead_layer": primary,
        "components": [
            {"field": "connors_quality_scaled", "severity": "GREEN"},
            {"field": "options_flow_adjust", "severity": "BLACK"},
            {"field": "market_pcr_adjust", "severity": "BLACK"},
        ],
    }


def fx_fix_session(overall="BLACK"):
    return {
        "date": "2026-05-11",
        "overall_severity": overall,
        "fixes": [
            {"fix": "FIX-A", "severity": "GREEN"},
            {"fix": "FIX-B", "severity": "BLACK"},
            {"fix": "FIX-C", "severity": "RED"},
            {"fix": "FIX-D", "severity": "BLACK"},
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractors(unittest.TestCase):
    def test_T01_norm_severity(self):
        self.assertEqual(dr._norm_severity("green"), "GREEN")
        self.assertEqual(dr._norm_severity("BLACK"), "BLACK")
        self.assertEqual(dr._norm_severity("rot"), "UNAVAILABLE")
        self.assertEqual(dr._norm_severity(None), "UNAVAILABLE")

    def test_T02_trade_funnel(self):
        out = dr._extract_trade_funnel(fx_trade_funnel())
        self.assertEqual(out["severity"], "BLACK")
        self.assertEqual(out["primary_target"], "score_pass")
        self.assertIn("bottleneck", out["summary"])
        self.assertIn("top_score", out["summary"])

    def test_T03_layer_heal_worst(self):
        out = dr._extract_layer_heal(fx_layer_heal())
        self.assertEqual(out["severity"], "BLACK")
        # Erster BLACK-Layer = insider (Reihenfolge erhaelt)
        self.assertIn(out["primary_target"], {"insider", "options_flow"})

    def test_T04_of_dq_cboe_primary_on_red(self):
        out = dr._extract_of_data_quality(
            fx_of_data_quality(severity="BLACK", silent=True)
        )
        self.assertEqual(out["primary_target"], "cboe_pcr")
        out2 = dr._extract_of_data_quality(
            fx_of_data_quality(severity="GREEN", silent=False)
        )
        self.assertIsNone(out2["primary_target"])

    def test_T05_oi_heal_primary_on_red(self):
        out = dr._extract_oi_heal(fx_oi_heal(severity="BLACK"))
        self.assertEqual(out["primary_target"], "yfinance_oi")
        out2 = dr._extract_oi_heal(fx_oi_heal(severity="GREEN"))
        self.assertIsNone(out2["primary_target"])

    def test_T06_threshold_passer_upstream(self):
        out = dr._extract_threshold_passer(fx_threshold_passer())
        self.assertEqual(out["primary_target"], "upstream_score_layer")

    def test_T07_score_component_primary(self):
        out = dr._extract_score_component(fx_score_component())
        self.assertEqual(out["severity"], "BLACK")
        self.assertEqual(out["primary_target"], "options_flow_adjust")

    def test_T08_fix_session_worst(self):
        out = dr._extract_fix_session(fx_fix_session())
        self.assertEqual(out["severity"], "BLACK")
        # FIX-B oder FIX-D, beide BLACK → erster gewinnt nicht zwingend; primary != GREEN
        self.assertIn(out["primary_target"], {"FIX-B", "FIX-D"})


class TestFileResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name, payload=None):
        p = self.tdir / name
        with p.open("w", encoding="utf-8") as fh:
            json.dump(payload or {"k": "v"}, fh)
        return p

    def test_T09_find_exact_date(self):
        self._touch("trade_funnel_2026-05-12.json")
        out = dr.find_latest_single_day_file(
            "trade_funnel_", "2026-05-12", 7, self.tdir
        )
        self.assertEqual(out.name, "trade_funnel_2026-05-12.json")

    def test_T10_fallback_to_earlier(self):
        self._touch("trade_funnel_2026-05-08.json")
        out = dr.find_latest_single_day_file(
            "trade_funnel_", "2026-05-12", 7, self.tdir
        )
        self.assertEqual(out.name, "trade_funnel_2026-05-08.json")

    def test_T11_range_files_excluded(self):
        self._touch("trade_funnel_range_2026-05-01_2026-05-08.json")
        # Lookback 30d, aber range muss ignoriert werden
        out = dr.find_latest_single_day_file(
            "trade_funnel_", "2026-05-12", 30, self.tdir
        )
        self.assertIsNone(out)

    def test_T12_build_row_unavailable(self):
        row = dr.build_source_row(
            "trade_funnel", "trade_funnel_", dr._extract_trade_funnel,
            "V7.9.15", "2026-05-12", 7, self.tdir
        )
        self.assertFalse(row["available"])
        self.assertEqual(row["severity"], "UNAVAILABLE")
        self.assertEqual(row["unavailable_reason"], "no_file_within_7d")

    def test_T13_build_row_available(self):
        self._touch(
            "trade_funnel_2026-05-11.json",
            payload=fx_trade_funnel(severity="RED"),
        )
        row = dr.build_source_row(
            "trade_funnel", "trade_funnel_", dr._extract_trade_funnel,
            "V7.9.15", "2026-05-12", 7, self.tdir
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["severity"], "RED")
        self.assertEqual(row["latest_date"], "2026-05-11")
        self.assertEqual(row["age_days"], 1)

    def test_T14_build_row_parse_error(self):
        bad = self.tdir / "trade_funnel_2026-05-12.json"
        with bad.open("w", encoding="utf-8") as fh:
            fh.write("{not-valid-json")
        row = dr.build_source_row(
            "trade_funnel", "trade_funnel_", dr._extract_trade_funnel,
            "V7.9.15", "2026-05-12", 7, self.tdir
        )
        self.assertFalse(row["available"])
        self.assertEqual(row["unavailable_reason"], "parse_error")


class TestAggregate(unittest.TestCase):
    def _row(self, sev, primary=None, available=True):
        return {
            "tool": "t",
            "available": available,
            "severity": sev if available else "UNAVAILABLE",
            "primary_target": primary,
        }

    def test_T15_histogram_counts(self):
        rows = [
            self._row("GREEN", "x"),
            self._row("YELLOW", "y"),
            self._row("RED", "z"),
            self._row("BLACK", "z"),
            self._row("BLACK", None, available=False),
        ]
        out = dr.aggregate_consensus(rows, min_tools=1)
        h = out["severity_histogram"]
        self.assertEqual(h["GREEN"], 1)
        self.assertEqual(h["YELLOW"], 1)
        self.assertEqual(h["RED"], 1)
        self.assertEqual(h["BLACK"], 1)
        self.assertEqual(h["UNAVAILABLE"], 1)

    def test_T16_worst_severity(self):
        rows = [self._row("GREEN"), self._row("YELLOW"), self._row("RED")]
        out = dr.aggregate_consensus(rows, min_tools=1)
        self.assertEqual(out["worst_severity"], "RED")
        rows2 = [self._row("GREEN"), self._row("BLACK")]
        self.assertEqual(
            dr.aggregate_consensus(rows2, min_tools=1)["worst_severity"], "BLACK"
        )

    def test_T17_vote_weighting(self):
        rows = [
            self._row("BLACK", "options_flow_adjust"),
            self._row("BLACK", "options_flow_adjust"),
            self._row("RED", "insider"),
            self._row("GREEN", "ignored"),  # GREEN traegt 0
        ]
        out = dr.aggregate_consensus(rows, min_tools=1)
        v = out["primary_target_vote"]
        self.assertEqual(v["options_flow_adjust"], 4)
        self.assertEqual(v["insider"], 1)
        self.assertEqual(out["primary_target_consensus"], "options_flow_adjust")

    def test_T18_insufficient_data(self):
        rows = [self._row("BLACK", "x")] + [
            self._row("BLACK", None, available=False) for _ in range(5)
        ]
        out = dr.aggregate_consensus(rows, min_tools=3)
        self.assertEqual(out["consensus_status"], "INSUFFICIENT_DATA")
        self.assertTrue(out["insufficient_data"])

    def test_T19_consensus_status_worst(self):
        rows = [
            self._row("GREEN"),
            self._row("YELLOW"),
            self._row("RED"),
            self._row("BLACK"),
        ]
        out = dr.aggregate_consensus(rows, min_tools=2)
        self.assertEqual(out["consensus_status"], "BLACK")


class TestRecommendation(unittest.TestCase):
    def test_T20_insufficient(self):
        rec = dr._build_recommendation("INSUFFICIENT_DATA", [], None, {})
        self.assertIn("Zu wenige", rec["headline"])

    def test_T21_green(self):
        rec = dr._build_recommendation("GREEN", [], None, {})
        self.assertIn("GREEN", rec["headline"])
        self.assertEqual(rec["levers"], [])

    def test_T22_black_options_flow_lever(self):
        rec = dr._build_recommendation(
            "BLACK",
            [{"tool": "x", "severity": "BLACK"}],
            "options_flow_adjust",
            {"options_flow_adjust": 4},
        )
        self.assertTrue(any("CBOE" in lv for lv in rec["levers"]))

    def test_T23_upstream_score_lever(self):
        rec = dr._build_recommendation(
            "BLACK",
            [{"tool": "x", "severity": "BLACK"}],
            "upstream_score_layer",
            {"upstream_score_layer": 2},
        )
        self.assertTrue(any("Score-Layer" in lv for lv in rec["levers"]))
        # Lever-Text muss explizit darauf hinweisen, dass V7.9.16
        # gezeigt hat: Threshold-Senken hilft NICHT.
        joined = " ".join(rec["levers"])
        self.assertIn("V7.9.16", joined)
        self.assertIn("NICHT Threshold senken", joined)


class TestStrictExit(unittest.TestCase):
    def test_T24_exit_codes(self):
        self.assertEqual(dr._strict_exit_code("GREEN"), 0)
        self.assertEqual(dr._strict_exit_code("YELLOW"), 0)
        self.assertEqual(dr._strict_exit_code("RED"), 1)
        self.assertEqual(dr._strict_exit_code("BLACK"), 2)
        self.assertEqual(dr._strict_exit_code("INSUFFICIENT_DATA"), 3)


class TestSafeIO(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_T25_transient_retry(self):
        p = self.tdir / "f.json"
        with p.open("w") as fh:
            json.dump({"ok": 1}, fh)
        calls = {"n": 0}
        orig = Path.open

        def flaky_open(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] < 2:
                err = OSError(35, "Resource deadlock avoided")
                err.errno = 35
                raise err
            return orig(self, *a, **kw)

        with patch.object(Path, "open", flaky_open):
            data, info = dr.safe_load_json(p, retries=5, delay_s=0.01)
        self.assertEqual(data, {"ok": 1})
        self.assertGreaterEqual(info["attempts"], 2)

    def test_T26_file_missing(self):
        data, info = dr.safe_load_json(self.tdir / "nope.json")
        self.assertIsNone(data)
        self.assertTrue(info["file_missing"])

    def test_T27_parse_error(self):
        p = self.tdir / "f.json"
        with p.open("w") as fh:
            fh.write("{not-valid")
        data, info = dr.safe_load_json(p)
        self.assertIsNone(data)
        self.assertTrue(info["parse_error"])


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _w(self, name, payload):
        with (self.tdir / name).open("w") as fh:
            json.dump(payload, fh)

    def test_T28_run_rollup_end_to_end(self):
        # 5 von 7 Quellen vorhanden, 2 fehlen → enough
        self._w("trade_funnel_2026-05-12.json", fx_trade_funnel(severity="BLACK"))
        self._w("layer_heal_2026-05-11.json", fx_layer_heal())
        self._w("of_data_quality_2026-05-10.json", fx_of_data_quality(severity="YELLOW"))
        self._w("oi_heal_telemetry_2026-05-12.json", fx_oi_heal(severity="GREEN"))
        self._w("score_component_effectiveness_2026-05-12.json", fx_score_component())
        out = dr.run_rollup("2026-05-12", lookback_days=7, tools_dir=self.tdir)
        self.assertEqual(out["target_date"], "2026-05-12")
        self.assertEqual(out["synth"]["n_sources_available"], 5)
        self.assertEqual(out["synth"]["consensus_status"], "BLACK")
        self.assertIn(
            out["synth"]["primary_target_consensus"],
            {"options_flow_adjust", "insider", "options_flow", "score_pass"},
        )

    def test_T29_render_markdown_has_fields(self):
        self._w("trade_funnel_2026-05-12.json", fx_trade_funnel())
        self._w("layer_heal_2026-05-12.json", fx_layer_heal())
        self._w("of_data_quality_2026-05-12.json", fx_of_data_quality(severity="BLACK", silent=True))
        self._w("oi_heal_telemetry_2026-05-12.json", fx_oi_heal(severity="BLACK"))
        out = dr.run_rollup("2026-05-12", lookback_days=7, tools_dir=self.tdir, min_tools=3)
        md = dr.render_markdown(out)
        self.assertIn("Diagnostic-Rollup", md)
        self.assertIn("Konsens", md)
        self.assertIn("Sprint-Recommendation", md)
        self.assertIn("Quellen-Detail", md)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tdir = Path(self.tmp.name)
        self.out = self.tdir / "out"
        self.brief = self.tdir / "brief"
        self.out.mkdir()
        self.brief.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _w(self, name, payload):
        with (self.tdir / name).open("w") as fh:
            json.dump(payload, fh)

    def test_T30_strict_green_zero(self):
        # >=4 Quellen alle GREEN → exit 0
        self._w("trade_funnel_2026-05-12.json", fx_trade_funnel(severity="GREEN"))
        self._w("layer_heal_2026-05-12.json",
                {"scorecard": {"overall_severity": "GREEN", "layer_stats": []}})
        self._w("of_data_quality_2026-05-12.json", fx_of_data_quality(severity="GREEN"))
        self._w("oi_heal_telemetry_2026-05-12.json", fx_oi_heal(severity="GREEN"))
        code = dr.main([
            "--date", "2026-05-12",
            "--tools-dir", str(self.tdir),
            "--out-dir", str(self.out),
            "--briefings-dir", str(self.brief),
            "--strict",
            "--no-md",
            "--quiet",
        ])
        self.assertEqual(code, 0)

    def test_T31_strict_black_two(self):
        self._w("trade_funnel_2026-05-12.json", fx_trade_funnel(severity="BLACK"))
        self._w("layer_heal_2026-05-12.json", fx_layer_heal())
        self._w("of_data_quality_2026-05-12.json",
                fx_of_data_quality(severity="BLACK", silent=True))
        self._w("oi_heal_telemetry_2026-05-12.json", fx_oi_heal(severity="BLACK"))
        code = dr.main([
            "--date", "2026-05-12",
            "--tools-dir", str(self.tdir),
            "--out-dir", str(self.out),
            "--briefings-dir", str(self.brief),
            "--strict",
            "--quiet",
        ])
        self.assertEqual(code, 2)
        # Brief sollte existieren
        self.assertTrue((self.brief / "Diagnostic-Rollup-2026-05-12.md").exists())

    def test_T32_strict_insufficient_three(self):
        # nur 2 Quellen → INSUFFICIENT_DATA (min_tools=4 default)
        self._w("trade_funnel_2026-05-12.json", fx_trade_funnel())
        self._w("layer_heal_2026-05-12.json", fx_layer_heal())
        code = dr.main([
            "--date", "2026-05-12",
            "--tools-dir", str(self.tdir),
            "--out-dir", str(self.out),
            "--briefings-dir", str(self.brief),
            "--strict",
            "--no-md",
            "--quiet",
        ])
        self.assertEqual(code, 3)


# ===========================================================================
# V7.9.26 Range-Mode (T33..T50)
# ===========================================================================


class TestRangeHelpers(unittest.TestCase):
    def test_T33_trading_days_basic(self):
        # 2026-05-11 Mo → 2026-05-15 Fr = 5 Werktage
        days = dr.trading_days_between("2026-05-11", "2026-05-15")
        self.assertEqual(days, [
            "2026-05-11", "2026-05-12", "2026-05-13",
            "2026-05-14", "2026-05-15",
        ])

    def test_T34_trading_days_skip_weekend(self):
        # Fr 2026-05-08 → Mo 2026-05-11 = 2 Werktage (Sa/So raus)
        days = dr.trading_days_between("2026-05-08", "2026-05-11")
        self.assertEqual(days, ["2026-05-08", "2026-05-11"])

    def test_T35_trading_days_inverted_range(self):
        # end vor start → leer
        self.assertEqual(
            dr.trading_days_between("2026-05-13", "2026-05-11"), []
        )

    def test_T36_trading_days_bad_input(self):
        self.assertEqual(dr.trading_days_between("nope", "2026-05-13"), [])
        self.assertEqual(dr.trading_days_between("2026-05-13", "nope"), [])

    def test_T37_longest_consecutive(self):
        self.assertEqual(dr._longest_consecutive([]), 0)
        self.assertEqual(dr._longest_consecutive([False, False]), 0)
        self.assertEqual(
            dr._longest_consecutive([True, True, False, True, True, True]),
            3,
        )
        self.assertEqual(
            dr._longest_consecutive([True, False, True, False, True]), 1
        )


def _mk_day(
    date: str,
    status: str,
    primary: str = None,
    sources_severities: dict = None,
) -> dict:
    """Baut ein run_rollup-aehnliches Objekt fuer Tests."""
    sources_severities = sources_severities or {}
    sources = []
    for tool in [
        "trade_funnel", "layer_heal", "of_data_quality",
        "oi_heal_telemetry", "threshold_passer_coverage",
        "score_component_effectiveness", "fix_session_verify",
    ]:
        sev = sources_severities.get(tool, "UNAVAILABLE")
        sources.append({
            "tool": tool,
            "severity": sev,
            "available": sev != "UNAVAILABLE",
            "primary_target": None,
        })
    return {
        "target_date": date,
        "sources": sources,
        "synth": {
            "consensus_status": status,
            "primary_target_consensus": primary,
        },
    }


class TestAggregateRange(unittest.TestCase):
    def test_T38_aggregate_range_empty(self):
        out = dr.aggregate_range([])
        self.assertEqual(out["n_days"], 0)
        self.assertEqual(out["worst_severity_streak"], 0)
        self.assertEqual(out["worst_consensus_in_window"], "UNAVAILABLE")

    def test_T39_aggregate_range_three_black_streak(self):
        days = [
            _mk_day("2026-05-09", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-12", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-13", "BLACK", "options_flow_adjust"),
        ]
        out = dr.aggregate_range(days)
        self.assertEqual(out["worst_consensus_in_window"], "BLACK")
        self.assertEqual(out["worst_severity_streak"], 3)
        self.assertEqual(out["red_or_black_streak"], 3)
        self.assertEqual(out["dominant_primary_target"], "options_flow_adjust")
        self.assertEqual(out["vote_persistence_pct"], 100.0)

    def test_T40_aggregate_range_mixed(self):
        days = [
            _mk_day("2026-05-07", "RED", "upstream_score_layer"),
            _mk_day("2026-05-08", "GREEN"),
            _mk_day("2026-05-12", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-13", "RED", "options_flow_adjust"),
        ]
        out = dr.aggregate_range(days)
        self.assertEqual(out["worst_consensus_in_window"], "BLACK")
        self.assertEqual(out["worst_severity_streak"], 1)  # nur 1 BLACK-Tag
        self.assertEqual(out["red_or_black_streak"], 2)  # BLACK+RED am Ende
        self.assertEqual(out["dominant_primary_target"], "options_flow_adjust")

    def test_T41_aggregate_range_all_insufficient(self):
        days = [
            _mk_day("2026-05-12", "INSUFFICIENT_DATA"),
            _mk_day("2026-05-13", "INSUFFICIENT_DATA"),
        ]
        out = dr.aggregate_range(days)
        self.assertEqual(out["n_with_consensus"], 0)
        self.assertEqual(out["n_insufficient_data"], 2)
        self.assertEqual(out["worst_severity_streak"], 0)
        self.assertEqual(out["dominant_primary_target"], None)

    def test_T42_aggregate_range_per_tool_distribution(self):
        days = [
            _mk_day("2026-05-12", "BLACK", "options_flow_adjust",
                    sources_severities={"trade_funnel": "BLACK",
                                        "layer_heal": "GREEN"}),
            _mk_day("2026-05-13", "RED", "options_flow_adjust",
                    sources_severities={"trade_funnel": "RED",
                                        "layer_heal": "GREEN"}),
        ]
        out = dr.aggregate_range(days)
        per_tool_map = {p["tool"]: p for p in out["per_tool"]}
        self.assertEqual(
            per_tool_map["trade_funnel"]["severity_distribution"]["BLACK"], 1
        )
        self.assertEqual(
            per_tool_map["trade_funnel"]["severity_distribution"]["RED"], 1
        )
        self.assertEqual(per_tool_map["layer_heal"]["availability_pct"], 100.0)
        self.assertEqual(
            per_tool_map["layer_heal"]["longest_black_streak"], 0
        )

    def test_T43_vote_persistence_partial(self):
        # 2x options_flow_adjust + 1x insider_adjust + 1x INSUFFICIENT
        days = [
            _mk_day("2026-05-08", "INSUFFICIENT_DATA"),
            _mk_day("2026-05-11", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-12", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-13", "RED", "insider_adjust"),
        ]
        out = dr.aggregate_range(days)
        self.assertEqual(out["dominant_primary_target"], "options_flow_adjust")
        # 2/3 Konsens-Tage = 66.7%
        self.assertAlmostEqual(out["vote_persistence_pct"], 66.7, places=1)


class TestRangeRecommendation(unittest.TestCase):
    def test_T44_sprint_trigger_black_streak_3_and_consensus(self):
        days = [
            _mk_day("2026-05-11", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-12", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-13", "BLACK", "options_flow_adjust"),
        ]
        out = dr.aggregate_range(days)
        rec = out["range_recommendation"]
        self.assertEqual(rec["verdict"], "sprint_trigger")
        self.assertTrue(rec["sprint_trigger"])
        self.assertTrue(any("CBOE" in lv for lv in rec["levers"]))

    def test_T45_watchdog_elevated_red_streak(self):
        days = [
            _mk_day("2026-05-11", "RED", "upstream_score_layer"),
            _mk_day("2026-05-12", "RED", "upstream_score_layer"),
            _mk_day("2026-05-13", "RED", "upstream_score_layer"),
        ]
        out = dr.aggregate_range(days)
        rec = out["range_recommendation"]
        self.assertEqual(rec["verdict"], "watchdog_elevated")
        self.assertFalse(rec["sprint_trigger"])

    def test_T46_isolated_black(self):
        days = [
            _mk_day("2026-05-11", "YELLOW", None),
            _mk_day("2026-05-12", "BLACK", "options_flow_adjust"),
            _mk_day("2026-05-13", "GREEN", None),
        ]
        out = dr.aggregate_range(days)
        rec = out["range_recommendation"]
        self.assertEqual(rec["verdict"], "isolated_black")
        self.assertFalse(rec["sprint_trigger"])

    def test_T47_stable_green(self):
        days = [
            _mk_day("2026-05-11", "GREEN", None),
            _mk_day("2026-05-12", "GREEN", None),
            _mk_day("2026-05-13", "YELLOW", None),
        ]
        out = dr.aggregate_range(days)
        rec = out["range_recommendation"]
        self.assertEqual(rec["verdict"], "stable")
        self.assertFalse(rec["sprint_trigger"])

    def test_T48_deferred_all_insufficient(self):
        days = [
            _mk_day("2026-05-12", "INSUFFICIENT_DATA"),
            _mk_day("2026-05-13", "INSUFFICIENT_DATA"),
        ]
        out = dr.aggregate_range(days)
        rec = out["range_recommendation"]
        self.assertEqual(rec["verdict"], "deferred")
        self.assertFalse(rec["sprint_trigger"])


class TestRangeCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tdir = Path(self.tmp.name) / "tools"
        self.out = Path(self.tmp.name) / "out"
        self.brief = Path(self.tmp.name) / "brief"
        self.tdir.mkdir(); self.out.mkdir(); self.brief.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _w(self, name, payload):
        with (self.tdir / name).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_T49_range_cli_writes_files_strict_two(self):
        # Beide Tage genug Quellen mit BLACK → range worst=BLACK → exit=2
        for d in ("2026-05-12", "2026-05-13"):
            self._w(f"trade_funnel_{d}.json", fx_trade_funnel(severity="BLACK"))
            self._w(f"layer_heal_{d}.json", fx_layer_heal())
            self._w(f"of_data_quality_{d}.json",
                    fx_of_data_quality(severity="BLACK", silent=True))
            self._w(f"oi_heal_telemetry_{d}.json", fx_oi_heal(severity="BLACK"))
        code = dr.main([
            "--start-date", "2026-05-12",
            "--end-date", "2026-05-13",
            "--tools-dir", str(self.tdir),
            "--out-dir", str(self.out),
            "--briefings-dir", str(self.brief),
            "--strict",
            "--quiet",
        ])
        self.assertEqual(code, 2)
        self.assertTrue(
            (self.out / "diagnostic_rollup_range_2026-05-12_2026-05-13.json").exists()
        )
        self.assertTrue(
            (self.brief / "Diagnostic-Rollup-Range-2026-05-12_2026-05-13.md").exists()
        )

    def test_T50_range_cli_insufficient_exit_three(self):
        # Beide Tage zu wenige Quellen → alle INSUFFICIENT → exit=3
        for d in ("2026-05-12", "2026-05-13"):
            self._w(f"trade_funnel_{d}.json", fx_trade_funnel())
        code = dr.main([
            "--start-date", "2026-05-12",
            "--end-date", "2026-05-13",
            "--tools-dir", str(self.tdir),
            "--out-dir", str(self.out),
            "--briefings-dir", str(self.brief),
            "--strict",
            "--no-md",
            "--quiet",
        ])
        self.assertEqual(code, 3)

    def test_T51_range_md_contains_headline(self):
        # Mindestens 4 Quellen pro Tag damit Konsens da ist
        for d in ("2026-05-12", "2026-05-13"):
            self._w(f"trade_funnel_{d}.json", fx_trade_funnel(severity="BLACK"))
            self._w(f"layer_heal_{d}.json", fx_layer_heal())
            self._w(f"of_data_quality_{d}.json",
                    fx_of_data_quality(severity="BLACK", silent=True))
            self._w(f"oi_heal_telemetry_{d}.json", fx_oi_heal(severity="BLACK"))
        dr.main([
            "--start-date", "2026-05-12",
            "--end-date", "2026-05-13",
            "--tools-dir", str(self.tdir),
            "--out-dir", str(self.out),
            "--briefings-dir", str(self.brief),
            "--quiet",
        ])
        md_path = self.brief / "Diagnostic-Rollup-Range-2026-05-12_2026-05-13.md"
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("Range-Verdikt", content)
        self.assertIn("BLACK-Streak", content)
        self.assertIn("Per-Tool-Aggregate", content)


if __name__ == "__main__":
    unittest.main()
