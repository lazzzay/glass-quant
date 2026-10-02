#!/usr/bin/env python3
"""exit_forward_test.py: H2 Exit-Hypothesen-Simulator (SCAFFOLD, 2026-06-20).

WARUM DIESES TOOL EXISTIERT
---------------------------
exit_attribution.py (V7.21) und exit_durability.py (V7.23) bewerten REALISIERTE
Exits (sie bucketten `v61_exit_reason`/`holding_days`/`netto_pnl` aus dem Trade-CSV).
Sie koennen NICHT beantworten „was waere gewesen, wenn wir bei T+1 die Haelfte
gebucht / bei T+2 hart gestoppt haetten?" dafuer braucht man den TAEGLICHEN
PREIS-PFAD je Trade, nicht nur das Endergebnis.

Dieses Tool liefert die fehlende Komponente: eine Exit-Simulations-Engine, die
beliebige Exit-Hypothesen ueber einen Tages-Pfad fahren laesst und sie auf einem
OUT-OF-SAMPLE-Split vergleicht (Pseudo-Forward, bis echte Live-Forward-Daten da
sind). Die in-sample-Befunde (connors_sma5 = Edge, 85% PnL in T+1..T+2, max-hold =
Drag) werden so VOR jeder Regel-Aenderung forward-pruefbar.

STATUS: WIRED (2026-06-20). Engine + 4 Hypothesen + Selftest + load_price_paths().
        Pfade kommen aus dem Backtest-CSV (v6_backtest_results_v61.csv) + Tages-OHLC
        (Parquet-Cache data_lake/prices/daily, offline; oder --source live = yfinance->
        Stooq auf dem Mac). Liefert echte OOS-Zahlen.
        WICHTIG: Das ist IN-SAMPLE-OOS (alte Daten), KEINE echte Live-Forward-
        Bestaetigung, KEINE Live-Regel-/Sizing-Aenderung, bis die Hypothese auch auf
        FRISCHEN Live-Trades haelt (CLAUDE.md §3/H2, eiserne Regel).

HYPOTHESEN (aus V7.21/23)
-------------------------
  current_t3       Live-Regel: connors_sma5 -> ATR-TP -> ATR-SL -> Hard-Exit T+3
  connors_sma5_only  Nur SMA5-Cross + Stop, KEIN max-hold (testet: max-hold = Drag?)
  time_stop_t2     Wie current, aber harter Exit T+2 wenn < +1% (testet: T+3->T+2?)
  partial_t1       50% bei T+1 buchen wenn gruen, Rest auf SMA5/Stop (Frontload T+1)

Usage:
    python3 tools/exit_forward_test.py --selftest
    python3 tools/exit_forward_test.py --oos-cutoff 2025-01-01   # (nach Wiring)
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# Pfad-Modell                                                                  #
# --------------------------------------------------------------------------- #
# Ein Trade-Pfad = Liste von Tages-Dicts ab Entry, T+1 = Index 0:
#   {"day": 1, "close": .., "high": .., "low": .., "sma5": ..}
# entry = Entry-Close (T0). levels = {"stop": .., "target": ..}.

def _ret(entry: float, price: float) -> float:
    return (price / entry - 1.0) * 100.0


def _result(day: int, price: float, entry: float, reason: str,
            ret_override: Optional[float] = None) -> dict:
    return {
        "exit_day": day,
        "exit_price": round(price, 4),
        "return_pct": round(ret_override if ret_override is not None else _ret(entry, price), 4),
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# Exit-Hypothesen (reine Funktionen ueber den Pfad)                            #
# --------------------------------------------------------------------------- #
def current_t3(entry: float, levels: dict, path: list[dict], max_hold: int = 3) -> dict:
    """Live-Regel V6.1: connors_sma5 -> ATR-TP -> ATR-SL -> Hard-Exit T+max_hold."""
    for d in path:
        if d.get("low") is not None and d["low"] <= levels["stop"]:
            return _result(d["day"], levels["stop"], entry, "atr_stop_loss")
        if d.get("high") is not None and d["high"] >= levels["target"]:
            return _result(d["day"], levels["target"], entry, "atr_take_profit")
        if d["close"] > d["sma5"]:
            return _result(d["day"], d["close"], entry, "connors_sma5")
        if d["day"] >= max_hold:
            return _result(d["day"], d["close"], entry, "hard_exit_t3")
    last = path[-1]
    return _result(last["day"], last["close"], entry, "eod")


def connors_sma5_only(entry: float, levels: dict, path: list[dict], max_hold: int = 99) -> dict:
    """Validierte Edge: SMA5-Cross + Stop, KEIN max-hold-Drag."""
    for d in path:
        if d.get("low") is not None and d["low"] <= levels["stop"]:
            return _result(d["day"], levels["stop"], entry, "atr_stop_loss")
        if d["close"] > d["sma5"]:
            return _result(d["day"], d["close"], entry, "connors_sma5")
    last = path[-1]
    return _result(last["day"], last["close"], entry, "eod")


def time_stop_t2(entry: float, levels: dict, path: list[dict], min_gain_pct: float = 1.0) -> dict:
    """Wie current, aber harter Exit T+2 wenn Gain < min_gain_pct."""
    for d in path:
        if d.get("low") is not None and d["low"] <= levels["stop"]:
            return _result(d["day"], levels["stop"], entry, "atr_stop_loss")
        if d.get("high") is not None and d["high"] >= levels["target"]:
            return _result(d["day"], levels["target"], entry, "atr_take_profit")
        if d["close"] > d["sma5"]:
            return _result(d["day"], d["close"], entry, "connors_sma5")
        if d["day"] >= 2 and _ret(entry, d["close"]) < min_gain_pct:
            return _result(d["day"], d["close"], entry, "time_stop_t2")
        if d["day"] >= 3:
            return _result(d["day"], d["close"], entry, "hard_exit_t3")
    last = path[-1]
    return _result(last["day"], last["close"], entry, "eod")


def partial_t1(entry: float, levels: dict, path: list[dict], frac: float = 0.5,
               max_hold: int = 3) -> dict:
    """50% bei T+1 buchen wenn gruen, Rest auf SMA5/Stop/Hard-Exit. Geblendeter Return."""
    booked = 0.0
    realized = 0.0
    for d in path:
        if d["day"] == 1 and d["close"] > entry and booked == 0.0:
            realized += frac * _ret(entry, d["close"])
            booked = frac
        rem = 1.0 - booked
        if d.get("low") is not None and d["low"] <= levels["stop"]:
            realized += rem * _ret(entry, levels["stop"])
            return _result(d["day"], levels["stop"], entry, "partial+atr_stop_loss", ret_override=realized)
        if d["close"] > d["sma5"]:
            realized += rem * _ret(entry, d["close"])
            return _result(d["day"], d["close"], entry, "partial+connors_sma5", ret_override=realized)
        if d["day"] >= max_hold:
            realized += rem * _ret(entry, d["close"])
            return _result(d["day"], d["close"], entry, "partial+hard_exit", ret_override=realized)
    last = path[-1]
    realized += (1.0 - booked) * _ret(entry, last["close"])
    return _result(last["day"], last["close"], entry, "partial+eod", ret_override=realized)


HYPOTHESES: dict[str, Callable] = {
    "current_t3": current_t3,
    "connors_sma5_only": connors_sma5_only,
    "time_stop_t2": time_stop_t2,
    "partial_t1": partial_t1,
}


# --------------------------------------------------------------------------- #
# Vergleichs-Engine                                                            #
# --------------------------------------------------------------------------- #
def evaluate(trades: list[dict]) -> dict:
    """trades: [{entry, levels, path, (date)}]. Fuer jede Hypothese: n, win_rate,
    avg_return_pct, avg_exit_day. Reiner Vergleich, keine Gewichtung."""
    out = {}
    for name, fn in HYPOTHESES.items():
        rets, days, wins = [], [], 0
        for t in trades:
            r = fn(t["entry"], t["levels"], t["path"])
            rets.append(r["return_pct"])
            days.append(r["exit_day"])
            if r["return_pct"] > 0:
                wins += 1
        n = len(trades) or 1
        out[name] = {
            "n": len(trades),
            "win_rate_pct": round(100.0 * wins / n, 1),
            "avg_return_pct": round(sum(rets) / n, 4),
            "avg_exit_day": round(sum(days) / n, 2),
            "sum_return_pct": round(sum(rets), 4),
        }
    return out


def oos_split(trades: list[dict], cutoff: str) -> tuple[list[dict], list[dict]]:
    """Train = date < cutoff, Test = date >= cutoff (ISO-Strings)."""
    train = [t for t in trades if t.get("date", "") < cutoff]
    test = [t for t in trades if t.get("date", "") >= cutoff]
    return train, test


# --------------------------------------------------------------------------- #
# WIRING (2026-06-20): echte Pfade aus Backtest-CSV + Tages-OHLC               #
# --------------------------------------------------------------------------- #
def _norm_ticker(t: str) -> str:
    return (t or "").upper().replace("-", "_").replace(".", "_").replace("=", "_")


def _cache_bar_provider(prices_dir: str = "data_lake/prices/daily"):
    """bar_provider(ticker) -> DataFrame(date,open,high,low,close) aus dem Parquet-Cache.
    Offline lauffaehig (kein Netz). Ticker-Normalisierung (-/./= -> _)."""
    import os
    import pandas as pd
    index = {}
    if os.path.isdir(prices_dir):
        for f in os.listdir(prices_dir):
            if f.endswith(".parquet"):
                index[_norm_ticker(f[:-len(".parquet")])] = os.path.join(prices_dir, f)
    cache: dict = {}

    def provider(ticker: str):
        key = _norm_ticker(ticker)
        if key not in index:
            return None
        if key not in cache:
            df = pd.read_parquet(index[key])
            df = df[["date", "open", "high", "low", "close"]].copy()
            df["date"] = df["date"].astype(str).str[:10]
            cache[key] = df.sort_values("date").reset_index(drop=True)
        return cache[key]

    return provider


def _live_bar_provider(start: str = "2022-01-01"):
    """bar_provider(ticker) via data_source_orchestrator.fetch_with_fallback
    (yfinance->Stooq). Mac-only: in der Sandbox blockt Yahoo (Throttle)."""
    from datetime import date
    from pathlib import Path
    import pandas as pd
    sys.path.insert(0, str(Path(__file__).parent))
    from data_source_orchestrator import fetch_with_fallback
    s = date.fromisoformat(start)

    def provider(ticker: str):
        df = fetch_with_fallback(ticker, s, date.today())
        if df is None or len(df) == 0:
            return None
        df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        dcol = [c for c in df.columns if c.startswith("date") or c == "index"][0]
        out = pd.DataFrame({
            "date": df[dcol].astype(str).str[:10],
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        })
        return out.sort_values("date").reset_index(drop=True)

    return provider


def _build_path(df, entry_date: str, lookahead: int = 8) -> Optional[list[dict]]:
    """OHLC-df -> Forward-Tage ab entry_date als path-Dicts {day,close,high,low,sma5}.
    SMA5 = 5-Tage-SMA des Close (Connors-Exit-Referenz)."""
    import pandas as pd
    d = df.copy()
    d["sma5"] = d["close"].rolling(5, min_periods=5).mean()
    ed = entry_date[:10]
    exact = d.index[d["date"] == ed]
    if len(exact):
        i0 = int(exact[0])
    else:
        ge = d.index[d["date"] >= ed]
        if not len(ge):
            return None
        i0 = int(ge[0])
    path = []
    for k in range(1, lookahead + 1):
        j = i0 + k
        if j >= len(d):
            break
        row = d.iloc[j]
        if pd.isna(row["sma5"]):
            return None
        path.append({"day": k, "close": float(row["close"]), "high": float(row["high"]),
                     "low": float(row["low"]), "sma5": float(row["sma5"])})
    return path or None


def load_price_paths(csv_path: str = "v6_backtest_results_v61.csv",
                     bar_provider=None, lookahead: int = 8,
                     cutoff: Optional[str] = None) -> list[dict]:
    """WIRED 2026-06-20: Trades aus Backtest-CSV + reale OHLC-Pfade (Long-only MR).
    Levels aus sl_pct/tp_pct. Rows ohne Cache-Ticker/History werden uebersprungen.
    cutoff (optional): nur date >= cutoff."""
    import csv as _csv
    if bar_provider is None:
        bar_provider = _cache_bar_provider()
    out = []
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    for r in rows:
        try:
            entry = float(r["entry"])
            date_ = (r.get("date") or "")[:10]
            sl = float(r["sl_pct"])
            tp = float(r["tp_pct"])
        except (TypeError, ValueError, KeyError):
            continue
        if cutoff and date_ < cutoff:
            continue
        df = bar_provider(r["ticker"])
        if df is None:
            continue
        path = _build_path(df, date_, lookahead)
        if not path:
            continue
        out.append({
            "ticker": r["ticker"], "date": date_, "entry": entry,
            "levels": {"stop": entry * (1 - sl / 100.0), "target": entry * (1 + tp / 100.0)},
            "path": path,
        })
    return out


# --------------------------------------------------------------------------- #
# Selftest (synthetische Pfade mit bekanntem Soll-Verhalten)                   #
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    entry = 100.0
    lv = {"stop": 95.0, "target": 110.0}

    # Pfad A: T+1 flach unter SMA5, T+2 Cross ueber SMA5 bei 102
    pathA = [
        {"day": 1, "close": 99.0, "high": 100.0, "low": 98.5, "sma5": 101.0},
        {"day": 2, "close": 102.0, "high": 102.5, "low": 99.0, "sma5": 101.5},
        {"day": 3, "close": 103.0, "high": 103.0, "low": 101.0, "sma5": 101.8},
    ]
    r = connors_sma5_only(entry, lv, pathA)
    check(r["exit_day"] == 2 and r["reason"] == "connors_sma5", f"A connors_sma5_only {r}")
    r = current_t3(entry, lv, pathA)
    check(r["exit_day"] == 2 and r["reason"] == "connors_sma5", f"A current_t3 {r}")

    # Pfad B: nie ueber SMA5, nie +1%, flach -> current_t3 = Hard T+3, time_stop_t2 = T+2
    pathB = [
        {"day": 1, "close": 100.2, "high": 100.5, "low": 99.0, "sma5": 101.0},
        {"day": 2, "close": 100.3, "high": 100.6, "low": 99.5, "sma5": 101.0},
        {"day": 3, "close": 100.1, "high": 100.4, "low": 99.4, "sma5": 101.0},
    ]
    r = current_t3(entry, lv, pathB)
    check(r["exit_day"] == 3 and r["reason"] == "hard_exit_t3", f"B current_t3 {r}")
    r = time_stop_t2(entry, lv, pathB)
    check(r["exit_day"] == 2 and r["reason"] == "time_stop_t2", f"B time_stop_t2 {r}")

    # Pfad C: Stop am T+1 (low unter 95)
    pathC = [
        {"day": 1, "close": 96.0, "high": 99.0, "low": 94.0, "sma5": 101.0},
        {"day": 2, "close": 97.0, "high": 98.0, "low": 95.5, "sma5": 101.0},
    ]
    r = current_t3(entry, lv, pathC)
    check(r["exit_day"] == 1 and r["reason"] == "atr_stop_loss" and r["return_pct"] == -5.0,
          f"C stop {r}")

    # Pfad D: T+1 gruen (104) -> partial bucht 0.5*4% = 2%; dann SMA5-Cross T+2 bei 106
    pathD = [
        {"day": 1, "close": 104.0, "high": 104.5, "low": 100.0, "sma5": 105.0},
        {"day": 2, "close": 106.0, "high": 106.0, "low": 103.0, "sma5": 105.0},
    ]
    r = partial_t1(entry, lv, pathD)
    # 0.5*4% + 0.5*6% = 2% + 3% = 5%
    check(abs(r["return_pct"] - 5.0) < 1e-6 and r["reason"] == "partial+connors_sma5",
          f"D partial_t1 {r}")

    # evaluate() Aggregation
    trades = [
        {"entry": entry, "levels": lv, "path": pathA, "date": "2024-06-01"},
        {"entry": entry, "levels": lv, "path": pathB, "date": "2025-06-01"},
        {"entry": entry, "levels": lv, "path": pathC, "date": "2025-07-01"},
        {"entry": entry, "levels": lv, "path": pathD, "date": "2026-01-01"},
    ]
    agg = evaluate(trades)
    check(set(agg) == set(HYPOTHESES), "evaluate keys")
    check(all(agg[h]["n"] == 4 for h in agg), "evaluate n")
    train, test = oos_split(trades, "2025-01-01")
    check(len(train) == 1 and len(test) == 3, f"oos_split {len(train)}/{len(test)}")

    if fails:
        print("SELFTEST FAILED:", file=sys.stderr)
        for f in fails:
            print("  -", f, file=sys.stderr)
        return 1
    print("OK: exit_forward_test Selftest bestanden (4 Hypothesen, Engine, OOS-Split)")
    print("    WIRED: '--run --source cache' fuer echte OOS-Zahlen (data_lake/prices/daily).")
    return 0


def _winner(agg: dict) -> str:
    """Hypothese mit hoechster avg_return_pct."""
    return max(agg, key=lambda h: agg[h]["avg_return_pct"]) if agg else "?"


def main() -> int:
    ap = argparse.ArgumentParser(description="H2 Exit-Hypothesen-Simulator")
    ap.add_argument("--selftest", action="store_true", help="Synthetik-Selftest (kein Netz/Daten)")
    ap.add_argument("--run", action="store_true", help="Echte Daten: Backtest-CSV + OHLC-Pfade")
    ap.add_argument("--source", choices=["cache", "live"], default="cache",
                    help="cache=Parquet-OHLC (offline) | live=yfinance->Stooq (Mac)")
    ap.add_argument("--prices-dir", default="data_lake/prices/daily")
    ap.add_argument("--csv", default="v6_backtest_results_v61.csv")
    ap.add_argument("--oos-cutoff", default="2025-01-01", help="Train < cutoff <= Test")
    ap.add_argument("--lookahead", type=int, default=8, help="Forward-Tage je Trade")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.run:
        import json
        bp = _live_bar_provider() if args.source == "live" else _cache_bar_provider(args.prices_dir)
        trades = load_price_paths(csv_path=args.csv, bar_provider=bp, lookahead=args.lookahead)
        if not trades:
            print("[exit_forward_test] keine Pfade gebaut (Cache leer? CSV-Pfad?).", file=sys.stderr)
            return 3
        all_agg = evaluate(trades)
        train, test = oos_split(trades, args.oos_cutoff)
        test_agg = evaluate(test)
        rep = {
            "source": args.source, "n_trades": len(trades),
            "oos_cutoff": args.oos_cutoff, "train_n": len(train), "test_n": len(test),
            "winner_all": _winner(all_agg), "winner_oos_test": _winner(test_agg),
            "all": all_agg, "oos_test": test_agg,
            "caveat": "IN-SAMPLE-OOS (alte Daten). KEINE Live-Forward-Bestaetigung -> "
                      "KEINE Regel-/Sizing-Aenderung vor Halten auf frischen Live-Trades.",
        }
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
