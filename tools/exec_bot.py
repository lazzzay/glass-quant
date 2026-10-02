#!/usr/bin/env python3
"""exec_bot.py: EXEC-BOT-1 Phase 0 (PAPER): deterministic order pipeline.

Plan → Approve → Run → Track → Reconcile. No LLM in the order path (Anti-Pattern #1).

Pipeline contract (all artifacts are the EXISTING system SSoT: the bot adds none):
  PLAN     reads tools/scan_<date>.json (canonical), signals/adjudication_gate_<date>.json
           (V7.36; rc must be 0), tools/regime_<date>.json (allowed_tiers, size_multiplier,
           regime class) and tools/universe.csv (per-ticker ATR multipliers + thresholds).
           Applies: gate-rc → blacklist → US/Alpaca-tradable → score → regime tier rules
           → cap. Writes signals/exec_bot_plan_<date>.json. NEVER submits.
  APPROVE  operator one-tap approval: writes signals/exec_bot_approval_<date>.json
           (explicit tickers; --by NAME, default $USER or 'operator').
  RUN      submits ONLY plan∩approval as server-side BRACKET orders (stop+target live at
           the broker), idempotent via client_order_id execbot-<date>-<TICKER>.
           Hard limits from tools/exec_bot_config.json. Kill file blocks everything.
  TRACK    evening: sync fills/positions → ledger, enforce Hard-Exit T+3 (<+1% → close),
           write German briefing Briefings/Exec-Bot-<date>.md.
  RECONCILE ledger vs broker positions: divergence report (Paper-Gate metric).

Safety model:
  * PAPER-ONLY lock lives in broker_alpaca (double unlock needed for live).
  * A live (non-paper) endpoint prints a loud, NON-blocking console warning:
    real money at risk, not financial advice. The lock above stays in force.
  * Kill switch: file tools/EXEC_BOT_KILL → rc=9, nothing happens, --kill cancels orders.
  * Limits: max_orders_per_day, max_positions, notional caps, long-only.
  * Regime size_multiplier scales notional (mirrors §A reduced-size rule).

Exit codes: 0 ok · 2 no-trade/nothing-to-do · 3 approval missing · 4 gate blocked or
            config invalid · 5 artifact missing · 6 broker keys missing (tools/.env) ·
            8 locked (another run is active) · 9 killed · 1 error (also broker/API errors)
CLI: --plan | --approve --tickers A,B [--by NAME] | --run | --track | --reconcile | --status
     --gate-report | --kill | --selftest [--date YYYY-MM-DD] [--base DIR]
"""
from __future__ import annotations

import csv
import datetime as dt
import fcntl
import json
import math
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from broker_alpaca import (AlpacaBroker, BrokerError, FakeTransport,  # noqa: E402
                           MissingCredentialsError, PAPER_BASE_URL,
                           resolve_credentials, warn_live_mode)
from user_blacklist import load_user_blacklist  # noqa: E402

try:  # G1 Push-Alerts: optional, fire-and-forget, Fehlen darf nichts brechen
    from notify import send as _notify_send
except Exception:  # noqa: BLE001
    _notify_send = None


def notify(title: str, msg: str, priority: str = "default", tags: str = "robot") -> None:
    if _notify_send is not None:
        try:
            _notify_send(title, msg, priority=priority, tags=tags)
        except Exception:  # noqa: BLE001
            pass

VERSION = "EXEC-BOT-1 Phase0 v0.1"

# Optional user blacklist, shared with daily_scanner (config/blacklist.txt or
# DAILYINVEST_BLACKLIST, EMPTY by default; see tools/user_blacklist.py).
USER_BLACKLIST = load_user_blacklist()
PERSONAL_BLACKLIST = USER_BLACKLIST  # backward-compatible alias (wf_replay imports it)

# Sync with rec_gating_attribution.REGIME_HARD_RULES (Skill §3d mirror, V7.35).
# Divergent/crisis regimes: Tier A only; Extreme-Fear-Thrust: A+B.
REGIME_HARD_TIER = {
    "Trending-Divergent": {"A"},
    "Bullish-Divergent": {"A"},
    "High-Vol-Krise": {"A"},
    "Extreme-Fear-Thrust": {"A", "B"},
}

DEFAULT_CONFIG = {
    "mode": "paper",
    "base_url": PAPER_BASE_URL,
    "allow_live": False,
    "max_orders_per_day": 2,
    "max_positions": 3,
    # Neutral example limits, identical to the shipped tools/exec_bot_config.json.
    # Paper money only; size them to your own situation before you change modes.
    "notional_usd_per_trade": 1000,
    "max_daily_notional_usd": 3000,
    "min_score": 7.0,
    "long_only": True,
    "entry_type": "market",           # P1 (21.07.): Config-File setzt "limit"
    "entry_limit_buffer_pct": 1.0,    # Limit = entry_ref × (1 + Buffer): nie den Gap kaufen
    "default_atr_mult_sl": 1.8,
    "default_atr_mult_tp": 2.7,
    "hard_exit_tplus": 3,
    "hard_exit_min_gain_pct": 1.0,
    # ── Skip-Cohort (Projekt-Regel, dokumentiert seit KW21; hier erstmals MECHANISCH,
    #    paper-only): mr_v61 × Bullish-Confirmed = SKIP (Panel −1,07 %, WR 48 %).
    "skip_regimes_mr": ["Bullish-Confirmed"],
    # ── M5-PILOT (operator approval required, paper-only, PRE-REGISTRIERT):
    #    Tier B auf divergenten Regimes zulassen: halbe Size, max 1/Tag,
    #    endet automatisch nach max_trades ODER end_date. Basis: tier_shadow
    #    37 Setups TD×B, T+3 +1,43 %, WR 60,7 %. Auswertung: Freitags-Review.
    "pilot_tier_b_divergent": False,   # aktiviert via exec_bot_config.json
    "pilot_tier_b_size_mult": 0.5,
    "pilot_tier_b_max_per_day": 1,
    "pilot_tier_b_max_trades": 10,
    "pilot_tier_b_end_date": None,     # optional ISO date; None = no end date (max_trades still caps)
    # ── SCHWARM-FIX: sleeve-übergreifender Gesamtdeckel (vorher konnten MR+MOM
    #    zusammen ~8k$/5 Positionen erreichen, ohne dass es irgendwo stand).
    #    Semantik-Feinjustierung (nur-MR vs. gesamt) = operator decision.
    "max_total_positions": 5,
    "max_total_notional_usd": 5000,
    # ── Momentum-Sleeve (Sleeve T, V8-Zielbild): Trend-Pullback-Entries aus dem
    #    Scan-Archiv, Exit via serverseitigem Trailing-Stop, KEIN T+3.
    "momentum_enabled": False,         # opt-in sleeve, off by default
    "momentum_max_positions": 2,
    "momentum_max_new_per_week": 1,
    "momentum_notional_usd": 1000,
    "momentum_trail_atr_mult": 2.5,
    "momentum_trail_min_pct": 4.0,
    "momentum_trail_max_pct": 15.0,
    "momentum_min_mom5d": 2.0,
    "momentum_max_rsi2": 45.0,
    "momentum_max_atr_pct": 8.0,
    "momentum_min_price": 5.0,
    # ── PANEL 20.07. (4/4-Konsens): SCHWARM-FIX 21.07. (F3): Keys gehören in
    #    DEFAULT_CONFIG, sonst greift der Unknown-Key-Guard nicht.
    "tier_evidence_guard": True,
    "max_risk_per_trade_usd": 50,
    "max_portfolio_heat_usd": 150,
    "reentry_cooldown_days": 2,
    "kill_gate": {},
}

KILL_FILE = "EXEC_BOT_KILL"
_LOCK_FH = None  # Prozess-Lebensdauer-Handle für flock


