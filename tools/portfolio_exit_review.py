#!/usr/bin/env python3
"""PORTFOLIO-EXIT-REVIEW-1 (V7.14): Portfolio-weites Exit-Decision-Sheet.

Schliesst die Portfolio-Luecke der Exit-Seite des V10-Lifecycles:
`exit_engine.evaluate_from_journal()` bewertet GENAU EINEN Ticker. Es gab bisher
kein Werkzeug, das die alltaegliche Frage beantwortet: "Was tue ich heute mit
JEDER meiner offenen Positionen?". Dieses Tool enumeriert read-only alle offenen
Journal-Positionen (`exit_date IS NULL`) und bewertet jede ueber `exit_engine`
(Single-Source-of-Truth, KEINE duplizierte Exit-Logik). Output: strukturiertes
JSON + deutsches Markdown-Sheet (briefing-tauglich) + Selftest.

Free-Stack-konform: KEIN Markt-API. Kursdaten sind optional via
`--closes-json` als `{ "TICKER": [c1, c2, ...] }`. Ohne Kurse nimmt exit_engine
`last_close = entry_price` (Return 0 %) -> nur Hard-Exit-T+3 koennte feuern;
solche Positionen werden deshalb als `PRICE_FEED_MISSING` markiert und ihr
vorlaeufiges Verdikt nur informativ ausgewiesen (kein irrefuehrendes EXIT/HOLD).

Die Live-DB wird NUR read-only gelesen (`mode=ro`). Es werden keine Trades
ausgefuehrt und keine Journal-Zeilen veraendert.

CLI:
    python3 tools/portfolio_exit_review.py --selftest
    python3 tools/portfolio_exit_review.py                       # live read-only, Summary
    python3 tools/portfolio_exit_review.py --write               # + Briefings/Exit-Review-<date>.md + signals/
    python3 tools/portfolio_exit_review.py --closes-json closes.json --as-of 2026-06-04
    python3 tools/portfolio_exit_review.py --json-out out.json --md-out out.md

Import-API (z.B. fuer Abend-Tracking 20:00: EINE Importstelle statt Ad-hoc-Logik):
    from portfolio_exit_review import review_portfolio, render_markdown
    review = review_portfolio(as_of="2026-06-04", closes_map=closes)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

# exit_engine liegt im selben Verzeichnis (kein __init__.py in tools/).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import exit_engine as ee  # noqa: E402  (Single-Source-of-Truth fuer Exit-Logik)

VERSION = "V7.14"
TOOL = "portfolio_exit_review"


# ----------------------------------------------------------------------------
# Pfade
# ----------------------------------------------------------------------------
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db() -> str:
    return str(_repo_root() / "trade_journal.sqlite")


# ----------------------------------------------------------------------------
# Journal-Zugriff (read-only)
# ----------------------------------------------------------------------------
def list_open_positions(db_path: Optional[str] = None) -> list[dict]:
    """Alle offenen Positionen (exit_date IS NULL) read-only aus dem Journal.

    Liefert [] wenn DB fehlt oder keine offene Position existiert. Reihenfolge:
    aelteste zuerst (entry_date ASC), so steht die laengst-gehaltene oben.
    """
    db = db_path or _default_db()
    if not Path(db).exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT * FROM trades WHERE exit_date IS NULL ORDER BY entry_date ASC, id ASC"
        )
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception as e:  # pragma: no cover
        print(f"[{TOOL}] journal read failed: {e}", file=sys.stderr)
        return []


# ----------------------------------------------------------------------------
# Klassifikation
# ----------------------------------------------------------------------------
def classify(verdict: "ee.ExitVerdict", has_price_feed: bool) -> str:
    """Uebersetzt ein ExitVerdict + Kursfeed-Status in eine Handlungsklasse.

    DATA_INCOMPLETE   : entry_price/Level fehlen -> set-levels noetig (kein Verdikt)
    PRICE_FEED_MISSING: kein Kursfeed -> Verdikt nur vorlaeufig (last_close=entry)
    EXIT / HOLD       : belastbares Verdikt aus exit_engine
    """
    if verdict.reason == "data_incomplete":
        return "DATA_INCOMPLETE"
    if not has_price_feed:
        return "PRICE_FEED_MISSING"
    return "EXIT" if verdict.action == "EXIT" else "HOLD"


# ----------------------------------------------------------------------------
# Kern: Portfolio-Review
# ----------------------------------------------------------------------------
def review_portfolio(*, as_of: Optional[str] = None,
                     closes_map: Optional[dict] = None,
                     db_path: Optional[str] = None) -> dict:
    """Bewerte ALLE offenen Positionen ueber exit_engine (SSoT).

    closes_map: {ticker: [closes...]} (optional, free-stack). Fehlt ein Ticker,
    wird er als PRICE_FEED_MISSING gefuehrt.
    """
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    closes_map = closes_map or {}
    db = db_path or _default_db()
    rows = list_open_positions(db)

    # Eindeutige Ticker (selten >1 offene Row je Ticker; exit_engine nimmt die
    # juengste). Reihenfolge erhalten.
    seen: set[str] = set()
    tickers: list[str] = []
    for r in rows:
        t = (r.get("ticker") or "").strip()
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)

    positions: list[dict] = []
    summary = {"EXIT": 0, "HOLD": 0, "DATA_INCOMPLETE": 0, "PRICE_FEED_MISSING": 0}
    actions_required: list[dict] = []

    # Lookup der Roh-Row je Ticker (fuer source/position_size/entry_date Echo)
    row_by_ticker = {}
    for r in rows:
        row_by_ticker.setdefault((r.get("ticker") or "").strip(), r)

    for t in tickers:
        closes = closes_map.get(t)
        has_feed = bool(closes)
        verdict = ee.evaluate_from_journal(t, as_of=as_of, closes=closes, db_path=db)
        if verdict is None:  # pragma: no cover - Position existiert ja
            continue
        cls = classify(verdict, has_feed)
        summary[cls] = summary.get(cls, 0) + 1
        raw = row_by_ticker.get(t, {})
        entry = verdict.to_dict()
        entry.update({
            "classification": cls,
            "has_price_feed": has_feed,
            "journal_source": raw.get("source"),
            "journal_entry_date": raw.get("entry_date"),
            "journal_position_size": raw.get("position_size"),
        })
        positions.append(entry)
        if cls in ("EXIT", "DATA_INCOMPLETE", "PRICE_FEED_MISSING"):
            actions_required.append({
                "ticker": t, "classification": cls,
                "action": verdict.action, "reason": verdict.reason,
            })

    return {
        "tool": TOOL,
        "version": VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of,
        "db_path": db,
        "n_open": len(tickers),
        "summary": summary,
        "actions_required": actions_required,
        "positions": positions,
    }


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------
_CLS_LABEL = {
    "EXIT": "🔴 EXIT",
    "HOLD": "🟢 HALTEN",
    "DATA_INCOMPLETE": "⚪ DATEN-LUECKE",
    "PRICE_FEED_MISSING": "🟡 KURS-FEED FEHLT",
}


def render_markdown(review: dict) -> str:
    as_of = review.get("as_of", "?")
    n = review.get("n_open", 0)
    s = review.get("summary", {})
    lines: list[str] = []
    lines.append(f"# Exit-Review {as_of}: Portfolio (V7.14)")
    lines.append("")
    lines.append(f"> Generiert (UTC): {review.get('generated_utc','?')} · "
                 f"Quelle: exit_engine (SSoT), read-only Journal")
    lines.append("")
    if n == 0:
        lines.append("✅ **Keine offenen Positionen**, kein Exit-Review noetig.")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"**Offene Positionen:** {n}  ·  "
                 f"EXIT {s.get('EXIT',0)} · HALTEN {s.get('HOLD',0)} · "
                 f"DATEN-LUECKE {s.get('DATA_INCOMPLETE',0)} · "
                 f"KURS-FEED FEHLT {s.get('PRICE_FEED_MISSING',0)}")
    lines.append("")
    lines.append("| Ticker | Klasse | Regel | Entry | Last | Return % | Tage | TP | SL | Hinweis |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for p in review.get("positions", []):
        cls = _CLS_LABEL.get(p.get("classification"), p.get("classification", "?"))

        def _f(x):
            return "-" if x in (None, "") else (f"{x:.2f}" if isinstance(x, (int, float)) else str(x))

        note = (p.get("note") or "").replace("\n", " ").strip()
        if p.get("classification") == "PRICE_FEED_MISSING":
            note = ("kein Kursfeed → Verdikt vorlaeufig (last=entry). "
                    "closes via --closes-json nachreichen. " + note).strip()
        note = note[:120]
        lines.append(
            f"| {p.get('ticker','?')} | {cls} | {p.get('reason','?')} | "
            f"{_f(p.get('entry_price'))} | {_f(p.get('last_close'))} | "
            f"{_f(p.get('return_pct'))} | {_f(p.get('trading_days_held'))} | "
            f"{_f(p.get('tp'))} | {_f(p.get('sl'))} | {note} |")
    lines.append("")

    actions = review.get("actions_required", [])
    lines.append("## Handlungsbedarf")
    if not actions:
        lines.append("- Keiner: alle offenen Positionen auf HALTEN.")
    else:
        for a in actions:
            if a["classification"] == "EXIT":
                lines.append(f"- **{a['ticker']}: EXIT** ({a['reason']}) → "
                             f"Verkauf zum naechsten Open (Regel exit_engine).")
            elif a["classification"] == "DATA_INCOMPLETE":
                lines.append(f"- **{a['ticker']}: DATEN-LUECKE** → `quick_trade.py set-levels "
                             f"{a['ticker']} --entry-price … --stop … --target …` "
                             f"(exit_engine ist sonst blind).")
            elif a["classification"] == "PRICE_FEED_MISSING":
                lines.append(f"- **{a['ticker']}: KURS-FEED FEHLT** → Closes nachreichen "
                             f"(`--closes-json`), dann ist das Verdikt belastbar.")
    lines.append("")
    lines.append("> Hard-Rule-Quelle: Exit-Prioritaet (Connors-SMA5 → ATR-TP → ATR-SL → Hard-Exit-T+3). "
                 "Keine Order automatisch, die Ausfuehrung entscheidet der User.")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Selftest (Known-Answer, offline gegen temporaere SQLite)
# ----------------------------------------------------------------------------
_SCHEMA = """
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


