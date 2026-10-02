#!/usr/bin/env python3
"""
EXIT-ENGINE-1: Deterministic Exit-Decision Engine (V7.11, Feature, Shadow-safe).

Single Source of Truth fuer die Exit-Logik des V6.1-Connors-MR-Systems.

Bisher lebte die Exit-Prioritaet nur als Prosa in CLAUDE.md (Sec. 3 + 7) und
wurde im 20:00-Abend-Tracking ad-hoc angewandt. Dieses Modul kapselt die exakt
dokumentierte Prioritaet in EINER getesteten Funktion, die das Abend-Tracking
(und spaeter der selbstlaufende V10+-Loop) per Import aufrufen kann.

Dokumentierte Exit-Prioritaet (CLAUDE.md Sec. 7):
    Connors-SMA5  ->  ATR-TP  ->  ATR-SL  ->  Hard-Exit-T+3

Regeln (CLAUDE.md Sec. 3):
    * Connors-SMA5  : Long-MR-Ziel erreicht, wenn close >= SMA5 (Mean-Reversion
                      back-to-mean).
    * ATR-TP        : close >= entry + atr_multi_tp * ATR  (Default-Konvention
                      build_universe: atr_multi_tp = 1.5 * atr_multi_sl).
    * ATR-SL        : close <= entry - atr_multi_sl * ATR.
    * Hard-Exit-T+3 : Wenn Trade nach >= 3 Trading-Tagen NICHT >= +1.0% ->
                      Exit naechster Open. KEINE Ausnahmen.

Design:
    * Long-only (System ist Long-MR; Hebelprodukte sind long-the-instrument).
    * EOD-Daily: jeder Exit wird am naechsten Open ausgefuehrt (execution=next_open).
    * Es werden ALLE getriggerten Bedingungen geloggt (`triggers`), die finale
      `action`/`reason` folgt strikt der dokumentierten Prioritaet (Tie-Break).
    * Shadow-safe: reine Entscheidungs-Funktion, kein Trade, kein DB-Write.
      Journal wird ausschliesslich read-only (mode=ro) gelesen.

Benutzung:
    python3 tools/exit_engine.py --selftest
    python3 tools/exit_engine.py --from-journal --ticker AAA --as-of 2024-01-05 \
        --closes 100,99.5,100.2,100.4
    python3 tools/exit_engine.py --entry 100.00 --last-close 100.40 \
        --sl 95.00 --tp 107.50 --holding-days 3 --sma5 102.0

Feature-Freeze-konform: kein Scoring-Change, kein Live-Order.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

try:
    import numpy as np
    _HAVE_NP = True
except Exception:  # pragma: no cover - numpy is present in repo env
    _HAVE_NP = False

# ----------------------------------------------------------------------------
# Konstanten (dokumentiert in CLAUDE.md Sec. 3)
# ----------------------------------------------------------------------------
HARD_EXIT_MIN_RET_PCT = 1.0      # "nicht >= +1%"
HARD_EXIT_MAX_HOLD = 3           # "nach 3 Trading-Tagen"
DEFAULT_ATR_MULT_SL = 2.0        # build_universe default
DEFAULT_TP_OVER_SL = 1.5         # atr_multi_tp = 1.5 * atr_multi_sl

# Prioritaets-Rang (kleiner = hoeher). Folgt CLAUDE.md Sec. 7.
PRIORITY = {
    "connors_sma5": 1,
    "atr_tp": 2,
    "atr_sl": 3,
    "hard_exit_t3": 4,
    "hold": 99,
}


@dataclass
class ExitVerdict:
    action: str                       # "HOLD" | "EXIT"
    reason: str                       # connors_sma5 | atr_tp | atr_sl | hard_exit_t3 | hold
    priority_rank: int
    execution: str                    # "next_open" | "n/a"
    ticker: str
    as_of: str
    entry_price: float
    last_close: float
    return_pct: float
    trading_days_held: Optional[int]
    sma5: Optional[float]
    tp: Optional[float]
    sl: Optional[float]
    triggers: list = field(default_factory=list)
    note: str = ""
    inputs_echo: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------------
def sma(values: Sequence[float], window: int = 5) -> Optional[float]:
    """Simple Moving Average der letzten `window` Werte. None falls zu kurz."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < window:
        return None
    tail = vals[-window:]
    return sum(tail) / float(window)


