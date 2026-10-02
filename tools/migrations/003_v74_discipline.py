#!/usr/bin/env python3
"""
SCHEMA-2: V7.4 Discipline-Layer Migration.

Fuegt fehlende Spalten zu 'trades' hinzu und legt 'discipline_overrides' an.

Spalten (trades):
  thesis             TEXT    -- Kurzthese beim Entry
  pre_trade_check    TEXT    -- JSON-Serialisierung der 5-Punkt-Checklist
  override_reason    TEXT    -- Pflicht-Begruendung bei Guardrail-Override
  verdict            TEXT    -- Post-Mortem: ok | loss_ok | edge_fail | discipline_fail
  lesson             TEXT    -- Post-Mortem-Lerneintrag
  thesis_outcome     TEXT    -- confirmed | violated | partial (V7.4.1)

Tabelle discipline_overrides:
  id INTEGER PRIMARY KEY AUTOINCREMENT
  trade_id INTEGER NOT NULL REFERENCES trades(id)
  guardrail TEXT NOT NULL         -- revenge | cooldown | overtrade | fomo
  reason TEXT NOT NULL            -- Pflicht-Begruendung
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP

Benutzung:
  python3 003_v74_discipline.py --dry-run  (Vorschau, keine Aenderung)
  python3 003_v74_discipline.py --apply    (Migration ausfuehren, mit Backup)
  python3 003_v74_discipline.py --verify   (Post-Migration PRAGMA-Check)

Sicher: erstellt vor jedem Run Backup als trade_journal.sqlite.bak_<ts>.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DEFAULT = REPO_ROOT / "trade_journal.sqlite"

TRADES_NEW_COLS = [
    ("thesis",          "TEXT"),
    ("pre_trade_check", "TEXT"),
    ("override_reason", "TEXT"),
    ("verdict",         "TEXT"),
    ("lesson",          "TEXT"),
    ("thesis_outcome",  "TEXT"),
]

OVERRIDES_DDL = """
CREATE TABLE IF NOT EXISTS discipline_overrides (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL,
    guardrail  TEXT    NOT NULL CHECK (guardrail IN ('revenge','cooldown','overtrade','fomo','other')),
    reason     TEXT    NOT NULL,
    ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);
"""

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_overrides_trade ON discipline_overrides(trade_id);",
    "CREATE INDEX IF NOT EXISTS idx_overrides_ts    ON discipline_overrides(ts);",
]


def existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def plan_migration(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        cols = existing_columns(con, "trades")
        missing = [(n, t) for n, t in TRADES_NEW_COLS if n not in cols]
        overrides = table_exists(con, "discipline_overrides")
        return {
            "db": str(db_path),
            "existing_trades_cols": sorted(cols),
            "missing_trades_cols": missing,
            "discipline_overrides_exists": overrides,
            "actions_needed": len(missing) + (0 if overrides else 1),
        }
    finally:
        con.close()


def apply_migration(db_path: Path) -> dict:
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_suffix(f".sqlite.bak_{ts}")
    shutil.copy2(db_path, backup)

    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON;")
    applied = []
    try:
        cur = con.cursor()
        cur.execute("BEGIN")
        cols = existing_columns(con, "trades")
        for name, typ in TRADES_NEW_COLS:
            if name not in cols:
                cur.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ};")
                applied.append(f"trades.{name}")
        if not table_exists(con, "discipline_overrides"):
            cur.execute(OVERRIDES_DDL)
            applied.append("discipline_overrides (table)")
            for ddl in INDEX_DDL:
                cur.execute(ddl)
                applied.append(ddl.split()[5])  # index name
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return {"backup": str(backup), "applied": applied}


def verify(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        cols = existing_columns(con, "trades")
        missing = [n for n, _ in TRADES_NEW_COLS if n not in cols]
        overrides_ok = table_exists(con, "discipline_overrides")
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        ok = (not missing) and overrides_ok
        return {
            "ok": ok,
            "missing_cols": missing,
            "overrides_table_exists": overrides_ok,
            "indexes": sorted(idx),
        }
    finally:
        con.close()


def _self_test() -> int:
    """In-memory test against temporary DB."""
    import tempfile
    td = Path(tempfile.mkdtemp())
    db = td / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE trades(
        id INTEGER PRIMARY KEY, ticker TEXT, entry_price REAL,
        exit_price REAL, exit_reason TEXT)""")
    con.commit()
    con.close()

    plan = plan_migration(db)
    assert plan["actions_needed"] == 7, f"expected 6 cols + 1 table = 7 actions, got {plan['actions_needed']}"
    assert not plan["discipline_overrides_exists"]

    res = apply_migration(db)
    assert len([a for a in res["applied"] if a.startswith("trades.")]) == 6
    assert any("discipline_overrides" in a for a in res["applied"])

    v = verify(db)
    assert v["ok"], f"verification failed: {v}"
    assert not v["missing_cols"]
    assert v["overrides_table_exists"]

    # idempotent
    res2 = apply_migration(db)
    assert res2["applied"] == [], f"second run should be no-op, got {res2['applied']}"

    # FK works
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("INSERT INTO trades(id, ticker) VALUES (1, 'AAPL')")
    con.execute(
        "INSERT INTO discipline_overrides(trade_id, guardrail, reason) "
        "VALUES (1, 'fomo', 'late entry despite cooldown')"
    )
    con.commit()
    row = con.execute("SELECT guardrail, reason FROM discipline_overrides").fetchone()
    con.close()
    assert row == ("fomo", "late entry despite cooldown")

    print("003_v74_discipline self-test: PASS")
    shutil.rmtree(td)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SCHEMA-2 V7.4 Discipline-Migration")
    p.add_argument("--db", default=str(DB_DEFAULT), help="Pfad zu trade_journal.sqlite")
    p.add_argument("--dry-run", action="store_true", help="Nur Plan ausgeben")
    p.add_argument("--apply", action="store_true", help="Migration ausfuehren (inkl. Backup)")
    p.add_argument("--verify", action="store_true", help="PRAGMA-Check nach Run")
    p.add_argument("--test", action="store_true", help="Self-Test (tempdir)")
    args = p.parse_args()

    if args.test:
        return _self_test()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB nicht gefunden: {db_path}", file=sys.stderr)
        return 2

    import json

    if args.dry_run:
        print(json.dumps(plan_migration(db_path), indent=2))
        return 0

    if args.apply:
        plan = plan_migration(db_path)
        if plan["actions_needed"] == 0:
            print(json.dumps({"status": "noop", "reason": "alles bereits vorhanden"}, indent=2))
            return 0
        res = apply_migration(db_path)
        v = verify(db_path)
        print(json.dumps({"migration": res, "verification": v}, indent=2))
        return 0 if v["ok"] else 3

    if args.verify:
        print(json.dumps(verify(db_path), indent=2))
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
