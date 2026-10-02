"""
Smoke-Tests V7.9.12 UNIVERSE-COVERAGE-DIAGNOSTIC (FEATURE, KW18 Mo-Abend).

Deckt:
  T1  Voll-Coverage (alle Universe-Tickers im scan)        -> GREEN, pct=1.0
  T2  Halbe Coverage 50.0%                                  -> RED
  T3  20% Coverage                                          -> BLACK
  T4  85% Coverage                                          -> YELLOW
  T5  Gap-Cluster werden korrekt erkannt (Block >= 6)
  T6  Tier/Region/Asset-Class-Aufriss konsistent
  T7  ticker_map als Zusatzquelle wird mitgezaehlt
  T8  CLI mit --no-write + Live-Daten 2026-04-27 -> RED, ~52%
  T9  --strict + RED -> exit 2 ; ohne --strict -> exit 0
  T10 --threshold harter Cutoff erzwingt exit 2
  T11 Markdown-Renderer enthaelt alle Sektionen + Severity-Emoji
  T12 missing_first_25 ist alphabetisch und cap=25

Reine stdlib (unittest, tempfile, json, csv).
Run:  python3 -m unittest tests.test_universe_coverage_audit -v
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from tools import universe_coverage_audit as uca  # noqa: E402


def _write_universe(path: Path, tickers, tier="B", region="US", asset_class="us_large"):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "tier", "asset_class", "region", "sector"])
        for t in tickers:
            w.writerow([t, t, tier, asset_class, region, "Tech"])


def _write_universe_mixed(path: Path, rows):
    """rows: list of dicts with ticker, tier, region, asset_class."""
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "tier", "asset_class", "region", "sector"])
        for r in rows:
            w.writerow([
                r["ticker"], r["ticker"], r.get("tier", "B"),
                r.get("asset_class", "us_large"), r.get("region", "US"), "Tech",
            ])


def _write_scan(path: Path, top_tickers, ticker_map_keys=None, **extra):
    payload = {
        "scan_date": "2026-04-27",
        "version": "v6.8",
        "universe_size": len(set(top_tickers) | set(ticker_map_keys or [])),
        "top_setups": [{"ticker": t} for t in top_tickers],
        "ticker_map": {k: {} for k in (ticker_map_keys or [])},
    }
    payload.update(extra)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f)


class TestCoverageScenarios(unittest.TestCase):
    def test_t1_full_coverage_green(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(50)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30], ticker_map_keys=tickers)
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(rep.severity, "GREEN")
            self.assertEqual(rep.coverage_pct, 1.0)
            self.assertEqual(rep.missing_total, 0)

    def test_t2_50pct_red(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(100)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30], ticker_map_keys=tickers[:50])
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(rep.severity, "RED")
            self.assertAlmostEqual(rep.coverage_pct, 0.50, places=4)

    def test_t3_20pct_black(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(100)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:10], ticker_map_keys=tickers[:20])
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(rep.severity, "BLACK")
            self.assertAlmostEqual(rep.coverage_pct, 0.20, places=4)

    def test_t4_85pct_yellow(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(100)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30], ticker_map_keys=tickers[:85])
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(rep.severity, "YELLOW")
            self.assertAlmostEqual(rep.coverage_pct, 0.85, places=4)


class TestGapClusters(unittest.TestCase):
    def test_t5_gap_cluster_detection(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            # Universe sortiert alphabetisch: A001..A100
            # Wir lassen A050..A059 fehlen (10 in Folge) und A005..A006 (zu klein, <6)
            all_tickers = [f"A{i:03d}" for i in range(100)]
            missing = set([f"A{i:03d}" for i in range(50, 60)]) | {"A005", "A006"}
            scanned = [t for t in all_tickers if t not in missing]
            _write_universe(uni_p, all_tickers)
            _write_scan(scan_p, top_tickers=scanned[:30], ticker_map_keys=scanned)
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(len(rep.gap_clusters), 1)
            cluster = rep.gap_clusters[0]
            self.assertEqual(cluster["first"], "A050")
            self.assertEqual(cluster["last"], "A059")
            self.assertEqual(cluster["size"], 10)


class TestBreakdowns(unittest.TestCase):
    def test_t6_dimensions_consistent(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            rows = [
                {"ticker": "AAA", "tier": "A", "region": "US", "asset_class": "us_large"},
                {"ticker": "BBB", "tier": "A", "region": "DE", "asset_class": "dax"},
                {"ticker": "CCC", "tier": "B", "region": "US", "asset_class": "us_small"},
                {"ticker": "DDD", "tier": "C", "region": "Global", "asset_class": "crypto"},
            ]
            _write_universe_mixed(uni_p, rows)
            # Nur AAA + BBB werden gescannt
            _write_scan(scan_p, top_tickers=["AAA"], ticker_map_keys=["AAA", "BBB"])
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            # Tier A komplett (2/2), Tier B 0/1, Tier C 0/1
            self.assertEqual(rep.by_tier["A"]["scanned"], 2)
            self.assertEqual(rep.by_tier["A"]["total"], 2)
            self.assertEqual(rep.by_tier["B"]["missing"], 1)
            self.assertEqual(rep.by_tier["C"]["missing"], 1)
            self.assertEqual(rep.by_region["US"]["scanned"], 1)
            self.assertEqual(rep.by_region["US"]["missing"], 1)
            self.assertEqual(rep.by_asset_class["crypto"]["missing"], 1)

    def test_t7_ticker_map_counted(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = ["A", "B", "C", "D", "E"]
            _write_universe(uni_p, tickers)
            # Nur ticker_map befuellt, top_setups leer -> trotzdem als gescannt zaehlen
            _write_scan(scan_p, top_tickers=[], ticker_map_keys=["A", "B", "C", "D"])
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(rep.scanned_total, 4)
            self.assertEqual(rep.missing_total, 1)


class TestCLI(unittest.TestCase):
    def test_t8_cli_no_write_runs_clean(self):
        # Prepare workspace
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(100)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30], ticker_map_keys=tickers[:50])
            argv = [
                "--date", "2026-04-27",
                "--scan-dir", str(td),
                "--universe", str(uni_p),
                "--out-dir", str(td),
                "--briefings-dir", str(td),
                "--no-write",
            ]
            rc = uca.main(argv)
            self.assertEqual(rc, 0)  # Default: RED erlaubt rc=0

    def test_t9_strict_red_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(100)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30], ticker_map_keys=tickers[:50])
            argv = [
                "--date", "2026-04-27",
                "--scan-dir", str(td),
                "--universe", str(uni_p),
                "--out-dir", str(td),
                "--briefings-dir", str(td),
                "--no-write",
                "--strict",
            ]
            rc = uca.main(argv)
            self.assertEqual(rc, 2)

    def test_t10_threshold_hard_cut(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(100)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30], ticker_map_keys=tickers[:90])
            # 90% Coverage -> YELLOW; mit threshold 0.95 muss exit 2 kommen
            argv = [
                "--date", "2026-04-27",
                "--scan-dir", str(td),
                "--universe", str(uni_p),
                "--out-dir", str(td),
                "--briefings-dir", str(td),
                "--no-write",
                "--threshold", "0.95",
            ]
            rc = uca.main(argv)
            self.assertEqual(rc, 2)


class TestMarkdown(unittest.TestCase):
    def test_t11_markdown_sections_and_emoji(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            tickers = [f"T{i:03d}" for i in range(50)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=tickers[:30],
                        ticker_map_keys=tickers[:30])  # 60% -> RED
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            md = uca.render_markdown(rep)
            self.assertIn("Universe Coverage Audit 2026-04-27", md)
            self.assertIn("## 1. Aufriss nach Tier", md)
            self.assertIn("## 2. Aufriss nach Region", md)
            self.assertIn("## 3. Aufriss nach Asset-Class", md)
            self.assertIn("## 4. Erste", md)
            self.assertIn("## 5. Gap-Cluster", md)
            self.assertIn("## 6. Notes", md)
            self.assertIn("RED", md)
            self.assertIn("🔴", md)


class TestMissingCap(unittest.TestCase):
    def test_t12_missing_first_25_alphabetical_capped(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            uni_p = td / "universe.csv"
            scan_p = td / "scan_2026-04-27.json"
            # 60 Tickers, alle fehlen -> first_25 muss alphabetisch sortiert sein, len=25
            tickers = [f"X{i:03d}" for i in range(60)]
            _write_universe(uni_p, tickers)
            _write_scan(scan_p, top_tickers=[], ticker_map_keys=[])
            uni = uca.load_universe(uni_p)
            scan = uca.load_scan(scan_p)
            rep = uca.build_report(uni, scan, scan_date="2026-04-27")
            self.assertEqual(len(rep.missing_first_25), 25)
            # alphabetisch
            self.assertEqual(rep.missing_first_25,
                             sorted(rep.missing_first_25))
            self.assertEqual(rep.missing_first_25[0], "X000")
            self.assertEqual(rep.missing_first_25[-1], "X024")


if __name__ == "__main__":
    unittest.main(verbosity=2)
