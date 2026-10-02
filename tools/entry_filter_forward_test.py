#!/usr/bin/env python3
"""
ENTRY-FILTER-FORWARD-TEST-1 (V7.29 · 2026-06-23 Di KW26 · FEATURE).

V7.32 LIVE-FORWARD-ETA-1 (2026-06-26 Fr KW26 · FEATURE): macht den seit V7.31
benannten, jetzt rein STRUKTURELLEN Engpass quantitativ sichtbar: bei der
beobachteten Akkumulations-Kadenz der feature-nutzbaren Live-Trades, wie weit
(Tage + projiziertes Kalenderdatum) ist das Ready-Gate (min_n=40) noch entfernt?
`project_eta()` extrapoliert linear die Inter-Arrival-Zeit der bisher getaggten
nutzbaren Trades: getrennt fuer das feature-nutzbare Gate, den Lead-Kandidaten
(atr_pct, der bindende numerische) und pro Kandidat. Statuts: GATE_MET /
REACHABLE_NEAR|MID|DISTANT / INSUFFICIENT_HISTORY (n<2, keine Kadenz schaetzbar)
/ NOT_ACCUMULATING (n=0: fuer regime der maschinenlesbare Beleg von
REGIME-TAG-GRANULARITY-1) / NO_LEDGER. EHRLICHKEITS-GRENZE: das ist eine reine
Kadenz-Fortschreibung, KEINE Prognose-Garantie (eine erhoehte oder gestoppte
Handelsfrequenz verschiebt die ETA proportional). read-only, KEINE Live-Regel/
Schwelle/Sizing geaendert.

V7.31 LIVE-FORWARD-READINESS-SEMANTICS-1 (2026-06-25 Do KW26 · FEATURE): das
`live_forward`-Readiness-Gate zaehlt jetzt FEATURE-NUTZBARE (forward_eligible)
Ledger-Zeilen statt roher post-cutoff Zeilen: damit ist es bitgenau konsistent
mit der Gating-Logik des V7.30-Producers `cohort_tag_ledger.forward_ready`
(Consumer und Producer koennen nicht mehr divergieren: 'erfasst' != 'feature-
komplett'). Zusaetzlich pro registriertem Kandidat (atr_pct = Lead) eine edge-
spezifische `usable_n`-Zaehlung (schaerfer als der permissive forward_eligible-OR
des Producers, weil der numerische Forward-Test atr_pct bzw. rsi14 KONKRET
braucht). read-only, KEINE Live-Regel/Schwelle/Sizing geaendert.

Read-only OOS-Forward-Validierung der ENTRY-Seite: der explizit vorgeplante
naechste Schritt nach V7.27 ENTRY-FILTER-ROBUSTNESS. V7.27 hat drei Entry-Filter-
Kandidaten IN-SAMPLE als robust ausgewiesen (FDR + temporale CV + Bootstrap auf
demselben 258-Trade-Korpus) und am Ende woertlich geschrieben:

    "robust = IN-SAMPLE (kein Forward-Test); naechste Phase = Forward-Validierung
     von atr_pct/regime(/rsi14) auf frischen Daten (vor jeder Regel-/Sizing-
     Aenderung)."

Dieses Tool ist das ENTRY-seitige Gegenstueck zu `tools/exit_forward_test.py`
(das die Exit-Schemata per OOS-Train/Test-Split out-of-sample prueft). Es nimmt
GENAU die drei von V7.27 als robust ausgewiesenen Kandidaten als
VOR-REGISTRIERTE Hypothesen und prueft, ob ihre Edge auf einem zeitlich
ABGETRENNTEN Out-of-Sample-Fenster (Trades >= Cutoff) ueberlebt: getrennt von
dem In-Sample-Fenster (Trades < Cutoff), auf dem die Edge entdeckt wurde.

Kernfrage (H2):

    **Halten die V7.27-Edges (atr_pct high_better >> regime >> rsi14 low_better)
     auf zeitlich nicht-ueberlappenden Out-of-Sample-Trades, oder waren sie
     In-Sample-Artefakte?**

VOR-REGISTRIERTE KANDIDATEN (eingefroren aus V7.27, NICHT aus den OOS-Daten neu
gewaehlt: das ist der Punkt eines Forward-Tests):

  atr_pct  numeric  high_better  IS rank_ic +0,1969  p_adj 0,007  (staerkster)
  regime   categ.   Trending-Divergent >> Bullish-Confirmed  IS spread ~2,60pp
  rsi14    numeric  low_better   IS rank_ic -0,1317  p_adj 0,086  (schwaechster,
                                 Vorbehalt: 2026-Flip, CI grenzt an 0)

Pro Kandidat
  registered   eingefrorenes V7.27-IS-Resultat (Richtung + Effektgroesse).
  in_sample    Metrik auf Trades VOR Cutoff (Kontext: wo die Edge gefunden wurde).
  oos_test     Metrik auf Trades AB Cutoff (das Forward-Proxy-Fenster).
  verdict      CONFIRMED | WEAKENED | REFUTED | INSUFFICIENT_DATA
               (Vergleich oos_test-Vorzeichen/Groesse vs. registrierte Richtung,
                gegen vor-registrierte Gates).

Numerische Metrik  : Spearman-rank_ic Feature <-> return_pct je Subset.
Kategoriale Metrik : gerichteter avg_return_pct-Spread (bevorzugte minus
                     gemiedene Zelle) + Expectancy (avg v61_netto_pnl) je Zelle.

Maschinen-lesbares Verdikt
  oos_proxy_holds          TRUE nur wenn der staerkste Kandidat (atr_pct)
                           CONFIRMED ist UND KEIN Kandidat REFUTED ist
                           (bei ausreichender OOS-Datenmenge).
  ready_for_true_forward   = oos_proxy_holds (das OOS-Proxy ist das Screening-Gate
                           VOR der echten Live-Forward-Akkumulation).
  per_candidate            Verdikt-Liste rangsortiert.

EHRLICHKEITS-GRENZE (zentral): das hier ist ein OOS-Split auf HISTORISCHEN Daten
(`scope = OOS_PROXY_HISTORICAL`), KEIN echter post-heute-Forward-Test
(`is_true_forward_test = False`). Ein echter Forward-Test braucht frische Trades
NACH heute. Der Abschnitt `live_forward` ist das Geruest dafuer: er konsumiert
das V7.30-Ledger und zaehlt feature-nutzbare (forward_eligible) Live-Trades nach
Cutoff (V7.31: Gate auf feature-nutzbar, bitgenau == cohort_tag_ledger.
forward_ready; Ready-Gate 40). Solange
`live_forward_ready = False`, ist das OOS-Proxy die beste verfuegbare Evidenz:
mehr Gewicht als IN-SAMPLE-robust (V7.27), weniger als echter Forward-Test.

WICHTIG (eiserne Regel CLAUDE.md §3/§8): NUR Analyse. Aendert KEINE Live-Regel,
KEINE Schwelle, KEIN Sizing. Size/Hebel NIE vor H2. Output ist H2-Panel-Input.

V7.17-Pattern: EDEADLK-resilienter Reader (geteilt mit V7.26/V7.27).

CLI:
    python3 tools/entry_filter_forward_test.py
    python3 tools/entry_filter_forward_test.py --cutoff 2025-01-01
    python3 tools/entry_filter_forward_test.py --date 2026-06-23 --write
    python3 tools/entry_filter_forward_test.py --selftest

Exit-Codes:
    0  OK / Selftest pass
    2  Quelle nicht lesbar (EDEADLK persistent / Datei fehlt / keine aktiven Trades)
    3  Selftest fail

Output JSON : tools/entry_filter_forward_test_<date>.json
Output MD   : Briefings/Entry-Filter-Forward-Test-<date>.md

"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Optional

# Geteilte Primitive aus V7.26: Single-Source-of-Truth fuer Reader, Skip-Filter,
# Spearman und Gruppen-Statistik (Konsistenz statt Copy-Paste, identisch zu V7.27).
from entry_filter_orthogonality import (  # noqa: E402  (gleiches tools/-Verzeichnis)
    _f,
    _spearman,
    _group_stats,
    load_trades,
    read_text_resilient,
)

# --------------------------------------------------------------------------- #
# Konfiguration (vor-registriert / eingefroren)
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CSV = _ROOT / "v6_backtest_results_v61.csv"

# Vor-registrierter OOS-Cutoff (identisch zur Konvention in exit_forward_test.py).
_OOS_CUTOFF = "2025-01-01"
# Garantiert nicht existenter Pfad fuer hermetische Selftests (no-ledger-Zweig,
# damit der Selftest NICHT vom realen signals/entry_filter_forward_ledger.json
# abhaengt, das der V7.30-Producer jetzt schreibt).
_SELFTEST_NO_LEDGER = Path("/nonexistent/__entry_fwd_no_ledger__.json")

# Vor-registrierte Gates (eingefroren, NICHT aus den OOS-Daten getuned).
_IC_CONFIRM = 0.10        # |oos rank_ic| >= das + richtige Richtung -> CONFIRMED (= V7.25/26-Gate)
_SPREAD_CONFIRM = 1.5     # avg_return_pct-Spread (pp) fuer kategoriale Bestaetigung (= V7.26-Gate)
_MIN_TEST_N = 40          # Mindest-OOS-Trades fuer eine belastbare numerische Aussage
_MIN_CELL_TEST = 8        # Mindest-OOS-Trades je kategorialer Zelle

# Die DREI von V7.27 als robust ausgewiesenen Kandidaten als VOR-REGISTRIERTE
# Hypothesen (Werte eingefroren aus entry_filter_robustness_2026-06-19.json).
REGISTERED_CANDIDATES = [
    {
        "name": "atr_pct",
        "kind": "numeric",
        "direction": "high_better",
        "is_rank_ic": 0.1969,
        "is_p_adj": 0.007412,
        "rank": 1,
        "strength": "staerkster",
        "caveat": None,
    },
    {
        "name": "regime",
        "kind": "categorical",
        "preferred": "Trending-Divergent",
        "avoided": "Bullish-Confirmed",
        "is_spread_pp": 2.60,
        "rank": 2,
        "strength": "robust (cross-validiert Skip-Cohort)",
        "caveat": None,
    },
    {
        "name": "rsi14",
        "kind": "numeric",
        "direction": "low_better",
        "is_rank_ic": -0.1317,
        "is_p_adj": 0.086057,
        "rank": 3,
        "strength": "schwaechster",
        "caveat": "2026-Sign-Flip in V7.27, Bootstrap-CI-Obergrenze grenzt an 0",
    },
]

# --------------------------------------------------------------------------- #
# V7.31 Live-Forward-Readiness-Semantik (Consumer == Producer)
# --------------------------------------------------------------------------- #
# Lead-Kandidat = der staerkste (rank 1) registrierte Kandidat. Der numerische
# OOS-/Live-Forward-Test ist atr_pct-gegated (oos_proxy_holds setzt atr_pct
# CONFIRMED voraus), deshalb ist die atr_pct-spezifische usable_n die bindende
# Fortschrittsgroesse: auch wenn der permissive forward_eligible-OR frueher das
# Gate erreicht.
_LEAD_CANDIDATE = next((c["name"] for c in REGISTERED_CANDIDATES
                        if c.get("rank") == 1), "atr_pct")
# Regime-Zellen, die die V7.26/27-Edge tragen: aus dem registrierten kategorialen
# Kandidaten ABGELEITET (kein dupliziertes Magic; bleibt synchron zu
# REGISTERED_CANDIDATES und zu cohort_tag_ledger._CANDIDATE_REGIME_CELLS).
_CANDIDATE_REGIME_CELLS = tuple(
    v for c in REGISTERED_CANDIDATES if c["kind"] == "categorical"
    for v in (c.get("preferred"), c.get("avoided")) if v
)


def _parse_iso(d) -> Optional[_date]:
    """ISO-Datum (YYYY-MM-DD) tolerant parsen; None bei Unparsebarkeit."""
    try:
        return _date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return None


def _candidate_usable_rows(post: list[dict], cand: dict) -> list[dict]:
    """Die post-cutoff Ledger-Zeilen, die einen NUTZBAREN Wert fuer GENAU diesen
    Kandidaten + einen Return tragen (Single-Source fuer Zaehlung UND ETA-Daten).

    Numerisch (atr_pct/rsi14): Feature non-null + return_pct non-null.
    Kategorial (regime):       nur die edge-tragenden Zellen
    (Trending-Divergent/Bullish-Confirmed) + return_pct, NICHT das grobe
    'Trending' aus dem Journal (REGIME-TAG-GRANULARITY-1).
    """
    name = cand["name"]
    if cand["kind"] == "numeric":
        return [t for t in post
                if t.get(name) is not None and t.get("return_pct") is not None]
    return [t for t in post
            if t.get(name) in _CANDIDATE_REGIME_CELLS
            and t.get("return_pct") is not None]


def _eta_stub(status: str, current_n: int, min_n: int) -> dict:
    """Einheitliche ETA-Form fuer Faelle ohne schaetzbare Kadenz."""
    return {
        "status": status,
        "current_n": current_n,
        "min_n": min_n,
        "remaining_n": max(0, min_n - current_n),
        "first_usable": None,
        "last_usable": None,
        "span_days": None,
        "avg_gap_days": None,
        "cadence_per_year": (0.0 if status == "NOT_ACCUMULATING" else None),
        "eta_days": (0 if status == "GATE_MET" else None),
        "eta_date": None,
        "eta_years": (0.0 if status == "GATE_MET" else None),
    }


def project_eta(usable_rows: list[dict], current_n: int, min_n: int,
                as_of: str) -> dict:
    """Kadenz-Extrapolation: bei der beobachteten Inter-Arrival-Zeit der bisher
    nutzbaren Trades: wie viele Tage / welches Kalenderdatum bis zum Gate min_n?

    Reine lineare Fortschreibung (KEINE Prognose-Garantie): avg_gap = Spanne der
    nutzbaren Trades / (n-1); eta_days = (min_n - n) * avg_gap; eta_date = as_of +
    eta_days. Statuts:
        GATE_MET            current_n >= min_n (nichts mehr zu projizieren)
        NOT_ACCUMULATING    current_n == 0 (keine nutzbaren Trades; regime ->
                            REGIME-TAG-GRANULARITY-1)
        INSUFFICIENT_HISTORY current_n == 1 oder Spanne <= 0 (Kadenz nicht schaetzbar)
        REACHABLE_NEAR|MID|DISTANT  < 0,5 J | < 2 J | >= 2 J
    read-only; keinerlei Einfluss auf Live-Regeln/Sizing.
    """
    if current_n >= min_n:
        return _eta_stub("GATE_MET", current_n, min_n)
    if current_n == 0:
        return _eta_stub("NOT_ACCUMULATING", current_n, min_n)
    dates = sorted(d for d in (_parse_iso(r.get("date")) for r in usable_rows) if d)
    if len(dates) < 2:
        return _eta_stub("INSUFFICIENT_HISTORY", current_n, min_n)
    span_days = (dates[-1] - dates[0]).days
    if span_days <= 0:
        return _eta_stub("INSUFFICIENT_HISTORY", current_n, min_n)
    intervals = len(dates) - 1
    avg_gap = span_days / intervals
    remaining = min_n - current_n
    eta_days = int(round(remaining * avg_gap))
    as_of_d = _parse_iso(as_of) or _date.today()
    eta_years = round(eta_days / 365.25, 2)
    status = ("REACHABLE_NEAR" if eta_years < 0.5
              else "REACHABLE_MID" if eta_years < 2.0
              else "REACHABLE_DISTANT")
    return {
        "status": status,
        "current_n": current_n,
        "min_n": min_n,
        "remaining_n": remaining,
        "first_usable": dates[0].isoformat(),
        "last_usable": dates[-1].isoformat(),
        "span_days": span_days,
        "avg_gap_days": round(avg_gap, 1),
        "cadence_per_year": round(365.25 / avg_gap, 2),
        "eta_days": eta_days,
        "eta_date": (as_of_d + timedelta(days=eta_days)).isoformat(),
        "eta_years": eta_years,
    }


def per_candidate_usable(post: list[dict], min_n: int = _MIN_TEST_N,
                         as_of: Optional[str] = None) -> list[dict]:
    """Pro registriertem Kandidat: wie viele post-cutoff Ledger-Zeilen tragen
    einen NUTZBAREN Wert fuer GENAU dieses Feature + einen Return, plus die
    edge-spezifische ETA bis zum Gate (V7.32).

    Schaerfer als der permissive `forward_eligible`-OR des V7.30-Producers (der
    bereits TRUE ist, wenn IRGENDEIN Kandidat nutzbar ist): hier edge-spezifisch,
    weil der numerische Forward-Test atr_pct bzw. rsi14 konkret braucht und der
    kategoriale die feinen Regime-Zellen (Trending-Divergent/Bullish-Confirmed),
    nicht das grobe 'Trending' aus dem Journal. read-only.
    """
    as_of = as_of or _date.today().isoformat()
    out = []
    for c in REGISTERED_CANDIDATES:
        rows = _candidate_usable_rows(post, c)
        usable = len(rows)
        out.append({
            "name": c["name"],
            "kind": c["kind"],
            "rank": c["rank"],
            "usable_n": usable,
            "ready": usable >= min_n,
            "eta": project_eta(rows, usable, min_n, as_of),
        })
    out.sort(key=lambda x: x["rank"])
    return out


# --------------------------------------------------------------------------- #
# OOS-Split
# --------------------------------------------------------------------------- #
def split_oos(active: list[dict], cutoff: str) -> tuple[list[dict], list[dict]]:
    """Teilt die aktiven Trades in In-Sample (date < cutoff) und OOS (date >= cutoff).

    Trades ohne parsebares Datum fallen aus beiden Mengen (konservativ).
    """
    train, test = [], []
    for r in active:
        d = str(r.get("date", "")).strip()
        if len(d) < 10:
            continue
        (test if d >= cutoff else train).append(r)
    return train, test


# --------------------------------------------------------------------------- #
# Metriken pro Subset
# --------------------------------------------------------------------------- #
def _numeric_ic(rows: list[dict], feature: str) -> tuple[Optional[float], int]:
    """Spearman rank_ic Feature <-> return_pct ueber Trades mit beidem vorhanden."""
    pairs = []
    for r in rows:
        fv = _f(r.get(feature))
        rv = _f(r.get("return_pct"))
        if fv is not None and rv is not None:
            pairs.append((fv, rv))
    if len(pairs) < 3:
        return None, len(pairs)
    ic = _spearman([a for a, _ in pairs], [b for _, b in pairs])
    return (None if ic is None else round(ic, 4)), len(pairs)


def _regime_directional(rows: list[dict], preferred: str, avoided: str) -> dict:
    """Gerichtete Trennung preferred vs avoided: avg_return_pct-Spread + Expectancy."""
    pref_rows = [r for r in rows if str(r.get("regime", "")).strip() == preferred]
    avoid_rows = [r for r in rows if str(r.get("regime", "")).strip() == avoided]
    ps = _group_stats(pref_rows)
    as_ = _group_stats(avoid_rows)
    spread = None
    if ps["avg_return_pct"] is not None and as_["avg_return_pct"] is not None:
        spread = round(ps["avg_return_pct"] - as_["avg_return_pct"], 4)
    return {
        "preferred": preferred,
        "avoided": avoided,
        "preferred_stats": ps,
        "avoided_stats": as_,
        "spread_pp": spread,
        "n_preferred": ps["n"],
        "n_avoided": as_["n"],
    }


# --------------------------------------------------------------------------- #
# Verdikt pro Kandidat
# --------------------------------------------------------------------------- #
def _verdict_numeric(direction: str, oos_ic: Optional[float], n: int,
                     min_test_n: int) -> str:
    if oos_ic is None or n < min_test_n:
        return "INSUFFICIENT_DATA"
    reg_sign = 1.0 if direction == "high_better" else -1.0
    if abs(oos_ic) < _IC_CONFIRM:
        return "WEAKENED"                      # Richtung egal: OOS-Signal zu schwach
    return "CONFIRMED" if (oos_ic > 0) == (reg_sign > 0) else "REFUTED"


def _verdict_categorical(direc: dict, min_cell_n: int) -> str:
    if (direc["n_preferred"] < min_cell_n or direc["n_avoided"] < min_cell_n
            or direc["spread_pp"] is None):
        return "INSUFFICIENT_DATA"
    if direc["spread_pp"] >= _SPREAD_CONFIRM:
        return "CONFIRMED"                     # bevorzugte Zelle klar > gemiedene
    if direc["spread_pp"] > 0:
        return "WEAKENED"                      # richtige Ordnung, aber Spread unter Gate
    return "REFUTED"                           # gemiedene >= bevorzugte (Vorzeichen kippt)


def evaluate_candidate(cand: dict, train: list[dict], test: list[dict],
                       min_test_n: int, min_cell_n: int) -> dict:
    out = {
        "name": cand["name"],
        "kind": cand["kind"],
        "rank": cand["rank"],
        "strength": cand["strength"],
        "caveat": cand.get("caveat"),
        "registered": {},
        "in_sample": {},
        "oos_test": {},
        "verdict": None,
    }
    if cand["kind"] == "numeric":
        out["registered"] = {"direction": cand["direction"], "rank_ic": cand["is_rank_ic"],
                             "p_adj": cand["is_p_adj"]}
        is_ic, is_n = _numeric_ic(train, cand["name"])
        oos_ic, oos_n = _numeric_ic(test, cand["name"])
        out["in_sample"] = {"rank_ic": is_ic, "n": is_n}
        out["oos_test"] = {"rank_ic": oos_ic, "n": oos_n}
        if oos_ic is not None and is_ic is not None:
            out["oos_test"]["delta_vs_in_sample"] = round(oos_ic - is_ic, 4)
            out["oos_test"]["delta_vs_registered"] = round(oos_ic - cand["is_rank_ic"], 4)
        out["verdict"] = _verdict_numeric(cand["direction"], oos_ic, oos_n, min_test_n)
    else:  # categorical (regime)
        out["registered"] = {"preferred": cand["preferred"], "avoided": cand["avoided"],
                             "spread_pp": cand["is_spread_pp"]}
        out["in_sample"] = _regime_directional(train, cand["preferred"], cand["avoided"])
        oos = _regime_directional(test, cand["preferred"], cand["avoided"])
        out["oos_test"] = oos
        out["verdict"] = _verdict_categorical(oos, min_cell_n)
    return out


# --------------------------------------------------------------------------- #
# Live-Forward-Geruest (echter post-heute-Forward-Test: heute noch leer)
# --------------------------------------------------------------------------- #
def assess_live_forward(cutoff: str, ledger_path: Optional[Path] = None,
                        min_n: int = _MIN_TEST_N,
                        as_of: Optional[str] = None) -> dict:
    """Konsumiert das V7.30-Ledger und beurteilt die Live-Forward-Readiness.

    Der V7.30-Producer `cohort_tag_ledger` schreibt
    `signals/entry_filter_forward_ledger.json` und taggt jeden Live-Trade, der an
    einem Tag MIT scan_<date>.json eingegangen ist (atr_pct/regime/rsi14 + Return).
    Diese Funktion liest das Ledger fail-soft (fehlt/EDEADLK/unparsebar -> not-ready
    mit Grund: historischer COHORT-TAGGING-BACKLOG, Sandbox Errno 35 EDEADLK).

    V7.31-Semantik (Consumer == Producer): `live_forward_ready` gatet auf die Zahl
    FEATURE-NUTZBARER (forward_eligible) Zeilen: IDENTISCH zu
    `cohort_tag_ledger.forward_ready`, und NICHT mehr auf die rohe post-cutoff
    Zeilenzahl (das ueberzaehlte: 'erfasst' != 'feature-komplett'). Beide Zahlen
    werden transparent ausgewiesen:
      live_forward_n          = erfasst (alle getaggten post-cutoff Trades)
      live_forward_eligible_n = feature-nutzbar (Gating-Numerator, == Producer)
    Plus `per_candidate` (edge-spezifische usable_n) + Lead-Kandidat-Readiness.
    read-only; KEINE Live-Regel/Schwelle/Sizing.
    """
    as_of = as_of or _date.today().isoformat()
    ledger = ledger_path if ledger_path is not None else (
        _ROOT / "signals" / "entry_filter_forward_ledger.json")
    try:
        rel = str(ledger.relative_to(_ROOT))
    except ValueError:
        rel = str(ledger)

    def _not_ready(reason: str) -> dict:
        return {
            "live_forward_ready": False,
            "ledger_path": rel,
            "live_forward_n": 0,
            "live_forward_eligible_n": 0,
            "lead_candidate": _LEAD_CANDIDATE,
            "lead_ready": False,
            "per_candidate": [],
            "min_n": min_n,
            "as_of": as_of,
            "eta": _eta_stub("NO_LEDGER", 0, min_n),
            "lead_eta": _eta_stub("NO_LEDGER", 0, min_n),
            "reason": reason,
        }

    txt = read_text_resilient(ledger)
    if txt is None:
        return _not_ready(
            "kein entry_filter_forward_ledger.json: Live-Trades sind im Journal "
            "ohne atr_pct/regime/rsi14-Entry-Tags (COHORT-TAGGING-BACKLOG); echter "
            "Forward-Test akkumuliert erst nach Tagging frischer Live-Trades NACH "
            "heute.")
    try:
        data = json.loads(txt)
    except (ValueError, AttributeError) as e:  # noqa: BLE001
        return _not_ready(f"ledger unparsebar: {e}")

    rows = data if isinstance(data, list) else data.get("trades", [])
    post = [t for t in rows if str(t.get("date", "")) >= cutoff]
    eligible_rows = [t for t in post if bool(t.get("forward_eligible"))]
    eligible_n = len(eligible_rows)
    per_cand = per_candidate_usable(post, min_n, as_of=as_of)
    lead = next((c for c in per_cand if c["name"] == _LEAD_CANDIDATE), None)
    lead_ready = bool(lead and lead["usable_n"] >= min_n)
    pc_txt = " · ".join(f"{c['name']} {c['usable_n']}" for c in per_cand)
    # V7.32: ETA bis Gate fuer das feature-nutzbare Gate (== Producer.forward_ready)
    # und fuer den bindenden Lead-Kandidaten (atr_pct).
    eta = project_eta(eligible_rows, eligible_n, min_n, as_of)
    lead_eta = lead["eta"] if lead else _eta_stub("NO_LEDGER", 0, min_n)
    return {
        # FIX V7.31: Gate auf feature-nutzbare (== Producer.forward_ready), nicht roh.
        "live_forward_ready": eligible_n >= min_n,
        "ledger_path": rel,
        "live_forward_n": len(post),
        "live_forward_eligible_n": eligible_n,
        "lead_candidate": _LEAD_CANDIDATE,
        "lead_ready": lead_ready,
        "per_candidate": per_cand,
        "min_n": min_n,
        "as_of": as_of,
        "eta": eta,
        "lead_eta": lead_eta,
        "reason": (f"{len(post)} erfasste / {eligible_n} feature-nutzbare getaggte "
                   f"Live-Trades >= {cutoff}; ready-Gate {min_n} auf feature-nutzbare "
                   f"(konsistent mit cohort_tag_ledger.forward_ready). "
                   f"Pro Kandidat nutzbar: {pc_txt}. "
                   f"ETA-Gate: {eta['status']}"
                   + (f" (~{eta['eta_date']}, {eta['eta_years']} J bei Kadenz "
                      f"{eta['cadence_per_year']}/J)" if eta.get('eta_date') else "")
                   + "."),
    }


# --------------------------------------------------------------------------- #
# Aggregation + Verdikt
# --------------------------------------------------------------------------- #
def decide(per_candidate: list[dict]) -> dict:
    by = {c["name"]: c for c in per_candidate}
    verdicts = {c["name"]: c["verdict"] for c in per_candidate}
    n_confirmed = sum(1 for v in verdicts.values() if v == "CONFIRMED")
    n_refuted = sum(1 for v in verdicts.values() if v == "REFUTED")
    n_weakened = sum(1 for v in verdicts.values() if v == "WEAKENED")
    n_insuff = sum(1 for v in verdicts.values() if v == "INSUFFICIENT_DATA")
    atr = by.get("atr_pct", {}).get("verdict")
    # OOS-Proxy haelt nur, wenn der staerkste Kandidat (atr_pct) OOS bestaetigt
    # ist UND KEIN Kandidat OOS klar widerlegt wurde.
    oos_proxy_holds = (atr == "CONFIRMED") and (n_refuted == 0)
    return {
        "scope": "OOS_PROXY_HISTORICAL",
        "is_true_forward_test": False,
        "oos_proxy_holds": bool(oos_proxy_holds),
        "ready_for_true_forward": bool(oos_proxy_holds),
        "strongest_candidate": "atr_pct",
        "strongest_verdict": atr,
        "n_confirmed": n_confirmed,
        "n_weakened": n_weakened,
        "n_refuted": n_refuted,
        "n_insufficient": n_insuff,
        "verdicts": verdicts,
        "interpretation": _interpret(oos_proxy_holds, atr, n_refuted, n_insuff),
        "next_step": ("atr_pct zuerst in den echten Live-Forward-Test (Entry-Tagging "
                      "frischer Trades); Feature-Freeze bis Live-Forward ready. "
                      "KEINE Live-Regel-/Sizing-Aenderung aus diesem OOS-Proxy."),
    }


def _interpret(holds: bool, atr: Optional[str], n_refuted: int, n_insuff: int) -> str:
    if holds:
        return ("OOS-Proxy HAELT: der staerkste V7.27-Kandidat (atr_pct) bestaetigt "
                "sich out-of-sample und kein Kandidat ist widerlegt. Das ist die "
                "staerkste verfuegbare Evidenz UNTERHALB eines echten Forward-Tests "
                "qualifiziert atr_pct fuer die Live-Forward-Akkumulation.")
    if atr == "REFUTED":
        return ("OOS-Proxy WIDERLEGT den staerksten Kandidaten (atr_pct) out-of-sample "
                "die V7.27-Edge war wahrscheinlich In-Sample-Artefakt. Forward-Test "
                "zuruekstellen, KEINE Regel darauf bauen.")
    if atr == "INSUFFICIENT_DATA" or n_insuff >= 2:
        return ("OOS-Proxy UNENTSCHIEDEN: zu wenig Out-of-Sample-Daten fuer ein "
                "belastbares Urteil. Mehr (Forward-)Daten noetig.")
    return ("OOS-Proxy GESCHWAECHT: der staerkste Kandidat bestaetigt sich nicht klar "
            "out-of-sample (WEAKENED). Edge eher fragil: vorsichtig weiter beobachten.")


# --------------------------------------------------------------------------- #
# Report-Bau
# --------------------------------------------------------------------------- #
def build_report(csv_path: Path, cutoff: str, run_date: str,
                 candidates: Optional[list[dict]] = None,
                 min_test_n: int = _MIN_TEST_N,
                 min_cell_n: int = _MIN_CELL_TEST) -> dict:
    txt = read_text_resilient(csv_path)
    if txt is None:
        return {"error": f"CSV nicht lesbar (EDEADLK/fehlt): {csv_path}"}
    active = load_trades(txt)
    if not active:
        return {"error": "keine aktiven V6.1-Trades nach v61_skip-Filter"}
    cands = candidates if candidates is not None else REGISTERED_CANDIDATES
    train, test = split_oos(active, cutoff)
    per = [evaluate_candidate(c, train, test, min_test_n, min_cell_n) for c in cands]
    per.sort(key=lambda c: c["rank"])
    verdict = decide(per)
    return {
        "tool": "entry_filter_forward_test",
        "version": "V7.32",
        "date": run_date,
        "source_csv": Path(csv_path).name,
        "oos_cutoff": cutoff,
        "n_active": len(active),
        "n_in_sample": len(train),
        "n_oos_test": len(test),
        "params": {
            "ic_confirm_gate": _IC_CONFIRM,
            "spread_confirm_gate_pp": _SPREAD_CONFIRM,
            "min_test_n": min_test_n,
            "min_cell_test_n": min_cell_n,
        },
        "registered_source": "entry_filter_robustness_2026-06-19.json (V7.27)",
        "per_candidate": per,
        "live_forward": assess_live_forward(cutoff, as_of=run_date),
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Markdown-Render (deutsch)
# --------------------------------------------------------------------------- #
def _fmt(x) -> str:
    return "-" if x is None else (f"{x:+.4f}" if isinstance(x, float) else str(x))


def _fmt_eta(eta: dict) -> str:
    """Kompakte deutsche ETA-Zeile aus einem project_eta()-Dict."""
    status = eta.get("status", "-")
    n, m = eta.get("current_n"), eta.get("min_n")
    head = f"**{status}** ({n}/{m})"
    if eta.get("eta_date"):
        return (f"{head} → ~**{eta['eta_date']}** "
                f"(~{eta.get('eta_years')} J · noch {eta.get('remaining_n')} Trades · "
                f"Kadenz {eta.get('cadence_per_year')}/Jahr · "
                f"ø Abstand {eta.get('avg_gap_days')} d)")
    if status == "NOT_ACCUMULATING":
        return f"{head} → 0 nutzbare Trades, akkumuliert NICHT (Kadenz 0/Jahr)"
    if status == "INSUFFICIENT_HISTORY":
        return f"{head} → Kadenz nicht schaetzbar (< 2 nutzbare Trades)"
    if status == "GATE_MET":
        return f"{head} → Gate erreicht"
    return head


def render_md(rep: dict) -> str:
    v = rep["verdict"]
    L = []
    L.append(f"# Entry-Filter Forward-Test (OOS-Proxy): {rep.get('date')}")
    L.append("")
    L.append(f"**Tool** `entry_filter_forward_test.py` (V7.32) · read-only · "
             f"Quelle `{rep.get('source_csv')}` · OOS-Cutoff **{rep.get('oos_cutoff')}**")
    L.append("")
    L.append(f"- Aktive V6.1-Trades: **{rep.get('n_active')}** "
             f"(In-Sample < Cutoff: {rep.get('n_in_sample')} · "
             f"OOS-Test ≥ Cutoff: {rep.get('n_oos_test')})")
    L.append(f"- Vor-registrierte Kandidaten aus: `{rep.get('registered_source')}`")
    L.append("")
    L.append("## Verdikt")
    L.append("")
    L.append(f"- **oos_proxy_holds: {v['oos_proxy_holds']}** · "
             f"staerkster Kandidat (atr_pct) = **{v['strongest_verdict']}**")
    L.append(f"- CONFIRMED {v['n_confirmed']} · WEAKENED {v['n_weakened']} · "
             f"REFUTED {v['n_refuted']} · INSUFFICIENT {v['n_insufficient']}")
    L.append(f"- Scope: `{v['scope']}` · is_true_forward_test: "
             f"**{v['is_true_forward_test']}**")
    L.append("")
    L.append(f"> {v['interpretation']}")
    L.append("")
    L.append("## Kandidaten (rangsortiert)")
    L.append("")
    L.append("| # | Feature | Registriert (IS) | In-Sample | OOS-Test | Verdikt |")
    L.append("|---|---|---|---|---|---|")
    for c in rep["per_candidate"]:
        if c["kind"] == "numeric":
            reg = f"{c['registered']['direction']} ic={c['registered']['rank_ic']:+.3f}"
            isv = f"ic={_fmt(c['in_sample'].get('rank_ic'))} (n={c['in_sample'].get('n')})"
            oos = f"ic={_fmt(c['oos_test'].get('rank_ic'))} (n={c['oos_test'].get('n')})"
        else:
            reg = f"{c['registered']['preferred']}≫{c['registered']['avoided']} " \
                  f"spread={c['registered']['spread_pp']:+.2f}pp"
            iss = c["in_sample"].get("spread_pp")
            oss = c["oos_test"].get("spread_pp")
            isv = (f"spread={_fmt(iss)}pp "
                   f"(n {c['in_sample'].get('n_preferred')}/{c['in_sample'].get('n_avoided')})")
            oos = (f"spread={_fmt(oss)}pp "
                   f"(n {c['oos_test'].get('n_preferred')}/{c['oos_test'].get('n_avoided')})")
        L.append(f"| {c['rank']} | {c['name']} ({c['strength']}) | {reg} | {isv} | "
                 f"{oos} | **{c['verdict']}** |")
    L.append("")
    lf = rep["live_forward"]
    L.append("## Live-Forward-Geruest (echter post-heute-Forward-Test)")
    L.append("")
    L.append(f"- **live_forward_ready: {lf['live_forward_ready']}** · "
             f"erfasst n={lf['live_forward_n']} · "
             f"feature-nutzbar n={lf.get('live_forward_eligible_n', 0)} · "
             f"ready-Gate {lf.get('min_n', _MIN_TEST_N)} (auf feature-nutzbar) · "
             f"Ledger `{lf['ledger_path']}`")
    _pc = lf.get("per_candidate") or []
    if _pc:
        L.append("- Pro Kandidat nutzbar (Feature + Return vorhanden): "
                 + " · ".join(f"{c['name']} {c['usable_n']}"
                              + (" ✓ready" if c['ready'] else "")
                              for c in _pc)
                 + f" · Lead **{lf.get('lead_candidate')}** "
                 f"ready={lf.get('lead_ready')}")
    # V7.32: ETA-Block bis Gate (feature-nutzbar + Lead + pro Kandidat)
    eta = lf.get("eta")
    if eta:
        L.append(f"- **ETA bis Gate (feature-nutzbar):** {_fmt_eta(eta)} "
                 f"_(as_of {lf.get('as_of', rep.get('date'))})_")
        L.append(f"- ETA Lead `{lf.get('lead_candidate')}`: "
                 f"{_fmt_eta(lf.get('lead_eta') or {})}")
        if _pc:
            L.append("- ETA pro Kandidat: "
                     + " · ".join(f"{c['name']} {c.get('eta', {}).get('status', '-')}"
                                  + (f" {c['eta'].get('eta_date')}"
                                     if c.get('eta', {}).get('eta_date') else "")
                                  for c in _pc))
    L.append(f"- {lf['reason']}")
    L.append("")
    L.append("> ETA = reine Kadenz-Fortschreibung der Inter-Arrival-Zeit nutzbarer "
             "Live-Trades, KEINE Prognose-Garantie: erhoehte/gestoppte "
             "Handelsfrequenz verschiebt die ETA proportional. "
             "`NOT_ACCUMULATING` bei regime = REGIME-TAG-GRANULARITY-1.")
    L.append("")
    L.append(f"**Naechster Schritt:** {v['next_step']}")
    L.append("")
    L.append("> EHRLICHKEIT: OOS-Split auf HISTORISCHEN Daten, staerker als "
             "IN-SAMPLE-robust (V7.27), aber KEIN echter Forward-Test. KEINE "
             "Live-Regel/Schwelle/Sizing geaendert (CLAUDE.md §3/§8).")
    L.append("")
    L.append("_Read-only Diagnostik. Keine Order, keine Regel-Aenderung._")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def write_outputs(rep: dict, run_date: str) -> tuple[Path, Path]:
    json_path = _ROOT / "tools" / f"entry_filter_forward_test_{run_date}.json"
    md_dir = _ROOT / "Briefings"
    md_dir.mkdir(exist_ok=True)
    md_path = md_dir / f"Entry-Filter-Forward-Test-{run_date}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(rep), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
_SYN_HEADER = ("date,ticker,regime,sector,tier,year,v6_score,rsi2,rsi5,rsi14,triple_rsi,"
               "atr_pct,return_pct,is_win,v61_skip,v61_exit_reason,v61_netto_pnl")

# Synthetik-Kandidaten-Set (eigene Zellnamen, damit der Selftest hermetisch ist)
_SYN_CANDS = [
    {"name": "atr_pct", "kind": "numeric", "direction": "high_better",
     "is_rank_ic": 0.20, "is_p_adj": 0.01, "rank": 1, "strength": "syn", "caveat": None},
    {"name": "regime", "kind": "categorical", "preferred": "GoodReg", "avoided": "BadReg",
     "is_spread_pp": 3.0, "rank": 2, "strength": "syn", "caveat": None},
    {"name": "rsi14", "kind": "numeric", "direction": "low_better",
     "is_rank_ic": -0.18, "is_p_adj": 0.05, "rank": 3, "strength": "syn", "caveat": None},
]


def _syn_rows(atr_post_sign: float, n_per_side: int = 60) -> str:
    """Synthetik-Panel mit kontrollierter OOS-Struktur.

    In-Sample (date 2024-..) und OOS (date 2025-..) je n_per_side Trades.
    atr_pct, rsi14 und regime werden UNABHAENGIG gezogen (sonst sind die
    marginalen rank_ic kollinear): jedes Feature traegt sein eigenes Signal:
    - atr_pct: high_better in-sample IMMER; OOS-Vorzeichen = atr_post_sign
      (+1 -> CONFIRMED, -1 -> REFUTED).
    - rsi14: low_better in beiden Fenstern (-> CONFIRMED).
    - regime: GoodReg >> BadReg in beiden Fenstern (-> CONFIRMED).
    Eine v61_skip=True-Zeile muss rausfallen.
    """
    import random as _r
    rng = _r.Random(3)
    rows = [_SYN_HEADER]
    for window, dt in (("is", "2024-03-15"), ("oos", "2025-03-15")):
        sign = 1.0 if window == "is" else atr_post_sign
        for k in range(n_per_side):
            atr = round(rng.uniform(1.0, 6.0), 3)        # unabhaengig
            rsi14 = round(rng.uniform(1.0, 91.0), 2)     # unabhaengig
            regime = "GoodReg" if k % 2 == 0 else "BadReg"
            reg_eff = 1.2 if regime == "GoodReg" else -1.2
            # atr high_better (Vorzeichen je Fenster, dominanter Treiber) +
            # rsi14 low_better (immer) + regime
            ret = round(sign * (atr - 3.5) * 1.6
                        + (46.0 - rsi14) * 0.06
                        + reg_eff
                        + rng.uniform(-0.05, 0.05), 3)
            netto = round(ret * 10.0, 2)
            win = "True" if ret > 0 else "False"
            rows.append(f"{dt},T{window}{k},{regime},SecX,A,{dt[:4]},6.5,30,45,"
                        f"{rsi14},6,{atr},{ret},{win},False,connors_sma5,{netto}")
    rows.append(f"2025-03-15,SKIP,GoodReg,SecX,A,2025,6.5,1,30,99,6,5.0,99.0,True,True,connors_sma5,990.0")
    return "\n".join(rows) + "\n"


def selftest() -> int:
    fails: list[str] = []

    # 1) OOS-Split korrekt + skip-Filter
    act = load_trades(_syn_rows(+1.0))
    if len(act) != 120:
        fails.append(f"skip-filter: expected 120 active, got {len(act)}")
    train, test = split_oos(act, "2025-01-01")
    if len(train) != 60 or len(test) != 60:
        fails.append(f"split: expected 60/60, got {len(train)}/{len(test)}")

    # 2) HOLDS-Pfad: atr_pct OOS positiv -> CONFIRMED; alle CONFIRMED -> oos_proxy_holds
    rep = _build_from_text(_syn_rows(+1.0), "2025-01-01", "selftest", min_test_n=30)
    vmap = rep["verdict"]["verdicts"]
    if vmap.get("atr_pct") != "CONFIRMED":
        fails.append(f"HOLDS: atr_pct expected CONFIRMED, got {vmap.get('atr_pct')}")
    if vmap.get("rsi14") != "CONFIRMED":
        fails.append(f"HOLDS: rsi14 expected CONFIRMED, got {vmap.get('rsi14')}")
    if vmap.get("regime") != "CONFIRMED":
        fails.append(f"HOLDS: regime expected CONFIRMED, got {vmap.get('regime')}")
    if not rep["verdict"]["oos_proxy_holds"]:
        fails.append("HOLDS: oos_proxy_holds should be True")

    # 3) REFUTED-Pfad: atr_pct OOS-Vorzeichen kippt -> REFUTED -> proxy haelt NICHT
    rep_r = _build_from_text(_syn_rows(-1.0), "2025-01-01", "selftest", min_test_n=30)
    if rep_r["verdict"]["verdicts"].get("atr_pct") != "REFUTED":
        fails.append(f"REFUTED: atr_pct expected REFUTED, got "
                     f"{rep_r['verdict']['verdicts'].get('atr_pct')}")
    if rep_r["verdict"]["oos_proxy_holds"]:
        fails.append("REFUTED: oos_proxy_holds should be False")

    # 4) INSUFFICIENT-Pfad: min_test_n hoeher als OOS-n -> atr_pct INSUFFICIENT
    rep_i = _build_from_text(_syn_rows(+1.0), "2025-01-01", "selftest", min_test_n=999)
    if rep_i["verdict"]["verdicts"].get("atr_pct") != "INSUFFICIENT_DATA":
        fails.append("INSUFFICIENT: atr_pct expected INSUFFICIENT_DATA with min_test_n=999")
    if rep_i["verdict"]["oos_proxy_holds"]:
        fails.append("INSUFFICIENT: oos_proxy_holds should be False")

    # 5) live_forward fail-soft (kein Ledger -> not ready, n=0)
    if rep["live_forward"]["live_forward_ready"] or rep["live_forward"]["live_forward_n"] != 0:
        fails.append("live_forward should be not-ready / n=0 without ledger")

    # 6) render_md crasht nicht + Kern-Sektionen
    try:
        md = render_md(rep)
        if "Verdikt" not in md or "Forward-Test" not in md:
            fails.append("render_md missing key sections")
    except Exception as e:  # noqa: BLE001
        fails.append(f"render_md crashed: {e}")

    # 7) categorical INSUFFICIENT bei zu kleiner Zelle
    if _verdict_categorical({"n_preferred": 3, "n_avoided": 30, "spread_pp": 5.0}, 8) \
            != "INSUFFICIENT_DATA":
        fails.append("categorical small cell should be INSUFFICIENT_DATA")
    # numeric WEAKENED bei schwachem IC
    if _verdict_numeric("high_better", 0.03, 100, 40) != "WEAKENED":
        fails.append("numeric weak IC should be WEAKENED")

    # 8) V7.31 Live-Forward eligible-Gating + per-Kandidat usable: das Gate MUSS
    #    auf feature-nutzbare (forward_eligible) zaehlen, NICHT auf rohe Zeilen.
    _tmp = Path(tempfile.gettempdir()) / "__entry_fwd_selftest_ledger__.json"
    try:
        _led = {"trades": [
            # post-cutoff, atr+rsi+return + edge-Regime-Zelle -> eligible + alle usable
            {"date": "2025-03-01", "atr_pct": 3.0, "rsi14": 40.0,
             "return_pct": 2.0, "regime": "Trending-Divergent",
             "forward_eligible": True},
            # post-cutoff, KEINE Features -> NICHT eligible (raw zaehlt, eligible nicht)
            {"date": "2025-03-02", "atr_pct": None, "rsi14": None,
             "return_pct": 1.0, "regime": "Trending", "forward_eligible": False},
            {"date": "2025-03-03", "atr_pct": None, "rsi14": None,
             "return_pct": 1.0, "regime": "Trending", "forward_eligible": False},
            # PRE-cutoff -> faellt komplett raus
            {"date": "2024-12-31", "atr_pct": 9.0, "rsi14": 10.0,
             "return_pct": 5.0, "regime": "Trending-Divergent",
             "forward_eligible": True},
        ]}
        _tmp.write_text(json.dumps(_led), encoding="utf-8")
        # min_n=2: raw post=3 >= 2, ABER eligible=1 < 2 -> ready MUSS False sein.
        lf2 = assess_live_forward("2025-01-01", ledger_path=_tmp, min_n=2)
        if lf2["live_forward_n"] != 3:
            fails.append(f"LF-V731: live_forward_n erwartet 3, got {lf2['live_forward_n']}")
        if lf2["live_forward_eligible_n"] != 1:
            fails.append(f"LF-V731: eligible_n erwartet 1, got {lf2['live_forward_eligible_n']}")
        if lf2["live_forward_ready"] is not False:
            fails.append("LF-V731: ready MUSS False sein (raw>=gate, aber eligible<gate)")
        _pcm = {c["name"]: c["usable_n"] for c in lf2["per_candidate"]}
        if _pcm.get("atr_pct") != 1 or _pcm.get("rsi14") != 1:
            fails.append(f"LF-V731: per-Kandidat atr/rsi usable erwartet 1/1, got {_pcm}")
        if _pcm.get("regime") != 1:
            fails.append("LF-V731: regime usable erwartet 1 (nur Trending-Divergent-Zelle), "
                         f"got {_pcm.get('regime')}")
        # min_n=1: eligible=1 >= 1 -> ready True (positiver Pfad).
        lf1 = assess_live_forward("2025-01-01", ledger_path=_tmp, min_n=1)
        if lf1["live_forward_ready"] is not True:
            fails.append("LF-V731: ready MUSS True sein bei eligible(1) >= min_n(1)")
    finally:
        try:
            _tmp.unlink()
        except OSError:
            pass

    # 9) V7.32 ETA-Projektion: GATE_MET / NOT_ACCUMULATING / INSUFFICIENT_HISTORY /
    #    REACHABLE (deterministische Kadenz-Fortschreibung) + ETA-Keys im
    #    assess_live_forward-Output.
    if project_eta([], 40, 40, "2026-06-26")["status"] != "GATE_MET":
        fails.append("ETA: current>=min_n sollte GATE_MET sein")
    if project_eta([], 0, 40, "2026-06-26")["status"] != "NOT_ACCUMULATING":
        fails.append("ETA: current==0 sollte NOT_ACCUMULATING sein")
    _one = [{"date": "2025-03-01"}]
    if project_eta(_one, 1, 40, "2026-06-26")["status"] != "INSUFFICIENT_HISTORY":
        fails.append("ETA: 1 nutzbarer Trade sollte INSUFFICIENT_HISTORY sein")
    # 2 nutzbare Trades, Spanne 10 d, min_n 4 -> avg_gap 10, remaining 2 -> eta 20 d
    _two = [{"date": "2025-01-01"}, {"date": "2025-01-11"}]
    _eta = project_eta(_two, 2, 4, "2025-01-11")
    if _eta["avg_gap_days"] != 10.0:
        fails.append(f"ETA: avg_gap erwartet 10.0, got {_eta['avg_gap_days']}")
    if _eta["eta_days"] != 20:
        fails.append(f"ETA: eta_days erwartet 20, got {_eta['eta_days']}")
    if _eta["eta_date"] != "2025-01-31":
        fails.append(f"ETA: eta_date erwartet 2025-01-31, got {_eta['eta_date']}")
    if _eta["status"] != "REACHABLE_NEAR":
        fails.append(f"ETA: status erwartet REACHABLE_NEAR, got {_eta['status']}")
    # DISTANT-Branch (38 Trades entfernt bei ~23 d Kadenz -> ~2,4 J)
    _dist = project_eta([{"date": "2025-01-01"}, {"date": "2025-01-24"}],
                        2, 40, "2026-06-26")
    if _dist["status"] != "REACHABLE_DISTANT":
        fails.append(f"ETA: DISTANT erwartet, got {_dist['status']}")
    # ETA-Keys im assess_live_forward-Output (eligible-Gate + Lead)
    _tmp2 = Path(tempfile.gettempdir()) / "__entry_fwd_selftest_eta__.json"
    try:
        _tmp2.write_text(json.dumps({"trades": [
            {"date": "2025-02-01", "atr_pct": 3.0, "rsi14": 40.0, "return_pct": 2.0,
             "regime": "Trending", "forward_eligible": True},
            {"date": "2025-03-01", "atr_pct": 4.0, "rsi14": 30.0, "return_pct": -1.0,
             "regime": "Trending", "forward_eligible": True},
        ]}), encoding="utf-8")
        _lf = assess_live_forward("2025-01-01", ledger_path=_tmp2, min_n=40,
                                  as_of="2026-06-26")
        if "eta" not in _lf or "lead_eta" not in _lf:
            fails.append("ETA: assess_live_forward fehlt eta/lead_eta")
        if _lf["eta"]["status"] not in ("REACHABLE_NEAR", "REACHABLE_MID",
                                        "REACHABLE_DISTANT"):
            fails.append(f"ETA: eligible-eta status unerwartet {_lf['eta']['status']}")
        # regime hat 0 nutzbare (beide Zeilen grob 'Trending', keine Edge-Zelle)
        # -> NOT_ACCUMULATING
        _reg = next(c for c in _lf["per_candidate"] if c["name"] == "regime")
        if _reg["eta"]["status"] != "NOT_ACCUMULATING":
            fails.append(f"ETA: regime sollte NOT_ACCUMULATING sein, got "
                         f"{_reg['eta']['status']}")
    finally:
        try:
            _tmp2.unlink()
        except OSError:
            pass

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  -", f)
        return 3
    print("SELFTEST PASS (9 Gruppen): split+skip, holds-confirmed, refuted, "
          "insufficient, live-forward-soft, render, verdict-edges, "
          "live-forward-eligible-gating (V7.31), live-forward-eta (V7.32)")
    return 0


def _build_from_text(csv_text: str, cutoff: str, run_date: str,
                     min_test_n: int = _MIN_TEST_N) -> dict:
    """Selftest-Helfer: build_report aus In-Memory-CSV mit Synthetik-Kandidaten."""
    active = load_trades(csv_text)
    train, test = split_oos(active, cutoff)
    per = [evaluate_candidate(c, train, test, min_test_n, _MIN_CELL_TEST)
           for c in _SYN_CANDS]
    per.sort(key=lambda c: c["rank"])
    return {
        "date": run_date, "source_csv": "synthetic", "oos_cutoff": cutoff,
        "n_active": len(active), "n_in_sample": len(train), "n_oos_test": len(test),
        "registered_source": "synthetic", "per_candidate": per,
        "live_forward": assess_live_forward(cutoff, ledger_path=_SELFTEST_NO_LEDGER),
        "verdict": decide(per),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="ENTRY-FILTER-FORWARD-TEST-1 (V7.29): OOS-Forward-Validierung "
                    "der V7.27-Robust-Kandidaten (read-only).")
    ap.add_argument("--csv", default=str(_DEFAULT_CSV),
                    help="Backtest-CSV (Default v6_backtest_results_v61.csv)")
    ap.add_argument("--cutoff", default=_OOS_CUTOFF,
                    help=f"Vor-registrierter OOS-Cutoff (Default {_OOS_CUTOFF})")
    ap.add_argument("--min-test-n", type=int, default=_MIN_TEST_N,
                    help="Mindest-OOS-Trades fuer numerisches Verdikt")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="JSON + MD schreiben")
    ap.add_argument("--selftest", action="store_true", help="Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rep = build_report(Path(args.csv), cutoff=args.cutoff, run_date=args.date,
                       min_test_n=args.min_test_n)
    if rep.get("error"):
        print(json.dumps(rep, ensure_ascii=False))
        return 2

    if args.write:
        jp, mp = write_outputs(rep, args.date)
        print(f"geschrieben: {jp}")
        print(f"geschrieben: {mp}")
    else:
        print(json.dumps(rep["verdict"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
