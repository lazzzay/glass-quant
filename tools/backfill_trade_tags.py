#!/usr/bin/env python3
"""
backfill_trade_tags.py: V7.8 · Trade-Tagging-Backfill (BACKFILL-TAGS-1)

Zweck
-----
Die V7.6-Phase-3 /stats/expectancy-Pivot (Setup x Regime x Expectancy) lebt
von den trades-Spalten `setup_fk` und `regime_at_entry`. Typical DB state
    before the backfill: imported trades (e.g. the V6.1 backtest corpus) with
    setup_fk=NULL and regime_at_entry=NULL. Die UI zeigt deshalb eine leere
    Pivot-Tabelle mit "tagge beim Close"-Hinweis statt Substanz.

Dieses Tool fuellt beide Spalten deterministisch anhand bereits vorhandener
Zeilen-Daten nach. Es aendert KEIN Scoring und KEINE Pipeline-Logik.

Heuristik
---------
setup_fk:
    system_version = 'V6.1'                   -> mr_v61
    system_version ~ 'discretionary'|'manual' -> discretionary
    system_version = 'momo_break'|'momentum'  -> breakout  (Shadow-Only bisher)
    system_version = 'pead'                   -> pead
    system_version = 'catalyst'               -> catalyst
    system_version LIKE 'hebel%'              -> hebel
    alles andere                              -> other

    Optional: existierendes premarket_setup oder hard_exit_tag kann den
    Default ueberschreiben (z. B. premarket_setup='PEAD_BUY' -> pead).

regime_at_entry:
    1) COALESCE(regime, '')  (die legacy-Spalte ist fuer Backtest-Trades
       bereits vollstaendig befuellt, inkl. Trending / Trending-Divergent /
       Neutral / Bullish-Confirmed / Bullish-Divergent / High-Vol-Krise /
       Extreme-Fear-Thrust).
    2) Fallback: regime_<entry_date>.json Archiv (tools/regime_YYYY-MM-DD.json
       oder top-level regime_YYYY-MM-DD.json).
    3) Wenn weder 1 noch 2 trefferstark -> None (bleibt NULL, wird in der
       Pivot-Tabelle in die (none)-Zelle laufen und in Totals transparent.)

Flags
-----
    --db PATH                Pfad zur trade_journal.sqlite (Default: Repo-Root)
    --regime-dir PATH        Verzeichnis mit regime_*.json (Default: tools/)
    --apply                  Schreibt die Aenderungen tatsaechlich in die DB
                             (ohne dieses Flag: Dry-Run, nur Vorschau)
    --backup PATH            Zielpfad fuer die vorherige Backup-Kopie
                             (Default: trade_journal.sqlite.bak_<ts>_backfill)
    --no-backup              Backup ueberspringen (nur fuer Tests empfohlen)
    --source backtest        Optionaler Source-Filter (nur diese Source wird
                             gebackfillt; Default: alle)
    --limit N                Max. Anzahl Trades, die veraendert werden
    --fill-exit-date         OPT-IN: exit_date = entry_date + holding_days
                             (nur fuer Rows mit exit_price AND holding_days
                             AND exit_date IS NULL). Ohne dieses Flag wird
                             exit_date NICHT angefasst.
    --test                   Selftest (keine DB-Touches), Exit-Code 0/1
    --verbose                Zeigt Row-by-Row-Diff

Sicherheit
----------
 * Dry-Run default: ohne --apply wird nichts geschrieben.
 * Backup vor --apply: Shutil.copy2 auf sqlite + wal + shm.
 * Transaktional: alle UPDATEs in einer einzigen Transaktion.
 * Idempotent: ueberschreibt NIE bereits belegte setup_fk oder regime_at_entry.
 * Solo-Author: keine Tool-Signaturen, kein AI-Hint in den Daten.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "trade_journal.sqlite"
DEFAULT_REGIME_DIR = REPO_ROOT / "tools"

# Canonical setup_fk-Werte deckungsgleich mit dem PostMortemModal
# (dashboard/v7/index.html -> pm-setup select).
VALID_SETUPS = {
    "mr_v61", "breakout", "pead", "catalyst", "hebel",
    "discretionary", "other",
}


# ---------------------------------------------------------------- heuristics

def _infer_setup_fk(system_version: Optional[str],
                    premarket_setup: Optional[str],
                    hard_exit_tag: Optional[str]) -> str:
    """Abbildung system_version -> setup_fk mit leichtem Override durch
    premarket_setup (PEAD_BUY / BREAKOUT_BUY / CATALYST_BUY)."""
    sv = (system_version or "").strip().lower()
    pre = (premarket_setup or "").strip().lower()

    # Premarket-Hints ueberschreiben, falls eindeutig.
    if "pead" in pre:
        return "pead"
    if "breakout" in pre or "momo" in pre or "momentum" in pre:
        return "breakout"
    if "catalyst" in pre or "event" in pre:
        return "catalyst"

    if sv in ("v6", "v6.0", "v6.1"):
        return "mr_v61"
    if sv.startswith("mr"):
        return "mr_v61"
    if sv.startswith("momo") or sv in ("breakout", "momentum"):
        return "breakout"
    if sv.startswith("pead"):
        return "pead"
    if sv.startswith("catalyst"):
        return "catalyst"
    if sv.startswith("hebel"):
        return "hebel"
    if "discretionary" in sv or "manual" in sv:
        return "discretionary"
    return "other"


def _load_regime_archive(regime_dir: Path) -> Dict[str, str]:
    """Baut Date(str) -> regime(str) Map aus tools/regime_*.json + Repo-Root."""
    cache: Dict[str, str] = {}
    candidates = []
    if regime_dir.exists():
        candidates.extend(sorted(regime_dir.glob("regime_*.json")))
    root_dir = regime_dir.parent if regime_dir.name == "tools" else regime_dir
    if root_dir.exists():
        candidates.extend(sorted(root_dir.glob("regime_*.json")))
    for p in candidates:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        dt = d.get("date") or ""
        reg = d.get("regime") or ""
        if dt and reg and dt not in cache:
            cache[dt] = reg
    return cache


def _infer_regime_at_entry(legacy_regime: Optional[str],
                           entry_date: Optional[str],
                           archive: Dict[str, str]) -> Optional[str]:
    if legacy_regime and legacy_regime.strip():
        return legacy_regime.strip()
    if entry_date and entry_date in archive:
        return archive[entry_date]
    return None


# ---------------------------------------------------------------- planning

@dataclass
class RowDiff:
    trade_id: int
    ticker: str
    entry_date: str
    old_setup_fk: Optional[str]
    new_setup_fk: Optional[str]
    old_regime_at_entry: Optional[str]
    new_regime_at_entry: Optional[str]
    old_exit_date: Optional[str] = None
    new_exit_date: Optional[str] = None

    @property
    def setup_changed(self) -> bool:
        return (self.old_setup_fk or "") != (self.new_setup_fk or "") and self.new_setup_fk is not None

    @property
    def regime_changed(self) -> bool:
        return (self.old_regime_at_entry or "") != (self.new_regime_at_entry or "") and self.new_regime_at_entry is not None

    @property
    def exit_date_changed(self) -> bool:
        return (self.old_exit_date or "") != (self.new_exit_date or "") and self.new_exit_date is not None

    @property
    def any_change(self) -> bool:
        return self.setup_changed or self.regime_changed or self.exit_date_changed


def _derive_exit_date(entry_date: Optional[str],
                      holding_days: Optional[int],
                      exit_price: Optional[float]) -> Optional[str]:
    """exit_date = entry_date + holding_days (calendar days), nur wenn
    exit_price gesetzt ist und holding_days >= 0. Return None sonst."""
    if not entry_date or holding_days is None or exit_price is None:
        return None
    try:
        d = datetime.strptime(entry_date[:10], "%Y-%m-%d")
    except Exception:
        return None
    try:
        hd = int(holding_days)
    except Exception:
        return None
    if hd < 0:
        return None
    from datetime import timedelta
    return (d + timedelta(days=hd)).strftime("%Y-%m-%d")


def plan_backfill(con: sqlite3.Connection,
                  archive: Dict[str, str],
                  source_filter: Optional[str] = None,
                  limit: Optional[int] = None,
                  fill_exit_date: bool = False) -> list[RowDiff]:
    where = []
    params: list = []
    if source_filter:
        where.append("source = ?")
        params.append(source_filter)
    # Nur Zeilen anfassen, bei denen mindestens eine Zielspalte leer ist
    # (setup_fk ODER regime_at_entry ODER: falls --fill-exit-date, exit_date).
    base = ("(setup_fk IS NULL OR TRIM(setup_fk)='' "
            "OR regime_at_entry IS NULL OR TRIM(regime_at_entry)='')")
    if fill_exit_date:
        base = "(" + base + " OR (exit_date IS NULL OR TRIM(exit_date)=''))"
    where.append(base)
    sql = """
        SELECT id, ticker, entry_date, system_version, regime,
               setup_fk, regime_at_entry,
               premarket_setup, hard_exit_tag,
               exit_date, exit_price, holding_days
          FROM trades
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = con.execute(sql, params).fetchall()

    diffs: list[RowDiff] = []
    for r in rows:
        (tid, ticker, entry_date, sv, legacy_regime,
         setup_fk, regime_at_entry, pre_setup, hard_exit,
         exit_date, exit_price, holding_days) = r
        new_setup = setup_fk if (setup_fk and setup_fk.strip()) else _infer_setup_fk(sv, pre_setup, hard_exit)
        new_regime = regime_at_entry if (regime_at_entry and regime_at_entry.strip()) \
                      else _infer_regime_at_entry(legacy_regime, entry_date, archive)
        new_exit_date: Optional[str] = None
        if fill_exit_date and (not exit_date or not str(exit_date).strip()):
            new_exit_date = _derive_exit_date(entry_date, holding_days, exit_price)
        diffs.append(RowDiff(
            trade_id=tid, ticker=ticker or "", entry_date=entry_date or "",
            old_setup_fk=setup_fk, new_setup_fk=new_setup,
            old_regime_at_entry=regime_at_entry, new_regime_at_entry=new_regime,
            old_exit_date=exit_date, new_exit_date=new_exit_date,
        ))
    return diffs