def trading_days_between(entry_date: str, as_of_date: str) -> Optional[int]:
    """Anzahl Handelstage [entry, as_of) (Mo-Fr, ohne Feiertage-Kalender).

    Konvention: Entry Mo 01.06, as_of Do 04.06 -> 3 (Mo,Di,Mi) == T+3-Checkpoint.
    """
    if not entry_date or not as_of_date:
        return None
    try:
        if _HAVE_NP:
            return int(np.busday_count(entry_date, as_of_date))
        # Fallback ohne numpy: simple Wochentags-Zaehlung
        d0 = datetime.fromisoformat(entry_date).date()
        d1 = datetime.fromisoformat(as_of_date).date()
        if d1 <= d0:
            return 0
        n, cur = 0, d0
        from datetime import timedelta
        while cur < d1:
            if cur.weekday() < 5:
                n += 1
            cur += timedelta(days=1)
        return n
    except Exception:
        return None


def derive_tp_sl(entry_price: float,
                 atr: Optional[float],
                 atr_multi_sl: float = DEFAULT_ATR_MULT_SL,
                 atr_multi_tp: Optional[float] = None) -> tuple[Optional[float], Optional[float]]:
    """Leite TP/SL aus Entry + ATR + Multiplikatoren ab (Long)."""
    if atr is None or atr <= 0:
        return None, None
    if atr_multi_tp is None:
        atr_multi_tp = atr_multi_sl * DEFAULT_TP_OVER_SL
    sl = entry_price - atr_multi_sl * atr
    tp = entry_price + atr_multi_tp * atr
    return tp, sl


def evaluate_exit(*,
                  ticker: str,
                  entry_price: float,
                  last_close: float,
                  sma5: Optional[float] = None,
                  tp: Optional[float] = None,
                  sl: Optional[float] = None,
                  trading_days_held: Optional[int] = None,
                  as_of: Optional[str] = None,
                  side: str = "long",
                  max_hold: int = HARD_EXIT_MAX_HOLD,
                  hard_exit_min_ret_pct: float = HARD_EXIT_MIN_RET_PCT) -> ExitVerdict:
    """Bewerte EINE offene Long-Position nach dokumentierter Exit-Prioritaet.

    Gibt ExitVerdict mit action/reason (hoechste Prioritaet) + voller Trigger-Liste.
    """
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Daten-Integritaets-Guard: ohne validen Entry-Preis ist KEINE Return-/Stop-
    # Rechnung moeglich. Lieber ehrlich DATA_INCOMPLETE als ein irrefuehrendes
    # hard_exit_t3 (z. B. Journal-Row mit position_size gesetzt, entry_price NULL).
    if not entry_price or entry_price <= 0 or not last_close or last_close <= 0:
        return ExitVerdict(
            action="HOLD", reason="data_incomplete", priority_rank=98,
            execution="n/a", ticker=ticker, as_of=as_of,
            entry_price=entry_price or 0.0, last_close=last_close or 0.0,
            return_pct=0.0, trading_days_held=trading_days_held,
            sma5=sma5, tp=tp, sl=sl,
            note=("Entry/Close unvollstaendig -> kein belastbares Exit-Verdikt. "
                  "Journal-Row backfillen (entry_price/stop/target)."),
        )

    ret_pct = (last_close - entry_price) / entry_price * 100.0 if entry_price else 0.0

    if side != "long":
        return ExitVerdict(
            action="HOLD", reason="unsupported_side", priority_rank=99,
            execution="n/a", ticker=ticker, as_of=as_of, entry_price=entry_price,
            last_close=last_close, return_pct=round(ret_pct, 4),
            trading_days_held=trading_days_held, sma5=sma5, tp=tp, sl=sl,
            note=f"side={side} nicht unterstuetzt (System ist long-only).",
        )

    triggers: list[dict] = []

    # 1) Connors-SMA5: Mean-Reversion-Ziel erreicht
    if sma5 is not None and last_close >= sma5:
        triggers.append({"rule": "connors_sma5", "rank": PRIORITY["connors_sma5"],
                         "detail": f"close {last_close:.4f} >= SMA5 {sma5:.4f}"})
    # 2) ATR-TP
    if tp is not None and last_close >= tp:
        triggers.append({"rule": "atr_tp", "rank": PRIORITY["atr_tp"],
                         "detail": f"close {last_close:.4f} >= TP {tp:.4f}"})
    # 3) ATR-SL
    if sl is not None and last_close <= sl:
        triggers.append({"rule": "atr_sl", "rank": PRIORITY["atr_sl"],
                         "detail": f"close {last_close:.4f} <= SL {sl:.4f}"})
    # 4) Hard-Exit-T+3
    if (trading_days_held is not None and trading_days_held >= max_hold
            and ret_pct < hard_exit_min_ret_pct):
        triggers.append({"rule": "hard_exit_t3", "rank": PRIORITY["hard_exit_t3"],
                         "detail": (f"held {trading_days_held}d >= {max_hold}d und "
                                    f"return {ret_pct:.2f}% < {hard_exit_min_ret_pct:.1f}%")})

    triggers.sort(key=lambda t: t["rank"])

    if triggers:
        top = triggers[0]
        return ExitVerdict(
            action="EXIT", reason=top["rule"], priority_rank=top["rank"],
            execution="next_open", ticker=ticker, as_of=as_of,
            entry_price=entry_price, last_close=last_close,
            return_pct=round(ret_pct, 4), trading_days_held=trading_days_held,
            sma5=sma5, tp=tp, sl=sl, triggers=triggers,
            note=f"EXIT via {top['rule']} ({top['detail']}).",
        )

    return ExitVerdict(
        action="HOLD", reason="hold", priority_rank=PRIORITY["hold"],
        execution="n/a", ticker=ticker, as_of=as_of, entry_price=entry_price,
        last_close=last_close, return_pct=round(ret_pct, 4),
        trading_days_held=trading_days_held, sma5=sma5, tp=tp, sl=sl,
        note="Keine Exit-Bedingung getriggert -> Position halten.",
    )


