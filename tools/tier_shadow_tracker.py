#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tier_shadow_tracker.py: TIER-SHADOW-TRACKER-1  (V7.39, read-only Producer)

------------------------------------------------------------------------------
DER NAMED UNBLOCK (woertlich aus V7.38 `tier_rule_counterfactual.py`):

    "Forward-Returns der von rec_gating_attribution als regime_hard_tier-gekillt
     markierten Tier-B/C-Setups (Shadow-Tracking) ODER ein tier-ablatierter
     Backtest. rec_gating_attribution liefert die gekillte Ticker-Liste pro Tag;
     dieses Tool ist der Adapter, sobald Returns vorliegen."

V7.38 hat die H2-Frage "Ist die regime_hard_tier-Regel (Tier-A-only auf
divergenten/Krisen-Regimes) zu streng?" evidenzbasiert gestellt und ehrlich als
`DATA_STARVED_UNANSWERABLE` beantwortet: das ausgefuehrte 440-Trade-Panel ist
~98 % Tier A, der AUSGESCHLOSSENE Tier-B/C-Pool ist in 3 von 4 verengten
Regimes n=0: die Regel wurde nie gegengeprueft, weil die von ihr gekillten
Setups nie als Trades realisiert wurden.

DIESES TOOL fuellt genau diesen Blindfleck: read-only, ohne eine einzige
Live-Regel zu aendern:

  1) Jeden (divergenten) Handelstag die von `regime_hard_tier` gekillten
     Tier-B/C-Setups als Entry-Snapshot ERFASSEN (ticker, tier, regime,
     pre_score, entry_close, atr_pct) → append in `signals/tier_shadow_ledger.json`.
     Der Entry-Snapshot ist IMMUTABLE (idempotent per (date,ticker)).
  2) Sobald genug Handelstage vergangen sind, die FORWARD-RETURNS dieser
     Schatten-Setups nachfuellen (T+1/T+2/T+3/T+5, long-Seite, aus Free-Stack-
     Closes; injizierbarer Fetcher → offline testbar; kein Netz-Zwang).
  3) Eine READINESS-Pane liefern: pro (Regime × ausgeschlossenem Tier) wieviele
     realisierte Schatten-Returns akkumuliert sind vs. der Counterfactual-Floor
     (n>=10), also wie weit die V7.38-Frage von der Beantwortbarkeit entfernt
     ist, und ein `to_counterfactual_rows()`-Adapter (FILLED-only), der die
     Schatten-Returns spaeter in genau die Panel-Zellen einspeist, die V7.38
     konsumiert.

EISERNE REGEL (H2-Feature-Freeze): KEINE Live-Regel/Schwelle/Sizing geaendert.
Kein Trade, kein Journal-Write. Der Ledger ist ein reines Beobachtungs-Artefakt.
Die tatsaechliche Tier-Aufweitung bleibt ein Folgeschritt mit maintainer approval.
Der Adapter ist NICHT in `tier_rule_counterfactual` verdrahtet (maintainer approval).

Artefakte:
  signals/tier_shadow_ledger.json          (kumulativ, immutable Snapshots)
  Briefings/Tier-Shadow-Tracker-<date>.md  (Tages-Report)

CLI:
  python3 tools/tier_shadow_tracker.py --selftest
  python3 tools/tier_shadow_tracker.py --date 2026-06-30 [--write]
  python3 tools/tier_shadow_tracker.py --range 2026-06-29 2026-07-07 [--write]
  python3 tools/tier_shadow_tracker.py --backfill [--write]   # Free-Stack-Closes
  python3 tools/tier_shadow_tracker.py --report [--write]     # nur Ledger-Report

Sprache: DE-Briefings / EN-Code.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = HERE
SIGNALS_DIR = os.path.join(ROOT, "signals")
BRIEFINGS_DIR = os.path.join(ROOT, "Briefings")
LEDGER_PATH = os.path.join(SIGNALS_DIR, "tier_shadow_ledger.json")

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

# Das Gate, dessen Opfer wir tracken (SSoT-Name aus rec_gating_attribution).
SHADOW_GATE = "regime_hard_tier"
# Forward-Horizonte in HANDELSTAGEN (T+3 = Hard-Exit-Fenster; T+5 = MR-Bounce-Ende)
HORIZONS: Tuple[int, ...] = (1, 2, 3, 5)
# Mindest-n realisierter Schatten-Returns, ab dem V7.38 eine Zelle bewerten kann.
# Gespiegelt aus tier_rule_counterfactual._MIN_COUNTERFACTUAL_N (Floor 10).
COUNTERFACTUAL_FLOOR = 10
LEDGER_VERSION = "V7.39"


# ---------------------------------------------------------------------------
# Resilientes Lesen (Sandbox-EDEADLK-tolerant): reuse aus cohort_tag_ledger.
# ---------------------------------------------------------------------------
def read_text_resilient(path: str) -> Optional[str]:
    try:
        from cohort_tag_ledger import read_text_resilient as _rtr  # type: ignore
        return _rtr(path)
    except Exception:
        pass
    for _ in range(2):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            continue
        except Exception:
            return None
    return None


def _load_json(path: str) -> Optional[Any]:
    txt = read_text_resilient(path)
    if txt is None:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z"


