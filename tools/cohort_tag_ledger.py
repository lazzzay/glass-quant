#!/usr/bin/env python3
"""
COHORT-TAG-LEDGER-1 (V7.30 · 2026-06-24 Mi KW26 · FEATURE).

Read-only Producer fuer `signals/entry_filter_forward_ledger.json`: den exakt
markierten naechsten H2-Engpass nach V7.29.

Kontext
-------
V7.29 (`tools/entry_filter_forward_test.py`) hat die drei robusten Entry-Filter
(atr_pct ≫ regime ≳ rsi14) per OOS-Split out-of-sample bestaetigt
(`oos_proxy_holds=TRUE`) und mit `assess_live_forward()` ein **fail-soft
Geruest** fuer den echten post-heute-Forward-Test angelegt. Dieses Geruest
konsumiert `signals/entry_filter_forward_ledger.json`. Heute existiert die Datei
nicht → `live_forward_ready=False`, `live_forward_n=0`, Grund maschinenlesbar =
COHORT-TAGGING-BACKLOG (Live-Trades im Journal ohne atr_pct/regime/rsi14-Entry-
Tags).

Dieses Tool ist der **Producer** dieses Ledgers. Es liest read-only
  (1) das `trade_journal.sqlite` (Live-/External-/Disc-Trades: Datum, Return,
      Netto, atr_pct, regime/regime_at_entry, setup_fk), und
  (2) das `scan_<entry_date>.json`-Archiv (per-Ticker rsi2/rsi5/rsi14/triple_rsi,
      sector, tier, v6_score, atr_pct) + das `regime_<entry_date>.json`-Archiv,
und **mergt** beides zu getaggten Ledger-Zeilen im kanonischen V6.1-Schema (genau
die Spalten, die `load_trades`/`_numeric_ic`/`_regime_directional` der Entry-
Diagnose-Familie konsumieren). Pro Trade wird transparent ausgewiesen, ob das
Tagging COMPLETE / PARTIAL / UNTAGGABLE ist und WARUM (`missing_fields`,
`tag_reason`): das macht den COHORT-TAGGING-BACKLOG erstmals maschinenlesbar +
quantifiziert.

Wirkung
-------
Sobald dieses Ledger existiert, flippt `entry_filter_forward_test.assess_live_
forward()` von "kein Ledger / Backlog" zu "N getaggte Live-Trades >= cutoff;
ready ab 40" messbarer Fortschritt auf genau der Metrik, die V7.29 als offen
markiert hat. `live_forward_n` zaehlt erfasste post-cutoff Live-Trades; die
**Feature-Vollstaendigkeit** (ob eine Zeile fuer den Forward-Test wirklich
nutzbare Kandidaten-Features traegt) wird SEPARAT als `forward_eligible` +
Summary `n_post_cutoff_forward_eligible` gefuehrt (Ehrlichkeits-Trennung:
"erfasst" ≠ "feature-komplett").

EISERNE REGEL (CLAUDE.md §3/§8): NUR Daten-Plumbing. Aendert KEINE Live-Regel,
KEINE Schwelle, KEIN Sizing, KEIN Scoring; schreibt NICHT ins Journal (read-only,
`mode=ro&immutable=1`), NIE Raw-SQL-UPDATE. Output sind zwei NEUE Artefakte
(Ledger-JSON + dt. Briefing-MD). Size/Hebel NIE vor H2: auch dieses Tool baut
nur den Daten-Unterbau fuer den spaeteren echten Forward-Test.

Fail-soft (V7.17-Pattern): Journal nicht lesbar (Errno 35 EDEADLK / Datei fehlt)
→ leeres Ledger mit maschinenlesbarem Grund, Exit-Code 2; KEIN Crash, KEIN
falsches "0 Live-Trades". Scan/Regime-Archiv-Reader EDEADLK-resilient (geteilt
mit V7.26/27/29 `read_text_resilient`).

CLI:
    python3 tools/cohort_tag_ledger.py                 # Vorschau (kein Write)
    python3 tools/cohort_tag_ledger.py --write         # Ledger + Briefing schreiben
    python3 tools/cohort_tag_ledger.py --date 2026-06-24 --write
    python3 tools/cohort_tag_ledger.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Journal nicht lesbar (EDEADLK persistent / Datei fehlt / keine Tabelle)
    3  Selftest fail

Output JSON : signals/entry_filter_forward_ledger.json  (der konsumierte Ledger)
Output MD   : Briefings/Cohort-Tag-Ledger-<date>.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date as _date
from pathlib import Path
from typing import Callable, Optional

# Geteilter EDEADLK-resilienter Reader (Single-Source-of-Truth, identisch zu
# V7.26/27/29), kein Copy-Paste.
from entry_filter_orthogonality import read_text_resilient  # noqa: E402

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _ROOT / "trade_journal.sqlite"
_DEFAULT_SCAN_DIR = _ROOT / "tools"
_DEFAULT_REGIME_DIR = _ROOT / "tools"
_LEDGER_PATH = _ROOT / "signals" / "entry_filter_forward_ledger.json"

# Identisch zur Konvention in entry_filter_forward_test.py / exit_forward_test.py.
_OOS_CUTOFF = "2025-01-01"
_MIN_FORWARD_N = 40  # = entry_filter_forward_test._MIN_TEST_N (ready-Gate)

# Die von V7.27/V7.29 vor-registrierten Kandidaten: bestimmt, was "feature-
# komplett genug fuer den Forward-Test" bedeutet.
_CANDIDATE_NUMERIC = ("atr_pct", "rsi14")
_CANDIDATE_REGIME_CELLS = ("Trending-Divergent", "Bullish-Confirmed")

# Live-relevante Sources (Backtest + Shadow gehoeren NICHT in den Live-Forward).
_LIVE_SOURCES = ("live", "external", "disc")

# Journal-Spalten, die wir (falls vorhanden) ziehen.
_WANT_COLS = [
    "ticker", "source", "entry_date", "exit_date", "atr_pct", "side",
    "return_pct", "netto_pnl", "is_win", "regime", "regime_at_entry",
    "setup_fk", "system_version", "holding_days",
]


# --------------------------------------------------------------------------- #
# Journal-Reader (read-only, EDEADLK fail-soft, connector-injizierbar)
# --------------------------------------------------------------------------- #
def _ro_connect(db_path: str | Path) -> sqlite3.Connection:
    """Read-only immutable Connection: kann das Journal NIE veraendern."""
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=2.0)


def load_journal_trades(
    db_path: str | Path,
    *,
    sources: tuple[str, ...] = _LIVE_SOURCES,
    connector: Optional[Callable[[str | Path], sqlite3.Connection]] = None,
) -> tuple[list[dict], Optional[str]]:
    """Liest Live-/External-/Disc-Trades read-only. Returns (rows, error).

    Fail-soft: bei EDEADLK / fehlender Datei / fehlender Tabelle -> ([], reason).
    `connector` ist fuer den hermetischen Selftest injizierbar (In-Memory-DB).
    """
    p = Path(db_path)
    if connector is None and not p.exists():
        return [], f"Journal-Datei fehlt: {p.name}"
    try:
        con = connector(db_path) if connector else _ro_connect(db_path)
    except sqlite3.Error as e:  # noqa: BLE001
        return [], f"Journal nicht lesbar ({type(e).__name__}: {e})"
    try:
        cur = con.cursor()
        cols = [r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()]
        if not cols:
            return [], "Journal hat keine trades-Tabelle (leer/Stub)"
        sel = [c for c in _WANT_COLS if c in cols]
        if "ticker" not in sel or "entry_date" not in sel:
            return [], "Journal-Schema ohne ticker/entry_date"
        ph = ",".join("?" * len(sources))
        sql = (f"SELECT {','.join(sel)} FROM trades "
               f"WHERE source IN ({ph}) ORDER BY entry_date")
        rows = cur.execute(sql, tuple(sources)).fetchall()
        return [dict(zip(sel, r)) for r in rows], None
    except sqlite3.Error as e:  # noqa: BLE001
        return [], f"Journal-Query fehlgeschlagen ({type(e).__name__}: {e})"
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Scan- + Regime-Archiv-Reader (read-only, EDEADLK-resilient)
# --------------------------------------------------------------------------- #
def _find_ticker_rows(obj) -> Optional[list[dict]]:
    """Findet die per-Ticker-Zeilenliste in einem scan_<date>.json.

    Bevorzugt `ticker_map`; faellt sonst auf die erste dict-Liste mit `ticker`.
    """
    if isinstance(obj, dict):
        tm = obj.get("ticker_map")
        if isinstance(tm, list) and tm and isinstance(tm[0], dict):
            return tm
        for v in obj.values():
            r = _find_ticker_rows(v)
            if r:
                return r
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "ticker" in obj[0]:
        return obj
    return None


def load_scan_features(scan_dir: str | Path, date: str, ticker: str) -> Optional[dict]:
    """Entry-Features fuer `ticker` am `date` aus scan_<date>.json, oder None.

    None bedeutet: kein Scan-Archiv fuer das Datum ODER Ticker nicht im Scan.
    Die Unterscheidung liefert `scan_file_present()`.
    """
    p = Path(scan_dir) / f"scan_{date}.json"
    txt = read_text_resilient(p)
    if txt is None:
        return None
    try:
        data = json.loads(txt)
    except ValueError:
        return None
    rows = _find_ticker_rows(data)
    if not rows:
        return None
    tu = str(ticker).upper()
    for r in rows:
        if str(r.get("ticker", "")).upper() == tu:
            return {
                "name": r.get("name"),
                "sector": r.get("sector") or r.get("gics_sector"),
                "tier": r.get("tier"),
                "v6_score": r.get("pre_score"),
                "triple_rsi": r.get("triple_rsi"),
                "rsi2": r.get("rsi2"),
                "rsi5": r.get("rsi5"),
                "rsi14": r.get("rsi14"),
                "atr_pct": r.get("atr_pct"),
            }
    return None


def scan_file_present(scan_dir: str | Path, date: str) -> bool:
    """True wenn scan_<date>.json existiert (egal ob Ticker drin): fuer den Grund."""
    if not date:
        return False
    return (Path(scan_dir) / f"scan_{date}.json").exists()


def load_regime_archive(regime_dir: str | Path) -> dict[str, str]:
    """Date(str) -> regime(str) aus regime_*.json (regime_dir + Repo-Root)."""
    cache: dict[str, str] = {}
    rd = Path(regime_dir)
    cands: list[Path] = []
    if rd.exists():
        cands.extend(sorted(rd.glob("regime_*.json")))
    root = rd.parent if rd.name == "tools" else rd
    if root.exists() and root != rd:
        cands.extend(sorted(root.glob("regime_*.json")))
    for p in cands:
        txt = read_text_resilient(p)
        if txt is None:
            continue
        try:
            d = json.loads(txt)
        except ValueError:
            continue
        dt = str(d.get("date") or "").strip()
        reg = str(d.get("regime") or "").strip()
        if dt and reg and dt not in cache:
            cache[dt] = reg
    return cache


# --------------------------------------------------------------------------- #
# Tagging
# --------------------------------------------------------------------------- #
def _clean_regime(*vals) -> Optional[str]:
    """Erster nicht-leerer, nicht-'?'-Regime-Wert (Journal schreibt '?' = unbekannt)."""
    for v in vals:
        s = str(v if v is not None else "").strip()
        if s and s != "?":
            return s
    return None


def tag_trade(jrow: dict, scan_feat: Optional[dict], regime: Optional[str],
              scan_present: bool) -> dict:
    """Mergt einen Journal-Trade + Scan-Features + Regime zu einer Ledger-Zeile
    im kanonischen V6.1-Schema, inkl. Provenienz/Vollstaendigkeit."""
    date = str(jrow.get("entry_date") or "")
    feat = scan_feat or {}
    # atr_pct: Journal bevorzugt (Entry-Zeit-ATR%), sonst Scan-Wert.
    atr = jrow.get("atr_pct")
    if atr is None:
        atr = feat.get("atr_pct")

    row = {
        # --- kanonisches V6.1-Schema (von load_trades/Forward-Test konsumiert) ---
        "date": date,
        "ticker": jrow.get("ticker"),
        "name": feat.get("name"),
        "tier": feat.get("tier"),
        "sector": feat.get("sector"),
        "regime": regime,
        "v6_score": feat.get("v6_score"),
        "triple_rsi": feat.get("triple_rsi"),
        "rsi2": feat.get("rsi2"),
        "rsi5": feat.get("rsi5"),
        "rsi14": feat.get("rsi14"),
        "atr_pct": atr,
        "return_pct": jrow.get("return_pct"),
        "is_win": jrow.get("is_win"),
        "netto_pnl": jrow.get("netto_pnl"),
        "v61_netto_pnl": jrow.get("netto_pnl"),   # live mr_v61 realized net
        "v61_skip": "",                            # Live-Trades sind nie skip
        "v61_exit_reason": jrow.get("setup_fk") or "",
        # --- Provenienz / Live-Kontext ---
        "source": jrow.get("source"),
        "setup_fk": jrow.get("setup_fk"),
        "holding_days": jrow.get("holding_days"),
    }

    have_atr = row["atr_pct"] is not None
    have_rsi14 = row["rsi14"] is not None
    have_regime = regime is not None
    have_return = row["return_pct"] is not None

    sources: list[str] = []
    if jrow.get("atr_pct") is not None:
        sources.append("journal_atr")
    if scan_feat:
        sources.append("scan")
    if have_regime:
        sources.append("regime")

    missing: list[str] = []
    if not have_atr:
        missing.append("atr_pct")
    if not have_rsi14:
        missing.append("rsi14")
    if not have_regime:
        missing.append("regime")
    if not have_return:
        missing.append("return_pct")

    # forward_eligible: mindestens EIN registrierter Kandidat nutzbar + Return da.
    candidate_usable = have_atr or have_rsi14 or (regime in _CANDIDATE_REGIME_CELLS)
    forward_eligible = bool(candidate_usable and have_return)

    if have_atr and have_rsi14 and have_regime and have_return:
        status = "COMPLETE"
    elif have_atr or have_rsi14 or have_regime:
        status = "PARTIAL"
    else:
        status = "UNTAGGABLE"

    # Maschinenlesbarer Grund (loest COHORT-TAGGING-BACKLOG-Sichtbarkeit).
    if status == "COMPLETE":
        reason = "voll getaggt (Scan + Regime + Return vorhanden)"
    elif not scan_present and date:
        reason = (f"kein scan_{date}.json archiviert (pre-Tagging-Aera / Entry-Tag "
                  f"wurde am Handelstag nicht konserviert) → numerische Features fehlen")
    elif scan_present and not scan_feat:
        reason = f"Ticker nicht im scan_{date}.json ticker_map (Blacklist/Universe-Diff)"
    elif not have_regime:
        reason = "Regime unbekannt ('?' im Journal, kein regime_<date>.json-Treffer)"
    else:
        reason = f"Teil-Tag: es fehlen {', '.join(missing)}"

    row["tag_status"] = status
    row["tag_sources"] = sources
    row["missing_fields"] = missing
    row["forward_eligible"] = forward_eligible
    row["tag_reason"] = reason
    return row


# --------------------------------------------------------------------------- #
# Ledger-Bau
# --------------------------------------------------------------------------- #
def build_ledger(
    db_path: str | Path,
    scan_dir: str | Path,
    regime_dir: str | Path,
    cutoff: str,
    run_date: str,
    *,
    connector: Optional[Callable[[str | Path], sqlite3.Connection]] = None,
    journal_loader: Optional[Callable[[], tuple[list[dict], Optional[str]]]] = None,
) -> dict:
    """Baut das vollstaendige Ledger-Objekt (read-only)."""
    if journal_loader is not None:
        trades, err = journal_loader()
    else:
        trades, err = load_journal_trades(db_path, connector=connector)

    regime_archive = load_regime_archive(regime_dir)

    tagged: list[dict] = []
    for jr in trades:
        date = str(jr.get("entry_date") or "")
        scan_feat = load_scan_features(scan_dir, date, jr.get("ticker")) if date else None
        present = scan_file_present(scan_dir, date)
        regime = _clean_regime(jr.get("regime_at_entry"), jr.get("regime"),
                               regime_archive.get(date))
        tagged.append(tag_trade(jr, scan_feat, regime, present))

    post = [t for t in tagged if t["date"] >= cutoff]
    by_status: dict[str, int] = {}
    for t in tagged:
        by_status[t["tag_status"]] = by_status.get(t["tag_status"], 0) + 1

    summary = {
        "n_trades": len(tagged),
        "n_post_cutoff": len(post),
        "n_complete": sum(1 for t in tagged if t["tag_status"] == "COMPLETE"),
        "n_partial": sum(1 for t in tagged if t["tag_status"] == "PARTIAL"),
        "n_untaggable": sum(1 for t in tagged if t["tag_status"] == "UNTAGGABLE"),
        "n_post_cutoff_forward_eligible": sum(1 for t in post if t["forward_eligible"]),
        "by_status": by_status,
        "min_forward_n": _MIN_FORWARD_N,
        "forward_ready": sum(1 for t in post if t["forward_eligible"]) >= _MIN_FORWARD_N,
        "journal_error": err,
    }
    backlog = _assess_backlog(tagged)

    return {
        "tool": "cohort_tag_ledger",
        "version": "V7.30",
        "date": run_date,
        "oos_cutoff": cutoff,
        "schema": ("canonical-v61: date,ticker,name,tier,sector,regime,v6_score,"
                   "triple_rsi,rsi2,rsi5,rsi14,atr_pct,return_pct,is_win,"
                   "v61_netto_pnl,v61_skip + provenance(tag_status/missing_fields/"
                   "forward_eligible/tag_reason)"),
        "source_db": Path(db_path).name,
        "consumed_by": "entry_filter_forward_test.assess_live_forward()",
        "honesty": ("live_forward_n zaehlt ERFASSTE post-cutoff Live-Trades; "
                    "feature-Vollstaendigkeit separat als forward_eligible / "
                    "n_post_cutoff_forward_eligible. 'erfasst' != 'feature-komplett'. "
                    "KEINE Live-Regel/Sizing geaendert (read-only)."),
        "summary": summary,
        "backlog": backlog,
        "trades": tagged,
    }


def _assess_backlog(tagged: list[dict]) -> dict:
    """Aggregiert WARUM Trades nicht voll getaggt sind (Backlog-Sichtbarkeit)."""
    open_rows = [t for t in tagged if t["tag_status"] != "COMPLETE"]
    by_reason: dict[str, int] = {}
    for t in open_rows:
        by_reason[t["tag_reason"]] = by_reason.get(t["tag_reason"], 0) + 1
    missing_counter: dict[str, int] = {}
    for t in open_rows:
        for m in t["missing_fields"]:
            missing_counter[m] = missing_counter.get(m, 0) + 1
    return {
        "n_open": len(open_rows),
        "by_reason": dict(sorted(by_reason.items(), key=lambda x: -x[1])),
        "missing_field_counts": dict(sorted(missing_counter.items(), key=lambda x: -x[1])),
        "resolution": ("Going-forward: jeder Live-Trade, der an einem Tag MIT "
                       "scan_<date>.json eingegangen ist, wird hier automatisch voll "
                       "getaggt. Historische pre-Tagging-Trades bleiben PARTIAL, bis "
                       "ein scan_<entry_date>.json verfuegbar ist."),
    }


# --------------------------------------------------------------------------- #
# Markdown-Render (deutsch)
# --------------------------------------------------------------------------- #
def _fmt(x) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def render_md(rep: dict) -> str:
    s = rep["summary"]
    L: list[str] = []
    L.append(f"# Cohort-Tag-Ledger (H2-Phase-III Producer): {rep.get('date')}")
    L.append("")
    L.append(f"**Tool** `cohort_tag_ledger.py` (V7.30) · read-only · "
             f"Quelle `{rep.get('source_db')}` · Cutoff **{rep.get('oos_cutoff')}**")
    L.append(f"**Konsumiert von** `{rep.get('consumed_by')}`")
    L.append("")
    if s.get("journal_error"):
        L.append(f"> ⚠️ Journal nicht voll lesbar: {s['journal_error']}: "
                 f"Ledger ggf. leer/fail-soft.")
        L.append("")
    L.append("## Zusammenfassung")
    L.append("")
    L.append(f"- Live-/External-/Disc-Trades erfasst: **{s['n_trades']}** "
             f"(post-cutoff ≥ {rep.get('oos_cutoff')}: {s['n_post_cutoff']})")
    L.append(f"- Tag-Status: COMPLETE {s['n_complete']} · PARTIAL {s['n_partial']} · "
             f"UNTAGGABLE {s['n_untaggable']}")
    L.append(f"- **forward_eligible (post-cutoff, feature-nutzbar): "
             f"{s['n_post_cutoff_forward_eligible']}** · ready-Gate {s['min_forward_n']} → "
             f"forward_ready: **{s['forward_ready']}**")
    L.append("")
    L.append("## Getaggte Trades")
    L.append("")
    L.append("| Datum | Ticker | Src | Regime | atr_pct | rsi14 | return% | Status | fwd? |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for t in rep["trades"]:
        L.append(f"| {t['date']} | {t['ticker']} | {t.get('source','')} | "
                 f"{_fmt(t['regime'])} | {_fmt(t['atr_pct'])} | {_fmt(t['rsi14'])} | "
                 f"{_fmt(t['return_pct'])} | {t['tag_status']} | "
                 f"{'✓' if t['forward_eligible'] else '-'} |")
    L.append("")
    b = rep["backlog"]
    L.append("## Backlog (warum nicht voll getaggt)")
    L.append("")
    L.append(f"- offene (nicht-COMPLETE) Trades: **{b['n_open']}**")
    if b["by_reason"]:
        for reason, n in b["by_reason"].items():
            L.append(f"  - {n}× {reason}")
    if b["missing_field_counts"]:
        mf = ", ".join(f"{k}:{v}" for k, v in b["missing_field_counts"].items())
        L.append(f"- fehlende Felder (Zaehlung): {mf}")
    L.append("")
    L.append(f"> {b['resolution']}")
    L.append("")
    L.append(f"> EHRLICHKEIT: {rep['honesty']}")
    L.append("")
    L.append("_Generated by tools/cohort_tag_ledger.py (read-only ledger producer)._")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def write_outputs(rep: dict, run_date: str,
                  ledger_path: Path = _LEDGER_PATH,
                  md_dir: Optional[Path] = None) -> tuple[Path, Path]:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    md_dir = Path(md_dir) if md_dir is not None else (_ROOT / "Briefings")
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"Cohort-Tag-Ledger-{run_date}.md"
    # Das Ledger ist {"trades":[...], ...meta}, assess_live_forward() liest
    # data.get("trades", []) und zaehlt date>=cutoff.
    ledger_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    md_path.write_text(render_md(rep), encoding="utf-8")
    return ledger_path, md_path


# --------------------------------------------------------------------------- #
# Selftest (hermetisch: In-Memory-Journal + Temp-Scan/Regime)
# --------------------------------------------------------------------------- #
def _mem_journal(rows: list[tuple]) -> sqlite3.Connection:
    """Baut eine In-Memory-trades-Tabelle mit dem Spalten-Subset, das wir ziehen."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE trades(ticker TEXT, source TEXT, entry_date TEXT, "
        "exit_date TEXT, atr_pct REAL, side TEXT, return_pct REAL, netto_pnl REAL, "
        "is_win INTEGER, regime TEXT, regime_at_entry TEXT, setup_fk TEXT, "
        "system_version TEXT, holding_days INTEGER)")
    con.executemany(
        "INSERT INTO trades(ticker,source,entry_date,exit_date,atr_pct,side,"
        "return_pct,netto_pnl,is_win,regime,regime_at_entry,setup_fk,system_version,"
        "holding_days) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return con


def selftest() -> int:
    import tempfile

    fails: list[str] = []

    def chk(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    # ---- 1) Journal-Reader: Source-Filter + Schema-Subset ----
    rows = [
        # ticker, source, entry, exit, atr, side, ret, netto, win, regime, reg@entry, setup, sysv, hold
        ("AAA", "live", "2025-03-15", "2025-03-18", None, "long", 5.0, 50.0, 1,
         "Trending-Divergent", None, "mr_v61", "V6.1", 3),
        ("BBB", "external", "2025-04-01", "2025-04-02", None, "long", None, 30.0, 1,
         None, "Trending", "discretionary", "discretionary-external", 1),
        ("CCC", "live", "2024-06-01", "2024-06-04", None, "long", -1.0, -10.0, 0,
         "?", None, "mr_v61", "V6.1", 3),
        ("ZZZ", "backtest", "2025-03-15", "2025-03-18", 2.0, "long", 1.0, 10.0, 1,
         "Neutral", None, "mr_v61", "V6.1", 3),  # muss rausgefiltert werden
    ]
    trades, err = load_journal_trades("ignored", connector=lambda _p: _mem_journal(rows))
    chk(err is None, f"journal err should be None, got {err}")
    chk(len(trades) == 3, f"source-filter: expected 3 live/external/disc, got {len(trades)}")
    chk(all(t["source"] in _LIVE_SOURCES for t in trades), "backtest leaked through filter")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # Scan nur fuer 2025-03-15 (AAA voll, CCC-Datum hat KEINEN Scan)
        scan = {
            "scan_date": "2025-03-15",
            "ticker_map": [
                {"ticker": "AAA", "name": "Alpha", "sector": "Tech", "tier": "A",
                 "pre_score": 8.1, "triple_rsi": 6, "rsi2": 3.0, "rsi5": 10.0,
                 "rsi14": 28.0, "atr_pct": 4.2},
            ],
        }
        (tdp / "scan_2025-03-15.json").write_text(json.dumps(scan), encoding="utf-8")
        (tdp / "regime_2025-04-01.json").write_text(
            json.dumps({"date": "2025-04-01", "regime": "Bullish-Confirmed"}),
            encoding="utf-8")

        rep = build_ledger("ignored", scan_dir=tdp, regime_dir=tdp,
                           cutoff="2025-01-01", run_date="selftest",
                           journal_loader=lambda: (trades, None))

        by_t = {t["ticker"]: t for t in rep["trades"]}

        # ---- 2) AAA: Scan + Regime + Return -> COMPLETE + forward_eligible ----
        chk(by_t["AAA"]["tag_status"] == "COMPLETE",
            f"AAA expected COMPLETE, got {by_t['AAA']['tag_status']}")
        chk(by_t["AAA"]["rsi14"] == 28.0, "AAA rsi14 not merged from scan")
        chk(by_t["AAA"]["atr_pct"] == 4.2, "AAA atr_pct not merged from scan")
        chk(by_t["AAA"]["regime"] == "Trending-Divergent", "AAA regime wrong")
        chk(by_t["AAA"]["forward_eligible"] is True, "AAA should be forward_eligible")
        chk(by_t["AAA"]["v61_netto_pnl"] == 50.0, "AAA v61_netto_pnl mapping wrong")

        # ---- 3) BBB: nur Regime (archive), kein Scan, kein Return -> PARTIAL ----
        chk(by_t["BBB"]["tag_status"] == "PARTIAL",
            f"BBB expected PARTIAL, got {by_t['BBB']['tag_status']}")
        chk(by_t["BBB"]["regime"] == "Trending", "BBB regime archive/legacy fallback failed")
        # Bullish-Confirmed-archive nur fuer 2025-04-01, aber legacy regime_at_entry
        # 'Trending' gewinnt (Prioritaet). forward_eligible False: regime nicht in
        # candidate cells, kein atr/rsi14, kein return.
        chk(by_t["BBB"]["forward_eligible"] is False, "BBB should NOT be forward_eligible")
        chk("return_pct" in by_t["BBB"]["missing_fields"], "BBB missing return_pct flag")

        # ---- 4) CCC: '?'-Regime + kein Scan -> UNTAGGABLE + pre-cutoff ----
        chk(by_t["CCC"]["tag_status"] == "UNTAGGABLE",
            f"CCC expected UNTAGGABLE, got {by_t['CCC']['tag_status']}")
        chk(by_t["CCC"]["regime"] is None, "CCC '?' should normalize to None")
        chk("kein scan_2024-06-01.json" in by_t["CCC"]["tag_reason"],
            f"CCC reason should cite missing scan, got {by_t['CCC']['tag_reason']}")

        # ---- 5) Summary + post-cutoff zaehlung ----
        s = rep["summary"]
        chk(s["n_trades"] == 3, f"summary n_trades expected 3, got {s['n_trades']}")
        chk(s["n_post_cutoff"] == 2, f"post-cutoff expected 2 (AAA,BBB), got {s['n_post_cutoff']}")
        chk(s["n_post_cutoff_forward_eligible"] == 1,
            f"forward-eligible post expected 1 (AAA), got {s['n_post_cutoff_forward_eligible']}")
        chk(s["forward_ready"] is False, "forward_ready should be False (1<40)")

        # ---- 6) Ledger konsumierbar durch V7.29 assess_live_forward() ----
        try:
            import entry_filter_forward_test as eff
            sig = tdp / "signals"
            lp, mp = write_outputs(rep, "selftest", ledger_path=sig / "ledger.json",
                                   md_dir=sig / "Briefings")
            # assess_live_forward liest fixen Pfad -> wir spiegeln die Logik:
            data = json.loads(lp.read_text(encoding="utf-8"))
            consumed = data.get("trades", [])
            post = [t for t in consumed if str(t.get("date", "")) >= "2025-01-01"]
            chk(len(post) == 2, f"consumer-shape: expected 2 post-cutoff rows, got {len(post)}")
            chk(all("date" in t for t in consumed), "consumer-shape: every row needs date")
            # Sanity: forward-test Metrik-Schluessel vorhanden
            chk(all(k in by_t["AAA"] for k in
                    ("regime", "return_pct", "v61_netto_pnl", "atr_pct", "rsi14")),
                "row missing forward-test metric keys")
            _ = eff  # importierbar = Modul-Kompatibilitaet
        except Exception as e:  # noqa: BLE001
            fails.append(f"consumer-compat check crashed: {e}")

        # ---- 7) render_md crasht nicht ----
        try:
            md = render_md(rep)
            chk("Cohort-Tag-Ledger" in md and "Backlog" in md, "render_md missing sections")
        except Exception as e:  # noqa: BLE001
            fails.append(f"render_md crashed: {e}")

    # ---- 8) Fail-soft: fehlende DB ----
    t2, e2 = load_journal_trades(_ROOT / "does_not_exist_xyz.sqlite")
    chk(t2 == [] and e2 is not None, "missing-db should fail-soft to ([], reason)")

    # ---- 9) _clean_regime Prioritaet/Normalisierung ----
    chk(_clean_regime("?", "Trending") == "Trending", "_clean_regime skip '?'")
    chk(_clean_regime(None, "", "?") is None, "_clean_regime all-empty -> None")
    chk(_clean_regime("Trending-Divergent") == "Trending-Divergent", "_clean_regime passthrough")

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  -", f)
        return 3
    print("SELFTEST PASS (9 Gruppen): journal-filter, COMPLETE, PARTIAL, "
          "UNTAGGABLE, summary, consumer-compat, render, fail-soft, regime-clean")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="COHORT-TAG-LEDGER-1 (V7.30): Producer fuer "
                    "signals/entry_filter_forward_ledger.json (read-only).")
    ap.add_argument("--db", default=str(_DEFAULT_DB), help="trade_journal.sqlite")
    ap.add_argument("--scan-dir", default=str(_DEFAULT_SCAN_DIR),
                    help="Verzeichnis mit scan_<date>.json (Default tools/)")
    ap.add_argument("--regime-dir", default=str(_DEFAULT_REGIME_DIR),
                    help="Verzeichnis mit regime_<date>.json (Default tools/)")
    ap.add_argument("--cutoff", default=_OOS_CUTOFF, help=f"OOS-Cutoff (Default {_OOS_CUTOFF})")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="Ledger + Briefing schreiben")
    ap.add_argument("--selftest", action="store_true", help="Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rep = build_ledger(args.db, scan_dir=args.scan_dir, regime_dir=args.regime_dir,
                       cutoff=args.cutoff, run_date=args.date)

    if args.write:
        lp, mp = write_outputs(rep, args.date)
        print(f"geschrieben: {lp}")
        print(f"geschrieben: {mp}")
    else:
        print(json.dumps({"summary": rep["summary"], "backlog": rep["backlog"]},
                         ensure_ascii=False, indent=2))

    # Exit-Code 2, wenn das Journal nicht lesbar war (fail-soft, kein Crash).
    return 2 if rep["summary"].get("journal_error") else 0


if __name__ == "__main__":
    sys.exit(main())