# ----------------------------------------------------------------------------
# Journal-Integration (read-only)
# ----------------------------------------------------------------------------
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_open_position(ticker: str, db_path: Optional[str] = None) -> Optional[dict]:
    """Lies eine offene Position (exit_date IS NULL) read-only aus dem Journal."""
    db = db_path or str(_repo_root() / "trade_journal.sqlite")
    if not Path(db).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT * FROM trades WHERE ticker = ? AND exit_date IS NULL "
            "ORDER BY entry_date DESC LIMIT 1", (ticker,))
        row = cur.fetchone()
        con.close()
        return dict(row) if row else None
    except Exception as e:  # pragma: no cover
        print(f"[exit_engine] journal read failed: {e}", file=sys.stderr)
        return None


def evaluate_from_journal(ticker: str, *, as_of: Optional[str] = None,
                          closes: Optional[Sequence[float]] = None,
                          db_path: Optional[str] = None) -> Optional[ExitVerdict]:
    """Bewerte eine offene Journal-Position. SMA5 aus --closes oder Fallback."""
    pos = load_open_position(ticker, db_path=db_path)
    if pos is None:
        return None
    entry_price = float(pos.get("entry_price") or 0.0)
    last_close = float(closes[-1]) if closes else entry_price
    sma5 = sma(closes, 5) if closes else None

    # TP/SL bevorzugt aus expliziten Journal-Feldern, sonst aus ATR ableiten
    tp = pos.get("target") or pos.get("tp1")
    sl = pos.get("stop")
    if (tp is None or sl is None):
        atr_abs = None
        atr_pct = pos.get("atr_pct")
        if atr_pct:
            atr_abs = entry_price * float(atr_pct) / 100.0
        d_tp, d_sl = derive_tp_sl(entry_price, atr_abs,
                                  atr_multi_sl=float(pos.get("atr_mult") or DEFAULT_ATR_MULT_SL))
        tp = tp if tp is not None else d_tp
        sl = sl if sl is not None else d_sl

    held = pos.get("holding_days")
    if held is None and as_of and pos.get("entry_date"):
        held = trading_days_between(str(pos["entry_date"])[:10], as_of)

    # SOURCE-AWARE (Schwarm-Audit 09.07.2026): Connors-SMA5 + Hard-Exit-T+3 sind
    # MR-SYSTEM-Regeln (§3 "gilt für MR"). Discretionary-Swings (source=
    # 'discretionary') werden NUR über ihre expliziten Levels regiert (ATR-TP/SL)
    # sonst mahnt die Engine z.B. einen diskretionaeren Swing taeglich per MR-Logik zum Exit.
    src = str(pos.get("source") or "").lower()
    is_disc_swing = src in ("discretionary", "disc")
    eff_sma5 = None if is_disc_swing else sma5
    eff_max_hold = (int(pos.get("max_hold_days"))
                    if pos.get("max_hold_days")
                    else (10_000 if is_disc_swing else HARD_EXIT_MAX_HOLD))

    return evaluate_exit(
        ticker=ticker, entry_price=entry_price, last_close=last_close,
        sma5=eff_sma5, tp=float(tp) if tp is not None else None,
        sl=float(sl) if sl is not None else None,
        trading_days_held=int(held) if held is not None else None,
        as_of=as_of, side=(pos.get("side") or "long"),
        max_hold=eff_max_hold,
    )


