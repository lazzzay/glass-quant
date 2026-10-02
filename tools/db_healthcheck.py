#!/usr/bin/env python3
"""
SQLITE-RECOVERY-1: SQLite Health-Check + Stale-Journal-Cleanup (2026-04-19).

Was dieses Script macht:
  1) Findet alle SQLite-DBs im Repo (trade_journal.sqlite, tools/oi_cache.sqlite, ...)
  2) Fuer jede DB:
     - Meldet Groesse, journal_mode, integrity_check
     - Entfernt verwaiste -journal / -wal / -shm-Dateien bei 0-Byte-DBs
     - Setzt WAL als persistenten journal_mode (falls DB schreibbar und >0 B)
  3) Gibt JSON-Report + Exit-Code (0=ok, 1=warn, 2=error)

Benutzung:
  python3 tools/db_healthcheck.py            # Dry-run: meldet Befunde, aendert nichts
  python3 tools/db_healthcheck.py --apply    # Cleanup + WAL-Switch ausfuehren
  python3 tools/db_healthcheck.py --apply --db trade_journal.sqlite  # Nur eine DB

WICHTIG: Vor --apply das Dashboard stoppen (kill $(pgrep -f dashboard_api.py)),
sonst blockiert Flask den WAL-Switch. Nach erfolgreichem Run: Dashboard restart.

Ist idempotent: Wiederholter Run ohne stale-Journals macht nichts.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Pfade, an denen die aktive Produktions-DBs liegen.
KNOWN_DBS = [
    REPO_ROOT / "trade_journal.sqlite",
    REPO_ROOT / "tools" / "oi_cache.sqlite",
]

SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def check_db(db_path: Path) -> dict:
    """Meldet Zustand einer DB + Sidecars."""
    result = {
        "db": str(db_path),
        "exists": db_path.exists(),
        "size_bytes": db_path.stat().st_size if db_path.exists() else None,
        "sidecars": {},
        "integrity": None,
        "journal_mode": None,
        "action_needed": [],
    }
    for suf in SIDECAR_SUFFIXES:
        sc = Path(str(db_path) + suf)
        if sc.exists():
            result["sidecars"][suf] = sc.stat().st_size

    if not result["exists"]:
        result["action_needed"].append("db-missing")
        return result

    # 0-Byte-DB mit Sidecars = stale-journal-Situation
    if result["size_bytes"] == 0 and result["sidecars"]:
        result["action_needed"].append("zero-byte-db-with-sidecars")
        return result

    # Integrity + journal_mode
    try:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            result["integrity"] = [r[0] for r in con.execute("PRAGMA integrity_check(1)")]
            result["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        result["integrity"] = [f"ERROR: {e}"]
        result["action_needed"].append("integrity-failed")
        return result

    # Empfehlung: WAL statt delete/memory
    if result["journal_mode"] not in ("wal",):
        result["action_needed"].append("switch-to-wal")

    # Altlasten: Wenn die DB erreichbar ist aber ein -journal > 0 Bytes neben
    # der DB liegt und die DB groesser als 0 ist, ist das Journal normalerweise
    # nach Commit leer. Ein nicht-leeres rollback-Journal nach Script-Ende
    # spricht fuer ungraceful shutdown. Wir melden es als Hinweis.
    if "-journal" in result["sidecars"] and result["sidecars"]["-journal"] > 0:
        result["action_needed"].append("non-empty-rollback-journal")

    return result


def apply_fixes(check: dict) -> dict:
    """Wendet Fixes basierend auf check_db-Befund an."""
    db_path = Path(check["db"])
    actions = []

    # 1) 0-Byte-DB mit Sidecars: Sidecars entfernen, DB neu anlegbar machen
    if "zero-byte-db-with-sidecars" in check["action_needed"]:
        for suf in SIDECAR_SUFFIXES:
            sc = Path(str(db_path) + suf)
            if sc.exists():
                try:
                    sc.unlink()
                    actions.append(f"removed-sidecar:{sc.name}")
                except OSError as e:
                    actions.append(f"FAILED-remove:{sc.name} ({e})")
        # Leere DB-Datei ebenfalls entfernen (wird bei naechstem Connect neu angelegt)
        try:
            if db_path.stat().st_size == 0:
                db_path.unlink()
                actions.append(f"removed-zerobyte-db:{db_path.name}")
        except OSError as e:
            actions.append(f"FAILED-remove-db:{db_path.name} ({e})")
        return {"actions": actions}

    if not check["exists"]:
        return {"actions": ["skipped:db-missing"]}

    # 2) Non-empty rollback-Journal (nach Graceful-Shutdown sollte es leer sein)
    if "non-empty-rollback-journal" in check["action_needed"]:
        # Vor Loeschen pruefen: DB muss clean recoverbar sein
        try:
            con = sqlite3.connect(str(db_path), timeout=10.0)
            ok = [r[0] for r in con.execute("PRAGMA integrity_check(1)")]
            con.close()
            if ok == ["ok"]:
                j = Path(str(db_path) + "-journal")
                if j.exists() and j.stat().st_size == 0:
                    j.unlink()
                    actions.append(f"removed-empty-journal:{j.name}")
                # Ein nicht-leeres Journal NICHT automatisch entfernen: das waere destruktiv.
        except sqlite3.DatabaseError as e:
            actions.append(f"skipped-journal-cleanup:integrity={e}")

    # 3) WAL-Switch
    if "switch-to-wal" in check["action_needed"]:
        try:
            con = sqlite3.connect(str(db_path), timeout=15.0)
            try:
                mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                con.execute("PRAGMA synchronous=NORMAL")
                actions.append(f"journal_mode->{mode}")
                # Trigger Checkpoint so dass die Modus-Umstellung persistent ist
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                actions.append("wal-checkpoint:truncate")
            finally:
                con.close()
        except sqlite3.OperationalError as e:
            actions.append(f"FAILED-wal-switch:{e}")

    return {"actions": actions}


def cleanup_orphan_journals(root: Path) -> list:
    """Findet und markiert orphaned Journals von .bak-Dateien."""
    orphans = []
    for f in root.glob("*.sqlite*-journal"):
        base = Path(str(f).removesuffix("-journal"))
        if not base.exists():
            orphans.append({"file": str(f), "reason": "base-missing"})
        elif base.stat().st_size == 0:
            orphans.append({"file": str(f), "reason": "base-zero-bytes"})
        elif ".bak" in base.name or ".manual_bak" in base.name:
            orphans.append({"file": str(f), "reason": "backup-file-has-journal"})
    return orphans


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite Health-Check + Stale-Journal-Cleanup")
    ap.add_argument("--apply", action="store_true", help="Fixes anwenden (sonst nur dry-run)")
    ap.add_argument("--db", action="append", default=None,
                    help="Eine spezifische DB pruefen (Pfad relativ zu Repo-Root). "
                         "Mehrfach angebbar. Default: trade_journal.sqlite + tools/oi_cache.sqlite")
    args = ap.parse_args()

    dbs = [REPO_ROOT / p for p in args.db] if args.db else KNOWN_DBS

    report = {"dbs": [], "orphan_journals": [], "applied": args.apply}
    exit_code = 0

    for db in dbs:
        check = check_db(db)
        if args.apply and check["action_needed"] and "db-missing" not in check["action_needed"]:
            check["apply"] = apply_fixes(check)
            # Re-check nach Fix
            check["post_check"] = {
                k: v for k, v in check_db(db).items()
                if k in ("size_bytes", "sidecars", "integrity", "journal_mode", "action_needed")
            }
        report["dbs"].append(check)
        if check["action_needed"] and "switch-to-wal" not in check["action_needed"]:
            # switch-to-wal ist ein WARN, kein ERROR
            exit_code = max(exit_code, 1)
        if "integrity-failed" in check["action_needed"]:
            exit_code = 2

    report["orphan_journals"] = cleanup_orphan_journals(REPO_ROOT)
    if args.apply:
        removed = []
        for o in report["orphan_journals"]:
            try:
                Path(o["file"]).unlink()
                removed.append(o["file"])
            except OSError as e:
                o["remove_error"] = str(e)
        report["orphan_journals_removed"] = removed

    print(json.dumps(report, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
