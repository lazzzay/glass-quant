"""
Unit-Tests fuer V7.9.27 MARKET-PCR-RESILIENCE-1 (KW21-Bugfix-Tag, 2026-05-18).

Adressiert die 5-Wochen-Streak (KW16..KW20) in dem CBOE-403 + yfinance-OI
gleichzeitig kaputt waren und V6.8 market_pcr_adjust 0% des Score-Beitrags
geliefert hat. Dritte Resolver-Stage liest letzten gueltigen market_pcr-Wert
aus options_flow_YYYY-MM-DD.json-Snapshots (max 2 Werktage stale).

Deckt:
  T01 Cache liefert ok bei gueltigem D-1-Snapshot.
  T02 Cache ueberspringt Sa/So und liefert D-3-Wert (Mo today -> Fr Werktag).
  T03 Cache respektiert max_age_workdays=2 (D-3 wird nicht angefasst).
  T04 PCR-Plausibilitaet: <0.10 oder >10.0 wird ignoriert, naechster Snapshot wird probiert.
  T05 Cache-of-Cache wird ignoriert (source=cache_lastknown im Snapshot).
  T06 Snapshot ohne ok=True wird uebersprungen.
  T07 max_age_workdays=0 -> sofort unavailable.
  T08 Fehlende options_flow_*.json -> unavailable.
  T09 fetch_market_pcr() reiht Stage 3 hinter CBOE+yfinance ein (Backwards-Compat).
  T10 Snapshot mit total=None wird uebersprungen.
  T11 Originale Source-Markierung (orig_source/orig_date) im Ergebnis vorhanden.
  T12 Korrupte JSON-Datei wird uebersprungen, nicht crash.

Run:   python3 -m unittest tests.test_market_pcr_lastgood -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from tools import options_flow as of_mod  # noqa: E402


def _write_snapshot(tmpdir: str, day: str, market_pcr: dict) -> None:
    """Hilfsfunktion: schreibt einen minimalen options_flow_YYYY-MM-DD.json-Snapshot."""
    payload = {
        "date": day,
        "version": "V6.8-E10F",
        "market_pcr": market_pcr,
        "sources": {},
        "ticker_map": {},
    }
    with open(os.path.join(tmpdir, f"options_flow_{day}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)


class TestMarketPcrLastGoodCache(unittest.TestCase):
    """Stage-3-Cache-Tests."""

    def test_T01_d_minus_1_returns_ok(self):
        """Cache liefert ok bei gueltigem D-1-Snapshot (Werktag-Distanz=1)."""
        with tempfile.TemporaryDirectory() as tmp:
            # Mi 2026-05-13 = D-1, Do 2026-05-14 = today
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": True, "total": 0.95, "equity": 0.80,
                             "index": 1.10, "tag": "neutral", "source": "yfinance_fallback"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["source"], "cache_lastknown")
            self.assertEqual(res["staleness_workdays"], 1)
            self.assertEqual(res["original_source"], "yfinance_fallback")
            self.assertEqual(res["original_date"], "2026-05-13")
            self.assertAlmostEqual(res["total"], 0.95)
            self.assertEqual(res["tag"], "neutral")

    def test_T02_skips_weekend_and_returns_friday(self):
        """Mo today (2026-05-18) -> Fr (2026-05-15) ist Werktag-Distanz=1."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_snapshot(tmp, "2026-05-15",
                            {"ok": True, "total": 1.25, "equity": 1.10,
                             "index": 1.40, "tag": "defensive_market",
                             "source": "cboe"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-18", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["staleness_workdays"], 1)
            self.assertEqual(res["original_date"], "2026-05-15")
            self.assertEqual(res["tag"], "defensive_market")

    def test_T03_max_age_workdays_cap_blocks_older(self):
        """max_age_workdays=2: D-3-Werktag wird nicht angefasst."""
        with tempfile.TemporaryDirectory() as tmp:
            # Nur ein 3-Werktage-alter Snapshot existiert
            _write_snapshot(tmp, "2026-05-11",
                            {"ok": True, "total": 0.90, "source": "cboe"})
            # today=2026-05-14 (Do); 3 Werktage zurueck = Mo 11.05.
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertFalse(res["ok"])
            self.assertEqual(res["source"], "cache_lastknown_unavailable")

    def test_T04_implausible_pcr_skipped_then_next_tried(self):
        """PCR<0.10 oder >10.0 wird uebersprungen, naechster Snapshot wird probiert."""
        with tempfile.TemporaryDirectory() as tmp:
            # D-1 hat impossible PCR, D-2 hat plausiblen
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": True, "total": 0.05, "source": "cboe"})
            _write_snapshot(tmp, "2026-05-12",
                            {"ok": True, "total": 1.10, "source": "yfinance_fallback"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["staleness_workdays"], 2)
            self.assertEqual(res["original_date"], "2026-05-12")

    def test_T05_cache_of_cache_skipped(self):
        """source='cache_lastknown' im Snapshot wird uebersprungen (Drift-Risiko)."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": True, "total": 0.95, "source": "cache_lastknown",
                             "staleness_workdays": 1, "original_source": "cboe"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertFalse(res["ok"])
            self.assertEqual(res["source"], "cache_lastknown_unavailable")

    def test_T06_not_ok_snapshot_skipped(self):
        """Snapshot mit ok=False wird uebersprungen."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": False, "error": "all_sources_down",
                             "source": "all_sources_down"})
            _write_snapshot(tmp, "2026-05-12",
                            {"ok": True, "total": 1.15, "source": "cboe"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["staleness_workdays"], 2)
            self.assertEqual(res["original_source"], "cboe")

    def test_T07_max_age_zero_returns_unavailable(self):
        """max_age_workdays<1 liefert sofort unavailable."""
        with tempfile.TemporaryDirectory() as tmp:
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=0)
            self.assertFalse(res["ok"])
            self.assertIn("max_age", res["error"])

    def test_T08_no_files_returns_unavailable(self):
        """Fehlende options_flow_*.json -> unavailable."""
        with tempfile.TemporaryDirectory() as tmp:
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=5)
            self.assertFalse(res["ok"])
            self.assertEqual(res["source"], "cache_lastknown_unavailable")

    def test_T09_fetch_market_pcr_uses_cache_when_live_dead(self):
        """fetch_market_pcr() reiht Cache-Stage hinter ALLEN live-Stages ein.

        V7.9.31 MARKET-PCR-RESILIENCE-3 (2026-05-21): jetzt 5 Stages.
        Muss auch SPX-Index + CBOE-HTML mocken, damit Cache greift.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": True, "total": 1.20, "source": "cboe",
                             "tag": "defensive_market"})
            with patch.object(of_mod, "_fetch_market_pcr_cboe",
                              return_value={"ok": False, "error": "HTTP 403",
                                            "source": "cboe"}), \
                 patch.object(of_mod, "_fetch_market_pcr_yfinance_fallback",
                              return_value={"ok": False, "error": "OI=0",
                                            "source": "yfinance_fallback"}), \
                 patch.object(of_mod, "_fetch_market_pcr_spx_index",
                              return_value={"ok": False, "error": "spx_no_oi",
                                            "source": "yfinance_index_unavailable"}), \
                 patch.object(of_mod, "_fetch_market_pcr_cboe_html",
                              return_value={"ok": False, "error": "html_parse_fail",
                                            "source": "cboe_html_unavailable"}):
                res = of_mod.fetch_market_pcr(
                    today="2026-05-14", tools_dir=tmp,
                    cache_max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["source"], "cache_lastknown")
            self.assertEqual(res["cboe_error"], "HTTP 403")
            self.assertEqual(res["yfinance_etf_error"], "OI=0")
            self.assertEqual(res["yfinance_index_error"], "spx_no_oi")
            self.assertEqual(res["cboe_html_error"], "html_parse_fail")

    def test_T10_total_none_snapshot_skipped(self):
        """Snapshot mit total=None wird uebersprungen."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": True, "total": None, "source": "cboe"})
            _write_snapshot(tmp, "2026-05-12",
                            {"ok": True, "total": 0.85, "source": "cboe"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["staleness_workdays"], 2)

    def test_T11_original_source_and_date_in_result(self):
        """Originale Source-Markierung muss im Ergebnis stehen."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_snapshot(tmp, "2026-05-13",
                            {"ok": True, "total": 0.95, "source": "yfinance_fallback",
                             "equity": 0.80, "index": 1.10})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["original_source"], "yfinance_fallback")
            self.assertEqual(res["original_date"], "2026-05-13")
            self.assertAlmostEqual(res["equity"], 0.80)
            self.assertAlmostEqual(res["index"], 1.10)
            self.assertIn("V7.9.27", res["note"])

    def test_T12_corrupt_json_skipped(self):
        """Korrupte JSON-Datei wird uebersprungen, nicht crash."""
        with tempfile.TemporaryDirectory() as tmp:
            # D-1 ist korrupt
            with open(os.path.join(tmp, "options_flow_2026-05-13.json"),
                      "w", encoding="utf-8") as f:
                f.write("{not valid json")
            _write_snapshot(tmp, "2026-05-12",
                            {"ok": True, "total": 1.05, "source": "cboe"})
            res = of_mod._fetch_market_pcr_lastgood_cache(
                today="2026-05-14", tools_dir=tmp, max_age_workdays=2)
            self.assertTrue(res["ok"])
            self.assertEqual(res["staleness_workdays"], 2)
            self.assertEqual(res["original_date"], "2026-05-12")


if __name__ == "__main__":
    unittest.main(verbosity=2)
