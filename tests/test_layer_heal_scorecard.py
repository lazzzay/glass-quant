"""
Tests V7.9.17: Layer-Heal-Scorecard.

Read-only Tool, deterministische Tests ueber In-Memory-Scans.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import layer_heal_scorecard as lh  # type: ignore


def _mk_setup(ticker: str, score: float = 6.0, mtf_tag: str = "neutral|neutral",
              insider: float = 0.0, options_flow: float = 0.0,
              sector_rs: float = 0.0) -> dict:
    return {
        "ticker": ticker,
        "pre_score": score,
        "pre_score_raw": score,
        "mtf_tag": mtf_tag,
        "insider_score": insider,
        "options_flow_score": options_flow,
        "sector_rs_score": sector_rs,
    }


def _mk_scan(top: list, scan_date: str = "2026-04-30",
             universe_size: int = 610, valid_results: int = 600) -> dict:
    return {
        "scan_date": scan_date,
        "universe_size": universe_size,
        "valid_results": valid_results,
        "top_setups": top,
    }


class T01_ParseHelpers(unittest.TestCase):
    def test_parse_iso(self):
        self.assertEqual(lh._parse_iso("2026-04-30"), date(2026, 4, 30))

    def test_is_weekday(self):
        self.assertTrue(lh._is_weekday(date(2026, 4, 30)))
        self.assertFalse(lh._is_weekday(date(2026, 4, 25)))

    def test_expand_range_skips_weekend(self):
        days = lh._expand_range(date(2026, 4, 24), date(2026, 4, 28))
        self.assertEqual([d.isoformat() for d in days],
                         ["2026-04-24", "2026-04-27", "2026-04-28"])

    def test_safe_score_handles_none_and_garbage(self):
        self.assertEqual(lh._safe_score({}, "x"), 0.0)
        self.assertEqual(lh._safe_score({"x": None}, "x"), 0.0)
        self.assertEqual(lh._safe_score({"x": "abc"}, "x"), 0.0)
        self.assertEqual(lh._safe_score({"x": 1.5}, "x"), 1.5)

    def test_layer_active(self):
        s = _mk_setup("X", insider=0.0, options_flow=0.0, sector_rs=-1.5)
        self.assertFalse(lh._layer_active(s, "insider"))
        self.assertFalse(lh._layer_active(s, "options_flow"))
        self.assertTrue(lh._layer_active(s, "sector_rs"))

    def test_mtf_good(self):
        self.assertTrue(lh._mtf_good({"mtf_tag": "bullish|primed"}))
        self.assertTrue(lh._mtf_good({"mtf_tag": "neutral|primed"}))
        self.assertFalse(lh._mtf_good({"mtf_tag": "neutral|neutral"}))
        self.assertFalse(lh._mtf_good({"mtf_tag": "bearish|primed"}))


class T02_Severity(unittest.TestCase):
    def test_severity_for_coverage(self):
        self.assertEqual(lh.severity_for_coverage(0.95), "GREEN")
        self.assertEqual(lh.severity_for_coverage(0.80), "GREEN")
        self.assertEqual(lh.severity_for_coverage(0.50), "YELLOW")
        self.assertEqual(lh.severity_for_coverage(0.20), "RED")
        self.assertEqual(lh.severity_for_coverage(0.05), "BLACK")
        self.assertEqual(lh.severity_for_coverage(0.0), "BLACK")

    def test_severity_rank(self):
        self.assertLess(lh.severity_rank("GREEN"), lh.severity_rank("YELLOW"))
        self.assertLess(lh.severity_rank("YELLOW"), lh.severity_rank("RED"))
        self.assertLess(lh.severity_rank("RED"), lh.severity_rank("BLACK"))


class T03_DayScorecard_Empty(unittest.TestCase):
    def test_no_top_setups(self):
        day = lh.analyze_day_scan(_mk_scan([]))
        self.assertFalse(day.available)
        self.assertEqual(day.top_setups_count, 0)
        self.assertEqual(day.note, "no_top_setups")


class T04_DayScorecard_Counts(unittest.TestCase):
    def test_layer_distribution(self):
        # 3 Setups: einer mit allen 3 layers, einer mit 2, einer mit 0
        top = [
            _mk_setup("A", insider=1.0, options_flow=1.0, sector_rs=1.0),
            _mk_setup("B", insider=1.0, options_flow=1.0, sector_rs=0.0),
            _mk_setup("C", insider=0.0, options_flow=0.0, sector_rs=0.0),
        ]
        day = lh.analyze_day_scan(_mk_scan(top))
        self.assertEqual(day.layers_full, 1)
        self.assertEqual(day.layers_2, 1)
        self.assertEqual(day.layers_1, 0)
        self.assertEqual(day.layers_0, 1)

    def test_layer_coverage(self):
        # 4 setups: 2 mit insider, 1 mit options_flow, 4 mit sector_rs
        top = [
            _mk_setup("A", insider=1.0, sector_rs=1.0),
            _mk_setup("B", insider=1.0, sector_rs=1.0),
            _mk_setup("C", options_flow=1.0, sector_rs=1.0),
            _mk_setup("D", sector_rs=1.0),
        ]
        day = lh.analyze_day_scan(_mk_scan(top))
        ins = next(ls for ls in day.layer_stats if ls.layer == "insider")
        of = next(ls for ls in day.layer_stats if ls.layer == "options_flow")
        rs = next(ls for ls in day.layer_stats if ls.layer == "sector_rs")
        self.assertAlmostEqual(ins.coverage_pct, 0.5, places=4)
        self.assertAlmostEqual(of.coverage_pct, 0.25, places=4)
        self.assertAlmostEqual(rs.coverage_pct, 1.0, places=4)
        self.assertEqual(rs.severity, "GREEN")
        self.assertEqual(ins.severity, "YELLOW")
        self.assertEqual(of.severity, "RED")


class T05_DayScorecard_RealisticBlack(unittest.TestCase):
    def test_all_zero_layers(self):
        # 30 setups, alle 3 layers null -> overall BLACK
        top = [_mk_setup(f"T{i}", sector_rs=0.0) for i in range(30)]
        day = lh.analyze_day_scan(_mk_scan(top))
        self.assertEqual(day.overall_severity, "BLACK")
        self.assertEqual(day.layers_0, 30)
        self.assertEqual(day.layers_full, 0)


class T06_DayScorecard_MTF(unittest.TestCase):
    def test_mtf_good_count(self):
        top = [
            _mk_setup("A", mtf_tag="bullish|primed"),
            _mk_setup("B", mtf_tag="bullish|neutral"),
            _mk_setup("C", mtf_tag="neutral|primed"),
            _mk_setup("D", mtf_tag="neutral|neutral"),
            _mk_setup("E", mtf_tag="bearish|primed"),
        ]
        day = lh.analyze_day_scan(_mk_scan(top))
        self.assertEqual(day.mtf_good_count, 3)


class T07_UnblockPotential(unittest.TestCase):
    def test_strict_and_loose(self):
        # 5 setups, jeweils komplementaere layer-stati
        top = [
            # mtf_good + alle 3 layer aktiv -> kein unblock noetig
            _mk_setup("A", mtf_tag="bullish|primed",
                      insider=1.0, options_flow=1.0, sector_rs=1.0),
            # mtf_good, fehlt nur insider -> insider_strict +1, insider_loose +1
            _mk_setup("B", mtf_tag="bullish|primed",
                      insider=0.0, options_flow=1.0, sector_rs=1.0),
            # mtf_NOT_good, fehlt nur insider -> insider_loose +1 only
            _mk_setup("C", mtf_tag="neutral|neutral",
                      insider=0.0, options_flow=1.0, sector_rs=1.0),
            # fehlt nur options_flow, mtf good -> of_strict +1, of_loose +1
            _mk_setup("D", mtf_tag="bullish|neutral",
                      insider=1.0, options_flow=0.0, sector_rs=1.0),
            # fehlt 2 layer -> kein unblock fuer einzelnen layer
            _mk_setup("E", mtf_tag="bullish|primed",
                      insider=0.0, options_flow=0.0, sector_rs=1.0),
        ]
        scan = _mk_scan(top)
        up = lh.compute_unblock_potential(scan)
        self.assertEqual(up["insider_strict"], 1)
        self.assertEqual(up["insider_loose"], 2)
        self.assertEqual(up["options_flow_strict"], 1)
        self.assertEqual(up["options_flow_loose"], 1)
        self.assertEqual(up["sector_rs_strict"], 0)
        self.assertEqual(up["sector_rs_loose"], 0)


class T08_RangeAggregate(unittest.TestCase):
    def test_streak_max(self):
        self.assertEqual(lh._streak_max([True, True, False, True, True, True]), 3)
        self.assertEqual(lh._streak_max([False, False, False]), 0)
        self.assertEqual(lh._streak_max([]), 0)
        self.assertEqual(lh._streak_max([True]), 1)

    def test_aggregate_with_some_missing(self):
        # 3 days; 1 missing
        d1 = lh.DayScorecard(date="2026-04-27", available=True,
                             universe_size=610, valid_results=600,
                             top_setups_count=2,
                             layer_stats=[
                                 lh.LayerStat("insider", 0.0, 0, 2, "BLACK"),
                                 lh.LayerStat("options_flow", 0.5, 1, 2, "YELLOW"),
                                 lh.LayerStat("sector_rs", 1.0, 2, 2, "GREEN"),
                             ],
                             layers_full=0, layers_2=1, layers_1=1, layers_0=0,
                             mtf_good_count=1, overall_severity="BLACK")
        d2 = lh.DayScorecard(date="2026-04-28", available=False,
                             universe_size=0, valid_results=0,
                             top_setups_count=0, note="scan_not_found")
        d3 = lh.DayScorecard(date="2026-04-29", available=True,
                             universe_size=610, valid_results=600,
                             top_setups_count=2,
                             layer_stats=[
                                 lh.LayerStat("insider", 0.0, 0, 2, "BLACK"),
                                 lh.LayerStat("options_flow", 0.0, 0, 2, "BLACK"),
                                 lh.LayerStat("sector_rs", 1.0, 2, 2, "GREEN"),
                             ],
                             layers_full=0, layers_2=0, layers_1=2, layers_0=0,
                             mtf_good_count=0, overall_severity="BLACK")
        rg = lh.aggregate_range([d1, d2, d3], scans={})
        self.assertEqual(rg.days_total, 3)
        self.assertEqual(rg.days_with_data, 2)
        self.assertEqual(rg.days_missing, ["2026-04-28"])
        ins = next(ls for ls in rg.layer_summaries if ls.layer == "insider")
        rs = next(ls for ls in rg.layer_summaries if ls.layer == "sector_rs")
        self.assertEqual(ins.severity_aggregate, "BLACK")
        self.assertEqual(rs.severity_aggregate, "GREEN")
        self.assertGreaterEqual(ins.longest_black_streak, 2)


class T09_Recommendation(unittest.TestCase):
    def test_all_green_layers_full_zero(self):
        # Edge case: alle Layer GREEN, aber kein Setup mit allen 3 layers aktiv
        # (mathematisch unrealistisch, aber als Logik-Edge-Case relevant)
        layer_summaries = [
            lh.RangeLayerSummary("insider", 1.0, 1.0, 5, 0, 0, "GREEN", 0, 0),
            lh.RangeLayerSummary("options_flow", 1.0, 1.0, 5, 0, 0, "GREEN", 0, 0),
            lh.RangeLayerSummary("sector_rs", 1.0, 1.0, 5, 0, 0, "GREEN", 0, 0),
        ]
        rec = lh.build_recommendation(layer_summaries, median_layers_full=0.0)
        self.assertEqual(rec["primary_target"], "review_layer_combination")

    def test_black_wins_over_yellow(self):
        layer_summaries = [
            lh.RangeLayerSummary("insider", 0.0, 0.0, 0, 5, 5, "BLACK", 10, 25),
            lh.RangeLayerSummary("options_flow", 0.5, 0.5, 5, 0, 0, "YELLOW", 0, 5),
            lh.RangeLayerSummary("sector_rs", 0.95, 0.95, 5, 0, 0, "GREEN", 0, 0),
        ]
        rec = lh.build_recommendation(layer_summaries, median_layers_full=0.0)
        self.assertEqual(rec["primary_target"], "insider")
        self.assertEqual(rec["priority_order"][0], "insider")
        self.assertIn("BLACK", rec["rationale"])

    def test_no_data(self):
        rec = lh.build_recommendation([], median_layers_full=0.0)
        self.assertEqual(rec["primary_target"], None)
        self.assertEqual(rec["rationale"], "no_data")


class T10_CLI_SingleDay(unittest.TestCase):
    def test_single_day_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "Briefings").mkdir()
            scan = _mk_scan(
                [
                    _mk_setup("A", insider=1.0, options_flow=1.0, sector_rs=1.0,
                              mtf_tag="bullish|primed"),
                    _mk_setup("B", sector_rs=1.0),
                    _mk_setup("C", insider=0.0),
                ],
                scan_date="2026-04-30",
            )
            (root / "tools" / "scan_2026-04-30.json").write_text(json.dumps(scan))
            payload, day = lh.cli_single_day(date(2026, 4, 30), root, write_md=True)
            self.assertEqual(payload["mode"], "single_day")
            self.assertEqual(day.top_setups_count, 3)
            self.assertEqual(day.layers_full, 1)
            self.assertTrue((root / "tools" / "layer_heal_2026-04-30.json").exists())
            self.assertTrue((root / "Briefings" / "Layer-Heal-Scorecard-2026-04-30.md").exists())

    def test_single_day_missing_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "Briefings").mkdir()
            payload, day = lh.cli_single_day(date(2026, 4, 30), root, write_md=True)
            self.assertEqual(day.note, "scan_not_found")
            self.assertFalse(day.available)
            self.assertTrue((root / "tools" / "layer_heal_2026-04-30.json").exists())


class T11_CLI_Range(unittest.TestCase):
    def test_range_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "Briefings").mkdir()
            for d in ["2026-04-27", "2026-04-28", "2026-04-29"]:
                scan = _mk_scan(
                    [_mk_setup("A", sector_rs=1.0)],
                    scan_date=d,
                )
                (root / "tools" / f"scan_{d}.json").write_text(json.dumps(scan))
            payload, rg = lh.cli_range(date(2026, 4, 27), date(2026, 4, 29), root, write_md=True)
            self.assertEqual(payload["mode"], "range")
            self.assertEqual(rg.days_with_data, 3)
            self.assertEqual(rg.days_total, 3)
            out_json = root / "tools" / "layer_heal_range_2026-04-27_2026-04-29.json"
            out_md = root / "Briefings" / "Layer-Heal-Range-2026-04-27_2026-04-29.md"
            self.assertTrue(out_json.exists())
            self.assertTrue(out_md.exists())


class T12_CLI_StrictExitCode(unittest.TestCase):
    def test_strict_exit_2_on_black(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "Briefings").mkdir()
            scan = _mk_scan(
                [_mk_setup("A", sector_rs=1.0) for _ in range(10)],
                scan_date="2026-04-30",
            )
            (root / "tools" / "scan_2026-04-30.json").write_text(json.dumps(scan))
            rc = lh.main(["--date", "2026-04-30", "--root", str(root),
                          "--strict", "--no-md"])
            # insider und options_flow sind beide BLACK, sector_rs GREEN
            self.assertEqual(rc, 2)

    def test_default_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "Briefings").mkdir()
            scan = _mk_scan(
                [
                    _mk_setup(f"T{i}", insider=1.0, options_flow=1.0, sector_rs=1.0)
                    for i in range(10)
                ],
                scan_date="2026-04-30",
            )
            (root / "tools" / "scan_2026-04-30.json").write_text(json.dumps(scan))
            rc = lh.main(["--date", "2026-04-30", "--root", str(root), "--no-md"])
            self.assertEqual(rc, 0)


class T13_RenderingNoCrash(unittest.TestCase):
    def test_render_day_md_with_scan(self):
        scan = _mk_scan([
            _mk_setup("A", insider=1.0, options_flow=1.0, sector_rs=1.0,
                      mtf_tag="bullish|primed"),
            _mk_setup("B", sector_rs=1.0),
        ], scan_date="2026-04-30")
        day = lh.analyze_day_scan(scan)
        md = lh.render_day_md(day, scan)
        self.assertIn("Layer-Heal-Scorecard 2026-04-30", md)
        self.assertIn("Unblock-Potential", md)

    def test_render_day_md_without_scan(self):
        day = lh.DayScorecard(date="2026-04-30", available=False,
                              universe_size=0, valid_results=0,
                              top_setups_count=0, note="scan_not_found")
        md = lh.render_day_md(day, None)
        self.assertIn("Layer-Heal-Scorecard 2026-04-30", md)
        # Unblock-Potential-Block fehlt
        self.assertNotIn("Unblock-Potential", md)

    def test_render_range_md(self):
        d1 = lh.analyze_day_scan(_mk_scan(
            [_mk_setup("A", sector_rs=1.0)], scan_date="2026-04-29"))
        rg = lh.aggregate_range([d1], scans={"2026-04-29": _mk_scan(
            [_mk_setup("A", sector_rs=1.0)], scan_date="2026-04-29")})
        md = lh.render_range_md(rg)
        self.assertIn("Sprint-Prio", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