def summarize(diffs: Iterable[RowDiff]) -> dict:
    diffs = list(diffs)
    n_total = len(diffs)
    n_setup = sum(1 for d in diffs if d.setup_changed)
    n_regime = sum(1 for d in diffs if d.regime_changed)
    n_exit = sum(1 for d in diffs if d.exit_date_changed)
    n_any = sum(1 for d in diffs if d.any_change)
    by_setup: Dict[str, int] = {}
    by_regime: Dict[str, int] = {}
    for d in diffs:
        if d.setup_changed:
            by_setup[d.new_setup_fk or "(none)"] = by_setup.get(d.new_setup_fk or "(none)", 0) + 1
        if d.regime_changed:
            by_regime[d.new_regime_at_entry or "(none)"] = by_regime.get(d.new_regime_at_entry or "(none)", 0) + 1
    return {
        "n_total": n_total,
        "n_setup_changed": n_setup,
        "n_regime_changed": n_regime,
        "n_exit_date_changed": n_exit,
        "n_any_changed": n_any,
        "by_new_setup_fk": dict(sorted(by_setup.items(), key=lambda x: -x[1])),
        "by_new_regime_at_entry": dict(sorted(by_regime.items(), key=lambda x: -x[1])),
    }


# ---------------------------------------------------------------- apply

