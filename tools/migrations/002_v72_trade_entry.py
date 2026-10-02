#!/usr/bin/env python3
"""
SCHEMA-1: V7.2 Trade-Entry Migration.

Fuegt die fehlenden Spalten zu 'trades' hinzu, die das V7.2 Trade-Entry-Modal
im Cockpit schreibt, aber die DB bisher nicht kennt.

Spalten (trades):
  tp1              REAL     -- Take-Profit 1 (abs. Kurs)
  tp2              REAL     -- Take-Profit 2 (abs. Kurs)
  route            TEXT     -- DIRECT | HEBEL_KO | HEBEL_OS | FAKTOR (CHECK enforced in app-layer)
  hebel_factor     REAL     -- Faktor bei Hebelprodukten (NULL bei DIRECT)
  setup_fk         TEXT     -- Connors-SMA5 | Breakout | MeanRev | Insider | Congress | External | Custom
  regime_at_entry  TEXT     -- aus regime_snapshots beim Entry, Snapshot
  risk_eur         REAL     -- geplantes Risk in EUR (position * |entry-stop|)
  account_pct      REAL     -- Risk % Konto zum Zeitpunkt Entry
  max_hold_days    INTEGER  -- Time-Stop Default 5
  updated_at_utc   TEXT     -- ISO-Timestamp letzte Modifikation (BUY/SELL/UPDATE)

Benutzung:
  python3 002_v72_trade_entry.py --dry-run  (Vorschau, keine Aenderung)
  python3 002_v72_trade_entry.py --apply    (Migration ausfuehren, mit Backup)
  python3 002_v72_trade_entry.py --verify   (Post-Migration PRAGMA-Check)
  python3 002_v72_trade_entry.py --test     (Self-Test gegen tempdir)

Sicher: erstellt vor jedem Run Backup als trade_journal.sqlite.bak_<ts>.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DEFAULT = REPO_ROOT / "trade_journal.sqlite"

TRADES_NEW_COLS = [
    ("tp1",             "REAL"),
    ("tp2",             "REAL"),
    ("route",           "TEXT"),
    ("hebel_factor",    "REAL"),
    ("setup_fk",        "TEXT"),
    ("regime_at_entry", "TEXT"),
    ("risk_eur",        "REAL"),
    ("account_pct",     "REAL"),
    ("max_hold_days",   "INTEGER"),
    ("updated_at_utc",  "TEXT"),
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_trades_route    ON trades(route);",
    "CREATE INDEX IF NOT EXISTS idx_trades_setup_fk ON trades(setup_fk);",
]


def existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def plan_migration(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        cols = existing_columns(con, "trades")
        missing = [(n, t) for n, t in TRADES_NEW_COLS if n not in cols]
        return {
            "db": str(db_path),
            "trades_col_count": len(cols),
            "missing_trades_cols": missing,
            "actions_needed": len(missing),
        }
    finally:
        con.close()


def apply_migration(db_path: Path) -> dict:
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_suffix(f".sqlite.bak_{ts}")
    shutil.copy2(db_path, backup)

    con = sqlite3.connect(str(db_path))
    applied = []
    try:
        cur = con.cursor()
        cur.execute("BEGIN")
        cols = existing_columns(con, "trades")
        for name, typ in TRADES_NEW_COLS:
            if name not in cols:
                cur.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ};")
                applied.append(f"trades.{name}")
        # Indizes auch wenn Spalten schon existieren: idempotent wegen IF NOT EXISTS
        for ddl in INDEX_DDL:
            cur.execute(ddl)
            idx_name = ddl.split("IF NOT EXISTS")[1].split("ON")[0].strip()
            applied.append(f"idx:{idx_name}")
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
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
        )}
        needed_idx = {"idx_trades_route", "idx_trades_setup_fk"}
        idx_ok = needed_idx.issubset(idx)
        ok = (not missing) and idx_ok
        return {
            "ok": ok,
            "missing_cols": missing,
            "indexes_present": sorted(idx),
            "indexes_required_ok": idx_ok,
        }
    finally:
        con.close()


def _self_test() -> int:
    """Self-Test gegen tempdir: baut trades-Tabelle mit alten Spalten, prueft Migration."""
    import tempfile
    td = Path(tempfile.mkdtemp())
    db = td / "test.sqlite"
    con = sqlite3.connect(str(db))
    # minimal schema analog zur Live-DB
    con.execute("""CREATE TABLE trades(
        id INTEGER PRIMARY KEY,
        ticker TEXT,
        entry_price REAL,
        exit_price REAL,
        exit_reason TEXT
    )""")
    con.commit()
    con.close()

    plan = plan_migration(db)
    assert plan["actions_needed"] == 10, f"expected 10 missing cols, got {plan['actions_needed']}"

    res = apply_migration(db)
    added = [a for a in res["applied"] if a.startswith("trades.")]
    assert len(added) == 10, f"expected 10 ADD COLUMN, got {added}"
    idx_added = [a for a in res["applied"] if a.startswith("idx:")]
    assert len(idx_added) == 2, f"expected 2 indexes, got {idx_added}"

    v = verify(db)
    assert v["ok"], f"verify failed: {v}"
    assert not v["missing_cols"]
    assert v["indexes_required_ok"]

    # idempotent second run
    res2 = apply_migration(db)
    added2 = [a for a in res2["applied"] if a.startswith("trades.")]
    assert added2 == [], f"second run should re-add no cols, got {added2}"
    # Indizes sind idempotent (IF NOT EXISTS) aber erscheinen in applied-Liste;
    # wichtig: keine ALTER TABLE darin.

    # Smoke-Test: insert mit neuen Spalten klappt
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO trades(ticker, entry_price, tp1, tp2, route, hebel_factor, setup_fk, "
        "regime_at_entry, risk_eur, account_pct, max_hold_days, updated_at_utc) "
        "VALUES ('TEST', 100.0, 102.0, 105.0, 'DIRECT', NULL, 'MeanRev', "
        "'risk_on', 50.0, 0.005, 5, '2026-04-18T14:00:00Z')"
    )
    con.commit()
    row = con.execute(
        "SELECT ticker, tp1, tp2, route, setup_fk, max_hold_days FROM trades "
        "WHERE ticker='TEST'"
    ).fetchone()
    con.close()
    assert row == ('TEST', 102.0, 105.0, 'DIRECT', 'MeanRev', 5), f"insert row mismatch: {row}"

    print("002_v72_trade_entry self-test: PASS")
    shutil.rmtree(td)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SCHEMA-1 V7.2 Trade-Entry Migration")
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

    if args.dry_run:
        print(json.dumps(plan_migration(db_path), indent=2))
        return 0

    if args.apply:
        plan = plan_migration(db_path)
        if plan["actions_needed"] == 0:
            # Spalten da, Indizes koennen trotzdem fehlen: trotzdem sauber durchlaufen
            res = apply_migration(db_path)
            v = verify(db_path)
            print(json.dumps({"status": "partial_noop_cols_ok", "migration": res, "verification": v}, indent=2))
            return 0 if v["ok"] else 3
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