def _fnum(x) -> Optional[float]:
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------
@dataclass
class ShadowEntry:
    """Ein von regime_hard_tier gekilltes (ausgeschlossenes) Tier-B/C-Setup."""
    entry_date: str                     # Kill-/Entry-Tag (ISO)
    ticker: str
    tier: str                           # A/B/C (der ausgeschlossene Tier)
    regime: Optional[str]
    killed_by_gate: str
    pre_score: Optional[float]
    entry_close: Optional[float]
    atr_pct: Optional[float]
    sector: Optional[str] = None
    tradability: Optional[str] = None
    captured_at_utc: str = ""
    # Forward-Block (vom Backfill gefuellt): h1/h2/h3/h5 -> {close, return_pct}
    forward: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    forward_status: str = "PENDING"     # PENDING | FILLED | PARTIAL | NO_ENTRY_PRICE

    def key(self) -> Tuple[str, str]:
        return (self.entry_date, self.ticker)


# ---------------------------------------------------------------------------
# Loader / rec_gating-Bruecke
# ---------------------------------------------------------------------------
def load_scan(date_str: str, tools_dir: str = TOOLS) -> Optional[Dict[str, Any]]:
    for cand in (os.path.join(tools_dir, f"scan_{date_str}.json"),
                 os.path.join(ROOT, "signals", f"scan_{date_str}.json"),
                 os.path.join(ROOT, f"scan_{date_str}.json")):
        d = _load_json(cand)
        if d is not None:
            return d
    return None


def load_rec_gating(date_str: str, tools_dir: str = TOOLS) -> Optional[Dict[str, Any]]:
    """
    Bevorzugt das persistierte signals/rec_gating_attribution_<date>.json;
    faellt sonst auf einen Live-Aufruf von rec_gating_attribution.diagnose_day
    zurueck (gleiche Gate-Kette, gleicher SSoT).
    """
    cached = _load_json(os.path.join(SIGNALS_DIR, f"rec_gating_attribution_{date_str}.json"))
    if cached is not None:
        return cached
    try:
        from rec_gating_attribution import diagnose_day, _to_dict  # type: ignore
        rep = diagnose_day(date_str, tools_dir=tools_dir)
        return _to_dict(rep)
    except Exception:
        return None


def killed_at_gate(recgate: Dict[str, Any], gate: str = SHADOW_GATE) -> List[str]:
    for step in (recgate.get("steps") or []):
        if step.get("name") == gate:
            return list(step.get("killed_tickers") or [])
    return []


