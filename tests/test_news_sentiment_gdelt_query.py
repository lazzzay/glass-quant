"""
Unit-Tests fuer NEWS-SENTIMENT-GDELT-QUERY-1 (KW21-vorgezogen, 2026-05-21).

Adressiert die 4-Tage-BLACK-Streak (KW21 Tag 1-4) seit 2026-05-18, wo GDELT
auf Single-Word-Phrases wie `"AAPL"` mit HTTP 200 + leerem Body antwortet
('phrase too short' Reject: GDELT verlangt Phrasen aus mindestens zwei Woertern).
Fix: 2-Wort-Phrasen als Default + 3-Variant-Retry-Loop + Cache-Last-Good Stage 3.

Deckt:
  T01 _http_get_json: HTTP 200 + leerer Body -> _EMPTY_PAYLOAD-Sentinel (nicht None)
  T02 _http_get_json: HTTP 200 + Body < 50 Bytes -> _EMPTY_PAYLOAD
  T03 _http_get_json: HTTP 200 + valides JSON -> dict
  T04 _http_get_json: HTTP 403 -> None (echter Netz-Fail)
  T05 _build_gdelt_query: phrase2 baut 2-Wort-Phrase
  T06 _build_gdelt_query: single baut Legacy-Single-Phrase
  T07 _fetch_gdelt_once: _EMPTY_PAYLOAD -> status='empty_payload'
  T08 _fetch_gdelt_once: valide articles -> status='ok'
  T09 _fetch_gdelt_once: valides JSON ohne articles -> status='no_articles'
  T10 fetch_gdelt_articles_with_status: 3-Variant-Retry-Loop (phrase2 empty -> phrase3 ok)
  T11 fetch_gdelt_articles_with_status: http_fail -> kein Retry
  T12 _business_days_between: Mo-Fr werden gezaehlt, Sa/So nicht
  T13 _load_cache_last_good: liefert ticker_map vom letzten valid Snapshot
  T14 _load_cache_last_good: skipt fail_soft=True Snapshots
  T15 _load_cache_last_good: respektiert max_business_days (zu alt -> None)
  T16 _load_cache_last_good: keine Snapshots -> None
  T17 fetch_sentiment_for_tickers: cache_hit bei empty_payload + valid Cache
  T18 fetch_sentiment_for_tickers: method='no_data' bei no_articles ohne Cache

Run:   python3 -m unittest tests.test_news_sentiment_gdelt_query -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from tools import news_sentiment_fetch as nsf  # noqa: E402


def _mock_urlopen(body: bytes | str, status: int = 200):
    """Erzeugt einen Context-Manager-Mock fuer urllib.request.urlopen."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestHttpGetJson(unittest.TestCase):
    def test_T01_empty_body_returns_empty_payload(self):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen("")):
            result = nsf._http_get_json("https://x/y")
        self.assertIs(result, nsf._EMPTY_PAYLOAD)

    def test_T02_short_body_returns_empty_payload(self):
        # < 50 Bytes (the threshold)
        with patch("urllib.request.urlopen", return_value=_mock_urlopen('{"a":1}')):
            result = nsf._http_get_json("https://x/y")
        self.assertIs(result, nsf._EMPTY_PAYLOAD)

    def test_T03_valid_json_returns_dict(self):
        big = json.dumps({"articles": [{"title": "Hello World" * 10}], "count": 1})
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(big)):
            result = nsf._http_get_json("https://x/y")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["count"], 1)

    def test_T04_http_error_returns_none(self):
        err = urllib.error.HTTPError("https://x/y", 403, "Forbidden", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            result = nsf._http_get_json("https://x/y")
        self.assertIsNone(result)


class TestBuildQuery(unittest.TestCase):
    def test_T05_phrase2_2word(self):
        q = nsf._build_gdelt_query("AAPL", "phrase2")
        self.assertIn('"AAPL stock"', q)
        self.assertIn('"AAPL shares"', q)
        self.assertIn("sourcelang:english", q)
        # GDELT-Syntax-Pflicht: OR-Phrasen in Klammern
        self.assertIn("(", q)
        self.assertIn(")", q)

    def test_T06_single_legacy(self):
        q = nsf._build_gdelt_query("AAPL", "single")
        self.assertEqual(q, '"AAPL" sourcelang:english')
        # Single hat keine OR-Klammern noetig (keine OR-Phrase)
        self.assertNotIn("(", q)

    def test_T05b_phrase3_3word(self):
        q = nsf._build_gdelt_query("AAPL", "phrase3")
        self.assertIn('"AAPL earnings"', q)
        self.assertIn("(", q)
        self.assertIn(")", q)


class TestFetchOnce(unittest.TestCase):
    def test_T07_empty_payload_status(self):
        with patch.object(nsf, "_http_get_json", return_value=nsf._EMPTY_PAYLOAD):
            articles, status = nsf._fetch_gdelt_once("AAPL", "phrase2", 3, 30)
        self.assertEqual(status, "empty_payload")
        self.assertIsNone(articles)

    def test_T08_ok_with_articles(self):
        payload = {"articles": [{"title": "Apple climbs on AI news"}]}
        with patch.object(nsf, "_http_get_json", return_value=payload):
            articles, status = nsf._fetch_gdelt_once("AAPL", "phrase2", 3, 30)
        self.assertEqual(status, "ok")
        self.assertEqual(len(articles), 1)

    def test_T09_no_articles_status(self):
        payload = {"articles": [], "count": 0}
        with patch.object(nsf, "_http_get_json", return_value=payload):
            articles, status = nsf._fetch_gdelt_once("AAPL", "phrase2", 3, 30)
        self.assertEqual(status, "no_articles")
        self.assertEqual(articles, [])

    def test_T09b_http_fail_status(self):
        with patch.object(nsf, "_http_get_json", return_value=None):
            articles, status = nsf._fetch_gdelt_once("AAPL", "phrase2", 3, 30)
        self.assertEqual(status, "http_fail")
        self.assertIsNone(articles)


class TestRetryLoop(unittest.TestCase):
    def test_T10_phrase2_empty_then_phrase3_ok(self):
        # phrase2 -> empty, phrase3 -> ok, single never called
        call_log = []

        def fake_once(ticker, variant, lookback_days, max_articles):
            call_log.append(variant)
            if variant == "phrase2":
                return (None, "empty_payload")
            if variant == "phrase3":
                return ([{"title": "AAPL stock jumps"}], "ok")
            return ([], "single_should_not_run")

        with patch.object(nsf, "_fetch_gdelt_once", side_effect=fake_once):
            articles, status = nsf.fetch_gdelt_articles_with_status("AAPL")

        self.assertEqual(status, "ok")
        self.assertEqual(len(articles), 1)
        self.assertEqual(call_log, ["phrase2", "phrase3"])  # nicht 'single'

    def test_T11_http_fail_no_retry(self):
        call_log = []

        def fake_once(ticker, variant, lookback_days, max_articles):
            call_log.append(variant)
            return (None, "http_fail")

        with patch.object(nsf, "_fetch_gdelt_once", side_effect=fake_once):
            articles, status = nsf.fetch_gdelt_articles_with_status("AAPL")

        self.assertEqual(status, "http_fail")
        self.assertEqual(articles, [])
        self.assertEqual(call_log, ["phrase2"])  # KEIN Retry bei Netz-Problem

    def test_T11b_all_three_variants_tried_when_all_empty(self):
        call_log = []

        def fake_once(ticker, variant, lookback_days, max_articles):
            call_log.append(variant)
            return (None, "empty_payload")

        with patch.object(nsf, "_fetch_gdelt_once", side_effect=fake_once):
            articles, status = nsf.fetch_gdelt_articles_with_status("AAPL")

        self.assertEqual(call_log, ["phrase2", "phrase3", "single"])
        self.assertEqual(status, "empty_payload")


class TestBusinessDays(unittest.TestCase):
    def test_T12_skips_weekend(self):
        # Fr 2026-05-15 -> Mo 2026-05-18 = 1 Werktag (Mo)
        d1 = datetime(2026, 5, 15)  # Fr
        d2 = datetime(2026, 5, 18)  # Mo
        self.assertEqual(nsf._business_days_between(d1, d2), 1)

    def test_T12b_counts_inclusive_mid(self):
        # Mo 2026-05-18 -> Fr 2026-05-22 = 4 Werktage (Di/Mi/Do/Fr)
        d1 = datetime(2026, 5, 18)
        d2 = datetime(2026, 5, 22)
        self.assertEqual(nsf._business_days_between(d1, d2), 4)

    def test_T12c_same_day_zero(self):
        d = datetime(2026, 5, 21)
        self.assertEqual(nsf._business_days_between(d, d), 0)


class TestCacheLastGood(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tools_patch = patch.object(nsf, "TOOLS_DIR", Path(self.tmpdir))
        self.tools_patch.start()

    def tearDown(self):
        self.tools_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_snapshot(self, date_str: str, payload: dict):
        p = Path(self.tmpdir) / f"news_sentiment_{date_str}.json"
        with open(p, "w") as f:
            json.dump(payload, f)
        return p

    def test_T13_loads_most_recent_valid(self):
        # Today = Do 2026-05-21
        self._write_snapshot("2026-05-20", {
            "n_with_news": 5,
            "fail_soft": False,
            "ticker_map": {"AAPL": {"article_count": 3, "sentiment_score": 0.5}}
        })
        cmap, cdate, age = nsf._load_cache_last_good("2026-05-21")
        self.assertIsNotNone(cmap)
        self.assertEqual(cdate, "2026-05-20")
        self.assertEqual(age, 1)
        self.assertEqual(cmap["AAPL"]["sentiment_score"], 0.5)

    def test_T14_skips_fail_soft(self):
        self._write_snapshot("2026-05-20", {
            "fail_soft": True,
            "ticker_map": {},
        })
        self._write_snapshot("2026-05-19", {
            "n_with_news": 5,
            "fail_soft": False,
            "ticker_map": {"AAPL": {"article_count": 2, "sentiment_score": -0.3}}
        })
        cmap, cdate, _ = nsf._load_cache_last_good("2026-05-21")
        self.assertEqual(cdate, "2026-05-19")
        self.assertEqual(cmap["AAPL"]["sentiment_score"], -0.3)

    def test_T15_too_old_returns_none(self):
        # 8 Werktage zurueck = ueber max_business_days=5
        self._write_snapshot("2026-05-08", {  # Fr → 9 Werktage zu 2026-05-21
            "n_with_news": 5,
            "fail_soft": False,
            "ticker_map": {"AAPL": {"article_count": 2, "sentiment_score": 0.1}}
        })
        cmap, cdate, age = nsf._load_cache_last_good("2026-05-21")
        self.assertIsNone(cmap)

    def test_T16_no_snapshots_returns_none(self):
        cmap, cdate, age = nsf._load_cache_last_good("2026-05-21")
        self.assertIsNone(cmap)
        self.assertIsNone(cdate)

    def test_T16b_skips_n_with_news_zero(self):
        self._write_snapshot("2026-05-20", {
            "n_with_news": 0,
            "fail_soft": False,
            "ticker_map": {"AAPL": {"article_count": 0, "sentiment_score": 0.0}}
        })
        cmap, _, _ = nsf._load_cache_last_good("2026-05-21")
        self.assertIsNone(cmap)


class TestFetchSentimentIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tools_patch = patch.object(nsf, "TOOLS_DIR", Path(self.tmpdir))
        self.tools_patch.start()

    def tearDown(self):
        self.tools_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_T17_cache_hit_on_empty_payload(self):
        # Schreibe Cache von gestern mit AAPL=0.7
        p = Path(self.tmpdir) / "news_sentiment_2026-05-20.json"
        with open(p, "w") as f:
            json.dump({
                "n_with_news": 1,
                "fail_soft": False,
                "ticker_map": {"AAPL": {"article_count": 5, "sentiment_score": 0.7}}
            }, f)

        # Live-Fetch antwortet komplett empty_payload
        with patch.object(nsf, "fetch_gdelt_articles_with_status", return_value=([], "empty_payload")):
            out = nsf.fetch_sentiment_for_tickers(["AAPL"], delay=0.0, today_date="2026-05-21")

        self.assertEqual(out["AAPL"]["sentiment_method"], "cache_lastgood")
        self.assertTrue(out["AAPL"]["cache_hit"])
        self.assertEqual(out["AAPL"]["sentiment_score"], 0.7)
        self.assertEqual(out["AAPL"]["cache_date"], "2026-05-20")

    def test_T18_no_data_when_no_articles_and_no_cache(self):
        with patch.object(nsf, "fetch_gdelt_articles_with_status", return_value=([], "no_articles")):
            out = nsf.fetch_sentiment_for_tickers(["XYZNEW"], delay=0.0, today_date="2026-05-21")

        self.assertEqual(out["XYZNEW"]["sentiment_method"], "no_data")
        self.assertFalse(out["XYZNEW"]["cache_hit"])
        self.assertEqual(out["XYZNEW"]["sentiment_score"], 0.0)

    def test_T18b_live_articles_use_keyword_when_no_llm(self):
        articles = [
            {"title": "Apple stock surges on strong earnings", "url": "u1", "domain": "d1"},
            {"title": "AAPL beats estimates, raises guidance", "url": "u2", "domain": "d2"},
        ]
        with patch.object(nsf, "fetch_gdelt_articles_with_status", return_value=(articles, "ok")):
            out = nsf.fetch_sentiment_for_tickers(["AAPL"], delay=0.0, today_date="2026-05-21", use_llm=False)

        self.assertEqual(out["AAPL"]["sentiment_method"], "keyword")
        self.assertGreater(out["AAPL"]["sentiment_score"], 0)  # bullish keywords drin
        self.assertEqual(out["AAPL"]["article_count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