def _build_demo_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    # AAA: vollstaendig, Closes hitten SMA5 -> EXIT/connors_sma5
    # BBB: vollstaendig, flach, T+3 -> EXIT/hard_exit_t3 (last<sma5, >sl, <tp)
    # CCC: entry_price NULL -> DATA_INCOMPLETE
    # DDD: vollstaendig aber KEIN Closes-Feed -> PRICE_FEED_MISSING
    # ZZZ: GESCHLOSSEN (exit_date gesetzt) -> darf NICHT auftauchen
    con.executemany(
        "INSERT INTO trades (source,ticker,entry_date,position_size,entry_price,"
        "stop,target,side,holding_days,max_hold_days,exit_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("live", "AAA", "2026-06-01", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None),
            ("live", "BBB", "2026-06-01", 1500.0, 100.0, 95.0, 115.0, "long", 3, 3, None),
            ("live", "CCC", "2026-06-02", 1500.0, None, None, None, "long", 1, 3, None),
            ("live", "DDD", "2026-06-02", 1500.0, 50.0, 45.0, 60.0, "long", 1, 3, None),
            ("live", "ZZZ", "2026-05-20", 1500.0, 100.0, 95.0, 115.0, "long", 4, 3, "2026-05-23"),
        ],
    )
    con.commit()
    con.close()


