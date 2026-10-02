#!/usr/bin/env python3
"""
V6.1 Trade Journal (SQLite)
- Initialisiert Schema
- Importiert die 440 Backtest-Trades aus v6_backtest_results_v61.csv als Startbestand
- Bietet CLI zum Hinzufügen neuer Live-Trades

Benutzung:
    python3 trade_journal.py init          # Erstellt trade_journal.sqlite + importiert Backtest
    python3 trade_journal.py add --ticker AAA --entry_date 2024-01-08 ...
    python3 trade_journal.py list --open   # Offene Positionen
    python3 trade_journal.py stats         # Aggregierte KPIs
"""
import sqlite3, csv, argparse, sys, os
from datetime import datetime

DB_PATH = os.environ.get("TRADE_JOURNAL_DB",
    os.path.join(os.path.dirname(__file__), "..", "trade_journal.sqlite"))
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "v6_backtest_results_v61.csv")

# DISC-BOOK-3 (2026-04-19): Default-Cohort-Filter.
# Valide Sources in der Live-DB: backtest, systematic, discretionary, external, other.
# Historisches 'live' wird als Alias fuer 'systematic' mitgefuehrt (falls alte Rows existieren).
_VALID_SOURCES = ("backtest", "systematic", "discretionary", "external", "other", "live")
_DEFAULT_LIVE_SOURCES = ("systematic", "live")  # V6.1 Live = systematic; 'live' = legacy alias


