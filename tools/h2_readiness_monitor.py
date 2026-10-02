#!/usr/bin/env python3
"""
V7.34 H2-READINESS-MONITOR-1: read-only Observability.

PROBLEM (Warum dieses Tool existiert):
  Die H2-Diagnose-Phase ist inhaltlich abgeschlossen (Entry: V7.25 Score=GATE,
  V7.26 Orthogonalitaet, V7.27 Robustheit, V7.29 OOS-Forward, V7.30 Producer,
  V7.31 Readiness-Semantik, V7.33 Regime-Cell-Edge-Map; Exit: V7.21/23). Was
  zwischen HIER und der ersten Sizing-Aufweitung (H3/H4) steht, ist (a) genug
  FEATURE-NUTZBARE Live-Trades bis Gate 40 und (b) die Operator-Freigabe der
  Forward-Kandidaten-Aufweitung. Der Status DIESER zwei Dinge ist heute ueber
  drei Tools verstreut:
    - entry_filter_forward_test.assess_live_forward()  -> Entry-numeric + LEGACY
      binaerer regime-Kandidat (usable_n, ETA je Kandidat)
    - regime_cell_edge_map.live_unblock                -> ERWEITERTER regime-View
      (legacy_usable_n -> extended_usable_n ueber alle 7 Zellen)
  Niemand komponiert diese in EINE ehrliche Antwort auf die Frage, die die
  Weekly-Health-Checks + Eskalations-Logs seit Wochen wiederholen: "Wie weit ist
  H2 davon entfernt, H3/H4 zu entsperren, und was genau blockiert?".

LIEFERUNG (read-only):
  Eine konsolidierte Pane:
    1. Per-Kandidat-Readiness (atr_pct rank-1, rsi14) mit usable_n vs Gate 40 +
       kadenzbasierter ETA (Tage / Kalenderdatum / Jahre).
    2. Der regime-Kandidat unter ZWEI Views nebeneinander:
         LEGACY (binaer, 2 von 7 Zellen)  -> usable_n=0, NOT_ACCUMULATING
         EXTENDED (V7.33 edge_rank, 7/7)  -> extended_usable_n,
                                             STATUS = PENDING_APPROVAL (noch nicht
                                             in den Forward-Test verdrahtet).
    3. Ein Gesamt-Verdikt + der EINE limitierende Faktor (Kadenz vs Operator-
       Freigabe vs beides), plus die harte Kadenz-Realitaet (ETA bis Gate 40
       bei der beobachteten Trade-Kadenz).
    4. Read-only "Was-waere-wenn"-Hebel (rein illustrativ, KEINE Empfehlung):
       - Adoption des erweiterten regime-Views (bestbewerteter Kandidat 2->N)
       - Kadenz-Sensitivitaet (ETA bei 26/52/104 Trades/Jahr)

EISERNE REGEL: KEINE Live-Regel/Schwelle/Sizing geaendert. Dieses Tool liest nur
(assess_live_forward + regime_cell_edge_map-JSON), rechnet, und schreibt ein
Report-JSON + eine deutsche MD. Es verdrahtet NICHTS und aendert KEIN Gate. Die
erweiterte regime-Adoption bleibt Operator-Freigabe (H2-Feature-Freeze). Pure
Observability.

CLI:
  python3 tools/h2_readiness_monitor.py                 # JSON auf stdout
  python3 tools/h2_readiness_monitor.py --write         # + signals/ + Briefings/
  python3 tools/h2_readiness_monitor.py --selftest      # hermetisch
  python3 tools/h2_readiness_monitor.py --cutoff 2025-01-01 --min-n 40
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

_SIGNALS = _ROOT / "signals"
_BRIEFINGS = _ROOT / "Briefings"

_OOS_CUTOFF = "2025-01-01"
_MIN_N = 40  # == entry_filter_forward_test._MIN_TEST_N (Gate 40)

# Kadenzen fuer die Was-waere-wenn-Sensitivitaet (Trades/Jahr).
_CADENCE_SCENARIOS = (26, 52, 104)  # ~2/Monat, ~1/Woche, ~2/Woche

TOOL_VERSION = "V7.34"


# ---------------------------------------------------------------------------
# Reine Rechenkerne (hermetisch testbar, keine I/O)
# ---------------------------------------------------------------------------
def project_days_to_gate(remaining_n: int, cadence_per_year: Optional[float]) -> Optional[int]:
    """Kalendertage bis Gate bei gegebener Jahres-Kadenz. None wenn Kadenz<=0."""
    if remaining_n <= 0:
        return 0
    if not cadence_per_year or cadence_per_year <= 0:
        return None
    return int(round(remaining_n * (365.0 / cadence_per_year)))


def _best_single_view_candidate(per_candidate: list[dict]) -> Optional[dict]:
    """Der bereits am weitesten akkumulierte Kandidat unter dem GELIEFERTEN
    (single-view) assess_live_forward-Output: hoechstes usable_n, Tie-Break rank."""
    if not per_candidate:
        return None
    return max(
        per_candidate,
        key=lambda c: (int(c.get("usable_n") or 0), -int(c.get("rank") or 99)),
    )


def compose_h2_readiness(assess: dict, edge_unblock: Optional[dict], *,
                         as_of: str, min_n: int = _MIN_N) -> dict:
    """Komponiert die konsolidierte H2-Readiness-Pane aus:
      - assess: entry_filter_forward_test.assess_live_forward()-Output
      - edge_unblock: regime_cell_edge_map['live_unblock']  (oder None)
    Rein funktional; keinerlei Datei-/Netz-I/O."""
    per_cand = list(assess.get("per_candidate") or [])
    lead_eta = assess.get("lead_eta") or assess.get("eta") or {}
    cadence = lead_eta.get("cadence_per_year")

    # --- Kandidaten-Tabelle: numerische + legacy-regime aus assess ---
    rows: list[dict] = []
    legacy_regime_usable = 0
    for c in per_cand:
        name = c.get("name")
        usable = int(c.get("usable_n") or 0)
        eta = c.get("eta") or {}
        is_regime = str(c.get("kind")) == "categorical" or name == "regime"
        row = {
            "candidate": name,
            "kind": c.get("kind"),
            "rank": c.get("rank"),
            "view": "legacy_binary" if is_regime else "single",
            "usable_n": usable,
            "gate": min_n,
            "remaining_n": max(0, min_n - usable),
            "ready": bool(c.get("ready")),
            "eta_status": eta.get("status"),
            "eta_date": eta.get("eta_date"),
            "eta_years": eta.get("eta_years"),
        }
        rows.append(row)
        if is_regime:
            legacy_regime_usable = usable

    # --- Erweiterter regime-View (V7.33), falls vorhanden ---
    extended_regime = None
    if edge_unblock:
        ext_usable = int(edge_unblock.get("extended_usable_n") or 0)
        # Kadenz fuer regime-ETA aus den Lead-numeric-ETA uebernehmen (gleiche
        # Live-Trade-Frequenz); regime-Trades teilen die Handelsfrequenz.
        ext_remaining = max(0, min_n - ext_usable)
        extended_regime = {
            "candidate": "regime",
            "view": "extended_edge_rank_V7.33",
            "legacy_usable_n": int(edge_unblock.get("legacy_usable_n") or 0),
            "extended_usable_n": ext_usable,
            "unblock_delta": int(edge_unblock.get("unblock_delta") or 0),
            "directional_agree_n": edge_unblock.get("directional_agree_n"),
            "directional_evaluated_n": edge_unblock.get("directional_evaluated_n"),
            "gate": min_n,
            "remaining_n": ext_remaining,
            "eta_days_at_lead_cadence": project_days_to_gate(ext_remaining, cadence),
            # WICHTIG: noch nicht verdrahtet -> zaehlt NICHT als ready.
            "wiring_status": "PENDING_APPROVAL",
            "ready": False,
            "note": ("Erweiterter Per-Zell-Edge-Rank (alle 7 Regime-Klassen) macht "
                     "die Live-Trades regime-forward-nutzbar; Verdrahtung in den "
                     "Forward-Test = Operator-Freigabe (H2-Feature-Freeze)."),
        }

    # --- Bester single-view-Kandidat (das, was das Gate real steuert) ---
    best = _best_single_view_candidate(per_cand)
    best_usable = int(best.get("usable_n") or 0) if best else 0
    remaining = max(0, min_n - best_usable)

    # --- Gesamt-Verdikt + limitierender Faktor ---
    any_ready = any(r["ready"] for r in rows)
    operator_lever_available = bool(extended_regime and extended_regime["extended_usable_n"]
                                 > extended_regime["legacy_usable_n"])
    eta_years = lead_eta.get("eta_years")
    cadence_bound_distant = bool(eta_years is not None and eta_years >= 1.0)

    if any_ready:
        verdict = "H2_LIVE_FORWARD_PARTIAL_READY"
        limiting_factor = "none_gate_met_for_at_least_one_candidate"
    elif best_usable == 0 and not operator_lever_available:
        verdict = "H2_NOT_ACCUMULATING"
        limiting_factor = "no_feature_usable_live_trades"
    elif cadence_bound_distant:
        verdict = "H2_FROZEN_CADENCE_BOUND"
        # Beides limitiert: Kadenz (dominant) + der Operator-Hebel liegt ungenutzt.
        limiting_factor = ("trade_cadence_primary"
                           + ("+regime_wiring_approval_pending"
                              if operator_lever_available else ""))
    else:
        verdict = "H2_REACHABLE_NEAR"
        limiting_factor = "trade_cadence"

    # --- Kadenz-Sensitivitaet (rein illustrativ) ---
    cadence_sensitivity = []
    for cps in _CADENCE_SCENARIOS:
        d = project_days_to_gate(remaining, cps)
        cadence_sensitivity.append({
            "cadence_per_year": cps,
            "remaining_n": remaining,
            "eta_days": d,
            "eta_date": (_date.fromisoformat(as_of) + timedelta(days=d)).isoformat()
            if d is not None else None,
        })

    # --- Operator-Hebel-Illustration: erweiterter regime als Best-Kandidat ---
    operator_lever = None
    if extended_regime:
        ext_usable = extended_regime["extended_usable_n"]
        new_best = max(best_usable, ext_usable)
        operator_lever = {
            "current_best_single_view_usable_n": best_usable,
            "regime_extended_usable_n": ext_usable,
            "best_usable_n_if_extended_adopted": new_best,
            "remaining_n_if_extended_adopted": max(0, min_n - new_best),
            "note": ("Adoption verschiebt den bestbewerteten Kandidaten von "
                     f"{best_usable} auf {new_best} nutzbare Trades: reduziert "
                     "das Rest-Gate marginal; der dominante Hebel bleibt die "
                     "Handelsfrequenz. KEINE Empfehlung, reine Illustration."),
        }

    return {
        "tool": "h2_readiness_monitor",
        "version": TOOL_VERSION,
        "roadmap_item": "H2-READINESS-OBSERVABILITY (V10+ Rendite-Hebel-Sequenz)",
        "date": as_of,
        "scope": "READ_ONLY_OBSERVABILITY",
        "honesty": ("Komponiert nur bestehende read-only Diagnostik. usable_n zaehlt "
                    "FEATURE-NUTZBARE (forward_eligible) Live-Trades == V7.30/31-Semantik. "
                    "Der erweiterte regime-View (V7.33) ist noch NICHT im Forward-Test "
                    "verdrahtet (PENDING_APPROVAL) und zaehlt daher NICHT als ready. KEINE "
                    "Live-Regel/Schwelle/Sizing geaendert."),
        "gate_min_n": min_n,
        "verdict": verdict,
        "limiting_factor": limiting_factor,
        "live_forward_n": assess.get("live_forward_n"),
        "live_forward_eligible_n": assess.get("live_forward_eligible_n"),
        "lead_candidate": assess.get("lead_candidate"),
        "lead_cadence_per_year": cadence,
        "lead_eta_date": lead_eta.get("eta_date"),
        "lead_eta_years": eta_years,
        "candidates": rows,
        "regime_extended_view": extended_regime,
        "best_single_view_candidate": (best.get("name") if best else None),
        "best_single_view_usable_n": best_usable,
        "remaining_to_gate": remaining,
        "cadence_sensitivity": cadence_sensitivity,
        "operator_lever_illustration": operator_lever,
        "next_step": ("Kein Code-Schritt R&D-seitig noetig: H2-Forward-Akkumulation "
                      "ist kadenz-gebunden (Handelsfrequenz) + der erweiterte regime-View "
                      "braucht Operator-Freigabe. Dieses Tool macht beide Fakten sichtbar."),
    }


# ---------------------------------------------------------------------------
# I/O-Adapter (nicht im Selftest genutzt)
# ---------------------------------------------------------------------------
def load_assess(cutoff: str, min_n: int) -> dict:
    import entry_filter_forward_test as eff  # noqa: E402
    return eff.assess_live_forward(cutoff, min_n=min_n)


def load_edge_unblock(as_of: str) -> Optional[dict]:
    """Nimm den neuesten regime_cell_edge_map_<date>.json (<= as_of bevorzugt,
    sonst neuesten vorhandenen). Fallback: None (Monitor laeuft dann ohne den
    erweiterten regime-View, verliert aber nur diesen einen Block)."""
    if not _SIGNALS.exists():
        return None
    candidates = sorted(_SIGNALS.glob("regime_cell_edge_map_*.json"))
    if not candidates:
        return None
    # bevorzugt <= as_of, sonst der neueste ueberhaupt
    le = [p for p in candidates if p.stem.split("_")[-1] <= as_of]
    pick = le[-1] if le else candidates[-1]
    try:
        d = json.loads(pick.read_text(encoding="utf-8"))
    except Exception:
        return None
    lu = d.get("live_unblock")
    if isinstance(lu, dict):
        lu = dict(lu)
        lu["_source_file"] = pick.name
    return lu


# ---------------------------------------------------------------------------
# Markdown-Report
# ---------------------------------------------------------------------------
def render_md(rep: dict) -> str:
    L: list[str] = []
    L.append(f"# H2-Readiness-Monitor: {rep['date']} ({rep['version']})")
    L.append("")
    L.append("> Read-only Observability. Komponiert `assess_live_forward` (Entry) + "
             "`regime_cell_edge_map` (erweiterter Regime-View). KEINE Live-Regel/"
             "Sizing geaendert, nichts verdrahtet.")
    L.append("")
    L.append(f"**Verdikt:** `{rep['verdict']}` · **Limitierender Faktor:** "
             f"`{rep['limiting_factor']}`")
    L.append("")
    L.append(f"- Erfasste Live-Trades (post-cutoff): **{rep['live_forward_n']}** · "
             f"feature-nutzbar: **{rep['live_forward_eligible_n']}** · Gate: "
             f"**{rep['gate_min_n']}**")
    lc = rep.get("lead_cadence_per_year")
    L.append(f"- Lead-Kandidat: **{rep['lead_candidate']}** · Live-Kadenz: "
             f"**{lc if lc is not None else '-'}/Jahr** · ETA Gate: "
             f"**{rep.get('lead_eta_date') or '-'}** "
             f"(~{rep.get('lead_eta_years') if rep.get('lead_eta_years') is not None else '-'} Jahre)")
    L.append("")
    L.append("## Kandidaten-Readiness (single-view)")
    L.append("")
    L.append("| Kandidat | Art | Rank | View | usable_n | Rest | ready | ETA-Status | ETA-Datum |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rep["candidates"]:
        L.append(f"| {r['candidate']} | {r.get('kind')} | {r.get('rank')} | "
                 f"{r['view']} | {r['usable_n']} | {r['remaining_n']} | "
                 f"{'✅' if r['ready'] else '-'} | {r.get('eta_status')} | "
                 f"{r.get('eta_date') or '-'} |")
    L.append("")

    er = rep.get("regime_extended_view")
    if er:
        L.append("## Regime-Kandidat: Legacy vs. Erweitert (V7.33)")
        L.append("")
        L.append(f"- **Legacy (binaer, 2/7 Zellen):** usable_n = "
                 f"**{er['legacy_usable_n']}** → `NOT_ACCUMULATING`")
        L.append(f"- **Erweitert (Per-Zell-edge_rank, 7/7 Zellen):** usable_n = "
                 f"**{er['extended_usable_n']}** (Δ +{er['unblock_delta']}), "
                 f"dir. Agreement {er.get('directional_agree_n')}/"
                 f"{er.get('directional_evaluated_n')}")
        L.append(f"- **Verdrahtungs-Status:** `{er['wiring_status']}`: {er['note']}")
        L.append("")

    L.append("## Kadenz-Sensitivitaet (illustrativ, keine Empfehlung)")
    L.append("")
    L.append("| Kadenz/Jahr | Rest-Trades | ETA-Tage | ETA-Datum |")
    L.append("|---|---|---|---|")
    for s in rep["cadence_sensitivity"]:
        L.append(f"| {s['cadence_per_year']} | {s['remaining_n']} | "
                 f"{s['eta_days'] if s['eta_days'] is not None else '-'} | "
                 f"{s['eta_date'] or '-'} |")
    L.append("")

    ll = rep.get("operator_lever_illustration")
    if ll:
        L.append("## Operator-Hebel (Illustration)")
        L.append("")
        L.append(f"- Bester single-view usable_n: **{ll['current_best_single_view_usable_n']}**")
        L.append(f"- regime erweitert usable_n: **{ll['regime_extended_usable_n']}**")
        L.append(f"- Bei Adoption bester usable_n: **{ll['best_usable_n_if_extended_adopted']}** "
                 f"→ Rest-Gate **{ll['remaining_n_if_extended_adopted']}**")
        L.append(f"- {ll['note']}")
        L.append("")

    L.append("---")
    L.append(f"**Naechster Schritt:** {rep['next_step']}")
    L.append("")
    L.append("> Read-only Diagnostik.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Hermetischer Selftest
# ---------------------------------------------------------------------------
def _fake_assess(*, atr_usable=2, rsi_usable=2, regime_usable=0, cadence=15.88,
                 min_n=40) -> dict:
    def eta(usable):
        rem = max(0, min_n - usable)
        if usable == 0:
            st = "NOT_ACCUMULATING"; dt = None; yrs = None
        elif usable < 2:
            st = "INSUFFICIENT_HISTORY"; dt = None; yrs = None
        else:
            days = int(round(rem * (365.0 / cadence)))
            st = "REACHABLE_DISTANT"
            dt = (_date(2026, 7, 1) + timedelta(days=days)).isoformat()
            yrs = round(days / 365.0, 2)
        return {"status": st, "current_n": usable, "min_n": min_n,
                "remaining_n": rem, "cadence_per_year": cadence,
                "eta_date": dt, "eta_years": yrs}
    return {
        "live_forward_ready": False,
        "live_forward_n": 4,
        "live_forward_eligible_n": 2,
        "lead_candidate": "atr_pct",
        "min_n": min_n,
        "as_of": "2026-07-01",
        "eta": eta(atr_usable),
        "lead_eta": eta(atr_usable),
        "per_candidate": [
            {"name": "atr_pct", "kind": "numeric", "rank": 1,
             "usable_n": atr_usable, "ready": atr_usable >= min_n, "eta": eta(atr_usable)},
            {"name": "rsi14", "kind": "numeric", "rank": 3,
             "usable_n": rsi_usable, "ready": rsi_usable >= min_n, "eta": eta(rsi_usable)},
            {"name": "regime", "kind": "categorical", "rank": 2,
             "usable_n": regime_usable, "ready": regime_usable >= min_n,
             "eta": eta(regime_usable)},
        ],
    }


def _fake_edge_unblock(legacy=0, extended=4) -> dict:
    return {"n_trades": 4, "legacy_usable_n": legacy, "extended_usable_n": extended,
            "unblock_delta": extended - legacy, "directional_agree_n": 2,
            "directional_evaluated_n": 3}


def selftest() -> int:
    groups = []

    # G1: project_days_to_gate math
    ok = (project_days_to_gate(0, 15.88) == 0
          and project_days_to_gate(38, 0) is None
          and project_days_to_gate(38, None) is None
          and project_days_to_gate(52, 52) == 365)
    groups.append(("G1 project_days_to_gate", ok))

    # G2: baseline compose -> FROZEN_CADENCE_BOUND (atr 2, regime legacy 0/ext 4)
    rep = compose_h2_readiness(_fake_assess(), _fake_edge_unblock(), as_of="2026-07-01")
    ok = (rep["verdict"] == "H2_FROZEN_CADENCE_BOUND"
          and rep["best_single_view_usable_n"] == 2
          and rep["remaining_to_gate"] == 38
          and "regime_wiring_approval_pending" in rep["limiting_factor"])
    groups.append(("G2 baseline verdict + limiting factor", ok))

    # G3: legacy-vs-extended regime block correct + PENDING_APPROVAL (not ready)
    er = rep["regime_extended_view"]
    ok = (er["legacy_usable_n"] == 0 and er["extended_usable_n"] == 4
          and er["unblock_delta"] == 4 and er["wiring_status"] == "PENDING_APPROVAL"
          and er["ready"] is False)
    # legacy regime row in candidates table must show usable_n 0 + view legacy_binary
    reg_row = [c for c in rep["candidates"] if c["candidate"] == "regime"][0]
    ok = ok and reg_row["usable_n"] == 0 and reg_row["view"] == "legacy_binary"
    groups.append(("G3 legacy vs extended regime views", ok))

    # G4: gate met for one candidate -> PARTIAL_READY
    rep2 = compose_h2_readiness(_fake_assess(atr_usable=40), _fake_edge_unblock(),
                                as_of="2026-07-01")
    ok = (rep2["verdict"] == "H2_LIVE_FORWARD_PARTIAL_READY"
          and rep2["remaining_to_gate"] == 0)
    groups.append(("G4 gate-met -> partial ready", ok))

    # G5: no feature-usable trades AND no operator lever -> NOT_ACCUMULATING
    rep3 = compose_h2_readiness(_fake_assess(atr_usable=0, rsi_usable=0, regime_usable=0),
                                _fake_edge_unblock(legacy=0, extended=0),
                                as_of="2026-07-01")
    ok = rep3["verdict"] == "H2_NOT_ACCUMULATING"
    groups.append(("G5 not accumulating", ok))

    # G6: edge_unblock None -> still composes, no extended block, no crash
    rep4 = compose_h2_readiness(_fake_assess(), None, as_of="2026-07-01")
    ok = (rep4["regime_extended_view"] is None
          and rep4["operator_lever_illustration"] is None
          and rep4["verdict"] == "H2_FROZEN_CADENCE_BOUND"
          # without lever, limiting factor must NOT claim the regime wiring
          and "regime_wiring" not in rep4["limiting_factor"])
    groups.append(("G6 no edge_unblock graceful", ok))

    # G7: cadence sensitivity monotone (higher cadence -> fewer days)
    cs = rep["cadence_sensitivity"]
    days = [s["eta_days"] for s in cs]
    ok = (len(cs) == 3 and days == sorted(days, reverse=True) and all(d > 0 for d in days))
    groups.append(("G7 cadence sensitivity monotone", ok))

    # G8: markdown renders without error and mentions key facts
    md = render_md(rep)
    ok = ("Legacy vs. Erweitert" in md and "Kadenz-Sensitivitaet" in md
          and "PENDING_APPROVAL" in md and md.startswith("# H2-Readiness-Monitor"))
    groups.append(("G8 markdown render", ok))

    # G9: operator lever illustration math
    ll = rep["operator_lever_illustration"]
    ok = (ll["current_best_single_view_usable_n"] == 2
          and ll["regime_extended_usable_n"] == 4
          and ll["best_usable_n_if_extended_adopted"] == 4
          and ll["remaining_n_if_extended_adopted"] == 36)
    groups.append(("G9 operator lever math", ok))

    print(f"{TOOL_VERSION} h2_readiness_monitor selftest")
    passed = 0
    for name, res in groups:
        print(f"  [{'PASS' if res else 'FAIL'}] {name}")
        passed += 1 if res else 0
    print(f"{passed}/{len(groups)} groups passed")
    return 0 if passed == len(groups) else 1


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="V7.34 H2-Readiness-Monitor (read-only)")
    ap.add_argument("--cutoff", default=_OOS_CUTOFF, help="OOS-Cutoff (Jahr)")
    ap.add_argument("--min-n", type=int, default=_MIN_N, help="Forward-Gate (default 40)")
    ap.add_argument("--date", default=_date.today().isoformat(), help="Run-Datum (ISO)")
    ap.add_argument("--write", action="store_true", help="JSON + MD schreiben")
    ap.add_argument("--selftest", action="store_true", help="Hermetischen Selftest fahren")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    assess = load_assess(args.cutoff, args.min_n)
    edge = load_edge_unblock(args.date)
    rep = compose_h2_readiness(assess, edge, as_of=args.date, min_n=args.min_n)
    print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))

    if args.write:
        _SIGNALS.mkdir(exist_ok=True)
        _BRIEFINGS.mkdir(exist_ok=True)
        sig = _SIGNALS / f"h2_readiness_{args.date}.json"
        sig.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str),
                       encoding="utf-8")
        md = _BRIEFINGS / f"H2-Readiness-{args.date}.md"
        md.write_text(render_md(rep), encoding="utf-8")
        print(f"\ngeschrieben: {sig.name} + {md.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