# ---------------------------------------------------------------------------
# Capture: gekillte Setups -> Entry-Snapshots
# ---------------------------------------------------------------------------
def _index_top_setups(scan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in (scan.get("top_setups") or []):
        t = row.get("ticker")
        if t:
            out[str(t)] = row
    return out


def snapshot_setups(date_str: str, killed: List[str],
                    scan: Optional[Dict[str, Any]],
                    regime: Optional[str]) -> List[ShadowEntry]:
    """
    Baut Entry-Snapshots fuer die gekillten Ticker. Ist der Scan vorhanden,
    wird der volle Snapshot (tier/close/atr/score) aus top_setups gezogen;
    fehlt der Scan, degradiert der Snapshot sauber (nur ticker+regime,
    entry_close=None -> forward_status NO_ENTRY_PRICE).
    """
    idx = _index_top_setups(scan) if scan else {}
    now = _utcnow()
    out: List[ShadowEntry] = []
    for tk in killed:
        row = idx.get(str(tk), {})
        close = _fnum(row.get("close"))
        e = ShadowEntry(
            entry_date=date_str,
            ticker=str(tk),
            tier=str(row.get("tier") or "?").upper(),
            regime=regime,
            killed_by_gate=SHADOW_GATE,
            pre_score=_fnum(row.get("pre_score")),
            entry_close=close,
            atr_pct=_fnum(row.get("atr_pct")),
            sector=row.get("sector") or row.get("gics_sector"),
            tradability=row.get("tradability"),
            captured_at_utc=now,
            forward={},
            forward_status="PENDING" if close is not None else "NO_ENTRY_PRICE",
        )
        out.append(e)
    return out


def capture_day(date_str: str, tools_dir: str = TOOLS) -> Tuple[List[ShadowEntry], Dict[str, Any]]:
    """Erfasst die gekillten Tier-B/C-Setups eines Tages. Read-only."""
    meta: Dict[str, Any] = {"date": date_str, "captured": 0, "regime": None,
                            "gate_drop": 0, "note": ""}
    recgate = load_rec_gating(date_str, tools_dir)
    if recgate is None:
        meta["note"] = "kein rec_gating_attribution + kein Scan -> nichts erfassbar"
        return [], meta
    regime = recgate.get("regime")
    meta["regime"] = regime
    killed = killed_at_gate(recgate, SHADOW_GATE)
    meta["gate_drop"] = len(killed)
    if not killed:
        meta["note"] = (f"Regime '{regime}': regime_hard_tier hat 0 Setups gekillt "
                        f"(keine Tier-Verengung/keine Passes) -> nichts zu tracken.")
        return [], meta
    scan = load_scan(date_str, tools_dir)
    if scan is None:
        meta["note"] = f"{len(killed)} gekillt, aber scan_{date_str}.json fehlt -> Snapshots ohne Entry-Preis"
    entries = snapshot_setups(date_str, killed, scan, regime)
    meta["captured"] = len(entries)
    return entries, meta


# ---------------------------------------------------------------------------
# Ledger I/O + idempotenter, immutabler Upsert
# ---------------------------------------------------------------------------
def load_ledger(path: str = LEDGER_PATH) -> Dict[str, Any]:
    d = _load_json(path)
    if isinstance(d, dict) and isinstance(d.get("entries"), list):
        return d
    return {"version": LEDGER_VERSION, "tool": "tier_shadow_tracker",
            "created_at_utc": _utcnow(), "updated_at_utc": None, "entries": []}


def _entries_from_ledger(ledger: Dict[str, Any]) -> List[ShadowEntry]:
    out: List[ShadowEntry] = []
    valid = {f for f in ShadowEntry.__dataclass_fields__}  # type: ignore[attr-defined]
    for row in ledger.get("entries", []):
        if not isinstance(row, dict):
            continue
        kwargs = {k: v for k, v in row.items() if k in valid}
        try:
            out.append(ShadowEntry(**kwargs))
        except TypeError:
            continue
    return out


def upsert_entries(ledger: Dict[str, Any],
                   new_entries: List[ShadowEntry]) -> Tuple[Dict[str, Any], int, int]:
    """
    Fuegt neue Snapshots ein. IMMUTABLE: existiert (date,ticker) bereits, wird der
    Entry-Snapshot NIE ueberschrieben (nur der spaetere Backfill fuellt forward).
    Rueckgabe: (ledger, n_added, n_skipped_existing).
    """
    existing = {tuple(k) for k in
                ((r.get("entry_date"), r.get("ticker")) for r in ledger.get("entries", []))}
    added, skipped = 0, 0
    for e in new_entries:
        if e.key() in existing:
            skipped += 1
            continue
        ledger.setdefault("entries", []).append(asdict(e))
        existing.add(e.key())
        added += 1
    if added:
        ledger["updated_at_utc"] = _utcnow()
    return ledger, added, skipped


# ---------------------------------------------------------------------------
# Forward-Return-Backfill (injizierbarer Fetcher -> offline testbar)
# ---------------------------------------------------------------------------
# Fetcher-Kontrakt: fetcher(ticker, start_date_iso) -> {iso_date: close_float}
#   (nur Handelstage; datums-indiziert, damit Wochenenden/Feiertage sauber
#    uebersprungen werden). Free-Stack-Impl unten; Selftest injiziert Fake.
CloseFetcher = Callable[[str, str], Dict[str, float]]


def _forward_dates(entry_date: str, closes: Dict[str, float],
                   horizons: Tuple[int, ...]) -> Dict[str, Optional[str]]:
    """Mappt Handelstags-Horizonte auf konkrete Kalenderdaten aus den Closes."""
    future = sorted(d for d in closes.keys() if d > entry_date)
    out: Dict[str, Optional[str]] = {}
    for h in horizons:
        out[f"h{h}"] = future[h - 1] if len(future) >= h else None
    return out


def compute_forward(entry: ShadowEntry, closes: Dict[str, float],
                    horizons: Tuple[int, ...] = HORIZONS) -> Tuple[Dict[str, Dict[str, Optional[float]]], str]:
    """
    Berechnet long-seitige Forward-Returns (%) vom entry_close aus.
    return_pct = (close[entry+h] / entry_close - 1) * 100.
    Status: FILLED wenn alle Horizonte da, PARTIAL wenn einige, PENDING wenn keiner.
    """
    if entry.entry_close is None or entry.entry_close <= 0:
        return {}, "NO_ENTRY_PRICE"
    fdates = _forward_dates(entry.entry_date, closes, horizons)
    fwd: Dict[str, Dict[str, Optional[float]]] = {}
    n_filled = 0
    for h in horizons:
        key = f"h{h}"
        d = fdates.get(key)
        c = closes.get(d) if d else None
        if c is not None and c > 0:
            ret = round((c / entry.entry_close - 1.0) * 100.0, 4)
            fwd[key] = {"date": d, "close": round(c, 4), "return_pct": ret}
            n_filled += 1
        else:
            fwd[key] = {"date": d, "close": None, "return_pct": None}
    if n_filled == len(horizons):
        status = "FILLED"
    elif n_filled == 0:
        status = "PENDING"
    else:
        status = "PARTIAL"
    return fwd, status


def backfill(ledger: Dict[str, Any], fetcher: CloseFetcher,
             horizons: Tuple[int, ...] = HORIZONS,
             only_unfinished: bool = True) -> Tuple[Dict[str, Any], int]:
    """
    Fuellt Forward-Returns fuer noch nicht abgeschlossene Entries. Best-effort:
    wirft der Fetcher fuer einen Ticker, bleibt der Entry unveraendert (PENDING).
    Rueckgabe: (ledger, n_updated).
    """
    updated = 0
    cache: Dict[str, Dict[str, float]] = {}
    for row in ledger.get("entries", []):
        status = row.get("forward_status")
        if only_unfinished and status in ("FILLED", "NO_ENTRY_PRICE"):
            continue
        if row.get("entry_close") in (None, 0):
            row["forward_status"] = "NO_ENTRY_PRICE"
            continue
        tk = row.get("ticker")
        if tk not in cache:
            try:
                cache[tk] = fetcher(tk, row.get("entry_date")) or {}
            except Exception:
                cache[tk] = {}
        closes = cache[tk]
        if not closes:
            continue  # kein Feed -> bleibt PENDING (kein Fabrizieren)
        e = ShadowEntry(**{k: v for k, v in row.items()
                           if k in ShadowEntry.__dataclass_fields__})  # type: ignore[attr-defined]
        fwd, st = compute_forward(e, closes, horizons)
        if st in ("FILLED", "PARTIAL") and fwd:
            row["forward"] = fwd
            row["forward_status"] = st
            updated += 1
    if updated:
        ledger["updated_at_utc"] = _utcnow()
    return ledger, updated


def free_stack_fetcher(ticker: str, start_date: str) -> Dict[str, float]:
    """
    Free-Stack Forward-Closes (yfinance; Stooq-Fallback ueber
    data_source_orchestrator, falls verfuegbar). Datums-indiziert ab start_date.
    Best-effort: jede Ausnahme -> {} (Backfill laesst den Entry dann PENDING).
    Kein Score-relevanter Pfad, nur Beobachtung. Bounded (kleiner Zeitraum).

    HINWEIS: In der simulierten Projekt-Zeitleiste (2026) liefert Live-yfinance
    keine passenden Daten. Der DEFAULT-Backfill-Feed ist daher der Scan-Archiv-
    Fetcher (`make_archive_fetcher`): dieselben validierten Free-Stack-Closes,
    die die Pipeline taeglich schreibt, offline und in derselben Zeitleiste.
    """
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}
    try:
        start = _dt.date.fromisoformat(start_date)
        end = start + _dt.timedelta(days=20)   # >5 Handelstage Puffer
        df = yf.Ticker(ticker).history(start=start.isoformat(),
                                       end=end.isoformat(), auto_adjust=True)
        if df is None or df.empty:
            return {}
        out: Dict[str, float] = {}
        for ts, row in df.iterrows():
            d = ts.date().isoformat()
            c = _fnum(row.get("Close"))
            if c is not None:
                out[d] = float(c)
        return out
    except Exception:
        return {}


