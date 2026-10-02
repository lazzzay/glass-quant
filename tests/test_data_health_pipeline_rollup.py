#!/usr/bin/env python3
"""Tests fuer PIPELINE-NOT-RUN-Rollup (V7.19).

Beweist, dass data_health.py mehrere GLEICHZEITIG fehlende aktive Producer zu
EINEM Sammel-Finding PIPELINE_NOT_RUN (H) buendelt, statt N separate SEV_H-
Tickets fuer EINE Wurzel (Pipeline lief nicht) zu erzeugen.

Invarianten:
  * Genau 1 H nach Rollup → overall_status bleibt CRITICAL → NO-TRADE erhalten.
  * Fehlt nur EIN Producer (z. B. earnings) → KEIN Rollup, spezifisches
    Einzel-Finding bleibt sichtbar (genuines Signal).
  * Disabled-Layer-Findings (SEV_L) zaehlen nie mit; PENDING/ENV_UNREADABLE
    (SEV_L) zaehlen nie mit.
  * Rollup-Finding traegt missing/present/rolled_up_codes im detail.

Laeuft offline, read-only, deterministisch.

    python3 tests/test_data_health_pipeline_rollup.py   # oder: pytest …
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_dh():
    spec = importlib.util.spec_from_file_location(
        "data_health_rollup_under_test", REPO / "tools" / "data_health.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dh = _load_dh()
TODAY = dt.date.today().isoformat()


def _missing_h(source: str) -> dict:
    code = "SCAN_MISSING" if source == "scan" else f"{source.upper()}_JSON_MISSING"
    return dh.make_finding(dh.SEV_H, source, code, f"Kein {source}_{TODAY}.json gefunden.")


# ---------------------------------------------------------------------------
# (1) _is_missing_active_producer_finding: Klassifikation
# ---------------------------------------------------------------------------
class TestMissingClassifier(unittest.TestCase):
    def test_scan_missing_h_counts(self):
        self.assertTrue(dh._is_missing_active_producer_finding(_missing_h("scan")))

    def test_premarket_missing_h_counts(self):
        self.assertTrue(dh._is_missing_active_producer_finding(_missing_h("premarket")))

    def test_sev_l_does_not_count(self):
        f = dh.make_finding(dh.SEV_L, "scan", "SCAN_AWAITING_PRODUCER", "pending")
        self.assertFalse(dh._is_missing_active_producer_finding(f))

    def test_non_active_source_does_not_count(self):
        f = dh.make_finding(dh.SEV_H, "options_flow", "OPTIONS_FLOW_JSON_MISSING", "x")
        self.assertFalse(dh._is_missing_active_producer_finding(f))

    def test_non_missing_code_does_not_count(self):
        f = dh.make_finding(dh.SEV_H, "scan", "SCAN_PARSE_ERROR", "corrupt")
        self.assertFalse(dh._is_missing_active_producer_finding(f))


# ---------------------------------------------------------------------------
# (2) apply_pipeline_rollup: Buendelung
# ---------------------------------------------------------------------------
class TestRollup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_three_missing_roll_into_one(self):
        # heute live: scan/premarket/earnings fehlen, sector_rs/regime da
        (self.td / f"sector_rs_{TODAY}.json").write_text("{}", encoding="utf-8")
        (self.td / f"regime_{TODAY}.json").write_text("{}", encoding="utf-8")
        findings = [_missing_h("scan"), _missing_h("premarket"), _missing_h("earnings")]
        tel = dh.apply_pipeline_rollup(findings, TODAY, tools_dir=self.td)
        h = [f for f in findings if f["severity"] == dh.SEV_H]
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["code"], "PIPELINE_NOT_RUN")
        self.assertEqual(tel["rolled_up"], 3)
        self.assertEqual(set(h[0]["detail"]["missing"]), {"scan", "premarket", "earnings"})
        self.assertEqual(set(h[0]["detail"]["present"]), {"sector_rs", "regime"})
        self.assertEqual(h[0]["detail"]["rolled_up_count"], 3)

    def test_two_missing_triggers_rollup(self):
        findings = [_missing_h("scan"), _missing_h("premarket")]
        tel = dh.apply_pipeline_rollup(findings, TODAY, tools_dir=self.td)
        self.assertEqual(tel["rolled_up"], 2)
        self.assertEqual([f["code"] for f in findings], ["PIPELINE_NOT_RUN"])

    def test_single_missing_no_rollup(self):
        # nur earnings fehlt → KEIN Rollup, spezifisches Signal bleibt
        findings = [_missing_h("earnings")]
        tel = dh.apply_pipeline_rollup(findings, TODAY, tools_dir=self.td)
        self.assertEqual(tel["rolled_up"], 0)
        self.assertEqual([f["code"] for f in findings], ["EARNINGS_JSON_MISSING"])

    def test_disabled_layer_l_findings_never_counted(self):
        # 1 echtes Missing (scan) + 2 disabled-layer L → < min_missing → kein Rollup
        l1 = dh.make_finding(dh.SEV_L, "options_flow", "OPTIONS_FLOW_JSON_MISSING", "disabled")
        l2 = dh.make_finding(dh.SEV_L, "insider", "INSIDER_JSON_MISSING", "disabled")
        findings = [_missing_h("scan"), l1, l2]
        tel = dh.apply_pipeline_rollup(findings, TODAY, tools_dir=self.td)
        self.assertEqual(tel["rolled_up"], 0)
        self.assertIn("SCAN_MISSING", [f["code"] for f in findings])

    def test_rollup_keeps_unrelated_findings(self):
        # ein unrelated M-Finding (z. B. SECTOR_RS_NAN) bleibt erhalten
        m = dh.make_finding(dh.SEV_M, "sector_rs", "SECTOR_RS_NAN", "all nan")
        findings = [_missing_h("scan"), _missing_h("premarket"), m]
        dh.apply_pipeline_rollup(findings, TODAY, tools_dir=self.td)
        codes = [f["code"] for f in findings]
        self.assertIn("PIPELINE_NOT_RUN", codes)
        self.assertIn("SECTOR_RS_NAN", codes)
        self.assertNotIn("SCAN_MISSING", codes)


# ---------------------------------------------------------------------------
# (3) launchd-Kontext-Helper
# ---------------------------------------------------------------------------
class TestLaunchdContext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = pathlib.Path(self._tmp.name)
        (self.td / "_logs").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_log_returns_none(self):
        self.assertIsNone(dh._launchd_morning_ran(TODAY, tools_dir=self.td))

    def test_log_today_returns_true(self):
        log = self.td / "_logs" / "launchd_morning.log"
        log.write_text("ran", encoding="utf-8")  # frisch → mtime heute
        self.assertTrue(dh._launchd_morning_ran(TODAY, tools_dir=self.td))

    def test_log_today_false_for_other_date(self):
        log = self.td / "_logs" / "launchd_morning.log"
        log.write_text("ran", encoding="utf-8")
        self.assertFalse(dh._launchd_morning_ran("2099-01-02", tools_dir=self.td))


# ---------------------------------------------------------------------------
# (4) build_report End-to-End (TOOLS_DIR gemockt): Invarianten
# ---------------------------------------------------------------------------
class TestBuildReportRollup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dh.TOOLS_DIR
        dh.TOOLS_DIR = pathlib.Path(self._tmp.name)
        self.POST = dt.datetime.combine(dt.date.today(), dt.time(11, 0))

    def tearDown(self):
        dh.TOOLS_DIR = self._orig
        self._tmp.cleanup()

    def test_partial_outage_after_cutoff_one_h(self):
        # sector_rs + regime da (degradiert ok), scan/premarket/earnings fehlen
        (dh.TOOLS_DIR / f"sector_rs_{TODAY}.json").write_text(
            '{"SPY":{"rs_1m":0.1}}', encoding="utf-8")
        (dh.TOOLS_DIR / f"regime_{TODAY}.json").write_text(
            '{"regime":"neutral"}', encoding="utf-8")
        rep = dh.build_report(TODAY, now=self.POST)
        self.assertEqual(rep["overall_status"], "CRITICAL")  # NO-TRADE erhalten
        self.assertEqual(rep["summary"]["findings_h"], 1)    # gebuendelt
        codes = [f["code"] for f in rep["findings"] if f["severity"] == dh.SEV_H]
        self.assertEqual(codes, ["PIPELINE_NOT_RUN"])
        self.assertGreaterEqual(rep["pipeline_rollup"]["rolled_up"], 2)

    def test_full_outage_after_cutoff_one_h(self):
        # alle aktiven Producer fehlen (Mac-Sleep nach Cutoff) → 1 Sammel-H
        rep = dh.build_report(TODAY, now=self.POST)
        self.assertEqual(rep["overall_status"], "CRITICAL")
        self.assertEqual(rep["summary"]["findings_h"], 1)
        h = [f for f in rep["findings"] if f["severity"] == dh.SEV_H]
        self.assertEqual(h[0]["code"], "PIPELINE_NOT_RUN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
