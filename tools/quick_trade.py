#!/usr/bin/env python3
"""
Quick-Trade-Update-Tool (2026-05-19).

Einfaches CLI um Trades manuell ins Journal zu verbuchen, ohne SQL-Hantieren.

Beispiele:
    # Neuen Trade buchen (Buy)
    python3 tools/quick_trade.py buy AAA 1000.00 --date 2024-01-08 --source live

    # Trade schliessen (Sell mit Verkaufspreis-EUR)
    python3 tools/quick_trade.py sell AAA 1050.00 --date 2024-01-11

    # Status: was ist offen?
    python3 tools/quick_trade.py status

    # YTD-Bilanz
    python3 tools/quick_trade.py summary

    # Per-Share-Level auf offene Position setzen (macht exit_engine auswertbar)
    python3 tools/quick_trade.py set-levels AAA --entry-price 100.00 --stop 95.00 --target 107.50 --side long

Source-Tags:
    - live          → Trade auf einen System-Kandidaten (V6.1 etc.), selbst entschieden
    - discretionary → Dein eigener Pick (kein System-Signal)
    - external      → Signal aus einer externen/zweiten Signalquelle
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "trade_journal.sqlite"

# Flat broker fee per order in EUR (buy and sell each). Default 1.0; override via
# DAILYINVEST_ORDER_FEE_EUR to match your own broker.
ORDER_FEE_EUR = float(os.environ.get("DAILYINVEST_ORDER_FEE_EUR", "1.0"))


def _connect():
    return sqlite3.connect(str(DB_PATH))


def buy(ticker: str, position_eur: float, date: str, source: str, note: str | None = None,
        *, entry_price: float | None = None, stop: float | None = None,
        target: float | None = None, atr_pct: float | None = None,
        side: str | None = None):
    """Neuen Trade ins Journal. position_eur = Wert in EUR (z.B. 1000.00 für 10 Stk à 100).

    Optional Per-Share-Level (entry_price/stop/target/atr_pct/side) gleich beim Kauf
    mitschreiben → die Position ist sofort exit_engine-auswertbar (kein DATA_INCOMPLETE).
    Werden keine Level übergeben, ist das INSERT byte-identisch zum Alt-Verhalten
    (EUR-Notional in position_size, Level bleiben NULL).
    """
    with _connect() as conn:
        c = conn.cursor()
        # Check ob schon offen
        c.execute("SELECT id FROM trades WHERE ticker=? AND entry_date=? AND exit_date IS NULL", (ticker, date))
        if c.fetchall():
            print(f"⚠️  {ticker} ist schon offen mit entry_date={date}, kein Insert", file=sys.stderr)
            return False

        cols = ["source", "system_version", "ticker", "tier", "regime",
                "entry_date", "position_size", "cost", "notes"]
        vals = [source, "V6.1", ticker, "?", "?", date, position_eur, ORDER_FEE_EUR,
                note or f"Quick-Trade {date}"]
        # Level nur anhängen, wenn übergeben → Alt-Pfad (minimal-Schema) bleibt unberührt.
        for col, val in (("entry_price", entry_price), ("stop", stop),
                         ("target", target), ("atr_pct", atr_pct), ("side", side)):
            if val is not None:
                cols.append(col)
                vals.append(val)
        placeholders = ", ".join("?" * len(vals))
        c.execute(f"INSERT INTO trades ({', '.join(cols)}) VALUES ({placeholders})", vals)
        conn.commit()
        lvl = ""
        if entry_price is not None:
            lvl = f"  [entry/Stk={entry_price} stop={stop} target={target}]"
        print(f"✓ BUY {ticker} {position_eur:.2f}€ @ {date} ({source}){lvl}")
        return True


def sell(ticker: str, position_eur_out: float, date: str,
         *, exit_price: float | None = None):
    """Schließt offenen Trade. position_eur_out = Verkaufserlös in EUR.
    Brutto-P&L = exit - entry. Cost wird automatisch um ORDER_FEE_EUR (Sell-Fee) erhoeht.

    K4-FIX 2026-06-14: optionaler exit_price (Per-Share) wird mitgeschrieben,
    damit geschlossene Trades die exit-price-Analytik (kelly/exit_engine) speisen.
    """
    with _connect() as conn:
        c = conn.cursor()
        # Suche offenen Trade
        c.execute(
            "SELECT id, position_size, cost FROM trades WHERE ticker=? AND exit_date IS NULL ORDER BY entry_date DESC LIMIT 1",
            (ticker,),
        )
        row = c.fetchone()
        if not row:
            print(f"⚠️  Kein offener {ticker}-Trade gefunden", file=sys.stderr)
            return False

        trade_id, entry_eur, old_cost = row
        brutto = position_eur_out - entry_eur
        cost_total = (old_cost or ORDER_FEE_EUR) + ORDER_FEE_EUR  # + Sell-Fee
        netto = brutto - cost_total
        pnl_pct = brutto / entry_eur * 100 if entry_eur else 0
        is_win = 1 if netto > 0 else 0

        # FIX 2026-06-01: netto_pnl/is_win/return_pct mitschreiben.
        # Vorher wurden nur brutto_pnl/cost gesetzt -> netto_pnl/is_win blieben NULL,
        # wodurch nettobasierte Tools (MCP-Summary, kelly_sizer, journal_metrics)
        # den Live-Gewinn als 0€ lasen.
        sets = ["exit_date=?", "brutto_pnl=?", "cost=?",
                "netto_pnl=?", "return_pct=?", "is_win=?"]
        vals = [date, round(brutto, 2), cost_total,
                round(netto, 2), round(pnl_pct, 3), is_win]
        if exit_price is not None:
            sets.append("exit_price=?")
            vals.append(exit_price)
        vals.append(trade_id)
        # Spaltennamen aus fixem Satz (keine Injection); Werte parametrisiert.
        c.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        emoji = "🎯" if brutto > 0 else "📉"
        print(f"{emoji} SELL {ticker} {position_eur_out:.2f}€ @ {date}: P&L: {brutto:+.2f}€ ({pnl_pct:+.2f}%) Netto: {brutto - cost_total:+.2f}€")
        return True


def status():
    """Listet alle offenen Positionen."""
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT ticker, entry_date, source, position_size, hard_exit_tag
               FROM trades WHERE exit_date IS NULL
               ORDER BY entry_date"""
        )
        rows = c.fetchall()
        if not rows:
            print("Keine offenen Positionen.")
            return
        print(f"\n{'Ticker':<10} {'Entry':<12} {'Source':<18} {'Size €':<10} {'Hard-Exit':<12}")
        print("-" * 65)
        for r in rows:
            ticker, entry, src, size, hex = r
            print(f"{ticker:<10} {entry:<12} {src or '?':<18} {size or 0:<10.2f} {hex or '?':<12}")


