"""pytest: EXIT-LOOP-WIRING-1 (V7.18) tools/evening_exit_tracking.py

Vollstaendig offline: temporaere SQLite-Journale + injizierter Fetcher.
Fokus: Status-/Exit-Code-Vertrag, kein Fetch ohne Not, kein False-All-Clear
bei unlesbarem Journal, kanonische Artefakt-Namen, Journal bleibt read-only.
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import evening_exit_tracking as eet  # noqa: E402
import portfolio_exit_review as per  # noqa: E402


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def make_db(path, rows):
    con = sqlite3.connect(str(path))
    con.executescript(per._SCHEMA)
    con.executemany(
        "INSERT INTO trades (source,ticker,entry_date,position_size,entry_price,"
        "stop,target,side,holding_days,max_hold_days,exit_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        list(rows),
    )
    con.commit()
    con.close()
    return str(path)


def counting_fetcher(series, calls):
    def _f(ticker):
        calls.append(ticker)
        v = series.get(ticker)
        return (None, "none") if v is None else v
    return _f


OPEN_AAA = ("live", "AAA", "2026-06-08", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None)
OPEN_DDD = ("live", "DDD", "2026-06-09", 1500.0, 50.0, 45.0, 60.0, "long", 1, 3, None)
CLOSED_ZZZ = ("live", "ZZZ", "2026-05-20", 1500.0, 100.0, 95.0, 115.0, "long", 4, 3, "2026-05-23")
AAA_EXIT_CLOSES = ([101, 102, 103, 104, 104], "yfinance")  # SMA5=102.8, last>=SMA5 -> EXIT


# ----------------------------------------------------------------------------
# Status- und Exit-Code-Vertrag
# ----------------------------------------------------------------------------
def test_no_positions_status_and_no_fetch(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [CLOSED_ZZZ])  # nur geschlossene Row
    calls = []
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({}, calls))
    assert r["status"] == eet.STATUS_NO_POSITIONS
    assert r["exit_code"] == eet.RC_OK
    assert calls == []                       # KEIN Netz-Fetch bei 0 offenen
    assert r["n_open"] == 0
    assert r["review"]["n_open"] == 0


def test_ok_exit_verdict_propagated(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [OPEN_AAA])
    calls = []
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({"AAA": AAA_EXIT_CLOSES}, calls))
    assert r["status"] == eet.STATUS_OK
    assert r["exit_code"] == eet.RC_OK
    assert calls == ["AAA"]
    by = {p["ticker"]: p for p in r["review"]["positions"]}
    assert by["AAA"]["classification"] == "EXIT"
    assert by["AAA"]["reason"] == "connors_sma5"
    assert r["review"]["summary"]["EXIT"] == 1


def test_feed_degraded_status_and_rc(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [OPEN_AAA, OPEN_DDD])
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({"AAA": AAA_EXIT_CLOSES}, []))
    assert r["status"] == eet.STATUS_FEED_DEGRADED
    assert r["exit_code"] == eet.RC_FEED_DEGRADED
    assert r["review"]["summary"]["PRICE_FEED_MISSING"] == 1


def test_journal_missing_is_unreadable_not_all_clear(tmp_path):
    calls = []
    r = eet.run_evening_exit(as_of="2026-06-10",
                             db_path=str(tmp_path / "missing.sqlite"),
                             fetcher=counting_fetcher({}, calls))
    assert r["status"] == eet.STATUS_JOURNAL_UNREADABLE
    assert r["exit_code"] == eet.RC_JOURNAL_UNREADABLE
    assert r["journal_probe"]["reason"] == "db_missing"
    assert r["review"] is None               # KEIN vorgetaeuschtes leeres Review
    assert calls == []


def test_missing_journal_says_run_init_not_sandbox(tmp_path):
    r = eet.run_evening_exit(as_of="2026-06-10",
                             db_path=str(tmp_path / "missing.sqlite"),
                             fetcher=counting_fetcher({}, []))
    md = eet.render_evening_markdown(r)
    assert "tools/trade_journal.py init" in md
    assert "EDEADLK" not in md


def test_journal_dir_as_db_is_unreadable(tmp_path):
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=str(tmp_path),
                             fetcher=counting_fetcher({}, []))
    assert r["status"] == eet.STATUS_JOURNAL_UNREADABLE


def test_probe_distinguishes_ok_from_error(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [OPEN_AAA])
    p_ok = eet.probe_journal(db)
    assert p_ok["readable"] is True and p_ok["n_open"] == 1
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"not a sqlite file at all" * 10)
    p_bad = eet.probe_journal(str(garbage))
    assert p_bad["readable"] is False
    assert p_bad["reason"] == "read_error"


# ----------------------------------------------------------------------------
# Artefakte + read-only-Garantie
# ----------------------------------------------------------------------------
def test_write_canonical_artifacts_and_db_untouched(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [OPEN_AAA])
    before = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({"AAA": AAA_EXIT_CLOSES}, []),
                             write=True,
                             briefings_dir=str(tmp_path / "B"),
                             signals_dir=str(tmp_path / "S"))
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before
    names = sorted(Path(p).name for p in r["artifacts"])
    assert names == sorted(["Exit-Review-2026-06-10.md", "exit_review_2026-06-10.json",
                            "closes_2026-06-10.json", "evening_exit_2026-06-10.json"])
    closes = json.loads((tmp_path / "S" / "closes_2026-06-10.json").read_text())
    assert closes == {"AAA": [101.0, 102.0, 103.0, 104.0, 104.0]}  # bare V7.15-Format
    review = json.loads((tmp_path / "S" / "exit_review_2026-06-10.json").read_text())
    assert review["summary"]["EXIT"] == 1    # V7.14-Format unveraendert


def test_write_on_unreadable_still_writes_warning_artifact(tmp_path):
    r = eet.run_evening_exit(as_of="2026-06-11",
                             db_path=str(tmp_path / "missing.sqlite"),
                             fetcher=counting_fetcher({}, []), write=True,
                             briefings_dir=str(tmp_path / "B"),
                             signals_dir=str(tmp_path / "S"))
    md = (tmp_path / "B" / "Exit-Review-2026-06-11.md").read_text()
    assert "JOURNAL NICHT LESBAR" in md
    assert "evening_exit_tracking.py --write" in md   # Mac-Nachhol-Befehl
    meta = json.loads((tmp_path / "S" / "evening_exit_2026-06-11.json").read_text())
    assert meta["status"] == eet.STATUS_JOURNAL_UNREADABLE
    assert meta["exit_code"] == eet.RC_JOURNAL_UNREADABLE


# ----------------------------------------------------------------------------
# Markdown-Inhalt
# ----------------------------------------------------------------------------
def test_md_exit_action_block(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [OPEN_AAA])
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({"AAA": AAA_EXIT_CLOSES}, []))
    md = eet.render_evening_markdown(r)
    assert "HEUTE HANDELN" in md
    assert "AAA: EXIT" in md
    assert "connors_sma5" in md


def test_md_no_positions(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [])
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({}, []))
    md = eet.render_evening_markdown(r)
    assert "Keine offenen Positionen" in md
    assert "HEUTE HANDELN" not in md


def test_md_feed_degraded_has_native_hint(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [OPEN_DDD])
    r = eet.run_evening_exit(as_of="2026-06-10", db_path=db,
                             fetcher=counting_fetcher({}, []))
    md = eet.render_evening_markdown(r)
    assert "FEED_DEGRADED" in md
    assert eet.NATIVE_CMD in md


# ----------------------------------------------------------------------------
# CLI / Selftest
# ----------------------------------------------------------------------------
def test_main_returns_status_exit_code(tmp_path):
    db = make_db(tmp_path / "j.sqlite", [])
    rc = eet.main(["--db", db, "--as-of", "2026-06-10", "--quiet"])
    assert rc == eet.RC_OK
    rc3 = eet.main(["--db", str(tmp_path / "missing.sqlite"),
                    "--as-of", "2026-06-10", "--quiet"])
    assert rc3 == eet.RC_JOURNAL_UNREADABLE


def test_selftest_green():
    assert eet.selftest() == 0