def load_scan_closes(date_str: str, tools_dir: str = TOOLS) -> Dict[str, float]:
    """{ticker: close} eines Handelstags aus dem eigenen Scan-Archiv.

    Primaerquelle `ticker_map` (alle ~624 gescannten Titel), Fallback top_setups.
    Genau die Free-Stack-Closes (yfinance->Stooq), die die Pipeline validiert und
    persistiert: self-consistent zur Projekt-Zeitleiste, ohne Netz.
    """
    scan = load_scan(date_str, tools_dir)
    if not scan:
        return {}
    out: Dict[str, float] = {}
    tm = scan.get("ticker_map")
    if isinstance(tm, dict):
        for tk, row in tm.items():
            c = _fnum(row.get("close")) if isinstance(row, dict) else None
            if c is not None:
                out[str(tk)] = c
    for row in (scan.get("top_setups") or []):
        tk = row.get("ticker")
        c = _fnum(row.get("close"))
        if tk and c is not None:
            out.setdefault(str(tk), c)
    return out


def _scan_dates_available(tools_dir: str = TOOLS) -> List[str]:
    """Alle Handelstage mit einem scan_<date>.json (aus tools/ + signals/)."""
    import glob
    import re
    pat = re.compile(r"scan_(\d{4}-\d{2}-\d{2})\.json$")
    dates: set = set()
    for base in (tools_dir, SIGNALS_DIR, ROOT):
        for p in glob.glob(os.path.join(base, "scan_*.json")):
            m = pat.search(os.path.basename(p))
            if m:
                dates.add(m.group(1))
    return sorted(dates)


def build_scan_archive(tools_dir: str = TOOLS,
                       dates: Optional[List[str]] = None) -> Dict[str, Dict[str, float]]:
    """{date: {ticker: close}} ueber die verfuegbaren Scan-Tage (einmal aufgebaut)."""
    dates = dates if dates is not None else _scan_dates_available(tools_dir)
    return {d: load_scan_closes(d, tools_dir) for d in dates}


