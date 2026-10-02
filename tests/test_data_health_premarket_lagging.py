#!/usr/bin/env python3
"""Tests fuer DATA-HEALTH-PREMARKET-TIMING-FP-1 / LAGGING-PRODUCER-GRACE-1 (V7.24).

Beweist, dass data_health.py die dritte Falsch-Positiv-Klasse (nach V7.16
Disabled-Layer und V7.17 Pre-Production-Timing) nicht mehr als SEV_H eskaliert:

  Der bekannt LANGSAME Producer `premarket` (premarket_scanner.py, 39-76 Min fuer
  639 Namen, PREMARKET-LATENCY-1) ist nativ erst ~11:16 CET fertig: lange nachdem
  die schnellen Producer (scan/sector_rs/regime/earnings) ihre JSONs geschrieben
  haben. Der data_health-Snapshot dazwischen flaggte das noch-produzierende
  premarket_<date>.json faelschlich als SEV_H PREMARKET_JSON_MISSING → overall
  CRITICAL → Risiko eines falschen Morgen-NO-TRADE (FP-Tage 2026-06-10/-16).

Fix: Lagging-Producer-Grace. Fehlt das JSON eines langsamen Producers heute vor
LAGGING_PRODUCER_CUTOFF_HOUR, waehrend ein anderer aktiver Producer bereits da ist
(Pipeline laeuft) → SEV_L AWAITING_PRODUCER (Status PENDING), KEIN Ticket.

Invarianten (NICHT maskiert):
  * Nach dem Cutoff → echtes spaetes premarket-Versagen bleibt SEV_H.
  * Komplett-Ausfall (kein Sibling da) → koordinierte Logik (PENDING/PIPELINE_NOT_RUN).
  * Schnelle Producer (scan/sector_rs/regime/earnings) → selektives Fehlen bleibt H.

    python3 -m unittest tests.test_data_health_premarket_lagging   # oder pytest
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
        "data_health_lagging_under_test", REPO / "tools" / "data_health.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dh = _load_dh()
TODAY = dt.date.today().isoformat()
MORNING = dt.datetime.combine(dt.date.today(), dt.time(9, 0))    # premarket laeuft noch
LATE_OK = dt.datetime.combine(dt.date.today(), dt.time(11, 30))  # noch in Lagging-Grace
AFTER = dt.datetime.combine(dt.date.today(), dt.time(12, 30))    # nach Lagging-Cutoff


# ---------------------------------------------------------------------------
# (A) Helper-Logik _within_lagging_grace
# ---------------------------------------------------------------------------
class TestLaggingGraceHelper(unittest.TestCase):
    def test_premarket_is_lagging(self):
        self.assertIn("premarket", dh._LAGGING_PRODUCERS)

    def test_cutoff_later_than_default(self):
        self.assertGreater(dh.LAGGING_PRODUCER_CUTOFF_HOUR, dh.GRACE_CUTOFF_HOUR)

    def test_grace_true_when_sibling_present_before_cutoff(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)
            (td / f"scan_{TODAY}.json").write_text("{}", encoding="utf-8")
            self.assertTrue(
                dh._within_lagging_grace(TODAY, "premarket", now=MORNING, tools_dir=td))
            self.assertTrue(
                dh._within_lagging_grace(TODAY, "premarket", now=LATE_OK, tools_dir=td))

    def test_grace_false_after_cutoff(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)
            (td / f"scan_{TODAY}.json").write_text("{}", encoding="utf-8")
            self.assertFalse(
                dh._within_lagging_grace(TODAY, "premarket", now=AFTER, tools_dir=td))

    def test_grace_false_total_outage_no_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)  # leer → kein Sibling
            self.assertFalse(
                dh._within_lagging_grace(TODAY, "premarket", now=MORNING, tools_dir=td))

    def test_grace_false_for_fast_producer(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)
            (td / f"scan_{TODAY}.json").write_text("{}", encoding="utf-8")
            # regime ist NICHT lagging → keine Grace, auch wenn scan da ist
            self.assertFalse(
                dh._within_lagging_grace(TODAY, "regime", now=MORNING, tools_dir=td))

    def test_grace_false_old_date(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)
            (td / "scan_2099-01-02.json").write_text("{}", encoding="utf-8")
            self.assertFalse(
                dh._within_lagging_grace("2099-01-02", "premarket", now=MORNING, tools_dir=td))


# ---------------------------------------------------------------------------
# (B) Routing in check_source_json (TOOLS_DIR gemockt)
# ---------------------------------------------------------------------------
class _DirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dh.TOOLS_DIR
        dh.TOOLS_DIR = pathlib.Path(self._tmp.name)

    def tearDown(self):
        dh.TOOLS_DIR = self._orig
        self._tmp.cleanup()

    def _touch(self, name):
        (dh.TOOLS_DIR / name).write_text("{}", encoding="utf-8")


class TestCheckSourceLagging(_DirMixin, unittest.TestCase):
    def test_premarket_pending_when_siblings_present(self):
        # genau der Live-FP 06-16: scan/sector_rs/regime/earnings da, premarket fehlt
        for s in ("scan", "sector_rs", "regime", "earnings"):
            self._touch(f"{s}_{TODAY}.json")
        summary, findings = dh.check_source_json(TODAY, "premarket", [], now=MORNING)
        self.assertEqual(summary["status"], dh.STATUS_PENDING)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], dh.SEV_L)
        self.assertEqual(findings[0]["code"], "PREMARKET_AWAITING_PRODUCER")
        self.assertTrue(findings[0]["detail"].get("lagging"))

    def test_premarket_h_after_cutoff(self):
        for s in ("scan", "sector_rs", "regime", "earnings"):
            self._touch(f"{s}_{TODAY}.json")
        summary, findings = dh.check_source_json(TODAY, "premarket", [], now=AFTER)
        self.assertEqual(summary["status"], dh.STATUS_MISSING)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "PREMARKET_JSON_MISSING")

    def test_fast_producer_selective_missing_still_h(self):
        # regime fehlt, scan da, 09:00 → regime ist nicht lagging → H bleibt
        self._touch(f"scan_{TODAY}.json")
        summary, findings = dh.check_source_json(TODAY, "regime", [], now=MORNING)
        self.assertEqual(summary["status"], dh.STATUS_MISSING)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "REGIME_JSON_MISSING")

    def test_premarket_present_ok(self):
        for s in ("scan", "sector_rs", "regime", "earnings", "premarket"):
            self._touch(f"{s}_{TODAY}.json")
        summary, findings = dh.check_source_json(TODAY, "premarket", [], now=MORNING)
        self.assertNotEqual(summary["status"], dh.STATUS_MISSING)
        self.assertFalse(any(f["code"] == "PREMARKET_JSON_MISSING" for f in findings))


# ---------------------------------------------------------------------------
# (C) build_report End-to-End
# ---------------------------------------------------------------------------
class TestBuildReportLagging(_DirMixin, unittest.TestCase):
    def _fast_present(self):
        # scan braucht eine gesunde Fill-Rate + gesetzten market_pcr_tag, damit der
        # scan-Check nicht selbst H wirft (sonst maskiert ein Stub-Artefakt den Test).
        scan_doc = (
            '{"universe_size": 100, "valid_results": 99, "market_pcr_tag": "neutral", '
            '"top_setups": []}'
        )
        (dh.TOOLS_DIR / f"scan_{TODAY}.json").write_text(scan_doc, encoding="utf-8")
        for s in ("sector_rs", "regime", "earnings"):
            (dh.TOOLS_DIR / f"{s}_{TODAY}.json").write_text(
                '{"sources": {}}', encoding="utf-8")

    def test_premarket_only_missing_not_critical(self):
        # Live-Szenario 06-16: nur premarket fehlt, 09:00 → KEIN CRITICAL
        self._fast_present()
        report = dh.build_report(TODAY, now=MORNING)
        self.assertNotEqual(report["overall_status"], "CRITICAL")
        h = [f for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertEqual(h, [])
        self.assertEqual(report["sources"]["premarket"]["status"], dh.STATUS_PENDING)

    def test_premarket_only_missing_after_cutoff_is_critical(self):
        # nach Lagging-Cutoff: echtes spaetes premarket-Versagen bleibt sichtbar
        self._fast_present()
        report = dh.build_report(TODAY, now=AFTER)
        self.assertEqual(report["overall_status"], "CRITICAL")
        h = [f for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertTrue(any(f["code"] == "PREMARKET_JSON_MISSING" for f in h))


if __name__ == "__main__":
    unittest.main(verbosity=2)