def apply_backfill(con: sqlite3.Connection, diffs: Iterable[RowDiff]) -> int:
    n = 0
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
    # updated_at_utc-Spalte mit patchen (Audit-Trail fuer spaetere Review)
    has_updated_col = any(r[1] == "updated_at_utc"
                          for r in con.execute("PRAGMA table_info(trades)"))
    cur = con.cursor()
    cur.execute("BEGIN")
    try:
        for d in diffs:
            if not d.any_change:
                continue
            sets, params = [], []
            if d.setup_changed:
                sets.append("setup_fk = ?")
                params.append(d.new_setup_fk)
            if d.regime_changed:
                sets.append("regime_at_entry = ?")
                params.append(d.new_regime_at_entry)
            if d.exit_date_changed:
                sets.append("exit_date = ?")
                params.append(d.new_exit_date)
            if has_updated_col:
                sets.append("updated_at_utc = ?")
                params.append(now_utc)
            params.append(d.trade_id)
            cur.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id = ?", params)
            n += cur.rowcount
        con.commit()
    except Exception:
        con.rollback()
        raise
    return n


def backup_db(db_path: Path, out_path: Optional[Path] = None) -> Path:
    if out_path is None:
        ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
        out_path = db_path.with_name(f"{db_path.name}.bak_{ts}_backfill")
    shutil.copy2(db_path, out_path)
    # WAL + SHM mitsichern, falls vorhanden
    for suffix in ("-wal", "-shm"):
        aux = db_path.with_name(db_path.name + suffix)
        if aux.exists():
            shutil.copy2(aux, out_path.with_name(out_path.name + suffix))
    return out_path


