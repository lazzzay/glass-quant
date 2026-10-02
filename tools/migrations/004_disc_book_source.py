#!/usr/bin/env python3
"""
SCHEMA-3 / DISC-BOOK-2: Source-Spalte in trades (2026-04-19).

Kontext:
Die Live-DB hat bereits eine 'source'-Spalte aus fruehen Dev-Tagen (alle 440
historischen Trades haben source='backtest'). Was fehlt: 'source_note' als
Freitext-Feld fuer Discretionary-Zusatzinformation (z. B. "externes Signal,
Quelle X"). Ausserdem etabliert diese Migration die Konvention fuer Live-Trades:

  source='backtest'      -> importierte Backtest-Rows (unveraendert)
  source='systematic'    -> V6/V6.1 Live-Pipeline (kuenftig, Post-Freeze)
  source='discretionary' -> manuelle, diskretionaere Picks
  source='external'      -> Signale aus einer externen/zweiten Signalquelle
  source='other'         -> Catch-all

Der Backfill ist ein No-Op bei Rows, die bereits ein source haben (die 440
Backtest-Trades bleiben unberuehrt). Nur wirklich NULL-Rows werden auf
'systematic' gesetzt.

Spalten (trades):
  source_note    TEXT      -- freier Notizfeld-String (z. B. "externes Signal, Quelle X")

Index:
  idx_trades_source  ON trades(source)

Backfill:
  UPDATE trades SET source='systematic' WHERE source IS NULL;

Benutzung:
  python3 004_disc_book_source.py --dry-run
  python3 004_disc_book_source.py --apply
  python3 004_disc_book_source.py --verify
  python3 004_disc_book_source.py --test

Sicher: erstellt vor jedem Run Backup als trade_journal.sqlite.bak_<ts>.

Cohort-Filter-Kompatibilitaet:
- trade_journal.py cohort_audit() filtert default auf source='systematic'
  (aber: Migration beruehrt keinen Python-Code, Cohort-Filter muss in KW17
  nachgezogen werden, Ticket DISC-BOOK-3).
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
    # 'source' existiert bereits in der Live-DB -> hier nicht aufgefuehrt.
    # Die Migration laeuft trotzdem sauber, weil existing_columns() bestehende
    # Spalten ausfiltert. Falls source fehlt (Test-DB), wird sie oben im
    # apply_migration()-Pfad mitgelegt.
    ("source_note", "TEXT"),
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source);",
]

VALID_SOURCES = ("backtest", "systematic", "discretionary", "external", "other")


def existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def plan_migration(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        cols = existing_columns(con, "trades")
        missing = [(n, t) for n, t in TRADES_NEW_COLS if n not in cols]
        n_trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        n_null_source = 0
        if "source" in cols:
            n_null_source = con.execute(
                "SELECT COUNT(*) FROM trades WHERE source IS NULL"
            ).fetchone()[0]
        return {
            "db": str(db_path),
            "trades_col_count": len(cols),
            "missing_trades_cols": missing,
            "actions_needed": len(missing),
            "n_trades": n_trades,
            "rows_to_backfill_source": n_null_source if "source" in cols else n_trades,
        }
    finally:
        con.close()


def apply_migration(db_path: Path) -> dict:
    ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_suffix(f".sqlite.bak_{ts}")
    shutil.copy2(db_path, backup)

    con = sqlite3.connect(str(db_path))
    applied = []
    backfilled = 0
    try:
        cur = con.cursor()
        cur.execute("BEGIN")
        cols = existing_columns(con, "trades")
        # Safety-Net: falls 'source' fehlt (Test-DB), mit-migrieren
        if "source" not in cols:
            cur.execute("ALTER TABLE trades ADD COLUMN source TEXT;")
            applied.append("trades.source")
            cols.add("source")
        for name, typ in TRADES_NEW_COLS:
            if name not in cols:
                cur.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ};")
                applied.append(f"trades.{name}")
        for ddl in INDEX_DDL:
            cur.execute(ddl)
            idx_name = ddl.split("IF NOT EXISTS")[1].split("ON")[0].strip()
            applied.append(f"idx:{idx_name}")
        # Backfill: NUR Rows mit NULL -> systematic.
        # Bereits vorhandene Werte (z. B. 'backtest') werden NIE veraendert.
        cur.execute(
            "UPDATE trades SET source='systematic' WHERE source IS NULL"
        )
        backfilled = cur.rowcount
        applied.append(f"backfill:source<-systematic ({backfilled} rows)")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return {"backup": str(backup), "applied": applied, "backfilled": backfilled}


def verify(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    try:
        cols = existing_columns(con, "trades")
        missing = [n for n, _ in TRADES_NEW_COLS if n not in cols]
        if "source" not in cols:
            missing.append("source")
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
        )}
        needed_idx = {"idx_trades_source"}
        idx_ok = needed_idx.issubset(idx)
        n_null = 0
        by_source = {}
        unknown_sources = []
        if "source" in cols:
            n_null = con.execute(
                "SELECT COUNT(*) FROM trades WHERE source IS NULL"
            ).fetchone()[0]
            for row in con.execute(
                "SELECT COALESCE(source,'(null)'), COUNT(*) FROM trades GROUP BY source"
            ):
                by_source[row[0]] = row[1]
                if row[0] != '(null)' and row[0] not in VALID_SOURCES:
                    unknown_sources.append(row[0])
        ok = (not missing) and idx_ok and n_null == 0 and not unknown_sources
        return {
            "ok": ok,
            "missing_cols": missing,
            "indexes_present": sorted(idx),
            "indexes_required_ok": idx_ok,
            "rows_with_null_source": n_null,
            "rows_by_source": by_source,
            "unknown_source_values": unknown_sources,
            "valid_source_values": list(VALID_SOURCES),
        }
    finally:
        con.close()


def _self_test() -> int:
    """Self-Test gegen tempdir."""
    import tempfile
    td = Path(tempfile.mkdtemp())
    db = td / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE trades(
        id INTEGER PRIMARY KEY,
        ticker TEXT,
        entry_price REAL,
        exit_price REAL,
        exit_reason TEXT
    )""")
    # 3 bestehende Rows
    con.executemany(
        "INSERT INTO trades(ticker, entry_price, exit_price) VALUES (?, ?, ?)",
        [("AAPL", 100.0, 110.0), ("MSFT", 200.0, 195.0), ("NVDA", 300.0, None)],
    )
    con.commit()
    con.close()

    plan = plan_migration(db)
    # Nur source_note fehlt in TRADES_NEW_COLS (source wird in apply safety-net'd)
    assert plan["actions_needed"] == 1, f"expected 1 missing col in TRADES_NEW_COLS, got {plan['actions_needed']}"
    assert plan["n_trades"] == 3
    assert plan["rows_to_backfill_source"] == 3

    res = apply_migration(db)
    added = [a for a in res["applied"] if a.startswith("trades.")]
    # source (safety-net) + source_note = 2 ADD-COLUMN
    assert len(added) == 2, f"expected 2 ADD COLUMN (source safety-net + source_note), got {added}"
    assert res["backfilled"] == 3, f"expected 3 backfilled, got {res['backfilled']}"

    v = verify(db)
    assert v["ok"], f"verify failed: {v}"
    assert v["rows_with_null_source"] == 0
    assert v["rows_by_source"].get("systematic") == 3

    # Idempotenz: zweiter Lauf soll 0 ADD-COLUMN und 0 Backfill liefern
    res2 = apply_migration(db)
    added2 = [a for a in res2["applied"] if a.startswith("trades.")]
    assert added2 == [], f"second run should re-add no cols, got {added2}"
    assert res2["backfilled"] == 0

    # Smoke-Test: external-Insert klappt (synthetisches Beispiel)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO trades(ticker, entry_price, exit_price, source, source_note) "
        "VALUES ('EXT1', 0, 30, 'external', 'external signal example')"
    )
    con.commit()
    row = con.execute(
        "SELECT source, source_note, exit_price FROM trades WHERE ticker='EXT1'"
    ).fetchone()
    con.close()
    assert row == ('external', 'external signal example', 30.0), f"mismatch: {row}"

    # Filter-Smoke: nur systematic
    con = sqlite3.connect(str(db))
    n_sys = con.execute(
        "SELECT COUNT(*) FROM trades WHERE source='systematic'"
    ).fetchone()[0]
    n_ext = con.execute(
        "SELECT COUNT(*) FROM trades WHERE source='external'"
    ).fetchone()[0]
    con.close()
    assert n_sys == 3 and n_ext == 1, f"filter mismatch: sys={n_sys}, ext={n_ext}"

    # Verify akzeptiert 'external' als VALID_SOURCE
    v2 = verify(db)
    assert v2["ok"], f"post-external-insert verify failed: {v2}"
    assert not v2["unknown_source_values"], f"unexpected unknown sources: {v2['unknown_source_values']}"

    print("004_disc_book_source self-test: PASS")
    shutil.rmtree(td)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SCHEMA-3 DISC-BOOK-2 Source-Spalte Migration")
    p.add_argument("--db", default=str(DB_DEFAULT), help="Pfad zu trade_journal.sqlite")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--test", action="store_true")
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
        if plan["actions_needed"] == 0 and plan["rows_to_backfill_source"] == 0:
            v = verify(db_path)
            print(json.dumps({"status": "noop", "verification": v}, indent=2))
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
