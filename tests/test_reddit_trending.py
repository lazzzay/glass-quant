#!/usr/bin/env python3
"""Unit-Tests fuer signals/reddit_trending.py (Q1, KW17 Tag 4).

Vier Pflicht-Szenarien gemaess Rollout-Plan KW17:
    1. Ticker-Extraktion aus Post-Title (Cashtag + Plain + Blacklist)
    2. Comment-Aggregation mit halb gewichteten Upvotes
    3. Universe-Filter (Ticker ausserhalb Tier-A/B wird gedroppt)
    4. Velocity-Ranking (cashtag-bias + upvote-bias)

Free-Stack: stdlib ``unittest``, keine Netzwerk-Calls.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from signals import reddit_trending as rt  # noqa: E402


class TickerExtractionTests(unittest.TestCase):
    universe = frozenset({"NVDA", "TSLA", "AAPL", "AMD", "SPY", "META"})

    def test_cashtag_extraction(self):
        found = rt.extract_tickers("$NVDA to the moon", self.universe)
        self.assertIn(("NVDA", True), found)

    def test_plain_ticker_extraction(self):
        found = rt.extract_tickers("I like TSLA and META here", self.universe)
        tickers = {t for t, _ in found}
        self.assertEqual(tickers, {"TSLA", "META"})
        # beide als plain gezaehlt
        self.assertTrue(all(not cash for _, cash in found))

    def test_blacklist_drops_noise(self):
        # "A" ist echter Ticker (Agilent), steht aber in HARD_BLACKLIST
        found = rt.extract_tickers("The CEO said A is undervalued",
                                   self.universe | {"A"})
        tickers = {t for t, _ in found}
        self.assertNotIn("A", tickers)
        self.assertNotIn("CEO", tickers)

    def test_universe_filter(self):
        # "ZZZZ" ist nicht im Universe -> wird gedroppt, auch als Cashtag
        found = rt.extract_tickers("$ZZZZ is mooning", self.universe)
        self.assertEqual(found, [])

    def test_cashtag_and_plain_not_double_counted(self):
        # "$NVDA and NVDA again" soll NVDA nur einmal (als cashtag) zaehlen
        found = rt.extract_tickers("$NVDA and NVDA again", self.universe)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0], ("NVDA", True))


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.universe = {"NVDA", "TSLA", "AAPL", "AMD"}
        self.now = datetime.now(timezone.utc)

    def _post(self, **kw):
        base = {
            "subreddit": "wallstreetbets",
            "id": "p1",
            "title": "",
            "selftext": "",
            "url": "https://reddit.com/r/wsb/p1",
            "created_utc": (self.now - timedelta(hours=3)).timestamp(),
            "ups": 100,
            "comments": [],
        }
        base.update(kw)
        return base

    def test_comment_aggregation_weights_upvotes(self):
        # Ticker NUR in Kommentaren: Post-Upvotes fliessen nicht in
        # upvote_sum ein (Post ist fuer NVDA kein "unique_post").
        post = self._post(
            title="general market talk",
            comments=[
                {"body": "NVDA calls printing", "ups": 200},
                {"body": "more NVDA", "ups": 80},
            ],
        )
        agg = rt.aggregate_mentions([post], self.universe, self.now, 24)
        nvda = agg["NVDA"]
        self.assertEqual(nvda.mentions, 2)
        # Comment-Upvotes werden halbiert (integer-floor): 200//2 + 80//2 = 140
        self.assertEqual(nvda.upvote_sum, 140)
        # Unique-Posts bleibt 0, weil Ticker nur in Kommentaren war
        self.assertEqual(nvda.unique_posts, 0)

    def test_post_upvotes_counted_only_when_ticker_in_post_body(self):
        post = self._post(
            title="$NVDA breakout", ups=500,
            comments=[
                {"body": "NVDA calls printing", "ups": 200},
            ],
        )
        agg = rt.aggregate_mentions([post], self.universe, self.now, 24)
        nvda = agg["NVDA"]
        # Post-Upvotes (500) + Comment-Upvotes halbiert (100) = 600
        self.assertEqual(nvda.upvote_sum, 600)
        self.assertEqual(nvda.unique_posts, 1)

    def test_universe_filter_integration(self):
        post = self._post(
            title="$GME is ripping",  # GME nicht im Universe
            selftext="AMD also looking strong",
        )
        agg = rt.aggregate_mentions([post], self.universe, self.now, 24)
        self.assertNotIn("GME", agg)
        self.assertIn("AMD", agg)

    def test_velocity_ranking_prefers_cashtag_and_upvotes(self):
        p1 = self._post(
            id="p1", title="$NVDA $NVDA $NVDA", ups=500,
        )
        p2 = self._post(
            id="p2", title="TSLA TSLA TSLA", ups=50,
        )
        agg = rt.aggregate_mentions([p1, p2], self.universe, self.now, 24)
        # Beide Ticker haben gleich viele Mentions, aber NVDA:
        #   - ist Cashtag (Bonus 0.5)
        #   - hat 10x mehr Upvotes
        # -> NVDA muss hoeheren velocity_score haben
        top = rt.top_n_summary(agg, 5)
        self.assertEqual(top[0]["ticker"], "NVDA")
        # Sanity: beide im Output
        tickers = [t["ticker"] for t in top]
        self.assertIn("TSLA", tickers)
        self.assertGreater(
            agg["NVDA"].velocity_score, agg["TSLA"].velocity_score,
        )

    def test_unique_posts_counted_once_per_ticker_post_pair(self):
        # Ticker kann in title UND selftext desselben Posts auftauchen,
        # aber unique_posts zaehlt nur einmal
        post = self._post(
            title="$AMD setup",
            selftext="AMD looks bullish after RDNA4",
        )
        agg = rt.aggregate_mentions([post], self.universe, self.now, 24)
        self.assertEqual(agg["AMD"].mentions, 2)  # 2 Fundstellen
        self.assertEqual(agg["AMD"].unique_posts, 1)  # aber 1 Post
        self.assertEqual(agg["AMD"].upvote_sum, 100)  # Post-Upvotes nur 1x


class BriefingLineTests(unittest.TestCase):
    def test_briefing_line_with_data(self):
        top = [
            {"ticker": "NVDA", "mentions": 12, "velocity_score": 3.45},
            {"ticker": "TSLA", "mentions": 7, "velocity_score": 1.21},
        ]
        line = rt._format_briefing_line(top, "2026-04-24")
        self.assertIn("$NVDA", line)
        self.assertIn("12x", line)
        self.assertIn("Q1-Shadow", line)

    def test_briefing_line_empty(self):
        line = rt._format_briefing_line([], "2026-04-24")
        self.assertIn("keine Mentions", line)
        self.assertIn("2026-04-24", line)


class UniverseLoaderTests(unittest.TestCase):
    """Smoke-Test gegen die echte tools/universe.csv."""

    def test_universe_loader_returns_plain_tickers_only(self):
        csv_path = PROJECT_ROOT / "tools" / "universe.csv"
        if not csv_path.exists():
            self.skipTest("universe.csv nicht vorhanden")
        uni = rt.load_target_universe(csv_path)
        self.assertGreater(len(uni), 50, "universe sollte >50 Tier-A/B-Ticker liefern")
        # Alle Tickers muessen dem Plain-Pattern entsprechen
        for t in list(uni)[:50]:
            self.assertRegex(t, r"^[A-Z]{1,5}$")
        # Kein Exchange-Suffix
        self.assertFalse(any("." in t for t in uni))


class OutputWriteTests(unittest.TestCase):
    def test_write_outputs_produces_shadow_json_and_briefing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shadow = tmp_path / "shadow_signals"
            briefings = tmp_path / "Briefings"
            top = [
                {"ticker": "NVDA", "mentions": 5, "velocity_score": 2.1,
                 "cashtag_mentions": 3, "unique_posts": 2,
                 "upvote_sum": 300, "subreddits": {"wallstreetbets": 2},
                 "sample_posts": []},
            ]
            as_of = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
            out, brief, annot = rt.write_outputs(
                top, {"NVDA": None}, as_of,
                ("wallstreetbets",), 24, "public_json", 1,
                shadow_dir=shadow, briefings_dir=briefings,
            )
            self.assertTrue(out.exists())
            self.assertTrue(brief.exists())
            self.assertTrue(annot.exists())
            data = json.loads(out.read_text())
            self.assertEqual(data["schema_version"], rt.SCHEMA_VERSION)
            self.assertEqual(data["top_tickers"][0]["ticker"], "NVDA")
            self.assertEqual(data["source_mode"], "public_json")
            # Briefing-Snippet enthaelt Ticker
            self.assertIn("NVDA", brief.read_text())
            # latest-Mirror existiert ebenfalls
            self.assertTrue((briefings / "reddit_trending_line_latest.md").exists())
            # Scanner-Annotation-Sidecar: retail_mention_rank=1 fuer NVDA
            adata = json.loads(annot.read_text())
            self.assertIn("NVDA", adata["tickers"])
            self.assertEqual(adata["tickers"]["NVDA"]["retail_mention_rank"], 1)
            self.assertEqual(adata["source"], "reddit_trending")
            # latest-Mirror fuer Dashboard-Pull
            self.assertTrue((shadow / "retail_annotation_latest.json").exists())


if __name__ == "__main__":
    unittest.main()