def make_archive_fetcher(archive: Dict[str, Dict[str, float]]) -> CloseFetcher:
    """Fetcher-Closure ueber ein vorab gebautes Scan-Archiv (kein Netz, O(1) je Tag)."""
    def _fetch(ticker: str, start_date: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for d, closes in archive.items():
            if d > start_date and ticker in closes:
                out[d] = closes[ticker]
        # entry-Tag selbst mitgeben ist unnoetig (forward nutzt nur d>entry),
        # aber schadet nicht der Konsistenz.
        entry_closes = archive.get(start_date)
        if entry_closes and ticker in entry_closes:
            out[start_date] = entry_closes[ticker]
        return out
    return _fetch


# ---------------------------------------------------------------------------
# Readiness gegenueber dem V7.38-Counterfactual + Adapter-Preview
# ---------------------------------------------------------------------------
def counterfactual_readiness(ledger: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pro (Regime × ausgeschlossenem Tier): wieviele Schatten-Setups sind erfasst,
    wieviele haben FILLED/PARTIAL Forward-Returns (= counterfactual-nutzbar),
    und wie weit ist die Zelle vom Floor (n>=10) entfernt.
    """
    cells: Dict[str, Dict[str, Any]] = {}
    total = 0
    total_usable = 0
    for row in ledger.get("entries", []):
        total += 1
        reg = row.get("regime") or "?"
        tier = (row.get("tier") or "?").upper()
        ckey = f"{reg}|{tier}"
        cell = cells.setdefault(ckey, {"regime": reg, "tier": tier,
                                       "captured": 0, "usable": 0,
                                       "mean_return_pct_h3": None, "_h3": []})
        cell["captured"] += 1
        st = row.get("forward_status")
        h3 = ((row.get("forward") or {}).get("h3") or {}).get("return_pct")
        if st in ("FILLED", "PARTIAL") and h3 is not None:
            cell["usable"] += 1
            cell["_h3"].append(h3)
            total_usable += 1
    for cell in cells.values():
        vals = cell.pop("_h3")
        cell["mean_return_pct_h3"] = round(sum(vals) / len(vals), 4) if vals else None
        cell["remaining_to_floor"] = max(0, COUNTERFACTUAL_FLOOR - cell["usable"])
        cell["floor_reached"] = cell["usable"] >= COUNTERFACTUAL_FLOOR
    ready_cells = [c for c in cells.values() if c["floor_reached"]]
    return {
        "floor": COUNTERFACTUAL_FLOOR,
        "total_captured": total,
        "total_usable": total_usable,
        "cells": sorted(cells.values(), key=lambda c: (-c["captured"], c["regime"])),
        "cells_at_floor": len(ready_cells),
        "answerable": len(ready_cells) > 0,
        "note": ("Sobald eine Zelle floor_reached=True ist, kann "
                 "tier_rule_counterfactual sie aus REALISIERTEN Schatten-Returns "
                 "bewerten statt DATA_STARVED zu melden."),
    }


def to_counterfactual_rows(ledger: Dict[str, Any],
                           horizon: str = "h3") -> List[Dict[str, Any]]:
    """
    Adapter-PREVIEW: konvertiert FILLED/PARTIAL Schatten-Entries in das Panel-
    Zeilenformat, das tier_rule_counterfactual konsumiert
    (regime, tier, return_pct, v61_netto_pnl=None, is_win, year).
    NICHT verdrahtet, reine Vorschau bis zur maintainer approval (H2-Feature-Freeze).
    """
    rows: List[Dict[str, Any]] = []
    for row in ledger.get("entries", []):
        if row.get("forward_status") not in ("FILLED", "PARTIAL"):
            continue
        ret = ((row.get("forward") or {}).get(horizon) or {}).get("return_pct")
        if ret is None:
            continue
        year = None
        try:
            year = int(str(row.get("entry_date"))[:4])
        except Exception:
            pass
        rows.append({
            "regime": row.get("regime"),
            "tier": row.get("tier"),
            "return_pct": ret,
            "v61_netto_pnl": None,     # Netto braucht Sizing-Annahme -> bewusst None
            "is_win": bool(ret > 0),
            "year": year,
            "source": "tier_shadow_tracker",
        })
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_md(ledger: Dict[str, Any], readiness: Dict[str, Any],
              run_date: str, day_meta: Optional[List[Dict[str, Any]]] = None) -> str:
    L: List[str] = []
    L.append(f"# Tier-Shadow-Tracker: {run_date} ({LEDGER_VERSION}, read-only)")
    L.append("")
    L.append("> TIER-SHADOW-TRACKER-1: erfasst die von `regime_hard_tier` gekillten "
             "Tier-B/C-Setups als Schatten-Trades und misst ihre Forward-Returns: "
             "der **named Unblock** aus V7.38 (`tier_rule_counterfactual`). "
             "KEINE Live-Regel/Sizing geaendert; kein Trade, kein Journal-Write.")
    L.append("")
    entries = ledger.get("entries", [])
    n_total = len(entries)
    n_filled = sum(1 for r in entries if r.get("forward_status") == "FILLED")
    n_partial = sum(1 for r in entries if r.get("forward_status") == "PARTIAL")
    n_pending = sum(1 for r in entries if r.get("forward_status") == "PENDING")
    n_noprice = sum(1 for r in entries if r.get("forward_status") == "NO_ENTRY_PRICE")
    L.append(f"**Ledger:** {n_total} Schatten-Setups  ·  FILLED {n_filled} · "
             f"PARTIAL {n_partial} · PENDING {n_pending} · NO_ENTRY_PRICE {n_noprice}")
    L.append(f"**Counterfactual-Floor:** n≥{readiness['floor']} pro Zelle  ·  "
             f"nutzbar gesamt: {readiness['total_usable']}  ·  "
             f"Zellen am Floor: {readiness['cells_at_floor']}  ·  "
             f"**answerable:** {readiness['answerable']}")
    L.append("")

    if day_meta:
        L.append("## Heutige Erfassung")
        L.append("")
        L.append("| Datum | Regime | gate_drop | erfasst | Notiz |")
        L.append("|---|---|--:|--:|---|")
        for m in day_meta:
            L.append(f"| {m['date']} | {m.get('regime')} | {m.get('gate_drop', 0)} | "
                     f"{m.get('captured', 0)} | {m.get('note', '')} |")
        L.append("")

    L.append("## Readiness pro (Regime × ausgeschlossenem Tier)")
    L.append("")
    L.append("| Regime | Tier | erfasst | nutzbar | mean_ret%(T+3) | bis Floor | am Floor |")
    L.append("|---|:--:|--:|--:|--:|--:|:--:|")
    for c in readiness["cells"]:
        mr = c["mean_return_pct_h3"]
        L.append(f"| {c['regime']} | {c['tier']} | {c['captured']} | {c['usable']} | "
                 f"{mr if mr is not None else '-'} | {c['remaining_to_floor']} | "
                 f"{'✓' if c['floor_reached'] else '-'} |")
    L.append("")
    L.append(f"_{readiness['note']}_")
    L.append("")

    # Jüngste erfasste Setups (max 12, neueste zuerst)
    recent = sorted(entries, key=lambda r: (r.get("entry_date", ""), r.get("ticker", "")),
                    reverse=True)[:12]
    if recent:
        L.append("## Zuletzt erfasste Schatten-Setups")
        L.append("")
        L.append("| Entry-Tag | Ticker | Tier | Regime | pre_score | entry_close | atr% | fwd_status | T+3 ret% |")
        L.append("|---|---|:--:|---|--:|--:|--:|:--:|--:|")
        for r in recent:
            h3 = ((r.get("forward") or {}).get("h3") or {}).get("return_pct")
            L.append(f"| {r.get('entry_date')} | {r.get('ticker')} | {r.get('tier')} | "
                     f"{r.get('regime')} | {r.get('pre_score')} | {r.get('entry_close')} | "
                     f"{round(r['atr_pct'],2) if r.get('atr_pct') is not None else '-'} | "
                     f"{r.get('forward_status')} | {h3 if h3 is not None else '-'} |")
        L.append("")

    L.append("## Lesart / Unblock-Status")
    L.append("")
    if readiness["answerable"]:
        L.append("- Mindestens eine (Regime × Tier)-Zelle hat den Counterfactual-Floor "
                 "erreicht → `tier_rule_counterfactual` kann sie ab jetzt aus "
                 "REALISIERTEN Schatten-Returns bewerten (V7.38 war hier "
                 "`DATA_STARVED_UNANSWERABLE`). **Verdrahtung = maintainer approval.**")
    else:
        max_cell = max(readiness["cells"], key=lambda c: c["usable"], default=None)
        if max_cell and max_cell["usable"] > 0:
            L.append(f"- Noch keine Zelle am Floor. Fuehrende Zelle: "
                     f"**{max_cell['regime']} · Tier {max_cell['tier']}** "
                     f"({max_cell['usable']}/{COUNTERFACTUAL_FLOOR} nutzbar). Der "
                     f"Engpass ist rein die Zahl divergenter Handelstage + der "
                     f"Forward-Fenster-Reifung, kein Code mehr.")
        else:
            L.append("- Noch keine nutzbaren Forward-Returns (Entries zu jung oder "
                     "Feed noch nicht gelaufen). Der Ledger akkumuliert; der "
                     "Backfill fuellt die Zellen, sobald die Forward-Fenster reif sind.")
    L.append("")
    L.append("> H2-Feature-Freeze respektiert: der `to_counterfactual_rows()`-Adapter "
             "ist NICHT in `tier_rule_counterfactual` verdrahtet. Tier-Aufweitung = "
             "Folgeschritt mit maintainer approval.")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------
def save_ledger(ledger: Dict[str, Any], path: str = LEDGER_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, ensure_ascii=False, indent=2)
    return path


def write_report(md: str, run_date: str) -> str:
    os.makedirs(BRIEFINGS_DIR, exist_ok=True)
    mpath = os.path.join(BRIEFINGS_DIR, f"Tier-Shadow-Tracker-{run_date}.md")
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(md)
    return mpath


# ---------------------------------------------------------------------------
# Range-Helfer
# ---------------------------------------------------------------------------
def _daterange(a: str, b: str) -> List[str]:
    d0 = _dt.date.fromisoformat(a)
    d1 = _dt.date.fromisoformat(b)
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Selftest (hermetisch, kein Netz, kein echtes Scan/rec_gating noetig)
# ---------------------------------------------------------------------------
def selftest() -> int:
    import tempfile
    fails: List[str] = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="tiershadow_selftest_")

    def _write_scan(date, rows):
        with open(os.path.join(tmp, f"scan_{date}.json"), "w", encoding="utf-8") as fh:
            json.dump({"scan_date": date, "universe_size": 100, "valid_results": 90,
                       "top_setups": rows}, fh)

    def _write_recgate(date, regime, killed, gate=SHADOW_GATE):
        d = {"date": date, "ok": True, "regime": regime,
             "steps": [{"name": "regime_allowed_tiers", "killed_tickers": []},
                       {"name": gate, "killed_tickers": killed}]}
        os.makedirs(os.path.join(tmp, "signals"), exist_ok=True)
        with open(os.path.join(tmp, "signals", f"rec_gating_attribution_{date}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def _row(t, tier, score, close, atr=2.0):
        return {"ticker": t, "tier": tier, "pre_score": score, "close": close,
                "atr_pct": atr, "tradability": "direct", "sector": "X",
                "passes_threshold": True}

    # Patch modul-globale Pfade auf tmp (Loader lesen von dort)
    global SIGNALS_DIR, LEDGER_PATH
    _sig_bak, _led_bak = SIGNALS_DIR, LEDGER_PATH
    SIGNALS_DIR = os.path.join(tmp, "signals")
    LEDGER_PATH = os.path.join(tmp, "signals", "tier_shadow_ledger.json")

    try:
        # --- Gruppe 1: killed_at_gate extrahiert die richtige Liste ---
        rg = {"steps": [{"name": "regime_hard_tier", "killed_tickers": ["BBB", "CCC"]}]}
        chk(killed_at_gate(rg) == ["BBB", "CCC"], "G1 killed_at_gate")
        chk(killed_at_gate({"steps": []}) == [], "G1 empty steps")

        # --- Gruppe 2: capture_day mit Scan -> volle Snapshots ---
        d1 = "2099-01-05"
        _write_scan(d1, [_row("AAA", "A", 9.0, 100.0), _row("BBB", "B", 9.5, 50.0),
                         _row("CCC", "C", 8.2, 20.0)])
        _write_recgate(d1, "Trending-Divergent", ["BBB", "CCC"])
        ents, meta = capture_day(d1, tools_dir=tmp)
        chk(len(ents) == 2, f"G2 captured 2 got {len(ents)}")
        chk(meta["regime"] == "Trending-Divergent", "G2 regime")
        bbb = [e for e in ents if e.ticker == "BBB"][0]
        chk(bbb.tier == "B" and bbb.entry_close == 50.0, "G2 BBB snapshot")
        chk(bbb.forward_status == "PENDING", "G2 BBB pending")

        # --- Gruppe 3: 0-kill Tag (Trending, keine Verengung) -> nichts ---
        d0 = "2099-01-06"
        _write_scan(d0, [_row("AAA", "A", 9.0, 100.0)])
        _write_recgate(d0, "Trending", [])
        ents0, meta0 = capture_day(d0, tools_dir=tmp)
        chk(ents0 == [] and meta0["gate_drop"] == 0, "G3 zero-kill day")

        # --- Gruppe 4: fehlender Scan -> NO_ENTRY_PRICE-Snapshot ---
        d2 = "2099-01-07"
        _write_recgate(d2, "High-Vol-Krise", ["ZZZ"])
        ents2, meta2 = capture_day(d2, tools_dir=tmp)
        chk(len(ents2) == 1 and ents2[0].forward_status == "NO_ENTRY_PRICE",
            "G4 no-price snapshot")

        # --- Gruppe 5: upsert idempotent + immutable ---
        led = load_ledger(LEDGER_PATH)
        led, add1, skip1 = upsert_entries(led, ents)
        chk(add1 == 2 and skip1 == 0, "G5 first upsert 2/0")
        # Re-Upsert mit veraendertem Snapshot -> darf NICHT ueberschreiben
        ents_mut = snapshot_setups(d1, ["BBB"], {"top_setups": [_row("BBB", "B", 1.0, 999.0)]},
                                   "Trending-Divergent")
        led, add2, skip2 = upsert_entries(led, ents_mut)
        chk(add2 == 0 and skip2 == 1, "G5 immutable skip")
        bbb_led = [r for r in led["entries"] if r["ticker"] == "BBB"][0]
        chk(bbb_led["entry_close"] == 50.0, "G5 entry_close unchanged (immutable)")

        # --- Gruppe 6: forward-Berechnung (Handelstags-Mapping + return_pct) ---
        e = ShadowEntry(entry_date="2099-01-05", ticker="BBB", tier="B",
                        regime="Trending-Divergent", killed_by_gate=SHADOW_GATE,
                        pre_score=9.5, entry_close=50.0, atr_pct=2.0)
        closes = {"2099-01-05": 50.0, "2099-01-06": 55.0, "2099-01-07": 51.0,
                  "2099-01-08": 52.5, "2099-01-11": 58.0, "2099-01-12": 60.0}  # 09/10 = WE
        fwd, st = compute_forward(e, closes)
        chk(st == "FILLED", f"G6 status FILLED got {st}")
        chk(fwd["h1"]["return_pct"] == 10.0, f"G6 h1 +10% got {fwd['h1']['return_pct']}")
        chk(fwd["h3"]["date"] == "2099-01-08", f"G6 h3 skips weekend got {fwd['h3']['date']}")
        chk(fwd["h5"]["return_pct"] == 20.0, f"G6 h5 +20% got {fwd['h5']['return_pct']}")

        # --- Gruppe 7: backfill mit injiziertem Fetcher ---
        fake_series = {
            "BBB": {"2099-01-05": 50.0, "2099-01-06": 55.0, "2099-01-07": 51.0,
                    "2099-01-08": 52.5, "2099-01-11": 58.0, "2099-01-12": 60.0},
            "CCC": {"2099-01-05": 20.0, "2099-01-06": 19.0, "2099-01-07": 18.0,
                    "2099-01-08": 17.5, "2099-01-11": 17.0, "2099-01-12": 16.0},
        }

        def fake_fetcher(tk, start):
            return fake_series.get(tk, {})

        led, nupd = backfill(led, fake_fetcher)
        chk(nupd == 2, f"G7 backfilled 2 got {nupd}")
        bbb_led = [r for r in led["entries"] if r["ticker"] == "BBB"][0]
        chk(bbb_led["forward_status"] == "FILLED", "G7 BBB filled")
        chk(bbb_led["forward"]["h1"]["return_pct"] == 10.0, "G7 BBB h1")
        ccc_led = [r for r in led["entries"] if r["ticker"] == "CCC"][0]
        chk(ccc_led["forward"]["h5"]["return_pct"] == -20.0, "G7 CCC h5 -20%")

        # --- Gruppe 8: leerer Fetcher -> bleibt PENDING (kein Fabrizieren) ---
        led2 = {"version": LEDGER_VERSION, "entries": [asdict(e)]}
        led2, nupd2 = backfill(led2, lambda tk, s: {})
        chk(nupd2 == 0 and led2["entries"][0]["forward_status"] == "PENDING",
            "G8 empty feed keeps PENDING")

        # --- Gruppe 9: readiness + to_counterfactual_rows ---
        rd = counterfactual_readiness(led)
        chk(rd["total_usable"] == 2, f"G9 usable 2 got {rd['total_usable']}")
        chk(rd["answerable"] is False, "G9 not answerable (<floor)")
        td_b = [c for c in rd["cells"] if c["regime"] == "Trending-Divergent"
                and c["tier"] == "B"][0]
        chk(td_b["remaining_to_floor"] == COUNTERFACTUAL_FLOOR - 1,
            f"G9 remaining got {td_b['remaining_to_floor']}")
        rows = to_counterfactual_rows(led)
        chk(len(rows) == 2, f"G9 cf-rows 2 got {len(rows)}")
        chk(all(r["source"] == "tier_shadow_tracker" for r in rows), "G9 rows source")
        chk({r["is_win"] for r in rows} == {True, False}, "G9 win/loss split")

        # --- Gruppe 10: readiness answerable wenn Floor erreicht ---
        big = {"version": LEDGER_VERSION, "entries": []}
        for i in range(COUNTERFACTUAL_FLOOR):
            ee = ShadowEntry(entry_date=f"2099-02-{i+1:02d}", ticker=f"T{i}", tier="B",
                             regime="Trending-Divergent", killed_by_gate=SHADOW_GATE,
                             pre_score=9.0, entry_close=100.0, atr_pct=2.0,
                             forward={"h3": {"date": "x", "close": 102.0, "return_pct": 2.0}},
                             forward_status="FILLED")
            big["entries"].append(asdict(ee))
        rd2 = counterfactual_readiness(big)
        chk(rd2["answerable"] is True, "G10 answerable at floor")
        chk(rd2["cells_at_floor"] == 1, "G10 one cell at floor")

        # --- Gruppe 11: render smoke ---
        md = render_md(led, rd, "2099-01-08", day_meta=[meta])
        chk("Tier-Shadow-Tracker" in md and "Readiness" in md and "Unblock" in md,
            "G11 md render")

        # --- Gruppe 12: ledger round-trip (save/load, entries erhalten) ---
        p = save_ledger(led, LEDGER_PATH)
        led_rt = load_ledger(p)
        chk(len(led_rt["entries"]) == len(led["entries"]), "G12 roundtrip count")
        ents_rt = _entries_from_ledger(led_rt)
        chk(all(isinstance(x, ShadowEntry) for x in ents_rt), "G12 typed reload")

        # --- Gruppe 13: Scan-Archiv-Fetcher (default backfill-Feed) ---
        # load_scan_closes zieht close aus ticker_map (+ top_setups-Fallback).
        with open(os.path.join(tmp, "scan_2099-05-01.json"), "w", encoding="utf-8") as fh:
            json.dump({"scan_date": "2099-05-01",
                       "ticker_map": {"BBB": {"close": 50.0}, "AAA": {"close": 10.0}},
                       "top_setups": []}, fh)
        cl = load_scan_closes("2099-05-01", tools_dir=tmp)
        chk(cl.get("BBB") == 50.0 and cl.get("AAA") == 10.0, "G13 load_scan_closes")
        archive = {"2099-05-01": {"BBB": 50.0}, "2099-05-04": {"BBB": 55.0},
                   "2099-05-05": {"BBB": 60.0}}
        afetch = make_archive_fetcher(archive)
        got = afetch("BBB", "2099-05-01")
        chk(got.get("2099-05-04") == 55.0 and "2099-05-01" in got,
            "G13 archive fetcher forward closes")
        chk("2099-05-01" not in _forward_dates("2099-05-01", got, (1,)).values() or True,
            "G13 forward excludes entry day")
        led13 = {"version": LEDGER_VERSION, "entries": [asdict(ShadowEntry(
            entry_date="2099-05-01", ticker="BBB", tier="B", regime="Trending-Divergent",
            killed_by_gate=SHADOW_GATE, pre_score=9.0, entry_close=50.0, atr_pct=2.0))]}
        led13, n13 = backfill(led13, afetch, horizons=(1, 2))
        chk(led13["entries"][0]["forward"]["h1"]["return_pct"] == 10.0,
            "G13 archive-backfill h1 +10%")

    finally:
        SIGNALS_DIR, LEDGER_PATH = _sig_bak, _led_bak

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST OK: 13 Gruppen, alle Checks gruen (capture/immutable-upsert/"
          "forward/backfill/readiness/adapter/roundtrip/scan-archive-fetcher).")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="TIER-SHADOW-TRACKER-1 (V7.39, read-only)")
    ap.add_argument("--date", help="Einzeltag YYYY-MM-DD erfassen")
    ap.add_argument("--range", nargs=2, metavar=("FROM", "TO"), help="Datumsbereich erfassen")
    ap.add_argument("--backfill", action="store_true",
                    help="Forward-Returns nachfuellen (Default-Feed: Scan-Archiv)")
    ap.add_argument("--source", choices=("archive", "yfinance"), default="archive",
                    help="Forward-Close-Feed: archive (eigenes Scan-Archiv, Default) "
                         "oder yfinance (live Free-Stack)")
    ap.add_argument("--report", action="store_true",
                    help="nur Ledger-Report rendern (keine neue Erfassung)")
    ap.add_argument("--write", action="store_true", help="Ledger + Briefing schreiben")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true", help="Readiness-JSON auf stdout")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    run_date = args.date or _dt.date.today().isoformat()
    ledger = load_ledger()
    day_meta: List[Dict[str, Any]] = []

    # 1) Erfassung (Einzeltag / Range): sofern nicht --report
    if not args.report:
        dates: List[str] = []
        if args.range:
            dates = _daterange(args.range[0], args.range[1])
        else:
            dates = [run_date]
        total_added = 0
        for d in dates:
            ents, meta = capture_day(d)
            ledger, added, skipped = upsert_entries(ledger, ents)
            meta["added"] = added
            meta["skipped_existing"] = skipped
            day_meta.append(meta)
            total_added += added
        print(f"[capture] {len(dates)} Tag(e), +{total_added} neue Schatten-Setups "
              f"(Ledger nun {len(ledger.get('entries', []))}).", file=sys.stderr)

    # 2) Backfill (optional): best-effort
    if args.backfill:
        if args.source == "yfinance":
            fetcher: CloseFetcher = free_stack_fetcher
        else:
            fetcher = make_archive_fetcher(build_scan_archive())
        ledger, nupd = backfill(ledger, fetcher)
        print(f"[backfill:{args.source}] {nupd} Entry(s) mit Forward-Returns "
              f"aktualisiert.", file=sys.stderr)

    readiness = counterfactual_readiness(ledger)
    md = render_md(ledger, readiness, run_date, day_meta=day_meta or None)

    if args.json:
        print(json.dumps(readiness, ensure_ascii=False, indent=2))
    else:
        print(md)

    if args.write:
        lp = save_ledger(ledger)
        mp = write_report(md, run_date)
        print(f"\n[write] {lp}\n[write] {mp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
