#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rec_gating_attribution.py: REC-GATING-ATTRIBUTION-1  (V7.35, read-only)

Fragestellung (seit 05.05./06.05. offen, in JEDEM Tripwire-Vermerk referenziert,
aber nie mechanisch zerlegt):

    "Warum landen 25-30 Scanner-Passes (pre_score 8-10, passes_threshold=True)
     in 0-1 Live-Empfehlungen?"

Der bestehende `trade_funnel_diagnostic.py` (V7.9.15) misst den SCANNER-internen
Trichter (universe -> scanned -> tier_pass -> score_pass -> mtf -> layers) und
springt dann direkt auf den rec_log-Count. Die Layer sind H2-ablatiert
(DISABLED_LAYERS) => Stage 6 ist bedeutungslos, und der eigentliche Kollaps
passiert in der ADJUDIKATIONS-Schicht des `daily-trading-empfehlung`-Skills, die
der Funnel gar nicht modelliert:

    blacklist -> tradability(direct) -> regime.allowed_tiers
              -> regime-Hard-Tier-Rule (z.B. Trending-Divergent -> nur Tier A)
              -> score-conviction (>=7.0 action / >=8.5 high-conviction)
              -> regime.max_recs (Cap)

Dieses Tool wendet exakt diese Gate-Kette (in Reihenfolge) auf die
`scan_<date>.json`-`top_setups` an und liefert pro Gate: wieviele der N Passes
ueberleben / sterben, welches Gate der Bottleneck ist, und wie gross die Luecke
zwischen "mechanisch eligiblen" Kandidaten und der tatsaechlichen Live-Rec-Zahl
ist (= qualitativer Falling-Knife-/Discipline-Residual, den kein Tool mechanisch
abbilden kann).

WICHTIG:
  * READ-ONLY. Keine Live-Regel/Schwelle/Sizing-Aenderung. Reine Observability.
  * Die Regime-Hard-Tier-Rules unten SPIEGELN die Adjudikations-Heuristik des
    Skills (§3d): sie sind hier als dokumentierte, anpassbare Daten-Map codiert,
    NICHT als neue Live-Regel. Sie greifen nur in dieser Diagnostik.
  * `allowed_tiers` kommt AUTORITATIV aus `regime_<date>.json` (data-driven).

Artefakte:
  signals/rec_gating_attribution_<date>.json
  Briefings/Rec-Gating-Attribution-<date>.md

CLI:
  python3 tools/rec_gating_attribution.py --selftest
  python3 tools/rec_gating_attribution.py --date 2026-06-30 [--write]
  python3 tools/rec_gating_attribution.py --range 2026-06-29 2026-07-01 [--write]

Sprache: DE-Briefings / EN-Code.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # Projekt-Root
TOOLS = HERE
SIGNALS_DIR = os.path.join(ROOT, "signals")
BRIEFINGS_DIR = os.path.join(ROOT, "Briefings")

# Konfigurierbare Ausschlussliste (config/blacklist.txt oder DAILYINVEST_BLACKLIST,
# Default LEER; siehe tools/user_blacklist.py). Name PERSONAL_BLACKLIST bleibt fuer
# die Synchronitaet mit daily_scanner/exec_bot/momentum_watch.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from user_blacklist import load_user_blacklist  # noqa: E402
PERSONAL_BLACKLIST = load_user_blacklist()

# Score-Schwellen (CLAUDE.md §3, Bahn-B-Banner)
ACTION_BUY = 7.0
HIGH_CONVICTION = 8.5

