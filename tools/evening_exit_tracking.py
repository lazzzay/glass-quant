#!/usr/bin/env python3
"""EXIT-LOOP-WIRING-1 (V7.18): Abend-Tracking-Einstiegspunkt der Exit-Seite.

Schliesst das letzte offene Glied des Exit-Loops (CLAUDE.md §9 EXIT-LOOP-WIRING,
"naechster Feature-Tag"): das 20:00-Abend-Tracking rekonstruierte Exit-Verdikte
bisher AD-HOC pro Session (fehleranfaellig, nicht deterministisch). Dieses Tool
ist die EINE Importstelle, die der Abend-Lauf (und der User per Einzeiler)
stattdessen aufruft:

    open_position_tickers()  ->  build_closes_map()  ->  review_portfolio()
       (exit_price_feed)         (exit_price_feed)       (portfolio_exit_review
                                                          -> exit_engine, SSoT)

KEINE duplizierte Exit-Logik: Journal-Enumeration, Kursfeed und Verdikt kommen
ausschliesslich aus den bestehenden SSoT-Modulen (V7.11/V7.14/V7.15).

Sicherheits-Detail (Lehre aus dem Abend-Tracking 2026-06-09): die Journal-DB
ist im Sandbox-Lauf gelegentlich UNLESBAR (FUSE/WAL EDEADLK, V7.17-Klasse
ENV_UNREADABLE). `list_open_positions` liefert dann []: ein stilles
"keine offenen Positionen" waere ein FALSCHES ALL-CLEAR. Deshalb probet dieses
Tool die DB-Lesbarkeit ZUERST und meldet ehrlich JOURNAL_UNREADABLE (Exit 3,
inkl. Mac-Nachhol-Befehl) statt ein leeres Review vorzutaeuschen.

Status-Klassen / Exit-Codes (Tool-Erfolg != Handlungsbedarf; ein EXIT-Verdikt
ist Exit-Code 0: die Aktion steht im Artefakt):
    NO_POSITIONS        rc=0  Journal lesbar, 0 offene Positionen (kein Fetch)
    OK                  rc=0  alle offenen Positionen mit Kursfeed bewertet
    FEED_DEGRADED       rc=2  >=1 Position ohne Kursfeed (PRICE_FEED_MISSING)
    JOURNAL_UNREADABLE  rc=3  DB fehlt/unlesbar -> KEIN Verdikt, nativ laufen!

Free-Stack only (yfinance -> Stooq via exit_price_feed), Journal strikt
read-only, KEINE Orders. Netz-Fetch hinter injizierbarem `fetcher` ->
Selftest + pytest laufen vollstaendig OFFLINE.

CLI:
    python3 tools/evening_exit_tracking.py --selftest
    python3 tools/evening_exit_tracking.py                 # Status auf stdout
    python3 tools/evening_exit_tracking.py --write         # + kanonische Artefakte

Artefakte bei --write (kanonische Namen, identisch zu V7.14/V7.15-Vertraegen):
    Briefings/Exit-Review-<as_of>.md      deutsches Abend-Sheet (1 Datei = alles)
    signals/exit_review_<as_of>.json      volles Review (V7.14-Format)
    signals/closes_<as_of>.json           bare closes-map (V7.15-Format)
    signals/evening_exit_<as_of>.json     Wiring-Meta (Status/Feed-Health/rc)

Abend-Workflow (Einzeiler, aus dem Repo-Root / 20:00-Session):
    python3 tools/evening_exit_tracking.py --write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

# Geschwister-Module liegen im selben Verzeichnis (kein __init__.py in tools/).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import exit_price_feed as epf  # noqa: E402  (V7.15, Ticker + Closes, SSoT)
import portfolio_exit_review as per  # noqa: E402  (V7.14, Review, SSoT -> exit_engine)

VERSION = "V7.18"
TOOL = "evening_exit_tracking"

STATUS_NO_POSITIONS = "NO_POSITIONS"
STATUS_OK = "OK"
STATUS_FEED_DEGRADED = "FEED_DEGRADED"
STATUS_JOURNAL_UNREADABLE = "JOURNAL_UNREADABLE"

RC_OK = 0
RC_FEED_DEGRADED = 2
RC_JOURNAL_UNREADABLE = 3

_RC_BY_STATUS = {
    STATUS_NO_POSITIONS: RC_OK,
    STATUS_OK: RC_OK,
    STATUS_FEED_DEGRADED: RC_FEED_DEGRADED,
    STATUS_JOURNAL_UNREADABLE: RC_JOURNAL_UNREADABLE,
}

# Repo-relativer Nachhol-Befehl (aus dem Repo-Root ausfuehren).
NATIVE_CMD = "python3 tools/evening_exit_tracking.py --write  (aus dem Repo-Root)"
DB_MISSING_HINT = "No journal found. Run python3 tools/trade_journal.py init first."


# ----------------------------------------------------------------------------
# Pfade
# ----------------------------------------------------------------------------
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db() -> str:
    return str(_repo_root() / "trade_journal.sqlite")


def _display_path(p) -> str:
    """Pfad fuer Artefakte: relativ zum Repo-Root, sonst nur der Dateiname.
    Verhindert, dass absolute lokale Pfade (Home-Verzeichnis) in signals/-JSONs landen."""
    try:
        return str(Path(str(p)).resolve().relative_to(_repo_root()))
    except Exception:  # noqa: BLE001
        return Path(str(p)).name


# ----------------------------------------------------------------------------
# Journal-Lesbarkeits-Probe (gegen False-All-Clear bei EDEADLK/Sandbox)
# ----------------------------------------------------------------------------
def probe_journal(db_path: Optional[str] = None) -> dict:
    """Pruefe ZUERST, ob das Journal wirklich lesbar ist (read-only).

    Liefert {'readable': bool, 'reason': str, 'detail': str, 'n_open': int|None}.
    Wichtig: `list_open_positions` faengt Lese-Fehler und liefert []: fuer den
    Abend-Loop waere das ein stilles, FALSCHES "keine offenen Positionen".
    Diese Probe unterscheidet ehrlich: db_missing / read_error / ok.
    """
    db = db_path or _default_db()
    p = Path(db)
    if not p.exists() or p.is_dir():
        return {"readable": False, "reason": "db_missing",
                "detail": f"nicht gefunden oder kein File: {db}", "n_open": None}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute("SELECT COUNT(*) FROM trades WHERE exit_date IS NULL")
            n_open = int(cur.fetchone()[0])
        finally:
            con.close()
        return {"readable": True, "reason": "ok", "detail": "", "n_open": n_open}
    except Exception as e:  # EDEADLK / disk I/O / schema kaputt -> ehrlich melden
        return {"readable": False, "reason": "read_error",
                "detail": str(e)[:200], "n_open": None}


# ----------------------------------------------------------------------------
# Kern: EIN Abend-Lauf (die EINE Importstelle)
# ----------------------------------------------------------------------------
def run_evening_exit(*, as_of: Optional[str] = None,
                     db_path: Optional[str] = None,
                     fetcher: Optional[Callable] = None,
                     lookback: int = epf.DEFAULT_LOOKBACK,
                     min_closes: int = epf.DEFAULT_MIN_CLOSES,
                     write: bool = False,
                     briefings_dir: Optional[str] = None,
                     signals_dir: Optional[str] = None) -> dict:
    """Fuehre den kompletten Abend-Exit-Loop aus (read-only, free-stack).

    fetcher(ticker) -> (closes|None, source); Default = free-stack-Live-Fetcher
    aus exit_price_feed (yfinance -> Stooq). In NO_POSITIONS- und
    JOURNAL_UNREADABLE-Faellen wird der Fetcher NIE aufgerufen (kein Netz noetig).
    """
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = db_path or _default_db()

    probe = probe_journal(db)
    feed: Optional[dict] = None
    review: Optional[dict] = None

    if not probe["readable"]:
        status = STATUS_JOURNAL_UNREADABLE
    else:
        tickers = epf.open_position_tickers(db_path=db)
        if not tickers:
            status = STATUS_NO_POSITIONS
            review = per.review_portfolio(as_of=as_of, closes_map={}, db_path=db)
        else:
            feed = epf.build_closes_map(tickers, lookback=lookback,
                                        min_closes=min_closes, fetcher=fetcher)
            review = per.review_portfolio(as_of=as_of,
                                          closes_map=feed["closes_map"], db_path=db)
            missing = review["summary"].get("PRICE_FEED_MISSING", 0)
            status = STATUS_FEED_DEGRADED if missing > 0 else STATUS_OK

    result = {
        "tool": TOOL,
        "version": VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of,
        "db_path": _display_path(db),
        "status": status,
        "exit_code": _RC_BY_STATUS[status],
        "journal_probe": probe,
        "n_open": (review or {}).get("n_open", probe.get("n_open")),
        "feed": ({k: feed[k] for k in ("n_requested", "n_fetched", "n_unavailable",
                                       "sources", "unavailable", "closes_map")}
                 if feed else None),
        "review": review,
        "artifacts": [],
    }

    if write:
        result["artifacts"] = _write_artifacts(result,
                                               briefings_dir=briefings_dir,
                                               signals_dir=signals_dir)
    return result


# ----------------------------------------------------------------------------
# Rendering (deutsches Abend-Sheet: 1 Datei = kompletter Exit-Status)
# ----------------------------------------------------------------------------
def render_evening_markdown(result: dict) -> str:
    as_of = result.get("as_of", "?")
    status = result.get("status", "?")
    lines = []
    lines.append(f"# Abend-Exit-Tracking {as_of}: Exit-Loop ({VERSION})")
    lines.append("")
    lines.append(f"> Generiert (UTC): {result.get('generated_utc','?')} · "
                 f"EINE Importstelle: open_position_tickers → build_closes_map → "
                 f"review_portfolio (exit_engine SSoT) · Journal read-only · Free-Stack")
    lines.append("")
    lines.append(f"**Status:** `{status}` (rc={result.get('exit_code','?')})")
    lines.append("")

    if status == STATUS_JOURNAL_UNREADABLE:
        probe = result.get("journal_probe", {})
        lines.append("## 🔴 JOURNAL NICHT LESBAR, KEIN Verdikt moeglich")
        lines.append("")
        lines.append(f"- Grund: `{probe.get('reason','?')}` {probe.get('detail','')}".rstrip())
        if probe.get("reason") == "db_missing":
            # Frischer Clone: es gibt schlicht noch kein Journal (kein Sandbox-Problem).
            lines.append(f"- {DB_MISSING_HINT}")
            lines.append("- Kein Journal gefunden. Zuerst das Journal anlegen: "
                         "`python3 tools/trade_journal.py init`")
            lines.append("- Danach erneut ausfuehren:")
        else:
            lines.append("- Das ist KEIN 'keine offenen Positionen': die DB konnte nicht "
                         "gelesen werden (typisch: Sandbox-FUSE/WAL EDEADLK, V7.17-Klasse "
                         "ENV_UNREADABLE; echte Korruption waere auch hier sichtbar).")
            lines.append("- **Nachholen nativ auf dem Mac (1 Befehl):**")
        lines.append("")
        lines.append("```bash")
        lines.append(NATIVE_CMD)
        lines.append("```")
        lines.append("")
        return "\n".join(lines)

    review = result.get("review") or {}
    actions = review.get("actions_required", [])
    exits = [a for a in actions if a.get("classification") == "EXIT"]
    if exits:
        lines.append("## 🔴 HEUTE HANDELN (Exit zum naechsten Open)")
        lines.append("")
        for a in exits:
            lines.append(f"- **{a['ticker']}: EXIT**: Regel `{a.get('reason','?')}`")
        lines.append("")

    feed = result.get("feed")
    if feed:
        lines.append(f"**Kursfeed:** angefragt {feed.get('n_requested',0)} · "
                     f"geliefert {feed.get('n_fetched',0)} · "
                     f"fehlend {feed.get('n_unavailable',0)}"
                     + (f" · Quellen: {feed.get('sources')}" if feed.get("sources") else ""))
        lines.append("")
        if status == STATUS_FEED_DEGRADED:
            lines.append("⚠️ **FEED_DEGRADED**: mind. 1 Position ohne Kursfeed "
                         "(PRICE_FEED_MISSING, Verdikt nur vorlaeufig). Im Sandbox-Lauf "
                         "normal (kein yfinance/Egress) → nativ nachholen:")
            lines.append("")
            lines.append("```bash")
            lines.append(NATIVE_CMD)
            lines.append("```")
            lines.append("")

    if review:
        lines.append(per.render_markdown(review))

    lines.append(f"> Wiring-Vertrag {VERSION}: Abend-Tracking 20:00 ruft genau dieses "
                 "Tool statt Ad-hoc-Exit-Logik. Exit-Codes: 0 ok / 2 feed-degraded / "
                 "3 journal-unreadable. Keine Order automatisch, die Ausfuehrung entscheidet der User.")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Artefakte (kanonische Namen: identisch zu V7.14/V7.15-Vertraegen)
# ----------------------------------------------------------------------------
def _write_artifacts(result: dict, *, briefings_dir: Optional[str] = None,
                     signals_dir: Optional[str] = None) -> list:
    as_of = result["as_of"]
    bdir = Path(briefings_dir) if briefings_dir else _repo_root() / "Briefings"
    sdir = Path(signals_dir) if signals_dir else _repo_root() / "signals"
    bdir.mkdir(parents=True, exist_ok=True)
    sdir.mkdir(parents=True, exist_ok=True)
    written = []

    md_path = bdir / f"Exit-Review-{as_of}.md"
    md_path.write_text(render_evening_markdown(result))
    written.append(str(md_path))

    if result.get("review") is not None:
        js_path = sdir / f"exit_review_{as_of}.json"
        js_path.write_text(json.dumps(result["review"], indent=2, ensure_ascii=False))
        written.append(str(js_path))

    feed = result.get("feed")
    if feed is not None:
        closes_path = sdir / f"closes_{as_of}.json"
        closes_path.write_text(json.dumps(feed.get("closes_map", {}),
                                          indent=2, ensure_ascii=False))
        written.append(str(closes_path))

    meta = {k: v for k, v in result.items() if k != "artifacts"}
    meta_path = sdir / f"evening_exit_{as_of}.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    written.append(str(meta_path))
    return written


# ----------------------------------------------------------------------------
# Selftest (Known-Answer, vollstaendig OFFLINE)
# ----------------------------------------------------------------------------
def _make_db(path: str, rows: Sequence[tuple]) -> None:
    con = sqlite3.connect(path)
    con.executescript(per._SCHEMA)
    con.executemany(
        "INSERT INTO trades (source,ticker,entry_date,position_size,entry_price,"
        "stop,target,side,holding_days,max_hold_days,exit_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        list(rows),
    )
    con.commit()
    con.close()


def _counting_fetcher(series: dict, calls: list) -> Callable:
    def _f(ticker: str):
        calls.append(ticker)
        v = series.get(ticker)
        return (None, "none") if v is None else v
    return _f


def selftest() -> int:
    import hashlib
    import os
    import tempfile

    cases = []

    def check(name, got, exp):
        ok = (got == exp)
        cases.append((name, ok, str(got), str(exp)))
        return ok

    tmp = tempfile.mkdtemp(prefix="eet_")
    bdir = os.path.join(tmp, "Briefings")
    sdir = os.path.join(tmp, "signals")

    # --- 1) NO_POSITIONS: leeres Journal -> kein Fetch, rc=0 -----------------
    db0 = os.path.join(tmp, "empty.sqlite")
    _make_db(db0, [])
    calls0 = []
    r0 = run_evening_exit(as_of="2026-06-10", db_path=db0,
                          fetcher=_counting_fetcher({}, calls0))
    check("nopos_status", r0["status"], STATUS_NO_POSITIONS)
    check("nopos_rc", r0["exit_code"], RC_OK)
    check("nopos_no_fetch", calls0, [])
    check("nopos_n_open", r0["n_open"], 0)
    check("nopos_md", "Keine offenen Positionen" in render_evening_markdown(r0), True)

    # --- 2) OK: Position mit Feed -> EXIT/connors_sma5 propagiert ------------
    db1 = os.path.join(tmp, "one.sqlite")
    _make_db(db1, [("live", "AAA", "2026-06-08", 1500.0, 100.0, 95.0, 115.0,
                    "long", 1, 3, None)])
    calls1 = []
    r1 = run_evening_exit(as_of="2026-06-10", db_path=db1,
                          fetcher=_counting_fetcher(
                              {"AAA": ([101, 102, 103, 104, 104], "yfinance")}, calls1))
    check("ok_status", r1["status"], STATUS_OK)
    check("ok_rc", r1["exit_code"], RC_OK)
    check("ok_fetch_called_once", calls1, ["AAA"])
    by1 = {p["ticker"]: p for p in r1["review"]["positions"]}
    check("ok_exit_verdict", by1["AAA"]["classification"], "EXIT")
    check("ok_exit_reason", by1["AAA"]["reason"], "connors_sma5")
    md1 = render_evening_markdown(r1)
    check("ok_md_handeln", "HEUTE HANDELN" in md1, True)
    check("ok_md_ticker", "AAA: EXIT" in md1, True)

    # --- 3) FEED_DEGRADED: 2. Position ohne Feed -> rc=2 ----------------------
    db2 = os.path.join(tmp, "two.sqlite")
    _make_db(db2, [
        ("live", "AAA", "2026-06-08", 1500.0, 100.0, 95.0, 115.0, "long", 1, 3, None),
        ("live", "DDD", "2026-06-09", 1500.0, 50.0, 45.0, 60.0, "long", 1, 3, None),
    ])
    r2 = run_evening_exit(as_of="2026-06-10", db_path=db2,
                          fetcher=_counting_fetcher(
                              {"AAA": ([101, 102, 103, 104, 104], "yfinance")}, []))
    check("deg_status", r2["status"], STATUS_FEED_DEGRADED)
    check("deg_rc", r2["exit_code"], RC_FEED_DEGRADED)
    check("deg_missing_count", r2["review"]["summary"]["PRICE_FEED_MISSING"], 1)
    check("deg_md_warn", "FEED_DEGRADED" in render_evening_markdown(r2), True)

    # --- 4) JOURNAL_UNREADABLE: fehlende DB + Verzeichnis-als-DB -> rc=3 ------
    calls4 = []
    r4a = run_evening_exit(as_of="2026-06-10",
                           db_path=os.path.join(tmp, "missing.sqlite"),
                           fetcher=_counting_fetcher({}, calls4))
    check("unread_missing_status", r4a["status"], STATUS_JOURNAL_UNREADABLE)
    check("unread_missing_rc", r4a["exit_code"], RC_JOURNAL_UNREADABLE)
    check("unread_missing_reason", r4a["journal_probe"]["reason"], "db_missing")
    r4b = run_evening_exit(as_of="2026-06-10", db_path=tmp,
                           fetcher=_counting_fetcher({}, calls4))
    check("unread_dir_status", r4b["status"], STATUS_JOURNAL_UNREADABLE)
    check("unread_no_fetch", calls4, [])
    check("unread_no_review", r4b["review"], None)
    md4 = render_evening_markdown(r4b)
    check("unread_md_warning", "JOURNAL NICHT LESBAR" in md4, True)
    check("unread_md_native_cmd", "evening_exit_tracking.py --write" in md4, True)

    # --- 5) --write: kanonische Artefakte + read-only-Beweis ------------------
    before = hashlib.sha256(Path(db1).read_bytes()).hexdigest()
    r5 = run_evening_exit(as_of="2026-06-10", db_path=db1,
                          fetcher=_counting_fetcher(
                              {"AAA": ([101, 102, 103, 104, 104], "yfinance")}, []),
                          write=True, briefings_dir=bdir, signals_dir=sdir)
    after = hashlib.sha256(Path(db1).read_bytes()).hexdigest()
    check("write_db_untouched", before == after, True)
    names = sorted(Path(p).name for p in r5["artifacts"])
    check("write_artifact_names", names, sorted([
        "Exit-Review-2026-06-10.md", "exit_review_2026-06-10.json",
        "closes_2026-06-10.json", "evening_exit_2026-06-10.json"]))
    meta = json.loads((Path(sdir) / "evening_exit_2026-06-10.json").read_text())
    check("write_meta_status", meta["status"], STATUS_OK)
    closes = json.loads((Path(sdir) / "closes_2026-06-10.json").read_text())
    check("write_closes_bare_map", closes, {"AAA": [101.0, 102.0, 103.0, 104.0, 104.0]})
    check("write_md_exists", (Path(bdir) / "Exit-Review-2026-06-10.md").exists(), True)

    # --- 6) UNREADABLE + write: Warn-Artefakt entsteht trotzdem ---------------
    r6 = run_evening_exit(as_of="2026-06-11", db_path=os.path.join(tmp, "missing.sqlite"),
                          fetcher=_counting_fetcher({}, []),
                          write=True, briefings_dir=bdir, signals_dir=sdir)
    names6 = sorted(Path(p).name for p in r6["artifacts"])
    check("unread_write_artifacts", names6, sorted([
        "Exit-Review-2026-06-11.md", "evening_exit_2026-06-11.json"]))
    check("unread_write_md_warn",
          "JOURNAL NICHT LESBAR" in (Path(bdir) / "Exit-Review-2026-06-11.md").read_text(),
          True)

    # --- 7) as_of-Propagation --------------------------------------------------
    check("as_of_propagated", r5["review"]["as_of"], "2026-06-10")

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
        description="EXIT-LOOP-WIRING-1: Abend-Exit-Loop in einem Aufruf (V7.18)")
    ap.add_argument("--selftest", action="store_true", help="Known-Answer-Tests (offline)")
    ap.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (Default: heute UTC)")
    ap.add_argument("--db", type=str, default=None, help="Pfad zur trade_journal.sqlite")
    ap.add_argument("--lookback", type=int, default=epf.DEFAULT_LOOKBACK,
                    help=f"Closes je Ticker (Default {epf.DEFAULT_LOOKBACK})")
    ap.add_argument("--min-closes", type=int, default=epf.DEFAULT_MIN_CLOSES,
                    help=f"Mindest-Closes, sonst PRICE_FEED_MISSING (Default {epf.DEFAULT_MIN_CLOSES})")
    ap.add_argument("--write", action="store_true",
                    help="kanonische Artefakte schreiben (Exit-Review-MD + signals/*.json)")
    ap.add_argument("--quiet", action="store_true", help="kein Markdown auf stdout")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        return selftest()

    result = run_evening_exit(as_of=args.as_of, db_path=args.db,
                              lookback=args.lookback, min_closes=args.min_closes,
                              write=args.write)
    s = (result.get("review") or {}).get("summary", {})
    print(f"[{TOOL}] as_of={result['as_of']} status={result['status']} "
          f"rc={result['exit_code']} open={result.get('n_open')} "
          f"EXIT={s.get('EXIT', '-')} HOLD={s.get('HOLD', '-')} "
          f"FEED_MISSING={s.get('PRICE_FEED_MISSING', '-')}")
    for a in result.get("artifacts", []):
        print(f"[{TOOL}] geschrieben: {a}")
    if (result.get("journal_probe") or {}).get("reason") == "db_missing":
        print(f"[{TOOL}] {DB_MISSING_HINT}", file=sys.stderr)
    if not args.quiet:
        print()
        print(render_evening_markdown(result))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
