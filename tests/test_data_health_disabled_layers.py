#!/usr/bin/env python3
"""Tests fuer DATA-HEALTH-DISABLED-LAYER-BLINDNESS-1 (Fix KW24 / V7.16).

Beweist, dass data_health.py die by-design abgeschalteten Scoring-Layer
(daily_scanner.DISABLED_LAYERS = news/options_flow/insider/market_pcr) kennt
und Findings dieser Layer auf SEV_L zurueckstuft, statt sie taeglich als
SEV_H "INACTIVE"/"JSON_MISSING" zu eskalieren (chronische Falsch-Eskalation,
9-Wochen-Streak im ops_escalation.md).

Deckt ab:
  - get_disabled_layers() liest DISABLED_LAYERS aus daily_scanner.py (SSoT)
  - _finding_layer() mappt Codes/Sources auf den richtigen Layer
  - apply_disabled_layer_policy() stuft nur deaktivierte Layer zurueck,
    laesst aktive Layer (scan/mtf/sector_rs/regime/premarket/earnings) H
  - Idempotenz (zweimal anwenden aendert nichts)
  - Aggregations-Effekt: disabled-only Findings -> kein CRITICAL mehr
  - build_report() End-to-End auf einem all-missing Sentinel-Datum:
    options_flow/insider -> DISABLED, sector_rs -> MISSING (aktiv bleibt rot)

Laeuft offline, read-only (build_report schreibt keine Tickets).
    python3 tests/test_data_health_disabled_layers.py   # oder: pytest …
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_dh():
    spec = importlib.util.spec_from_file_location(
        "data_health_under_test", REPO / "tools" / "data_health.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dh = _load_dh()


def _f(severity, source, code, message="x"):
    return dh.make_finding(severity, source, code, message)


class TestGetDisabledLayers(unittest.TestCase):
    def test_reads_ssot_from_daily_scanner(self):
        layers = dh.get_disabled_layers()
        self.assertIsInstance(layers, set)
        self.assertTrue(layers, "DISABLED_LAYERS darf nicht leer sein")
        # die beiden in CLAUDE.md kanonisch genannten Layer muessen drin sein
        self.assertIn("options_flow", layers)
        self.assertIn("market_pcr", layers)

    def test_fallback_default_is_sane(self):
        # Default-Set deckt die H2-Ablation ab
        self.assertEqual(
            dh._DEFAULT_DISABLED_LAYERS,
            {"news", "options_flow", "insider", "market_pcr"},
        )


class TestFindingLayer(unittest.TestCase):
    def test_options_flow_codes(self):
        self.assertEqual(_layer("OPTIONS_FLOW_INACTIVE", "scan"), "options_flow")
        self.assertEqual(_layer("OPTIONS_FLOW_JSON_MISSING", "options_flow"), "options_flow")
        self.assertEqual(_layer("OPTIONS_FLOW_CBOE_PCR_ERROR", "options_flow"), "options_flow")

    def test_market_pcr_codes_take_priority(self):
        # MARKET_PCR muss vor OPTIONS_FLOW greifen
        self.assertEqual(_layer("MARKET_PCR_INACTIVE", "scan"), "market_pcr")
        self.assertEqual(_layer("SCAN_MARKET_PCR_TAG_UNKNOWN", "scan"), "market_pcr")
        self.assertEqual(_layer("OPTIONS_FLOW_MARKET_PCR_DOWN", "options_flow"), "market_pcr")

    def test_insider_codes(self):
        self.assertEqual(_layer("INSIDER_JSON_MISSING", "insider"), "insider")
        self.assertEqual(_layer("INSIDER_COVERAGE_GAP", "cross_insider_scan"), "insider")

    def test_active_layers_return_none(self):
        # diese duerfen NIE einem disabled-Layer zugeordnet werden
        self.assertIsNone(_layer("SCAN_MISSING", "scan"))
        self.assertIsNone(_layer("SCAN_FILL_LT_80", "scan"))
        self.assertIsNone(_layer("MTF_H1_INACTIVE", "scan"))
        self.assertIsNone(_layer("SECTOR_RS_JSON_MISSING", "sector_rs"))
        self.assertIsNone(_layer("REGIME_JSON_MISSING", "regime"))
        self.assertIsNone(_layer("PREMARKET_JSON_MISSING", "premarket"))
        self.assertIsNone(_layer("EARNINGS_JSON_MISSING", "earnings"))


def _layer(code, source):
    return dh._finding_layer({"code": code, "source": source})


class TestApplyPolicy(unittest.TestCase):
    DIS = {"news", "options_flow", "insider", "market_pcr"}

    def test_downgrades_disabled_layer_h_to_l(self):
        findings = [_f("H", "scan", "OPTIONS_FLOW_INACTIVE")]
        n = dh.apply_disabled_layer_policy(findings, self.DIS)
        self.assertEqual(n, 1)
        self.assertEqual(findings[0]["severity"], "L")
        self.assertTrue(findings[0]["intentionally_disabled"])
        self.assertEqual(findings[0]["original_severity"], "H")
        self.assertEqual(findings[0]["disabled_layer"], "options_flow")

    def test_preserves_active_layer_findings(self):
        findings = [
            _f("H", "scan", "SCAN_MISSING"),
            _f("H", "sector_rs", "SECTOR_RS_JSON_MISSING"),
            _f("H", "regime", "REGIME_JSON_MISSING"),
        ]
        dh.apply_disabled_layer_policy(findings, self.DIS)
        self.assertTrue(all(x["severity"] == "H" for x in findings))
        self.assertTrue(all(not x.get("intentionally_disabled") for x in findings))

    def test_mixed_set(self):
        findings = [
            _f("H", "scan", "SCAN_MISSING"),              # aktiv -> bleibt H
            _f("H", "scan", "OPTIONS_FLOW_INACTIVE"),     # disabled -> L
            _f("H", "scan", "MARKET_PCR_INACTIVE"),       # disabled -> L
            _f("M", "insider", "INSIDER_COVERAGE_GAP"),   # disabled -> L
        ]
        dh.apply_disabled_layer_policy(findings, self.DIS)
        sev = [x["severity"] for x in findings]
        self.assertEqual(sev, ["H", "L", "L", "L"])

    def test_idempotent(self):
        findings = [_f("H", "scan", "OPTIONS_FLOW_INACTIVE")]
        dh.apply_disabled_layer_policy(findings, self.DIS)
        msg_after_first = findings[0]["message"]
        n2 = dh.apply_disabled_layer_policy(findings, self.DIS)
        self.assertEqual(n2, 0)  # nichts mehr zurueckzustufen
        self.assertEqual(findings[0]["severity"], "L")
        self.assertEqual(findings[0]["message"], msg_after_first)  # kein Doppel-Prefix


class TestAggregationEffect(unittest.TestCase):
    """Spiegelt die build_report-Aggregation: disabled-only -> nicht CRITICAL."""
    DIS = {"news", "options_flow", "insider", "market_pcr"}

    def _status(self, findings):
        dh.apply_disabled_layer_policy(findings, self.DIS)
        h = sum(1 for x in findings if x["severity"] == "H")
        m = sum(1 for x in findings if x["severity"] == "M")
        return "CRITICAL" if h else ("DEGRADED" if m else "OK")

    def test_disabled_only_is_not_critical(self):
        findings = [
            _f("H", "scan", "OPTIONS_FLOW_INACTIVE"),
            _f("H", "scan", "MARKET_PCR_INACTIVE"),
            _f("H", "scan", "SCAN_MARKET_PCR_TAG_UNKNOWN"),
            _f("H", "options_flow", "OPTIONS_FLOW_CBOE_PCR_ERROR"),
            _f("M", "insider", "INSIDER_COVERAGE_GAP"),
        ]
        self.assertEqual(self._status(findings), "OK")

    def test_real_scan_missing_still_critical(self):
        findings = [
            _f("H", "scan", "SCAN_MISSING"),
            _f("H", "scan", "OPTIONS_FLOW_INACTIVE"),  # FP daneben
        ]
        self.assertEqual(self._status(findings), "CRITICAL")


class TestBuildReportIntegration(unittest.TestCase):
    """End-to-End gegen ein all-missing Sentinel-Datum (keine Fixtures noetig,
    read-only: build_report schreibt keine Tickets)."""
    SENTINEL = "2099-01-02"

    def setUp(self):
        self.report = dh.build_report(self.SENTINEL)

    def test_disabled_sources_marked_disabled(self):
        srcs = self.report["sources"]
        self.assertEqual(srcs["options_flow"]["status"], dh.STATUS_DISABLED)
        self.assertEqual(srcs["insider"]["status"], dh.STATUS_DISABLED)

    def test_active_sources_still_missing(self):
        srcs = self.report["sources"]
        # aktive Inputs duerfen NICHT versteckt werden
        self.assertEqual(srcs["sector_rs"]["status"], dh.STATUS_MISSING)
        self.assertEqual(srcs["regime"]["status"], dh.STATUS_MISSING)

    def test_report_exposes_policy_metadata(self):
        self.assertIn("disabled_layers", self.report)
        self.assertIn("options_flow", self.report["disabled_layers"])
        self.assertGreaterEqual(self.report["suppressed_disabled_findings"], 2)

    def test_all_disabled_findings_are_L(self):
        for f in self.report["findings"]:
            if f.get("intentionally_disabled"):
                self.assertEqual(f["severity"], "L")


if __name__ == "__main__":
    unittest.main(verbosity=2)