def acquire_lock(base: str) -> bool:
    """SCHWARM-FIX: exklusives flock gegen parallele run/track/kill-Läufe
    (Battery-Catchup + manuelle Session konnten kollidieren)."""
    global _LOCK_FH
    _LOCK_FH = open(p_tools(base, "exec_bot.lock"), "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """SCHWARM-FIX: Wertebereichs-Prüfung, ein Tippfehler (z.B. notional 16000)
    darf nie stillschweigend 10x-Size handeln."""
    errs: List[str] = []
    for k in ("max_orders_per_day", "max_positions", "momentum_max_positions",
              "momentum_max_new_per_week", "pilot_tier_b_max_per_day",
              "pilot_tier_b_max_trades", "max_total_positions"):
        v = cfg.get(k)
        if not isinstance(v, (int, float)) or not 1 <= v <= 20:
            errs.append(f"{k}={v!r} (erwartet 1..20)")
    for k in ("notional_usd_per_trade", "max_daily_notional_usd",
              "momentum_notional_usd", "max_total_notional_usd"):
        v = cfg.get(k)
        if not isinstance(v, (int, float)) or not 50 <= v <= 20000:
            errs.append(f"{k}={v!r} (erwartet 50..20000: Paper-Phase-Deckel)")
    if not 0 < float(cfg.get("pilot_tier_b_size_mult", 0.5)) <= 1:
        errs.append("pilot_tier_b_size_mult außerhalb (0,1]")
    tmin, tmax = float(cfg.get("momentum_trail_min_pct", 4)), float(cfg.get("momentum_trail_max_pct", 15))
    if not 0 < tmin < tmax <= 30:
        errs.append(f"trail-Klammer {tmin}/{tmax} unplausibel")
    # SCHWARM-FIX 21.07. (F3): Panel-Keys validieren + Tippfehler-Guard.
    if not 10 <= float(cfg.get("max_risk_per_trade_usd", DEFAULT_CONFIG["max_risk_per_trade_usd"])) <= 200:
        errs.append(f"max_risk_per_trade_usd={cfg.get('max_risk_per_trade_usd')!r} (erwartet 10..200)")
    if not 100 <= float(cfg.get("max_portfolio_heat_usd", DEFAULT_CONFIG["max_portfolio_heat_usd"])) <= 1000:
        errs.append(f"max_portfolio_heat_usd={cfg.get('max_portfolio_heat_usd')!r} (erwartet 100..1000)")
    if not 0 <= int(cfg.get("reentry_cooldown_days", 2)) <= 10:
        errs.append(f"reentry_cooldown_days={cfg.get('reentry_cooldown_days')!r} (erwartet 0..10)")
    if not 0 <= float(cfg.get("entry_limit_buffer_pct", 1.0)) <= 3:
        errs.append(f"entry_limit_buffer_pct={cfg.get('entry_limit_buffer_pct')!r} (erwartet 0..3)")
    if str(cfg.get("entry_type", "market")) not in ("market", "limit"):
        errs.append(f"entry_type={cfg.get('entry_type')!r} (erwartet market|limit)")
    unknown = set(cfg) - set(DEFAULT_CONFIG)
    if unknown:
        errs.append(f"unbekannte Config-Keys (Tippfehler?): {sorted(unknown)}")
    return errs


def _us_market_closed_now() -> bool:
    """SCHWARM-FIX 21.07. (F1): True, wenn die US-Session sicher vorbei ist
    (≥16:03 ET). Der 15:45-CET-Track (09:45 ET) darf frische Limits NIE canceln;
    der 22:00-Track (≥16:00 ET) soll Same-Evening-Stales räumen. Fallback False
    = konservativ (dann greift der Folgetags-Cleanup)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(ZoneInfo("America/New_York"))
        return (now_et.hour, now_et.minute) >= (16, 3)
    except Exception:
        return False


# ── plumbing ─────────────────────────────────────────────────────────────────
def repo_base() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def p_tools(base: str, name: str) -> str:
    return os.path.join(base, "tools", name)


def p_sig(base: str, name: str) -> str:
    return os.path.join(base, "signals", name)


def p_brief(base: str, name: str) -> str:
    return os.path.join(base, "Briefings", name)


def load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def load_config(base: str) -> Dict[str, Any]:
    """SCHWARM-FIX 09.07.: kaputtes Config-JSON fiel STILL auf Defaults zurück
    (M5-Pilot wäre lautlos verschwunden). Jetzt: fail-loud."""
    cfg = dict(DEFAULT_CONFIG)
    path = p_tools(base, "exec_bot_config.json")
    if os.path.exists(path):
        user = load_json(path)
        if not isinstance(user, dict):
            notify("Exec-Bot: CONFIG UNLESBAR",
                   "exec_bot_config.json kaputt: Abbruch statt stiller Defaults.",
                   priority="urgent", tags="rotating_light")
            print("CONFIG_UNREADABLE: tools/exec_bot_config.json ist kein valides "
                  "JSON: Abbruch (kein stiller Default-Fallback).")
            sys.exit(4)
        cfg.update(user)
    errs = validate_config(cfg)
    if errs:
        notify("Exec-Bot: CONFIG INVALID", "; ".join(errs)[:180],
               priority="urgent", tags="rotating_light")
        print("CONFIG_INVALID: " + " | ".join(errs))
        sys.exit(4)
    return cfg


def killed(base: str) -> bool:
    return os.path.exists(p_tools(base, KILL_FILE))


def today_str() -> str:
    return dt.date.today().isoformat()


def effective_track_date(now: dt.datetime) -> str:
    """MMM-LEKTION 14.07.: ein 22:00-Track, der (Mac-Schlaf) erst um 01:09
    aufwacht, gehört zum VORTAGES-Handelsabend, sonst zählt T+3 einen Tag,
    der noch gar nicht gehandelt wurde. Läufe vor 08:00 werten den Vortag."""
    d = now.date()
    if now.hour < 8:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def alpaca_tradable(sym: str) -> bool:
    """Paper phase trades the US-listed subset only (Alpaca has no .DE/.L/=F …)."""
    return bool(sym) and sym.isascii() and "." not in sym and "=" not in sym \
        and "-" not in sym and sym.upper() == sym


def load_universe(base: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    path = p_tools(base, "universe.csv")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    out[row["ticker"]] = {
                        "atr_multi_sl": float(row.get("atr_multi_sl") or 0) or None,
                        "atr_multi_tp": float(row.get("atr_multi_tp") or 0) or None,
                        "score_threshold": float(row.get("score_threshold") or 0) or None,
                    }
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return out


# SCHWARM-FIX 09.07.: NYSE-Feiertage zählten als Handelstage → T+3 einen Tag zu früh.
US_MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def trading_days_between(d0: dt.date, d1: dt.date) -> int:
    """NYSE-Handelstage in (d0, d1]: Wochenenden + statische Feiertagsliste."""
    n, cur = 0, d0
    while cur < d1:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5 and cur.isoformat() not in US_MARKET_HOLIDAYS:
            n += 1
    return n


# ── ledger ───────────────────────────────────────────────────────────────────
def ledger_path(base: str) -> str:
    return p_sig(base, "exec_bot_paper_ledger.json")


def load_ledger(base: str) -> Dict[str, Any]:
    led = load_json(ledger_path(base))
    if not isinstance(led, dict):
        led = {}
    led.setdefault("orders", {})   # client_order_id -> record
    led.setdefault("closed", [])
    led.setdefault("errors", [])
    return led


# ── PLAN ─────────────────────────────────────────────────────────────────────
def _pilot_trades_done(base: str) -> int:
    """Selbstabschaltung des M5-Piloten. SCHWARM-FIX: zählt GEFÜLLTE Pilot-Orders
    (pre-registriert sind 10 auswertbare Trades, nicht 10 Submits)."""
    led = load_ledger(base)
    return sum(1 for r in led["orders"].values()
               if r.get("pilot") and r.get("filled_avg_price"))


def build_plan(base: str, date: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    plan: Dict[str, Any] = {"version": VERSION, "date": date, "orders": [],
                            "rejections": [], "verdict": None, "reason": None}

    gate = load_json(p_sig(base, f"adjudication_gate_{date}.json"))
    if gate is None:
        plan.update(verdict="NO_TRADE", reason="GATE_ARTIFACT_MISSING (V7.36 zuerst ausführen)")
        return plan
    # FIX 24.07.: Gate liefert bei rc=0 auch "TRADE_ALLOWED_WITH_WARNINGS"
    # (z. B. data_health-Timing); rc ist die Wahrheit, Warnings blocken nicht.
    if gate.get("rc") != 0 or gate.get("verdict") not in (
            "TRADE_ALLOWED", "TRADE_ALLOWED_WITH_WARNINGS"):
        plan.update(verdict="NO_TRADE",
                    reason=f"GATE_BLOCKED rc={gate.get('rc')} verdict={gate.get('verdict')}")
        return plan

    scan = load_json(p_tools(base, f"scan_{date}.json"))
    if not scan or scan.get("scan_date") != date:
        plan.update(verdict="NO_TRADE", reason="SCAN_MISSING_OR_DATE_MISMATCH")
        return plan

    regime_doc = load_json(p_tools(base, f"regime_{date}.json"))
    if not isinstance(regime_doc, dict) or not regime_doc:
        plan.update(verdict="NO_TRADE",
                    reason="REGIME_ARTIFACT_MISSING (run tools/regime_gate.py --save first)")
        return plan
    if str(regime_doc.get("status", "ok")).lower() == "degraded":
        plan.update(verdict="NO_TRADE",
                    reason="REGIME_DEGRADED (missing: "
                           f"{', '.join(regime_doc.get('fallback_metrics') or []) or '?'})")
        return plan
    regime = regime_doc.get("regime", "?")
    size_mult = float(regime_doc.get("size_multiplier") or 1.0)
    allowed_tiers = set(regime_doc.get("allowed_tiers") or ["A"])
    hard = REGIME_HARD_TIER.get(regime)
    pilot_active = bool(
        cfg.get("pilot_tier_b_divergent") and hard is not None
        and date <= str(cfg.get("pilot_tier_b_end_date") or "9999-12-31")
        and _pilot_trades_done(base) < int(cfg.get("pilot_tier_b_max_trades", 10)))
    if hard:
        hard_eff = set(hard) | ({"B"} if pilot_active else set())
        allowed_tiers &= hard_eff
    plan["regime"] = regime
    plan["size_multiplier"] = size_mult
    plan["allowed_tiers"] = sorted(allowed_tiers)
    plan["pilot_tier_b_active"] = pilot_active
    pilot_today = 0

    # Earnings-Blackout MECHANISCH (09.07.): war bisher qualitative Pflicht der
    # Session: jetzt Filter im Plan (Vorstufe für Auto-Run mit Veto-Fenster).
    earn = load_json(p_tools(base, f"earnings_{date}.json")) or {}
    blackout = set(earn.get("blackout_tickers") or [])
    plan["earnings_blackout_n"] = len(blackout)

    uni = load_universe(base)
    notional = round(float(cfg["notional_usd_per_trade"]) * size_mult, 2)
    budget = float(cfg["max_daily_notional_usd"])

    # ── PANEL-REGELN 20.07. (4/4-Konsens, vorregistriert) ────────────────────
    led_now = load_ledger(base)
    open_recs = [r for r in led_now["orders"].values()
                 if not r.get("closed") and str(r.get("status")) not in DEAD_ORDER_STATES]
    # Evidenz-Guard: Panel 434 A / 6 B / 0 C → MR nur Tier A; B exklusiv Pilot.
    evid_tiers = ({"A"} | ({"B"} if pilot_active else set())
                  if cfg.get("tier_evidence_guard", True) else {"A", "B", "C"})
    # Re-Entry-Cooldown: kein Wiedereinstieg <2 Handelstage nach Close/Stop-Out.
    cool_days = int(cfg.get("reentry_cooldown_days", 2))
    cooling = set()
    for crec in led_now.get("closed", []):
        try:
            # SCHWARM-FIX 21.07. (F5/S1): nie gefüllte/gecancelte Orders sind KEIN
            # Exit, mit Limit-Entries ist Nicht-Fill der Normalfall, der Ticker
            # muss am Folgetag sofort wieder planbar sein (Panel: Cooldown nur
            # nach Close/Stop-Out).
            if str(crec.get("why", "")) in ("NEVER_FILLED", "STALE_ENTRY_CANCELED"):
                continue
            if trading_days_between(dt.date.fromisoformat(str(crec.get("date"))),
                                    dt.date.fromisoformat(date)) < cool_days:
                cooling.add(crec.get("ticker"))
        except (ValueError, TypeError):
            continue
    # Portfolio-Heat: Summe offener Stop-Risiken (MR) + heute geplante.
    heat_cap = float(cfg.get("max_portfolio_heat_usd", DEFAULT_CONFIG["max_portfolio_heat_usd"]))
    heat_now = sum((r.get("qty") or 0) * max(0.0, (r.get("entry_ref") or 0)
                                             - (r.get("stop") or 0))
                   for r in open_recs if r.get("strategy") != "momentum")
    planned_risk = 0.0
    risk_cap = float(cfg.get("max_risk_per_trade_usd", DEFAULT_CONFIG["max_risk_per_trade_usd"]))
    plan["heat_now_usd"] = round(heat_now, 2)

    # SESSION-FUND 10.07.: Plan muss offene Positionen + freie Slots kennen,
    # sonst bietet er Doppel-Entries und Kandidaten ohne Slot an.
    led_now = load_ledger(base)
    open_recs = [r for r in led_now["orders"].values()
                 if not r.get("closed") and str(r.get("status")) not in DEAD_ORDER_STATES]
    open_tickers = {r["ticker"] for r in open_recs}
    open_mr = sum(1 for r in open_recs if r.get("strategy") != "momentum")
    mr_slots = max(0, int(cfg["max_positions"]) - open_mr)
    plan["mr_slots_free"] = mr_slots
    if mr_slots == 0:
        plan["reason"] = "MR_SLOTS_FULL"

    for s in scan.get("top_setups", []):
        if len(plan["orders"]) >= min(int(cfg["max_orders_per_day"]), mr_slots):
            break
        t = str(s.get("ticker", ""))
        score = float(s.get("pre_score") or 0)
        tier = str(s.get("tier") or "?")
        close = float(s.get("close") or 0)
        atr_pct = float(s.get("atr_pct") or 0)

        def reject(why: str) -> None:
            plan["rejections"].append({"ticker": t, "why": why})

        if regime in (cfg.get("skip_regimes_mr") or []):
            reject(f"skip_cohort_mr({regime})"); continue
        if t in open_tickers:
            reject("already_open_position"); continue
        if t in USER_BLACKLIST:
            reject("blacklist"); continue
        if t in blackout:
            reject("earnings_blackout"); continue
        if not alpaca_tradable(t):
            reject("not_alpaca_tradable"); continue
        u = uni.get(t) or {}
        min_score = max(float(cfg["min_score"]), u.get("score_threshold") or 0)
        if score < min_score:
            reject(f"score {score:.2f} < {min_score:.2f}"); continue
        if tier not in allowed_tiers:
            reject(f"tier {tier} not in {sorted(allowed_tiers)} (regime {regime})"); continue
        if tier not in evid_tiers:
            reject(f"tier_not_evidenced({tier}; Panel 20.07.: 434A/6B/0C)"); continue
        if t in cooling:
            reject("reentry_cooldown"); continue
        if close <= 0 or atr_pct <= 0:
            reject("bad_price_or_atr"); continue

        is_pilot = bool(pilot_active and hard and tier == "B" and tier not in hard)
        if is_pilot and pilot_today >= int(cfg.get("pilot_tier_b_max_per_day", 1)):
            reject("pilot_daily_cap"); continue
        eff_notional = notional * (float(cfg.get("pilot_tier_b_size_mult", 0.5))
                                   if is_pilot else 1.0)
        # SCHWARM-FIX 21.07. (F7): Size-Multiplikator skaliert AUCH das Risk-Cap,
        # sonst wäre "halbe Pilot-Size" bei bindendem Risiko-Cap faktisch Voll-Size
        # (Pre-Registrierung verletzt, Pilot-Auswertung verzerrt).
        size_factor = eff_notional / notional if notional > 0 else 1.0

        # Stop-Mathe VOR dem Sizing: Position wird über die Stop-Distanz dimensioniert
        atr_abs = close * atr_pct / 100.0
        sl_mult = u.get("atr_multi_sl") or float(cfg["default_atr_mult_sl"])
        tp_mult = u.get("atr_multi_tp") or float(cfg["default_atr_mult_tp"])
        stop = round(close - sl_mult * atr_abs, 2)
        target = round(close + tp_mult * atr_abs, 2)
        if not (0 < stop < close < target):
            reject("bracket_math_invalid"); continue
        # SCHWARM-FIX 21.07. (S7): Mini-ATR → Limit-Entry läge ÜBER dem TP;
        # Alpaca wirft 422 = Order-Fehler gegen das Paper-Gate → vorher raus.
        if str(cfg.get("entry_type")) == "limit":
            _lim = round(close * (1 + float(cfg.get("entry_limit_buffer_pct", 1.0)) / 100), 2)
            if _lim >= target:
                reject(f"tp_below_limit_entry (Limit {_lim} >= TP {target}, Mini-ATR)"); continue

        # PANEL/CRO: Risiko-Sizing, Risiko/Trade = qty × Stop-Distanz ≤ risk_cap
        stop_dist = close - stop
        qty_notional = int(math.floor(eff_notional / close))
        qty_risk = int(math.floor(risk_cap * size_factor / stop_dist)) if stop_dist > 0 else 0
        qty = min(qty_notional, qty_risk)
        if qty < 1:
            reject(f"risk_sized_out (Stop-Distanz {stop_dist:.2f}$ × 1 > {risk_cap}$)"
                   if qty_risk < 1 else f"qty_zero at notional {eff_notional}"); continue
        trade_risk = qty * stop_dist
        if heat_now + planned_risk + trade_risk > heat_cap:
            reject(f"heat_limit ({heat_now + planned_risk + trade_risk:.0f}$ > {heat_cap:.0f}$)"); continue
        if qty * close > budget:  # SCHWARM-FIX 21.07. (F9): echte Order-Kosten zählen
            reject("daily_notional_budget"); continue
        budget -= qty * close
        planned_risk += trade_risk

        if is_pilot:
            pilot_today += 1
        plan["orders"].append({
            "ticker": t, "tier": tier, "score": round(score, 2),
            "qty": qty, "entry_ref": round(close, 2),
            "stop": stop, "target": target,
            "atr_pct": round(atr_pct, 2),
            "notional_usd": round(qty * close, 2),
            "risk_usd": round(trade_risk, 2),
            "pilot": is_pilot,
            "client_order_id": f"execbot-{date}-{t}",
        })

    # tag strategy on MR orders
    for o in plan["orders"]:
        o["strategy"] = "mr"

    # ── Momentum-Sleeve (Sleeve T): Trend-Pullback aus ticker_map ────────────
    plan["momentum_orders"] = []
    if cfg.get("momentum_enabled"):
        plan["momentum_orders"] = build_momentum_candidates(base, date, cfg, scan,
                                                            plan["orders"])

    planned_any = bool(plan["orders"] or plan["momentum_orders"])
    plan["verdict"] = "ORDERS_PLANNED" if planned_any else "NO_TRADE"
    if not planned_any and plan["reason"] is None:
        plan["reason"] = "ALL_CANDIDATES_REJECTED"
    return plan


def _momentum_open_this_week(base: str, date: str) -> int:
    """Count momentum entries whose entry date falls in the same ISO week."""
    led = load_ledger(base)
    try:
        y, w, _ = dt.date.fromisoformat(date).isocalendar()
    except ValueError:
        return 0
    n = 0
    for rec in led["orders"].values():
        if rec.get("strategy") != "momentum":
            continue
        try:
            ry, rw, _ = dt.date.fromisoformat(rec["date"]).isocalendar()
        except (ValueError, KeyError):
            continue
        if (ry, rw) == (y, w):
            n += 1
    return n


DEAD_ORDER_STATES = {"canceled", "cancelled", "expired", "rejected"}


def _momentum_open_positions(base: str) -> int:
    led = load_ledger(base)
    return sum(1 for r in led["orders"].values()
               if r.get("strategy") == "momentum" and not r.get("closed")
               and str(r.get("status")) not in DEAD_ORDER_STATES)


def build_momentum_candidates(base: str, date: str, cfg: Dict[str, Any],
                              scan: Dict[str, Any],
                              mr_orders: List[dict]) -> List[dict]:
    """Sleeve-T v1 entry: strong uptrend taking a breather (Momentum-Watch-Logik,
    deterministisch aus ticker_map): close>SMA200, weekly SMA10>SMA40, mom_5d
    kräftig, rsi2 im Atempause-Band, ATR gedeckelt. Exit = Trailing-Stop (broker-
    seitig), KEIN Target, KEIN T+3: Gewinner dürfen 6-25 % laufen."""
    out: List[dict] = []
    if _momentum_open_this_week(base, date) >= int(cfg["momentum_max_new_per_week"]):
        return out
    if _momentum_open_positions(base) >= int(cfg["momentum_max_positions"]):
        return out
    mr_tickers = {o["ticker"] for o in mr_orders}
    led_open = {r["ticker"] for r in load_ledger(base)["orders"].values()
                if not r.get("closed") and str(r.get("status")) not in DEAD_ORDER_STATES}
    tm = scan.get("ticker_map") or {}
    cands = []
    for t, row in tm.items():
        try:
            close = float(row.get("close") or 0)
            sma200 = float(row.get("sma200") or 0)
            w10 = float(row.get("weekly_sma10") or 0)
            w40 = float(row.get("weekly_sma40") or 0)
            mom5 = float(row.get("mom_5d") or 0)
            rsi2 = float(row.get("rsi2") or 100)
            atr_pct = float(row.get("atr_pct") or 99)
        except (TypeError, ValueError):
            continue
        if (t in USER_BLACKLIST or not alpaca_tradable(t)
                or t in mr_tickers or t in led_open):
            continue
        if close < float(cfg["momentum_min_price"]) or sma200 <= 0 or close <= sma200:
            continue
        if not (w10 > w40 > 0):
            continue
        if mom5 < float(cfg["momentum_min_mom5d"]) or rsi2 > float(cfg["momentum_max_rsi2"]):
            continue
        if atr_pct > float(cfg["momentum_max_atr_pct"]) or atr_pct <= 0:
            continue
        trend_strength = (close / sma200 - 1) * 100
        cands.append((trend_strength, t, close, atr_pct, mom5, rsi2))
    cands.sort(reverse=True)

    # SCHWARM-FIX: Wochen-Cap muss bereits verbrauchte Entries dieser Woche
    # abziehen, sonst sind nach schnellem Stop-Out 3/Woche bei Cap 2 möglich.
    budget_slots = min(int(cfg["momentum_max_new_per_week"])
                       - _momentum_open_this_week(base, date),
                       int(cfg["momentum_max_positions"]) - _momentum_open_positions(base))
    for trend_strength, t, close, atr_pct, mom5, rsi2 in cands[:max(0, budget_slots)]:
        qty = int(math.floor(float(cfg["momentum_notional_usd"]) / close))
        if qty < 1:
            continue
        trail = max(float(cfg["momentum_trail_min_pct"]),
                    min(float(cfg["momentum_trail_max_pct"]),
                        float(cfg["momentum_trail_atr_mult"]) * atr_pct))
        out.append({
            "ticker": t, "strategy": "momentum", "qty": qty,
            "entry_ref": round(close, 2), "trail_percent": round(trail, 1),
            "trend_strength_pct": round(trend_strength, 1),
            "mom_5d": round(mom5, 1), "rsi2": round(rsi2, 0),
            "atr_pct": round(atr_pct, 2),
            "notional_usd": round(qty * close, 2),
            "client_order_id": f"execbot-{date}-{t}-MOM",
        })
    return out


# ── RUN ──────────────────────────────────────────────────────────────────────
RC_KEYS_MISSING = 6


def make_broker(cfg: Dict[str, Any], transport=None) -> AlpacaBroker:
    creds = resolve_credentials()
    if transport is None and (not creds["key"] or not creds["secret"]):
        # Never send unauthenticated requests to the broker.
        raise MissingCredentialsError(
            "ALPACA_KEY_ID/ALPACA_SECRET_KEY missing (tools/.env or environment)")
    return AlpacaBroker(base_url=cfg.get("base_url", PAPER_BASE_URL),
                        key_id=creds["key"], secret_key=creds["secret"],
                        allow_live_cfg=bool(cfg.get("allow_live", False)),
                        transport=transport)


def run_orders(base: str, date: str, cfg: Dict[str, Any],
               broker: Optional[AlpacaBroker] = None) -> int:
    if killed(base):
        print("KILLED: tools/EXEC_BOT_KILL vorhanden, keine Aktion."); return 9
    plan = load_json(p_sig(base, f"exec_bot_plan_{date}.json"))
    if not plan:
        print("Kein Plan-Artefakt: erst --plan ausführen."); return 5
    if plan.get("verdict") != "ORDERS_PLANNED":
        print(f"Plan-Verdikt {plan.get('verdict')} ({plan.get('reason')}): nichts zu tun.")
        return 2
    approval = load_json(p_sig(base, f"exec_bot_approval_{date}.json"))
    if not approval or not approval.get("approved"):
        print("Keine Freigabe (signals/exec_bot_approval): operator approval missing."); return 3
    approved = set(approval["approved"])

    broker = broker or make_broker(cfg)
    led = load_ledger(base)
    positions = {p.get("symbol") for p in broker.get_positions()}
    # SCHWARM-FIX: ungefüllte Buy-Orders (Overnight-Queue!) zählen wie Positionen,
    # sonst ist Doppel-Entry am Folgetag möglich.
    try:
        pending_buys = {o.get("symbol") for o in broker.get_open_orders()
                        if o.get("side") == "buy"}
    except BrokerError:
        pending_buys = set()
    busy = positions | pending_buys
    submitted, skipped = [], []
    # SCHWARM-FIX: sleeve-übergreifende Gesamtdeckel
    max_total_pos = int(cfg.get("max_total_positions", 5))
    max_total_notional = float(cfg.get("max_total_notional_usd", DEFAULT_CONFIG["max_total_notional_usd"]))
    today_prior_notional = sum(
        (r.get("qty") or 0) * (r.get("entry_ref") or 0)
        for r in led["orders"].values()
        if r.get("date") == date and str(r.get("status")) not in DEAD_ORDER_STATES)
    new_count, new_notional = 0, 0.0

    for o in plan["orders"]:
        cid, t = o["client_order_id"], o["ticker"]
        if t not in approved:
            skipped.append((t, "not_approved")); continue
        if cid in led["orders"]:
            skipped.append((t, "already_submitted_idempotent")); continue
        if t in busy:
            skipped.append((t, "position_or_open_order_exists")); continue
        if len(positions) + len(submitted) >= int(cfg["max_positions"]):
            skipped.append((t, "max_positions")); continue
        if len(busy) + new_count >= max_total_pos:
            skipped.append((t, "max_total_positions")); continue
        if today_prior_notional + new_notional + o["notional_usd"] > max_total_notional:
            skipped.append((t, "max_total_notional")); continue
        try:
            # P1 SLIPPAGE-FIX: Limit-Entry bei Referenz+Buffer, der Overnight-Gap
            # wird nie mehr bezahlt; öffnet es tiefer, füllen wir besser.
            limit_px = (round(o["entry_ref"] * (1 + float(cfg.get("entry_limit_buffer_pct", 1.0)) / 100), 2)
                        if cfg.get("entry_type") == "limit" else None)
            if limit_px is not None and limit_px >= float(o["target"]):
                # SCHWARM-FIX 21.07. (S7): Defense-in-Depth zum Plan-Guard
                skipped.append((t, "tp_below_limit_entry")); continue
            resp = broker.submit_bracket_buy(
                t, qty=o["qty"], take_profit=o["target"], stop_loss=o["stop"],
                client_order_id=cid, limit_price=limit_px)
            led["orders"][cid] = {
                "ticker": t, "date": date, "qty": o["qty"],
                "entry_ref": o["entry_ref"], "stop": o["stop"], "target": o["target"],
                "pilot": bool(o.get("pilot")),
                "broker_order_id": resp.get("id"), "status": resp.get("status"),
                "filled_avg_price": None, "slippage_pct": None, "closed": False,
            }
            write_json(ledger_path(base), led)  # SCHWARM-FIX: persist pro Submit
            submitted.append(t)
            new_count += 1; new_notional += float(o["notional_usd"])
        except BrokerError as e:
            led["errors"].append({"date": date, "ticker": t, "err": str(e)[:200]})
            skipped.append((t, f"broker_error:{str(e)[:80]}"))

    # ── Momentum-Sleeve orders (plain buy; Trailing-Stop wird nach Fill in
    #    track() serverseitig attached) ─────────────────────────────────────
    for o in plan.get("momentum_orders", []):
        cid, t = o["client_order_id"], o["ticker"]
        if t not in approved:
            skipped.append((t, "mom_not_approved")); continue
        if cid in led["orders"]:
            skipped.append((t, "mom_already_submitted")); continue
        if t in busy:
            skipped.append((t, "mom_position_or_open_order_exists")); continue
        if _momentum_open_positions(base) >= int(cfg["momentum_max_positions"]):
            skipped.append((t, "mom_max_positions")); continue
        if len(busy) + new_count >= max_total_pos:
            skipped.append((t, "max_total_positions")); continue
        if today_prior_notional + new_notional + o["notional_usd"] > max_total_notional:
            skipped.append((t, "max_total_notional")); continue
        try:
            resp = broker.submit_market_buy(t, qty=o["qty"], client_order_id=cid)
            led["orders"][cid] = {
                "ticker": t, "date": date, "qty": o["qty"], "strategy": "momentum",
                "entry_ref": o["entry_ref"], "trail_percent": o["trail_percent"],
                "protect_pending": True,
                "broker_order_id": resp.get("id"), "status": resp.get("status"),
                "filled_avg_price": None, "slippage_pct": None, "closed": False,
            }
            submitted.append(f"{t}(MOM)")
            new_count += 1; new_notional += float(o["notional_usd"])
            write_json(ledger_path(base), led)  # persist before next submit
        except BrokerError as e:
            led["errors"].append({"date": date, "ticker": t, "err": str(e)[:200]})
            skipped.append((t, f"mom_broker_error:{str(e)[:80]}"))

    write_json(ledger_path(base), led)
    write_json(p_sig(base, f"exec_bot_orders_{date}.json"),
               {"date": date, "submitted": submitted, "skipped": skipped})
    print(f"RUN {date}: submitted={submitted or '-'} skipped={len(skipped)}")
    if submitted:
        notify("Exec-Bot: Orders raus", f"{date}: {', '.join(submitted)}", tags="rocket")
    return 0 if submitted or skipped else 2


# ── TRACK (evening) ──────────────────────────────────────────────────────────
def hard_exit_due(entry_date: str, today: str, unrealized_pct: Optional[float],
                  cfg: Dict[str, Any]) -> bool:
    """T+3 discipline: after >=3 trading days, position must show >= +1% or exit."""
    try:
        d0 = dt.date.fromisoformat(entry_date); d1 = dt.date.fromisoformat(today)
    except ValueError:
        return False
    if trading_days_between(d0, d1) < int(cfg["hard_exit_tplus"]):
        return False
    if unrealized_pct is None:
        return False  # DATA_INCOMPLETE → kein Blind-Exit (exit_engine-Konvention)
    return unrealized_pct < float(cfg["hard_exit_min_gain_pct"])


def track(base: str, date: str, cfg: Dict[str, Any],
          broker: Optional[AlpacaBroker] = None) -> int:
    if killed(base):
        print("KILLED, kein Tracking."); return 9
    broker = broker or make_broker(cfg)
    led = load_ledger(base)

    # 1) sync order fills + slippage: SCHWARM-FIX: fail-soft, ein Feed-Ausfall
    #    darf Schutz-Wächter und T+3 NIE mehr abschalten (vorher: return 2).
    try:
        broker_orders = {o.get("client_order_id"): o for o in broker.list_orders_today()}
    except BrokerError as e:
        broker_orders = {}
        led["errors"].append({"date": date, "err": f"fill_sync:{str(e)[:120]}"})
        print(f"FEED_DEGRADED (fill-sync): {e}, Wächter/T+3 laufen trotzdem.")
    for cid, rec in led["orders"].items():
        bo = broker_orders.get(cid)
        if not bo:
            continue
        rec["status"] = bo.get("status", rec["status"])
        fap = bo.get("filled_avg_price")
        if fap:
            rec["filled_avg_price"] = float(fap)
            if bo.get("filled_qty"):
                rec["filled_qty"] = int(float(bo["filled_qty"]))
            if bo.get("filled_at") and not rec.get("fill_date"):
                rec["fill_date"] = str(bo["filled_at"])[:10]  # T+3 ab FILL, nicht Submit
            if rec.get("entry_ref"):
                rec["slippage_pct"] = round(
                    (float(fap) - rec["entry_ref"]) / rec["entry_ref"] * 100, 3)
        # SCHWARM-FIX: tote Orders ohne Fill freigeben (Slot-/Wochen-Cap-Leak)
        if (str(rec.get("status")) in DEAD_ORDER_STATES
                and not rec.get("filled_avg_price") and not rec.get("closed")):
            rec["closed"] = True
            led["closed"].append({"ticker": rec["ticker"], "date": date,
                                  "why": "NEVER_FILLED"})

    # 1a-P2) EXIT-SCHATTENBUCH: Tages-Close je offener MR-Position mitschreiben
    #        (Quelle: heutiger Scan, dieselben Daten wie das System). Beim Close
    #        rechnet _shadow_math() die T+1-/T+2-Hypothesen. Read-only-Analytik.
    scan_today = load_json(p_tools(base, f"scan_{date}.json")) or {}
    tmap_today = scan_today.get("ticker_map") or {}
    for cid, rec in led["orders"].items():
        if (rec.get("closed") or not rec.get("filled_avg_price")
                or rec.get("strategy") == "momentum"):
            continue
        # SCHWARM-FIX 21.07. (F4/S8): scan_<D> trägt den Close der VORSESSION,
        # am Fill-Abend wäre das der PRE-Entry-Close. Erst ab dem Folgetag loggen;
        # dann ist path[0] der erste Close NACH Entry (korrekte T+1-Semantik).
        if str(rec.get("fill_date") or rec.get("date") or "") >= date:
            continue
        row = tmap_today.get(rec["ticker"])
        if row and row.get("close"):
            path = rec.setdefault("path", [])
            sess = str(row.get("date") or date)  # ehrliches Session-Label
            if not any(p.get("date") == sess for p in path):
                path.append({"date": sess, "close": float(row["close"])})
                del path[10:]

    # 1a-P1) STALE-ENTRY-CLEANUP: ungefüllte Limit-Entries vom VORTAG canceln.
    #        MR-Signal ist nach einem Tag stale: kein Fill = kein Trade (bewusst).
    #        Eigener Loop: darf NICHT an der broker_orders-Antwort hängen.
    for cid, rec in led["orders"].items():
        rec_d = str(rec.get("date", ""))
        # SCHWARM-FIX 21.07. (F1): auch SAME-EVENING canceln, sobald die US-Session
        # vorbei ist, sonst arbeitet das GTC-Limit die komplette Folgesession mit
        # einem 2 Tage alten Signal. Der 15:45-Track cancelt NICHT (Uhr-Guard).
        stale_due = rec_d < date or (rec_d == date and _us_market_closed_now())
        if (not rec.get("closed") and not rec.get("filled_avg_price")
                and stale_due
                and str(rec.get("status")) not in DEAD_ORDER_STATES
                and rec.get("broker_order_id")):
            try:
                broker.cancel_order(rec["broker_order_id"])
                rec["closed"] = True
                rec["status"] = "canceled"
                led["closed"].append({"ticker": rec["ticker"], "date": date,
                                      "why": "STALE_ENTRY_CANCELED"})
                print(f"STALE-ENTRY {rec['ticker']}: Vortages-Limit gecancelt "
                      "(kein Fill = kein Trade)")
            except BrokerError as e:
                led["errors"].append({"date": date, "ticker": rec["ticker"],
                                      "err": f"stale_cancel:{str(e)[:120]}"})

    # SCHWARM-FIX 21.07. (F2/S2): PARTIAL-Fill, Rest-Qty des GTC-Limit-Parents
    # canceln, sonst kann ein stales Signal Tage später "nachkaufen" (typisch im
    # Falling Knife). Der gefüllte Teil bleibt normal geschützt (Brackets/Wächter).
    for cid, rec in led["orders"].items():
        rec_d = str(rec.get("date", ""))
        if (not rec.get("closed") and rec.get("filled_avg_price")
                and rec.get("filled_qty") and rec.get("qty")
                and int(rec["filled_qty"]) < int(rec["qty"])
                and str(rec.get("status")) == "partially_filled"
                and (rec_d < date or (rec_d == date and _us_market_closed_now()))
                and not rec.get("partial_rest_canceled")
                and rec.get("broker_order_id")):
            try:
                broker.cancel_order(rec["broker_order_id"])
                rec["partial_rest_canceled"] = date
                rec["qty"] = int(rec["filled_qty"])  # ab jetzt zählt die echte Größe
                print(f"PARTIAL-REST {rec['ticker']}: Rest-Limit gecancelt, "
                      f"qty -> {rec['qty']}")
            except BrokerError as e:
                led["errors"].append({"date": date, "ticker": rec["ticker"],
                                      "err": f"partial_cancel:{str(e)[:120]}"})

    # 1b) Momentum: nach Fill den serverseitigen Trailing-Stop attachen +
    #     geschlossene Trails erkennen
    for cid, rec in led["orders"].items():
        if rec.get("strategy") != "momentum":
            continue
        bo_now = broker_orders.get(cid) or {}
        fully_filled = (bo_now.get("status") == "filled"
                        or rec.get("status") == "filled")
        # SCHWARM-FIX: nur bei VOLL-Fill attachen und mit Fill-Qty (Partial-Fill
        # hätte sonst einen Über-Verkauf/Short-Flip riskiert).
        if rec.get("protect_pending") and rec.get("filled_avg_price") and fully_filled:
            try:
                tr = broker.submit_trailing_stop_sell(
                    rec["ticker"], qty=int(rec.get("filled_qty") or rec["qty"]),
                    trail_percent=float(rec["trail_percent"]),
                    client_order_id=(cid + "-TRAIL")[:48])
                rec["protect_pending"] = False
                rec["trail_order_id"] = tr.get("id")
            except BrokerError as e:
                led["errors"].append({"date": date, "ticker": rec["ticker"],
                                      "err": f"trail_attach:{str(e)[:150]}"})
        trail_bo = broker_orders.get((cid + "-TRAIL")[:48])
        if trail_bo and trail_bo.get("status") == "filled" and not rec.get("closed"):
            rec["closed"] = True
            led["closed"].append({"ticker": rec["ticker"], "date": date,
                                  "why": "TRAIL_STOP",
                                  "exit_price": trail_bo.get("filled_avg_price")})

    # 1c) SCHUTZ-WÄCHTER (TIF-Lektion 09.07.): jede offene MR-Position MUSS eine
    #     aktive Schutz-Order haben. Day-Bracket-Legs verfallen am Handelsschluss
    #     → Wächter erkennt nackte Positionen und rüstet OCO (GTC) nach.
    try:
        # SCHWARM-FIX 21.07.: Status explizit filtern, "offen" darf NIE von der
        # API-Filterung abhängen (gefüllter/toter Sell ist KEIN Schutz mehr).
        open_sells = {o.get("symbol") for o in broker.get_open_orders()
                      if o.get("side") == "sell"
                      and str(o.get("status")) != "filled"
                      and str(o.get("status")) not in DEAD_ORDER_STATES}
        pos_syms = set()
        for pos in broker.get_positions():
            sym = pos.get("symbol")
            pos_syms.add(sym)
            if sym in open_sells:
                continue
            rec = next((r for r in led["orders"].values()
                        if r["ticker"] == sym and not r.get("closed")), None)
            if not rec:
                continue
            qty = int(float(pos.get("qty") or 0))
            if qty < 1:
                continue  # ohne belastbare Stückzahl keine Reparatur-Order
            try:
                # SCHWARM-FIX: Wächter deckt jetzt BEIDE Sleeves (vorher blind
                # für Momentum: gleiche Fehlerklasse wie die TIF-Lektion).
                if rec.get("strategy") == "momentum" and rec.get("trail_percent"):
                    broker.submit_trailing_stop_sell(
                        sym, qty=qty, trail_percent=float(rec["trail_percent"]),
                        client_order_id=f"execbot-trailfix-{date}-{sym}"[:48])
                    fixed = f"Trailing {rec['trail_percent']} % (GTC)"
                    rec["protect_pending"] = False
                elif rec.get("stop") and rec.get("target"):
                    broker.submit_oco_sell(sym, qty=qty, take_profit=rec["target"],
                                           stop_loss=rec["stop"],
                                           client_order_id=f"execbot-oco-{date}-{sym}"[:48])
                    fixed = f"OCO {rec['stop']}/{rec['target']} (GTC)"
                else:
                    continue
                rec["protection_repaired"] = date
                notify("Exec-Bot: Schutz nachgerüstet", f"{sym}: {fixed} neu gesetzt",
                       priority="high", tags="shield")
                print(f"SCHUTZ-REPARATUR {sym}: {fixed} gesetzt")
            except BrokerError as e:
                led["errors"].append({"date": date, "ticker": sym,
                                      "err": f"oco_repair:{str(e)[:150]}"})
                notify("Exec-Bot: SCHUTZ FEHLT", f"{sym} ohne Stop: Reparatur fehlgeschlagen!",
                       priority="urgent", tags="rotating_light")

        # 1d) SCHWARM-FIX: Leg-Exits zurückbuchen, Rekord gefüllt+offen, aber
        #     weder Position noch Sell-Order beim Broker ⇒ TP/SL/Trail hat
        #     geschlossen. Ohne das bricht Reconcile beim ERSTEN regulären Exit.
        for cid, rec in led["orders"].items():
            if rec.get("closed") or not rec.get("filled_avg_price"):
                continue
            sym = rec["ticker"]
            if sym in pos_syms or sym in open_sells:
                continue
            rec["closed"] = True
            why = ("TRAIL_OR_MANUAL_EXIT" if rec.get("strategy") == "momentum"
                   else "BRACKET_LEG_EXIT")
            led["closed"].append({"ticker": sym, "date": date, "why": why})
            if rec.get("strategy") != "momentum":
                # P2 (S8-Fix): echten Leg-Fill-Preis bevorzugen; Close nur Fallback
                exit_px = None
                for bo2 in broker_orders.values():
                    if (bo2.get("symbol") == sym and bo2.get("side") == "sell"
                            and bo2.get("status") == "filled"
                            and bo2.get("filled_avg_price")):
                        exit_px = float(bo2["filled_avg_price"]); break
                path = rec.get("path") or []
                if exit_px is not None:
                    _shadow_math(rec, exit_px, approx="leg_fill")
                elif path:
                    _shadow_math(rec, path[-1]["close"])
            notify("Exec-Bot: Exit erkannt", f"{sym} via Schutz-Order geschlossen ({why})",
                   tags="checkered_flag")
    except BrokerError as e:
        led["errors"].append({"date": date, "err": f"protection_check:{str(e)[:150]}"})

    # 2) T+3 hard exit, NUR MR-Positionen (Momentum darf Wochen laufen)
    # MO-CRASH-LEKTION 13.07.: get_positions crashte bei DNS-Ausfall den ganzen
    # Track → jetzt fail-soft wie Schritt 1, eine Quelle für alle Verwender.
    exits = []
    try:
        positions_now = broker.get_positions()
    except BrokerError as e:
        positions_now = []
        led["errors"].append({"date": date, "err": f"t3_positions:{str(e)[:120]}"})
        notify("Exec-Bot: Track degradiert", "Positions-Feed down: T+3 verschoben",
               priority="high", tags="warning")
    mom_open = {r["ticker"] for r in led["orders"].values()
                if r.get("strategy") == "momentum" and not r.get("closed")}
    for pos in positions_now:
        sym = pos.get("symbol")
        if sym in mom_open:
            continue
        upl = pos.get("unrealized_plpc")
        upl_pct = float(upl) * 100 if upl is not None else None
        entry_date = None
        for rec in led["orders"].values():
            if rec["ticker"] == sym and not rec.get("closed") \
                    and rec.get("strategy") != "momentum":
                # SCHWARM-FIX: T+3 zählt ab FILL-Datum (Overnight-Queue!), nicht Submit
                entry_date = rec.get("fill_date") or rec["date"]; break
        if entry_date and hard_exit_due(entry_date, date, upl_pct, cfg):
            try:
                broker.close_position(sym)
                exits.append((sym, upl_pct))
                for rec in led["orders"].values():
                    if rec["ticker"] == sym:
                        if rec.get("closed"):
                            continue  # SCHWARM-FIX (F6): Alt-Recs nie überschreiben
                        rec["closed"] = True
                        if rec.get("strategy") != "momentum":  # P2-Schattenbuch
                            _shadow_math(rec, float(pos.get("current_price") or 0) or None,
                                         approx="t3_current_price")
                led["closed"].append({"ticker": sym, "date": date,
                                      "why": "HARD_EXIT_T3", "upl_pct": upl_pct})
            except BrokerError as e:
                led["errors"].append({"date": date, "ticker": sym, "err": str(e)[:200]})

    write_json(ledger_path(base), led)

    new_fills = [f"{r['ticker']}@{r['filled_avg_price']}" for r in led["orders"].values()
                 if r.get("filled_avg_price") and not r.get("_fill_notified")]
    for r in led["orders"].values():
        if r.get("filled_avg_price"):
            r["_fill_notified"] = True
    if new_fills:
        write_json(ledger_path(base), led)
        notify("Exec-Bot: Fills", ", ".join(new_fills), tags="white_check_mark")
    if exits:
        notify("Exec-Bot: T+3-Exits", ", ".join(f"{s} ({u:+.1f}%)" for s, u in exits),
               priority="high", tags="scissors")
    if led["errors"] and led["errors"][-1].get("date") == date:
        notify("Exec-Bot: FEHLER", led["errors"][-1].get("err", "?")[:120],
               priority="urgent", tags="rotating_light")

    lines = [f"# Exec-Bot Paper-Tracking {date}", "",
             f"Offene Broker-Positionen: {len(positions_now)}",
             f"T+3-Exits heute: {', '.join(f'{s} ({u:+.2f}%)' for s, u in exits) or 'keine'}",
             f"Kumulierte Order-Fehler: {len(led['errors'])}", ""]
    os.makedirs(os.path.dirname(p_brief(base, "x")), exist_ok=True)
    with open(p_brief(base, f"Exec-Bot-{date}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"TRACK {date}: exits={len(exits)} errors_total={len(led['errors'])}")
    return 0


# ── RECONCILE + GATE REPORT ──────────────────────────────────────────────────
def reconcile(base: str, cfg: Dict[str, Any],
              broker: Optional[AlpacaBroker] = None) -> Dict[str, Any]:
    broker = broker or make_broker(cfg)
    led = load_ledger(base)
    ledger_open = {r["ticker"] for r in led["orders"].values()
                   if not r.get("closed") and r.get("status") in
                   ("filled", "partially_filled", "accepted", "new")}
    broker_open = {p.get("symbol") for p in broker.get_positions()}
    only_ledger = sorted(ledger_open - broker_open)
    only_broker = sorted(broker_open - ledger_open)
    return {"match": not only_ledger and not only_broker,
            "only_in_ledger": only_ledger, "only_at_broker": only_broker}


def _shadow_math(rec: Dict[str, Any], exit_price: float,
                 approx: str = "close_based") -> None:
    """P2: T+1-Teilgewinn- und T+2-Zeitstop-Hypothesen gegen den echten Exit
    rechnen. Close-basierte Approximation (Flag), verändert NICHTS am Trade."""
    fill = rec.get("filled_avg_price")
    path = rec.get("path") or []
    if not fill or exit_price is None:
        return
    actual = (exit_price - fill) / fill * 100
    rec["exit_price"] = round(float(exit_price), 4)
    rec["actual_ret_pct"] = round(actual, 3)
    if len(path) >= 1:
        t1 = (path[0]["close"] - fill) / fill * 100
        rec["shadow_t1_half_ret_pct"] = round(0.5 * t1 + 0.5 * actual, 3)
    if len(path) >= 2:
        rec["shadow_t2_stop_ret_pct"] = round((path[1]["close"] - fill) / fill * 100, 3)
    rec["shadow_approx"] = approx


def exit_shadow_report(base: str) -> Dict[str, Any]:
    """Aggregat der Schatten-Hypothesen über alle geschlossenen MR-Trades."""
    led = load_ledger(base)
    rows = [r for r in led["orders"].values()
            if r.get("closed") and r.get("actual_ret_pct") is not None
            and r.get("strategy") != "momentum"]
    def agg(key):
        v = [r[key] for r in rows if r.get(key) is not None]
        return {"n": len(v), "mean_ret_pct": round(sum(v) / len(v), 3)} if v else {"n": 0}
    return {"n_closed_with_shadow": len(rows),
            "actual": agg("actual_ret_pct"),
            "t2_zeitstop": agg("shadow_t2_stop_ret_pct"),
            "t1_teilgewinn": agg("shadow_t1_half_ret_pct"),
            "hinweis": "close-basierte Approximation; Entscheidungsreife ab n≈20"}


def gate_report(base: str) -> Dict[str, Any]:
    """Paper-Gate (operative Prüfpunkte). SCHWARM-FIX: MR/Momentum/Pilot getrennt
    gezählt, das Gate prüft die MR-Bracket-Ausführung, nicht einen gemischten Topf.
    Es misst nur, ob der Bot technisch sauber läuft, nicht ob er Geld verdient."""
    led = load_ledger(base)
    fills = [r for r in led["orders"].values() if r.get("filled_avg_price")]
    mr = [r for r in fills if r.get("strategy") != "momentum"]
    mom = [r for r in fills if r.get("strategy") == "momentum"]
    pil = [r for r in fills if r.get("pilot")]
    slips = [r["slippage_pct"] for r in fills if r.get("slippage_pct") is not None]
    # SCHWARM-FIX 21.07. (F8): das Gate beweist Tier-A-MR, Pilot-Fills getrennt.
    mr_ex_pilot = [r for r in mr if not r.get("pilot")]
    return {
        "fills_n": len(fills), "fills_mr": len(mr),
        "fills_mr_ex_pilot": len(mr_ex_pilot), "fills_momentum": len(mom),
        "fills_pilot": len(pil), "fills_target": 15,
        "order_errors": len(led["errors"]),
        "avg_slippage_pct": round(sum(slips) / len(slips), 3) if slips else None,
        "slippage_hinweis": "misst Fill vs. Vortags-Scan-Close = enthält Overnight-Gap; "
                            "echte Execution-Metrik (vs. Tages-Open) folgt mit G6-Wiring",
        # Operational checks only (enough filled paper orders, no order errors). Passing
        # them says NOTHING about profitability and is never a reason to trade live.
        "paper_gate_ops_checks_passed": len(mr_ex_pilot) >= 15 and not led["errors"],
        "paper_gate_note": ("Operational paper checks only. Not a profitability test and "
                            "not a reason to trade live. Not investment advice."),
        "kill_gate": {"status": "MANUAL_REVIEW: Freitags-Review wertet aus "
                                "(nicht auto-verdrahtet; Approx-Zahlen killen nicht)"},
    }


# ── SELFTEST (offline) ───────────────────────────────────────────────────────
def _mk_sandbox(tmp: str, date: str, *, gate_rc: int = 0,
                regime: str = "Trending", setups: Optional[List[dict]] = None) -> None:
    os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "signals"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "Briefings"), exist_ok=True)
    write_json(p_sig(tmp, f"adjudication_gate_{date}.json"),
               {"rc": gate_rc, "verdict": "TRADE_ALLOWED" if gate_rc == 0 else "NO_TRADE"})
    if setups is None:
        setups = [
            {"ticker": "AAA", "tier": "A", "close": 50.0, "atr_pct": 2.0, "pre_score": 8.2},
            {"ticker": "EEE", "tier": "A", "close": 60.0, "atr_pct": 2.0, "pre_score": 9.0},
            {"ticker": "SZG.DE", "tier": "B", "close": 30.0, "atr_pct": 2.0, "pre_score": 8.5},
            {"ticker": "BBB", "tier": "B", "close": 40.0, "atr_pct": 3.0, "pre_score": 7.4},
            {"ticker": "CCC", "tier": "A", "close": 20.0, "atr_pct": 2.5, "pre_score": 6.4},
            {"ticker": "DDD", "tier": "A", "close": 25.0, "atr_pct": 2.0, "pre_score": 7.9},
        ]
    def tmrow(close, sma200, w10, w40, mom5, rsi2, atr):
        return {"close": close, "sma200": sma200, "weekly_sma10": w10,
                "weekly_sma40": w40, "mom_5d": mom5, "rsi2": rsi2, "atr_pct": atr}
    ticker_map = {
        "TREND": tmrow(100.0, 80.0, 95.0, 90.0, 4.0, 30.0, 3.0),   # qualifiziert
        "HOT":   tmrow(100.0, 80.0, 95.0, 90.0, 9.0, 92.0, 3.0),   # überhitzt
        "DOWN":  tmrow(50.0, 60.0, 95.0, 90.0, 4.0, 30.0, 3.0),    # unter SMA200
        "WILD":  tmrow(100.0, 80.0, 95.0, 90.0, 4.0, 30.0, 12.0),  # ATR zu hoch
        "EEE":   tmrow(100.0, 80.0, 95.0, 90.0, 4.0, 30.0, 3.0),   # Blacklist (synthetic)
        "FOO.DE": tmrow(100.0, 80.0, 95.0, 90.0, 4.0, 30.0, 3.0),  # nicht US
        "AAA":   tmrow(50.0, 40.0, 48.0, 45.0, 4.0, 30.0, 2.0),    # schon MR-Order
    }
    write_json(p_tools(tmp, f"scan_{date}.json"),
               {"scan_date": date, "universe_size": 6, "valid_results": 6,
                "top_setups": setups, "ticker_map": ticker_map})
    write_json(p_tools(tmp, f"regime_{date}.json"),
               {"regime": regime, "size_multiplier": 1.0,
                "allowed_tiers": ["A", "B", "C"]})
    with open(p_tools(tmp, "universe.csv"), "w", encoding="utf-8") as fh:
        fh.write("ticker,name,tier,asset_class,region,sector,atr_multi_sl,atr_multi_tp,score_threshold\n")
        fh.write("AAA,TestA,A,us_large,US,Tech,1.7,2.55,7.5\n")


# Explicit, frozen config for the offline selftest. The assertions below are written
# for exactly these synthetic values, so changing DEFAULT_CONFIG or the shipped
# tools/exec_bot_config.json can never break (or silently weaken) the selftest.
SELFTEST_CONFIG_OVERRIDES: Dict[str, Any] = {
    "max_orders_per_day": 2,
    "max_positions": 3,
    "notional_usd_per_trade": 1000,
    "max_daily_notional_usd": 3000,
    "min_score": 7.0,
    "entry_type": "market",
    "entry_limit_buffer_pct": 1.0,
    "default_atr_mult_sl": 1.8,
    "default_atr_mult_tp": 2.7,
    "hard_exit_tplus": 3,
    "hard_exit_min_gain_pct": 1.0,
    "skip_regimes_mr": ["Bullish-Confirmed"],
    "pilot_tier_b_divergent": False,
    "pilot_tier_b_size_mult": 0.5,
    "pilot_tier_b_max_per_day": 1,
    "pilot_tier_b_max_trades": 10,
    "pilot_tier_b_end_date": None,
    "max_total_positions": 5,
    "max_total_notional_usd": 5000,
    "momentum_enabled": True,          # the selftest exercises the momentum sleeve
    "momentum_max_positions": 2,
    "momentum_max_new_per_week": 1,
    "momentum_notional_usd": 1000,
    "momentum_trail_atr_mult": 2.5,
    "momentum_trail_min_pct": 4.0,
    "momentum_trail_max_pct": 15.0,
    "momentum_min_mom5d": 2.0,
    "momentum_max_rsi2": 45.0,
    "momentum_max_atr_pct": 8.0,
    "momentum_min_price": 5.0,
    "tier_evidence_guard": True,
    "max_risk_per_trade_usd": 50,
    "max_portfolio_heat_usd": 150,
    "reentry_cooldown_days": 2,
}

SELFTEST_BLACKLIST = frozenset({"EEE"})  # synthetic ticker, never a real preference


def _selftest() -> int:
    """Offline selftest. Runs with a synthetic test blacklist ({"EEE"}) so the
    result does not depend on the user's config/blacklist.txt (empty by default)."""
    global USER_BLACKLIST, PERSONAL_BLACKLIST
    saved = (USER_BLACKLIST, PERSONAL_BLACKLIST)
    USER_BLACKLIST = PERSONAL_BLACKLIST = SELFTEST_BLACKLIST
    try:
        return _selftest_body()
    finally:
        USER_BLACKLIST, PERSONAL_BLACKLIST = saved


def _selftest_body() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    date = "2026-07-08"
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(SELFTEST_CONFIG_OVERRIDES)
    N = float(cfg["notional_usd_per_trade"])       # notional per trade in the test config
    R = float(cfg["max_risk_per_trade_usd"])       # risk cap per trade in the test config
    print(f"exec_bot --selftest (offline) · {VERSION}")

    with tempfile.TemporaryDirectory() as tmp:
        _mk_sandbox(tmp, date)
        plan = build_plan(tmp, date, cfg)
        tickers = [o["ticker"] for o in plan["orders"]]

        check("G1 plan verdict ORDERS_PLANNED", plan["verdict"] == "ORDERS_PLANNED")
        check("G2 blacklist filtered (EEE raus)", "EEE" not in tickers)
        check("G3 non-US filtered (SZG.DE raus)", "SZG.DE" not in tickers)
        check("G4 sub-threshold filtered (CCC 6.4 raus)", "CCC" not in tickers)
        check("G5 cap max_orders_per_day=2", len(plan["orders"]) == 2)
        check("G6 best-first (AAA zuerst)", tickers and tickers[0] == "AAA")

        o = plan["orders"][0]
        atr = 50.0 * 2.0 / 100
        check("G7 per-ticker SL-Mult (1.7 aus universe.csv)",
              abs(o["stop"] - round(50 - 1.7 * atr, 2)) < 1e-9)
        check("G7 bracket sl<entry<tp", o["stop"] < o["entry_ref"] < o["target"])
        check("G8 qty = floor(notional/close)", o["qty"] == int(N // 50))

        # G8b Earnings-Blackout mechanisch (Cap 3, damit DDD evaluiert wird)
        write_json(p_tools(tmp, f"earnings_{date}.json"),
                   {"blackout_tickers": ["DDD"]})
        cfg8b = dict(cfg); cfg8b.update({"max_orders_per_day": 3,
                                         "max_daily_notional_usd": 3 * N})
        p8b = build_plan(tmp, date, cfg8b)
        check("G8b Earnings-Blackout filtert DDD mechanisch",
              all(o["ticker"] != "DDD" for o in p8b["orders"])
              and any(r["ticker"] == "DDD" and r["why"] == "earnings_blackout"
                      for r in p8b["rejections"]))
        os.remove(p_tools(tmp, f"earnings_{date}.json"))

        # G33 Plan kennt offene Positionen + freie Slots (Session-Fund 10.07.)
        sbase = os.path.join(tmp, "m33"); _mk_sandbox(sbase, date)
        led33 = load_ledger(sbase)
        led33["orders"]["execbot-old-AAA"] = {"ticker": "AAA", "date": "2026-07-01",
                                              "qty": 1, "status": "filled",
                                              "filled_avg_price": 50.0, "closed": False}
        write_json(ledger_path(sbase), led33)
        p33 = build_plan(sbase, date, cfg)
        check("G33 offene Position AAA nicht erneut geplant",
              all(o["ticker"] != "AAA" for o in p33["orders"])
              and any(r["ticker"] == "AAA" and r["why"] == "already_open_position"
                      for r in p33["rejections"]))
        check("G33 mr_slots_free korrekt (3 Slots − 1 offen = 2)",
              p33["mr_slots_free"] == 2)

        # G9 gate rc!=0 blocks
        _mk_sandbox(tmp, "2026-07-09", gate_rc=3)
        p2 = build_plan(tmp, "2026-07-09", cfg)
        check("G9 gate rc!=0 → NO_TRADE", p2["verdict"] == "NO_TRADE"
              and "GATE_BLOCKED" in str(p2["reason"]))

        # G49 gate rc=0 + TRADE_ALLOWED_WITH_WARNINGS blockt NICHT (Bug 24.07.)
        wbase = os.path.join(tmp, "g49"); _mk_sandbox(wbase, date)
        write_json(p_sig(wbase, f"adjudication_gate_{date}.json"),
                   {"rc": 0, "verdict": "TRADE_ALLOWED_WITH_WARNINGS"})
        p49 = build_plan(wbase, date, cfg)
        check("G49 gate rc=0 WITH_WARNINGS → Plan läuft (kein GATE_BLOCKED)",
              p49["verdict"] == "ORDERS_PLANNED")

        # G50 regime artifact missing → NO_TRADE (never trade on an invented regime)
        rbase50 = os.path.join(tmp, "g50"); _mk_sandbox(rbase50, date)
        os.remove(p_tools(rbase50, f"regime_{date}.json"))
        p50 = build_plan(rbase50, date, cfg)
        check("G50 regime file missing → NO_TRADE",
              p50["verdict"] == "NO_TRADE" and "REGIME_ARTIFACT_MISSING" in str(p50["reason"]))

        # G51 regime status degraded (VIX/SPY missing) → NO_TRADE
        rbase51 = os.path.join(tmp, "g51"); _mk_sandbox(rbase51, date)
        write_json(p_tools(rbase51, f"regime_{date}.json"),
                   {"regime": "Unknown", "status": "degraded", "size_multiplier": 0.0,
                    "allowed_tiers": [], "fallback_metrics": ["vix"]})
        p51 = build_plan(rbase51, date, cfg)
        check("G51 regime degraded → NO_TRADE",
              p51["verdict"] == "NO_TRADE" and "REGIME_DEGRADED" in str(p51["reason"]))

        # G52 missing broker keys → MissingCredentialsError before any request
        _orig_rc = globals()["resolve_credentials"]
        try:
            globals()["resolve_credentials"] = lambda: {"key": "", "secret": ""}
            try:
                make_broker(cfg)
                keys_guard = False
            except MissingCredentialsError:
                keys_guard = True
        finally:
            globals()["resolve_credentials"] = _orig_rc
        check("G52 missing keys → MissingCredentialsError (no unauthenticated request)", keys_guard)

        # G10 regime hard tier: Trending-Divergent → nur Tier A
        _mk_sandbox(tmp, "2026-07-10", regime="Trending-Divergent")
        p3 = build_plan(tmp, "2026-07-10", cfg)
        check("G10 TD-Regime killt Tier B (BBB raus)",
              all(x["tier"] == "A" for x in p3["orders"]) and
              any("tier B" in r["why"] for r in p3["rejections"]))

        # G11 size_multiplier scales notional
        write_json(p_tools(tmp, f"regime_{date}.json"),
                   {"regime": "Trending", "size_multiplier": 0.5,
                    "allowed_tiers": ["A", "B", "C"]})
        p4 = build_plan(tmp, date, cfg)
        check("G11 size_multiplier 0.5 halbiert qty",
              p4["orders"][0]["qty"] == int((N * 0.5) // 50))

        # RUN-Kette mit FakeTransport
        _mk_sandbox(tmp, date)  # reset multiplier
        plan = build_plan(tmp, date, cfg)
        write_json(p_sig(tmp, f"exec_bot_plan_{date}.json"), plan)

        ft = FakeTransport()
        ft.route("GET", "/v2/positions", 200, [])
        ft.route("GET", "/v2/orders", 200, [])
        ft.route("POST", "/v2/orders", 200, {"id": "b1", "status": "accepted"})
        br = AlpacaBroker(transport=ft)

        check("G12 run ohne Approval → rc 3",
              run_orders(tmp, date, cfg, broker=br) == 3)

        write_json(p_sig(tmp, f"exec_bot_approval_{date}.json"),
                   {"approved": ["AAA"], "by": "selftest"})
        rc = run_orders(tmp, date, cfg, broker=br)
        led = load_ledger(tmp)
        check("G13 run submits approved only (AAA, nicht BBB)",
              rc == 0 and f"execbot-{date}-AAA" in led["orders"]
              and f"execbot-{date}-BBB" not in led["orders"])
        posts = [c for c in ft.calls if c[0] == "POST"]
        check("G13 genau 1 POST /v2/orders", len(posts) == 1)

        rc2 = run_orders(tmp, date, cfg, broker=br)
        posts2 = [c for c in ft.calls if c[0] == "POST"]
        check("G14 idempotent: 2. run submitted nichts Neues",
              rc2 == 0 and len(posts2) == 1)

        # G15 kill switch
        open(p_tools(tmp, KILL_FILE), "w").close()
        check("G15 kill file blockt run (rc 9)",
              run_orders(tmp, date, cfg, broker=br) == 9)
        os.remove(p_tools(tmp, KILL_FILE))

        # G34 Mitternachts-Guard (MMM-Lektion): Track um 01:09 gehört zum Vortag
        check("G34 Track 01:09 → Vortages-Datum",
              effective_track_date(dt.datetime(2026, 7, 14, 1, 9)) == "2026-07-13")
        check("G34 Track 13:00 → heutiges Datum",
              effective_track_date(dt.datetime(2026, 7, 14, 13, 0)) == "2026-07-14")

        # G16 T+3 logic (Fr→Mi = 3 Handelstage; Wochenende zählt nicht)
        check("G16 T+3 fällig bei +0.5% nach 3 Handelstagen",
              hard_exit_due("2026-07-03", "2026-07-08", 0.5, cfg) is True)
        check("G16 T+3 NICHT fällig bei +1.5%",
              hard_exit_due("2026-07-03", "2026-07-08", 1.5, cfg) is False)
        check("G16 T+2 noch nicht fällig",
              hard_exit_due("2026-07-06", "2026-07-08", 0.0, cfg) is False)
        check("G16 DATA_INCOMPLETE → kein Blind-Exit",
              hard_exit_due("2026-07-01", "2026-07-08", None, cfg) is False)

        # G17 track: fill sync + T+3 exit + briefing
        ft.route("GET", "/v2/orders", 200,
                 [{"client_order_id": f"execbot-{date}-AAA", "status": "filled",
                   "filled_avg_price": "50.25"}])
        ft.route("GET", "/v2/positions", 200,
                 [{"symbol": "AAA", "unrealized_plpc": "0.004"}])
        ft.route("DELETE", "/v2/positions/AAA", 200, {"symbol": "AAA"})
        led = load_ledger(tmp)
        led["orders"][f"execbot-{date}-AAA"]["date"] = "2026-07-03"  # 3 Handelstage her
        write_json(ledger_path(tmp), led)
        rc = track(tmp, date, cfg, broker=br)
        led = load_ledger(tmp)
        rec = led["orders"][f"execbot-{date}-AAA"]
        check("G17 fill sync + slippage 0.5%",
              rc == 0 and rec["filled_avg_price"] == 50.25
              and abs(rec["slippage_pct"] - 0.5) < 1e-6)
        check("G17 T+3-Exit ausgeführt + geloggt",
              rec["closed"] is True and led["closed"]
              and led["closed"][-1]["why"] == "HARD_EXIT_T3")
        check("G17 deutsches Briefing geschrieben",
              os.path.exists(p_brief(tmp, f"Exec-Bot-{date}.md")))

        # G39 P1: Limit-Entry, Order trägt type=limit mit Ref+1 %
        lbase = os.path.join(tmp, "m39"); _mk_sandbox(lbase, date)
        cfgl = dict(cfg); cfgl["entry_type"] = "limit"
        pl = build_plan(lbase, date, cfgl)
        write_json(p_sig(lbase, f"exec_bot_plan_{date}.json"), pl)
        write_json(p_sig(lbase, f"exec_bot_approval_{date}.json"),
                   {"approved": ["AAA"], "by": "selftest"})
        ftl = FakeTransport()
        ftl.route("GET", "/v2/positions", 200, [])
        ftl.route("GET", "/v2/orders", 200, [])
        ftl.route("POST", "/v2/orders", 200, {"id": "l1", "status": "accepted"})
        run_orders(lbase, date, cfgl, broker=AlpacaBroker(transport=ftl))
        postl = [c for c in ftl.calls if c[0] == "POST"][0][2]
        check("G39 Limit-Entry: type=limit, Preis = Ref×1.01 (50.50)",
              postl.get("type") == "limit" and postl.get("limit_price") == "50.50"
              and postl.get("time_in_force") == "gtc")

        # G40 P1: Stale-Entry-Cleanup, Vortages-Limit ohne Fill wird gecancelt
        ledl = load_ledger(lbase)
        ledl["orders"][f"execbot-{date}-AAA"]["date"] = "2026-07-07"  # gestern
        write_json(ledger_path(lbase), ledl)
        ftl.route("GET", "/v2/orders", 200, [])   # kein Fill gemeldet
        ftl.route("DELETE", "/v2/orders/l1", 204, None)
        track(lbase, date, cfgl, broker=AlpacaBroker(transport=ftl))
        ledl = load_ledger(lbase)
        deletes = [c for c in ftl.calls if c[0] == "DELETE"]
        check("G40 Stale-Entry gecancelt + als STALE_ENTRY_CANCELED gebucht",
              len(deletes) == 1
              and ledl["orders"][f"execbot-{date}-AAA"]["closed"] is True
              and any(c["why"] == "STALE_ENTRY_CANCELED" for c in ledl["closed"]))

        # G29 Schutz-Wächter: nackte MR-Position → OCO-Nachrüstung (TIF-Lektion)
        gbase = os.path.join(tmp, "m29"); _mk_sandbox(gbase, date)
        gled = load_ledger(gbase)
        gled["orders"]["execbot-2026-07-07-NAKED"] = {
            "ticker": "NAKED", "date": "2026-07-07", "qty": 5, "strategy": "mr",
            "entry_ref": 100.0, "stop": 95.0, "target": 110.0,
            "status": "filled", "filled_avg_price": 100.0, "closed": False}
        write_json(ledger_path(gbase), gled)
        ftg = FakeTransport()
        ftg.route("GET", "/v2/orders", 200, [])          # keine aktiven Orders
        ftg.route("GET", "/v2/positions", 200,
                  [{"symbol": "NAKED", "qty": "5", "unrealized_plpc": "0.02"}])
        ftg.route("POST", "/v2/orders", 200, {"id": "oco1", "status": "accepted"})
        brg = AlpacaBroker(transport=ftg)
        track(gbase, date, cfg, broker=brg)
        oco_posts = [c for c in ftg.calls if c[0] == "POST"
                     and c[2].get("order_class") == "oco"]
        gled = load_ledger(gbase)
        check("G29 Wächter rüstet OCO nach (GTC, Stop 95/Target 110)",
              len(oco_posts) == 1
              and oco_posts[0][2]["stop_loss"]["stop_price"] == "95.00"
              and oco_posts[0][2]["take_profit"]["limit_price"] == "110.00"
              and oco_posts[0][2]["time_in_force"] == "gtc")
        check("G29 Reparatur im Ledger dokumentiert",
              gled["orders"]["execbot-2026-07-07-NAKED"].get("protection_repaired") == date)

        # G41 P2: Schattenbuch, Pfad-Logging + Schatten-Mathe beim T+3-Close
        sbase41 = os.path.join(tmp, "m41"); _mk_sandbox(sbase41, date)
        led41 = load_ledger(sbase41)
        led41["orders"]["execbot-old-AAA"] = {
            "ticker": "AAA", "date": "2026-07-01", "qty": 2, "strategy": "mr",
            "entry_ref": 50.0, "stop": 48.0, "target": 55.0, "status": "filled",
            "filled_avg_price": 50.0, "closed": False}
        write_json(ledger_path(sbase41), led41)
        ft41 = FakeTransport()
        ft41.route("GET", "/v2/orders", 200, [])
        ft41.route("GET", "/v2/positions", 200,
                   [{"symbol": "AAA", "qty": "2", "unrealized_plpc": "0.004",
                     "current_price": "50.2"}])
        ft41.route("DELETE", "/v2/positions/AAA", 200, {"symbol": "AAA"})
        track(sbase41, date, cfg, broker=AlpacaBroker(transport=ft41))
        led41 = load_ledger(sbase41)
        r41 = led41["orders"]["execbot-old-AAA"]
        check("G41 Pfad geloggt + Schatten beim T+3-Close gerechnet",
              r41.get("path") and r41["path"][0]["close"] == 50.0
              and abs(r41.get("actual_ret_pct", 0) - 0.4) < 0.01
              and abs(r41.get("shadow_t1_half_ret_pct", 9) - 0.2) < 0.01)
        sh41 = exit_shadow_report(sbase41)
        check("G41 exit_shadow_report aggregiert (n=1)",
              sh41["n_closed_with_shadow"] == 1
              and sh41["t1_teilgewinn"]["n"] == 1)

        # G42 Schatten-Mathe direkt (Pfad T+1=+1 %, T+2=+2 %, Exit +3 %)
        r42 = {"filled_avg_price": 100.0, "strategy": "mr",
               "path": [{"date": "d1", "close": 101.0}, {"date": "d2", "close": 102.0}]}
        _shadow_math(r42, 103.0)
        check("G42 Schatten-Mathe: actual 3.0 / t1_half 2.0 / t2 2.0",
              abs(r42["actual_ret_pct"] - 3.0) < 1e-9
              and abs(r42["shadow_t1_half_ret_pct"] - 2.0) < 1e-9
              and abs(r42["shadow_t2_stop_ret_pct"] - 2.0) < 1e-9)

        # ── SCHWARM-FIX-RUNDE 21.07. ────────────────────────────────────────
        # G43 Partial-Fill: Rest-Limit am Folgetag gecancelt, qty → filled_qty
        sbase43 = os.path.join(tmp, "m43"); _mk_sandbox(sbase43, date)
        led43 = load_ledger(sbase43)
        led43["orders"]["execbot-p-AAA"] = {
            "ticker": "AAA", "date": "2026-07-01", "qty": 5, "strategy": "mr",
            "entry_ref": 50.0, "stop": 48.0, "target": 55.0,
            "status": "partially_filled", "filled_avg_price": 50.0,
            "filled_qty": 3, "closed": False, "broker_order_id": "bo43"}
        write_json(ledger_path(sbase43), led43)
        ft43 = FakeTransport()
        ft43.route("GET", "/v2/orders", 200, [])
        ft43.route("GET", "/v2/positions", 200,
                   [{"symbol": "AAA", "qty": "3", "unrealized_plpc": "0.02",
                     "current_price": "51.0"}])
        ft43.route("DELETE", "/v2/orders/bo43", 200, {})
        ft43.route("POST", "/v2/orders", 200, {"id": "oco43", "status": "accepted"})
        track(sbase43, date, cfg, broker=AlpacaBroker(transport=ft43))
        r43 = load_ledger(sbase43)["orders"]["execbot-p-AAA"]
        check("G43 Partial-Rest gecancelt + qty auf filled_qty reduziert",
              r43.get("partial_rest_canceled") == date and r43["qty"] == 3
              and not r43.get("closed"))

        # G44 Cooldown ignoriert Cancel-Buchungen (F5/S1)
        sbase44 = os.path.join(tmp, "m44"); _mk_sandbox(sbase44, date)
        led44 = load_ledger(sbase44)
        led44["closed"] = [{"ticker": "AAA", "date": date,
                            "why": "STALE_ENTRY_CANCELED"}]
        write_json(ledger_path(sbase44), led44)
        p44 = build_plan(sbase44, date, cfg)
        check("G44 STALE/NEVER_FILLED lösen KEINEN Re-Entry-Cooldown aus",
              not any(r["why"] == "reentry_cooldown" for r in p44["rejections"])
              and any(o["ticker"] == "AAA" for o in p44["orders"]))

        # G45 Mini-ATR: Limit-Entry >= TP wird im Plan rejected (S7)
        sbase45 = os.path.join(tmp, "m45")
        _mk_sandbox(sbase45, date, setups=[{"ticker": "AAA", "pre_score": 8.0,
                    "tier": "A", "close": 5.00, "atr_pct": 0.2}])
        cfg45 = dict(cfg); cfg45["entry_type"] = "limit"
        p45 = build_plan(sbase45, date, cfg45)
        check("G45 tp_below_limit_entry rejected (Mini-ATR)",
              any(str(r["why"]).startswith("tp_below_limit_entry")
                  for r in p45["rejections"]) and not p45["orders"])

        # G46 Same-Evening-Stale: Cancel NUR nach US-Close (F1)
        def _mk46(tag):
            sb = os.path.join(tmp, tag); _mk_sandbox(sb, date)
            led_ = load_ledger(sb)
            led_["orders"]["execbot-s-AAA"] = {
                "ticker": "AAA", "date": date, "qty": 2, "strategy": "mr",
                "status": "new", "filled_avg_price": None, "closed": False,
                "broker_order_id": "bo46"}
            write_json(ledger_path(sb), led_)
            ft_ = FakeTransport()
            ft_.route("GET", "/v2/orders", 200, [])
            ft_.route("GET", "/v2/positions", 200, [])
            ft_.route("DELETE", "/v2/orders/bo46", 200, {})
            return sb, ft_
        _orig_uc = globals()["_us_market_closed_now"]
        try:
            globals()["_us_market_closed_now"] = lambda: False
            sb46a, ft46a = _mk46("m46a")
            track(sb46a, date, cfg, broker=AlpacaBroker(transport=ft46a))
            open_kept = not load_ledger(sb46a)["orders"]["execbot-s-AAA"].get("closed")
            globals()["_us_market_closed_now"] = lambda: True
            sb46b, ft46b = _mk46("m46b")
            track(sb46b, date, cfg, broker=AlpacaBroker(transport=ft46b))
            led46b = load_ledger(sb46b)
            closed_after = (led46b["orders"]["execbot-s-AAA"].get("closed")
                            and any(c["why"] == "STALE_ENTRY_CANCELED"
                                    for c in led46b["closed"]))
        finally:
            globals()["_us_market_closed_now"] = _orig_uc
        check("G46 Same-Evening-Stale: intraday NEIN, nach US-Close JA",
              open_kept and closed_after)

        # G47 validate_config: Panel-Keys + Unknown-Key-Guard (F3)
        bad47 = dict(DEFAULT_CONFIG)
        bad47["max_risk_per_trade_usd"] = 950
        bad47["entry_typ"] = "limit"  # Tippfehler
        e47 = validate_config(bad47)
        check("G47 Risk-Range + unbekannter Key werden geblockt",
              any("max_risk_per_trade_usd" in x for x in e47)
              and any("unbekannte" in x for x in e47)
              and validate_config(dict(DEFAULT_CONFIG)) == [])

        # G48 Pilot-Halbierung skaliert auch das Risk-Cap (F7)
        sbase48 = os.path.join(tmp, "m48")
        _mk_sandbox(sbase48, date, regime="Trending-Divergent",
                    setups=[{"ticker": "BB1", "tier": "B", "close": 40.0,
                             "atr_pct": 10.0, "pre_score": 7.9}])
        pcfg48 = dict(cfg); pcfg48.update({"pilot_tier_b_divergent": True})
        p48 = build_plan(sbase48, date, pcfg48)
        o48 = next((o for o in p48["orders"] if o["ticker"] == "BB1"), None)
        # stop_dist = 40×10%×1.8 = 7.2$ → voll floor(R/7.2), halb floor((R/2)/7.2)
        full48, half48 = int(R // 7.2), int((R / 2) // 7.2)
        check(f"G48 Pilot: Risk-Cap halbiert (qty {half48} statt {full48})",
              o48 is not None and o48["pilot"] is True and o48["qty"] == half48
              and half48 < full48)

        # G18 reconcile mismatch
        ft.route("GET", "/v2/positions", 200, [{"symbol": "ZZZ"}])
        rec18 = reconcile(tmp, cfg, broker=br)
        check("G18 reconcile erkennt Divergenz (ZZZ nur beim Broker)",
              rec18["match"] is False and rec18["only_at_broker"] == ["ZZZ"])

        # G19 gate report shape
        gr = gate_report(tmp)
        check("G19 gate report zählt fills + errors",
              gr["fills_n"] == 1 and gr["order_errors"] == 0
              and gr["paper_gate_ops_checks_passed"] is False)

        # ── Momentum + Skip-Cohort (Sleeve T) ────────────────────────────────
        # G20: Bullish-Confirmed → MR komplett geskippt (dokumentierte §3-Regel)
        mbase = os.path.join(tmp, "m20"); _mk_sandbox(mbase, date, regime="Bullish-Confirmed")
        cfg_nomom = dict(cfg); cfg_nomom["momentum_enabled"] = False
        p20 = build_plan(mbase, date, cfg_nomom)
        check("G20 BC-Skip: alle MR-Kandidaten verworfen",
              p20["verdict"] == "NO_TRADE" and
              all("skip_cohort_mr" in r["why"] for r in p20["rejections"]))

        # G21: Momentum-Kandidaten-Auswahl
        mbase = os.path.join(tmp, "m21"); _mk_sandbox(mbase, date)
        p21 = build_plan(mbase, date, cfg)
        mo = p21.get("momentum_orders", [])
        check("G21 Momentum: genau TREND qualifiziert",
              [m["ticker"] for m in mo] == ["TREND"])
        check("G21 Trail = 2.5×ATR geclampt (7.5%)",
              mo and abs(mo[0]["trail_percent"] - 7.5) < 1e-9)
        check("G21 HOT/DOWN/WILD/EEE/FOO.DE/AAA ausgeschlossen",
              all(m["ticker"] not in ("HOT", "DOWN", "WILD", "EEE", "FOO.DE", "AAA")
                  for m in mo))

        # G22: Wochen-Cap, vorhandener Momentum-Eintrag diese Woche blockt
        led22 = load_ledger(mbase)
        led22["orders"]["execbot-2026-07-06-XYZ-MOM"] = {
            "ticker": "XYZ", "date": "2026-07-06", "qty": 1, "strategy": "momentum",
            "closed": False}
        write_json(ledger_path(mbase), led22)
        p22 = build_plan(mbase, date, cfg)
        check("G22 max 1 neuer Momentum-Entry pro Woche",
              p22.get("momentum_orders") == [])

        # G23: Run submitted Momentum als plain Market-Buy
        mbase = os.path.join(tmp, "m23"); _mk_sandbox(mbase, date)
        p23 = build_plan(mbase, date, cfg)
        write_json(p_sig(mbase, f"exec_bot_plan_{date}.json"), p23)
        write_json(p_sig(mbase, f"exec_bot_approval_{date}.json"),
                   {"approved": ["TREND"], "by": "selftest"})
        ft2 = FakeTransport()
        ft2.route("GET", "/v2/positions", 200, [])
        ft2.route("GET", "/v2/orders", 200, [])
        ft2.route("POST", "/v2/orders", 200, {"id": "m1", "status": "accepted"})
        br2 = AlpacaBroker(transport=ft2)
        rc23 = run_orders(mbase, date, cfg, broker=br2)
        posts23 = [c for c in ft2.calls if c[0] == "POST"]
        led23 = load_ledger(mbase)
        cid_t = f"execbot-{date}-TREND-MOM"
        check("G23 Momentum-Buy submitted (market, ohne Bracket)",
              rc23 == 0 and len(posts23) == 1
              and posts23[0][2].get("type") == "market"
              and "order_class" not in posts23[0][2])
        check("G23 Ledger: strategy=momentum, protect_pending",
              led23["orders"][cid_t]["strategy"] == "momentum"
              and led23["orders"][cid_t]["protect_pending"] is True)

        # G24: Track attached Trailing-Stop nach Fill; kein T+3 für Momentum
        ft2.route("GET", "/v2/orders", 200,
                  [{"client_order_id": cid_t, "status": "filled",
                    "filled_avg_price": "100.50"}])
        ft2.route("GET", "/v2/positions", 200,
                  [{"symbol": "TREND", "unrealized_plpc": "0.004"}])
        led23["orders"][cid_t]["date"] = "2026-07-01"  # längst über T+3
        write_json(ledger_path(mbase), led23)
        rc24 = track(mbase, date, cfg, broker=br2)
        led24 = load_ledger(mbase)
        trail_posts = [c for c in ft2.calls if c[0] == "POST"
                       and c[2].get("type") == "trailing_stop"]
        check("G24 Trailing-Stop serverseitig attached (7.5%, GTC)",
              rc24 == 0 and len(trail_posts) == 1
              and trail_posts[0][2]["trail_percent"] == "7.5"
              and led24["orders"][cid_t]["protect_pending"] is False)
        check("G24 KEIN T+3-Exit auf Momentum trotz +0.4% nach T+5",
              not any(c["ticker"] == "TREND" and c["why"] == "HARD_EXIT_T3"
                      for c in led24["closed"]))

        # G24b: Trail-Fill wird als TRAIL_STOP-Close erkannt
        ft2.route("GET", "/v2/orders", 200,
                  [{"client_order_id": cid_t, "status": "filled",
                    "filled_avg_price": "100.50"},
                   {"client_order_id": (cid_t + "-TRAIL")[:48], "status": "filled",
                    "filled_avg_price": "108.20"}])
        ft2.route("GET", "/v2/positions", 200, [])
        track(mbase, date, cfg, broker=br2)
        led24b = load_ledger(mbase)
        check("G24b Trail-Fill → closed why=TRAIL_STOP",
              led24b["orders"][cid_t]["closed"] is True
              and any(c["why"] == "TRAIL_STOP" for c in led24b["closed"]))

        # ── M5-Pilot (Tier B auf divergenten Regimes) + M6-Cap ───────────────
        pcfg = dict(cfg); pcfg.update({"pilot_tier_b_divergent": True,
                                       "max_orders_per_day": 4,
                                       "max_daily_notional_usd": 4 * N})
        setups_b = [
            {"ticker": "AXA", "tier": "A", "close": 50.0, "atr_pct": 2.0, "pre_score": 8.0},
            {"ticker": "BB1", "tier": "B", "close": 40.0, "atr_pct": 2.0, "pre_score": 7.9},
            {"ticker": "BB2", "tier": "B", "close": 40.0, "atr_pct": 2.0, "pre_score": 7.5},
            {"ticker": "CC1", "tier": "C", "close": 20.0, "atr_pct": 2.0, "pre_score": 8.8},
        ]
        pbase = os.path.join(tmp, "m25")
        _mk_sandbox(pbase, date, regime="Trending-Divergent", setups=setups_b)
        p25 = build_plan(pbase, date, pcfg)
        picked = {o["ticker"]: o for o in p25["orders"]}
        check("G25 Pilot aktiv: Tier A + genau 1 Tier-B (Tages-Cap)",
              "AXA" in picked and "BB1" in picked and "BB2" not in picked
              and any("pilot_daily_cap" in r["why"] for r in p25["rejections"]))
        check("G25 Tier C bleibt draußen trotz Pilot",
              "CC1" not in picked
              and any(r["ticker"] == "CC1" and "tier C" in r["why"]
                      for r in p25["rejections"]))
        check("G25 Pilot-Order: halbe Size + Flag",
              picked["BB1"]["pilot"] is True
              and picked["BB1"]["qty"] == int((N * 0.5) // 40)
              and picked["AXA"]["pilot"] is False
              and picked["AXA"]["qty"] == int(N // 50))

        # G26 Selbstabschaltung nach max_trades
        led26 = load_ledger(pbase)
        for i in range(10):
            led26["orders"][f"x{i}"] = {"ticker": f"T{i}", "date": "2026-07-01",
                                        "qty": 1, "pilot": True, "closed": True,
                                        "filled_avg_price": 100.0}  # gefüllte Pilots zählen
        write_json(ledger_path(pbase), led26)
        p26 = build_plan(pbase, date, pcfg)
        check("G26 Pilot stoppt sich nach 10 Trades selbst",
              p26["pilot_tier_b_active"] is False
              and all(not o.get("pilot") for o in p26["orders"]))

        # G27 End-Datum überschritten → Pilot aus
        pcfg27 = dict(pcfg); pcfg27["pilot_tier_b_end_date"] = "2026-07-01"
        pbase27 = os.path.join(tmp, "m27")
        _mk_sandbox(pbase27, date, regime="Trending-Divergent", setups=setups_b)
        p27 = build_plan(pbase27, date, pcfg27)
        check("G27 nach End-Datum: nur noch Tier A",
              p27["pilot_tier_b_active"] is False
              and [o["ticker"] for o in p27["orders"]] == ["AXA"])

        # G28 M6: Cap 3 lässt dritten Kandidaten durch
        cfg28 = dict(cfg); cfg28.update({"max_orders_per_day": 3,
                                         "max_daily_notional_usd": 3 * N})
        cbase = os.path.join(tmp, "m28"); _mk_sandbox(cbase, date)
        p28 = build_plan(cbase, date, cfg28)
        check("G28 Cap 3, aber Evidenz-Guard filtert B → [AAA, DDD]",
              [o["ticker"] for o in p28["orders"]] == ["AAA", "DDD"]
              and any("tier_not_evidenced" in r["why"] for r in p28["rejections"]))

        # ── PANEL-REGELN 20.07. ──────────────────────────────────────────────
        # G35 Evidenz-Guard abschaltbar (nur explizit)
        cfg35 = dict(cfg28); cfg35["tier_evidence_guard"] = False
        p35 = build_plan(cbase, date, cfg35)
        check("G35 Guard aus → B wieder zugelassen (bewusste Entscheidung nötig)",
              "BBB" in [o["ticker"] for o in p35["orders"]])

        # G36 Risiko-Sizing: hoher ATR → qty über Stop-Distanz gedeckelt
        rbase = os.path.join(tmp, "m36")
        _mk_sandbox(rbase, date, setups=[
            {"ticker": "WILD2", "tier": "A", "close": 100.0, "atr_pct": 10.0,
             "pre_score": 8.5}])
        p36 = build_plan(rbase, date, cfg)
        o36 = p36["orders"][0]
        # stop_dist = 100 × 10% × 1.8 = 18$ → qty_risk = floor(R/18) < floor(N/100)
        q36 = int(R // 18.0)
        check(f"G36 Risiko-Sizing: qty {q36} statt {int(N // 100)}, Risiko ≈ {q36 * 18}$ ≤ {R:.0f}$",
              q36 < int(N // 100) and o36["qty"] == q36
              and abs(o36["risk_usd"] - q36 * 18.0) < 0.5)

        # G37 Heat-Limit: zweiter Trade überschreitet Portfolio-Heat
        cfg37 = dict(cfg); cfg37.update({"max_orders_per_day": 3,
                                         "max_daily_notional_usd": 3 * N,
                                         "max_portfolio_heat_usd": 60.0})
        p37 = build_plan(cbase, date, cfg37)
        check("G37 Heat-Limit: max 1 Trade bei 60$-Deckel",
              len(p37["orders"]) == 1
              and any("heat_limit" in r["why"] for r in p37["rejections"]))

        # G38 Re-Entry-Cooldown nach Close/Stop-Out
        led38 = load_ledger(cbase)
        led38.setdefault("closed", []).append(
            {"ticker": "AAA", "date": date, "why": "BRACKET_LEG_EXIT"})
        write_json(ledger_path(cbase), led38)
        p38 = build_plan(cbase, date, cfg28)
        check("G38 Cooldown: AAA nach Stop-Out 2 Tage gesperrt",
              all(o["ticker"] != "AAA" for o in p38["orders"])
              and any(r["why"] == "reentry_cooldown" for r in p38["rejections"]))

        # ── Schwarm-Fixes ────────────────────────────────────────────────────
        # G30 flock: zweiter "Prozess" bekommt das Lock nicht
        got = acquire_lock(tmp)
        fh2 = open(p_tools(tmp, "exec_bot.lock"), "w")
        try:
            fcntl.flock(fh2, fcntl.LOCK_EX | fcntl.LOCK_NB); second = True
        except OSError:
            second = False
        fh2.close()
        check("G30 flock exklusiv (2. Zugriff blockiert)", got is True and second is False)
        fcntl.flock(_LOCK_FH, fcntl.LOCK_UN)

        # G31 Config-Validierung fängt 10x-Tippfehler
        bad = dict(DEFAULT_CONFIG); bad["notional_usd_per_trade"] = 160000
        errs31 = validate_config(bad)
        check("G31 validate_config: 160000$-Tippfehler erkannt, Defaults sauber",
              any("notional_usd_per_trade" in e for e in errs31)
              and validate_config(dict(DEFAULT_CONFIG)) == [])

        # G32 Gesamt-Positions-Deckel über beide Sleeves
        tbase = os.path.join(tmp, "m32"); _mk_sandbox(tbase, date)
        cfg32 = dict(cfg); cfg32.update({"max_orders_per_day": 3,
                                         "max_daily_notional_usd": 3 * N,
                                         "max_positions": 5,
                                         "max_total_positions": 3})
        p32 = build_plan(tbase, date, cfg32)
        write_json(p_sig(tbase, f"exec_bot_plan_{date}.json"), p32)
        write_json(p_sig(tbase, f"exec_bot_approval_{date}.json"),
                   {"approved": [o["ticker"] for o in p32["orders"]], "by": "selftest"})
        ft32 = FakeTransport()
        ft32.route("GET", "/v2/positions", 200, [{"symbol": "X1"}, {"symbol": "X2"}])
        ft32.route("GET", "/v2/orders", 200, [])
        ft32.route("POST", "/v2/orders", 200, {"id": "t1", "status": "accepted"})
        rc32 = run_orders(tbase, date, cfg32, broker=AlpacaBroker(transport=ft32))
        posts32 = [c for c in ft32.calls if c[0] == "POST"]
        led32_orders = json.load(open(p_sig(tbase, f"exec_bot_orders_{date}.json")))
        check("G32 max_total_positions deckelt über Sleeves (1 Submit, Rest geskippt)",
              rc32 == 0 and len(posts32) == 1
              and any(s[1] == "max_total_positions" for s in led32_orders["skipped"]))

    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: List[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    try:
        return _main(argv)
    except MissingCredentialsError as e:
        print(f"KEYS_MISSING: {e}. Copy tools/.env.example to tools/.env and add your "
              f"PAPER keys (rc {RC_KEYS_MISSING}).")
        return RC_KEYS_MISSING
    except BrokerError as e:
        print(f"BROKER_ERROR: {str(e)[:300]} (rc 1). Check keys, network and the endpoint.")
        return 1


def _main(argv: List[str]) -> int:

    base = repo_base()
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    date = (effective_track_date(dt.datetime.now()) if "--track" in argv
            else today_str())
    if "--date" in argv:
        date = argv[argv.index("--date") + 1]
    cfg = load_config(base)
    if str(cfg.get("base_url") or PAPER_BASE_URL).rstrip("/") != PAPER_BASE_URL \
            or str(cfg.get("mode", "paper")).lower() != "paper":
        # Non-blocking: only informs. The double live lock in broker_alpaca stays in force.
        warn_live_mode(str(cfg.get("base_url") or ""))

    # SCHWARM-FIX: exklusives Lock für alle zustandsändernden Modi
    if any(f in argv for f in ("--run", "--track", "--kill")) and not acquire_lock(base):
        print("LOCKED: anderer exec_bot-Lauf aktiv, Abbruch, kein Doppel-Zugriff (rc 8).")
        return 8

    if "--kill" in argv:
        # SCHWARM-FIX 09.07.: Alte Semantik stornierte ALLE Orders (auch Schutz-
        # Legs), schloss aber keine Position → Notaus erzeugte nackte Positionen
        # und blockierte via KILL-Datei zugleich die Wächter-Reparatur.
        # Neue Semantik: KILL = FLATTEN. Erst alle Positionen schließen (cancelt
        # je Symbol die Legs mit), dann Rest-Orders stornieren, dann sperren.
        flat, probs = [], []
        try:
            br = make_broker(cfg)
            for pos in br.get_positions():
                sym = pos.get("symbol")
                try:
                    br.close_position(sym); flat.append(sym)
                except BrokerError as e:
                    probs.append(f"{sym}:{str(e)[:60]}")
            br.cancel_all_orders()
        except (BrokerError, Exception) as e:  # noqa: BLE001
            probs.append(f"broker:{str(e)[:80]}")
        open(p_tools(base, KILL_FILE), "w").close()
        msg = (f"FLAT: {', '.join(flat) or 'keine Positionen'}"
               + (f" | PROBLEME: {'; '.join(probs)}: MANUELL PRÜFEN!" if probs else ""))
        notify("Exec-Bot: KILL = FLATTEN", msg, priority="urgent", tags="octagonal_sign")
        print(f"KILL gesetzt. {msg}")
        return 0 if not probs else 1

    if "--plan" in argv:
        plan = build_plan(base, date, cfg)
        write_json(p_sig(base, f"exec_bot_plan_{date}.json"), plan)
        print(json.dumps({k: plan[k] for k in
                          ("date", "verdict", "reason", "regime", "orders")
                          if k in plan}, indent=1, ensure_ascii=False))
        return 0 if plan["verdict"] == "ORDERS_PLANNED" else 2

    if "--approve" in argv:
        if "--tickers" not in argv:
            print("--approve braucht --tickers T1,T2"); return 1
        tick = [t.strip().upper() for t in argv[argv.index("--tickers") + 1].split(",") if t.strip()]
        by = (argv[argv.index("--by") + 1] if "--by" in argv
              else (os.environ.get("USER") or "operator"))
        write_json(p_sig(base, f"exec_bot_approval_{date}.json"),
                   {"date": date, "approved": tick, "by": by,
                    "ts": dt.datetime.now().isoformat(timespec="seconds")})
        print(f"Freigabe gespeichert: {tick}")
        return 0

    if "--run" in argv:
        return run_orders(base, date, cfg)
    if "--track" in argv:
        return track(base, date, cfg)
    if "--reconcile" in argv:
        print(json.dumps(reconcile(base, cfg), indent=1)); return 0
    if "--gate-report" in argv:
        print(json.dumps(gate_report(base), indent=1)); return 0
    if "--exit-shadow" in argv:
        print(json.dumps(exit_shadow_report(base), indent=1, ensure_ascii=False)); return 0
    if "--status" in argv:
        led = load_ledger(base)
        print(json.dumps({"orders": len(led["orders"]),
                          "closed": len(led["closed"]),
                          "errors": len(led["errors"]),
                          "gate": gate_report(base),
                          "killed": killed(base)}, indent=1))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
