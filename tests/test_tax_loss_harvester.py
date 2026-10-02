#!/usr/bin/env python3
"""Unit-Tests für tools/tax_loss_harvester.py (X-1).

Drei Pflicht-Szenarien gemäß Rollout-Plan KW17:
    1. Gewinn-Jahr (Sparerpauschbetrag wird ausgenutzt)
    2. Verlust-Jahr mit Derivate-Verlusten (TFS-Deckel greift)
    3. Sparerpauschbetrag-Ausnutzung mit gemischten Töpfen

Free-Stack: stdlib ``unittest`` + sqlite3, keine externen Libs.
Preis-Lookup wird via Monkey-Patch umgangen: Tests fokussieren auf Steuer-Logik.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

# Ermögliche direkten Import aus tools/
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import tax_loss_harvester as tlh  # noqa: E402


TRADES_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    system_version TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    tier TEXT,
    sector TEXT,
    regime TEXT,
    entry_date TEXT NOT NULL,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    holding_days INTEGER,
    hard_exit_tag TEXT,
    atr_pct REAL,
    atr_mult REAL,
    sl_pct REAL,
    tp_pct REAL,
    v6_score REAL,
    triple_rsi REAL,
    rsi2 REAL, rsi5 REAL, rsi14 REAL,
    position_size REAL,
    brutto_pnl REAL,
    cost REAL DEFAULT 2.0,
    netto_pnl REAL,
    return_pct REAL,
    exit_reason TEXT,
    is_win INTEGER,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    route TEXT,
    hebel_factor REAL
);
"""


def _mk_db(path: Path, rows: list[dict]) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.executescript(TRADES_SCHEMA)
        for r in rows:
            cols = ",".join(r.keys())
            ph = ",".join("?" for _ in r)
            con.execute(f"INSERT INTO trades ({cols}) VALUES ({ph})", tuple(r.values()))
        con.commit()
    finally:
        con.close()


class TaxLossHarvesterTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trade_journal.sqlite"
        self.prices_dir = Path(self.tmp.name) / "prices"
        self.out_dir = Path(self.tmp.name) / "out"
        self.year_start = date(2026, 1, 1)
        self.as_of = date(2026, 4, 21)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # Szenario 1: Gewinn-Jahr, Pauschbetrag wird ausgenutzt
    # ------------------------------------------------------------------
    def test_gewinn_jahr_spar_pauschbetrag_ausgenutzt(self) -> None:
        _mk_db(self.db_path, [
            {
                "source": "live", "system_version": "V6.1",
                "ticker": "AAPL", "entry_date": "2026-02-01", "exit_date": "2026-03-01",
                "entry_price": 100.0, "exit_price": 120.0,
                "position_size": 2000.0, "brutto_pnl": 400.0, "cost": 2.0,
                "netto_pnl": 398.0,
            },
            {
                "source": "live", "system_version": "V6.1",
                "ticker": "MSFT", "entry_date": "2026-02-15", "exit_date": "2026-03-15",
                "entry_price": 300.0, "exit_price": 330.0,
                "position_size": 1500.0, "brutto_pnl": 150.0, "cost": 2.0,
                "netto_pnl": 148.0,
            },
        ])

        closed, open_pos = tlh.load_trades(self.db_path, self.year_start, self.as_of)
        self.assertEqual(len(closed), 2)
        self.assertEqual(len(open_pos), 0)

        report = tlh.build_report(
            closed, open_pos,
            year_start=self.year_start, as_of=self.as_of,
            spar_pauschbetrag=1000.0, tfs_cap=20000.0, loss_threshold=50.0,
            prices_dir=self.prices_dir,
        )
        self.assertAlmostEqual(report.toepfe["aktien_realized_eur"], 546.0, places=2)
        self.assertAlmostEqual(report.toepfe["derivate_realized_eur"], 0.0, places=2)
        self.assertEqual(report.toepfe["trades_count_aktien"], 2)
        # Pauschbetrag noch 454 € frei (1000 - 546)
        self.assertAlmostEqual(report.sparer_used_eur, 546.0, places=2)
        self.assertAlmostEqual(report.sparer_remaining_eur, 454.0, places=2)
        # Keine Harvest-Kandidaten, da keine offenen Positionen
        self.assertEqual(report.candidates, [])
        self.assertFalse(report.hook_trigger)

    # ------------------------------------------------------------------
    # Szenario 2: Verlust-Jahr, Derivate-Topf mit TFS-Deckel
    # ------------------------------------------------------------------
    def test_verlust_jahr_tfs_deckel(self) -> None:
        # 25.000 € Derivate-Verluste → oberhalb 20k TFS-Cap
        _mk_db(self.db_path, [
            {
                "source": "live", "system_version": "V6.8-Hebel",
                "ticker": "NVDA-KO-5x", "entry_date": "2026-01-10", "exit_date": "2026-01-20",
                "entry_price": 10.0, "exit_price": 2.0,
                "position_size": 800.0, "brutto_pnl": -12000.0, "cost": 2.0,
                "netto_pnl": -12002.0,
                "route": "KO", "hebel_factor": 5.0,
            },
            {
                "source": "live", "system_version": "V6.8-Hebel",
                "ticker": "TSLA-OS-10x", "entry_date": "2026-02-01", "exit_date": "2026-02-10",
                "entry_price": 5.0, "exit_price": 1.0,
                "position_size": 500.0, "brutto_pnl": -13000.0, "cost": 2.0,
                "netto_pnl": -13002.0,
                "route": "OS", "hebel_factor": 10.0,
            },
            # kleiner Aktien-Gewinn
            {
                "source": "live", "system_version": "V6.1",
                "ticker": "AAPL", "entry_date": "2026-03-01", "exit_date": "2026-03-15",
                "entry_price": 100.0, "exit_price": 110.0,
                "position_size": 1000.0, "brutto_pnl": 100.0, "cost": 2.0,
                "netto_pnl": 98.0,
            },
        ])

        closed, open_pos = tlh.load_trades(self.db_path, self.year_start, self.as_of)
        report = tlh.build_report(
            closed, open_pos,
            year_start=self.year_start, as_of=self.as_of,
            spar_pauschbetrag=1000.0, tfs_cap=20000.0, loss_threshold=50.0,
            prices_dir=self.prices_dir,
        )
        # Derivate-Topf: -25004 €, Aktien +98 €
        self.assertAlmostEqual(report.toepfe["aktien_realized_eur"], 98.0, places=2)
        self.assertAlmostEqual(report.toepfe["derivate_realized_eur"], -25004.0, places=2)
        # Verlust-Summe (positiv ausgedrückt)
        self.assertAlmostEqual(report.toepfe["derivate_verluste_eur"], 25004.0, places=2)
        # TFS-Deckel kappt nutzbare Verluste auf 20.000
        self.assertAlmostEqual(report.tfs_verlust_nutzbar_eur, 20000.0, places=2)
        # Pauschbetrag kommt nur auf positiven Gewinn zur Anwendung
        self.assertAlmostEqual(report.sparer_used_eur, 98.0, places=2)
        self.assertAlmostEqual(report.sparer_remaining_eur, 902.0, places=2)
        # Hook greift wegen TFS-Cap-Überschreitung (auch ohne offene Harvest-Kandidaten)
        self.assertTrue(report.hook_trigger)
        # Action-String enthält TFS-Hinweis
        self.assertTrue(any("TFS" in a or "Folgejahr" in a for a in report.actions))

    # ------------------------------------------------------------------
    # Szenario 3: Sparerpauschbetrag voll ausgenutzt + offene Verlust-Position
    # ------------------------------------------------------------------
    def test_sparer_pauschbetrag_und_harvest_kandidat(self) -> None:
        _mk_db(self.db_path, [
            # geschlossener Gewinn deutlich über 1000 €
            {
                "source": "live", "system_version": "V6.1",
                "ticker": "NVDA", "entry_date": "2026-01-10", "exit_date": "2026-02-10",
                "entry_price": 500.0, "exit_price": 700.0,
                "position_size": 5000.0, "brutto_pnl": 2002.0, "cost": 2.0,
                "netto_pnl": 2000.0,
            },
            # Offene Verlust-Position (AMD): entry 150 €, size 1500 €, 10 Stk → bei 130 € aktuell → -200 €
            {
                "source": "live", "system_version": "V6.1",
                "ticker": "AMD", "entry_date": "2026-04-01",
                "entry_price": 150.0,
                "position_size": 1500.0,
            },
        ])

        # Monkey-Patch load_last_close, damit wir keinen parquet-Read brauchen
        original = tlh.load_last_close

        def fake_price(ticker, as_of, prices_dir):  # noqa: ARG001
            return {"AMD": 130.0}.get(ticker)

        tlh.load_last_close = fake_price
        try:
            closed, open_pos = tlh.load_trades(self.db_path, self.year_start, self.as_of)
            self.assertEqual(len(closed), 1)
            self.assertEqual(len(open_pos), 1)
            report = tlh.build_report(
                closed, open_pos,
                year_start=self.year_start, as_of=self.as_of,
                spar_pauschbetrag=1000.0, tfs_cap=20000.0, loss_threshold=50.0,
                prices_dir=self.prices_dir,
            )
        finally:
            tlh.load_last_close = original

        # Pauschbetrag voll aufgezehrt
        self.assertAlmostEqual(report.sparer_used_eur, 1000.0, places=2)
        self.assertAlmostEqual(report.sparer_remaining_eur, 0.0, places=2)
        # Harvest-Kandidat erkannt (AMD, -200 €)
        self.assertEqual(len(report.candidates), 1)
        cand = report.candidates[0]
        self.assertEqual(cand["ticker"], "AMD")
        self.assertAlmostEqual(cand["unrealized_pnl_eur"], -200.0, places=2)
        self.assertEqual(cand["topf"], "aktien")
        # Hook greift
        self.assertTrue(report.hook_trigger)
        # Mindestens eine Action empfiehlt Harvesting
        self.assertTrue(any("Harvest-Kandidat" in a for a in report.actions))
        # Action erwähnt dass Pauschbetrag überschritten ist
        self.assertTrue(any("übersteigen" in a or "Pauschbetrag" in a for a in report.actions))

    # ------------------------------------------------------------------
    # Zusatz: Topf-Routing
    # ------------------------------------------------------------------
    def test_classify_topf_routing(self) -> None:
        self.assertEqual(tlh.classify_topf({"route": "KO", "hebel_factor": None}), "derivate")
        self.assertEqual(tlh.classify_topf({"route": "Direkt", "hebel_factor": 5.0}), "derivate")
        self.assertEqual(tlh.classify_topf({"route": None, "hebel_factor": 1.0}), "aktien")
        self.assertEqual(tlh.classify_topf({"route": None, "hebel_factor": None,
                                             "system_version": "V6.1"}), "aktien")
        self.assertEqual(tlh.classify_topf({"route": None, "hebel_factor": None,
                                             "system_version": "V6.8-Options-Alpha"}), "derivate")


