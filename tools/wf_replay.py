#!/usr/bin/env python3
"""wf_replay.py: P0b (Panel-Auftrag 20.07.2026): Out-of-Sample-Replay der
MR-Entry-Regeln auf dem eigenen Scan-Archiv (Apr-Jul 2026).

(Nicht zu verwechseln mit tools/walk_forward.py von April = In-Panel-Splitter.)

WARUM: Das 440er-Panel endet 2026-03 und flaggt NEGATIVE_RECENT. Im Archiv
liegen seit April tägliche Scans mit point-in-time-Signalen (top_setups) und
~600 Closes/Tag (ticker_map): 4 Monate ungenutzte OOS-Daten. Dieses Tool
beantwortet die Weichenstellungs-Frage des Reviews 24.07.: WF ≥ 0 → Live-Pfad;
WF < 0 → Research-Modus.

METHODE (deterministisch, read-only):
  * Entry-Kandidaten je Tag D exakt nach Bot-Plan-Logik, in ZWEI Regelwelten:
      ALT  = Regeln vor dem Panel (Tier per Regime, kein Evidenz-Guard)
      NEU  = Panel-Regeln (nur Tier A; Cooldown 2 Handelstage)
    Gemeinsam: min_score+per-Ticker-Threshold, Blacklist, alpaca_tradable,
    Regime-Hard-Tiers, BC-Skip (dokumentierte Pflicht-Heuristik), Tages-Cap 2,
    keine Position doppelt.
  * Fill-Approximation: Entry = Close des Scan-Tags D (Archiv hat nur Closes).
  * Exit-Sim auf Folge-Closes: connors_sma5 → ATR-TP → ATR-SL → Hard-Exit T+3
    (<+1 %); Stops close-basiert (Gaps werden UNTERSCHÄTZT → konservativ
    lesen); Horizon 12 Handelstage, sonst letzter Close (INCOMPLETE).
  * Kosten: kanonisch 1.500 €/2 € (M12) + Slippage-Haircut 0,41 %/Trade
    (im Paper GEMESSEN, KW28/29).

CLI: --selftest | [--write] [--base DIR]
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sys
import tempfile
from statistics import mean
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exec_bot import (REGIME_HARD_TIER, PERSONAL_BLACKLIST,  # noqa: E402
                      alpaca_tradable, load_universe)

NOTIONAL_EUR = 1500.0
COST_RT_EUR = 2.0
SLIPPAGE_HAIRCUT_PCT = 0.41
MIN_SCORE = 7.0
DAILY_CAP = 2
HOLD_HORIZON = 12
HARD_EXIT_T = 3
HARD_EXIT_MIN_GAIN = 1.0
SKIP_REGIMES = {"Bullish-Confirmed"}
CANON_RE = re.compile(r"^scan_(\d{4}-\d{2}-\d{2})\.json$")


def net_eur(ret_pct: float, slippage: bool = True) -> float:
    r = ret_pct - (SLIPPAGE_HAIRCUT_PCT if slippage else 0.0)
    return r / 100.0 * NOTIONAL_EUR - COST_RT_EUR


def load_archive(base: str, start: str, end: str) -> List[dict]:
    days = []
    for p in sorted(glob.glob(os.path.join(base, "tools", "scan_*.json"))):
        m = CANON_RE.match(os.path.basename(p))
        if not m or not (start <= m.group(1) <= end):
            continue
        try:
            d = json.load(open(p))
        except (OSError, ValueError):
            continue
        if d.get("scan_date") != m.group(1):
            continue
        uni_n, val_n = d.get("universe_size") or 0, d.get("valid_results") or 0
        d["_fill_ok"] = bool(uni_n and val_n / uni_n >= 0.8)
        rpath = os.path.join(base, "tools", f"regime_{m.group(1)}.json")
        rg = json.load(open(rpath)) if os.path.exists(rpath) else {}
        d["_regime"] = rg.get("regime", "?")
        d["_allowed"] = set(rg.get("allowed_tiers") or ["A", "B", "C"])
        days.append(d)
    return days


def candidates(day: dict, ruleset: str, uni: Dict[str, dict],
               open_pos: set, cooling: set) -> List[dict]:
    regime = day["_regime"]
    if regime in SKIP_REGIMES:
        return []
    allowed = set(day["_allowed"])
    hard = REGIME_HARD_TIER.get(regime)
    if hard:
        allowed &= set(hard)
    if ruleset == "NEU":
        allowed &= {"A"}
    out = []
    for s in day.get("top_setups", []):
        if len(out) >= DAILY_CAP:
            break
        t = str(s.get("ticker", ""))
        tier = str(s.get("tier") or "?")
        score = float(s.get("pre_score") or 0)
        close = float(s.get("close") or 0)
        atr_pct = float(s.get("atr_pct") or 0)
        thr = max(MIN_SCORE, (uni.get(t, {}).get("score_threshold") or 0))
        if (t in PERSONAL_BLACKLIST or not alpaca_tradable(t) or t in open_pos
                or (ruleset == "NEU" and t in cooling)
                or score < thr or tier not in allowed
                or close <= 0 or atr_pct <= 0):
            continue
        sl_m = uni.get(t, {}).get("atr_multi_sl") or 1.8
        tp_m = uni.get(t, {}).get("atr_multi_tp") or 2.7
        atr_abs = close * atr_pct / 100.0
        out.append({"ticker": t, "tier": tier, "date": day["scan_date"],
                    "entry": close, "stop": close - sl_m * atr_abs,
                    "target": close + tp_m * atr_abs, "atr_pct": atr_pct,
                    "regime": regime})
    return out


def simulate_exit(tr: dict, future: List[dict]) -> dict:
    entry = tr["entry"]
    held = 0
    last_close = entry
    for day in future[:HOLD_HORIZON]:
        row = (day.get("ticker_map") or {}).get(tr["ticker"])
        if not row or not row.get("close"):
            continue
        held += 1
        c = float(row["close"])
        last_close = c
        sma5 = row.get("sma5")
        ret = (c - entry) / entry * 100
        if sma5 and c >= float(sma5):
            return {**tr, "exit": c, "exit_date": day["scan_date"], "days": held,
                    "reason": "connors_sma5", "ret_pct": ret}
        if c >= tr["target"]:
            return {**tr, "exit": c, "exit_date": day["scan_date"], "days": held,
                    "reason": "atr_tp", "ret_pct": ret}
        if c <= tr["stop"]:
            return {**tr, "exit": c, "exit_date": day["scan_date"], "days": held,
                    "reason": "atr_sl", "ret_pct": ret}
        if held >= HARD_EXIT_T and ret < HARD_EXIT_MIN_GAIN:
            return {**tr, "exit": c, "exit_date": day["scan_date"], "days": held,
                    "reason": "hard_exit_t3", "ret_pct": ret}
    ret = (last_close - entry) / entry * 100
    return {**tr, "exit": last_close, "exit_date": "OPEN_END", "days": held,
            "reason": "horizon_incomplete", "ret_pct": ret}


def run_wf(base: str, start: str = "2026-04-01", end: str = "2026-07-18") -> dict:
    days = load_archive(base, start, end)
    uni = load_universe(base)
    results = {}
    for ruleset in ("ALT", "NEU"):
        trades, open_pos, cooling_until = [], set(), {}
        skipped_days = 0
        for i, day in enumerate(days):
            if not day["_fill_ok"]:
                skipped_days += 1
                continue
            d0 = day["scan_date"]
            cooling = {t for t, until in cooling_until.items() if d0 <= until}
            for tr in candidates(day, ruleset, uni, open_pos, cooling):
                open_pos.add(tr["ticker"])
                res = simulate_exit(tr, days[i + 1:])
                trades.append(res)
                open_pos.discard(tr["ticker"])
                if res["exit_date"] != "OPEN_END":
                    try:
                        xd = dt.date.fromisoformat(res["exit_date"])
                        cooling_until[tr["ticker"]] = (xd + dt.timedelta(days=4)).isoformat()
                    except ValueError:
                        pass
        comp = [t for t in trades if t["reason"] != "horizon_incomplete"]
        rets = [t["ret_pct"] for t in comp]
        stats = {
            "n": len(comp), "incomplete": len(trades) - len(comp),
            "wr_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1) if rets else None,
            "mean_ret_pct": round(mean(rets), 3) if rets else None,
            "mean_net_eur": round(mean(net_eur(r) for r in rets), 2) if rets else None,
            "sum_net_eur": round(sum(net_eur(r) for r in rets), 2) if rets else None,
            "mean_net_eur_ohne_slippage": round(mean(net_eur(r, False) for r in rets), 2) if rets else None,
            "by_month": {}, "by_exit": {}, "worst_3": [], "best_3": [],
        }
        for t in comp:
            stats["by_month"].setdefault(t["date"][:7], []).append(t["ret_pct"])
            stats["by_exit"].setdefault(t["reason"], []).append(t["ret_pct"])
        stats["by_month"] = {k: {"n": len(v), "mean_net": round(mean(net_eur(r) for r in v), 2)}
                             for k, v in sorted(stats["by_month"].items())}
        stats["by_exit"] = {k: {"n": len(v), "mean_ret": round(mean(v), 2)}
                            for k, v in sorted(stats["by_exit"].items())}
        ranked = sorted(comp, key=lambda t: t["ret_pct"])
        stats["worst_3"] = [(t["ticker"], t["date"], round(t["ret_pct"], 1)) for t in ranked[:3]]
        stats["best_3"] = [(t["ticker"], t["date"], round(t["ret_pct"], 1)) for t in ranked[-3:]]
        results[ruleset] = stats
    neu = results["NEU"]
    verdict = ("INSUFFICIENT_DATA" if (neu["n"] or 0) < 8 else
               "WF_PASS" if (neu["mean_net_eur"] or -1) >= 0 else "WF_FAIL")
    return {"tool": "wf_replay", "window": [start, end],
            "archive_days": len(days), "skipped_lowfill_days": skipped_days,
            "assumptions": {"fill": "Close des Scan-Tags", "stops": "close-basiert (Gaps unterschätzt)",
                            "kosten": f"{NOTIONAL_EUR:.0f}€/{COST_RT_EUR:.0f}€ + Slippage {SLIPPAGE_HAIRCUT_PCT}%",
                            "cap": DAILY_CAP, "horizon": HOLD_HORIZON},
            "ALT": results["ALT"], "NEU": results["NEU"], "verdict_neu": verdict}


# ── selftest ─────────────────────────────────────────────────────────────────
def _mk_day(tmp, date, setups, tmap, regime="Trending", fill=1.0):
    json.dump({"scan_date": date, "universe_size": 100, "valid_results": int(100 * fill),
               "top_setups": setups, "ticker_map": tmap},
              open(os.path.join(tmp, "tools", f"scan_{date}.json"), "w"))
    json.dump({"regime": regime, "allowed_tiers": ["A", "B", "C"]},
              open(os.path.join(tmp, "tools", f"regime_{date}.json"), "w"))


def _selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("wf_replay --selftest (offline, synthetisches Archiv)")
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tools"))
        with open(os.path.join(tmp, "tools", "universe.csv"), "w") as fh:
            fh.write("ticker,name,tier,asset_class,region,sector,atr_multi_sl,atr_multi_tp,score_threshold\n")
        A = {"ticker": "AAA", "tier": "A", "close": 100.0, "atr_pct": 5.0, "pre_score": 8.0}
        B = {"ticker": "BBB", "tier": "B", "close": 50.0, "atr_pct": 4.0, "pre_score": 8.0}
        _mk_day(tmp, "2026-04-01", [A, B], {})
        _mk_day(tmp, "2026-04-02", [], {"AAA": {"close": 104.0, "sma5": 103.0},
                                        "BBB": {"close": 48.0, "sma5": 51.0}})
        _mk_day(tmp, "2026-04-03", [], {"AAA": {"close": 105.0, "sma5": 104.0},
                                        "BBB": {"close": 46.0, "sma5": 50.0}})
        _mk_day(tmp, "2026-04-06", [], {"BBB": {"close": 45.0, "sma5": 49.0}})
        rep = run_wf(tmp, "2026-04-01", "2026-04-30")

        alt, neu = rep["ALT"], rep["NEU"]
        check("G1 ALT nimmt A+B (n=2), NEU nur A (n=1)",
              alt["n"] == 2 and neu["n"] == 1)
        check("G2 AAA-Exit via connors_sma5 bei +4 %",
              alt["by_exit"].get("connors_sma5", {}).get("n") == 1
              and abs(alt["by_exit"]["connors_sma5"]["mean_ret"] - 4.0) < 0.01)
        check("G3 BBB-Exit via atr_sl (Verlierer)",
              alt["by_exit"].get("atr_sl", {}).get("n") == 1
              and alt["by_exit"]["atr_sl"]["mean_ret"] < -5)
        check("G4 kanonisches Netto mit Slippage-Haircut korrekt (+51.85€)",
              abs(neu["mean_net_eur"] - 51.85) < 0.1)
        check("G5 Verdikt: n=1 < 8 → INSUFFICIENT_DATA",
              rep["verdict_neu"] == "INSUFFICIENT_DATA")
        _mk_day(tmp, "2026-04-07", [A], {}, fill=0.5)
        rep2 = run_wf(tmp, "2026-04-01", "2026-04-30")
        check("G6 Low-Fill-Tag (50 %) wird als Entry-Tag geskippt",
              rep2["skipped_lowfill_days"] == 1 and rep2["ALT"]["n"] == 2)

    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    rep = run_wf(base)
    print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    if "--write" in argv:
        out = os.path.join(base, "signals", f"wf_replay_{dt.date.today().isoformat()}.json")
        json.dump(rep, open(out, "w"), indent=1, ensure_ascii=False, default=str)
        print(f"→ {out}", file=sys.stderr)
