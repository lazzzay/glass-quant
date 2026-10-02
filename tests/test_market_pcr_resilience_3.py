"""
Unit-Tests fuer V7.9.31 MARKET-PCR-RESILIENCE-3 (KW21-vorgezogen, 2026-05-21).

Adressiert die 9-Tage-down-Streak (KW20-21) in dem alle 3 Resolver-Stages
(CBOE-JSON, yfinance-ETF, Cache-Last-Good) gleichzeitig nicht liefern.
Fix: 2 neue Stages eingefuegt, yfinance Index ^SPX/^VIX + CBOE HTML-Scrape.
Plus Cache-Window von 2 auf 5 Werktage erweitert.

Deckt:
  T01 _parse_cboe_html_pcr: escaped JSON (\\\"name\\\":...) wird geparst
  T02 _parse_cboe_html_pcr: plain JSON ("name":...) wird auch geparst
  T03 _parse_cboe_html_pcr: leeres HTML -> _parse_error
  T04 _parse_cboe_html_pcr: ungueltige Werte (>10.0 oder <0.10) gefiltert
  T05 _parse_cboe_html_pcr: alle 6 Ratio-Typen (TOTAL/EQUITY/INDEX/ETP/VIX/SPX)
  T06 _parse_cboe_html_pcr: selectedDate wird extrahiert (escaped + plain)
  T07 _fetch_market_pcr_cboe_html: 400-Bytes Response -> unavailable
  T08 _fetch_market_pcr_cboe_html: gemockt HTML -> ok + korrekt geparst
  T09 _fetch_market_pcr_spx_index: kein OI -> unavailable
  T10 fetch_market_pcr Resolver-Chain: CBOE-JSON tot -> yf-ETF tot -> SPX ok
  T11 fetch_market_pcr Resolver-Chain: alle live tot -> CBOE-HTML ok
  T12 fetch_market_pcr Resolver-Chain: alle live tot, HTML tot -> Cache greift
  T13 fetch_market_pcr Resolver-Chain: ALLE 5 tot -> all_sources_down
  T14 Default cache_max_age_workdays = 5 (vorher 2)
  T15 Default _fetch_market_pcr_lastgood_cache max_age_workdays = 5

Run:   python3 -m unittest tests.test_market_pcr_resilience_3 -v
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import options_flow as of_mod  # noqa: E402


# Sample HTML payloads
_HTML_ESCAPED = (
    'foo bar prefix... "data\\":{\\"ratios\\":['
    '{\\"name\\":\\"TOTAL PUT/CALL RATIO\\",\\"value\\":\\"0.75\\"},'
    '{\\"name\\":\\"INDEX PUT/CALL RATIO\\",\\"value\\":\\"0.77\\"},'
    '{\\"name\\":\\"EQUITY PUT/CALL RATIO\\",\\"value\\":\\"0.49\\"},'
    '{\\"name\\":\\"EXCHANGE TRADED PRODUCTS PUT/CALL RATIO\\",\\"value\\":\\"1.18\\"},'
    '{\\"name\\":\\"CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO\\",\\"value\\":\\"0.19\\"},'
    '{\\"name\\":\\"SPX + SPXW PUT/CALL RATIO\\",\\"value\\":\\"1.00\\"}],'
    '\\"selectedDate\\":\\"2026-05-20\\"}'
    + 'x' * 600  # padding to clear 500-byte threshold
)

_HTML_PLAIN = (
    'foo bar prefix... {"data":{"ratios":['
    '{"name":"TOTAL PUT/CALL RATIO","value":"0.95"},'
    '{"name":"EQUITY PUT/CALL RATIO","value":"0.55"},'
    '{"name":"INDEX PUT/CALL RATIO","value":"1.30"}],'
    '"selectedDate":"2026-05-21"}}'
    + 'x' * 600
)

_HTML_INVALID_VALUE = (
    'foo... {"name":"TOTAL PUT/CALL RATIO","value":"15.0"},'  # > 10.0 -> skip
    '{"name":"EQUITY PUT/CALL RATIO","value":"0.05"},'  # < 0.10 -> skip
    '{"name":"INDEX PUT/CALL RATIO","value":"0.85"}'
    + 'x' * 600
)


class TestParseCboeHtml(unittest.TestCase):
    def test_T01_escaped_json_parses(self):
        r = of_mod._parse_cboe_html_pcr(_HTML_ESCAPED)
        self.assertNotIn("_parse_error", r)
        self.assertEqual(r["total"], 0.75)
        self.assertEqual(r["index"], 0.77)
        self.assertEqual(r["equity"], 0.49)
        self.assertEqual(r["etp"], 1.18)
        self.assertEqual(r["vix"], 0.19)
        self.assertEqual(r["spx"], 1.00)

    def test_T02_plain_json_parses(self):
        r = of_mod._parse_cboe_html_pcr(_HTML_PLAIN)
        self.assertEqual(r["total"], 0.95)
        self.assertEqual(r["equity"], 0.55)
        self.assertEqual(r["index"], 1.30)

    def test_T03_empty_html_returns_parse_error(self):
        r = of_mod._parse_cboe_html_pcr("<html><body>nothing here</body></html>")
        self.assertIn("_parse_error", r)

    def test_T04_invalid_values_filtered(self):
        r = of_mod._parse_cboe_html_pcr(_HTML_INVALID_VALUE)
        # TOTAL (15.0) und EQUITY (0.05) muessen gefiltert sein
        self.assertIsNone(r.get("total"))
        self.assertIsNone(r.get("equity"))
        self.assertEqual(r["index"], 0.85)

    def test_T05_all_six_ratio_types_extracted(self):
        r = of_mod._parse_cboe_html_pcr(_HTML_ESCAPED)
        self.assertIsNotNone(r["total"])
        self.assertIsNotNone(r["equity"])
        self.assertIsNotNone(r["index"])
        self.assertIsNotNone(r["etp"])
        self.assertIsNotNone(r["vix"])
        self.assertIsNotNone(r["spx"])
        self.assertEqual(r["n_ratios_parsed"], 6)

    def test_T06_selected_date_extracted(self):
        r1 = of_mod._parse_cboe_html_pcr(_HTML_ESCAPED)
        r2 = of_mod._parse_cboe_html_pcr(_HTML_PLAIN)
        self.assertEqual(r1["selected_date"], "2026-05-20")
        self.assertEqual(r2["selected_date"], "2026-05-21")


class TestFetchCboeHtml(unittest.TestCase):
    def _mock_urlopen(self, body: str):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.read.return_value = body.encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_T07_short_response_unavailable(self):
        with patch("urllib.request.urlopen",
                   return_value=self._mock_urlopen("<html>" + "x" * 50 + "</html>")):
            r = of_mod._fetch_market_pcr_cboe_html()
        self.assertFalse(r["ok"])
        self.assertIn("html too short", r["error"].lower())

    def test_T08_mock_html_ok_and_parsed(self):
        with patch("urllib.request.urlopen",
                   return_value=self._mock_urlopen(_HTML_ESCAPED)):
            r = of_mod._fetch_market_pcr_cboe_html()
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "cboe_html_scrape")
        self.assertEqual(r["total"], 0.75)
        self.assertEqual(r["tag"], "neutral")  # 0.70 < 0.75 < 1.20
        self.assertEqual(r["components"]["selected_date"], "2026-05-20")
        self.assertEqual(r["components"]["n_ratios_parsed"], 6)


class TestSpxIndexResolver(unittest.TestCase):
    def test_T09_spx_no_oi_returns_unavailable(self):
        # _yf_ticker liefert ticker mit options=[] -> no usable expiry
        from unittest.mock import MagicMock
        mock_tk = MagicMock()
        mock_tk.options = []
        with patch.object(of_mod, "_yf_ticker", return_value=mock_tk):
            r = of_mod._fetch_market_pcr_spx_index()
        self.assertFalse(r["ok"])
        self.assertEqual(r["source"], "yfinance_index_unavailable")


class TestResolverChain(unittest.TestCase):
    def _patch_all(self, cboe_ok=False, etf_ok=False, spx_ok=False, html_ok=False,
                    cboe_total=0.95, etf_total=0.85, spx_total=0.75, html_total=0.65):
        """Helper: patcht alle 4 Live-Resolver mit jeweils ok/nicht-ok-Response."""
        def make(ok, total, src):
            if ok:
                return {"ok": True, "total": total, "equity": None, "index": None,
                        "tag": of_mod._pcr_tag(total), "source": src}
            return {"ok": False, "error": f"{src}_failed", "source": src}

        return [
            patch.object(of_mod, "_fetch_market_pcr_cboe",
                         return_value=make(cboe_ok, cboe_total, "cboe")),
            patch.object(of_mod, "_fetch_market_pcr_yfinance_fallback",
                         return_value=make(etf_ok, etf_total, "yfinance_fallback")),
            patch.object(of_mod, "_fetch_market_pcr_spx_index",
                         return_value=make(spx_ok, spx_total, "yfinance_index_spx")),
            patch.object(of_mod, "_fetch_market_pcr_cboe_html",
                         return_value=make(html_ok, html_total, "cboe_html_scrape")),
        ]

    def test_T10_chain_cboe_dead_etf_dead_spx_ok(self):
        patches = self._patch_all(cboe_ok=False, etf_ok=False, spx_ok=True)
        for p in patches:
            p.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = of_mod.fetch_market_pcr(today="2026-05-21", tools_dir=tmp)
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "yfinance_index_spx")
        self.assertEqual(r["total"], 0.75)
        self.assertIn("cboe_error", r)
        self.assertIn("yfinance_etf_error", r)

    def test_T11_chain_all_yf_dead_html_ok(self):
        patches = self._patch_all(cboe_ok=False, etf_ok=False, spx_ok=False, html_ok=True)
        for p in patches:
            p.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = of_mod.fetch_market_pcr(today="2026-05-21", tools_dir=tmp)
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "cboe_html_scrape")
        self.assertEqual(r["total"], 0.65)

    def test_T12_chain_all_live_dead_cache_5d(self):
        patches = self._patch_all(cboe_ok=False, etf_ok=False, spx_ok=False, html_ok=False)
        for p in patches:
            p.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # Werktag 5 zurueck = 2026-05-14 (Do)
                with open(os.path.join(tmp, "options_flow_2026-05-14.json"), "w") as f:
                    json.dump({
                        "date": "2026-05-14",
                        "market_pcr": {"ok": True, "total": 1.05, "source": "cboe",
                                       "tag": "neutral"},
                    }, f)
                r = of_mod.fetch_market_pcr(today="2026-05-21", tools_dir=tmp)
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "cache_lastknown")
        self.assertEqual(r["total"], 1.05)
        self.assertIn("cboe_html_error", r)

    def test_T13_chain_all_dead_returns_all_sources_down(self):
        patches = self._patch_all(cboe_ok=False, etf_ok=False, spx_ok=False, html_ok=False)
        for p in patches:
            p.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # Kein Cache-Snapshot
                r = of_mod.fetch_market_pcr(today="2026-05-21", tools_dir=tmp)
        finally:
            for p in patches:
                p.stop()
        self.assertFalse(r["ok"])
        self.assertEqual(r["source"], "all_sources_down")


class TestDefaults(unittest.TestCase):
    def test_T14_fetch_market_pcr_default_cache_window_is_5(self):
        sig = inspect.signature(of_mod.fetch_market_pcr)
        self.assertEqual(sig.parameters["cache_max_age_workdays"].default, 5)

    def test_T15_lastgood_default_max_age_is_5(self):
        sig = inspect.signature(of_mod._fetch_market_pcr_lastgood_cache)
        self.assertEqual(sig.parameters["max_age_workdays"].default, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
