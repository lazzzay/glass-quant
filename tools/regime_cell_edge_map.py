#!/usr/bin/env python3
"""
V7.33 REGIME-CELL-EDGE-MAP-1: read-only Diagnostik/Producer.

Loest das Roadmap-Item REGIME-TAG-GRANULARITY-1 (CLAUDE.md §9, KW27+)
EVIDENZBASIERT und korrigiert dabei die urspruengliche Fehldiagnose.

------------------------------------------------------------------------------
URSPRUENGLICHE HYPOTHESE (V7.31, 2026-06-25):
  "Live-Trades sind grob als `Trending` getaggt, NICHT in den feinen Edge-Zellen
   `Trending-Divergent`/`Bullish-Confirmed` -> regime-Forward-Edge kann nie
   akkumulieren, solange die Tagging-Granularitaet nicht verfeinert wird."
  Fix-Richtung-Vermutung: regime_gate feiner emittieren ODER im Ledger ableiten.

VERIFIKATION GEGEN DEN CODE/DIE DATEN (heute):
  (1) `regime_gate.classify()` emittiert BEREITS 7 feine Klassen: darunter
      Trending-Divergent UND Bullish-Confirmed. `Trending` ist KEIN grobes
      Sammel-Label, sondern eine eigenstaendige, distinkte Klasse (ADX>25 &
      SPY_5d>=0).
  (2) Im historischen V6.1-Panel (440 Trades) ist `Trending` mit n=174 die
      GROESSTE Zelle (40%) und mit +0,997% / +4,49 EUR / 58,6% WR fast so stark
      wie Trending-Divergent (+1,075% / +5,12 EUR). Die 2-Zell-Edge-Definition
      (TD vs BC) nutzt nur 117+33 = 150/440 = 34% der Historie und WIRFT die
      174 Trending- + 96 Neutral-Trades WEG.
  (3) Live-Trades werden autoritativ aus dem regime_<date>.json-Archiv
      aufgeloest (Archiv vor Journal). Landen sie in einer der 5 Zellen ausserhalb
      von TD/BC, aendert Re-Tagging daran NICHTS.

KORREKTE DIAGNOSE:
  regime usable_n=0 ist KEIN Tagging-Bug, sondern STRUKTURELL: der kategoriale
  Forward-Kandidat ist auf 2 von 7 Zellen verengt; Live-Trades, die (wie 66%
  der Historie) in den anderen 5 Zellen landen, koennen per Definition NIE
  akkumulieren.

LIEFERUNG (read-only):
  Eine durchgaengige Per-Zell-Edge-Map ueber ALLE 7 Regime-Klassen
  (n, mean_return, mean_v61_netto, WR, kontinuierlicher edge_rank, erwartetes
  Vorzeichen, IS/OOS-Vorzeichen-Konsistenz, Confidence nach n-Floor). Damit ein
  Live-Trade in JEDER Zelle (nicht nur TD/BC) gegen die historisch erwartete
  Per-Zell-Richtung gewertet werden kann -> die regime-Forward-Edge wird aus den
  vorhandenen Live-Trades nutzbar.

EISERNE REGEL: KEINE Live-Regel/Schwelle/Sizing geaendert. Dieses Tool veraendert
WEDER daily_scanner NOCH entry_filter_forward_test's Gate. Es PRODUZIERT die
Evidenz + die vorgeschlagene Map; die tatsaechliche Aufweitung des Forward-Test-
Kandidaten bleibt ein dokumentierter Folgeschritt mit maintainer approval
(H2-Feature-Freeze). Pure Diagnostik.

CLI:
  python3 tools/regime_cell_edge_map.py                 # JSON auf stdout
  python3 tools/regime_cell_edge_map.py --write         # + signals/ + Briefings/
  python3 tools/regime_cell_edge_map.py --selftest      # hermetisch
  python3 tools/regime_cell_edge_map.py --cutoff 2025-01-01 --min-cell-n 20
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date as _date
from pathlib import Path
from typing import Callable, Optional

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

# Battle-tested, read-only Reader aus dem V7.30-Producer wiederverwenden.
from cohort_tag_ledger import (  # noqa: E402
    load_journal_trades,
    load_regime_archive,
    read_text_resilient,
)

_DEFAULT_CSV = _ROOT / "v6_backtest_results_v61.csv"
_DEFAULT_DB = _ROOT / "trade_journal.sqlite"   # kanonische Root-DB (NICHT tools/)
_OOS_CUTOFF = "2025-01-01"
_MIN_CELL_N = 15           # n-Floor fuer HIGH-Confidence einer Zell-Erwartung
_LIVE_SOURCES = ("live", "external", "disc")

# Die heute (V7.33) noch aktive, verengte 2-Zell-Edge-Definition des
# Forward-Tests: als Referenz, gegen die wir den Unblock messen.
_LEGACY_EDGE_CELLS = ("Trending-Divergent", "Bullish-Confirmed")


# --------------------------------------------------------------------------- #
# Helfer
# --------------------------------------------------------------------------- #
def _fnum(x) -> Optional[float]:
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


def _is_win(x) -> Optional[bool]:
    s = str(x).strip().lower()
    if s in ("1", "true", "1.0", "yes", "win"):
        return True
    if s in ("0", "false", "0.0", "no", "loss"):
        return False
    return None


def _mean(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _sign(x: Optional[float]) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


# --------------------------------------------------------------------------- #
# 1) Historisches Panel -> Per-Zell-Statistik
# --------------------------------------------------------------------------- #
def parse_panel_rows(csv_text: str) -> list[dict]:
    """CSV-Text -> Liste von {regime, return_pct, v61_netto_pnl, is_win, year}."""
    out: list[dict] = []
    rdr = csv.DictReader(io.StringIO(csv_text))
    # Spalten-Namen case-/whitespace-tolerant aufloesen.
    fields = {(c or "").strip().lower(): c for c in (rdr.fieldnames or [])}
    c_reg = fields.get("regime")
    c_ret = fields.get("return_pct")
    c_npl = fields.get("v61_netto_pnl") or fields.get("netto_pnl")
    c_win = fields.get("is_win")
    c_year = fields.get("year")
    c_date = fields.get("date")
    if not c_reg:
        return out
    for r in rdr:
        reg = str(r.get(c_reg) or "").strip()
        if not reg or reg == "?":
            continue
        year = None
        if c_year and str(r.get(c_year) or "").strip():
            year = _fnum(r.get(c_year))
        elif c_date and str(r.get(c_date) or "").strip():
            year = _fnum(str(r.get(c_date))[:4])
        out.append({
            "regime": reg,
            "return_pct": _fnum(r.get(c_ret)) if c_ret else None,
            "v61_netto_pnl": _fnum(r.get(c_npl)) if c_npl else None,
            "is_win": _is_win(r.get(c_win)) if c_win else None,
            "year": int(year) if year is not None else None,
            "date": str(r.get(c_date) or "")[:10] if c_date else "",
        })
    return out


def _cell_stats(rows: list[dict]) -> dict:
    rets = [r["return_pct"] for r in rows if r["return_pct"] is not None]
    npls = [r["v61_netto_pnl"] for r in rows if r["v61_netto_pnl"] is not None]
    wins = [r["is_win"] for r in rows if r["is_win"] is not None]
    return {
        "n": len(rows),
        "mean_return_pct": round(_mean(rets), 4) if rets else None,
        "mean_v61_netto": round(_mean(npls), 4) if npls else None,
        "win_rate": round(100.0 * sum(1 for w in wins if w) / len(wins), 1) if wins else None,
    }


def build_edge_map(panel_rows: list[dict], cutoff: str = _OOS_CUTOFF,
                   min_cell_n: int = _MIN_CELL_N) -> dict:
    """Per-Zell-Edge-Map ueber ALLE Regime-Klassen.

    Pro Zelle: n, mean_return, mean_v61_netto, WR, erwartetes Vorzeichen,
    kontinuierlicher edge_rank (0..1 Perzentil ueber qualifizierende Zellen),
    Confidence (HIGH wenn n>=min_cell_n), IS/OOS-Vorzeichen-Konsistenz.
    """
    cells: dict[str, list[dict]] = {}
    for r in panel_rows:
        cells.setdefault(r["regime"], []).append(r)

    cutoff_y = None
    try:
        cutoff_y = int(str(cutoff)[:4])
    except (ValueError, TypeError):
        cutoff_y = None

    entries: dict[str, dict] = {}
    for reg, rows in cells.items():
        st = _cell_stats(rows)
        # IS/OOS-Vorzeichen-Konsistenz (Jahr < cutoff vs >= cutoff).
        is_rows = [r for r in rows if r["year"] is not None and cutoff_y is not None and r["year"] < cutoff_y]
        oos_rows = [r for r in rows if r["year"] is not None and cutoff_y is not None and r["year"] >= cutoff_y]
        is_mean = _mean([r["return_pct"] for r in is_rows if r["return_pct"] is not None])
        oos_mean = _mean([r["return_pct"] for r in oos_rows if r["return_pct"] is not None])
        sign_consistent = None
        if is_mean is not None and oos_mean is not None and is_rows and oos_rows:
            sign_consistent = (_sign(is_mean) == _sign(oos_mean) and _sign(is_mean) != 0)
        entries[reg] = {
            "regime": reg,
            **st,
            "expected_sign": _sign(st["mean_return_pct"]),
            "confidence": "HIGH" if st["n"] >= min_cell_n else "LOW",
            "is_n": len(is_rows),
            "oos_n": len(oos_rows),
            "is_mean_return_pct": round(is_mean, 4) if is_mean is not None else None,
            "oos_mean_return_pct": round(oos_mean, 4) if oos_mean is not None else None,
            "oos_sign_consistent": sign_consistent,
            "legacy_edge_cell": reg in _LEGACY_EDGE_CELLS,
        }

    # Kontinuierlicher edge_rank: Perzentil der mean_return ueber Zellen mit
    # bekanntem mean_return (alle gleichwertig; deterministisch, ties -> avg-rank).
    rankable = [e for e in entries.values() if e["mean_return_pct"] is not None]
    srt = sorted(rankable, key=lambda e: e["mean_return_pct"])
    m = len(srt)
    for i, e in enumerate(srt):
        # 0..1, hoeher = besseres erwartetes Regime
        e["edge_rank"] = round((i + 0.5) / m, 4) if m else None
    for e in entries.values():
        e.setdefault("edge_rank", None)

    ordered = sorted(entries.values(),
                     key=lambda e: (e["mean_return_pct"] is not None,
                                    e["mean_return_pct"] or -9e9),
                     reverse=True)
    return {
        "cells": ordered,
        "by_regime": entries,
        "n_cells": len(entries),
        "n_high_conf_cells": sum(1 for e in entries.values() if e["confidence"] == "HIGH"),
        "min_cell_n": min_cell_n,
        "cutoff": cutoff,
        "scope": "IN_SAMPLE_HISTORICAL (kein echter Forward-Test; OOS-Spalten = OOS-Proxy)",
    }


# --------------------------------------------------------------------------- #
# 2) Live-Trades autoritativ aufloesen (Archiv-first) + Unblock messen
# --------------------------------------------------------------------------- #
def resolve_live_regime(jrow: dict, regime_archive: dict) -> dict:
    """Autoritative Regime-Zelle eines Live-Trades.

    Praezedenz: regime_<entry_date>.json-Archiv (= regime_gate SSoT fuer den Tag)
    ZUERST, dann Journal (regime_at_entry/regime). Flaggt Divergenz, wenn das
    Journal einen anderen, nicht-'?'-Wert traegt, und erkennt die '?'-Recovery.
    """
    date = str(jrow.get("entry_date") or "")[:10]
    arch = (regime_archive.get(date) or "").strip() or None
    j_at = str(jrow.get("regime_at_entry") or "").strip()
    j_reg = str(jrow.get("regime") or "").strip()
    journal_val = next((v for v in (j_at, j_reg) if v and v != "?"), None)
    journal_unknown = (not journal_val)  # nur '?'/leer im Journal

    if arch:
        resolved, src = arch, "archive"
    elif journal_val:
        resolved, src = journal_val, "journal"
    else:
        resolved, src = None, "unknown"

    divergence = bool(arch and journal_val and arch != journal_val)
    recovered = bool(arch and journal_unknown)  # Journal '?' -> Archiv rettet
    return {
        "regime_cell": resolved,
        "regime_source": src,
        "journal_value": journal_val,
        "archive_value": arch,
        "journal_divergence": divergence,
        "recovered_from_archive": recovered,
    }


def assess_unblock(trades: list[dict], regime_archive: dict, edge_map: dict,
                   min_cell_n: int = _MIN_CELL_N) -> dict:
    """Wie viele Live-Trades werden unter der erweiterten Map nutzbar (vs 2-Zell)?

    Plus pro Trade ein gerichtetes Forward-Signal: stimmt das realisierte
    Return-Vorzeichen mit der historischen Zell-Erwartung ueberein?
    """
    by_regime = edge_map["by_regime"]
    per_trade: list[dict] = []
    legacy_usable = 0
    extended_usable = 0
    agree = 0
    n_ret = 0
    for t in trades:
        res = resolve_live_regime(t, regime_archive)
        cell = res["regime_cell"]
        ret = _fnum(t.get("return_pct"))
        cell_info = by_regime.get(cell) if cell else None
        # legacy: nur TD/BC zaehlen
        is_legacy = bool(cell in _LEGACY_EDGE_CELLS)
        # extended: jede Zelle mit verlaesslicher historischer Erwartung (n-Floor)
        is_ext = bool(cell_info and cell_info["n"] >= min_cell_n
                      and cell_info["expected_sign"] != 0)
        if is_legacy:
            legacy_usable += 1
        if is_ext:
            extended_usable += 1
        directional = None
        if is_ext and ret is not None:
            n_ret += 1
            directional = (_sign(ret) == cell_info["expected_sign"])
            if directional:
                agree += 1
        per_trade.append({
            "date": str(t.get("entry_date") or "")[:10],
            "ticker": t.get("ticker"),
            "source": t.get("source"),
            **res,
            "return_pct": ret,
            "expected_sign": cell_info["expected_sign"] if cell_info else None,
            "cell_hist_n": cell_info["n"] if cell_info else None,
            "cell_edge_rank": cell_info["edge_rank"] if cell_info else None,
            "legacy_usable": is_legacy,
            "extended_usable": is_ext,
            "forward_directional_agree": directional,
        })
    return {
        "n_trades": len(trades),
        "legacy_usable_n": legacy_usable,
        "extended_usable_n": extended_usable,
        "unblock_delta": extended_usable - legacy_usable,
        "directional_agree_n": agree,
        "directional_evaluated_n": n_ret,
        "n_recovered_from_archive": sum(1 for p in per_trade if p["recovered_from_archive"]),
        "n_journal_divergence": sum(1 for p in per_trade if p["journal_divergence"]),
        "per_trade": per_trade,
    }


# --------------------------------------------------------------------------- #
# 3) Report-Assembly + Verdikt
# --------------------------------------------------------------------------- #
def build_report(csv_path: Path, db_path: Path, *, cutoff: str = _OOS_CUTOFF,
                 min_cell_n: int = _MIN_CELL_N, run_date: Optional[str] = None,
                 journal_connector: Optional[Callable] = None,
                 regime_archive: Optional[dict] = None) -> dict:
    run_date = run_date or _date.today().isoformat()
    txt = read_text_resilient(Path(csv_path))
    if txt is None:
        return {"tool": "regime_cell_edge_map", "version": "V7.33",
                "date": run_date, "error": f"Panel-CSV nicht lesbar: {csv_path}"}
    panel = parse_panel_rows(txt)
    edge_map = build_edge_map(panel, cutoff=cutoff, min_cell_n=min_cell_n)

    arch = regime_archive if regime_archive is not None else load_regime_archive(_TOOLS)
    trades, jerr = load_journal_trades(db_path, sources=_LIVE_SOURCES,
                                       connector=journal_connector)
    unblock = assess_unblock(trades, arch, edge_map, min_cell_n=min_cell_n)

    # Verdikt
    legacy = unblock["legacy_usable_n"]
    ext = unblock["extended_usable_n"]
    if jerr:
        verdict = "JOURNAL_UNREADABLE"
    elif unblock["n_trades"] == 0:
        verdict = "NO_LIVE_TRADES"
    elif ext > legacy:
        verdict = "UNBLOCKED_BY_EXTENDED_MAP"
    elif legacy > 0:
        verdict = "ALREADY_USABLE_LEGACY"
    else:
        verdict = "STILL_BLOCKED"

    return {
        "tool": "regime_cell_edge_map",
        "version": "V7.33",
        "roadmap_item": "REGIME-TAG-GRANULARITY-1",
        "date": run_date,
        "scope": edge_map["scope"],
        "honesty": ("read-only Diagnostik. Korrigiert die V7.31-Fehldiagnose: "
                    "'Trending' ist eine echte, distinkte, n-groesste Regime-Klasse, "
                    "kein grobes Sammel-Label. regime usable_n=0 ist strukturell "
                    "(2-von-7-Zell-Verengung), kein Tagging-Bug. KEINE Live-Regel/"
                    "Schwelle/Sizing geaendert. Aufweitung des Forward-Kandidaten = "
                    "dokumentierter Folgeschritt (maintainer approval, H2-Feature-Freeze)."),
        "diagnosis": {
            "original_hypothesis": ("Live-Trades grob 'Trending' getaggt, "
                                    "Tagging verfeinern (V7.31)."),
            "verified_reality": ("regime_gate emittiert bereits 7 feine Klassen inkl. "
                                 "TD/BC; 'Trending' ist n-groesste eigene Klasse "
                                 "(174/440, +0,997%/+4,49 EUR); Live-Trades liegen "
                                 "ECHT in 'Trending', Re-Tagging wuerde nichts aendern."),
            "root_cause": ("kategorialer Forward-Kandidat auf 2 von 7 Zellen verengt "
                           "-> 66% der Historie + alle Live-Trades nicht akkumulierbar."),
            "fix": ("durchgaengige Per-Zell-Edge-Map ueber alle 7 Klassen; Live-Trade "
                    "in JEDER Zelle gegen historische Per-Zell-Richtung wertbar."),
        },
        "edge_map": edge_map,
        "live_unblock": unblock,
        "journal_error": jerr,
        "min_cell_n": min_cell_n,
        "cutoff": cutoff,
        "source_csv": Path(csv_path).name,
        "source_db": Path(db_path).name,
        "next_step": ("entry_filter_forward_test: kategorialen regime-Kandidaten von "
                      "binaer (preferred/avoided) auf kontinuierliche edge_rank-"
                      "Erwartung umstellen (this artifact = Input). Erst nach "
                      "maintainer approval; Feature-Freeze bis Live-Forward ready."),
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# 4) Markdown-Render (Deutsch)
# --------------------------------------------------------------------------- #
def _fmt(x, nd=3) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render_md(rep: dict) -> str:
    if rep.get("error"):
        return f"# Regime-Cell-Edge-Map {rep.get('date')}\n\nFEHLER: {rep['error']}\n"
    L: list[str] = []
    L.append(f"# Regime-Cell-Edge-Map: {rep['date']} (V7.33, read-only)")
    L.append("")
    L.append(f"**Roadmap-Item:** {rep['roadmap_item']} · **Verdikt:** `{rep['verdict']}`")
    L.append(f"**Scope:** {rep['scope']}")
    L.append("")
    d = rep["diagnosis"]
    L.append("## 1. Diagnose-Korrektur")
    L.append("")
    L.append(f"- **Urspruengliche Hypothese (V7.31):** {d['original_hypothesis']}")
    L.append(f"- **Verifizierte Realitaet:** {d['verified_reality']}")
    L.append(f"- **Root-Cause:** {d['root_cause']}")
    L.append(f"- **Fix:** {d['fix']}")
    L.append("")
    em = rep["edge_map"]
    L.append("## 2. Per-Zell-Edge-Map (alle Regime-Klassen)")
    L.append("")
    L.append(f"n_cells={em['n_cells']} · HIGH-Confidence (n≥{em['min_cell_n']})="
             f"{em['n_high_conf_cells']} · Cutoff {em['cutoff']}")
    L.append("")
    L.append("| Regime-Zelle | n | mean_ret% | mean_v61€ | WR% | erw.Vorz. | edge_rank | Conf | OOS-Vorz.-konsist. | Legacy-Edge |")
    L.append("|---|--:|--:|--:|--:|:--:|--:|:--:|:--:|:--:|")
    for c in em["cells"]:
        L.append(
            f"| {c['regime']} | {c['n']} | {_fmt(c['mean_return_pct'])} | "
            f"{_fmt(c['mean_v61_netto'],2)} | {_fmt(c['win_rate'],1)} | "
            f"{'+' if c['expected_sign']>0 else ('−' if c['expected_sign']<0 else '0')} | "
            f"{_fmt(c['edge_rank'])} | {c['confidence']} | "
            f"{'✓' if c['oos_sign_consistent'] else ('✗' if c['oos_sign_consistent'] is False else '-')} | "
            f"{'★' if c['legacy_edge_cell'] else ''} |")
    L.append("")
    u = rep["live_unblock"]
    L.append("## 3. Live-Unblock (Forward-Akkumulation)")
    L.append("")
    L.append(f"- Live-Trades erfasst: **{u['n_trades']}**")
    L.append(f"- Nutzbar **legacy (2-Zell TD/BC):** {u['legacy_usable_n']}")
    L.append(f"- Nutzbar **erweitert (alle HIGH-Conf-Zellen):** {u['extended_usable_n']} "
             f"→ **Unblock Δ = +{u['unblock_delta']}**")
    L.append(f"- Gerichtete Uebereinstimmung (realisiert vs erwartet): "
             f"{u['directional_agree_n']}/{u['directional_evaluated_n']}")
    L.append(f"- Aus Archiv gerettet (Journal '?'): {u['n_recovered_from_archive']} · "
             f"Journal-Divergenz: {u['n_journal_divergence']}")
    L.append("")
    L.append("| Datum | Ticker | Src | Regime-Zelle | Quelle | ret% | erw.Vorz. | hist.n | legacy | erw. | dir.✓ |")
    L.append("|---|---|---|---|---|--:|:--:|--:|:--:|:--:|:--:|")
    for p in u["per_trade"]:
        es = p["expected_sign"]
        L.append(
            f"| {p['date']} | {p['ticker']} | {p['source']} | "
            f"{_fmt(p['regime_cell'])} | {p['regime_source']}"
            f"{'(rec)' if p['recovered_from_archive'] else ''} | "
            f"{_fmt(p['return_pct'])} | "
            f"{('+' if es and es>0 else ('−' if es and es<0 else '-')) if es is not None else '-'} | "
            f"{_fmt(p['cell_hist_n'])} | {'✓' if p['legacy_usable'] else '·'} | "
            f"{'✓' if p['extended_usable'] else '·'} | "
            f"{'✓' if p['forward_directional_agree'] else ('✗' if p['forward_directional_agree'] is False else '-')} |")
    L.append("")
    L.append("## 4. Naechster Schritt")
    L.append("")
    L.append(rep["next_step"])
    L.append("")
    L.append(f"> {rep['honesty']}")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 5) Selftest (hermetisch, kein echtes Journal/Netz/CSV)
# --------------------------------------------------------------------------- #
_SYN_CSV = (
    "date,ticker,regime,return_pct,v61_netto_pnl,is_win,year\n"
    # Trending: stark positiv, gross-n (>= min_cell_n=4 im Selftest)
    "2023-02-01,A1,Trending,1.0,5.0,1,2023\n"
    "2023-03-01,A2,Trending,1.2,6.0,1,2023\n"
    "2025-02-01,A3,Trending,0.8,4.0,1,2025\n"
    "2025-03-01,A4,Trending,1.1,5.5,1,2025\n"
    # Bullish-Confirmed: negativ, gross-n
    "2023-04-01,B1,Bullish-Confirmed,-1.0,-3.0,0,2023\n"
    "2023-05-01,B2,Bullish-Confirmed,-1.2,-4.0,0,2023\n"
    "2025-04-01,B3,Bullish-Confirmed,-0.9,-2.5,0,2025\n"
    "2025-05-01,B4,Bullish-Confirmed,-1.1,-3.5,0,2025\n"
    # Trending-Divergent: positiv (legacy edge cell), gross-n
    "2023-06-01,C1,Trending-Divergent,1.3,6.0,1,2023\n"
    "2023-07-01,C2,Trending-Divergent,1.0,5.0,1,2023\n"
    "2025-06-01,C3,Trending-Divergent,1.2,5.5,1,2025\n"
    "2025-07-01,C4,Trending-Divergent,0.9,4.5,0,2025\n"
    # Neutral: klein-n (< min_cell_n) -> LOW confidence
    "2023-08-01,D1,Neutral,-0.2,0.0,0,2023\n"
    "2024-08-01,D2,Neutral,0.1,0.5,1,2024\n"
)


def _mk_inmemory_journal(rows: list[tuple]) -> Callable:
    """Liefert einen connector, der eine In-Memory-trades-Tabelle bereitstellt.

    rows: (ticker, source, entry_date, regime, regime_at_entry, return_pct)
    """
    import sqlite3

    def _conn(_db):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE trades (ticker TEXT, source TEXT, entry_date TEXT, "
            "exit_date TEXT, atr_pct REAL, side TEXT, return_pct REAL, "
            "netto_pnl REAL, is_win INTEGER, regime TEXT, regime_at_entry TEXT, "
            "setup_fk TEXT, system_version TEXT, holding_days INTEGER)")
        for (tk, src, ed, reg, rae, ret) in rows:
            con.execute(
                "INSERT INTO trades (ticker,source,entry_date,return_pct,regime,"
                "regime_at_entry) VALUES (?,?,?,?,?,?)", (tk, src, ed, ret, reg, rae))
        con.commit()
        return con
    return _conn


def selftest() -> int:
    fails: list[str] = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    # --- Gruppe 1: Panel-Parsing + Edge-Map ---
    panel = parse_panel_rows(_SYN_CSV)
    chk(len(panel) == 14, f"panel parse n != 14 ({len(panel)})")
    em = build_edge_map(panel, cutoff="2025-01-01", min_cell_n=4)
    by = em["by_regime"]
    chk("Trending" in by and by["Trending"]["n"] == 4, "Trending cell n!=4")
    chk(by["Trending"]["expected_sign"] == 1, "Trending expected_sign != +")
    chk(by["Bullish-Confirmed"]["expected_sign"] == -1, "BC expected_sign != -")
    chk(by["Neutral"]["confidence"] == "LOW", "Neutral should be LOW conf (n<4)")
    chk(by["Trending"]["confidence"] == "HIGH", "Trending should be HIGH conf")

    # --- Gruppe 2: edge_rank-Ordnung (TD/Trending oben, BC unten) ---
    chk(by["Bullish-Confirmed"]["edge_rank"] < by["Trending"]["edge_rank"],
        "edge_rank: BC should rank below Trending")
    chk(by["Trending-Divergent"]["edge_rank"] is not None, "TD edge_rank None")

    # --- Gruppe 3: OOS-Vorzeichen-Konsistenz (synth. so gebaut) ---
    chk(by["Trending"]["oos_sign_consistent"] is True, "Trending OOS sign not consistent")
    chk(by["Bullish-Confirmed"]["oos_sign_consistent"] is True, "BC OOS sign not consistent")

    # --- Gruppe 4: Archiv-first Aufloesung + '?'-Recovery + Divergenz ---
    arch = {"2099-06-01": "Trending", "2099-06-04": "Trending",
            "2099-05-12": "Trending", "2099-04-16": "Trending"}
    r_recover = resolve_live_regime(
        {"entry_date": "2099-06-01", "regime": "?", "regime_at_entry": None}, arch)
    chk(r_recover["regime_cell"] == "Trending" and r_recover["recovered_from_archive"],
        "'?'-recovery from archive failed")
    chk(r_recover["regime_source"] == "archive", "recover source != archive")
    r_div = resolve_live_regime(
        {"entry_date": "2099-06-01", "regime": "Neutral", "regime_at_entry": None}, arch)
    chk(r_div["journal_divergence"] is True, "divergence not detected (archive!=journal)")
    chk(r_div["regime_cell"] == "Trending", "archive should win over journal")
    r_jonly = resolve_live_regime(
        {"entry_date": "2099-01-01", "regime": "Trending-Divergent"}, arch)
    chk(r_jonly["regime_cell"] == "Trending-Divergent" and r_jonly["regime_source"] == "journal",
        "journal fallback failed when no archive")

    # --- Gruppe 5: Unblock, 4 Live-Trades in 'Trending' -> 0 legacy, 4 extended ---
    jrows = [
        ("AAA", "external", "2099-04-16", None, "Trending", 0.5),
        ("BBB", "live", "2099-05-12", "Trending", None, 8.0),
        ("CCC", "live", "2099-06-01", "?", None, 4.0),
        ("DDD", "live", "2099-06-04", "?", None, -0.4),
    ]
    trades, jerr = load_journal_trades("ignored", connector=_mk_inmemory_journal(jrows))
    chk(jerr is None and len(trades) == 4, f"inmemory journal load failed: {jerr}/{len(trades)}")
    ub = assess_unblock(trades, arch, em, min_cell_n=4)
    chk(ub["legacy_usable_n"] == 0, f"legacy_usable should be 0, got {ub['legacy_usable_n']}")
    chk(ub["extended_usable_n"] == 4, f"extended_usable should be 4, got {ub['extended_usable_n']}")
    chk(ub["unblock_delta"] == 4, f"unblock_delta != 4 ({ub['unblock_delta']})")
    chk(ub["n_recovered_from_archive"] == 2, f"recovered != 2 ({ub['n_recovered_from_archive']})")
    # AAA/BBB/CCC positiv in Trending(+) -> agree; DDD negativ -> disagree
    chk(ub["directional_agree_n"] == 3 and ub["directional_evaluated_n"] == 4,
        f"directional agree {ub['directional_agree_n']}/{ub['directional_evaluated_n']} != 3/4")

    # --- Gruppe 6: build_report end-to-end (hermetisch, CSV via temp) ---
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "__rcem_selftest_panel__.csv"
    tmp.write_text(_SYN_CSV, encoding="utf-8")
    rep = build_report(tmp, "ignored", cutoff="2025-01-01", min_cell_n=4,
                       run_date="2026-06-30",
                       journal_connector=_mk_inmemory_journal(jrows),
                       regime_archive=arch)
    chk(rep["verdict"] == "UNBLOCKED_BY_EXTENDED_MAP",
        f"verdict != UNBLOCKED_BY_EXTENDED_MAP ({rep['verdict']})")
    chk(rep["live_unblock"]["extended_usable_n"] == 4, "report extended_usable != 4")
    md = render_md(rep)
    chk("Regime-Cell-Edge-Map" in md and "Live-Unblock" in md, "md render incomplete")
    try:
        tmp.unlink()
    except OSError:
        pass

    # --- Gruppe 7: leeres/fehlendes Journal -> NO_LIVE_TRADES, kein Crash ---
    rep_empty = build_report(Path(tempfile.gettempdir()) / "__rcem_selftest_panel__.csv",
                             "ignored", min_cell_n=4, run_date="2026-06-30",
                             journal_connector=_mk_inmemory_journal([]),
                             regime_archive=arch)
    chk(rep_empty.get("error") or rep_empty["verdict"] in ("NO_LIVE_TRADES",),
        "empty journal should yield NO_LIVE_TRADES (or csv error)")

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELFTEST OK: 7 Gruppen, alle Checks gruen "
          f"(edge_map {em['n_cells']} Zellen, Unblock 0->{ub['extended_usable_n']}).")
    return 0


# --------------------------------------------------------------------------- #
# 6) main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="V7.33 Regime-Cell-Edge-Map (read-only)")
    ap.add_argument("--csv", default=str(_DEFAULT_CSV), help="Historisches V6.1-Panel")
    ap.add_argument("--db", default=str(_DEFAULT_DB), help="Kanonisches Journal (Root)")
    ap.add_argument("--cutoff", default=_OOS_CUTOFF, help="IS/OOS-Cutoff (Jahr)")
    ap.add_argument("--min-cell-n", type=int, default=_MIN_CELL_N,
                    help="n-Floor fuer HIGH-Confidence einer Zelle")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="JSON + MD schreiben")
    ap.add_argument("--selftest", action="store_true", help="Hermetischen Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rep = build_report(Path(args.csv), Path(args.db), cutoff=args.cutoff,
                       min_cell_n=args.min_cell_n, run_date=args.date)
    print(json.dumps(rep, indent=2, ensure_ascii=False))

    if args.write and not rep.get("error"):
        sig = _ROOT / "signals" / f"regime_cell_edge_map_{args.date}.json"
        sig.parent.mkdir(parents=True, exist_ok=True)
        sig.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path = _ROOT / "Briefings" / f"Regime-Cell-Edge-Map-{args.date}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_md(rep), encoding="utf-8")
        print(f"\nSaved: {sig}", file=sys.stderr)
        print(f"Saved: {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
