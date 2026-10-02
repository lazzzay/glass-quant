#!/usr/bin/env python3
"""
Credit-Ampel V3.1
Overlay O4: credit stress score

Berechnet taeglich den Credit-Stress-Score (0-10) aus Free-Data-Quellen:
  - FRED BAMLH0A0HYM2  (High-Yield OAS)
  - FRED BAMLC0A0CM    (Investment-Grade OAS)
  - FRED T10Y3M        (10Y-3M Yield-Curve Slope)
  - Yahoo ^MOVE        (MOVE Index: US Treasury Vol)

Output (lokale Run-Artefakte, gitignored):
  - credit_ampel_YYYY-MM-DD.json
  - credit_ampel_history.csv     (Historie, Append-Mode)

Forschungs-Overlay: Ampel, Score und "overlay_rule_state" beschreiben den Zustand einer
hypothetischen Backtest-Regel. Sie sind keine Anlageempfehlung und keine Anweisung,
Positionen zu kaufen, zu verkaufen oder abzusichern.

Trigger-Logik V3.1:
  Ampel:  0-3 GRUEN  |  4-6 GELB  |  7-8 ORANGE  |  9-10 ROT
  Veto V3: ausgeloest wenn HY-OAS z>=3.0 ODER (HY-OAS>600 BP UND +50 BP in 5d)

Dependencies:
  pip install pandas numpy requests yfinance

Exit-Codes: 0 ok · 2 Daten nicht abrufbar (Netzwerk, FRED/Yahoo) · 1 sonstiger Fehler

Usage:
  python3 credit_ampel_v31.py                       # run live (network: FRED + Yahoo)
  python3 credit_ampel_v31.py --out-dir outputs/    # Outputs in ein eigenes Verzeichnis
  python3 credit_ampel_v31.py --test                # smoke-test mit Dummy-Werten (offline)

Note: ICE BofA OAS series on FRED are third-party data; fetch them, do not
redistribute them. Outputs are local run artifacts (gitignored).
"""
from __future__ import annotations
import argparse
import json
import sys
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
    import requests
except ImportError:
    print("ERROR: run `pip install pandas numpy requests yfinance`")
    sys.exit(1)

# -------------------- Config --------------------
FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
FRED_SERIES = {
    "HY_OAS": "BAMLH0A0HYM2",   # ICE BofA US HY Master II OAS (percentage points)
    "IG_OAS": "BAMLC0A0CM",     # ICE BofA US Corporate Master OAS
    "CURVE":  "T10Y3M",         # 10Y minus 3M yield spread
}
MOVE_TICKER = "^MOVE"
ROLLING_WINDOW = 252  # ~1 Jahr Handelstage fuer z-Scores
OUT_DIR = Path(__file__).parent
HISTORY_CSV = OUT_DIR / "credit_ampel_history.csv"


# -------------------- Data Fetchers --------------------
def fetch_fred(series_id: str) -> pd.Series:
    """Holt eine FRED-Zeitreihe als pandas.Series (Date-Index, float)."""
    url = FRED_BASE.format(series_id)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    # FRED uses DATE / <series_id> columns
    date_col = df.columns[0]
    val_col = df.columns[1]
    df[date_col] = pd.to_datetime(df[date_col])
    df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
    df = df.dropna().set_index(date_col)
    return df[val_col].rename(series_id)


