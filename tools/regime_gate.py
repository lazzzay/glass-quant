#!/usr/bin/env python3
"""
V6.1 Regime Gate: Automatisiertes Regime-Scoring
Lädt VIX, ADX(SPY), 10Y Yield, DXY, Öl, Gold und gibt ein JSON mit
dem aktuellen Regime, Confidence und Size-Multiplikator zurück.

Benutzung:
    python3 regime_gate.py              # Ausgabe als JSON auf stdout
    python3 regime_gate.py --save       # Speichert regime_YYYY-MM-DD.json ins Verzeichnis

Quellen: Yahoo Finance (yfinance). McClellan wird approximiert durch
NYSE-Breadth-Proxy (Advance/Decline via $NYAD falls verfügbar).

Fehlende Daten (fail loud, keine erfundenen Werte):
    * VIX oder SPY fehlen  -> status "degraded": kein Regime ("Unknown"), keine
      erlaubten Tiers, size_multiplier 0. Exit-Code 3. exec_bot --plan und
      data_health werten das als NO TRADE.
    * nur 10Y/DXY/WTI/Gold fehlen -> status "partial": Default-Werte, die in keine
      Klassifikations-Regel eingehen, werden in "sources" offen markiert. Exit-Code 0.

Exit-Codes: 0 ok oder partial · 3 degraded (VIX oder SPY fehlen) · 1 Fehler
"""
import json, sys, argparse, os
from datetime import datetime, timedelta, timezone

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    if __name__ == "__main__":
        print("ERROR: pip install yfinance pandas --break-system-packages", file=sys.stderr)
        sys.exit(1)
    raise  # FIX 2026-06-01: Import-safe (kein Prozess-Kill bei fehlendem Dep)


RC_DEGRADED = 3
CRITICAL_INPUTS = ("vix", "spy")


def fetch_last(ticker: str, days: int = 40) -> pd.DataFrame:
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days + 20)
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.tail(days)


def adx(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["High"], df["Low"], df["Close"]
    plus_dm = (h.diff()).clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di) * 100
    return float(dx.rolling(period).mean().iloc[-1])


def pct_change_5d(df: pd.DataFrame) -> float:
    return float((df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1) * 100)


def classify(metrics: dict) -> dict:
    """
    6 Regime-Klassen: Extreme-Fear-Thrust, High-Vol-Krise, Trending-Divergent,
    Trending, Bullish-Confirmed, Neutral.
    Rückgabe: dict(regime, confidence, size_multiplier, allowed_tiers, notes)
    """
    vix = metrics["vix"]
    adx_spy = metrics["adx_spy"]
    spy_5d = metrics["spy_5d_pct"]
    yield_chg = metrics["yield_5d_bps"]
    dxy_5d = metrics["dxy_5d_pct"]

    notes = []

    # Extreme-Fear-Thrust: VIX > 35 + SPY down hard
    if vix > 35 and spy_5d < -5:
        return dict(regime="Extreme-Fear-Thrust", confidence=0.85,
                    size_multiplier=1.2, allowed_tiers=["A", "B"],
                    notes=["Contrarian Bonus. Nur Tier A/B kaufen, keine Tier X."])

    # High-Vol/Krise
    if vix > 30:
        return dict(regime="High-Vol-Krise", confidence=0.80,
                    size_multiplier=0.3, allowed_tiers=["A"],
                    notes=["Nur Tier A, keine Crypto, keine Tier C/X."])

    # Trending-Divergent: starker ADX aber fallende Breadth
    if adx_spy > 25 and spy_5d < 0:
        return dict(regime="Trending-Divergent", confidence=0.65,
                    size_multiplier=0.4, allowed_tiers=["A", "B"],
                    notes=["Divergenz zwischen Trend und Momentum: reduzierte Size."])

    # Trending
    if adx_spy > 25:
        return dict(regime="Trending", confidence=0.70,
                    size_multiplier=0.6, allowed_tiers=["A", "B", "C"],
                    notes=["Starker Trend, MR-Setups schwächer: Size reduziert."])

    # Bullish-Confirmed (V6.3 REVISED nach Backtest 2026-04-14):
    # Regime hatte historisch WR 45.5%, PF 0.53, -104€ auf 22 Trades.
    # E6-Fix (Variante B+E): DEFAULT = Skip. Nur Entry wenn Triple-RSI ≥ 9
    # (echte Capitulation): in-sample 4 Trades / 75% WR / +13€.
    # Size bleibt 1.0× für den Ausnahmefall (statistisch zu wenig Daten für Reduktion).
    if vix < 18 and spy_5d > 0.5 and adx_spy < 22:
        return dict(regime="Bullish-Confirmed", confidence=0.85,
                    size_multiplier=1.0, allowed_tiers=["A", "B"],
                    notes=[
                        "V6.3-FIX: DEFAULT = Skip MR-Entries.",
                        "Ausnahme: NUR Entry wenn Triple-RSI ≥ 9 (Capitulation-Filter).",
                        "Begründung: historisches WR 45.5% (Audit 2026-04-14), -104€ in-sample.",
                        "Full-Size 1.0 gilt nur für diese seltenen High-Confidence-Setups.",
                    ],
                    mr_entry_filter="triple_rsi>=9")

    # Default: Neutral
    return dict(regime="Neutral", confidence=0.60,
                size_multiplier=0.8, allowed_tiers=["A", "B", "C"],
                notes=["Kein klares Regime: Standard-Betrieb leicht reduziert."])


TICKERS = {
    "vix": "^VIX",
    "spy": "SPY",
    "dxy": "DX-Y.NYB",
    "tnx": "^TNX",    # 10Y Yield (x10)
    "wti": "CL=F",
    "gold": "GC=F",
}