def _source_where(args) -> tuple[str, str]:
    """
    Liefert (sql_fragment, label) fuer Cohort-/List-/Close-Queries.

    --include-all       => leerer Filter, alle sources
    --source X[,Y,...]  => explizite Liste (kommasepariert, z. B. "external,discretionary")
    (default)           => source IN ('systematic','live')  (V6.1-Systematic + Legacy)

    sql_fragment enthaelt bereits das fuehrende 'AND ' bzw. 'WHERE ', je nach Caller;
    wir geben NUR den Source-Teil inkl. Klammer zurueck, Caller haengen an.
    """
    if getattr(args, "include_all", False):
        return ("", "ALL-SOURCES")
    src = getattr(args, "source", None)
    if src:
        parts = [s.strip() for s in src.split(",") if s.strip()]
        unknown = [p for p in parts if p not in _VALID_SOURCES]
        if unknown:
            print(f"WARN: unbekannte source-Werte: {unknown} (valid: {_VALID_SOURCES})", file=sys.stderr)
        quoted = ",".join("'" + p.replace("'", "''") + "'" for p in parts)
        return (f"source IN ({quoted})", f"source={','.join(parts)}")
    quoted = ",".join("'" + s + "'" for s in _DEFAULT_LIVE_SOURCES)
    return (f"source IN ({quoted})", f"source={'+'.join(_DEFAULT_LIVE_SOURCES)} [default]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- 'backtest' | 'live'
    system_version TEXT NOT NULL,      -- 'V6' | 'V6.1' ...
    ticker TEXT NOT NULL,
    name TEXT,
    tier TEXT,                         -- A | B | C | X
    sector TEXT,
    regime TEXT,
    entry_date TEXT NOT NULL,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    holding_days INTEGER,
    hard_exit_tag TEXT,                -- entry + 3 business days
    atr_pct REAL,
    atr_mult REAL,
    sl_pct REAL,
    tp_pct REAL,
    v6_score REAL,
    triple_rsi REAL,
    rsi2 REAL, rsi5 REAL, rsi14 REAL,
    position_size REAL,
    brutto_pnl REAL,
    cost REAL DEFAULT 2.0,             -- round-trip 1€ × 2
    netto_pnl REAL,
    return_pct REAL,
    exit_reason TEXT,                  -- connors_sma5 | atr_tp | atr_sl | max_hold | day3_flat | skipped_tier_x | rule_violation
    is_win INTEGER,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_entry_date ON trades(entry_date);
CREATE INDEX IF NOT EXISTS idx_source ON trades(source);
CREATE INDEX IF NOT EXISTS idx_exit_reason ON trades(exit_reason);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    regime TEXT NOT NULL,
    confidence REAL,
    size_multiplier REAL,
    vix REAL, adx_spy REAL, spy_5d REAL, yield_now REAL,
    notes TEXT,
    active_vetos TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rule_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    violation_date TEXT NOT NULL,
    rule TEXT NOT NULL,
    description TEXT,
    cost_estimate REAL,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

-- V6.5 equity-curve snapshots (pro Handelstag ein Eintrag nach Abend-Tracking)
CREATE TABLE IF NOT EXISTS daily_equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    equity REAL NOT NULL,
    realized_pnl_cum REAL DEFAULT 0,
    open_positions INTEGER DEFAULT 0,
    source TEXT DEFAULT 'live',
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_equity_date ON daily_equity(date);
"""

# V6.5 Schema-Migrations (idempotent ALTER TABLEs). Python-Seite: try/except pro Spalte.
V65_MIGRATIONS = [
    ("trades", "sector_rs_score",    "REAL"),
    ("trades", "gics_sector",        "TEXT"),
    ("trades", "sector_regime_tag",  "TEXT"),
    ("trades", "cum_rsi2_3d",        "REAL"),
    ("trades", "pullback_strength_3d","INTEGER"),
    ("trades", "connors_quality",    "INTEGER"),
    ("trades", "premarket_gap_pct",  "REAL"),
    ("trades", "premarket_setup",    "TEXT"),
    ("trades", "r_multiple",         "REAL"),     # realized / planned risk
]

# V6.6 E8 Multi-Timeframe-Spalten
V66_MIGRATIONS = [
    ("trades", "weekly_trend",   "TEXT"),   # bullish/neutral/bearish/unknown
    ("trades", "weekly_rsi14",   "REAL"),
    ("trades", "h1_rsi2",        "REAL"),
    ("trades", "h1_state",       "TEXT"),   # primed/neutral/overheated/unknown
    ("trades", "mtf_tag",        "TEXT"),   # "{weekly}|{h1}"
    ("trades", "mtf_adjust",     "REAL"),   # applied pre_score delta
]

# V6.7 E7F Insider/Congress-Spalten
V67_MIGRATIONS = [
    ("trades", "insider_score",    "REAL"),   # composite -2..+2
    ("trades", "insider_adjust",   "REAL"),   # applied pre_score delta
    ("trades", "insider_breakdown","TEXT"),   # JSON-as-text
]

# V6.8 E10F Options-Flow-Spalten
V68_MIGRATIONS = [
    ("trades", "options_flow_score",    "REAL"),   # composite -2..+2
    ("trades", "options_flow_adjust",   "REAL"),   # applied pre_score delta
    ("trades", "flow_tag",              "TEXT"),   # bullish_flow/mild_bullish/neutral/mild_bearish/bearish_flow/no_data
    ("trades", "market_pcr_tag",        "TEXT"),   # neutral/defensive_market/euphoric_market/unknown
    ("trades", "market_pcr_adjust",     "REAL"),   # global PCR tilt (±0.10)
]


def run_v65_migrations(conn):
    """Idempotente Spalten-Erweiterung für V6.5 + V6.6 + V6.7 + V6.8 (Journal-Upgrade)."""
    for table, col, col_type in V65_MIGRATIONS + V66_MIGRATIONS + V67_MIGRATIONS + V68_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Spalte existiert bereits
    conn.commit()


def connect():
    """
    Oeffnet die SQLite-Verbindung. Zwei-Stufen-Strategie (SQLITE-RECOVERY-1, 2026-04-19):

    1) Primaer: WAL (Write-Ahead-Log). Multi-Reader + Single-Writer parallel,
       crash-safe, kein Lock-Kampf zwischen Flask-Dashboard und CLI.
    2) Fallback: MEMORY. Falls WAL auf dem Filesystem nicht zulaessig ist
       (read-only FS / Sandbox ohne File-Delete), kein Filesystem-Journal.

    Zusaetzlich vor dem Connect: ein veraltetes rollback-Journal wird invalidiert,
    falls die DB leer/fast-leer ist (0-Byte-DB + orphaned Journal = Crash-Schleife).

    **Pragmas fuer robuste Koexistenz von Flask + CLI + Scheduled-Tasks:**
    - busy_timeout = 15000 ms (wartet statt sofort 'database is locked' zu werfen)
    - synchronous  = NORMAL    (WAL-safe, merklich schneller als FULL)
    - foreign_keys = ON        (konsistente Relationen zu Journal-Events-Tabelle)
    """
    db_path = DB_PATH
    journal_path = db_path + "-journal"

    # Recovery: orphaned Journal bei fast-leerer DB invalidieren
    try:
        if (os.path.exists(journal_path)
                and os.path.exists(db_path)
                and os.path.getsize(db_path) < 4096):
            with open(journal_path, "r+b") as jf:
                jf.seek(0)
                jf.write(b"\x00" * 8)
    except OSError:
        pass  # Best-effort

    conn = sqlite3.connect(db_path, timeout=15.0)
    # Koexistenz-Pragmas zuerst, damit sie auch im MEMORY-Fallback greifen
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # WAL versuchen; bei unsupported Filesystem auf MEMORY zurueckfallen
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode and str(mode).lower() != "wal":
            # SQLite hat das Umschalten verweigert (z. B. read-only FS) -> MEMORY
            conn.execute("PRAGMA journal_mode=MEMORY")
    except sqlite3.OperationalError:
        conn.execute("PRAGMA journal_mode=MEMORY")
    return conn


def cmd_init(args):
    conn = connect()
    conn.executescript(SCHEMA)
    run_v65_migrations(conn)
    conn.commit()
    print(f"DB initialized: {DB_PATH} (V6.5 schema)")

    # Import backtest if empty
    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE source='backtest'")
    if cur.fetchone()[0] > 0 and not args.force:
        print("Backtest already imported. Use --force to re-import.")
        return

    if args.force:
        conn.execute("DELETE FROM trades WHERE source='backtest'")

    with open(CSV_PATH) as f:
        r = csv.DictReader(f)
        rows = list(r)

    def ff(v):
        try: return float(v)
        except: return None

    def fi(v):
        try: return int(v)
        except: return None

    imported = 0
    for row in rows:
        conn.execute("""
            INSERT INTO trades (source, system_version, ticker, name, tier, sector, regime,
                entry_date, entry_price, exit_date, exit_price, holding_days, atr_pct, atr_mult,
                sl_pct, tp_pct, v6_score, triple_rsi, rsi2, rsi5, rsi14,
                brutto_pnl, cost, netto_pnl, return_pct, exit_reason, is_win, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            'backtest', 'V6.1',
            row['ticker'], row.get('name'), row.get('tier'), row.get('sector'), row.get('regime'),
            row['entry'][:10] if 'entry' in row and '-' in row.get('entry','') else row['date'],
            ff(row.get('entry')), None, ff(row.get('exit')),
            fi(row.get('holding_days')),
            ff(row.get('atr_pct')), ff(row.get('v61_atr_mult')),
            ff(row.get('sl_pct')), ff(row.get('tp_pct')),
            ff(row.get('v6_score')), ff(row.get('triple_rsi')),
            ff(row.get('rsi2')), ff(row.get('rsi5')), ff(row.get('rsi14')),
            ff(row.get('brutto_pnl')), 2.0,
            ff(row.get('v61_netto_pnl')),
            ff(row.get('return_pct')),
            row.get('v61_exit_reason'),
            1 if row.get('is_win')=='True' else 0,
            f"v6_exit={row.get('exit_reason')} v61_skip={row.get('v61_skip')}"
        ))
        imported += 1
    conn.commit()
    print(f"Imported {imported} backtest trades.")


