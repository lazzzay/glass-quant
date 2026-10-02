#!/usr/bin/env python3
"""
momentum_watch.py: informativer Momentum-Spotter (Zusatz, KEIN MR-Signal).

Liest den vorhandenen Tages-Scan (scan_YYYY-MM-DD.json) und flaggt starke
Aufwaerts-Trends: getrennt nach:
  🟢 Pullback-Entry  : starker Uptrend + Atempause (rsi2 niedrig) = sauberer Momentum-Einstieg
  🔥 Leader ueberhitzt: starker Uptrend, aber schon weit/rsi2 hoch = nur Thema-Watch, Entry zu spaet

Bewusst KEINE Order-Empfehlung mit Size: das ist H3 (nach H2) und ein anderes,
schwankungsreicheres Spiel (niedrigere WR, groessere Drawdowns), NICHT fuer das
MR-System validiert. Output ist eine Ideen-Liste fuer das eigene Urteil des Users
(keine Anlageberatung).

stdlib only, keine Netz-Abrufe (nur Scan-JSON lesen). CLI:
    python3 tools/momentum_watch.py [--date YYYY-MM-DD] [--top 8]
    python3 tools/momentum_watch.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from user_blacklist import load_user_blacklist  # noqa: E402

# User-konfigurierbare Ausschlussliste (config/blacklist.txt oder DAILYINVEST_BLACKLIST,
# Default LEER; siehe tools/user_blacklist.py). Der Name PERSONAL_BLACKLIST bleibt
# fuer die Synchronitaet mit daily_scanner/exec_bot/rec_gating_attribution.
PERSONAL_BLACKLIST = load_user_blacklist()
# Fuer EU-Retail oft nicht/schwer direkt handelbar -> aus dem Watch raus.
NON_EQUITY = {"commodity", "crypto", "etf_thematic", "etf_commodity",
              "etf_broad", "etf_sector"}

# Schwellen
PULLBACK_RSI2_MAX = 45.0     # "Atempause im Trend": sauberer Entry
EXTENDED_RSI2_MIN = 80.0     # rsi2 darueber = ueberhitzt/zu spaet
MIN_MOM5D = 3.0              # Mindest-5-Tage-Momentum fuer "stark"


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def strong_uptrend(r: dict) -> bool:
    """Sauberer Aufwaerts-Stack: Wochen-Trend bullish, Kurs ueber 200er und
    ueber 40-Wochen-Schnitt, 10-Wochen ueber 40-Wochen (intermediate up)."""
    close = _f(r.get("close"))
    if close <= 0:
        return False
    if str(r.get("weekly_trend")) != "bullish":
        return False
    if close <= _f(r.get("sma200"), 1e18):
        return False
    w10, w40 = _f(r.get("weekly_sma10")), _f(r.get("weekly_sma40"))
    if w40 <= 0 or w10 <= 0:
        return False
    return close > w40 and w10 >= w40


def classify(r: dict) -> str | None:
    if r.get("ticker") in PERSONAL_BLACKLIST:
        return None
    if str(r.get("asset_class")) in NON_EQUITY:
        return None
    if not strong_uptrend(r):
        return None
    rsi2 = _f(r.get("rsi2"))
    mom5 = _f(r.get("mom_5d"))
    if mom5 < MIN_MOM5D and rsi2 > PULLBACK_RSI2_MAX:
        return None  # weder stark gelaufen noch im Pullback -> uninteressant
    if rsi2 <= PULLBACK_RSI2_MAX and mom5 > 0:
        return "pullback"      # 🟢 Trend intakt, gerade durchgeatmet
    if rsi2 >= EXTENDED_RSI2_MIN or mom5 >= 20.0:
        return "extended"      # 🔥 Leader, aber heiss
    return "riding"            # 🟡 stark, mittlere Lage


def scan_momentum(scan: dict, top: int = 8) -> dict:
    rows = list((scan.get("ticker_map") or {}).values())
    buckets: dict[str, list] = {"pullback": [], "extended": [], "riding": []}
    for r in rows:
        c = classify(r)
        if c:
            buckets[c].append(r)
    # Pullback nach Trendstaerke (Abstand ueber 40W-Schnitt) sortieren,
    # extended/riding nach 5d-Momentum.
    buckets["pullback"].sort(
        key=lambda r: (_f(r.get("close")) / _f(r.get("weekly_sma40"), 1e18)), reverse=True)
    for k in ("extended", "riding"):
        buckets[k].sort(key=lambda r: _f(r.get("mom_5d")), reverse=True)
    return {k: v[:top] for k, v in buckets.items()}


def _line(r: dict) -> str:
    return (f"{r.get('ticker',''):10} mom5d {(_f(r.get('mom_5d'))):+5.1f}%  "
            f"rsi2 {(_f(r.get('rsi2'))):3.0f}  ATR {(_f(r.get('atr_pct'))):3.1f}%  "
            f"{str(r.get('sector',''))}")


def render(date: str, b: dict) -> str:
    L = [f"# 🔭 Momentum-Watch: {date}  (INFORMATIV · separater Zusatz · KEIN MR-Signal)\n"]
    pb = b["pullback"]
    L.append("## 🟢 Saubere Momentum-Pullback-Kandidaten (starker Uptrend + Atempause)\n")
    if pb:
        L += ["    " + _line(r) for r in pb]
        L.append("\n→ Regelzustand: starker Uptrend mit kurzer Atempause (rsi2 niedrig). Nur zur Beobachtung, kein Signal.")
    else:
        L.append("    heute keine. Alle starken Werte gelten nach der Regel als überdehnt (siehe unten).")
    L.append("\n## 🔥 Starke Leader, nach der Regel ÜBERHITZT (rsi2 hoch / schon weit gelaufen, nur Thema-Watch)\n")
    L += ["    " + _line(r) for r in b["extended"]] or ["    "]
    if b["riding"]:
        L.append("\n## 🟡 Stark, mittlere Lage (beobachten)\n")
        L += ["    " + _line(r) for r in b["riding"]]
    L.append("\n---")
    L.append("**Caveats (ehrlich):** Momentum ist ein *anderes* Spiel als der MR-Kern: "
             "niedrigere Trefferquote, größere Drawdowns, **nicht** für dieses System backgetestet (Hebel H3, gehört nach H2). "
             "Das hier ist eine **informative Regel-Ausgabe, kein Order-Signal und keine Anlageempfehlung.** "
             "Überhitzte Werte (rsi2≈99) erfüllen die Pullback-Regel nicht.")
    return "\n".join(L)


def load_scan(date: str | None):
    if date:
        p = TOOLS / f"scan_{date}.json"
    else:
        cands = sorted(glob.glob(str(TOOLS / "scan_2*-*-*.json")))
        cands = [c for c in cands if "manifest" not in c]
        if not cands:
            raise FileNotFoundError("kein scan_*.json gefunden")
        p = Path(cands[-1])
        date = p.stem.replace("scan_", "")
    return json.load(open(p, encoding="utf-8")), date


def selftest() -> int:
    """Hermetisch: nutzt eine synthetische Test-Blacklist ({"EEE"}) statt der User-Config."""
    global PERSONAL_BLACKLIST
    saved = PERSONAL_BLACKLIST
    PERSONAL_BLACKLIST = frozenset({"EEE"})
    try:
        return _selftest_body()
    finally:
        PERSONAL_BLACKLIST = saved


def _selftest_body() -> int:
    # Synthetik: ein sauberer Pullback, ein ueberhitzter Leader, ein Blacklist-Name.
    scan = {"ticker_map": {
        "GOODPB": {"ticker": "GOODPB", "asset_class": "us_large", "weekly_trend": "bullish",
                   "close": 120, "sma200": 90, "weekly_sma10": 115, "weekly_sma40": 100,
                   "rsi2": 30, "mom_5d": 6, "atr_pct": 2.5, "sector": "Tech"},
        "HOT":    {"ticker": "HOT", "asset_class": "us_large", "weekly_trend": "bullish",
                   "close": 300, "sma200": 100, "weekly_sma10": 250, "weekly_sma40": 180,
                   "rsi2": 99, "mom_5d": 40, "atr_pct": 6, "sector": "Semi"},
        "EEE":    {"ticker": "EEE", "asset_class": "asia_adr", "weekly_trend": "bullish",
                   "close": 50, "sma200": 30, "weekly_sma10": 45, "weekly_sma40": 35,
                   "rsi2": 30, "mom_5d": 8, "atr_pct": 5, "sector": "Auto"},
        "DOWN":   {"ticker": "DOWN", "asset_class": "us_large", "weekly_trend": "bearish",
                   "close": 50, "sma200": 80, "weekly_sma10": 55, "weekly_sma40": 70,
                   "rsi2": 20, "mom_5d": -5, "atr_pct": 3, "sector": "X"},
    }}
    b = scan_momentum(scan)
    ok = True
    pb = [r["ticker"] for r in b["pullback"]]
    ext = [r["ticker"] for r in b["extended"]]
    if "GOODPB" not in pb:
        print("FAIL: GOODPB sollte Pullback sein", pb); ok = False
    if "HOT" not in ext:
        print("FAIL: HOT sollte extended sein", ext); ok = False
    if any("EEE" in [r["ticker"] for r in v] for v in b.values()):
        print("FAIL: EEE (Blacklist) darf nicht auftauchen"); ok = False
    if any("DOWN" in [r["ticker"] for r in v] for v in b.values()):
        print("FAIL: DOWN (kein Uptrend) darf nicht auftauchen"); ok = False
    print("Buckets:", {k: [r["ticker"] for r in v] for k, v in b.items()})
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Momentum-Watch (informativ)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--write", action="store_true", help="zusaetzlich nach Briefings/Momentum-Watch-<date>.md schreiben")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        scan, date = load_scan(args.date)
    except FileNotFoundError as e:
        wanted = f"tools/scan_{args.date}.json" if args.date else "tools/scan_<date>.json"
        print(f"[momentum_watch] no scan found ({wanted}): {e}. "
              "Run python3 tools/daily_scanner.py first. Exit code 2.", file=sys.stderr)
        return 2
    b = scan_momentum(scan, top=args.top)
    report = render(date, b)
    print(report)
    if args.write:
        out = TOOLS.parent / "Briefings" / f"Momentum-Watch-{date}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\n[+] geschrieben: {out}", file=sys.stderr)
        # Shadow-Log fuer das LERNEN: Picks + Close + Bucket -> spaeter Forward-Return-Auswertung.
        shadow = TOOLS.parent / "shadow" / f"momentum_watch_{date}.json"
        shadow.parent.mkdir(exist_ok=True)
        picks = []
        for bucket, rows in b.items():
            for r in rows:
                picks.append({
                    "date": date, "ticker": r.get("ticker"), "bucket": bucket,
                    "close": r.get("close"), "mom_5d": r.get("mom_5d"),
                    "rsi2": r.get("rsi2"), "atr_pct": r.get("atr_pct"),
                    "weekly_trend": r.get("weekly_trend"), "sector": r.get("sector"),
                })
        shadow.write_text(json.dumps({
            "date": date, "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "leg": "momentum_watch_shadow", "n": len(picks), "picks": picks,
        }, indent=2), encoding="utf-8")
        print(f"[+] shadow-log: {shadow} ({len(picks)} picks), Lern-Datengrundlage", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