# ---------------------------------------------------------------------------
# Regime-Hard-Rules (Adjudikations-Modell, spiegelt Skill §3d)
# ---------------------------------------------------------------------------
# hard_tiers : engere Tier-Verengung ALS regime.allowed_tiers (None = keine
#              zusaetzliche Verengung, dann greift allowed_tiers aus JSON).
# max_recs   : Adjudikations-Cap auf die Zahl der Direkt-Empfehlungen.
# Belegt aus Trading-Empfehlung-MDs (z.B. 2026-06-30: Trending-Divergent ->
# "nur Tier A, max 2 Empfehlungen"). Fuer nicht belegte Regime = konservativer
# Default (allowed_tiers unveraendert, max_recs aus Divergenz-Heuristik).
REGIME_HARD_RULES: Dict[str, Dict[str, Any]] = {
    "Trending-Divergent":   {"hard_tiers": ["A"],           "max_recs": 2},
    "Bullish-Divergent":    {"hard_tiers": ["A"],           "max_recs": 2},
    "High-Vol-Krise":       {"hard_tiers": ["A"],           "max_recs": 1},
    "Extreme-Fear-Thrust":  {"hard_tiers": ["A", "B"],      "max_recs": 2},
    "Trending":             {"hard_tiers": None,            "max_recs": 3},
    "Bullish-Confirmed":    {"hard_tiers": None,            "max_recs": 3},
    "Neutral":              {"hard_tiers": None,            "max_recs": 3},
}
DEFAULT_HARD_RULE = {"hard_tiers": None, "max_recs": 3}

GATE_ORDER = [
    "blacklist",
    "tradability_direct",
    "regime_allowed_tiers",
    "regime_hard_tier",
    "score_action_buy",
    "regime_max_recs",
]


# ---------------------------------------------------------------------------
# Datencontainer
# ---------------------------------------------------------------------------
@dataclass
class GateStep:
    name: str
    count_in: int
    count_out: int
    dropped: int
    drop_pct: Optional[float]           # dropped / count_in
    note: str = ""
    killed_tickers: List[str] = field(default_factory=list)


@dataclass
class DayAttribution:
    date: str
    ok: bool
    reason: str = ""
    regime: Optional[str] = None
    allowed_tiers: Optional[List[str]] = None
    hard_tiers: Optional[List[str]] = None
    max_recs: Optional[int] = None
    scan_universe: Optional[int] = None
    scan_valid: Optional[int] = None
    n_passes: int = 0                   # Eingangs-Passes (top_setups)
    tier_dist_in: Dict[str, int] = field(default_factory=dict)
    steps: List[GateStep] = field(default_factory=list)
    mechanical_eligible_n: int = 0      # nach allen Gates + Cap
    high_conviction_n: int = 0          # eligible mit pre_score >= 8.5
    bottleneck_gate: Optional[str] = None
    bottleneck_drop: Optional[int] = None
    live_rec_n: Optional[int] = None    # tatsaechliche Live-Recs (rec_log)
    qualitative_residual: Optional[int] = None   # eligible - live_rec (Falling-Knife-Skips)
    verdict: str = ""
    eligible_sample: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_scan(date_str: str, tools_dir: str = TOOLS) -> Optional[Dict[str, Any]]:
    for cand in (os.path.join(tools_dir, f"scan_{date_str}.json"),
                 os.path.join(ROOT, "signals", f"scan_{date_str}.json"),
                 os.path.join(ROOT, f"scan_{date_str}.json")):
        d = _load_json(cand)
        if d is not None:
            return d
    return None


def load_regime(date_str: str, tools_dir: str = TOOLS) -> Optional[Dict[str, Any]]:
    for cand in (os.path.join(tools_dir, f"regime_{date_str}.json"),
                 os.path.join(ROOT, "signals", f"regime_{date_str}.json"),
                 os.path.join(ROOT, f"regime_{date_str}.json")):
        d = _load_json(cand)
        if d is not None:
            return d
    return None


def load_universe_tiers(tools_dir: str = TOOLS) -> Dict[str, str]:
    """ticker -> tier aus universe.csv (Fallback wenn top_setups kein tier hat)."""
    out: Dict[str, str] = {}
    path = os.path.join(tools_dir, "universe.csv")
    if not os.path.exists(path):
        path = os.path.join(ROOT, "universe.csv")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                t = (row.get("ticker") or "").strip()
                if t:
                    out[t] = (row.get("tier") or "").strip()
    except Exception:
        pass
    return out