def cmd_add(args):
    conn = connect()
    run_v65_migrations(conn)
    conn.execute("""
        INSERT INTO trades (source, system_version, ticker, name, tier, entry_date, entry_price,
                            hard_exit_tag, position_size, v6_score, regime, notes, sector,
                            sector_rs_score, gics_sector, sector_regime_tag,
                            cum_rsi2_3d, pullback_strength_3d, connors_quality,
                            premarket_gap_pct, premarket_setup,
                            atr_pct, atr_mult, sl_pct, tp_pct, triple_rsi, rsi2, rsi5, rsi14,
                            weekly_trend, weekly_rsi14, h1_rsi2, h1_state, mtf_tag, mtf_adjust,
                            insider_score, insider_adjust, insider_breakdown,
                            options_flow_score, options_flow_adjust, flow_tag, market_pcr_tag, market_pcr_adjust)
        VALUES ('systematic', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?)
    """, (args.system_version, args.ticker, args.name, args.tier, args.entry_date, args.entry_price,
          args.hard_exit, args.size, args.score, args.regime, args.notes, args.sector,
          args.sector_rs_score, args.gics_sector, args.sector_regime_tag,
          args.cum_rsi2_3d, args.pullback_strength_3d, args.connors_quality,
          args.premarket_gap_pct, args.premarket_setup,
          args.atr_pct, args.atr_mult, args.sl_pct, args.tp_pct,
          args.triple_rsi, args.rsi2, args.rsi5, args.rsi14,
          args.weekly_trend, args.weekly_rsi14, args.h1_rsi2, args.h1_state, args.mtf_tag, args.mtf_adjust,
          args.insider_score, args.insider_adjust, args.insider_breakdown,
          args.options_flow_score, args.options_flow_adjust, args.flow_tag,
          args.market_pcr_tag, args.market_pcr_adjust))
    conn.commit()
    print(f"Added systematic live trade: {args.ticker} @ {args.entry_price} entry {args.entry_date} ({args.system_version})")