# ----------------------------------------------------------------------------
# Selftest (Known-Answer)
# ----------------------------------------------------------------------------
def selftest() -> int:
    cases = []

    def check(name, verdict, exp_action, exp_reason):
        ok = (verdict.action == exp_action and verdict.reason == exp_reason)
        cases.append((name, ok, f"{verdict.action}/{verdict.reason}",
                      f"{exp_action}/{exp_reason}"))
        return ok

    # 1) Connors-SMA5
    v = evaluate_exit(ticker="T", entry_price=100, last_close=104, sma5=103,
                      tp=115, sl=95, trading_days_held=1)
    check("connors_sma5", v, "EXIT", "connors_sma5")

    # 2) ATR-TP (kein SMA5-Trigger: close < sma5)
    v = evaluate_exit(ticker="T", entry_price=100, last_close=112, sma5=115,
                      tp=110, sl=95, trading_days_held=1)
    check("atr_tp", v, "EXIT", "atr_tp")

    # 3) ATR-SL
    v = evaluate_exit(ticker="T", entry_price=100, last_close=92, sma5=105,
                      tp=115, sl=93, trading_days_held=1)
    check("atr_sl", v, "EXIT", "atr_sl")

    # 4) Hard-Exit-T+3 (underwater, held>=3)
    v = evaluate_exit(ticker="T", entry_price=100, last_close=100.4, sma5=101,
                      tp=110, sl=95, trading_days_held=3)
    check("hard_exit_t3", v, "EXIT", "hard_exit_t3")

    # 5) HOLD (held < 3)
    v = evaluate_exit(ticker="T", entry_price=100, last_close=100.4, sma5=101,
                      tp=110, sl=95, trading_days_held=2)
    check("hold_held2", v, "HOLD", "hold")

    # 6) T+3 aber profitabel (>=+1%) -> HOLD
    v = evaluate_exit(ticker="T", entry_price=100, last_close=102, sma5=103,
                      tp=110, sl=95, trading_days_held=5)
    check("t3_profit_hold", v, "HOLD", "hold")

    # 7) Prioritaet: SMA5 UND TP feuern -> connors_sma5 gewinnt, beide in triggers
    v = evaluate_exit(ticker="T", entry_price=100, last_close=104, sma5=103,
                      tp=103.5, sl=95, trading_days_held=1)
    ok7 = (v.reason == "connors_sma5" and len(v.triggers) == 2
           and {t["rule"] for t in v.triggers} == {"connors_sma5", "atr_tp"})
    cases.append(("priority_sma5>tp", ok7, v.reason + f"/{len(v.triggers)}trig", "connors_sma5/2trig"))

    # 8) Synthetisch realistisch HOLD (Tag 1, knapp im Plus)
    v = evaluate_exit(ticker="AAA", entry_price=100.00, last_close=100.20,
                      sma5=102.0, tp=107.50, sl=95.00, trading_days_held=1)
    check("synthetic_day1_hold", v, "HOLD", "hold")

    # 9) Synthetisches Hard-Exit-Szenario (flach, T+3)
    v = evaluate_exit(ticker="AAA", entry_price=100.00, last_close=100.40,
                      sma5=102.0, tp=107.50, sl=95.00, trading_days_held=3)
    ok9 = check("synthetic_t3_hardexit", v, "EXIT", "hard_exit_t3")
    ok9b = (v.execution == "next_open")
    cases.append(("synthetic_t3_next_open", ok9b, v.execution, "next_open"))

    # 10) trading_days_between Konvention: Mo 2024-01-08 -> Do 2024-01-11 == 3
    tdb = trading_days_between("2024-01-08", "2024-01-11")
    cases.append(("tdb_mo_to_do_eq3", tdb == 3, str(tdb), "3"))

    # 11) derive_tp_sl reproduziert synthetischen Spread (atr_multi_sl=1.0, ATR=5.00)
    tp, sl = derive_tp_sl(100.00, 5.00, atr_multi_sl=1.0)  # tp=1.5*sl_dist
    ok11 = (abs(sl - 95.00) < 0.01 and abs(tp - 107.50) < 0.02)
    cases.append(("derive_tp_sl_synthetic", ok11, f"sl={sl:.2f},tp={tp:.2f}", "sl=95.00,tp=107.50"))

    # 12) sma() Helper
    ok12 = (sma([10, 12, 14, 16, 18], 5) == 14.0 and sma([1, 2], 5) is None)
    cases.append(("sma_helper", ok12, "ok" if ok12 else "fail", "ok"))

    # 13) Daten-Guard: entry_price fehlt (unvollstaendige Journal-Row) -> data_incomplete, KEIN EXIT
    v = evaluate_exit(ticker="AAA", entry_price=0, last_close=100.40, sma5=102.0,
                      tp=107.50, sl=95.00, trading_days_held=3)
    check("data_incomplete_guard", v, "HOLD", "data_incomplete")

    # Report
    passed = sum(1 for _, ok, *_ in cases if ok)
    total = len(cases)
    print(f"=== exit_engine selftest: {passed}/{total} ===")
    for name, ok, got, exp in cases:
        flag = "PASS" if ok else "FAIL"
        extra = "" if ok else f"  (got {got}, exp {exp})"
        print(f"  [{flag}] {name}{extra}")
    return 0 if passed == total else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _parse_closes(s: Optional[str]) -> Optional[list[float]]:
    if not s:
        return None
    return [float(x) for x in s.replace(" ", "").split(",") if x]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="EXIT-ENGINE-1: deterministic exit decision (V7.11)")
    ap.add_argument("--selftest", action="store_true", help="Known-Answer-Tests")
    ap.add_argument("--from-journal", action="store_true", help="offene Position aus Journal lesen (read-only)")
    ap.add_argument("--ticker", type=str, default="DEMO")
    ap.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (Default: heute UTC)")
    ap.add_argument("--closes", type=str, default=None, help="Komma-Liste der letzten Closes (fuer SMA5)")
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--last-close", type=float, default=None)
    ap.add_argument("--sma5", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--atr", type=float, default=None, help="ATR absolut (leitet TP/SL ab, falls nicht gesetzt)")
    ap.add_argument("--atr-mult-sl", type=float, default=DEFAULT_ATR_MULT_SL)
    ap.add_argument("--holding-days", type=int, default=None)
    ap.add_argument("--json-out", type=str, default=None, help="Pfad fuer JSON-Artefakt")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    closes = _parse_closes(args.closes)

    if args.from_journal:
        verdict = evaluate_from_journal(args.ticker, as_of=args.as_of, closes=closes)
        if verdict is None:
            print(f"[exit_engine] keine offene Position fuer {args.ticker} im Journal.")
            return 2
    else:
        if args.entry is None or args.last_close is None:
            ap.error("--entry und --last-close noetig (oder --from-journal / --selftest)")
        sma5 = args.sma5 if args.sma5 is not None else (sma(closes, 5) if closes else None)
        tp, sl = args.tp, args.sl
        if (tp is None or sl is None) and args.atr is not None:
            d_tp, d_sl = derive_tp_sl(args.entry, args.atr, atr_multi_sl=args.atr_mult_sl)
            tp = tp if tp is not None else d_tp
            sl = sl if sl is not None else d_sl
        held = args.holding_days
        if held is None and args.as_of:
            # ohne entry_date keine Ableitung; bleibt None
            pass
        verdict = evaluate_exit(
            ticker=args.ticker, entry_price=args.entry, last_close=args.last_close,
            sma5=sma5, tp=tp, sl=sl, trading_days_held=held, as_of=args.as_of,
        )

    out = verdict.to_dict()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"\n[exit_engine] geschrieben: {args.json_out}")
    # Exit-Code: 0=HOLD, 10=EXIT (skript-freundlich fuer Cron-Verkettung)
    return 10 if verdict.action == "EXIT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