def fetch_move() -> pd.Series:
    """Holt den MOVE Index via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        print("WARN: yfinance nicht installiert, MOVE wird auf 100 fallback-gesetzt")
        idx = pd.date_range(end=date.today(), periods=ROLLING_WINDOW * 2, freq="B")
        return pd.Series([100.0] * len(idx), index=idx, name="MOVE")
    df = yf.Ticker(MOVE_TICKER).history(period="2y", auto_adjust=False)
    if df.empty:
        print("WARN: MOVE-Fetch leer, fallback 100")
        idx = pd.date_range(end=date.today(), periods=ROLLING_WINDOW, freq="B")
        return pd.Series([100.0] * len(idx), index=idx, name="MOVE")
    # Index normalisieren (auf Date ohne TZ)
    s = df["Close"].copy()
    s.index = s.index.tz_localize(None).normalize()
    return s.rename("MOVE")


# -------------------- Score Components --------------------
def zscore(series: pd.Series, window: int = ROLLING_WINDOW) -> float:
    """Z-Score der letzten Beobachtung auf rollierendem Mittel."""
    if len(series) < window:
        window = max(60, len(series) // 2)
    tail = series.tail(window)
    mu = tail.mean()
    sd = tail.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return 0.0
    return float((series.iloc[-1] - mu) / sd)


def subscore(z: float, invert: bool = False) -> float:
    """Map z-Score -> 0..10 linear clipped.
    Falls invert=True: positive z-Score = BULLISH (z.B. Curve-Steilheit ist positiv).
    Hier scoren wir STRESS, also: hoeherer z = mehr Stress = hoeherer Score.
    Wenn invert=True, dreht das Vorzeichen."""
    if invert:
        z = -z
    # z=0 -> 5, z=+2 -> 8, z=+3 -> 10, z=-2 -> 2, z=-3 -> 0
    val = 5.0 + z * 1.6
    return float(max(0.0, min(10.0, val)))


def composite_score(hy_z: float, ig_z: float, move_z: float, curve_z: float) -> float:
    """Gewichtetes Mittel der Subscores."""
    # HY-OAS dominiert (40%), IG 20%, MOVE 20%, Curve 20%
    # Curve invertiert: steilere Kurve = WENIGER Stress -> invert=True bei positiv
    # Aber inverse-Kurve (T10Y3M < 0) = BEARISH -> invert=True so that negative curve z=positive stress
    s_hy = subscore(hy_z)
    s_ig = subscore(ig_z)
    s_move = subscore(move_z)
    # T10Y3M hoch = steil = gesund. Also invert=True (negative z -> high stress).
    s_curve = subscore(curve_z, invert=True)
    composite = 0.40 * s_hy + 0.20 * s_ig + 0.20 * s_move + 0.20 * s_curve
    return round(composite, 2)


def ampel_from_score(score: float) -> str:
    if score <= 3: return "GRUEN"
    if score <= 6: return "GELB"
    if score <= 8: return "ORANGE"
    return "ROT"


def overlay_rule_state(ampel: str) -> dict:
    """Zustand der hypothetischen V3.1-Overlay-Regel je Ampel (Forschung, Backtest).

    exposure_factor_pct: Exposure-Faktor, den das Backtest-Overlay ansetzt.
    leverage_flag / hedge_flag: Regel-Flags des Overlays. Keine Handlungsanweisung.
    """
    return {
        "GRUEN":  {"exposure_factor_pct": 100, "leverage_flag": True,  "hedge_flag": "normal"},
        "GELB":   {"exposure_factor_pct": 80,  "leverage_flag": True,  "hedge_flag": "normal"},
        "ORANGE": {"exposure_factor_pct": 60,  "leverage_flag": False, "hedge_flag": "on"},
        "ROT":    {"exposure_factor_pct": 40,  "leverage_flag": False, "hedge_flag": "max"},
    }[ampel]


def veto_v3_check(hy_series: pd.Series, hy_z: float) -> tuple[bool, str]:
    """Veto V3: HY-OAS z>=3.0 ODER (HY-OAS>600 BP UND +50 BP in 5d).
    FRED-Serie ist in Prozentpunkten, also 600 BP = 6.0.
    50 BP in 5 Tagen = 0.5 Punkte.
    """
    triggers = []
    if hy_z >= 3.0:
        triggers.append(f"HY-OAS z-Score {hy_z:.2f} >= 3.0")
    latest = hy_series.iloc[-1]
    if len(hy_series) >= 6:
        change_5d = latest - hy_series.iloc[-6]
        if latest > 6.0 and change_5d >= 0.5:
            triggers.append(f"HY-OAS {latest:.2f}% (>600BP) mit +{change_5d*100:.0f}BP in 5d")
    if triggers:
        return True, " | ".join(triggers)
    return False, ""


# -------------------- Main Run --------------------
def run_live() -> dict:
    print("[1/4] Fetch FRED HY-OAS ...", end=" ")
    hy = fetch_fred(FRED_SERIES["HY_OAS"]); print(f"ok ({len(hy)} obs, latest={hy.iloc[-1]:.2f}%)")
    print("[2/4] Fetch FRED IG-OAS ...", end=" ")
    ig = fetch_fred(FRED_SERIES["IG_OAS"]); print(f"ok ({len(ig)} obs, latest={ig.iloc[-1]:.2f}%)")
    print("[3/4] Fetch FRED T10Y3M ...", end=" ")
    curve = fetch_fred(FRED_SERIES["CURVE"]); print(f"ok ({len(curve)} obs, latest={curve.iloc[-1]:.2f}%)")
    print("[4/4] Fetch ^MOVE ...", end=" ")
    move = fetch_move(); print(f"ok ({len(move)} obs, latest={move.iloc[-1]:.2f})")

    hy_z = zscore(hy); ig_z = zscore(ig); curve_z = zscore(curve); move_z = zscore(move)
    score = composite_score(hy_z, ig_z, move_z, curve_z)
    ampel = ampel_from_score(score)
    veto, veto_reason = veto_v3_check(hy, hy_z)

    result = {
        "date": str(date.today()),
        "components": {
            "HY_OAS":    {"latest": round(float(hy.iloc[-1]), 3),   "zscore": round(hy_z, 2),   "subscore": subscore(hy_z)},
            "IG_OAS":    {"latest": round(float(ig.iloc[-1]), 3),   "zscore": round(ig_z, 2),   "subscore": subscore(ig_z)},
            "MOVE":      {"latest": round(float(move.iloc[-1]), 2), "zscore": round(move_z, 2), "subscore": subscore(move_z)},
            "T10Y3M":    {"latest": round(float(curve.iloc[-1]), 3),"zscore": round(curve_z, 2),"subscore": subscore(curve_z, invert=True)},
        },
        "composite_score": score,
        "ampel": ampel,
        "veto_v3_triggered": veto,
        "veto_reason": veto_reason,
        "overlay_rule_state": overlay_rule_state(ampel),
        "interpretation": interpret(ampel, veto),
        "disclaimer": NOT_ADVICE,
    }
    return result


NOT_ADVICE = ("Forschungsausgabe eines hypothetischen Overlays, keine Anlageempfehlung "
              "(research overlay output, not investment advice).")


def interpret(ampel: str, veto: bool) -> str:
    """Beschreibt den Regelzustand des Research-Overlays O4 (keine Handlungsanweisung)."""
    base = {
        "GRUEN":  "Credit-Stress niedrig. Regelzustand Overlay O4: Exposure-Faktor 100 %, Hebel-Flag an.",
        "GELB":   "Leichter Credit-Stress. Regelzustand Overlay O4: Exposure-Faktor 80 %, Hebel-Flag an.",
        "ORANGE": "Deutlicher Credit-Stress. Regelzustand Overlay O4: Exposure-Faktor 60 %, Hedge-Flag an, Hebel-Flag aus.",
        "ROT":    "Starker Credit-Stress. Regelzustand Overlay O4: Exposure-Faktor 40 %, Hedge-Flag max, keine neuen Long-Signale im Overlay.",
    }[ampel]
    if veto:
        base += " | Veto V3 aktiv: das Overlay friert den Exposure-Faktor ein."
    return base + " Keine Anlageempfehlung."


def save_output(result: dict, out_dir: Path | None = None):
    stamp = result["date"]
    base = Path(out_dir) if out_dir else OUT_DIR
    base.mkdir(parents=True, exist_ok=True)
    history_csv = base / HISTORY_CSV.name
    out_json = base / f"credit_ampel_{stamp}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nSaved: {out_json}")

    # Append to history CSV
    newrow = {
        "date": result["date"],
        "score": result["composite_score"],
        "ampel": result["ampel"],
        "veto_v3": result["veto_v3_triggered"],
        "hy_oas_pct": result["components"]["HY_OAS"]["latest"],
        "hy_z": result["components"]["HY_OAS"]["zscore"],
        "ig_oas_pct": result["components"]["IG_OAS"]["latest"],
        "move": result["components"]["MOVE"]["latest"],
        "t10y3m_pct": result["components"]["T10Y3M"]["latest"],
    }
    write_header = not history_csv.exists()
    with open(history_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(newrow.keys()))
        if write_header:
            w.writeheader()
        w.writerow(newrow)
    print(f"Appended: {history_csv}")


def test_mode():
    """Smoke-Test mit Dummy-Daten, keine Netzwerk-Calls."""
    print("=== TEST MODE ===")
    idx = pd.date_range(end=date.today(), periods=300, freq="B")
    hy = pd.Series(np.random.normal(4.5, 0.5, 300), index=idx, name="HY")
    # Letzter Wert: Stress-Spike
    hy.iloc[-1] = 7.2
    hy.iloc[-6] = 6.5  # +70 BP in 5d
    ig = pd.Series(np.random.normal(1.2, 0.15, 300), index=idx)
    move = pd.Series(np.random.normal(100, 15, 300), index=idx)
    curve = pd.Series(np.random.normal(0.5, 0.3, 300), index=idx)
    curve.iloc[-1] = -0.3  # Invertiert

    hy_z = zscore(hy); ig_z = zscore(ig); curve_z = zscore(curve); move_z = zscore(move)
    score = composite_score(hy_z, ig_z, move_z, curve_z)
    ampel = ampel_from_score(score)
    veto, vreason = veto_v3_check(hy, hy_z)

    print(f"HY z={hy_z:.2f}, IG z={ig_z:.2f}, MOVE z={move_z:.2f}, Curve z={curve_z:.2f}")
    print(f"Composite Score = {score}  ->  Ampel = {ampel}")
    print(f"Veto V3: {veto} ({vreason})")
    print(f"Interpretation: {interpret(ampel, veto)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Smoke-Test ohne Netzwerk")
    parser.add_argument("--out-dir", default=None,
                        help="Output-Verzeichnis (Default: Repo-Root neben dem Skript)")
    args = parser.parse_args()

    if args.test:
        test_mode()
    else:
        try:
            result = run_live()
        except (requests.RequestException, OSError, ValueError, IndexError, KeyError) as e:
            print(f"\n[credit_ampel] data not available ({type(e).__name__}: {str(e)[:200]}). "
                  "FRED or Yahoo could not be reached or returned unusable data. "
                  "Nothing was written. Offline check: python3 credit_ampel_v31.py --test. "
                  "Exit code 2.", file=sys.stderr)
            sys.exit(2)
        print("\n" + "=" * 60)
        print(f"  Credit-Ampel V3.1: {result['date']}")
        print("=" * 60)
        print(f"  Composite Score: {result['composite_score']} / 10")
        print(f"  Ampel:           {result['ampel']}")
        print(f"  Veto V3:         {'JA: ' + result['veto_reason'] if result['veto_v3_triggered'] else 'nein'}")
        print(f"\n  {result['interpretation']}")
        print("=" * 60)
        save_output(result, out_dir=args.out_dir)