def summary(year: int | None = None):
    """Realisierter YTD-P&L (Default: aktuelles Jahr)."""
    year = year or datetime.now().year
    with _connect() as conn:
        c = conn.cursor()
        # FIX 2026-06-01: Netto statt Brutto (COALESCE faengt Legacy-Rows mit NULL netto_pnl ab).
        c.execute(
            """SELECT source,
                      COUNT(*) as n,
                      SUM(CASE WHEN COALESCE(netto_pnl, brutto_pnl - COALESCE(cost,0)) > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(COALESCE(netto_pnl, brutto_pnl - COALESCE(cost,0))) as total_pnl
               FROM trades
               WHERE brutto_pnl IS NOT NULL
                 AND exit_date LIKE ?
                 AND source IN ('live', 'discretionary', 'external')
               GROUP BY source""",
            (f"{year}-%",),
        )
        print(f"\n=== Realisierter Netto-P&L {year} (echte Trades, ohne shadow) ===")
        print(f"{'Source':<18} {'N':<5} {'WR':<8} {'Total €':<12}")
        print("-" * 50)
        total_all = 0
        for r in c.fetchall():
            src, n, wins, total = r
            wr = wins / n * 100 if n else 0
            total_all += total or 0
            print(f"{src or '?':<18} {n:<5} {wr:<6.0f}% {total or 0:<+12.2f}")
        print("-" * 50)
        print(f"{'TOTAL':<32} {total_all:<+12.2f}")