# ---------------------------------------------------------------- self-test

def run_selftest() -> int:
    errs: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    # Heuristik-Tabelle
    check(_infer_setup_fk("V6.1", None, None) == "mr_v61", "V6.1 -> mr_v61")
    check(_infer_setup_fk("V6", None, None) == "mr_v61", "V6 -> mr_v61")
    check(_infer_setup_fk("discretionary-manual", None, None) == "discretionary",
          "discretionary-manual -> discretionary")
    check(_infer_setup_fk("momo_break", None, None) == "breakout",
          "momo_break -> breakout")
    check(_infer_setup_fk("pead_v1", None, None) == "pead", "pead_v1 -> pead")
    check(_infer_setup_fk("catalyst", None, None) == "catalyst", "catalyst -> catalyst")
    check(_infer_setup_fk("hebel-ko", None, None) == "hebel", "hebel-ko -> hebel")
    check(_infer_setup_fk("v99", None, None) == "other", "unknown -> other")
    # premarket_setup-Override
    check(_infer_setup_fk("V6.1", "PEAD_BUY", None) == "pead",
          "premarket PEAD_BUY ueberschreibt V6.1 -> pead")
    check(_infer_setup_fk("V6.1", "BREAKOUT_BUY", None) == "breakout",
          "premarket BREAKOUT_BUY ueberschreibt V6.1 -> breakout")

    # regime: legacy gewinnt, sonst archive-hit, sonst None
    arch = {"2024-04-16": "Trending"}
    check(_infer_regime_at_entry("Neutral", "2024-04-16", arch) == "Neutral",
          "legacy regime gewinnt")
    check(_infer_regime_at_entry(None, "2024-04-16", arch) == "Trending",
          "archive fallback trifft")
    check(_infer_regime_at_entry(None, "2099-01-01", arch) is None,
          "ohne Daten -> None")
    check(_infer_regime_at_entry("", "2024-04-16", arch) == "Trending",
          "leerer legacy wird archive-fallback")

    # Planning + summarize auf einer Mini-In-Memory-DB
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE trades(
        id INTEGER PRIMARY KEY, source TEXT, system_version TEXT, ticker TEXT,
        entry_date TEXT, regime TEXT, setup_fk TEXT, regime_at_entry TEXT,
        premarket_setup TEXT, hard_exit_tag TEXT,
        exit_date TEXT, exit_price REAL, holding_days INTEGER,
        updated_at_utc TEXT
    )""")
    rows = [
        (1, "backtest", "V6.1", "AAPL", "2024-02-01", "Trending", None, None, None, None, None, None, None, None),
        (2, "backtest", "V6.1", "MSFT", "2024-02-05", "Neutral", None, None, "PEAD_BUY", None, None, None, None, None),
        (3, "discretionary", "discretionary-manual", "ZZZ", "2024-04-16", None, None, None, None, None, None, None, None, None),
        (4, "backtest", "V6.1", "KEEP", "2024-03-01", "Trending", "mr_v61", "Trending", None, None, None, None, None, None),
    ]
    con.executemany("""INSERT INTO trades
        (id,source,system_version,ticker,entry_date,regime,setup_fk,regime_at_entry,
         premarket_setup,hard_exit_tag,exit_date,exit_price,holding_days,updated_at_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    con.commit()

    diffs = plan_backfill(con, archive={"2024-04-16": "Neutral"})
    check(len(diffs) == 3, f"plan_backfill liefert 3 Kandidaten, nicht {len(diffs)} (Reihe 4 ist fertig)")
    by_id = {d.trade_id: d for d in diffs}
    check(by_id[1].new_setup_fk == "mr_v61", "Row1 -> mr_v61")
    check(by_id[1].new_regime_at_entry == "Trending", "Row1 regime -> legacy")
    check(by_id[2].new_setup_fk == "pead", "Row2 PEAD_BUY -> pead (override)")
    check(by_id[3].new_setup_fk == "discretionary", "Row3 -> discretionary")
    check(by_id[3].new_regime_at_entry == "Neutral", "Row3 regime -> archive fallback")

    summ = summarize(diffs)
    check(summ["n_setup_changed"] == 3, f"3 setup changes, got {summ['n_setup_changed']}")
    check(summ["n_regime_changed"] == 3, f"3 regime changes, got {summ['n_regime_changed']}")
    check("mr_v61" in summ["by_new_setup_fk"], "by_new_setup_fk contains mr_v61")

    # apply_backfill auf Memory-DB
    n = apply_backfill(con, diffs)
    check(n == 3, f"apply aktualisiert 3 Rows, nicht {n}")
    leftover = con.execute("""SELECT COUNT(*) FROM trades
                              WHERE setup_fk IS NULL OR regime_at_entry IS NULL""").fetchone()[0]
    check(leftover == 0, f"nach apply sollte kein NULL mehr sein, leftover={leftover}")
    r4 = con.execute("SELECT setup_fk, regime_at_entry FROM trades WHERE id=4").fetchone()
    check(r4 == ("mr_v61", "Trending"), f"Row4 unveraendert (idempotent), got {r4}")

    # Idempotenz-Zweiter-Lauf
    diffs2 = plan_backfill(con, archive={})
    check(len(diffs2) == 0, f"zweiter Lauf liefert 0 Kandidaten, nicht {len(diffs2)}")

    # ---- exit_date-Derivation (opt-in) ----
    check(_derive_exit_date("2024-02-01", 3, 10.5) == "2024-02-04",
          "exit_date = entry_date + holding_days")
    check(_derive_exit_date("2024-02-01", 0, 10.5) == "2024-02-01",
          "holding_days=0 -> same-day-exit")
    check(_derive_exit_date("2024-02-01", 3, None) is None,
          "ohne exit_price -> None")
    check(_derive_exit_date(None, 3, 10.5) is None,
          "ohne entry_date -> None")
    check(_derive_exit_date("2024-02-01", None, 10.5) is None,
          "ohne holding_days -> None")
    check(_derive_exit_date("2024-02-01", -1, 10.5) is None,
          "negative holding_days -> None")

    # exit_date plan auf In-Memory-DB
    con2 = sqlite3.connect(":memory:")
    con2.execute("""CREATE TABLE trades(
        id INTEGER PRIMARY KEY, source TEXT, system_version TEXT, ticker TEXT,
        entry_date TEXT, regime TEXT, setup_fk TEXT, regime_at_entry TEXT,
        premarket_setup TEXT, hard_exit_tag TEXT,
        exit_date TEXT, exit_price REAL, holding_days INTEGER,
        updated_at_utc TEXT
    )""")
    con2.executemany("""INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
        (1, "backtest", "V6.1", "A", "2024-02-01", "Trending",
         "mr_v61", "Trending", None, None, None, 10.5, 3, None),
        (2, "backtest", "V6.1", "B", "2024-02-10", "Neutral",
         "mr_v61", "Neutral", None, None, "2024-02-15", 22.0, 5, None),
        (3, "backtest", "V6.1", "C", "2024-03-01", "Trending",
         "mr_v61", "Trending", None, None, None, None, 4, None),  # kein exit_price
    ])
    con2.commit()
    # Ohne --fill-exit-date: keine Aenderungen
    d_noexit = plan_backfill(con2, archive={}, fill_exit_date=False)
    check(len(d_noexit) == 0, f"ohne --fill-exit-date sollen 0 Kandidaten uebrig sein, got {len(d_noexit)}")
    # Mit --fill-exit-date: Row1 bekommt 2024-02-04, Row3 nicht (kein exit_price)
    d_exit = plan_backfill(con2, archive={}, fill_exit_date=True)
    ids = {d.trade_id: d for d in d_exit}
    check(set(ids.keys()) == {1, 3}, f"fill_exit_date muss Row1+Row3 listen, got {sorted(ids)}")
    check(ids[1].new_exit_date == "2024-02-04", f"Row1 exit_date -> 2024-02-04, got {ids[1].new_exit_date}")
    check(ids[3].new_exit_date is None, f"Row3 ohne exit_price bleibt None, got {ids[3].new_exit_date}")
    n = apply_backfill(con2, d_exit)
    # Nur Row1 schreibt tatsaechlich (Row3 hat any_change=False)
    check(n == 1, f"apply darf nur 1 Zeile geschrieben haben, got {n}")
    r1 = con2.execute("SELECT exit_date FROM trades WHERE id=1").fetchone()
    check(r1[0] == "2024-02-04", f"Row1 exit_date persisted 2024-02-04, got {r1}")
    con2.close()

    con.close()

    if errs:
        print("SELFTEST FAIL:")
        for e in errs:
            print("  -", e)
        return 1
    print("backfill_trade_tags self-test: PASS (incl. exit_date-Derivation)")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="V7.8 Trade-Tagging-Backfill")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--regime-dir", default=str(DEFAULT_REGIME_DIR))
    ap.add_argument("--apply", action="store_true",
                    help="Schreibt die Aenderungen (Dry-Run ohne dieses Flag)")
    ap.add_argument("--backup", default=None)
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--source", default=None,
                    help="Nur Trades dieser Source backfillen (z. B. 'backtest')")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fill-exit-date", action="store_true",
                    help="Opt-in: exit_date = entry_date + holding_days, "
                         "nur wenn exit_price gesetzt und exit_date leer.")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args(argv)

    if args.test:
        return run_selftest()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERR: DB nicht gefunden: {db_path}")
        return 2

    archive = _load_regime_archive(Path(args.regime_dir))
    print(f"Regime-Archiv: {len(archive)} Datums-Eintraege aus {args.regime_dir}")

    con = sqlite3.connect(str(db_path))
    try:
        diffs = plan_backfill(con, archive,
                              source_filter=args.source, limit=args.limit,
                              fill_exit_date=args.fill_exit_date)
        summ = summarize(diffs)
        print("")
        print(f"Kandidaten (mind. eine Leerstelle): {summ['n_total']}")
        print(f"   setup_fk-Updates:         {summ['n_setup_changed']}")
        print(f"   regime_at_entry-Updates:  {summ['n_regime_changed']}")
        print(f"   exit_date-Updates:        {summ['n_exit_date_changed']}"
              + (" (--fill-exit-date)" if args.fill_exit_date else " (opt-out)"))
        print(f"   Zeilen mit >=1 Aenderung: {summ['n_any_changed']}")
        if summ["by_new_setup_fk"]:
            print("   setup_fk-Breakdown:")
            for k, v in summ["by_new_setup_fk"].items():
                print(f"      {k:<14s} {v}")
        if summ["by_new_regime_at_entry"]:
            print("   regime-Breakdown (Top-6):")
            for i, (k, v) in enumerate(summ["by_new_regime_at_entry"].items()):
                if i >= 6:
                    break
                print(f"      {k:<22s} {v}")
        if args.verbose:
            print("")
            for d in diffs[:25]:
                print(f"   #{d.trade_id:>4} {d.ticker:<8s} {d.entry_date} "
                      f"setup_fk: {d.old_setup_fk or '-':<14s} -> {d.new_setup_fk or '-':<14s}  "
                      f"regime: {d.old_regime_at_entry or '-':<22s} -> {d.new_regime_at_entry or '-':<22s}")
            if len(diffs) > 25:
                print(f"   ... +{len(diffs)-25} weitere")

        if not args.apply:
            print("")
            print("DRY-RUN abgeschlossen. Re-run mit --apply, um Aenderungen zu schreiben.")
            return 0

        # ---- apply path ----
        if not args.no_backup:
            backup_path = Path(args.backup) if args.backup else None
            bpath = backup_db(db_path, backup_path)
            print(f"Backup: {bpath}")

        n = apply_backfill(con, diffs)
        con.execute("PRAGMA wal_checkpoint(FULL)")
        print(f"APPLY OK. Zeilen geschrieben: {n}")

        # Post-Check: wie viele Trades sind jetzt voll getaggt?
        row = con.execute("""SELECT
            SUM(CASE WHEN setup_fk IS NOT NULL AND TRIM(setup_fk)<>'' THEN 1 ELSE 0 END),
            SUM(CASE WHEN regime_at_entry IS NOT NULL AND TRIM(regime_at_entry)<>'' THEN 1 ELSE 0 END),
            SUM(CASE WHEN exit_date IS NOT NULL AND TRIM(exit_date)<>'' THEN 1 ELSE 0 END),
            COUNT(*) FROM trades""").fetchone()
        print(f"Post-Check: setup_fk {row[0]}/{row[3]} · regime_at_entry {row[1]}/{row[3]} · exit_date {row[2]}/{row[3]}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
