#!/usr/bin/env python3
"""Tests fuer SCAN-WRITE-LAG-GRACE-1 (Fix DATA-HEALTH-PIPELINE-ROLLUP-TIMING-FP-1, V7.28).

Beweist, dass data_health.py die vierte Falsch-Positiv-Klasse (nach V7.16
Disabled-Layer, V7.17 Pre-Production-Timing, V7.24 premarket-Lagging) nicht mehr
als SEV_H eskaliert:

  Der data_health-Morgen-Snapshot laeuft als EIGENER launchd-Job (~06:40 CET) VOR
  der 08:00-Haupt-Pipeline. Frueh/separat geschriebene Producer
  (regime/sector_rs/earnings/insider) sind dann schon da, der HEAVY scan
  (daily_scanner.py, 639 Namen, Haupt-Pipeline-Step ~08:01 CET) aber noch NICHT.
  coordinated_absence ist damit False (Siblings da) und scan war bisher KEIN
  Lagging-Producer → SCAN_MISSING fiel faelschlich auf SEV_H → persistentes
  FP-Ticket (FP-Tage 2026-06-18/-19/-22; dieselbe Wurzel erzeugte am 2026-06-17
  ein FP PIPELINE_NOT_RUN aus scan+premarket).

Fix: scan ist jetzt Lagging-Producer mit producer-spezifischem (FRUEHEREM) Cutoff
(scan=GRACE_CUTOFF_HOUR=10 ~08:01 fertig; premarket=12 ~11:16 fertig). Fehlt scan
heute vor seinem Cutoff waehrend ein Sibling da ist → SEV_L AWAITING_PRODUCER
(PENDING), KEIN Ticket.

Invarianten (NICHT maskiert):
  * Nach dem scan-Cutoff (>=10) → echtes spaetes scan-Versagen bleibt SEV_H.
  * Komplett-Ausfall (kein Sibling) → koordinierte Logik (PENDING/PIPELINE_NOT_RUN).
  * Frueh-separate Producer (sector_rs/regime/earnings) → selektives Fehlen bleibt H.
  * premarket-Grace (V7.24, Cutoff 12) bleibt unveraendert.

Laeuft offline, read-only, deterministisch.

    python3 -m unittest tests.test_data_health_scan_write_lag   # oder pytest
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
        "data_health_scanlag_under_test", REPO / "tools" / "data_health.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dh = _load_dh()
TODAY = dt.date.today().isoformat()
T0640 = dt.datetime.combine(dt.date.today(), dt.time(6, 40))   # scan schreibt noch (vor 08:01)
T0900 = dt.datetime.combine(dt.date.today(), dt.time(9, 0))    # noch in scan-Grace (<10)
T1030 = dt.datetime.combine(dt.date.today(), dt.time(10, 30))  # nach scan-Cutoff(10), vor premarket(12)
T1230 = dt.datetime.combine(dt.date.today(), dt.time(12, 30))  # nach beiden Cutoffs


# ---------------------------------------------------------------------------
# (A) Konstanten + Helper-Logik
# ---------------------------------------------------------------------------
class TestScanLaggingConstants(unittest.TestCase):
    def test_scan_is_lagging(self):
        self.assertIn("scan", dh._LAGGING_PRODUCER_CUTOFFS)
        self.assertIn("scan", dh._LAGGING_PRODUCERS)  # back-compat Tuple-View

    def test_premarket_still_lagging(self):
        self.assertIn("premarket", dh._LAGGING_PRODUCER_CUTOFFS)
        self.assertIn("premarket", dh._LAGGING_PRODUCERS)

    def test_scan_cutoff_is_grace_cutoff_hour(self):
        # scan ist ~08:01 fertig → frueherer Cutoff als premarket
        self.assertEqual(dh._LAGGING_PRODUCER_CUTOFFS["scan"], dh.GRACE_CUTOFF_HOUR)

    def test_scan_cutoff_earlier_than_premarket(self):
        self.assertLess(dh._LAGGING_PRODUCER_CUTOFFS["scan"],
                        dh._LAGGING_PRODUCER_CUTOFFS["premarket"])


class TestScanLaggingHelper(unittest.TestCase):
    def _td_with(self, *names):
        d = tempfile.TemporaryDirectory()
        td = pathlib.Path(d.name)
        for n in names:
            (td / f"{n}_{TODAY}.json").write_text("{}", encoding="utf-8")
        return d, td

    def test_grace_true_when_sibling_present_before_cutoff(self):
        d, td = self._td_with("sector_rs", "regime", "earnings")
        try:
            self.assertTrue(dh._within_lagging_grace(TODAY, "scan", now=T0640, tools_dir=td))
            self.assertTrue(dh._within_lagging_grace(TODAY, "scan", now=T0900, tools_dir=td))
        finally:
            d.cleanup()

    def test_grace_false_after_scan_cutoff(self):
        d, td = self._td_with("sector_rs", "regime", "earnings")
        try:
            # 10:30 > scan-Cutoff(10) → echtes spaetes Versagen, keine Grace mehr
            self.assertFalse(dh._within_lagging_grace(TODAY, "scan", now=T1030, tools_dir=td))
        finally:
            d.cleanup()

    def test_grace_false_total_outage_no_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)  # leer → kein Sibling
            self.assertFalse(dh._within_lagging_grace(TODAY, "scan", now=T0640, tools_dir=td))

    def test_grace_false_old_date(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)
            (td / "regime_2099-01-02.json").write_text("{}", encoding="utf-8")
            self.assertFalse(
                dh._within_lagging_grace("2099-01-02", "scan", now=T0640, tools_dir=td))

    def test_premarket_grace_unchanged_by_scan_addition(self):
        # Regression V7.24: premarket-Grace (Cutoff 12) weiter aktiv bei sibling@11:00
        d, td = self._td_with("scan", "sector_rs", "regime", "earnings")
        try:
            eleven = dt.datetime.combine(dt.date.today(), dt.time(11, 0))
            self.assertTrue(
                dh._within_lagging_grace(TODAY, "premarket", now=eleven, tools_dir=td))
        finally:
            d.cleanup()


# ---------------------------------------------------------------------------
# (B) Routing in check_scan (TOOLS_DIR gemockt)
# ---------------------------------------------------------------------------
class _DirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dh.TOOLS_DIR
        dh.TOOLS_DIR = pathlib.Path(self._tmp.name)

    def tearDown(self):
        dh.TOOLS_DIR = self._orig
        self._tmp.cleanup()

    def _touch(self, name, body="{}"):
        (dh.TOOLS_DIR / name).write_text(body, encoding="utf-8")


class TestCheckScanLagging(_DirMixin, unittest.TestCase):
    def test_scan_pending_when_siblings_present(self):
        # exakter Live-FP 06-22: regime/sector_rs/earnings da, scan fehlt, 06:40
        for s in ("sector_rs", "regime", "earnings"):
            self._touch(f"{s}_{TODAY}.json")
        summary, findings = dh.check_scan(TODAY, now=T0640)
        self.assertEqual(summary["status"], dh.STATUS_PENDING)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], dh.SEV_L)
        self.assertEqual(findings[0]["code"], "SCAN_AWAITING_PRODUCER")
        self.assertTrue(findings[0]["detail"].get("lagging"))

    def test_scan_h_after_cutoff_with_siblings(self):
        # nach scan-Cutoff: echtes spaetes scan-Versagen bleibt sichtbar
        for s in ("sector_rs", "regime", "earnings"):
            self._touch(f"{s}_{TODAY}.json")
        summary, findings = dh.check_scan(TODAY, now=T1030)
        self.assertEqual(summary["status"], dh.STATUS_MISSING)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "SCAN_MISSING")

    def test_scan_coordinated_absence_still_pending_pre_production(self):
        # leeres TOOLS_DIR @06:40 → koordinierte Abwesenheit (V7.17), nicht lagging
        summary, findings = dh.check_scan(TODAY, now=T0640)
        self.assertEqual(summary["status"], dh.STATUS_PENDING)
        self.assertEqual(findings[0]["code"], "SCAN_AWAITING_PRODUCER")

    def test_scan_total_outage_after_cutoff_is_h(self):
        # leer @10:30 → kein Sibling, nach Cutoff → SEV_H
        summary, findings = dh.check_scan(TODAY, now=T1030)
        self.assertEqual(summary["status"], dh.STATUS_MISSING)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "SCAN_MISSING")

    def test_scan_present_ok(self):
        self._touch(f"scan_{TODAY}.json",
                    '{"universe_size":100,"valid_results":99,'
                    '"market_pcr_tag":"neutral","top_setups":[]}')
        summary, findings = dh.check_scan(TODAY, now=T0640)
        self.assertNotEqual(summary["status"], dh.STATUS_MISSING)
        self.assertFalse(any(f["code"] == "SCAN_MISSING" for f in findings))

    def test_scan_parse_error_still_h(self):
        self._touch(f"scan_{TODAY}.json", "{bad json")
        summary, findings = dh.check_scan(TODAY, now=T0640)
        self.assertEqual(summary["status"], dh.STATUS_FAIL)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "SCAN_PARSE_ERROR")


# ---------------------------------------------------------------------------
# (C) build_report End-to-End: Live-06-22 + Invarianten
# ---------------------------------------------------------------------------
class TestBuildReportScanLag(_DirMixin, unittest.TestCase):
    def _fast_siblings_present(self):
        for s in ("sector_rs", "regime", "earnings"):
            (dh.TOOLS_DIR / f"{s}_{TODAY}.json").write_text(
                '{"sources": {}}', encoding="utf-8")

    def test_live_2026_06_22_morning_not_critical(self):
        # scan + premarket fehlen (heavy/lagging), Siblings da, 06:40 → KEIN CRITICAL
        self._fast_siblings_present()
        report = dh.build_report(TODAY, now=T0640)
        self.assertNotEqual(report["overall_status"], "CRITICAL")
        h = [f for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertEqual(h, [])
        self.assertEqual(report["sources"]["scan"]["status"], dh.STATUS_PENDING)
        self.assertFalse(any(f["code"] == "SCAN_MISSING" for f in report["findings"]))
        # keine ticket-faehigen (>=M) Findings → kein FP-Ticket
        self.assertFalse(
            any(f["severity"] in (dh.SEV_H, dh.SEV_M) for f in report["findings"]))

    def test_scan_genuine_late_failure_after_cutoff_is_critical(self):
        # Siblings da, scan fehlt @10:30 (nach scan-Cutoff) → echtes scan-Versagen H
        self._fast_siblings_present()
        report = dh.build_report(TODAY, now=T1030)
        self.assertEqual(report["overall_status"], "CRITICAL")
        h_codes = [f["code"] for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertIn("SCAN_MISSING", h_codes)

    def test_no_false_pipeline_not_run_when_scan_and_premarket_lag(self):
        # 06-17-FP-Szenario: scan+premarket fehlen mid-pipeline, Siblings da, 06:40
        # → beide gracen → KEIN PIPELINE_NOT_RUN, kein CRITICAL.
        self._fast_siblings_present()
        report = dh.build_report(TODAY, now=T0640)
        codes = [f["code"] for f in report["findings"]]
        self.assertNotIn("PIPELINE_NOT_RUN", codes)
        self.assertNotIn("SCAN_MISSING", codes)
        self.assertNotEqual(report["overall_status"], "CRITICAL")

    def test_genuine_total_outage_after_cutoff_still_pipeline_not_run(self):
        # leer @10:30 → echter Komplett-Ausfall bleibt CRITICAL/PIPELINE_NOT_RUN
        report = dh.build_report(TODAY, now=T1030)
        self.assertEqual(report["overall_status"], "CRITICAL")
        h = [f for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertEqual(h[0]["code"], "PIPELINE_NOT_RUN")

    def test_fast_producer_selective_missing_still_h(self):
        # Invariante: scan da, regime fehlt @06:40 → regime ist NICHT lagging → H
        (dh.TOOLS_DIR / f"scan_{TODAY}.json").write_text(
            '{"universe_size":100,"valid_results":99,'
            '"market_pcr_tag":"neutral","top_setups":[]}', encoding="utf-8")
        (dh.TOOLS_DIR / f"sector_rs_{TODAY}.json").write_text('{"sources":{}}', encoding="utf-8")
        (dh.TOOLS_DIR / f"earnings_{TODAY}.json").write_text('{"sources":{}}', encoding="utf-8")
        report = dh.build_report(TODAY, now=T0640)
        h_codes = [f["code"] for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertIn("REGIME_JSON_MISSING", h_codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