def recompute():
    """Backfill: netto_pnl/is_win/return_pct fuer geschlossene Trades nachrechnen,
    wo sie NULL sind (Legacy-Rows aus aelterer sell()-Logik).
    Aendert NUR abgeleitete Felder (netto_pnl/is_win/return_pct),
    nie entry/exit/brutto_pnl/cost. Sanktionierter Pfad statt Raw-SQL."""
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, ticker, position_size, brutto_pnl, cost
               FROM trades
               WHERE exit_date IS NOT NULL
                 AND brutto_pnl IS NOT NULL
                 AND (netto_pnl IS NULL OR is_win IS NULL)"""
        )
        rows = c.fetchall()
        if not rows:
            print("✓ Nichts zu tun: alle geschlossenen Trades haben netto_pnl/is_win.")
            return 0
        n = 0
        for trade_id, ticker, entry_eur, brutto, cost in rows:
            cost = cost if cost is not None else 0.0
            netto = brutto - cost
            ret = (brutto / entry_eur * 100) if entry_eur else None
            is_win = 1 if netto > 0 else 0
            c.execute(
                """UPDATE trades SET netto_pnl=?, is_win=?,
                       return_pct=COALESCE(return_pct, ?)
                   WHERE id=?""",
                (round(netto, 2), is_win,
                 round(ret, 3) if ret is not None else None, trade_id),
            )
            n += 1
            print(f"  ↻ {ticker} (id={trade_id}): netto={netto:+.2f}€ is_win={is_win} ret={('%.2f%%' % ret) if ret is not None else 'n/a'}")
        conn.commit()
        print(f"✓ {n} Trade(s) nachgerechnet.")
        return n


def set_levels(ticker: str, *, entry_price: float | None = None,
               stop: float | None = None, target: float | None = None,
               atr_pct: float | None = None, atr_mult: float | None = None,
               side: str | None = None, position_size: float | None = None,
               exit_price: float | None = None, closed: bool = False,
               date: str | None = None):
    """Setzt Felder auf eine OFFENE Position (exit_date IS NULL).

    Sanktionierter Backfill-Pfad analog recompute(): kein Raw-SQL, lock-safe `with`,
    rührt NUR die übergebenen Spalten an (entry_price/stop/target/atr_pct/atr_mult/
    side/position_size): nie entry_date/brutto_pnl/cost.
    `position_size` optional korrigierbar, falls der EUR-Notional falsch geloggt wurde
    (z.B. 2000 geschätzt, real 1250.00 → sonst rechnet sell() den P&L falsch).

    Motivation: quick_trade.py buy loggt EUR-Notional,
    aber keinen Per-Share-Entry → tools/exit_engine.py liefert korrekt DATA_INCOMPLETE.
    Nach set-levels ist die Position exit_engine-auswertbar. KEIN Trade, nur Journal-Metadaten.
    """
    fields = {"entry_price": entry_price, "stop": stop, "target": target,
              "atr_pct": atr_pct, "atr_mult": atr_mult, "side": side,
              "position_size": position_size, "exit_price": exit_price}
    provided = {k: v for k, v in fields.items() if v is not None}
    if not provided:
        print("⚠️  Nichts angegeben (mind. eines von --entry-price/--stop/--target/"
              "--atr-pct/--atr-mult/--side/--position-size).", file=sys.stderr)
        return False

    with _connect() as conn:
        c = conn.cursor()
        # K4-FIX 2026-06-14: --closed erlaubt Backfill auf GESCHLOSSENE Trades
        # (z.B. exit_price nachtragen); default unveraendert (offene Position).
        exit_pred = "exit_date IS NOT NULL" if closed else "exit_date IS NULL"
        if date:
            c.execute(
                "SELECT id, entry_price, stop, target FROM trades "
                f"WHERE ticker=? AND entry_date=? AND {exit_pred} "
                "ORDER BY entry_date DESC LIMIT 1", (ticker, date))
        else:
            c.execute(
                "SELECT id, entry_price, stop, target FROM trades "
                f"WHERE ticker=? AND {exit_pred} "
                "ORDER BY entry_date DESC LIMIT 1", (ticker,))
        row = c.fetchone()
        if not row:
            kind = "geschlossener" if closed else "offener"
            print(f"⚠️  Kein {kind} {ticker}-Trade gefunden", file=sys.stderr)
            return False

        trade_id, old_entry, old_stop, old_target = row
        # updated_at_utc nur setzen, wenn die Spalte existiert (Schema-robust).
        existing_cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        upd = dict(provided)
        if "updated_at_utc" in existing_cols:
            upd["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        # Spaltennamen stammen aus fixem Schlüsselsatz (keine User-Injection).
        set_clause = ", ".join(f"{k}=?" for k in upd)
        c.execute(f"UPDATE trades SET {set_clause} WHERE id=?",
                  list(upd.values()) + [trade_id])
        conn.commit()
        changed = ", ".join(f"{k}={v}" for k, v in provided.items())
        print(f"✓ SET-LEVELS {ticker} (id={trade_id}): {changed}")
        if old_entry is not None:
            print(f"   (vorher entry_price={old_entry}, stop={old_stop}, target={old_target})")
        return True


def export(out_path: str | None = None):
    """Deterministischer CSV-Voll-Dump aller trades-Rows -> versionierbares Backup (K1).

    Schreibt REPO_ROOT/trade_journal_export.csv (oder --out). Spalten = aktuelles
    Schema, Zeilen nach id sortiert -> diff-freundlich & stabil. Read-only auf der DB.
    Fuer ein lokales/privates Backup der Journal-Historie (die .sqlite selbst ist
    gitignored: WAL-Binary).
    PRIVACY: die Export-Datei enthaelt deine KOMPLETTE Trade-Historie. Sie ist
    gitignored und gehoert NIE in ein oeffentliches Repository oder einen Fork.
    """
    import csv
    out = Path(out_path) if out_path else (REPO_ROOT / "trade_journal_export.csv")
    with _connect() as conn:
        cur = conn.execute("SELECT * FROM trades ORDER BY id")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"✓ EXPORT {len(rows)} Trades → {out}")
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Quick Trade Update (Journal-CLI)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_buy = sub.add_parser("buy", help="Kauf verbuchen")
    p_buy.add_argument("ticker")
    p_buy.add_argument("position_eur", type=float, help="Kaufsumme in EUR (z.B. 1000.00)")
    p_buy.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p_buy.add_argument("--source", default="live", choices=["live", "discretionary", "external"])
    p_buy.add_argument("--note", default=None)
    # Optionale Per-Share-Level direkt beim Kauf (exit_engine-ready)
    p_buy.add_argument("--entry-price", type=float, default=None, help="Per-Share-Entry ($)")
    p_buy.add_argument("--stop", type=float, default=None, help="Stop-Loss ($)")
    p_buy.add_argument("--target", type=float, default=None, help="Take-Profit ($)")
    p_buy.add_argument("--atr-pct", type=float, default=None, help="ATR in % vom Entry")
    p_buy.add_argument("--side", default=None, choices=["long", "short"])

    p_sell = sub.add_parser("sell", help="Verkauf verbuchen")
    p_sell.add_argument("ticker")
    p_sell.add_argument("position_eur_out", type=float, help="Verkaufserlös in EUR")
    p_sell.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p_sell.add_argument("--exit-price", type=float, default=None,
                        help="Per-Share-Exit ($): fuer exit_price-Analytik (K4)")

    sub.add_parser("status", help="Offene Positionen anzeigen")
    p_sum = sub.add_parser("summary", help="YTD-P&L-Bilanz")
    p_sum.add_argument("--year", type=int, default=None, help="Default: aktuelles Jahr")
    sub.add_parser("recompute", help="netto_pnl/is_win fuer geschlossene Legacy-Trades nachrechnen")

    p_exp = sub.add_parser("export", help="Deterministischer CSV-Dump aller Trades (versionierbares Backup, K1)")
    p_exp.add_argument("--out", default=None, help="Zielpfad (default REPO_ROOT/trade_journal_export.csv)")

    p_set = sub.add_parser(
        "set-levels",
        help="Per-Share-Level (entry/stop/target/atr) auf offene Position setzen: exit_engine-ready")
    p_set.add_argument("ticker")
    p_set.add_argument("--entry-price", type=float, default=None, help="Per-Share-Entry ($)")
    p_set.add_argument("--stop", type=float, default=None, help="Stop-Loss ($)")
    p_set.add_argument("--target", type=float, default=None, help="Take-Profit ($)")
    p_set.add_argument("--atr-pct", type=float, default=None, help="ATR in % vom Entry")
    p_set.add_argument("--atr-mult", type=float, default=None, help="ATR-Multiplikator für SL")
    p_set.add_argument("--side", default=None, choices=["long", "short"])
    p_set.add_argument("--position-size", type=float, default=None,
                       help="EUR-Notional korrigieren, falls Einsatz falsch geloggt (z.B. 2000→1250.00)")
    p_set.add_argument("--exit-price", type=float, default=None,
                       help="Per-Share-Exit ($) nachtragen (Backfill geschlossener Trades, K4)")
    p_set.add_argument("--closed", action="store_true",
                       help="Auf GESCHLOSSENE Position zielen (exit_date NOT NULL) statt offene")
    p_set.add_argument("--date", default=None, help="entry_date-Filter, falls mehrere offene Rows")

    args = ap.parse_args()

    if args.cmd == "buy":
        buy(args.ticker, args.position_eur, args.date, args.source, args.note,
            entry_price=args.entry_price, stop=args.stop, target=args.target,
            atr_pct=args.atr_pct, side=args.side)
    elif args.cmd == "sell":
        sell(args.ticker, args.position_eur_out, args.date, exit_price=args.exit_price)
    elif args.cmd == "status":
        status()
    elif args.cmd == "summary":
        summary(args.year)
    elif args.cmd == "recompute":
        recompute()
    elif args.cmd == "export":
        export(args.out)
    elif args.cmd == "set-levels":
        set_levels(args.ticker, entry_price=args.entry_price, stop=args.stop,
                   target=args.target, atr_pct=args.atr_pct, atr_mult=args.atr_mult,
                   side=args.side, position_size=args.position_size,
                   exit_price=args.exit_price, closed=args.closed, date=args.date)


if __name__ == "__main__":
    main()