def selftest() -> int:
    import tempfile
    import os

    cases: list[tuple] = []

    def check(name, got, exp):
        ok = (got == exp)
        cases.append((name, ok, str(got), str(exp)))
        return ok

    fd, db = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        _build_demo_db(db)
        closes = {
            "AAA": [101, 102, 103, 104, 104],   # SMA5=102.8, last 104>=SMA5 -> connors_sma5
            "BBB": [101, 101, 101, 101, 100],   # SMA5=100.8, last 100<SMA5, >sl95,<tp115, T+3 -> hard_exit_t3
            # CCC: kein Feed noetig (data_incomplete)
            # DDD: BEWUSST kein Feed -> PRICE_FEED_MISSING
        }
        rev = review_portfolio(as_of="2026-06-04", closes_map=closes, db_path=db)

        check("n_open_excl_closed", rev["n_open"], 4)               # ZZZ ausgeschlossen
        check("sum_exit", rev["summary"]["EXIT"], 2)                 # AAA + BBB
        check("sum_data_incomplete", rev["summary"]["DATA_INCOMPLETE"], 1)  # CCC
        check("sum_price_missing", rev["summary"]["PRICE_FEED_MISSING"], 1)  # DDD
        check("sum_hold", rev["summary"]["HOLD"], 0)

        by = {p["ticker"]: p for p in rev["positions"]}
        check("AAA_reason", by["AAA"]["reason"], "connors_sma5")
        check("AAA_action", by["AAA"]["action"], "EXIT")
        check("BBB_reason", by["BBB"]["reason"], "hard_exit_t3")
        check("CCC_class", by["CCC"]["classification"], "DATA_INCOMPLETE")
        check("DDD_class", by["DDD"]["classification"], "PRICE_FEED_MISSING")
        check("ZZZ_absent", "ZZZ" in by, False)

        # actions_required enthaelt EXIT/DATA_INCOMPLETE/PRICE_FEED_MISSING (nicht HOLD)
        ar = {a["ticker"] for a in rev["actions_required"]}
        check("actions_set", ar == {"AAA", "BBB", "CCC", "DDD"}, True)

        # Markdown: enthaelt Ticker + Handlungsbedarf-Sektion
        md = render_markdown(rev)
        check("md_has_AAA", "AAA" in md, True)
        check("md_has_handlungsbedarf", "Handlungsbedarf" in md, True)
        check("md_has_setlevels_hint", "set-levels CCC" in md, True)

        # Leerer Fall: keine offene Position -> sauberes Sheet
        fd2, db2 = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd2)
        try:
            con = sqlite3.connect(db2)
            con.executescript(_SCHEMA)
            con.commit()
            con.close()
            rev0 = review_portfolio(as_of="2026-06-04", db_path=db2)
            check("empty_n_open", rev0["n_open"], 0)
            check("empty_md", "Keine offenen Positionen" in render_markdown(rev0), True)
        finally:
            os.remove(db2)

        # Fehlende DB -> [] (kein Crash)
        check("missing_db_empty", list_open_positions("/nonexistent/never.sqlite"), [])
    finally:
        os.remove(db)

    passed = sum(1 for _, ok, *_ in cases if ok)
    total = len(cases)
    print(f"=== {TOOL} selftest: {passed}/{total} ===")
    for name, ok, got, exp in cases:
        flag = "PASS" if ok else "FAIL"
        extra = "" if ok else f"  (got {got}, exp {exp})"
        print(f"  [{flag}] {name}{extra}")
    return 0 if passed == total else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_closes_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"[{TOOL}] closes-json nicht gefunden: {path}", file=sys.stderr)
        return None
    try:
        data = json.loads(p.read_text())
        # Werte zu float-Listen normalisieren
        return {str(k): [float(x) for x in v] for k, v in data.items()}
    except Exception as e:
        print(f"[{TOOL}] closes-json unlesbar: {e}", file=sys.stderr)
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="PORTFOLIO-EXIT-REVIEW-1: Exit-Verdikt fuer ALLE offenen Positionen (V7.14)")
    ap.add_argument("--selftest", action="store_true", help="Known-Answer-Tests (offline)")
    ap.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (Default: heute UTC)")
    ap.add_argument("--db", type=str, default=None, help="Pfad zur trade_journal.sqlite (Default: Repo-Root)")
    ap.add_argument("--closes-json", type=str, default=None,
                    help='JSON {"TICKER":[c1,c2,...]} fuer SMA5/last_close (free-stack, optional)')
    ap.add_argument("--write", action="store_true",
                    help="schreibt Briefings/Exit-Review-<as_of>.md + signals/exit_review_<as_of>.json")
    ap.add_argument("--json-out", type=str, default=None, help="JSON an diesen Pfad schreiben")
    ap.add_argument("--md-out", type=str, default=None, help="Markdown an diesen Pfad schreiben")
    ap.add_argument("--quiet", action="store_true", help="keine Markdown-Ausgabe auf stdout")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        return selftest()

    closes_map = _load_closes_json(args.closes_json)
    review = review_portfolio(as_of=args.as_of, closes_map=closes_map, db_path=args.db)
    md = render_markdown(review)

    s = review["summary"]
    print(f"[{TOOL}] as_of={review['as_of']} open={review['n_open']} "
          f"EXIT={s['EXIT']} HOLD={s['HOLD']} "
          f"DATA_INCOMPLETE={s['DATA_INCOMPLETE']} PRICE_FEED_MISSING={s['PRICE_FEED_MISSING']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(review, indent=2, ensure_ascii=False))
        print(f"[{TOOL}] JSON -> {args.json_out}")
    if args.md_out:
        Path(args.md_out).write_text(md)
        print(f"[{TOOL}] MD   -> {args.md_out}")

    if args.write:
        as_of = review["as_of"]
        bdir = _repo_root() / "Briefings"
        sdir = _repo_root() / "signals"
        bdir.mkdir(exist_ok=True)
        sdir.mkdir(exist_ok=True)
        md_path = bdir / f"Exit-Review-{as_of}.md"
        js_path = sdir / f"exit_review_{as_of}.json"
        md_path.write_text(md)
        js_path.write_text(json.dumps(review, indent=2, ensure_ascii=False))
        print(f"[{TOOL}] geschrieben: {md_path}")
        print(f"[{TOOL}] geschrieben: {js_path}")

    if not args.quiet and not args.write:
        print()
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
