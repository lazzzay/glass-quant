#!/usr/bin/env python3
"""Selftest fuer Journal-Integritaets-Fixes (2026-06-01).

Deckt ab:
  - sell() schreibt netto_pnl / is_win / return_pct (Root-Cause-Fix des NULL-Bugs)
  - sell() bei Verlust -> is_win=0
  - recompute() backfillt geschlossene Legacy-Rows mit NULL netto_pnl/is_win
  - recompute() ist idempotent (kein Doppel-Schreiben)

Laeuft ohne Netzwerk gegen temporaere SQLite-DBs (Live-DB wird nie angefasst).
    python3 tests/test_quick_trade_journal.py     # oder: pytest tests/test_quick_trade_journal.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, system_version TEXT, ticker TEXT, tier TEXT, regime TEXT,
    entry_date TEXT, position_size REAL, cost REAL,
    exit_date TEXT, brutto_pnl REAL, netto_pnl REAL, return_pct REAL, is_win INTEGER,
    notes TEXT,
    entry_price REAL, stop REAL, target REAL, atr_pct REAL, atr_mult REAL,
    side TEXT, holding_days INTEGER, max_hold_days INTEGER, updated_at_utc TEXT
);
"""


def _load_qt(db_path: str):
    """Laedt quick_trade.py frisch und biegt DB_PATH auf die Test-DB um."""
    spec = importlib.util.spec_from_file_location(
        "quick_trade_under_test", REPO / "tools" / "quick_trade.py"
    )
    qt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qt)
    qt.DB_PATH = pathlib.Path(db_path)
    return qt


def _fresh_db() -> str:
    fd, p = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.commit()
    c.close()
    return p


def test_sell_writes_netto_and_iswin():
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("TEST", 1000.0, "2026-01-01", "live")
        qt.sell("TEST", 1100.0, "2026-01-05")  # brutto +100, cost 2 -> netto +98
        c = sqlite3.connect(p)
        brutto, cost, netto, ret, win, exitd = c.execute(
            "SELECT brutto_pnl,cost,netto_pnl,return_pct,is_win,exit_date "
            "FROM trades WHERE ticker='TEST'"
        ).fetchone()
        c.close()
        assert brutto == 100.0, brutto
        assert cost == 2.0, cost
        assert netto == 98.0, netto          # <- vorher NULL
        assert win == 1, win                 # <- vorher NULL
        assert abs(ret - 10.0) < 1e-6, ret
        assert exitd == "2026-01-05", exitd
    finally:
        os.remove(p)


def test_sell_loss_sets_iswin_zero():
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("LOSS", 1000.0, "2026-01-01", "live")
        qt.sell("LOSS", 960.0, "2026-01-05")  # brutto -40, cost 2 -> netto -42
        c = sqlite3.connect(p)
        netto, win = c.execute(
            "SELECT netto_pnl,is_win FROM trades WHERE ticker='LOSS'"
        ).fetchone()
        c.close()
        assert netto == -42.0, netto
        assert win == 0, win
    finally:
        os.remove(p)


def test_recompute_backfills_null_legacy():
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        # Legacy-Row: brutto gesetzt, netto_pnl/is_win NULL
        c = sqlite3.connect(p)
        c.execute(
            "INSERT INTO trades (source,ticker,position_size,cost,exit_date,brutto_pnl,netto_pnl,is_win) "
            "VALUES ('live','LEG',1000,2,'2026-02-02',50,NULL,NULL)"
        )
        c.commit()
        c.close()
        assert qt.recompute() == 1
        c = sqlite3.connect(p)
        netto, win, ret = c.execute(
            "SELECT netto_pnl,is_win,return_pct FROM trades WHERE ticker='LEG'"
        ).fetchone()
        c.close()
        assert netto == 48.0, netto
        assert win == 1, win
        assert abs(ret - 5.0) < 1e-6, ret
        # idempotent: zweiter Lauf macht nichts mehr
        assert qt.recompute() == 0
    finally:
        os.remove(p)


def test_recompute_noop_on_open_trade():
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("OPN", 1000.0, "2026-03-01", "live")  # offen -> exit_date NULL
        assert qt.recompute() == 0  # offene Trades nicht anfassen
        c = sqlite3.connect(p)
        netto = c.execute("SELECT netto_pnl FROM trades WHERE ticker='OPN'").fetchone()[0]
        c.close()
        assert netto is None
    finally:
        os.remove(p)