def load_live_rec_count(date_str: str, tools_dir: str = TOOLS) -> Optional[int]:
    """
    Tatsaechliche Live-Recs des Tages. Quellen in Prioritaet:
      1. rec_log_<date>.json mit explizitem Live-Feld (selten: shadow ist
         counterfactual, kein Live-Count).
      2. Trading-Empfehlung-V6.1-<date>.md, `**Verdikt:** N Empfehlung(en)` /
         `NO-TRADE` / `KEINE Empfehlung`. OPTIONAL: das sind private, lokal
         erzeugte Briefings (gitignored); ohne sie fehlt nur der Live-Rec-Count.
    Sonst None (Tool degradiert sauber).
    """
    for cand in (os.path.join(ROOT, "shadow", f"rec_log_{date_str}.json"),
                 os.path.join(tools_dir, f"rec_log_{date_str}.json")):
        d = _load_json(cand)
        if d is None:
            continue
        for key in ("live_recs", "n_live_recs"):
            v = d.get(key)
            if isinstance(v, int):
                return v
            if isinstance(v, list):
                return len(v)
    return load_live_rec_from_md(date_str)


def load_live_rec_from_md(date_str: str, root: str = ROOT) -> Optional[int]:
    """Parst die Verdikt-Zeile des Trading-Empfehlung-MD (best effort)."""
    import re
    path = os.path.join(root, f"Trading-Empfehlung-V6.1-{date_str}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return None
    # Nur Verdikt-Zeilen betrachten, die auch von Empfehlung/NO-TRADE handeln.
    for line in text.splitlines():
        if "Verdikt" not in line:
            continue
        low = line.lower()
        if ("empfehlung" not in low) and ("no-trade" not in low) and ("no trade" not in low):
            continue
        if ("no-trade" in low) or ("no trade" in low) or ("keine empfehlung" in low):
            return 0
        m = re.search(r"(\d+)\s*empfehlung", low)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Kern-Logik
# ---------------------------------------------------------------------------
def _tier_of(row: Dict[str, Any], uni: Dict[str, str]) -> str:
    t = row.get("tier")
    if t:
        return str(t).strip().upper()
    return (uni.get(row.get("ticker", ""), "") or "").upper()


def _score_of(row: Dict[str, Any]) -> float:
    for k in ("pre_score", "score", "final_score"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _mk_step(name: str, survivors_in: List[Dict[str, Any]],
             survivors_out: List[Dict[str, Any]], note: str = "") -> GateStep:
    cin, cout = len(survivors_in), len(survivors_out)
    dropped = cin - cout
    out_set = {id(x) for x in survivors_out}
    killed = [x.get("ticker", "?") for x in survivors_in if id(x) not in out_set]
    drop_pct = round(100.0 * dropped / cin, 1) if cin > 0 else None
    return GateStep(name=name, count_in=cin, count_out=cout, dropped=dropped,
                    drop_pct=drop_pct, note=note, killed_tickers=killed[:25])


def diagnose_day(date_str: str, tools_dir: str = TOOLS,
                 hard_rules: Optional[Dict[str, Dict[str, Any]]] = None) -> DayAttribution:
    hard_rules = hard_rules or REGIME_HARD_RULES
    rep = DayAttribution(date=date_str, ok=False)

    scan = load_scan(date_str, tools_dir)
    if scan is None:
        rep.reason = f"scan_{date_str}.json fehlt"
        rep.verdict = "NO_SCAN"
        return rep

    top = scan.get("top_setups") or []
    if not isinstance(top, list):
        rep.reason = "top_setups kein list"
        rep.verdict = "MALFORMED_SCAN"
        return rep

    uni = load_universe_tiers(tools_dir)
    regime = load_regime(date_str, tools_dir) or {}
    regime_name = regime.get("regime")
    allowed = regime.get("allowed_tiers")
    if isinstance(allowed, list):
        allowed_up = [str(x).strip().upper() for x in allowed]
    else:
        allowed_up = None
    rule = hard_rules.get(regime_name or "", DEFAULT_HARD_RULE)
    hard_tiers = rule.get("hard_tiers")
    hard_tiers_up = [str(x).strip().upper() for x in hard_tiers] if hard_tiers else None
    max_recs = rule.get("max_recs")

    rep.ok = True
    rep.regime = regime_name
    rep.allowed_tiers = allowed_up
    rep.hard_tiers = hard_tiers_up
    rep.max_recs = max_recs
    rep.scan_universe = scan.get("universe_size")
    rep.scan_valid = scan.get("valid_results")

    # Eingang: alle top_setups mit passes_threshold True (der "N Passes"-Pool).
    passes = [r for r in top if r.get("passes_threshold")]
    rep.n_passes = len(passes)
    # Tier-Verteilung Eingang
    dist: Dict[str, int] = {}
    for r in passes:
        t = _tier_of(r, uni) or "?"
        dist[t] = dist.get(t, 0) + 1
    rep.tier_dist_in = dist

    survivors = list(passes)

    # Gate 1: blacklist (i.d.R. schon vom Scanner entfernt -> 0 Drop, Vollstaendigkeit)
    s_out = [r for r in survivors if r.get("ticker") not in PERSONAL_BLACKLIST]
    rep.steps.append(_mk_step("blacklist", survivors, s_out,
                              note="Personal-Blacklist (CLAUDE.md §3)"))
    survivors = s_out

    # Gate 2: tradability == direct (nur direkt kaufbare Titel; hebel_watch separat)
    s_out = [r for r in survivors if str(r.get("tradability", "direct")).lower() == "direct"]
    rep.steps.append(_mk_step("tradability_direct", survivors, s_out,
                              note="nur tradability=direct (§A); hebel_watch = §Hebel-Pool"))
    survivors = s_out

    # Gate 3: regime.allowed_tiers (autoritativ aus regime-JSON)
    if allowed_up:
        s_out = [r for r in survivors if _tier_of(r, uni) in allowed_up]
        note = f"regime.allowed_tiers={allowed_up} (regime_{date_str}.json)"
    else:
        s_out = list(survivors)
        note = "keine allowed_tiers im regime-JSON -> Gate passiv"
    rep.steps.append(_mk_step("regime_allowed_tiers", survivors, s_out, note=note))
    survivors = s_out

    # Gate 4: regime-Hard-Tier-Rule (Adjudikations-Modell, §3d)
    if hard_tiers_up:
        s_out = [r for r in survivors if _tier_of(r, uni) in hard_tiers_up]
        note = f"Regime-Hard-Rule {regime_name} -> nur Tier {hard_tiers_up} (Modell §3d)"
    else:
        s_out = list(survivors)
        note = f"{regime_name or 'kein Regime'}: keine engere Hard-Tier-Rule -> passiv"
    rep.steps.append(_mk_step("regime_hard_tier", survivors, s_out, note=note))
    survivors = s_out

    # Gate 5: score-conviction >= ACTION_BUY (7.0)
    s_out = [r for r in survivors if _score_of(r) >= ACTION_BUY]
    rep.steps.append(_mk_step("score_action_buy", survivors, s_out,
                              note=f"pre_score >= {ACTION_BUY} (Action-Buy)"))
    survivors = s_out

    # Gate 6: regime.max_recs (Cap, schneidet, sortiert nach pre_score desc)
    survivors_sorted = sorted(survivors, key=_score_of, reverse=True)
    if isinstance(max_recs, int) and max_recs >= 0:
        s_out = survivors_sorted[:max_recs]
        note = f"Adjudikations-Cap max_recs={max_recs} ({regime_name})"
    else:
        s_out = survivors_sorted
        note = "kein max_recs-Cap"
    rep.steps.append(_mk_step("regime_max_recs", survivors_sorted, s_out, note=note))
    survivors = s_out

    rep.mechanical_eligible_n = len(survivors)
    rep.high_conviction_n = sum(1 for r in survivors if _score_of(r) >= HIGH_CONVICTION)
    rep.eligible_sample = [
        {"ticker": r.get("ticker"), "tier": _tier_of(r, uni),
         "pre_score": round(_score_of(r), 2), "tradability": r.get("tradability")}
        for r in survivors[:10]
    ]

    # Bottleneck = Gate mit groesstem absoluten Drop
    real_steps = [s for s in rep.steps if s.count_in > 0]
    if real_steps:
        bn = max(real_steps, key=lambda s: s.dropped)
        if bn.dropped > 0:
            rep.bottleneck_gate = bn.name
            rep.bottleneck_drop = bn.dropped

    # Live-Recs + qualitativer Residual
    rep.live_rec_n = load_live_rec_count(date_str, tools_dir)
    if rep.live_rec_n is not None:
        rep.qualitative_residual = rep.mechanical_eligible_n - rep.live_rec_n

    rep.verdict = _verdict(rep)
    return rep


def _verdict(rep: DayAttribution) -> str:
    if rep.n_passes == 0:
        return "NO_PASSES"
    if rep.mechanical_eligible_n == 0:
        if rep.bottleneck_gate in ("regime_hard_tier", "regime_allowed_tiers"):
            return "GATED_BY_REGIME_TIER"      # Scanner-Passes strukturell unerreichbar
        if rep.bottleneck_gate == "tradability_direct":
            return "GATED_BY_TRADABILITY"
        if rep.bottleneck_gate == "score_action_buy":
            return "GATED_BY_SCORE"
        return "GATED_ZERO_ELIGIBLE"
    if rep.mechanical_eligible_n > 0 and (rep.live_rec_n == 0):
        return "ELIGIBLE_BUT_ZERO_LIVE"        # Falling-Knife/Discipline-Residual
    if rep.live_rec_n is not None and rep.qualitative_residual not in (None, 0):
        return "ELIGIBLE_DISCIPLINE_RESIDUAL"  # mechanisch != live -> Discipline aktiv
    return "ELIGIBLE_OK"


# ---------------------------------------------------------------------------
# Range
# ---------------------------------------------------------------------------
def _daterange(a: str, b: str) -> List[str]:
    d0 = _dt.date.fromisoformat(a)
    d1 = _dt.date.fromisoformat(b)
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


def diagnose_range(a: str, b: str, tools_dir: str = TOOLS) -> List[DayAttribution]:
    return [diagnose_day(d, tools_dir) for d in _daterange(a, b)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_md(rep: DayAttribution) -> str:
    L = []
    L.append(f"# Rec-Gating-Attribution: {rep.date}")
    L.append("")
    L.append("> REC-GATING-ATTRIBUTION-1 (V7.35, read-only). Zerlegt den Kollaps "
             "der Scanner-Passes in Live-Recs Gate fuer Gate. KEINE Live-Regel-Aenderung.")
    L.append("")
    if not rep.ok:
        L.append(f"**Kein Ergebnis:** {rep.reason} (`{rep.verdict}`)")
        return "\n".join(L)

    L.append(f"**Regime:** {rep.regime}  ·  allowed_tiers={rep.allowed_tiers}  ·  "
             f"Hard-Tier={rep.hard_tiers}  ·  max_recs={rep.max_recs}")
    L.append(f"**Scan:** Universe {rep.scan_universe} / valid {rep.scan_valid}  ·  "
             f"**Passes (Eingang):** {rep.n_passes}  ·  Tier-Verteilung {rep.tier_dist_in}")
    L.append(f"**Verdikt:** `{rep.verdict}`")
    L.append("")
    L.append("## Gate-Kette")
    L.append("")
    L.append("| # | Gate | rein | raus | Drop | Drop% | Notiz |")
    L.append("|---|---|---|---|---|---|---|")
    for i, s in enumerate(rep.steps, 1):
        L.append(f"| {i} | `{s.name}` | {s.count_in} | {s.count_out} | "
                 f"{s.dropped} | {s.drop_pct if s.drop_pct is not None else '-'} | {s.note} |")
    L.append("")
    if rep.bottleneck_gate:
        L.append(f"**Bottleneck-Gate:** `{rep.bottleneck_gate}` "
                 f"(−{rep.bottleneck_drop} Passes)")
    L.append(f"**Mechanisch eligible (nach Cap):** {rep.mechanical_eligible_n} "
             f"(davon High-Conviction ≥{HIGH_CONVICTION}: {rep.high_conviction_n})")
    if rep.live_rec_n is not None:
        L.append(f"**Tatsaechliche Live-Recs:** {rep.live_rec_n}  ·  "
                 f"**Qualitativer Residual (Falling-Knife/Discipline-Skips):** "
                 f"{rep.qualitative_residual}")
    else:
        L.append("**Tatsaechliche Live-Recs:** (kein rec_log gefunden)")
    if rep.eligible_sample:
        L.append("")
        L.append("**Eligible-Sample:** " +
                 ", ".join(f"{x['ticker']}({x['tier']},{x['pre_score']})"
                           for x in rep.eligible_sample))
    L.append("")
    L.append("### Lesart")
    if rep.verdict == "GATED_BY_REGIME_TIER":
        L.append("- Der gesamte Scanner-Pass-Pool ist unter der aktiven Regime-Tier-"
                 "Rule **strukturell unerreichbar**: die Mean-Reversion-Kandidaten "
                 "sind ueberwiegend Tier B/C, das Regime laesst aber (fast) nur Tier A. "
                 "Der Agent muss Tier-A-Namen AUSSERHALB des Scans hand-picken. Das ist "
                 "die mechanische Wurzel des '25-30 Passes -> 0-1 Recs'-Mismatch.")
    elif rep.verdict == "ELIGIBLE_BUT_ZERO_LIVE":
        L.append("- Mechanisch waeren Kandidaten eligible, live wurde aber 0 empfohlen "
                 "=> der Rest-Filter ist die **qualitative Falling-Knife-/Discipline-"
                 "Adjudikation** (nicht mechanisch modellierbar).")
    elif rep.verdict == "ELIGIBLE_DISCIPLINE_RESIDUAL":
        L.append(f"- Mechanisch waeren {rep.mechanical_eligible_n} Kandidaten eligible, "
                 f"live wurden {rep.live_rec_n} empfohlen (Residual "
                 f"{rep.qualitative_residual}). Die Differenz ist die **qualitative "
                 f"Falling-Knife-/Discipline-Adjudikation**: teils werden mechanische "
                 f"Top-Tier-A-Namen (z.B. Falling-Knife-Overhang) verworfen und andere "
                 f"Tier-A-Namen ausserhalb des mechanischen Top-Sets gewaehlt.")
    elif rep.verdict == "ELIGIBLE_OK":
        L.append("- Mechanisch eligible == Live-Recs (im Rahmen): keine unerklaerte Luecke.")
    elif rep.verdict == "NO_PASSES":
        L.append("- Keine Scanner-Passes am Eingang (Scan duenn/leer).")
    L.append("")
    return "\n".join(L)


def render_range_md(reps: List[DayAttribution]) -> str:
    L = ["# Rec-Gating-Attribution: Range", ""]
    L.append("| Datum | Regime | Passes | →eligible | Bottleneck | Live | Verdikt |")
    L.append("|---|---|---|---|---|---|---|")
    for r in reps:
        if not r.ok:
            L.append(f"| {r.date} | - | - | - | - | - | {r.verdict} |")
            continue
        L.append(f"| {r.date} | {r.regime} | {r.n_passes} | {r.mechanical_eligible_n} | "
                 f"{r.bottleneck_gate or '-'} | "
                 f"{r.live_rec_n if r.live_rec_n is not None else '-'} | {r.verdict} |")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------
def write_artifacts(rep: DayAttribution) -> Tuple[str, str]:
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    os.makedirs(BRIEFINGS_DIR, exist_ok=True)
    jpath = os.path.join(SIGNALS_DIR, f"rec_gating_attribution_{rep.date}.json")
    mpath = os.path.join(BRIEFINGS_DIR, f"Rec-Gating-Attribution-{rep.date}.md")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(_to_dict(rep), fh, ensure_ascii=False, indent=2)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(render_md(rep))
    return jpath, mpath


def _to_dict(rep: DayAttribution) -> Dict[str, Any]:
    d = asdict(rep)
    return d


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def _selftest() -> int:
    """Hermetisch: nutzt eine synthetische Test-Blacklist ({"EEE"})."""
    global PERSONAL_BLACKLIST
    saved = PERSONAL_BLACKLIST
    PERSONAL_BLACKLIST = frozenset({"EEE"})
    try:
        return _selftest_body()
    finally:
        PERSONAL_BLACKLIST = saved


def _selftest_body() -> int:
    import tempfile
    failures: List[str] = []

    def chk(cond: bool, msg: str):
        if not cond:
            failures.append(msg)

    tmp = tempfile.mkdtemp(prefix="recgate_selftest_")

    # Minimal-universe.csv
    with open(os.path.join(tmp, "universe.csv"), "w", encoding="utf-8") as fh:
        fh.write("ticker,name,tier,asset_class,region,sector,atr_multi_sl,atr_multi_tp,score_threshold\n")
        fh.write("AAA,AlphaA,A,us_large,US,Tech,1.7,2.55,7.5\n")
        fh.write("BBB,BetaB,B,eu_large,DE,Materials,1.7,2.55,7.5\n")
        fh.write("CCC,GammaC,C,us_small,US,Growth,1.7,2.55,7.5\n")

    def _write_scan(date, rows, universe=100, valid=90):
        with open(os.path.join(tmp, f"scan_{date}.json"), "w", encoding="utf-8") as fh:
            json.dump({"scan_date": date, "universe_size": universe,
                       "valid_results": valid, "top_setups": rows}, fh)

    def _write_regime(date, regime, allowed):
        with open(os.path.join(tmp, f"regime_{date}.json"), "w", encoding="utf-8") as fh:
            json.dump({"date": date, "regime": regime, "allowed_tiers": allowed}, fh)

    def _row(t, tier, score, trad="direct", passes=True):
        return {"ticker": t, "tier": tier, "pre_score": score,
                "tradability": trad, "passes_threshold": passes}

    # --- Gruppe 1: Trending-Divergent -> Tier-A-only kills B/C ---
    d1 = "2099-01-01"
    _write_scan(d1, [_row("AAA", "A", 9.0), _row("BBB", "B", 9.5),
                     _row("CCC", "C", 8.0), _row("BBB2", "B", 7.2)])
    _write_regime(d1, "Trending-Divergent", ["A", "B"])
    r1 = diagnose_day(d1, tools_dir=tmp)
    chk(r1.ok, "G1 ok")
    chk(r1.n_passes == 4, f"G1 n_passes=4 got {r1.n_passes}")
    # allowed A/B removes the C; hard-rule A removes both B -> only AAA
    chk(r1.mechanical_eligible_n == 1, f"G1 eligible=1 got {r1.mechanical_eligible_n}")
    chk(r1.bottleneck_gate == "regime_hard_tier",
        f"G1 bottleneck regime_hard_tier got {r1.bottleneck_gate}")
    hard_step = [s for s in r1.steps if s.name == "regime_hard_tier"][0]
    chk(hard_step.dropped == 2, f"G1 hard_tier drops 2 B got {hard_step.dropped}")
    chk(r1.verdict in ("ELIGIBLE_OK", "ELIGIBLE_BUT_ZERO_LIVE"),
        f"G1 verdict got {r1.verdict}")

    # --- Gruppe 2: ALLE Passes Tier B unter Trending-Divergent -> 0 eligible ---
    d2 = "2099-01-02"
    _write_scan(d2, [_row("BBB", "B", 9.5), _row("BBB2", "B", 8.8),
                     _row("CCC", "C", 8.0)])
    _write_regime(d2, "Trending-Divergent", ["A", "B"])
    r2 = diagnose_day(d2, tools_dir=tmp)
    chk(r2.mechanical_eligible_n == 0, f"G2 eligible=0 got {r2.mechanical_eligible_n}")
    chk(r2.verdict == "GATED_BY_REGIME_TIER", f"G2 verdict got {r2.verdict}")

    # --- Gruppe 3: Trending (kein Hard-Tier) -> max_recs=3 Cap greift ---
    d3 = "2099-01-03"
    rows3 = [_row(f"T{i}", "A", 9.9 - i * 0.1) for i in range(6)]
    _write_scan(d3, rows3)
    _write_regime(d3, "Trending", ["A", "B", "C"])
    r3 = diagnose_day(d3, tools_dir=tmp)
    chk(r3.n_passes == 6, f"G3 n_passes=6 got {r3.n_passes}")
    chk(r3.mechanical_eligible_n == 3, f"G3 eligible=3 (cap) got {r3.mechanical_eligible_n}")
    chk(r3.bottleneck_gate == "regime_max_recs",
        f"G3 bottleneck max_recs got {r3.bottleneck_gate}")
    # Cap keeps highest scores
    tickers = [x["ticker"] for x in r3.eligible_sample]
    chk(tickers == ["T0", "T1", "T2"], f"G3 cap keeps top3 got {tickers}")

    # --- Gruppe 4: tradability gate ---
    d4 = "2099-01-04"
    _write_scan(d4, [_row("AAA", "A", 9.0, trad="hebel_only"),
                     _row("AA2", "A", 8.5, trad="direct")])
    _write_regime(d4, "Neutral", ["A", "B", "C"])
    r4 = diagnose_day(d4, tools_dir=tmp)
    trad_step = [s for s in r4.steps if s.name == "tradability_direct"][0]
    chk(trad_step.dropped == 1, f"G4 tradability drops 1 got {trad_step.dropped}")
    chk("AAA" in trad_step.killed_tickers, "G4 AAA killed by tradability")

    # --- Gruppe 5: blacklist gate ---
    d5 = "2099-01-05"
    _write_scan(d5, [_row("EEE", "A", 9.9), _row("AAA", "A", 8.0)])
    _write_regime(d5, "Neutral", ["A", "B", "C"])
    r5 = diagnose_day(d5, tools_dir=tmp)
    bl_step = [s for s in r5.steps if s.name == "blacklist"][0]
    chk(bl_step.dropped == 1, f"G5 blacklist drops 1 got {bl_step.dropped}")
    chk("EEE" in bl_step.killed_tickers, "G5 EEE killed by blacklist")

    # --- Gruppe 6: score gate ---
    d6 = "2099-01-06"
    _write_scan(d6, [_row("AAA", "A", 6.5), _row("AA2", "A", 7.4)])
    _write_regime(d6, "Neutral", ["A", "B", "C"])
    r6 = diagnose_day(d6, tools_dir=tmp)
    sc_step = [s for s in r6.steps if s.name == "score_action_buy"][0]
    chk(sc_step.dropped == 1, f"G6 score drops 1 got {sc_step.dropped}")

    # --- Gruppe 7: missing scan ---
    r7 = diagnose_day("2099-12-31", tools_dir=tmp)
    chk(not r7.ok and r7.verdict == "NO_SCAN", f"G7 NO_SCAN got {r7.verdict}")

    # --- Gruppe 8: live_rec residual ---
    d8 = "2099-01-08"
    _write_scan(d8, [_row("AAA", "A", 9.0), _row("AA2", "A", 8.5)])
    _write_regime(d8, "Neutral", ["A", "B", "C"])
    os.makedirs(os.path.join(tmp, "shadow"), exist_ok=True)
    # rec_log at tmp-level (tools_dir search covers tools_dir path)
    with open(os.path.join(tmp, f"rec_log_{d8}.json"), "w", encoding="utf-8") as fh:
        json.dump({"live_recs": 0}, fh)
    r8 = diagnose_day(d8, tools_dir=tmp)
    chk(r8.live_rec_n == 0, f"G8 live=0 got {r8.live_rec_n}")
    chk(r8.qualitative_residual == r8.mechanical_eligible_n,
        f"G8 residual got {r8.qualitative_residual}")
    chk(r8.verdict == "ELIGIBLE_BUT_ZERO_LIVE", f"G8 verdict got {r8.verdict}")

    # --- Gruppe 9: render smoke ---
    md = render_md(r1)
    chk("Gate-Kette" in md and "Bottleneck" in md, "G9 md render")
    rmd = render_range_md([r1, r2, r3])
    chk("Range" in rmd, "G9 range md render")

    print(f"[selftest] rec_gating_attribution: {9 - 0} Gruppen, "
          f"{len(failures)} Fehler")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="REC-GATING-ATTRIBUTION-1 (read-only)")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--range", nargs=2, metavar=("FROM", "TO"))
    ap.add_argument("--write", action="store_true", help="Artefakte schreiben")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true", help="JSON auf stdout")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.range:
        reps = diagnose_range(args.range[0], args.range[1])
        if args.json:
            print(json.dumps([_to_dict(r) for r in reps], ensure_ascii=False, indent=2))
        else:
            print(render_range_md(reps))
        if args.write:
            for r in reps:
                if r.ok:
                    write_artifacts(r)
        return 0

    date = args.date or _dt.date.today().isoformat()
    rep = diagnose_day(date)
    if args.json:
        print(json.dumps(_to_dict(rep), ensure_ascii=False, indent=2))
    else:
        print(render_md(rep))
    if args.write and rep.ok:
        j, m = write_artifacts(rep)
        print(f"\n[write] {j}\n[write] {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