def test_no_loss_cap_by_default(tmp_path):
    """Default: no derivative loss cap (reportedly abolished by the JStG 2024)."""
    db = tmp_path / "trade_journal.sqlite"
    _mk_db(db, [{
        "source": "live", "system_version": "V6.8-Hebel",
        "ticker": "AAA-OS-10x", "entry_date": "2099-02-01", "exit_date": "2099-02-10",
        "entry_price": 5.0, "exit_price": 1.0,
        "position_size": 500.0, "brutto_pnl": -25000.0, "cost": 2.0,
        "netto_pnl": -25002.0, "route": "OS", "hebel_factor": 10.0,
    }])
    assert tlh.parse_args([]).tfs_cap is None
    closed, open_pos = tlh.load_trades(db, date(2099, 1, 1), date(2099, 4, 21))
    report = tlh.build_report(
        closed, open_pos, year_start=date(2099, 1, 1), as_of=date(2099, 4, 21),
        spar_pauschbetrag=1000.0, tfs_cap=None, loss_threshold=50.0,
        prices_dir=tmp_path / "prices")
    assert report.tfs_cap_eur is None
    assert abs(report.tfs_verlust_nutzbar_eur - 25002.0) < 0.01
    assert not any("Folgejahr" in a for a in report.actions)
    assert report.hook_trigger is False


if __name__ == "__main__":
    unittest.main()