def cmd_close(args):
    """Schließt einen offenen Trade mit Exit-Preis, -Datum, -Reason und berechnet pnl/r_multiple."""
    conn = connect()
    run_v65_migrations(conn)

    if args.trade_id:
        cur = conn.execute("SELECT id, entry_date, entry_price, position_size, sl_pct, atr_pct "
                           "FROM trades WHERE id=? AND exit_date IS NULL", (args.trade_id,))
    else:
        # Juengster offener Live-Trade mit dem Ticker (systematic + legacy 'live')
        cur = conn.execute("SELECT id, entry_date, entry_price, position_size, sl_pct, atr_pct "
                           "FROM trades WHERE ticker=? AND source IN ('systematic','live') "
                           "AND exit_date IS NULL "
                           "ORDER BY entry_date DESC LIMIT 1", (args.ticker,))
    row = cur.fetchone()
    if not row:
        print(f"FEHLER: Kein offener Trade gefunden "
              f"({'id='+str(args.trade_id) if args.trade_id else 'ticker='+args.ticker})", file=sys.stderr)
        sys.exit(1)

    trade_id, entry_date, entry_price, size, sl_pct, atr_pct = row
    # Position-Size Fallback: wenn None, nimm default 500 für Anzeige; r_multiple dann nicht berechenbar
    size = size or 500.0

    # P&L-Berechnung (long only in diesem System)
    qty = size / entry_price if entry_price else 0
    brutto_pnl = (args.exit_price - entry_price) * qty
    cost = args.cost if args.cost is not None else 2.0  # flat 1€ × 2 (round-trip), via --cost aenderbar
    netto_pnl = brutto_pnl - cost
    return_pct = (args.exit_price - entry_price) / entry_price * 100 if entry_price else None

    # Holding-Days (kalendertageweise, pragmatisch)
    try:
        d_entry = datetime.strptime(entry_date[:10], "%Y-%m-%d")
        d_exit = datetime.strptime(args.exit_date[:10], "%Y-%m-%d")
        holding_days = (d_exit - d_entry).days
    except Exception:
        holding_days = None

    # R-Multiple: realized / initial risk in € (abs(entry - SL) * qty)
    r_multiple = None
    if sl_pct:
        risk_per_share = entry_price * (abs(sl_pct) / 100.0)
        risk_total = risk_per_share * qty
        if risk_total > 0:
            r_multiple = round(netto_pnl / risk_total, 3)

    is_win = 1 if netto_pnl > 0 else 0

    conn.execute("""
        UPDATE trades
        SET exit_date=?, exit_price=?, exit_reason=?, holding_days=?,
            brutto_pnl=?, cost=?, netto_pnl=?, return_pct=?, is_win=?, r_multiple=?
        WHERE id=?
    """, (args.exit_date, args.exit_price, args.exit_reason, holding_days,
          round(brutto_pnl, 2), cost, round(netto_pnl, 2),
          round(return_pct, 3) if return_pct is not None else None,
          is_win, r_multiple, trade_id))
    conn.commit()

    print(f"CLOSED trade #{trade_id}: entry {entry_date}@{entry_price} → "
          f"exit {args.exit_date}@{args.exit_price}  "
          f"brutto={brutto_pnl:+.2f}  netto={netto_pnl:+.2f}  "
          f"r={r_multiple if r_multiple is not None else 'n/a'}  reason={args.exit_reason}")


