#!/usr/bin/env python3
"""Tests fuer COHORT-TAG-LEDGER-1 (V7.30).

Hermetisch: In-Memory-Journal (connector-Injektion) + Temp-Scan/Regime-Files.
Kein Zugriff auf das echte trade_journal.sqlite / Netz.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import cohort_tag_ledger as ctl  # noqa: E402


# --------------------------------------------------------------------------- #
# Helfer
# --------------------------------------------------------------------------- #
def _mem(rows):
    return ctl._mem_journal(rows)


_ROWS = [
    ("AAA", "live", "2025-03-15", "2025-03-18", None, "long", 5.0, 50.0, 1,
     "Trending-Divergent", None, "mr_v61", "V6.1", 3),
    ("BBB", "external", "2025-04-01", "2025-04-02", None, "long", None, 30.0, 1,
     None, "Trending", "discretionary", "discretionary-external", 1),
    ("CCC", "live", "2024-06-01", "2024-06-04", None, "long", -1.0, -10.0, 0,
     "?", None, "mr_v61", "V6.1", 3),
    ("ZZZ", "backtest", "2025-03-15", "2025-03-18", 2.0, "long", 1.0, 10.0, 1,
     "Neutral", None, "mr_v61", "V6.1", 3),
]


@pytest.fixture
def archive(tmp_path):
    scan = {
        "scan_date": "2025-03-15",
        "ticker_map": [
            {"ticker": "AAA", "name": "Alpha", "sector": "Tech", "tier": "A",
             "pre_score": 8.1, "triple_rsi": 6, "rsi2": 3.0, "rsi5": 10.0,
             "rsi14": 28.0, "atr_pct": 4.2},
        ],
    }
    (tmp_path / "scan_2025-03-15.json").write_text(json.dumps(scan), encoding="utf-8")
    (tmp_path / "regime_2025-04-01.json").write_text(
        json.dumps({"date": "2025-04-01", "regime": "Bullish-Confirmed"}),
        encoding="utf-8")
    return tmp_path


def _ledger(tmp_path, rows=_ROWS):
    trades, err = ctl.load_journal_trades("ignored", connector=lambda _p: _mem(rows))
    assert err is None
    return ctl.build_ledger("ignored", scan_dir=tmp_path, regime_dir=tmp_path,
                            cutoff="2025-01-01", run_date="test",
                            journal_loader=lambda: (trades, None))


# --------------------------------------------------------------------------- #
# Selftest (eingebettet)
# --------------------------------------------------------------------------- #
def test_embedded_selftest_passes():
    assert ctl.selftest() == 0


# --------------------------------------------------------------------------- #
# Journal-Reader
# --------------------------------------------------------------------------- #
def test_source_filter_excludes_backtest():
    trades, err = ctl.load_journal_trades("x", connector=lambda _p: _mem(_ROWS))
    assert err is None
    assert {t["ticker"] for t in trades} == {"AAA", "BBB", "CCC"}
    assert all(t["source"] in ctl._LIVE_SOURCES for t in trades)


def test_missing_db_fail_soft():
    trades, err = ctl.load_journal_trades(Path("/no/such/journal_xyz.sqlite"))
    assert trades == []
    assert err is not None


def test_empty_table_fail_soft():
    def connector(_p):
        c = sqlite3.connect(":memory:")
        return c  # keine trades-Tabelle
    trades, err = ctl.load_journal_trades("x", connector=connector)
    assert trades == []
    assert "keine trades-Tabelle" in err


# --------------------------------------------------------------------------- #
# Tagging-Klassifikation
# --------------------------------------------------------------------------- #
def test_complete_row(archive):
    rep = _ledger(archive)
    aaa = next(t for t in rep["trades"] if t["ticker"] == "AAA")
    assert aaa["tag_status"] == "COMPLETE"
    assert aaa["rsi14"] == 28.0 and aaa["atr_pct"] == 4.2
    assert aaa["regime"] == "Trending-Divergent"
    assert aaa["forward_eligible"] is True
    assert aaa["v61_netto_pnl"] == 50.0
    assert aaa["v61_skip"] == ""  # ueberlebt load_trades-Filter


def test_partial_row_regime_only(archive):
    rep = _ledger(archive)
    bbb = next(t for t in rep["trades"] if t["ticker"] == "BBB")
    assert bbb["tag_status"] == "PARTIAL"
    assert bbb["regime"] == "Trending"        # legacy/at_entry gewinnt vor archive
    assert bbb["forward_eligible"] is False    # nicht-candidate regime + kein return
    assert "return_pct" in bbb["missing_fields"]
    assert "rsi14" in bbb["missing_fields"]


def test_untaggable_row(archive):
    rep = _ledger(archive)
    ccc = next(t for t in rep["trades"] if t["ticker"] == "CCC")
    assert ccc["tag_status"] == "UNTAGGABLE"
    assert ccc["regime"] is None               # '?' normalisiert
    assert "kein scan_2024-06-01.json" in ccc["tag_reason"]


def test_forward_eligible_needs_candidate_and_return(archive):
    # candidate regime cell + return -> eligible auch ohne numeric
    rows = [("DDD", "live", "2025-05-01", "2025-05-03", None, "long", 2.0, 20.0, 1,
             "Bullish-Confirmed", None, "mr_v61", "V6.1", 2)]
    rep = _ledger(archive, rows=rows)
    ddd = rep["trades"][0]
    assert ddd["regime"] == "Bullish-Confirmed"
    assert ddd["forward_eligible"] is True     # candidate cell + return present


# --------------------------------------------------------------------------- #
# Summary + Backlog
# --------------------------------------------------------------------------- #
def test_summary_counts(archive):
    s = _ledger(archive)["summary"]
    assert s["n_trades"] == 3
    assert s["n_post_cutoff"] == 2             # AAA, BBB (CCC is 2024)
    assert s["n_complete"] == 1
    assert s["n_post_cutoff_forward_eligible"] == 1
    assert s["forward_ready"] is False
    assert s["journal_error"] is None


def test_backlog_reasons(archive):
    b = _ledger(archive)["backlog"]
    assert b["n_open"] == 2                     # BBB partial + CCC untaggable
    assert "rsi14" in b["missing_field_counts"]
    assert b["by_reason"]


# --------------------------------------------------------------------------- #
# Consumer-Kompatibilitaet (V7.29 assess_live_forward Shape)
# --------------------------------------------------------------------------- #
def test_ledger_consumer_shape(archive, tmp_path, monkeypatch):
    # Markdown nach tmp_path/Briefings statt ins Repo schreiben (hermetisch).
    monkeypatch.setattr(ctl, "_ROOT", tmp_path)
    rep = _ledger(archive)
    lp, mp = ctl.write_outputs(rep, "test", ledger_path=tmp_path / "sig" / "ledger.json")
    assert str(mp).startswith(str(tmp_path))
    data = json.loads(lp.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("trades", [])
    assert rows, "trades list must be consumable"
    post = [t for t in rows if str(t.get("date", "")) >= "2025-01-01"]
    assert len(post) == 2
    # genau die Schluessel, die der Forward-Test fuer Metriken liest
    for k in ("date", "regime", "return_pct", "v61_netto_pnl", "atr_pct", "rsi14"):
        assert all(k in t for t in rows)


def test_entry_filter_forward_test_importable():
    # Modul-Kompatibilitaet: der Consumer muss importierbar bleiben.
    import entry_filter_forward_test as eff
    assert hasattr(eff, "assess_live_forward")


def test_render_md_smoke(archive):
    md = ctl.render_md(_ledger(archive))
    assert "Cohort-Tag-Ledger" in md
    assert "Backlog" in md
    assert "Generated by tools/cohort_tag_ledger.py" in md


# --------------------------------------------------------------------------- #
# Scan-Lookup-Details
# --------------------------------------------------------------------------- #
def test_scan_file_present(archive):
    assert ctl.scan_file_present(archive, "2025-03-15") is True
    assert ctl.scan_file_present(archive, "2099-01-01") is False
    assert ctl.scan_file_present(archive, "") is False


def test_scan_features_ticker_miss(archive):
    # Scan existiert, Ticker nicht drin -> None
    assert ctl.load_scan_features(archive, "2025-03-15", "NOTHERE") is None
    # Scan existiert, Ticker drin
    f = ctl.load_scan_features(archive, "2025-03-15", "AAA")
    assert f and f["rsi14"] == 28.0
