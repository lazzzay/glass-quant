#!/usr/bin/env python3
"""EXIT-PRICE-FEED-1 (V7.15): Free-Stack Closes-Snapshot fuer das Exit-Review.

Schliesst die letzte Luecke der Exit-Seite des V10-Lifecycles. Das V7.14
`portfolio_exit_review` markiert jede offene Position OHNE Kursfeed als
PRICE_FEED_MISSING und liefert nur ein vorlaeufiges Verdikt. Dieses Tool
beschafft die fehlenden Kurse FREE-STACK (yfinance -> Stooq-Fallback ueber
`data_source_orchestrator.fetch_with_fallback`) und schreibt sie in genau das
`closes-json`-Format `{ "TICKER": [c1, c2, ...] }`, das
`portfolio_exit_review --closes-json` (und `exit_engine --closes`) konsumieren.
Damit kippt eine PRICE_FEED_MISSING-Position zu einem belastbaren EXIT/HOLD.

Free-Stack-Hard-Constraint (CLAUDE.md §6/§8): KEINE Paid-API. yfinance + Stooq
sind die einzigen Quellen: identisch zur Scanner-Datenschicht. Die
offene-Positionen-Liste kommt read-only ueber
`portfolio_exit_review.list_open_positions` (Single-Source-of-Truth, keine
zweite Journal-Abfrage-Logik). Es werden KEINE Trades ausgefuehrt und KEINE
Journal-Zeilen veraendert.

Testbarkeit / Sandbox: Der Netz-Fetch steckt hinter einem injizierbaren
`fetcher`-Callable. Selftest + pytest laufen vollstaendig OFFLINE (kein
yfinance/Netz noetig: im Cowork-Sandbox ist yfinance gar nicht importierbar).
Der Live-Pfad (Mac, Residential-IP) nutzt den realen Free-Stack-Fetcher.

CLI:
    python3 tools/exit_price_feed.py --selftest
    python3 tools/exit_price_feed.py                       # auto: offene Journal-Ticker
    python3 tools/exit_price_feed.py --tickers AAA,BBB --out signals/closes.json
    # End-to-End (free-stack): erst Feed, dann Review:
    python3 tools/exit_price_feed.py --write
    python3 tools/portfolio_exit_review.py --closes-json signals/closes_<date>.json --write

Import-API (fuer das spaetere Abend-Tracking-Wiring 20:00: EINE Importstelle):
    from exit_price_feed import build_closes_map, open_position_tickers
    from portfolio_exit_review import review_portfolio
    tickers = open_position_tickers()
    fed = build_closes_map(tickers)                 # {'closes_map':..., 'unavailable':...}
    review = review_portfolio(closes_map=fed["closes_map"])
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

# Geschwister-Module liegen im selben Verzeichnis (kein __init__.py in tools/).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

VERSION = "V7.15"
TOOL = "exit_price_feed"

DEFAULT_LOOKBACK = 12       # Handelstage Closes, die in die Map geschrieben werden (>= 5 fuer robusten SMA5)
DEFAULT_MIN_CLOSES = 5      # exit_engine.sma(., 5) braucht mind. 5 Werte, sonst SMA5=None
DEFAULT_PERIOD_DAYS = 45    # Kalendertage Fetch-Fenster (~31 Handelstage Puffer)


# ----------------------------------------------------------------------------
# Pfade
# ----------------------------------------------------------------------------
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db() -> str:
    return str(_repo_root() / "trade_journal.sqlite")


# ----------------------------------------------------------------------------
# Offene Positionen (SSoT: portfolio_exit_review, read-only)
# ----------------------------------------------------------------------------
def open_position_tickers(db_path: Optional[str] = None) -> list:
    """Eindeutige Ticker aller offenen Journal-Positionen (read-only, Reihenfolge erhalten).

    Reuse von `portfolio_exit_review.list_open_positions` => keine zweite
    Journal-Abfrage-Logik (SSoT fuer "was ist offen").
    """
    try:
        import portfolio_exit_review as per
    except Exception as e:  # pragma: no cover
        print(f"[{TOOL}] import portfolio_exit_review fehlgeschlagen: {e}", file=sys.stderr)
        return []
    seen = set()
    out = []
    for r in per.list_open_positions(db_path or _default_db()):
        t = (r.get("ticker") or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ----------------------------------------------------------------------------
# Live-Fetcher (free-stack, lazy import -> Modul bleibt Sandbox-importierbar)
# ----------------------------------------------------------------------------
def default_live_fetcher(ticker: str, *, period_days: int = DEFAULT_PERIOD_DAYS,
                         prefer: str = "yfinance") -> Tuple[Optional[list], str]:
    """Holt taegliche Closes (chronologisch aufsteigend) ueber den Free-Stack.

    Liefert (closes_list | None, source) mit source in
    {'yfinance','stooq','none','error'}. Lazy import, damit das Modul auch ohne
    installiertes yfinance (Sandbox) importierbar bleibt und nur der konkrete
    Fetch-Aufruf scheitert (sauber zu 'error').
    """
    fetch_with_fallback = None
    try:
        from data_source_orchestrator import fetch_with_fallback  # type: ignore
    except Exception:
        try:
            from tools.data_source_orchestrator import fetch_with_fallback  # type: ignore
        except Exception:
            return None, "error"
    try:
        df, src = fetch_with_fallback(ticker, period_days=period_days, prefer=prefer)
        if df is None or "Close" not in getattr(df, "columns", []):
            return None, "none"
        closes = [float(x) for x in df["Close"].dropna().tolist()]
        if not closes:
            return None, "none"
        return closes, src
    except Exception:
        return None, "error"


# ----------------------------------------------------------------------------
# Kern: closes-map bauen
# ----------------------------------------------------------------------------
def build_closes_map(tickers: Sequence[str], *,
                     lookback: int = DEFAULT_LOOKBACK,
                     min_closes: int = DEFAULT_MIN_CLOSES,
                     fetcher: Optional[Callable] = None) -> dict:
    """Baue `{ ticker: [closes...] }` fuer das Exit-Review (free-stack).

    fetcher(ticker) -> (closes_list | None, source). Default: default_live_fetcher.

    Ticker mit < `min_closes` verwertbaren Closes landen NICHT in der Map,
    sondern in `unavailable` (mit Grund), so markiert `portfolio_exit_review`
    sie sauber als PRICE_FEED_MISSING, statt einen erfundenen Kurs zu nutzen
    (ehrlicher Status statt Fake-Daten, vgl. CLAUDE.md-Philosophie).

    Reihenfolge der Eingabe-Ticker bleibt erhalten. Ein einzelner kaputter
    Ticker (Fetcher wirft) kippt den Gesamtlauf nicht.
    """
    fetch = fetcher or default_live_fetcher
    closes_map: dict = {}
    sources: dict = {}
    unavailable: list = []

    clean_tickers = [(t or "").strip() for t in tickers]
    clean_tickers = [t for t in clean_tickers if t]

    for t in clean_tickers:
        try:
            res = fetch(t)
        except Exception as e:  # defensiv: ein kaputter Ticker darf den Lauf nicht kippen
            unavailable.append({"ticker": t, "reason": "fetch_exception", "detail": str(e)[:120]})
            continue
        if isinstance(res, tuple):
            closes, src = res
        else:
            closes, src = res, "unknown"
        if not closes:
            unavailable.append({"ticker": t, "reason": "no_data", "source": src})
            continue
        clean = [round(float(c), 4) for c in closes if c is not None]
        if len(clean) < int(min_closes):
            unavailable.append({"ticker": t, "reason": "too_few_closes",
                                "n": len(clean), "min": int(min_closes), "source": src})
            continue
        closes_map[t] = clean[-int(lookback):]
        sources[t] = src

    return {
        "tool": TOOL,
        "version": VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback": int(lookback),
        "min_closes": int(min_closes),
        "n_requested": len(clean_tickers),
        "n_fetched": len(closes_map),
        "n_unavailable": len(unavailable),
        "closes_map": closes_map,
        "sources": sources,
        "unavailable": unavailable,
    }


# ----------------------------------------------------------------------------
# Rendering (kleines deutsches Status-Sheet)
# ----------------------------------------------------------------------------
def render_status_markdown(result: dict, as_of: Optional[str] = None) -> str:
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cm = result.get("closes_map", {})
    sources = result.get("sources", {})
    unavailable = result.get("unavailable", [])
    lines: list = []
    lines.append(f"# Exit-Price-Feed {as_of}: Free-Stack Closes-Snapshot ({VERSION})")
    lines.append("")
    lines.append(f"> Generiert (UTC): {result.get('generated_utc','?')} · "
                 f"Quelle: yfinance -> Stooq (free-stack) · read-only Journal")
    lines.append("")
    lines.append(f"**Angefragt:** {result.get('n_requested',0)} · "
                 f"**Geliefert:** {result.get('n_fetched',0)} · "
                 f"**Fehlend:** {result.get('n_unavailable',0)}")
    lines.append("")
    if cm:
        lines.append("| Ticker | #Closes | Letzter Close | Quelle |")
        lines.append("|---|---|---|---|")
        for t, closes in cm.items():
            last = closes[-1] if closes else None
            last_s = "-" if last is None else f"{last:.2f}"
            lines.append(f"| {t} | {len(closes)} | {last_s} | {sources.get(t,'?')} |")
        lines.append("")
    else:
        lines.append("_Keine Closes geliefert (keine offene Position, oder Egress blockiert)._")
        lines.append("")
    if unavailable:
        lines.append("## Nicht verfuegbar (-> PRICE_FEED_MISSING im Exit-Review)")
        for u in unavailable:
            extra = u.get("detail") or u.get("source") or ""
            n = u.get("n")
            ncol = f" (n={n})" if n is not None else ""
            lines.append(f"- **{u.get('ticker','?')}**: {u.get('reason','?')}{ncol} {extra}".rstrip())
        lines.append("")
    lines.append("> Naechster Schritt: `portfolio_exit_review.py --closes-json <out> --write` "
                 "macht aus PRICE_FEED_MISSING ein belastbares EXIT/HOLD.")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Selftest (Known-Answer, vollstaendig OFFLINE via injiziertem Fetcher)
# ----------------------------------------------------------------------------
def _fake_fetcher(series_by_ticker: dict) -> Callable:
    """Baue einen deterministischen Offline-Fetcher fuer die Tests.

    series_by_ticker: {ticker: (closes_list | None, source)} oder
                      {ticker: Exception-Instanz} (=> wirft).
    """
    def _f(ticker: str):
        v = series_by_ticker.get(ticker)
        if isinstance(v, Exception):
            raise v
        if v is None:
            return (None, "none")
        return v
    return _f


def selftest() -> int:
    import os
    import tempfile

    cases: list = []

    def check(name, got, exp):
        ok = (got == exp)
        cases.append((name, ok, str(got), str(exp)))
        return ok

    # --- 1) Happy path + lookback-Trim + min_closes ----------------------
    fetcher = _fake_fetcher({
        "AAA": ([float(x) for x in range(101, 121)], "yfinance"),   # 20 Closes -> Trim auf lookback
        "BBB": ([10, 11, 12, 13, 14, 15, 16, 17], "stooq"),         # 8 Closes
        "CCC": ([100, 101, 102], "yfinance"),                       # 3 < min_closes 5 -> unavailable
        "DDD": (None, "none"),                                      # kein Feed -> unavailable
        "EEE": ([5, 6, 7, 8, 9], "yfinance"),                       # exakt 5 -> Grenze inkludiert
        "FFF": ValueError("boom"),                                  # wirft -> fetch_exception
    })
    res = build_closes_map(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", " "],
                           lookback=12, min_closes=5, fetcher=fetcher)

    check("n_requested_blank_skipped", res["n_requested"], 6)        # leerer String faellt raus
    check("AAA_present", "AAA" in res["closes_map"], True)
    check("AAA_trimmed_to_lookback", len(res["closes_map"]["AAA"]), 12)
    check("AAA_last_is_most_recent", res["closes_map"]["AAA"][-1], 120.0)   # juengster Wert bleibt
    check("BBB_full_kept", len(res["closes_map"]["BBB"]), 8)
    check("EEE_boundary_included", "EEE" in res["closes_map"], True)
    check("source_label_propagated", res["sources"]["BBB"], "stooq")

    unavail = {u["ticker"]: u["reason"] for u in res["unavailable"]}
    check("CCC_too_few", unavail.get("CCC"), "too_few_closes")
    check("DDD_no_data", unavail.get("DDD"), "no_data")
    check("FFF_fetch_exception", unavail.get("FFF"), "fetch_exception")
    check("n_fetched", res["n_fetched"], 3)                          # AAA, BBB, EEE
    check("n_unavailable", res["n_unavailable"], 3)                  # CCC, DDD, FFF

    # --- 2) Leerer Input ---------------------------------------------------
    res0 = build_closes_map([], fetcher=fetcher)
    check("empty_input_map", res0["closes_map"], {})
    check("empty_input_n", res0["n_requested"], 0)

    # --- 3) Status-Markdown befuellt --------------------------------------
    md = render_status_markdown(res, as_of="2026-06-05")
    check("md_has_AAA", "AAA" in md, True)
    check("md_has_unavailable_section", "Nicht verfuegbar" in md, True)

    # --- 4) END-TO-END: PRICE_FEED_MISSING -> EXIT (Loop schliesst sich) ---
    # Temp-Journal mit EINER offenen Position AAA; exit_engine erwartet
    # SMA5([101,102,103,104,104])=102.8, last 104 >= SMA5 -> connors_sma5 EXIT.
    import portfolio_exit_review as per
    fd, db = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        import sqlite3
        con = sqlite3.connect(db)
        con.executescript(per._SCHEMA)
        con.executemany(
            "INSERT INTO trades (source,ticker,entry_date,position_size,entry_price,"
            "stop,target,side,holding_days,max_hold_days,exit_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("live", "AAA", "2026-06-02", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None),
                ("live", "ZZZ", "2026-05-20", 1500.0, 100.0, 95.0, 115.0, "long", 4, 3, "2026-05-23"),
            ],
        )
        con.commit()
        con.close()

        # 4a) open_position_tickers liest AAA read-only, schliesst geschlossene ZZZ aus
        tickers = open_position_tickers(db_path=db)
        check("open_tickers_AAA", tickers, ["AAA"])

        # 4b) OHNE Feed -> PRICE_FEED_MISSING
        rev_before = per.review_portfolio(as_of="2026-06-05", closes_map={}, db_path=db)
        by_before = {p["ticker"]: p for p in rev_before["positions"]}
        check("before_AAA_price_feed_missing", by_before["AAA"]["classification"], "PRICE_FEED_MISSING")

        # 4c) MIT Feed (free-stack-Pfad simuliert) -> EXIT/connors_sma5
        feed = build_closes_map(tickers, fetcher=_fake_fetcher({
            "AAA": ([101, 102, 103, 104, 104], "yfinance"),
        }))
        check("feed_has_AAA", "AAA" in feed["closes_map"], True)
        rev_after = per.review_portfolio(as_of="2026-06-05",
                                         closes_map=feed["closes_map"], db_path=db)
        by_after = {p["ticker"]: p for p in rev_after["positions"]}
        check("after_AAA_class_EXIT", by_after["AAA"]["classification"], "EXIT")
        check("after_AAA_reason_connors", by_after["AAA"]["reason"], "connors_sma5")
        check("after_summary_exit_1", rev_after["summary"]["EXIT"], 1)
        check("after_summary_price_missing_0", rev_after["summary"]["PRICE_FEED_MISSING"], 0)
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
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="EXIT-PRICE-FEED-1: free-stack Closes-Snapshot fuer portfolio_exit_review (V7.15)")
    ap.add_argument("--selftest", action="store_true", help="Known-Answer-Tests (offline, injizierter Fetcher)")
    ap.add_argument("--tickers", type=str, default=None,
                    help="Komma-Liste (z.B. AAA,BBB). Ohne -> offene Journal-Positionen (read-only)")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help=f"Anzahl Closes je Ticker in der Map (Default {DEFAULT_LOOKBACK})")
    ap.add_argument("--min-closes", type=int, default=DEFAULT_MIN_CLOSES,
                    help=f"Mindest-Closes, sonst PRICE_FEED_MISSING (Default {DEFAULT_MIN_CLOSES})")
    ap.add_argument("--period-days", type=int, default=DEFAULT_PERIOD_DAYS,
                    help=f"Fetch-Fenster in Kalendertagen (Default {DEFAULT_PERIOD_DAYS})")
    ap.add_argument("--prefer", type=str, default="yfinance", choices=["yfinance", "stooq"],
                    help="bevorzugte Quelle (Default yfinance, dann Stooq-Fallback)")
    ap.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (Default: heute UTC)")
    ap.add_argument("--db", type=str, default=None, help="Pfad zur trade_journal.sqlite (Default: Repo-Root)")
    ap.add_argument("--out", type=str, default=None,
                    help="Pfad fuer die BARE closes-map {TICKER:[...]} (direkt von --closes-json konsumierbar)")
    ap.add_argument("--status-out", type=str, default=None,
                    help="Pfad fuer das volle Status-JSON (closes_map + sources + unavailable)")
    ap.add_argument("--write", action="store_true",
                    help="schreibt signals/closes_<as_of>.json (bare map) + Briefings/Exit-Price-Feed-<as_of>.md")
    ap.add_argument("--quiet", action="store_true", help="kein Status-Markdown auf stdout")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        return selftest()

    as_of = args.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        src_note = "CLI --tickers"
    else:
        tickers = open_position_tickers(db_path=args.db)
        src_note = "offene Journal-Positionen (read-only)"

    # Live-Fetcher mit CLI-Parametern verdrahten
    def _live(ticker: str):
        return default_live_fetcher(ticker, period_days=args.period_days, prefer=args.prefer)

    result = build_closes_map(tickers, lookback=args.lookback,
                              min_closes=args.min_closes, fetcher=_live)
    result["as_of"] = as_of
    result["ticker_source"] = src_note

    print(f"[{TOOL}] as_of={as_of} requested={result['n_requested']} "
          f"fetched={result['n_fetched']} unavailable={result['n_unavailable']} "
          f"(quelle: {src_note})")

    bare = result["closes_map"]

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(bare, indent=2, ensure_ascii=False))
        print(f"[{TOOL}] bare closes-map -> {args.out}")
    if args.status_out:
        Path(args.status_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.status_out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"[{TOOL}] status-json   -> {args.status_out}")

    if args.write:
        sdir = _repo_root() / "signals"
        bdir = _repo_root() / "Briefings"
        sdir.mkdir(exist_ok=True)
        bdir.mkdir(exist_ok=True)
        js_path = sdir / f"closes_{as_of}.json"
        md_path = bdir / f"Exit-Price-Feed-{as_of}.md"
        js_path.write_text(json.dumps(bare, indent=2, ensure_ascii=False))
        md_path.write_text(render_status_markdown(result, as_of=as_of))
        print(f"[{TOOL}] geschrieben: {js_path}")
        print(f"[{TOOL}] geschrieben: {md_path}")

    if not args.quiet:
        print()
        print(render_status_markdown(result, as_of=as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