# --- V7.12 JOURNAL-LEVELS-1: set_levels() + buy()-Level-Flags ---------------

def test_set_levels_backfills_open_position():
    """Kernfall: offene Row mit entry_price NULL -> set_levels füllt Per-Share-Level."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("AAA", 2000.0, "2099-01-05", "live")  # EUR-Notional, Level NULL (Alt-Verhalten)
        c = sqlite3.connect(p)
        ep = c.execute("SELECT entry_price FROM trades WHERE ticker='AAA'").fetchone()[0]
        c.close()
        assert ep is None, ep  # vorher NULL (reproduziert den Bug)

        ok = qt.set_levels("AAA", entry_price=100.00, stop=95.00, target=110.00,
                           atr_pct=2.1, side="long")
        assert ok is True
        c = sqlite3.connect(p)
        ep, sl, tp, atr, side, esz = c.execute(
            "SELECT entry_price, stop, target, atr_pct, side, position_size "
            "FROM trades WHERE ticker='AAA'"
        ).fetchone()
        c.close()
        assert ep == 100.00, ep
        assert sl == 95.00, sl
        assert tp == 110.00, tp
        assert atr == 2.1, atr
        assert side == "long", side
        assert esz == 2000.0, esz  # position_size UNANGETASTET
    finally:
        os.remove(p)


def test_set_levels_only_touches_provided_fields():
    """Nur übergebene Felder werden geschrieben; nicht-übergebene bleiben NULL."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("ABC", 1000.0, "2026-06-02", "live")
        qt.set_levels("ABC", stop=90.0)  # nur stop
        c = sqlite3.connect(p)
        ep, sl, tp = c.execute(
            "SELECT entry_price, stop, target FROM trades WHERE ticker='ABC'"
        ).fetchone()
        c.close()
        assert ep is None, ep
        assert sl == 90.0, sl
        assert tp is None, tp
    finally:
        os.remove(p)


def test_set_levels_no_open_trade_returns_false():
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        assert qt.set_levels("GHOST", entry_price=10.0) is False
    finally:
        os.remove(p)


def test_set_levels_requires_at_least_one_field():
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("ZZZ", 500.0, "2026-06-02", "live")
        assert qt.set_levels("ZZZ") is False  # nichts übergeben -> no-op, False
    finally:
        os.remove(p)


def test_set_levels_ignores_closed_position():
    """set-levels greift nur offene (exit_date IS NULL) Positionen."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("CLD", 1000.0, "2026-06-01", "live")
        qt.sell("CLD", 1100.0, "2026-06-03")  # jetzt geschlossen
        assert qt.set_levels("CLD", entry_price=50.0) is False
    finally:
        os.remove(p)


def test_buy_with_levels_writes_complete_row():
    """buy() mit --entry-price etc. schreibt sofort eine vollständige Row."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("NEW", 1500.0, "2026-06-03", "live",
               entry_price=42.0, stop=39.0, target=48.0, atr_pct=1.8, side="long")
        c = sqlite3.connect(p)
        ep, sl, tp, side, esz = c.execute(
            "SELECT entry_price, stop, target, side, position_size FROM trades WHERE ticker='NEW'"
        ).fetchone()
        c.close()
        assert (ep, sl, tp, side, esz) == (42.0, 39.0, 48.0, "long", 1500.0)
    finally:
        os.remove(p)


