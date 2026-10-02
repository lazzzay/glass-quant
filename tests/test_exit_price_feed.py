#!/usr/bin/env python3
"""pytest fuer EXIT-PRICE-FEED-1 (V7.15).

Deckt ab:
  - build_closes_map(): Happy-Path, lookback-Trim, min_closes-Grenze, Quellen-Label
  - Graceful Degradation: too_few_closes / no_data / fetch_exception -> unavailable
  - open_position_tickers(): liest nur offene Journal-Rows (read-only, SSoT-Reuse)
  - END-TO-END: eine PRICE_FEED_MISSING-Position kippt mit Feed zu EXIT/connors_sma5
    (Beweis ueber die echte Modulgrenze exit_price_feed -> portfolio_exit_review)

Laeuft vollstaendig OFFLINE via injiziertem Fetcher + temporaeren SQLite-DBs
(weder Netz/yfinance noch die Live-DB werden angefasst).
    python3 tests/test_exit_price_feed.py   # oder: pytest tests/test_exit_price_feed.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3
import sys
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


def _load(name, rel):
    # tools/ in sys.path, damit interne Geschwister-Imports (exit_engine,
    # portfolio_exit_review) aufloesen.
    tools = str(REPO / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _epf():
    return _load("exit_price_feed_under_test", "tools/exit_price_feed.py")


def _per():
    return _load("portfolio_exit_review_under_test_epf", "tools/portfolio_exit_review.py")


def _fake(series):
    def _f(ticker):
        v = series.get(ticker)
        if isinstance(v, Exception):
            raise v
        if v is None:
            return (None, "none")
        return v
    return _f


def _db_with(rows) -> str:
    fd, p = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO trades (source,ticker,entry_date,position_size,entry_price,"
        "stop,target,side,holding_days,max_hold_days,exit_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return p


# ---------------------------------------------------------------------------
def test_build_closes_map_happy():
    epf = _epf()
    res = epf.build_closes_map(
        ["AAA", "BBB"],
        fetcher=_fake({"AAA": ([1, 2, 3, 4, 5, 6], "yfinance"),
                       "BBB": ([10, 11, 12, 13, 14], "stooq")}),
    )
    assert res["n_fetched"] == 2
    assert res["n_unavailable"] == 0
    assert res["closes_map"]["AAA"][-1] == 6.0
    assert res["sources"]["BBB"] == "stooq"


def test_lookback_trim_keeps_most_recent():
    epf = _epf()
    series = [float(x) for x in range(1, 21)]  # 20 Werte, juengster = 20
    res = epf.build_closes_map(["AAA"], lookback=12,
                               fetcher=_fake({"AAA": (series, "yfinance")}))
    assert len(res["closes_map"]["AAA"]) == 12
    assert res["closes_map"]["AAA"][-1] == 20.0   # juengster Wert bleibt
    assert res["closes_map"]["AAA"][0] == 9.0      # genau die letzten 12


def test_min_closes_boundary_included():
    epf = _epf()
    res = epf.build_closes_map(["EEE"], min_closes=5,
                               fetcher=_fake({"EEE": ([1, 2, 3, 4, 5], "yfinance")}))
    assert "EEE" in res["closes_map"]
    assert res["n_unavailable"] == 0


def test_too_few_closes_unavailable():
    epf = _epf()
    res = epf.build_closes_map(["CCC"], min_closes=5,
                               fetcher=_fake({"CCC": ([1, 2, 3], "yfinance")}))
    assert "CCC" not in res["closes_map"]
    reasons = {u["ticker"]: u["reason"] for u in res["unavailable"]}
    assert reasons["CCC"] == "too_few_closes"


def test_no_data_unavailable():
    epf = _epf()
    res = epf.build_closes_map(["DDD"], fetcher=_fake({"DDD": (None, "none")}))
    assert "DDD" not in res["closes_map"]
    assert res["unavailable"][0]["reason"] == "no_data"


def test_fetch_exception_isolated():
    epf = _epf()
    # FFF wirft, AAA muss trotzdem durchlaufen
    res = epf.build_closes_map(
        ["FFF", "AAA"],
        fetcher=_fake({"FFF": ValueError("boom"),
                       "AAA": ([1, 2, 3, 4, 5], "yfinance")}),
    )
    assert "AAA" in res["closes_map"]
    reasons = {u["ticker"]: u["reason"] for u in res["unavailable"]}
    assert reasons["FFF"] == "fetch_exception"


def test_empty_input():
    epf = _epf()
    res = epf.build_closes_map([], fetcher=_fake({}))
    assert res["closes_map"] == {}
    assert res["n_requested"] == 0


def test_open_position_tickers_reads_open_only():
    epf = _epf()
    db = _db_with([
        ("live", "AAA", "2026-06-02", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None),
        ("live", "ZZZ", "2026-05-20", 1500.0, 100.0, 95.0, 115.0, "long", 4, 3, "2026-05-23"),
    ])
    try:
        assert epf.open_position_tickers(db_path=db) == ["AAA"]
    finally:
        os.remove(db)


def test_end_to_end_price_feed_missing_flips_to_exit():
    """Der Kern-Beweis: ohne Feed PRICE_FEED_MISSING, mit Feed EXIT/connors_sma5."""
    epf = _epf()
    per = _per()
    db = _db_with([
        ("live", "AAA", "2026-06-02", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None),
    ])
    try:
        # ohne Feed
        rev0 = per.review_portfolio(as_of="2026-06-05", closes_map={}, db_path=db)
        assert rev0["positions"][0]["classification"] == "PRICE_FEED_MISSING"

        # mit Feed (SMA5([101,102,103,104,104])=102.8, last 104 >= SMA5)
        feed = epf.build_closes_map(["AAA"],
                                    fetcher=_fake({"AAA": ([101, 102, 103, 104, 104], "yfinance")}))
        rev1 = per.review_portfolio(as_of="2026-06-05",
                                    closes_map=feed["closes_map"], db_path=db)
        pos = rev1["positions"][0]
        assert pos["classification"] == "EXIT"
        assert pos["reason"] == "connors_sma5"
        assert rev1["summary"]["EXIT"] == 1
        assert rev1["summary"]["PRICE_FEED_MISSING"] == 0
    finally:
        os.remove(db)


if __name__ == "__main__":
    # Fallback-Runner ohne pytest
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa
            print(f"  [FAIL] {fn.__name__}: {e}")
    print(f"=== test_exit_price_feed: {passed}/{len(fns)} ===")
    raise SystemExit(0 if passed == len(fns) else 1)
