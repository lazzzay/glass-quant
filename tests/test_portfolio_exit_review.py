#!/usr/bin/env python3
"""pytest fuer PORTFOLIO-EXIT-REVIEW-1 (V7.14).

Deckt ab:
  - list_open_positions() liest nur offene Rows (geschlossene ausgeschlossen)
  - review_portfolio() klassifiziert EXIT / DATA_INCOMPLETE / PRICE_FEED_MISSING korrekt
  - Wiederverwendung von exit_engine als SSoT (reason connors_sma5 / hard_exit_t3)
  - render_markdown() Leer- und Befuellt-Fall
  - actions_required listet nur Nicht-HOLD-Faelle

Laeuft offline gegen temporaere SQLite-DBs (Live-DB wird nie angefasst).
    python3 tests/test_portfolio_exit_review.py   # oder: pytest tests/test_portfolio_exit_review.py
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


def _load_per():
    spec = importlib.util.spec_from_file_location(
        "portfolio_exit_review_under_test", REPO / "tools" / "portfolio_exit_review.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db_with(rows) -> str:
    fd, p = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.executemany(
        "INSERT INTO trades (source,ticker,entry_date,position_size,entry_price,"
        "stop,target,side,holding_days,max_hold_days,exit_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    c.commit()
    c.close()
    return p


# (source,ticker,entry_date,position_size,entry_price,stop,target,side,holding_days,max_hold_days,exit_date)
_DEMO = [
    ("live", "AAA", "2026-06-01", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None),
    ("live", "BBB", "2026-06-01", 1500.0, 100.0, 95.0, 115.0, "long", 3, 3, None),
    ("live", "CCC", "2026-06-02", 1500.0, None, None, None, "long", 1, 3, None),
    ("live", "DDD", "2026-06-02", 1500.0, 50.0, 45.0, 60.0, "long", 1, 3, None),
    ("live", "ZZZ", "2026-05-20", 1500.0, 100.0, 95.0, 115.0, "long", 4, 3, "2026-05-23"),
]
_CLOSES = {
    "AAA": [101, 102, 103, 104, 104],   # SMA5=102.8 -> connors_sma5 EXIT
    "BBB": [101, 101, 101, 101, 100],   # last<SMA5, T+3 flach -> hard_exit_t3 EXIT
}


def test_list_open_excludes_closed():
    per = _load_per()
    p = _db_with(_DEMO)
    try:
        opens = per.list_open_positions(p)
        tickers = {r["ticker"] for r in opens}
        assert tickers == {"AAA", "BBB", "CCC", "DDD"}, tickers
        assert "ZZZ" not in tickers
    finally:
        os.remove(p)


def test_review_summary_counts():
    per = _load_per()
    p = _db_with(_DEMO)
    try:
        rev = per.review_portfolio(as_of="2026-06-04", closes_map=_CLOSES, db_path=p)
        assert rev["n_open"] == 4, rev["n_open"]
        s = rev["summary"]
        assert s["EXIT"] == 2, s
        assert s["DATA_INCOMPLETE"] == 1, s
        assert s["PRICE_FEED_MISSING"] == 1, s
        assert s["HOLD"] == 0, s
    finally:
        os.remove(p)


def test_review_uses_exit_engine_reasons():
    per = _load_per()
    p = _db_with(_DEMO)
    try:
        rev = per.review_portfolio(as_of="2026-06-04", closes_map=_CLOSES, db_path=p)
        by = {x["ticker"]: x for x in rev["positions"]}
        assert by["AAA"]["reason"] == "connors_sma5", by["AAA"]
        assert by["AAA"]["action"] == "EXIT"
        assert by["BBB"]["reason"] == "hard_exit_t3", by["BBB"]
        assert by["CCC"]["classification"] == "DATA_INCOMPLETE"
        assert by["DDD"]["classification"] == "PRICE_FEED_MISSING"
    finally:
        os.remove(p)


def test_actions_required_excludes_hold():
    per = _load_per()
    p = _db_with(_DEMO)
    try:
        rev = per.review_portfolio(as_of="2026-06-04", closes_map=_CLOSES, db_path=p)
        ar = {a["ticker"] for a in rev["actions_required"]}
        assert ar == {"AAA", "BBB", "CCC", "DDD"}, ar
    finally:
        os.remove(p)


def test_markdown_filled_and_empty():
    per = _load_per()
    p = _db_with(_DEMO)
    try:
        rev = per.review_portfolio(as_of="2026-06-04", closes_map=_CLOSES, db_path=p)
        md = per.render_markdown(rev)
        assert "AAA" in md
        assert "Handlungsbedarf" in md
        assert "set-levels CCC" in md
    finally:
        os.remove(p)

    empty = _db_with([])
    try:
        rev0 = per.review_portfolio(as_of="2026-06-04", db_path=empty)
        assert rev0["n_open"] == 0
        assert "Keine offenen Positionen" in per.render_markdown(rev0)
    finally:
        os.remove(empty)


def test_missing_db_returns_empty():
    per = _load_per()
    assert per.list_open_positions("/nonexistent/never.sqlite") == []


def test_selftest_green():
    per = _load_per()
    assert per.selftest() == 0


if __name__ == "__main__":
    import sys
    rc = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[PASS] {name}")
            except Exception as e:  # noqa: BLE001
                rc = 1
                print(f"[FAIL] {name}: {e}")
    sys.exit(rc)
