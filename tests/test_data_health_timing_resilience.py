#!/usr/bin/env python3
"""Tests fuer DATA-HEALTH-TIMING-RESILIENCE-1 (Fix KW24 / V7.17).

Beweist, dass data_health.py die zweite Falsch-Positiv-Klasse (nach der
V7.16-Disabled-Layer-Policy) nicht mehr als SEV_H eskaliert:

  (A) Pre-Production-Timing-Grace: fehlt ein aktiver Producer waehrend einer
      KOORDINIERTEN Komplett-Abwesenheit am heutigen Tag vor GRACE_CUTOFF_HOUR
      → SEV_L AWAITING_PRODUCER (Status PENDING), KEIN Ticket. Selektives Fehlen
      (ein Producer da → Produktion gestartet), Lauf nach Cutoff oder an einem
      Alt-Datum → unveraendert SEV_H *_JSON_MISSING (echter Ausfall sichtbar).

  (B) EDEADLK-resilienter Reader: Datei existiert (stat ok), Read scheitert mit
      OSError trotz Retry → SEV_L UNREADABLE_ENV (Status ENV_UNREADABLE), KEIN
      Ticket. Echte Korruption (json.loads wirft) → unveraendert SEV_H
      *_PARSE_ERROR.

Invarianten: ein echter Komplett-Ausfall nach dem Cutoff bleibt CRITICAL;
die V7.16-Disabled-Layer-Policy bleibt wirksam. Laeuft offline, read-only.

    python3 tests/test_data_health_timing_resilience.py   # oder: pytest …
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
        "data_health_timing_under_test", REPO / "tools" / "data_health.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dh = _load_dh()
TODAY = dt.date.today().isoformat()
PRE = dt.datetime.combine(dt.date.today(), dt.time(6, 40))    # vor Cutoff (06:40)
POST = dt.datetime.combine(dt.date.today(), dt.time(11, 0))   # nach Cutoff (11:00)


# ---------------------------------------------------------------------------
# (B1) Fehlerklassifikation
# ---------------------------------------------------------------------------
class TestClassifyLoadErr(unittest.TestCase):
    def test_missing(self):
        self.assertEqual(dh._classify_load_err("file_not_found: scan_x.json"), "missing")

    def test_env_unreadable(self):
        self.assertEqual(
            dh._classify_load_err("unreadable_env: [OSError errno=35] deadlock"),
            "env_unreadable")

    def test_parse(self):
        self.assertEqual(dh._classify_load_err("parse_error: Expecting value"), "parse")

    def test_none(self):
        self.assertIsNone(dh._classify_load_err(None))


# ---------------------------------------------------------------------------
# (B2) Resilienter Reader: Retry + OSError-Durchreichung
# ---------------------------------------------------------------------------
class _BoomPath:
    """Datei-Stub, dessen read_text immer mit EDEADLK scheitert (zaehlt Versuche)."""
    def __init__(self, errno=35):
        self.calls = 0
        self._errno = errno

    def read_text(self, encoding="utf-8"):
        self.calls += 1
        raise OSError(self._errno, "Resource deadlock avoided")


class TestResilientReader(unittest.TestCase):
    def test_success_reads_real_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.json"
            p.write_text('{"a": 1}', encoding="utf-8")
            text, exc = dh._read_text_resilient(p)
            self.assertIsNone(exc)
            self.assertIn('"a"', text)

    def test_retries_then_returns_oserror(self):
        bp = _BoomPath(35)
        text, exc = dh._read_text_resilient(bp, attempts=3, sleep_s=0.0)
        self.assertIsNone(text)
        self.assertIsInstance(exc, OSError)
        self.assertEqual(exc.errno, 35)
        self.assertEqual(bp.calls, 3)  # alle Versuche ausgeschoepft

    def test_load_json_real_corruption_is_parse(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "bad.json"
            p.write_text("{ this is : not json", encoding="utf-8")
            doc, err = dh._load_json(p)
            self.assertIsNone(doc)
            self.assertEqual(dh._classify_load_err(err), "parse")

    def test_load_json_missing(self):
        with tempfile.TemporaryDirectory() as d:
            doc, err = dh._load_json(pathlib.Path(d) / "nope.json")
            self.assertIsNone(doc)
            self.assertEqual(dh._classify_load_err(err), "missing")


# ---------------------------------------------------------------------------
# (A1) Pre-Production-Fenster-Logik
# ---------------------------------------------------------------------------
class TestProductionWindow(unittest.TestCase):
    def test_coordinated_absence_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(dh._coordinated_absence(TODAY, tools_dir=pathlib.Path(d)))

    def test_not_coordinated_when_one_producer_present(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / f"scan_{TODAY}.json").write_text("{}", encoding="utf-8")
            self.assertFalse(dh._coordinated_absence(TODAY, tools_dir=pathlib.Path(d)))

    def test_grace_only_today_before_cutoff(self):
        self.assertTrue(dh._within_production_grace(TODAY, now=PRE))
        self.assertFalse(dh._within_production_grace(TODAY, now=POST))
        self.assertFalse(dh._within_production_grace("2099-01-02", now=PRE))

    def test_pre_production_true_only_when_coordinated_and_in_grace(self):
        with tempfile.TemporaryDirectory() as d:
            td = pathlib.Path(d)
            self.assertTrue(dh._is_pre_production(TODAY, now=PRE, tools_dir=td))
            self.assertFalse(dh._is_pre_production(TODAY, now=POST, tools_dir=td))
            # selektiv: ein Producer da → kein Pre-Production mehr
            (td / f"scan_{TODAY}.json").write_text("{}", encoding="utf-8")
            self.assertFalse(dh._is_pre_production(TODAY, now=PRE, tools_dir=td))


# ---------------------------------------------------------------------------
# (A2/B3) Routing in check_scan / check_source_json (TOOLS_DIR gemockt)
# ---------------------------------------------------------------------------
class _DirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_tools = dh.TOOLS_DIR
        dh.TOOLS_DIR = pathlib.Path(self._tmp.name)
        self._orig_load = dh._load_json

    def tearDown(self):
        dh.TOOLS_DIR = self._orig_tools
        dh._load_json = self._orig_load
        self._tmp.cleanup()


class TestCheckTimingRouting(_DirMixin, unittest.TestCase):
    def test_scan_pending_pre_production(self):
        summary, findings = dh.check_scan(TODAY, now=PRE)  # leeres TOOLS_DIR
        self.assertEqual(summary["status"], dh.STATUS_PENDING)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], dh.SEV_L)
        self.assertEqual(findings[0]["code"], "SCAN_AWAITING_PRODUCER")

    def test_scan_missing_after_cutoff_is_h(self):
        summary, findings = dh.check_scan(TODAY, now=POST)
        self.assertEqual(summary["status"], dh.STATUS_MISSING)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "SCAN_MISSING")

    def test_source_pending_pre_production(self):
        summary, findings = dh.check_source_json(TODAY, "sector_rs", [], now=PRE)
        self.assertEqual(summary["status"], dh.STATUS_PENDING)
        self.assertEqual(findings[0]["severity"], dh.SEV_L)
        self.assertEqual(findings[0]["code"], "SECTOR_RS_AWAITING_PRODUCER")

    def test_source_missing_selective_is_h(self):
        # scan da → Produktion gestartet → ein noch fehlender Producer ist echt H
        (dh.TOOLS_DIR / f"scan_{TODAY}.json").write_text("{}", encoding="utf-8")
        summary, findings = dh.check_source_json(TODAY, "regime", [], now=PRE)
        self.assertEqual(summary["status"], dh.STATUS_MISSING)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "REGIME_JSON_MISSING")

    def test_source_env_unreadable_is_l(self):
        # Datei existiert, _load_json liefert env-unreadable → SEV_L, kein H
        (dh.TOOLS_DIR / f"sector_rs_{TODAY}.json").write_text("{}", encoding="utf-8")
        dh._load_json = lambda p: (None, "unreadable_env: [OSError errno=35] deadlock (stat_size=86980)")
        summary, findings = dh.check_source_json(TODAY, "sector_rs", [], now=POST)
        self.assertEqual(summary["status"], dh.STATUS_ENV_UNREADABLE)
        self.assertEqual(findings[0]["severity"], dh.SEV_L)
        self.assertEqual(findings[0]["code"], "SECTOR_RS_UNREADABLE_ENV")

    def test_source_parse_error_still_h(self):
        (dh.TOOLS_DIR / f"regime_{TODAY}.json").write_text("{bad", encoding="utf-8")
        summary, findings = dh.check_source_json(TODAY, "regime", [], now=POST)
        self.assertEqual(summary["status"], dh.STATUS_FAIL)
        self.assertEqual(findings[0]["severity"], dh.SEV_H)
        self.assertEqual(findings[0]["code"], "REGIME_PARSE_ERROR")

    def test_scan_env_unreadable_is_l(self):
        dh._load_json = lambda p: (None, "unreadable_env: [OSError errno=35] deadlock (stat_size=1244781)")
        summary, findings = dh.check_scan(TODAY, now=POST)
        self.assertEqual(summary["status"], dh.STATUS_ENV_UNREADABLE)
        self.assertEqual(findings[0]["severity"], dh.SEV_L)
        self.assertEqual(findings[0]["code"], "SCAN_UNREADABLE_ENV")


# ---------------------------------------------------------------------------
# (A3) build_report End-to-End: morgendliches Pre-Production darf nicht
#      CRITICAL sein: ein echter Komplett-Ausfall nach dem Cutoff schon.
# ---------------------------------------------------------------------------
class TestBuildReportTiming(_DirMixin, unittest.TestCase):
    def test_pre_production_is_not_critical(self):
        report = dh.build_report(TODAY, now=PRE)  # leeres TOOLS_DIR, 06:40
        self.assertEqual(report["overall_status"], dh.STATUS_OK)
        self.assertEqual(report["summary"]["findings_h"], 0)
        self.assertEqual(report["summary"]["sources_pending"], 5)  # scan+sector_rs+regime+premarket+earnings
        self.assertEqual(report.get("advisory"), "warming_up")

    def test_total_outage_after_cutoff_is_critical(self):
        # V7.19: ein Komplett-Ausfall nach Cutoff buendelt die fehlenden aktiven
        # Producer zu EINEM PIPELINE_NOT_RUN-Finding (H): die NO-TRADE-Invariante
        # (overall_status CRITICAL) bleibt erhalten, aber 1 Ticket statt 5.
        report = dh.build_report(TODAY, now=POST)  # leeres TOOLS_DIR, 11:00
        self.assertEqual(report["overall_status"], "CRITICAL")
        self.assertEqual(report["summary"]["findings_h"], 1)
        h = [f for f in report["findings"] if f["severity"] == dh.SEV_H]
        self.assertEqual(h[0]["code"], "PIPELINE_NOT_RUN")
        self.assertEqual(report["summary"]["sources_pending"], 0)
        self.assertNotIn("advisory", report)

    def test_no_pending_tickets_written(self):
        # L-Findings (PENDING/ENV) duerfen NIE Tickets erzeugen.
        report = dh.build_report(TODAY, now=PRE)
        pending = [f for f in report["findings"] if f["code"].endswith("AWAITING_PRODUCER")]
        self.assertTrue(pending)
        self.assertTrue(all(f["severity"] == dh.SEV_L for f in pending))


if __name__ == "__main__":
    unittest.main(verbosity=2)