def _has_rows(df, n: int) -> bool:
    return df is not None and len(df) > n


def build_snapshot(data: dict, today: str, generated_at_utc: str) -> tuple[dict, int]:
    """Pure: fetched frames (or None) -> (regime JSON, exit code).

    Missing VIX or SPY data never becomes an invented regime: the snapshot is
    status "degraded" with no tradable tiers and exit code RC_DEGRADED.
    Missing informational inputs (10Y, DXY, WTI, gold) are marked in "sources".
    """
    metrics: dict = {}
    sources: dict = {}
    fallback: list = []

    if _has_rows(data.get("vix"), 0):
        metrics["vix"] = float(data["vix"]["Close"].iloc[-1])
        sources["vix"] = "ok (yfinance ^VIX)"
    else:
        sources["vix"] = "missing (no ^VIX data): regime not classified"
        fallback.append("vix")

    if _has_rows(data.get("spy"), 14):
        metrics["spy_5d_pct"] = pct_change_5d(data["spy"])
        metrics["adx_spy"] = adx(data["spy"])
        sources["spy"] = "ok (yfinance SPY)"
    else:
        sources["spy"] = "missing (SPY needs 15+ daily rows): regime not classified"
        fallback.append("spy")

    if _has_rows(data.get("tnx"), 6):
        # ^TNX on Yahoo returns yield in percent directly (e.g. 4.31 = 4.31%)
        yield_now = float(data["tnx"]["Close"].iloc[-1])
        yield_prev = float(data["tnx"]["Close"].iloc[-6])
        metrics["yield_now"] = yield_now
        metrics["yield_5d_bps"] = (yield_now - yield_prev) * 100
        sources["tnx"] = "ok (yfinance ^TNX)"
    else:
        metrics["yield_now"] = 4.3
        metrics["yield_5d_bps"] = 0
        sources["tnx"] = "missing, default value used (informational only, not in any regime rule)"
        fallback.append("tnx")

    if _has_rows(data.get("dxy"), 6):
        metrics["dxy_5d_pct"] = pct_change_5d(data["dxy"])
        sources["dxy"] = "ok (yfinance DX-Y.NYB)"
    else:
        metrics["dxy_5d_pct"] = 0.0
        sources["dxy"] = "missing, default value used (informational only, not in any regime rule)"
        fallback.append("dxy")

    for name in ("wti", "gold"):
        if _has_rows(data.get(name), 6):
            metrics[f"{name}_5d_pct"] = pct_change_5d(data[name])
            sources[name] = f"ok (yfinance {TICKERS[name]})"
        else:
            sources[name] = "missing (optional, informational only)"

    critical_missing = [m for m in CRITICAL_INPUTS if m in fallback]
    if critical_missing:
        status = "degraded"
        result = dict(
            regime="Unknown", confidence=0.0, size_multiplier=0.0, allowed_tiers=[],
            notes=[f"NO TRADE: {', '.join(x.upper() for x in critical_missing)} data missing. "
                   "The regime is not classified and no fallback value is used."])
    else:
        status = "partial" if fallback else "ok"
        result = classify(metrics)

    # Intermarket vetos (informational; only from metrics that were really fetched)
    vetos = []
    if metrics.get("wti_5d_pct", 0) > 10:
        vetos.append("WTI +10% in 5 Tagen: Airlines/Transport -1.5, Energy +0.5")
    if "tnx" not in fallback and metrics.get("yield_5d_bps", 0) > 20:
        vetos.append("10Y +20bps: Growth/Tech -1.0, Banken +0.5")
    if "dxy" not in fallback and metrics.get("dxy_5d_pct", 0) > 1.5:
        vetos.append("DXY +1.5%: Gold/EM -1.0")
    if metrics.get("vix", 0) > 25:
        vetos.append("VIX >25: Small Caps/Crypto -1.5")

    output = {
        "date": today,
        "generated_at_utc": generated_at_utc,
        "status": status,
        "fallback_metrics": fallback,
        "sources": sources,
        "metrics": {k: round(v, 2) if isinstance(v, (int, float)) else v for k, v in metrics.items()},
        "regime": result["regime"],
        "confidence": result["confidence"],
        "size_multiplier": result["size_multiplier"],
        "allowed_tiers": result["allowed_tiers"],
        "notes": result["notes"],
        "active_vetos": vetos,
        "mr_entry_filter": result.get("mr_entry_filter"),
        "system_version": "V6.3",
    }
    return output, (RC_DEGRADED if status == "degraded" else 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true", help="Speichert JSON-Snapshot")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.strftime("%Y-%m-%d")

    data = {}
    for name, tk in TICKERS.items():
        try:
            data[name] = fetch_last(tk, days=30)
        except Exception as e:
            data[name] = None
            print(f"WARN: {name} ({tk}) fetch failed: {e}", file=sys.stderr)

    output, rc = build_snapshot(data, today, now.isoformat(timespec="seconds"))

    print(json.dumps(output, indent=2, ensure_ascii=False))

    if args.save:
        # M1-FIX 2026-06-14: explizit ins Script-/tools-Verzeichnis schreiben, nicht ins
        # CWD. Sonst landet die Datei je nach Aufrufort im Repo-Root, während die Consumer
        # (data_health u.a.) tools/ lesen → stale tools/-Kopie / Doppelschreibung.
        _outdir = os.path.dirname(os.path.abspath(__file__))
        fname = os.path.join(_outdir, f"regime_{today}.json")
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\nSaved: {fname}", file=sys.stderr)

    if rc == RC_DEGRADED:
        print(f"REGIME DEGRADED: missing {output['fallback_metrics']}. "
              "No regime classified, treat today as NO TRADE (exit code 3).", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
