#!/usr/bin/env python3
"""
shadow_recommender.py: Parallel-Logger für Filter-Counterfactuals

Zweck
=====
Beantwortet quantitativ die Frage: *"Welcher Filter kostet wie viele
Empfehlungen in der aktuellen Marktphase?"* Dafür rekonstruiert er aus dem
bereits gerechneten `scan_YYYY-MM-DD.json` fünf Schatten-Szenarien, die zeigen,
wieviele Tickers in einem alternativen Filter-Regime die Threshold überschreiten
würden, und welche Top-Tickers das wären.

Wichtig: Dieses Tool ändert **nichts** am Live-System.
Es ist reines Observation, optimiert für die Cohort-Audit-Entscheidung am
2026-04-30 (COHORT-AUDIT-1). Feature-Freeze bleibt unangetastet.

Szenarien
---------
1. **baseline**: wie Live-Empfehlung (Kontrolle, muss mit
                                 Scanner-Output übereinstimmen)
2. **no_pcr_tilt**: market_pcr_adjust = 0 für alle Tickers
3. **no_laggard_cut**: alle negativen sector_rs_adjust entfernt
                                 (Behält positive Boni für Leader-Sektoren)
4. **no_flow_hard_rule**: options_flow_adjust = 0, flow-SKIPs ignoriert
5. **all_relaxed**: 2+3+4 kombiniert

Outputs (shadow/rec_log_YYYY-MM-DD.json)
----------------------------------------
    scenarios: { name: { passes_count, top5, highest_pre_score, threshold_gap } }
    delta_vs_baseline: { name: +X Passes }

Usage
-----
    python3 tools/shadow_recommender.py
    python3 tools/shadow_recommender.py --date 2026-04-17
    python3 tools/shadow_recommender.py --quiet
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
SHADOW_DIR = PROJECT_ROOT / "shadow"

# Schwellen (V6.1-Regelwerk). Bei Änderung im Live-System hier nachziehen:
# diese Konstanten sind die Referenzpunkte des Cohort-Audits.
TIER_THRESHOLD = {"A": 7.50, "B": 7.50, "C": 8.00, "X": 9.00}
DEFAULT_THRESHOLD = 7.50

# -----------------------------------------------------------------------------
# Scoring-Rekonstruktion
# -----------------------------------------------------------------------------


def _threshold_for(tier: str | None) -> float:
    return TIER_THRESHOLD.get((tier or "").upper(), DEFAULT_THRESHOLD)


def _passes(score: float, tier: str | None) -> bool:
    return score >= _threshold_for(tier)


def _adjust(setup: dict, adjust_key: str) -> float:
    """Sichere Extraktion eines Scoring-Adjusts (None → 0)."""
    v = setup.get(adjust_key)
    return float(v) if v is not None else 0.0


def recompute_score(setup: dict, *,
                    remove_pcr: bool = False,
                    remove_laggard_cut: bool = False,
                    remove_flow_rule: bool = False) -> float:
    """Berechnet den hypothetischen pre_score, wenn ausgewählte Adjusts
    entfernt werden.

    Baseline: pre_score = Summe aus rohem Score + allen Adjusts (wie live).
    Bei "remove_*" addieren wir die entsprechenden *negativen* Beiträge
    zurück, damit der Score "entsperrt" wird.

    Wichtig: positive Boni bleiben in den Szenarien erhalten, wir simulieren
    nur das Wegfallen der restriktiven Layer, nicht aller Scoring-Komponenten.
    """
    score = float(setup.get("pre_score") or 0.0)

    if remove_pcr:
        # market_pcr_adjust zurückrollen (i.d.R. negativ in defensiv_market)
        score -= _adjust(setup, "market_pcr_adjust")

    if remove_laggard_cut:
        # Nur negative sector_rs_adjust entfernen (Laggard-Penalty), positive
        # Leader-Boni behalten.
        srs = _adjust(setup, "sector_rs_adjust")
        if srs < 0:
            score -= srs

    if remove_flow_rule:
        # options_flow_adjust + die Hard-Rule-Wirkung entfernen.
        # Da der Hard-Skip in Scanner-JSON bereits nicht sichtbar ist (er
        # betrifft nur die Empfehlungs-Ausgabe), reicht es, den negativen
        # Adjust zurückzurollen.
        ofa = _adjust(setup, "options_flow_adjust")
        if ofa < 0:
            score -= ofa

    return round(score, 3)


# -----------------------------------------------------------------------------
# Szenarien
# -----------------------------------------------------------------------------

SCENARIOS = [
    ("baseline",          dict()),
    ("no_pcr_tilt",       dict(remove_pcr=True)),
    ("no_laggard_cut",    dict(remove_laggard_cut=True)),
    ("no_flow_hard_rule", dict(remove_flow_rule=True)),
    ("all_relaxed",       dict(remove_pcr=True, remove_laggard_cut=True, remove_flow_rule=True)),
]

# -----------------------------------------------------------------------------
# Hebel-Szenarien (COHORT-AUDIT-1-Vorbereitung, nicht live-geschaltet)
# -----------------------------------------------------------------------------
# H1: Threshold pauschal auf 6.50/7.00 senken
# H2: Regime-adaptive Threshold, Trending/HighVol = 7.50, sonst 6.50
# H3: Setup-Override, CQ>=12 AND triple_rsi>=7 AND PS3d>=3 AND score>=6.00 → Pass
# H4: Hard-SKIP → Malus −0.5 (Flow/Insider/Laggard werden zu weichen Penalties)
# H5: Kombi (H3 + H4), der realistische Post-Audit-Zustand
HEBEL_SCENARIOS = [
    ("h1_thr_6_50",          dict(override_threshold=6.50)),
    ("h2_regime_adaptive",   dict(regime_adaptive=True)),
    ("h3_setup_override",    dict(setup_override=True)),
    ("h4_malus_statt_skip",  dict(malus_statt_skip=True)),
    ("h5_kombi_3_plus_4",    dict(setup_override=True, malus_statt_skip=True)),
]

# Setup-Override-Gate-Kriterien (aus V6-Logik rekonstruiert)
SO_MIN_CQ           = 12   # connors_quality 0..14
SO_MIN_TRIPLE_RSI   = 7    # triple_rsi 0..10
SO_MIN_PS3D         = 3    # pullback_strength_3d 0..3
SO_MIN_SCORE        = 6.00

# Malus-statt-SKIP-Konstanten
MALUS_PER_HARD_SKIP = 0.50

# Regime-adaptive Thresholds
THR_TRENDING  = 7.50   # wie live
THR_NEUTRAL   = 6.50   # gelockert für Neutral/Sideways-Tage
TRENDING_TAGS = {"trending", "trending-strong", "high-vol", "highvol", "high_vol"}


def _regime_adaptive_threshold(tier: str | None, regime_tag: str) -> float:
    """Threshold abhängig vom Regime. Tier C bekommt +0.50 Strafe wie im Live-System."""
    base = THR_TRENDING if regime_tag.lower() in TRENDING_TAGS else THR_NEUTRAL
    return base + (0.50 if (tier or "").upper() == "C" else 0.0)


def _load_regime_tag(date: str) -> str:
    """Liest regime_YYYY-MM-DD.json, fallback 'trending' (konservativ: wie live)."""
    path = TOOLS_DIR / f"regime_{date}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return str(d.get("regime") or "trending")
    except Exception:
        return "trending"


def evaluate_hebel_scenario(top_setups: list[dict], kwargs: dict, regime_tag: str) -> dict:
    """Bewertet ein Hebel-Szenario.

    kwargs verstehen:
      - override_threshold: float | None: pauschale Schwelle (Tier C bekommt +0.50)
      - regime_adaptive: bool, regime-adaptive Schwelle
      - setup_override: bool, zweiter Entry-Pfad über Qualitäts-Gate
      - malus_statt_skip: bool, Hard-SKIP-Adjusts zurückrollen + −0.5 Malus je Kriterium
    """
    override_threshold = kwargs.get("override_threshold")
    regime_adaptive    = kwargs.get("regime_adaptive", False)
    setup_override     = kwargs.get("setup_override", False)
    malus_statt_skip   = kwargs.get("malus_statt_skip", False)

    scored = []
    passes_via_override_count = 0

    for s in top_setups:
        tier = (s.get("tier") or "").upper()
        if tier == "X":
            continue  # Blacklist bleibt gesperrt

        score = float(s.get("pre_score") or 0.0)

        # Hebel 4: Hard-SKIP-Adjusts zurückrollen, durch Malus −0.5 pro Kriterium ersetzen
        if malus_statt_skip:
            score -= _adjust(s, "options_flow_adjust")
            score -= _adjust(s, "sector_rs_adjust")
            score -= _adjust(s, "insider_adjust")
            malus = 0.0
            if (s.get("options_flow_score") or 0) <= -2:
                malus -= MALUS_PER_HARD_SKIP
            if (s.get("insider_score") or 0) <= -2:
                malus -= MALUS_PER_HARD_SKIP
            if s.get("sector_regime_tag") == "laggard":
                malus -= MALUS_PER_HARD_SKIP
            score += malus

        # Threshold bestimmen
        if override_threshold is not None:
            thr = override_threshold + (0.50 if tier == "C" else 0.0)
        elif regime_adaptive:
            thr = _regime_adaptive_threshold(tier, regime_tag)
        else:
            thr = _threshold_for(tier)

        passes_std = score >= thr

        # Setup-Override-Pfad (Hebel 3)
        passes_override = False
        if setup_override:
            cq   = s.get("connors_quality") or 0
            trsi = s.get("triple_rsi") or 0
            ps3  = s.get("pullback_strength_3d") or 0
            if cq >= SO_MIN_CQ and trsi >= SO_MIN_TRIPLE_RSI and ps3 >= SO_MIN_PS3D and score >= SO_MIN_SCORE:
                passes_override = True

        passes = passes_std or passes_override
        if passes_override and not passes_std:
            passes_via_override_count += 1

        scored.append({
            "ticker": s.get("ticker"),
            "tier": tier,
            "sector": s.get("sector"),
            "gics_sector": s.get("gics_sector"),
            "pre_score_live": s.get("pre_score"),
            "pre_score_shadow": round(score, 3),
            "threshold_used": round(thr, 3),
            "passes_standard": passes_std,
            "passes_via_override": passes_override,
            "passes_shadow": passes,
            "delta": round(score - float(s.get("pre_score") or 0.0), 3),
        })

    scored.sort(key=lambda x: x["pre_score_shadow"], reverse=True)
    passes_list = [x for x in scored if x["passes_shadow"]]
    top5 = scored[:5]
    highest = scored[0]["pre_score_shadow"] if scored else None

    return {
        "passes_count": len(passes_list),
        "passes_via_override_count": passes_via_override_count,
        "highest_pre_score": highest,
        "top5": [
            {
                "ticker": t["ticker"],
                "tier": t["tier"],
                "score": t["pre_score_shadow"],
                "threshold_used": t["threshold_used"],
                "delta_vs_live": t["delta"],
                "passes": t["passes_shadow"],
                "via_override": t["passes_via_override"],
            }
            for t in top5
        ],
        "new_passes_tickers": [p["ticker"] for p in passes_list],
    }


def evaluate_scenario(top_setups: list[dict], kwargs: dict) -> dict:
    scored = []
    for s in top_setups:
        tier = (s.get("tier") or "").upper()
        if tier == "X":
            continue  # Blacklist bleibt auch in Shadow gesperrt
        new_score = recompute_score(s, **kwargs)
        scored.append({
            "ticker": s.get("ticker"),
            "tier": tier,
            "sector": s.get("sector"),
            "gics_sector": s.get("gics_sector"),
            "pre_score_live": s.get("pre_score"),
            "pre_score_shadow": new_score,
            "threshold": _threshold_for(tier),
            "passes_shadow": _passes(new_score, tier),
            "delta": round(new_score - float(s.get("pre_score") or 0.0), 3),
        })

    scored.sort(key=lambda x: x["pre_score_shadow"], reverse=True)
    passes = [x for x in scored if x["passes_shadow"]]
    top5 = scored[:5]
    highest = scored[0]["pre_score_shadow"] if scored else None
    gap = (_threshold_for(top5[0]["tier"]) - highest) if (top5 and highest is not None) else None

    return {
        "passes_count": len(passes),
        "highest_pre_score": highest,
        "threshold_gap_to_highest": round(gap, 3) if gap is not None else None,
        "top5": [
            {
                "ticker": t["ticker"],
                "tier": t["tier"],
                "score": t["pre_score_shadow"],
                "delta_vs_live": t["delta"],
                "passes": t["passes_shadow"],
            }
            for t in top5
        ],
        "new_passes_tickers": [p["ticker"] for p in passes],
    }


# -----------------------------------------------------------------------------
# Analyse-Entry
# -----------------------------------------------------------------------------

def analyze(date: str) -> dict:
    scan_path = TOOLS_DIR / f"scan_{date}.json"
    if not scan_path.exists():
        return {
            "date": date,
            "status": "MISSING_SCAN",
            "error": f"{scan_path.name} nicht gefunden",
            "scenarios": {},
        }

    doc = json.loads(scan_path.read_text(encoding="utf-8"))
    top = doc.get("top_setups") or []

    scenarios: dict[str, Any] = {}
    for name, kw in SCENARIOS:
        scenarios[name] = evaluate_scenario(top, kw)

    baseline_passes = scenarios["baseline"]["passes_count"]
    delta_vs_baseline = {
        name: s["passes_count"] - baseline_passes
        for name, s in scenarios.items()
    }

    # Für welche Filter-Lockerung würde man heute welche Tickers zusätzlich handeln?
    additional_by_scenario = {}
    baseline_set = set(scenarios["baseline"]["new_passes_tickers"])
    for name, s in scenarios.items():
        if name == "baseline":
            continue
        addl = [t for t in s["new_passes_tickers"] if t not in baseline_set]
        additional_by_scenario[name] = addl

    # Hebel-Szenarien (COHORT-AUDIT-1-Vorbereitung), nicht live geschaltet
    regime_tag = _load_regime_tag(date)
    hebel: dict[str, Any] = {}
    for name, kw in HEBEL_SCENARIOS:
        hebel[name] = evaluate_hebel_scenario(top, kw, regime_tag=regime_tag)

    hebel_delta_vs_baseline = {
        name: s["passes_count"] - baseline_passes
        for name, s in hebel.items()
    }
    hebel_additional = {}
    for name, s in hebel.items():
        addl = [t for t in s["new_passes_tickers"] if t not in baseline_set]
        hebel_additional[name] = addl

    return {
        "date": date,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "version": "shadow_recommender/2.0",
        "scan_source": scan_path.name,
        "scan_universe_size": doc.get("universe_size"),
        "scan_valid_results": doc.get("valid_results"),
        "scan_top_count": len(top),
        "regime_tag": regime_tag,
        "scenarios": scenarios,
        "delta_vs_baseline": delta_vs_baseline,
        "additional_tickers_vs_baseline": additional_by_scenario,
        "hebel_scenarios": hebel,
        "hebel_delta_vs_baseline": hebel_delta_vs_baseline,
        "hebel_additional_tickers": hebel_additional,
        "note": (
            "Reines Observation-Log für COHORT-AUDIT-1 (2026-04-30). "
            "Keine Auswirkung auf die Live-Kandidatenliste oder das Dashboard. "
            "Hebel-Szenarien sind Vorbereitung für den Audit-Entscheid."
        ),
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def print_report(report: dict) -> None:
    if report.get("status") == "MISSING_SCAN":
        print(f"  SHADOW {report['date']}: MISSING SCAN, {report.get('error')}")
        return

    print(f"\n╔══ SHADOW-REC {report['date']} ══╗")
    print(f"  Scan-Universe: {report['scan_universe_size']} · valid: {report['scan_valid_results']} · top_n: {report['scan_top_count']}")
    print(f"╚{'═' * 40}╝")
    print(f"  Szenario                | Passes | Delta | Highest | Gap  | Top-3")
    print(f"  ------------------------|--------|-------|---------|------|-------")
    for name, s in report["scenarios"].items():
        delta = report["delta_vs_baseline"][name]
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        top3 = ",".join(f"{t['ticker']}({t['score']:.2f})" for t in s["top5"][:3])
        highest = s["highest_pre_score"]
        highest_str = f"{highest:.2f}" if highest is not None else "-"
        gap = s["threshold_gap_to_highest"]
        gap_str = f"{gap:+.2f}" if gap is not None else "-"
        print(f"  {name:<23} | {s['passes_count']:>6} | {delta_str:>5} | {highest_str:>7} | {gap_str:>4} | {top3}")

    print()
    print("  Additional-Tickers vs. baseline:")
    any_addl = False
    for name, addl in report["additional_tickers_vs_baseline"].items():
        if addl:
            any_addl = True
            print(f"    {name:<23}: {', '.join(addl)}")
    if not any_addl:
        print("    (keiner, kein Szenario erzeugt zusätzlich passierende Tickers)")
    print()

    # Hebel-Block (COHORT-AUDIT-1-Vorbereitung)
    hebel = report.get("hebel_scenarios") or {}
    if hebel:
        regime = report.get("regime_tag", "?")
        print(f"╔══ HEBEL-ANALYSE (Regime={regime}) ══╗")
        print(f"  Szenario                | Passes | Delta | via-Override | Highest | Top-3 (+Override?)")
        print(f"  ------------------------|--------|-------|--------------|---------|-------------------")
        for name, s in hebel.items():
            delta = report["hebel_delta_vs_baseline"][name]
            delta_str = f"+{delta}" if delta > 0 else str(delta)
            over = s.get("passes_via_override_count", 0)
            highest = s["highest_pre_score"]
            highest_str = f"{highest:.2f}" if highest is not None else "-"
            top3_parts = []
            for t in s["top5"][:3]:
                tag = "*" if t.get("via_override") else ""
                top3_parts.append(f"{t['ticker']}({t['score']:.2f}){tag}")
            top3 = ",".join(top3_parts)
            print(f"  {name:<23} | {s['passes_count']:>6} | {delta_str:>5} | {over:>12} | {highest_str:>7} | {top3}")
        print(f"  (* = Pass via Setup-Override-Gate)")
        print()
        print("  Hebel-Additional-Tickers vs. baseline:")
        any_addl_h = False
        for name, addl in report.get("hebel_additional_tickers", {}).items():
            if addl:
                any_addl_h = True
                preview = ", ".join(addl[:10]) + (f" … +{len(addl)-10} weitere" if len(addl) > 10 else "")
                print(f"    {name:<23}: {preview}")
        if not any_addl_h:
            print("    (keiner: auch keine Hebel erzeugen Passes)")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Shadow-Recommender Counterfactual Logger")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="ISO-Datum (default: heute)")
    ap.add_argument("--quiet", action="store_true", help="Kein stdout, nur Exit-Code")
    ap.add_argument("--out-dir", default=None, help="Output-Verzeichnis (default: shadow/)")
    args = ap.parse_args()

    report = analyze(args.date)

    out_dir = Path(args.out_dir) if args.out_dir else SHADOW_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"rec_log_{args.date}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet:
        print_report(report)
        print(f"Report: {out_path.relative_to(PROJECT_ROOT)}")

    if report.get("status") == "MISSING_SCAN":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