def cmd_equity(args):
    """Schreibt/aktualisiert einen Tages-Equity-Snapshot."""
    conn = connect()
    run_v65_migrations(conn)
    # Upsert
    conn.execute("""
        INSERT INTO daily_equity (date, equity, realized_pnl_cum, open_positions, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            equity=excluded.equity,
            realized_pnl_cum=excluded.realized_pnl_cum,
            open_positions=excluded.open_positions,
            notes=excluded.notes
    """, (args.date, args.equity, args.realized_pnl, args.open_positions, args.notes))
    conn.commit()
    print(f"Equity-Snapshot {args.date}: {args.equity:.2f} €  realized_cum={args.realized_pnl:+.2f}  "
          f"open={args.open_positions}")


def cmd_list(args):
    conn = connect()
    sql = "SELECT id, entry_date, ticker, tier, entry_price, exit_date, netto_pnl, exit_reason, source FROM trades"
    params = []
    # --live ist Legacy-Alias fuer 'source IN (systematic,live)' -> neu ueber _source_where.
    # Wenn weder --live noch --source noch --include-all gesetzt ist, zeigt list() weiterhin
    # *alle* Rows (Backwards-kompatibles Default fuer reine Inventar-Anzeige).
    filter_active = args.live or getattr(args, "include_all", False) or getattr(args, "source", None)
    where = []
    if args.open:
        where.append("exit_date IS NULL")
    if filter_active:
        src_sql, _ = _source_where(args)
        if src_sql:
            where.append(src_sql)
    elif args.open:
        # --open ohne --include-all: per Default nur systematic+live
        where.append("source IN ('systematic','live')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY entry_date DESC LIMIT ?"
    params.append(args.limit)
    for row in conn.execute(sql, params):
        print(" | ".join(str(x) if x is not None else "-" for x in row))


def cmd_stats(args):
    conn = connect()
    q = conn.execute("""
        SELECT source, COUNT(*), SUM(is_win), ROUND(SUM(netto_pnl),2),
               ROUND(AVG(netto_pnl),2)
        FROM trades
        WHERE netto_pnl IS NOT NULL
        GROUP BY source
    """)
    print(f"{'source':12s} {'trades':>7s} {'wins':>6s} {'netto':>10s} {'avg':>8s}")
    for row in q:
        print(f"{row[0]:12s} {row[1]:>7d} {row[2] or 0:>6d} {row[3] or 0:>10.2f} {row[4] or 0:>8.2f}")

    print("\n--- Per Exit Reason (all sources) ---")
    for row in conn.execute("""
        SELECT exit_reason, COUNT(*), SUM(is_win), ROUND(SUM(netto_pnl),2)
        FROM trades WHERE netto_pnl IS NOT NULL
        GROUP BY exit_reason ORDER BY SUM(netto_pnl) DESC
    """):
        print(f"  {row[0] or '?':20s}: {row[1]:4d} trades, wins {row[2] or 0:3d}, netto {row[3] or 0:+8.2f}€")

    print("\n--- Top 5 Ticker (Netto) ---")
    for row in conn.execute("""
        SELECT ticker, COUNT(*), ROUND(SUM(netto_pnl),2)
        FROM trades WHERE netto_pnl IS NOT NULL
        GROUP BY ticker ORDER BY SUM(netto_pnl) DESC LIMIT 5
    """):
        print(f"  {row[0]:10s}: {row[1]:3d}tr  {row[2]:+8.2f}€")


def cmd_cohort(args):
    """
    V6.8 Observation-Window Cohort-Audit.
    Vergleicht Live-Trades mit/ohne jedes V6.6/6.7/6.8-Signal und meldet WR + Netto-PnL-Delta.

    DISC-BOOK-3 (2026-04-19): Default-Filter ist source IN ('systematic','live').
    Mit --include-all werden auch backtest/external/discretionary mitgerechnet.
    Mit --source=external,discretionary laesst sich gezielt eine andere Kohorte auswerten.
    """
    conn = connect()
    run_v65_migrations(conn)

    src_sql, src_label = _source_where(args)
    where_base = "netto_pnl IS NOT NULL"
    if src_sql:
        where_base = src_sql + " AND " + where_base
    params: list = []
    # --since/--until als SQL-Parameter (keine String-Interpolation von CLI-Input)
    if args.since:
        where_base += " AND entry_date >= ?"
        params.append(args.since)
    if args.until:
        where_base += " AND entry_date <= ?"
        params.append(args.until)

    def q(where_extra: str, label: str):
        sql = f"""SELECT COUNT(*), SUM(is_win), ROUND(AVG(netto_pnl),2), ROUND(SUM(netto_pnl),2),
                         ROUND(AVG(r_multiple),2)
                  FROM trades WHERE {where_base} {where_extra}"""
        row = conn.execute(sql, params).fetchone()
        n = row[0] or 0
        wins = row[1] or 0
        wr = (wins / n * 100) if n > 0 else 0.0
        return {"label": label, "n": n, "wins": wins, "wr": wr,
                "avg_netto": row[2] or 0.0, "sum_netto": row[3] or 0.0,
                "avg_r": row[4] or 0.0}

    cohorts = [
        # Baseline
        q("", "ALL TRADES"),
        # V6.6 MTF
        q("AND weekly_trend='bullish' AND h1_state='primed'", "V6.6 MTF bullish|primed (A-Setup)"),
        q("AND weekly_trend='bullish'",                        "V6.6 Weekly-Trend=bullish"),
        q("AND weekly_trend='bearish'",                        "V6.6 Weekly-Trend=bearish (Edge-Case)"),
        # V6.7 Insider
        q("AND insider_score >= 2",                            "V6.7 Insider Cluster-Buy (score>=+2)"),
        q("AND insider_score >= 1",                            "V6.7 Insider Bullish (score>=+1)"),
        q("AND insider_score <= -1",                           "V6.7 Insider Selling (score<=-1, SKIP-Override)"),
        # V6.8 Options-Flow
        q("AND options_flow_score >= 2",                       "V6.8 Options Bullish-Flow (score>=+2)"),
        q("AND options_flow_score >= 1",                       "V6.8 Options mild_bullish+ (score>=+1)"),
        q("AND options_flow_score <= -1",                      "V6.8 Options Bearish (score<=-1, SKIP-Override)"),
        q("AND market_pcr_tag='defensive_market'",             "V6.8 Markt-PCR defensive (PCR>=1.20)"),
        q("AND market_pcr_tag='euphoric_market'",              "V6.8 Markt-PCR euphoric (PCR<=0.70)"),
        # Confluence
        q("AND weekly_trend='bullish' AND h1_state='primed' AND insider_score >= 1",
          "CONFLUENCE Triple (MTF-A + Insider>=+1)"),
        q("AND weekly_trend='bullish' AND h1_state='primed' AND insider_score >= 1 AND options_flow_score >= 1",
          "CONFLUENCE Quadruple (MTF-A + Insider>=+1 + Flow>=+1)"),
        q("AND weekly_trend='bullish' AND h1_state='primed' AND insider_score >= 2 AND options_flow_score >= 2 AND sector_rs_score >= 0.5",
          "CONFLUENCE MAX (alle 4 Hebel strong)"),
    ]

    print(f"\n=== V6.8 COHORT-AUDIT ({args.since or 'all'} → {args.until or 'now'}) "
          f"[{src_label}] ===\n")
    print(f"{'Cohort':<60s} {'N':>4s} {'WR%':>6s} {'AvgPnL':>9s} {'SumPnL':>10s} {'AvgR':>6s}")
    for c in cohorts:
        if c["n"] == 0:
            print(f"{c['label']:<60s} {0:>4d}   n/a       n/a        n/a     n/a")
            continue
        print(f"{c['label']:<60s} {c['n']:>4d} {c['wr']:>5.1f}% "
              f"{c['avg_netto']:>+8.2f}€ {c['sum_netto']:>+9.2f}€ {c['avg_r']:>+5.2f}")

    # Alpha-Deltas vs Baseline
    baseline = cohorts[0]
    if baseline["n"] > 0:
        print(f"\n--- Alpha-Delta vs Baseline (WR={baseline['wr']:.1f}%, AvgR={baseline['avg_r']:.2f}) ---")
        for c in cohorts[1:]:
            if c["n"] < 3:
                continue
            d_wr = c["wr"] - baseline["wr"]
            d_r  = c["avg_r"] - baseline["avg_r"]
            flag = "✅" if d_wr > 5 and d_r > 0.05 else ("⚠️" if d_wr < -5 else " ")
            print(f"  {flag} {c['label']:<55s}  ΔWR={d_wr:+5.1f}%  ΔAvgR={d_r:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add")
    p.add_argument("--ticker", required=True); p.add_argument("--name", default="")
    p.add_argument("--tier", default="B"); p.add_argument("--entry_date", required=True)
    p.add_argument("--entry_price", type=float, required=True)
    p.add_argument("--hard_exit", required=True, help="Entry + 3 business days")
    p.add_argument("--size", type=float, default=500.0)
    p.add_argument("--score", type=float, default=7.5)
    p.add_argument("--regime", default="Neutral"); p.add_argument("--notes", default="")
    p.add_argument("--system_version", default="V6.8")
    p.add_argument("--sector", default=None)
    p.add_argument("--sector_rs_score", type=float, default=None)
    p.add_argument("--gics_sector", default=None)
    p.add_argument("--sector_regime_tag", default=None)
    p.add_argument("--cum_rsi2_3d", type=float, default=None)
    p.add_argument("--pullback_strength_3d", type=int, default=None)
    p.add_argument("--connors_quality", type=int, default=None)
    p.add_argument("--premarket_gap_pct", type=float, default=None)
    p.add_argument("--premarket_setup", default=None)
    p.add_argument("--atr_pct", type=float, default=None)
    p.add_argument("--atr_mult", type=float, default=None)
    p.add_argument("--sl_pct", type=float, default=None)
    p.add_argument("--tp_pct", type=float, default=None)
    p.add_argument("--triple_rsi", type=float, default=None)
    p.add_argument("--rsi2", type=float, default=None)
    p.add_argument("--rsi5", type=float, default=None)
    p.add_argument("--rsi14", type=float, default=None)
    # V6.6 E8 Multi-Timeframe
    p.add_argument("--weekly_trend", default=None, help="bullish/neutral/bearish/unknown")
    p.add_argument("--weekly_rsi14", type=float, default=None)
    p.add_argument("--h1_rsi2", type=float, default=None)
    p.add_argument("--h1_state", default=None, help="primed/neutral/overheated/unknown")
    p.add_argument("--mtf_tag", default=None, help="'{weekly}|{h1}'")
    p.add_argument("--mtf_adjust", type=float, default=None, help="applied pre_score delta from MTF")
    # V6.7 E7F Insider/Congress
    p.add_argument("--insider_score", type=float, default=None, help="composite -2..+2")
    p.add_argument("--insider_adjust", type=float, default=None, help="applied pre_score delta")
    p.add_argument("--insider_breakdown", default=None, help="JSON-as-text breakdown (e.g. '{\"cluster_buy\":\"+2\"}')")
    # V6.8 E10F Options-Flow
    p.add_argument("--options_flow_score", type=float, default=None, help="composite -2..+2")
    p.add_argument("--options_flow_adjust", type=float, default=None, help="applied pre_score delta (max ±0.20)")
    p.add_argument("--flow_tag", default=None, help="bullish_flow/mild_bullish/neutral/mild_bearish/bearish_flow/no_data")
    p.add_argument("--market_pcr_tag", default=None, help="neutral/defensive_market/euphoric_market/unknown")
    p.add_argument("--market_pcr_adjust", type=float, default=None, help="global PCR tilt (±0.10)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("close")
    p.add_argument("--trade_id", type=int, default=None, help="Explicit trade ID (oder --ticker nutzen)")
    p.add_argument("--ticker", default=None, help="Schließt den jüngsten offenen Live-Trade mit diesem Ticker")
    p.add_argument("--exit_date", required=True)
    p.add_argument("--exit_price", type=float, required=True)
    p.add_argument("--exit_reason", required=True,
                   choices=["connors_sma5","atr_tp","atr_sl","max_hold","day3_flat",
                            "intermarket_veto","rule_violation","manual","hard_exit"])
    p.add_argument("--cost", type=float, default=None, help="Round-trip €-Cost (default 2.0)")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("equity", help="Tages-Equity-Snapshot schreiben")
    p.add_argument("--date", required=True)
    p.add_argument("--equity", type=float, required=True)
    p.add_argument("--realized_pnl", type=float, default=0.0)
    p.add_argument("--open_positions", type=int, default=0)
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_equity)

    p = sub.add_parser("list")
    p.add_argument("--open", action="store_true")
    p.add_argument("--live", action="store_true", help="Legacy-Alias: wie --source=systematic,live")
    p.add_argument("--source", default=None,
                   help="Kommaseparierte Source-Liste (backtest,systematic,discretionary,external,other)")
    p.add_argument("--include-all", action="store_true", dest="include_all",
                   help="Alle sources inklusive backtest")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stats"); p.set_defaults(func=cmd_stats)

    p = sub.add_parser("cohort", help="V6.8 Observation-Window Cohort-Audit (Confluence vs Baseline)")
    p.add_argument("--since", default=None, help="YYYY-MM-DD (default: all time)")
    p.add_argument("--until", default=None, help="YYYY-MM-DD (default: now)")
    p.add_argument("--source", default=None,
                   help="Kohorten-Filter (default: systematic+live). Kommasepariert, "
                        "z. B. --source=external oder --source=backtest")
    p.add_argument("--include-all", action="store_true", dest="include_all",
                   help="Alle sources inklusive backtest in die Baseline+Cohorts einbeziehen")
    p.set_defaults(func=cmd_cohort)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
