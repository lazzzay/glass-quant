"""Regression-Tests fuer die P0-MCP-Fixes (Audit 2026-06-14).

- K2: get_recommendations las Phantom-Keys (date/n_universe/recommendations) statt
      der echten Scan-Keys (scan_date/universe_size/top_setups) -> immer leer.
- K3/N8: get_journal_summary('disc') las 0 Rows (DB-Label = 'discretionary') und
      baute WHERE per f-string -> Alias + parametrisierte Query.

Die Quell-Assertions laufen ueberall (keine Abhaengigkeiten) und pinnen exakt die
gefixten Stellen, damit der Bug nicht still zurueckkehrt. Der DB-Check verifiziert,
dass 'discretionary' das korrekte Alias-Ziel ist.
"""
from pathlib import Path
import sqlite3
import pytest

REPO = Path(__file__).resolve().parents[1]
_MCP_PATH = REPO / "mcp" / "daily_invest_mcp.py"
if not _MCP_PATH.exists():
    pytest.skip("mcp/daily_invest_mcp.py not present", allow_module_level=True)
MCP_SRC = _MCP_PATH.read_text(encoding="utf-8")

# Minimal-Schema der trades-Tabelle (wie tests/test_quick_trade_journal.py).
_MINI_SCHEMA = """
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


def test_get_recommendations_uses_real_scan_keys():
    # Muss die echten Scan-Keys lesen ...
    assert 'data.get("scan_date")' in MCP_SRC
    assert 'data.get("top_setups"' in MCP_SRC
    assert 'data.get("hebel_watch"' in MCP_SRC
    # ... und die alten Phantom-Keys duerfen NICHT zurueck sein.
    assert 'data.get("recommendations"' not in MCP_SRC
    assert 'data.get("n_universe")' not in MCP_SRC


def test_get_journal_summary_disc_alias_and_parametrized():
    # K3: disc -> discretionary Alias vorhanden.
    assert '"disc": "discretionary"' in MCP_SRC
    # N8: parametrisiertes WHERE, kein f-string-Source mehr.
    assert "WHERE source = ?" in MCP_SRC
    assert "WHERE source = '{source}'" not in MCP_SRC


def test_discretionary_is_the_real_db_label(tmp_path):
    """Hermetisch: tools/quick_trade.py schreibt das DB-Label 'discretionary'
    (nie 'disc') -> 'discretionary' ist das korrekte Alias-Ziel fuer source='disc'."""
    import importlib.util
    db = tmp_path / "journal.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(_MINI_SCHEMA)
    con.commit()
    con.close()
    spec = importlib.util.spec_from_file_location(
        "quick_trade_mcp_test", REPO / "tools" / "quick_trade.py")
    qt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qt)
    qt.DB_PATH = db
    assert qt.buy("AAA", 1000.0, "2024-01-08", "discretionary")
    con = sqlite3.connect(str(db))
    try:
        labels = {r[0] for r in con.execute("SELECT DISTINCT source FROM trades")}
    finally:
        con.close()
    # Das Alias-Ziel muss existieren; 'disc' selbst darf NICHT als Label vorkommen.
    assert "discretionary" in labels
    assert "disc" not in labels