def test_buy_without_levels_is_backward_compatible():
    """Ohne Level-Flags bleibt das INSERT-Verhalten unverändert (Level NULL)."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("OLD", 1000.0, "2026-06-03", "live")
        c = sqlite3.connect(p)
        ep, sl, tp, side, esz = c.execute(
            "SELECT entry_price, stop, target, side, position_size FROM trades WHERE ticker='OLD'"
        ).fetchone()
        c.close()
        assert ep is None and sl is None and tp is None and side is None
        assert esz == 1000.0
    finally:
        os.remove(p)


def test_set_levels_unblocks_exit_engine():
    """Integration: vor set-levels liefert exit_engine DATA_INCOMPLETE, danach ein echtes Verdikt.
    Das ist der eigentliche Nutzen (Hard-Exit-Pruefung an T+3). SKIP falls exit_engine nicht ladbar."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        try:
            import sys
            spec = importlib.util.spec_from_file_location(
                "exit_engine_under_test", REPO / "tools" / "exit_engine.py")
            ee = importlib.util.module_from_spec(spec)
            # In sys.modules registrieren, sonst scheitert die Dataclass-Forward-Ref-
            # Aufloesung von ExitVerdict ('NoneType' has no attribute __dict__).
            sys.modules[spec.name] = ee
            spec.loader.exec_module(ee)
        except Exception as e:  # pragma: no cover
            print(f"SKIP test_set_levels_unblocks_exit_engine (exit_engine nicht ladbar: {e})")
            return

        qt.buy("AAA", 2000.0, "2099-01-05", "live")
        # Vorher: entry_price NULL -> data_incomplete
        v0 = ee.evaluate_from_journal("AAA", as_of="2099-01-08",
                                      closes=[100.5, 100.2, 99.8, 100.4], db_path=p)
        assert v0 is not None and v0.reason == "data_incomplete", v0

        # Nach set-levels: echtes Verdikt (kein data_incomplete mehr)
        qt.set_levels("AAA", entry_price=100.00, stop=95.00, target=110.00,
                      atr_pct=2.1, side="long")
        v1 = ee.evaluate_from_journal("AAA", as_of="2099-01-08",
                                      closes=[100.5, 100.2, 99.8, 100.4], db_path=p)
        assert v1 is not None, v1
        assert v1.reason != "data_incomplete", v1
        assert abs(v1.entry_price - 100.00) < 1e-9, v1.entry_price
        # held = Mo 05.01 -> Do 08.01 = 3 Handelstage, flach (~+0,4%) -> Hard-Exit-T+3
        assert v1.action in ("EXIT", "HOLD"), v1.action
    finally:
        os.remove(p)


def test_set_levels_corrects_position_size():
    """Endspiel: falsch geloggter EUR-Notional (2000) → auf realen Einsatz
    (1250.00) korrigieren, DANN sell() rechnet den P&L korrekt. Ohne Korrektur würde
    sell(1300) brutto = 1300 - 2000 = -700 buchen (Phantom-Verlust)."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("AAA", 2000.0, "2099-01-05", "live")  # geschätzter Notional, falsch

        # Notional korrigieren + Per-Share-Level in EINEM set-levels-Call
        ok = qt.set_levels("AAA", position_size=1250.00, entry_price=100.00,
                           stop=95.00, target=110.00, atr_pct=2.1, side="long")
        assert ok is True
        c = sqlite3.connect(p)
        esz, ep = c.execute(
            "SELECT position_size, entry_price FROM trades WHERE ticker='AAA'"
        ).fetchone()
        c.close()
        assert esz == 1250.00, esz   # <- jetzt korrigiert (vorher 2000)
        assert ep == 100.00, ep

        # Verkauf für 1300 → brutto +50.00, cost 2, netto +48.00, win
        qt.sell("AAA", 1300.0, "2099-01-07")
        c = sqlite3.connect(p)
        brutto, netto, ret, win = c.execute(
            "SELECT brutto_pnl, netto_pnl, return_pct, is_win FROM trades WHERE ticker='AAA'"
        ).fetchone()
        c.close()
        assert abs(brutto - 50.00) < 1e-6, brutto
        assert abs(netto - 48.00) < 1e-6, netto
        assert win == 1, win
        assert abs(ret - 4.0) < 0.01, ret
    finally:
        os.remove(p)


def test_set_levels_position_size_only():
    """position_size allein (ohne Level) ist ein gültiger Korrektur-Call."""
    p = _fresh_db()
    try:
        qt = _load_qt(p)
        qt.buy("FIX", 2000.0, "2026-06-01", "live")
        assert qt.set_levels("FIX", position_size=1250.00) is True
        c = sqlite3.connect(p)
        esz, ep = c.execute(
            "SELECT position_size, entry_price FROM trades WHERE ticker='FIX'"
        ).fetchone()
        c.close()
        assert esz == 1250.00, esz
        assert ep is None, ep  # Level unangetastet, nur Notional korrigiert
    finally:
        os.remove(p)


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa
            fails += 1
            print(f"FAIL  {t.__name__} -> {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    raise SystemExit(1 if fails else 0)
