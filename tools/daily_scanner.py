"""
V6.2 Daily Universe Scanner: scannt die 600+ Asset-Universe auf V6.1-MR-Setups.

Berechnet pro Ticker:
- RSI2, RSI5, RSI14 (Wilder)
- ATR(14), ATR%
- SMA5, SMA20, SMA200
- Connors-Setup-Qualität (0-10)
- Triple-RSI-Score (0-10)
- Momentum 1d/5d
- Pre-V6.1-Score (ohne Options/Insider/News, Platzhalter 5.0)

Output: tools/scan_YYYY-MM-DD.csv, Top-20 Setup-Kandidaten im Stdout, JSON-Snapshot.
"""
from __future__ import annotations  # FIX 2026-06-01: macht "X | None"-Annotations Py3.9-kompatibel (war TypeError auf 3.9)

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    if __name__ == "__main__":
        print("ERROR: pip install yfinance --break-system-packages", file=sys.stderr)
        sys.exit(1)
    raise  # FIX 2026-06-01: beim Import (Tests/Module) sauberen ImportError werfen statt Prozess-Kill

# SCAN-IMMUTABILITY-1: Write-once Helper (2026-04-19)
sys.path.insert(0, str(Path(__file__).parent))
from scan_artifacts import write_immutable, ImmutableArtifactError  # noqa: E402

# SCAN-OF-ORCHESTRATION-1 Fix (V7.9.10, 2026-04-27): blockierender Wait-Loop
# auf options_flow_{date}.json, damit der Scanner nicht VOR dem OF-Cron joint.
from options_flow_wait import (  # noqa: E402
    wait_for_options_flow,
    fail_hard_if_required,
)

TOOLS_DIR = Path(__file__).parent
UNIVERSE_CSV = TOOLS_DIR / "universe.csv"
SCANNER_VERSION = "V6.8"

# H2-Ablation (2026-06-01): Layer empirisch deaktiviert, Edge nicht nachweisbar.
#   news / options_flow : tote Quellen, 0-Beitrag in JEDEM Daten-Schnitt
#   insider / market_pcr: Δ-Expectancy NIE positiv (full-sample negativ)
# sector_rs/mtf/Regime bleiben aktiv (mind. in einem Schnitt positiv).
# Schwellen UNVERAENDERT (Threshold-Edge im OOS-Split nicht bestaetigt).
# Reversibel: Layer-Namen aus dem Set entfernen reaktiviert den Adjust.
DISABLED_LAYERS = {"news", "options_flow", "insider", "market_pcr"}

# User blacklist (optional, EMPTY by default): tickers listed here are removed HARD
# from the top setups. Configure via config/blacklist.txt or the DAILYINVEST_BLACKLIST
# environment variable (see tools/user_blacklist.py). Shared with exec_bot,
# momentum_watch and rec_gating_attribution.
from user_blacklist import load_user_blacklist  # noqa: E402
USER_BLACKLIST = load_user_blacklist()
PERSONAL_BLACKLIST = USER_BLACKLIST  # backward-compatible alias

# UNIVERSE-TRADABLE-1 (2026-06-10): Direkthandel (EU-Retail-Broker) vs. Hebel-Basiswert.
# Typischerweise NICHT direkt bei einem EU-Retail-Broker kaufbar (PRIIPs/KID):
#   - Futures (Ticker "=F" / asset_class commodity)            -> 16 Symbole
#   - US-domizilierte ETFs ohne PRIIPs-KID (etf_* ohne Suffix)  -> 74 Symbole
#   - Asia-Lokallistings (.HK/.TW/.KS/.T/.SS/.SZ: viele EU-Retail-Broker haben
#     keine HK/Tokyo/Korea/Taiwan-Boersenanbindung; ADRs sind direct)  -> 29 Symbole
# Diese ~19% des Universe duerfen KEINE §A-Direkt-Slots in den Top-N belegen.
# Sie bleiben als Hebel-Watch erhalten (KO/Faktor-Kandidat ab HEBEL_WATCH_MIN_SCORE,
# Hebel-Regel) bzw. als Markt-Kontext. UCITS-ETFs (Suffix .DE/.L/...)
# blieben direct: aktuell keine im Universe, Regel ist future-proof.
ASIA_LOCAL_SUFFIXES = (".HK", ".TW", ".KS", ".T", ".SS", ".SZ")
ETF_ASSET_CLASSES = {"etf_thematic", "etf_sector", "etf_commodity", "etf_broad"}
HEBEL_WATCH_MIN_SCORE = 8.0
HEBEL_WATCH_MAX_N = 10


def tradability(ticker: str, asset_class: str) -> str:
    """Klassifiziert die Handelbarkeit (EU-Retail-Broker): 'direct' | 'hebel_only'.

    hebel_only = kein Direktkauf moeglich; nur als Basiswert fuer
    KO/Faktor-Zertifikate (Score >= HEBEL_WATCH_MIN_SCORE) oder Kontext.
    """
    t = (ticker or "").strip().upper()
    ac = (asset_class or "").strip().lower()
    if t.endswith("=F") or ac == "commodity":
        return "hebel_only"
    if ac in ETF_ASSET_CLASSES and "." not in t:
        return "hebel_only"  # US-ETF ohne KID
    if any(t.endswith(suf) for suf in ASIA_LOCAL_SUFFIXES):
        return "hebel_only"  # Asia-Lokalboerse
    return "direct"


# DATA-FETCH-RESILIENCE-1 (2026-06-10): Scanner failt LAUT statt still auf
# Cache/Muell zu ranken (Lehre certifi-Bruch 08.06.: "possibly delisted"-Wand
# ueber ALLE Ticker -> Rohstoff-ETFs oben, scan_initial rc=3 versank im Log).
# Vier Guards, alle per --no-env-guard im Notfall uebersteuerbar:
#   1) certifi-Selbstcheck + SSL-Pinning (SSL_CERT_FILE/REQUESTS_CA_BUNDLE)
#   2) Canary-Probe (SPY/AAPL/MSFT ueber denselben Fetch-Pfad inkl. Stooq-
#      Fallback) -> liefert KEINER Daten, ist die Umgebung tot, nicht der Markt
#   3) Coverage-Floor nach batch_fetch (default 50% des Universe): FETCH-Ausbeute
#   4) Valid-Fill-Gate nach dem Scoring (SCAN-CATCHUP-QUALITY-GATE-1, 2026-06-29):
#      Anteil GUELTIGER Scan-Ergebnisse >= 80% (spiegelt data_health
#      SCAN_FILL_LT_80 = SEV_H). Ein degradierter-aber-vorhandener Scan (Coverage
#      bestanden, aber zu wenige valide Ergebnisse, z.B. transienter yfinance-
#      Throttle) wuerde sonst geschrieben, die Catch-up-Battery (catch_up_skip
#      prueft NUR Existenz von scan_<date>.json) ueberlisten und einen
#      Ganztages-NO-TRADE erzwingen. Sub-Floor -> KEIN Artefakt -> scan bleibt
#      'fehlend' -> 08:45/09:29/10:30-Catch-up laeuft erneut (Self-Heal).
# Bei Abort: scan_abort_<date>.json mit Grund + Fix-Hinweis, Exit 4/5/6 ->
# run_morning_pipeline loggt FAIL, data_health sieht SCAN_MISSING (SEV_H),
# Empfehlung geht in NO-TRADE. Fehlender Scan ist SICHER, Muell-Scan nicht.
ENV_GUARD_CANARIES = ("SPY", "AAPL", "MSFT")
COVERAGE_FLOOR_DEFAULT = 0.5
VALID_FILL_FLOOR_DEFAULT = 0.80  # == data_health SCAN_FILL_LT_80 (H/CRITICAL)
CERTIFI_MIN_BYTES = 50_000
EXIT_ENV_GUARD = 4
EXIT_COVERAGE_FLOOR = 5
EXIT_VALID_FILL_FLOOR = 6


def certifi_status() -> dict:
    """Prueft, ob das certifi-CA-Bundle existiert und plausibel gross ist."""
    try:
        import certifi
        p = Path(certifi.where())
        size = p.stat().st_size if p.exists() else 0
        ok = bool(p.exists() and size >= CERTIFI_MIN_BYTES)
        return {"ok": ok, "path": str(p), "size_bytes": size,
                "error": None if ok else f"cacert.pem fehlt/zu klein (<{CERTIFI_MIN_BYTES}B)"}
    except Exception as e:  # certifi nicht importierbar etc.
        return {"ok": False, "path": None, "size_bytes": 0, "error": str(e)}


def pin_ssl_to_certifi(status: dict | None = None) -> bool:
    """Pinnt SSL-Env-Vars auf certifi (setdefault: respektiert Overrides)."""
    import os
    st = status or certifi_status()
    if st.get("ok") and st.get("path"):
        os.environ.setdefault("SSL_CERT_FILE", st["path"])
        os.environ.setdefault("REQUESTS_CA_BUNDLE", st["path"])
        return True
    return False


def canary_probe(fetch_fn, canaries=ENV_GUARD_CANARIES) -> dict:
    """Probe-Fetch hochliquider Referenz-Ticker ueber fetch_fn(ticker)->df|None.

    ok=True sobald EIN Canary Daten liefert (Umgebung lebt). ok=False nur bei
    Komplett-Ausfall, dann ist Cert/Netz/Bot-Wall kaputt, nicht der Markt.
    """
    ok_tickers, failed = [], []
    for t in canaries:
        try:
            df = fetch_fn(t)
            if df is not None and len(df) > 0:
                ok_tickers.append(t)
            else:
                failed.append(t)
        except Exception:
            failed.append(t)
    return {"ok": bool(ok_tickers), "n_ok": len(ok_tickers),
            "ok_tickers": ok_tickers, "failed": failed,
            "canaries": list(canaries)}


def coverage_guard(n_data: int, n_universe: int,
                   floor: float = COVERAGE_FLOOR_DEFAULT) -> dict:
    """Prueft Daten-Ausbeute nach batch_fetch gegen den Floor (Anteil 0..1)."""
    ratio = (n_data / n_universe) if n_universe else 0.0
    return {"ok": ratio >= floor, "ratio": round(ratio, 4), "floor": floor,
            "n_data": int(n_data), "n_universe": int(n_universe)}


def valid_fill_guard(n_valid: int, n_universe: int,
                     floor: float = VALID_FILL_FLOOR_DEFAULT) -> dict:
    """SCAN-CATCHUP-QUALITY-GATE-1 (2026-06-29): prueft den Anteil GUELTIGER
    Scan-Ergebnisse (nicht nur gefetchte Ticker) gegen den Floor.

    Unterschied zu coverage_guard: coverage_guard misst FETCH-Coverage
    (len(data)/Universe) direkt nach dem Batch; valid_fill_guard misst die
    VALID-RESULTS-Fill (len(results)/Universe) NACH dem Scoring: exakt die
    Groesse, die data_health als SCAN_FILL bewertet. Der Default-Floor 0.80
    spiegelt die data_health-H-Schwelle (fill < 0.80 -> SEV_H/CRITICAL): ein
    Scan, den data_health CRITICAL nennen wuerde, gilt damit als 'fehlend' und
    darf von der Catch-up-Battery ersetzt werden, statt einen Ganztages-NO-TRADE
    zu zementieren. Spiegel-API zu coverage_guard (offline testbar)."""
    ratio = (n_valid / n_universe) if n_universe else 0.0
    return {"ok": ratio >= floor, "ratio": round(ratio, 4), "floor": floor,
            "n_valid": int(n_valid), "n_universe": int(n_universe)}


def fill_guard_applies(is_rescan: bool, canonical_exists: bool) -> bool:
    """SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1 (2026-07-06, V7.37): entscheidet,
    ob der valid_fill_guard fuer diesen Lauf greift.

    - Nicht-Rescan (scan_initial): IMMER (kanonischer Producer).
    - `--rescan` UND kanonisches scan_<date>.json FEHLT: JA, der Rescan agiert als
      Ersatz-Producer (scan_initial ist per Gate abgebrochen), write_immutable()
      nimmt den First-Write-Zweig und schreibt in den KANONISCHEN Namespace. Ohne
      Guard landet hier der degradierte Scan (Live 07-01 36,8% / 07-02 66,0%),
      ueberlistet catch_up_skip() und zementiert den Throttle-Zombie-NO-TRADE.
    - `--rescan` UND kanonischer Scan vorhanden: NEIN, normaler Re-Score, schreibt
      forensisch in den Rescan-Namespace (von catch_up_skip ignoriert), ungated.

    Reine Praedikat-Funktion (offline testbar). --no-env-guard uebersteuert das
    Abort-Verhalten separat im Aufrufer."""
    return (not is_rescan) or (not canonical_exists)


def write_scan_abort(scan_date: str, reason: str, details: dict) -> Path:
    """Schreibt scan_abort_<date>.json: fuer Empfehlungs-Session/Forensik."""
    p = TOOLS_DIR / f"scan_abort_{scan_date}.json"
    payload = {
        "date": scan_date,
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "writer": "daily_scanner.py",
        "reason": reason,  # CERTIFI_BROKEN | CANARY_DEAD | COVERAGE_FLOOR | VALID_FILL_FLOOR
        "details": details,
        "hint": ("CERTIFI_BROKEN: /usr/bin/python3 -m pip install --upgrade "
                 "--force-reinstall certifi --break-system-packages | "
                 "CANARY_DEAD: Netz/Bot-Wall pruefen (nativ vs Sandbox) | "
                 "COVERAGE_FLOOR: yfinance-Throttling/Teilausfall, Re-Run "
                 "oder --coverage-floor senken (bewusst!) | "
                 "VALID_FILL_FLOOR: zu wenige GUELTIGE Ergebnisse (transienter "
                 "Throttle): Catch-up-Battery laeuft erneut; sonst Re-Run oder "
                 "--valid-fill-floor senken (bewusst!) | Override: --no-env-guard"),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return p


def rsi_wilder(s: pd.Series, period: int) -> pd.Series:
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr_wilder(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def weekly_trend_from_daily(df_daily: pd.DataFrame) -> dict:
    """
    V6.6 E8: Weekly-Trend-Filter aus Daily-Bars (Resample).

    Logik:
    - Weekly-Resample (W-FRI) Close
    - Weekly-SMA10 / Weekly-SMA40
    - Weekly-RSI14

    Klassifikation weekly_trend:
    - bullish : SMA10>SMA40 UND RSI14>=50
    - bearish : SMA10<SMA40 UND RSI14<50
    - neutral : sonst
    """
    if df_daily is None or len(df_daily) < 60:
        return {"weekly_trend": "unknown", "weekly_rsi14": np.nan,
                "weekly_sma10": np.nan, "weekly_sma40": np.nan}
    try:
        wk = df_daily["Close"].resample("W-FRI").last().dropna()
        if len(wk) < 40:
            return {"weekly_trend": "unknown", "weekly_rsi14": np.nan,
                    "weekly_sma10": np.nan, "weekly_sma40": np.nan}
        sma10 = wk.rolling(10).mean().iloc[-1]
        sma40 = wk.rolling(40).mean().iloc[-1]
        rsi14 = rsi_wilder(wk, 14).iloc[-1]
        last_close = wk.iloc[-1]
        if pd.isna(sma10) or pd.isna(sma40) or pd.isna(rsi14):
            return {"weekly_trend": "unknown", "weekly_rsi14": np.nan,
                    "weekly_sma10": np.nan, "weekly_sma40": np.nan}
        bullish = (sma10 > sma40) and (rsi14 >= 50)
        bearish = (sma10 < sma40) and (rsi14 < 50)
        trend = "bullish" if bullish else ("bearish" if bearish else "neutral")
        return {
            "weekly_trend": trend,
            "weekly_rsi14": float(round(rsi14, 2)),
            "weekly_sma10": float(round(sma10, 4)),
            "weekly_sma40": float(round(sma40, 4)),
            "weekly_close": float(round(last_close, 4)),
        }
    except Exception:
        return {"weekly_trend": "unknown", "weekly_rsi14": np.nan,
                "weekly_sma10": np.nan, "weekly_sma40": np.nan}


def h1_timing(ticker: str) -> dict:
    """
    V6.6 E8: 60-min-Timing fuer Top-Kandidaten.

    Fetcht 5 Tage 60m-Bars und klassifiziert H1-RSI2:
    - primed     : RSI2 < 20 (Einstiegsfenster offen, Entry jetzt)
    - neutral    : 20 <= RSI2 <= 50
    - overheated : RSI2 > 50 (warte auf Ruecksetzer)
    - unknown    : zu wenig Daten
    """
    try:
        tk = yf.Ticker(ticker)
        h = tk.history(period="10d", interval="60m", auto_adjust=True)
        if h is None or len(h) < 20:
            return {"h1_rsi2": np.nan, "h1_state": "unknown"}
        r2 = rsi_wilder(h["Close"], 2).iloc[-1]
        if pd.isna(r2):
            return {"h1_rsi2": np.nan, "h1_state": "unknown"}
        if r2 < 20:
            state = "primed"
        elif r2 > 50:
            state = "overheated"
        else:
            state = "neutral"
        return {"h1_rsi2": float(round(r2, 2)), "h1_state": state}
    except Exception:
        return {"h1_rsi2": np.nan, "h1_state": "unknown"}


def mtf_quality(weekly_trend: str, h1_state: str) -> tuple[str, float]:
    """
    V6.6 E8: Multi-Timeframe Composite.

    Returns (mtf_tag, mtf_adjust).
    mtf_adjust wird auf pre_score addiert, max +-0.30.
    """
    # Defaults
    tag = f"{weekly_trend}|{h1_state}"
    adj = 0.0
    if weekly_trend == "bullish":
        if h1_state == "primed":     adj = +0.30
        elif h1_state == "neutral":  adj = +0.15
        elif h1_state == "overheated": adj = -0.05  # Trend gut, aber nicht jetzt
        else:                        adj = +0.10
    elif weekly_trend == "bearish":
        if h1_state == "primed":     adj = -0.15  # Catching falling knife
        elif h1_state == "overheated": adj = -0.30
        else:                        adj = -0.20
    elif weekly_trend == "neutral":
        if h1_state == "primed":     adj = +0.10
        elif h1_state == "overheated": adj = -0.10
        else:                        adj = 0.0
    else:  # unknown
        adj = 0.0
    return tag, round(adj, 3)


def connors_setup_quality(row) -> float:
    """
    V6.3 Connors Setup Quality (0-14 extended, rescaled in pre_score):
    Base V6.1 (0-10):
    - RSI2 < 10 : 4 pts
    - RSI5 < 25 : 3 pts
    - Close > SMA200 : 2 pts
    - Close < SMA5 : 1 pt
    V6.3 Extensions:
    - E2 Cumulative RSI2 (3d-Sum < 30) : +2 pts  [Quantitativo-Studie: Sharpe 1.05→1.18]
    - E3 Pullback-Strength (3 consecutive RSI2<15 bars) : +2 pts  [Capitulation-Filter, WR +10-15%]
    """
    pts = 0
    # Base V6.1
    if row["rsi2"] < 10: pts += 4
    elif row["rsi2"] < 15: pts += 2
    if row["rsi5"] < 25: pts += 3
    elif row["rsi5"] < 30: pts += 1
    if row["close"] > row["sma200"]: pts += 2
    if row["close"] < row["sma5"]: pts += 1
    # E2: Cumulative RSI2 Bonus
    if row.get("cum_rsi2_3d", 999) < 30: pts += 2
    elif row.get("cum_rsi2_3d", 999) < 45: pts += 1
    # E3: Pullback-Strength Bonus (3 consecutive RSI2 < 15)
    if row.get("pullback_strength_3d", 0) >= 3: pts += 2
    elif row.get("pullback_strength_3d", 0) >= 2: pts += 1
    return pts


def triple_rsi_score(row) -> float:
    """Triple-RSI Score (0-10): RSI2+RSI5+RSI14 combined."""
    pts = 0
    r2, r5, r14 = row["rsi2"], row["rsi5"], row["rsi14"]
    if r2 < 5: pts += 4
    elif r2 < 10: pts += 3
    elif r2 < 20: pts += 2
    elif r2 < 30: pts += 1
    if r5 < 15: pts += 3
    elif r5 < 25: pts += 2
    elif r5 < 35: pts += 1
    if r14 < 30: pts += 3
    elif r14 < 40: pts += 2
    elif r14 < 50: pts += 1
    return min(pts, 10)


def compute_metrics(ticker: str, df: pd.DataFrame) -> dict | None:
    """Berechne alle Metriken für einen Ticker."""
    if df is None or len(df) < 210:
        return None
    df = df.copy()
    df = df.dropna(subset=["Close"])
    if len(df) < 210:
        return None
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    df["rsi2"] = rsi_wilder(close, 2)
    df["rsi5"] = rsi_wilder(close, 5)
    df["rsi14"] = rsi_wilder(close, 14)
    df["atr14"] = atr_wilder(high, low, close, 14)
    df["sma5"] = close.rolling(5).mean()
    df["sma20"] = close.rolling(20).mean()
    df["sma200"] = close.rolling(200).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    prev5 = df.iloc[-6] if len(df) >= 6 else last

    # E2 + E3: Cumulative RSI2 + Pullback-Strength über letzte 3 Tage
    rsi2_series = df["rsi2"].iloc[-3:]
    cum_rsi2_3d = float(rsi2_series.sum()) if rsi2_series.notna().all() else 999.0
    pullback_strength_3d = int(sum(1 for v in rsi2_series if pd.notna(v) and v < 15))

    # V6.6 E8: Weekly-Trend-Filter aus Daily-Bars (keine Extra-API)
    wk = weekly_trend_from_daily(df)

    row = {
        "ticker": ticker,
        "date": str(df.index[-1].date()),
        "close": float(last["Close"]),
        "rsi2": float(last["rsi2"]) if pd.notna(last["rsi2"]) else np.nan,
        "rsi5": float(last["rsi5"]) if pd.notna(last["rsi5"]) else np.nan,
        "rsi14": float(last["rsi14"]) if pd.notna(last["rsi14"]) else np.nan,
        "atr14": float(last["atr14"]) if pd.notna(last["atr14"]) else np.nan,
        "atr_pct": float(last["atr14"] / last["Close"] * 100) if pd.notna(last["atr14"]) else np.nan,
        "sma5": float(last["sma5"]) if pd.notna(last["sma5"]) else np.nan,
        "sma20": float(last["sma20"]) if pd.notna(last["sma20"]) else np.nan,
        "sma200": float(last["sma200"]) if pd.notna(last["sma200"]) else np.nan,
        "mom_1d": float((last["Close"] / prev["Close"] - 1) * 100) if prev["Close"] else 0.0,
        "mom_5d": float((last["Close"] / prev5["Close"] - 1) * 100) if prev5["Close"] else 0.0,
        "cum_rsi2_3d": cum_rsi2_3d,
        "pullback_strength_3d": pullback_strength_3d,
        # V6.6 E8 Weekly-Trend
        "weekly_trend": wk.get("weekly_trend", "unknown"),
        "weekly_rsi14": wk.get("weekly_rsi14"),
        "weekly_sma10": wk.get("weekly_sma10"),
        "weekly_sma40": wk.get("weekly_sma40"),
    }
    if any(pd.isna([row["rsi2"], row["rsi5"], row["rsi14"], row["atr14"], row["sma200"]])):
        return None

    row["connors_quality"] = connors_setup_quality(row)
    row["triple_rsi"] = triple_rsi_score(row)

    # Pre-Score V6.3: connors_quality jetzt 0-14, rescale auf 0-10-Äquivalent:
    # connors_scaled = min(connors_quality, 14) / 14 * 10
    connors_scaled = min(row["connors_quality"], 14) / 14.0 * 10.0

    # T1.1 STUB-FIX (2026-05-02 REC-RECOVERY-1 / Multi-Agent-Diagnose):
    # Bisherige Formel hatte 7 Komponenten als 5.0-Stubs (MACD+RSI/VWAP/PEAD/Put-Call/News
    # plus Insider+Options-Flow als Pre-Score-Stub). Die Stubs lagen mathematisch bei
    # einem fixen Beitrag von 0.50 * 5.0 = 2.50 pro Setup, was kombiniert mit /0.80
    # ein Pre-Score-Maximum von 6.88 erzwang. Live-Top-Scores 5.89-6.77 reflektierten
    # genau das, weshalb Threshold 7.5 strukturell unerreichbar war.
    #
    # Neue Formel: nur die echt berechneten Komponenten (Triple-RSI + Connors) gehen in
    # den Pre-Score ein, mit auf 1.0 renormalisierten Gewichten. Insider, Options-Flow,
    # Sector-RS und Market-PCR bleiben Post-Hoc-Adjusts (außerhalb dieser Funktion).
    # PEAD, MACD+RSI, VWAP, News, Per-Ticker-PCR sind echte Stubs → Tier-2-Implementation.
    #
    # Mathematisches Maximum: 10.0 (wenn triple_rsi=10 UND connors_scaled=10)
    # Realistisches Maximum mit Adjusts: ~12.0 (clampted to 10 in passes_threshold-Check
    # eigentlich nicht, aber score_threshold ist 6.5/7.0/8.0 → genug Headroom)
    pre = (
        0.667 * row["triple_rsi"] +    # vorher 0.20, renormalisiert (0.20 / 0.30)
        0.333 * connors_scaled          # vorher 0.10, renormalisiert (0.10 / 0.30)
    )
    row["connors_quality_scaled"] = round(connors_scaled, 2)
    row["pre_score"] = round(pre, 2)
    row["pre_score_v6_legacy"] = round(
        (0.20 * row["triple_rsi"] + 0.50 * 5.0 + 0.10 * connors_scaled) / 0.80,
        2
    )  # Backward-Compat-Diagnose-Spalte: zeigt was die alte Formel ergeben hätte
    return row


# DATA-FETCH-RESILIENCE-1 HOTFIX (2026-06-11): Der Cache-Reset darf NICHT laufen,
# nachdem yfinance im Prozess bereits initialisiert wurde (z.B. durch die Canary-
# Probe): rmtree + set_tz_cache_location unter offener tz-SQLite-Connection
# kippt JEDEN folgenden yf.download in OperationalError('unable to open database
# file') → 0/639-Batch. Live-Beweis 11.06. 10:14/10:17 (2x COVERAGE_FLOOR-Abort).
# Loesung: Reset ist idempotent (einmal pro Prozess, VOR erstem yf-Touch in
# main); die REAKTIVEN Resets bei echter Cache-Korruption erzwingen mit force=True.
_YF_CACHE_RESET_DONE = False


def _reset_yfinance_cache(force: bool = False):
    """COVERAGE-RESILIENCE-1 (2026-05-04): bei OperationalError disk I/O wird der
    yfinance-Cache korrupt. Default: den yfinance tz-Cache auf ein eigenes
    Temp-Verzeichnis umbiegen (DAILYINVEST_YF_CACHE_DIR, sonst
    <tempfile.gettempdir()>/dailyinvest_yf_cache).
    Das Loeschen des GLOBALEN yfinance-Caches im Home-Verzeichnis
    (~/Library/Caches/py-yfinance und ~/Library/Caches/yfinance, macOS) ist OPT-IN
    via DAILYINVEST_RESET_YF_HOME_CACHE=1, weil es Caches anderer Programme betrifft.
    HOTFIX 2026-06-11: idempotent pro Prozess (s.o.); force=True fuer reaktive Faelle."""
    global _YF_CACHE_RESET_DONE
    if _YF_CACHE_RESET_DONE and not force:
        return
    _YF_CACHE_RESET_DONE = True
    try:
        import os, shutil, tempfile
        # 1. OPT-IN: globalen yfinance-Cache im Home-Verzeichnis loeschen
        if os.environ.get("DAILYINVEST_RESET_YF_HOME_CACHE", "").strip() == "1":
            for cache_dir_name in ("py-yfinance", "yfinance"):
                home_cache = os.path.expanduser(f"~/Library/Caches/{cache_dir_name}")
                if os.path.exists(home_cache):
                    try:
                        shutil.rmtree(home_cache, ignore_errors=True)
                        print(f"[COVERAGE-RESILIENCE-1] Reset cache: {home_cache}", file=sys.stderr)
                    except Exception:
                        pass
        # 2. tz-Cache auf ein eigenes Temp-Verzeichnis umbiegen (falls die yfinance-API das kann)
        cache_dir = (os.environ.get("DAILYINVEST_YF_CACHE_DIR")
                     or os.path.join(tempfile.gettempdir(), "dailyinvest_yf_cache"))
        try:
            os.makedirs(cache_dir, exist_ok=True)
            yf.set_tz_cache_location(cache_dir)
            print(f"[COVERAGE-RESILIENCE-1] tz-cache moved to {cache_dir}", file=sys.stderr)
        except (AttributeError, Exception):
            pass
    except Exception as e:
        print(f"[COVERAGE-RESILIENCE-1] cache-reset failed: {e}", file=sys.stderr)


def _fetch_single_ticker(ticker: str, start_date, end_date, max_retries: int = 2) -> "pd.DataFrame | None":
    """COVERAGE-RESILIENCE-1: Per-Ticker-Fallback wenn Batch-Mode fehlschlägt.
    FIX-B 2026-05-08: bei yfinance Failure → Stooq als 2. Stufe (data_source_orchestrator).
    """
    import time
    for attempt in range(max_retries):
        try:
            t = yf.Ticker(ticker)
            df = t.history(
                start=start_date,
                end=end_date,
                auto_adjust=True,
                timeout=20,
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            if attempt == 0:
                _reset_yfinance_cache(force=True)  # reaktiv: Cache wirklich resetten
            time.sleep(0.5)  # Rate-Limit-Atempause

    # FIX-B 2026-05-08: yfinance hat auch Per-Ticker fehlgeschlagen → Stooq-Fallback
    _fetch_stooq = _import_stooq_fetcher()
    if _fetch_stooq is not None:
        try:
            df = _fetch_stooq(ticker, start_date, end_date)
            if df is not None and not df.empty:
                print(f"  [FIX-B Stooq-Fallback OK] {ticker}", file=sys.stderr)
                return df
        except Exception as e:  # noqa: BLE001  (fetch error: fall through to None)
            print(f"  [FIX-B Stooq-Fallback failed] {ticker}: {str(e)[:100]}", file=sys.stderr)

    return None


_STOOQ_IMPORT_WARNED = False


def _import_stooq_fetcher():
    """Imports _fetch_stooq from the sibling module tools/data_source_orchestrator.py.

    The scanner runs as `python3 tools/daily_scanner.py`, so tools/ (not the repo root)
    is on sys.path and the plain sibling import is the one that works. The
    `tools.`-prefixed form stays as a fallback for imports with the repo root on
    sys.path. An ImportError is logged once instead of being swallowed silently.
    """
    global _STOOQ_IMPORT_WARNED
    try:
        from data_source_orchestrator import _fetch_stooq  # type: ignore
        return _fetch_stooq
    except ImportError as first_err:
        try:
            from tools.data_source_orchestrator import _fetch_stooq  # type: ignore
            return _fetch_stooq
        except ImportError:
            if not _STOOQ_IMPORT_WARNED:
                _STOOQ_IMPORT_WARNED = True
                print(f"  [FIX-B Stooq-Fallback unavailable] cannot import "
                      f"data_source_orchestrator: {first_err}", file=sys.stderr)
            return None


# COVERAGE-THROTTLE-1 (2026-06-20): Yahoo rate-limitet nach ~240 Namen (6 Chunks).
# Befund Scan 06-19: Chunks 0-240 = 0-5% fail, ab 240 = 82-92% fail (US/Asia, weil
# spaeter in universe.csv) -> nur 44,6% Coverage, geografisch verzerrtes Ranking.
# Gegenmittel: (a) Baseline-Pacing zwischen Chunks haelt unter dem Yahoo-Fenster;
# (b) adaptive Backoff-Recovery, wenn eine Chunk-Erfolgsquote einbricht (Throttle-
# Signatur) -> Cache-Reset + Sleep + EIN Chunk-Retry. Gedeckelt per Zeit-Budget.
# Tunable: ENV SCAN_CHUNK_SLEEP / SCAN_THROTTLE_MAX_S, Arg throttle_recover.
CHUNK_SLEEP_DEFAULT = 1.0              # s Basis-Pause zwischen Chunks
THROTTLE_RATE = 0.5                    # Chunk-Erfolg < 50% = Throttle erkannt
THROTTLE_BACKOFF = (8.0, 20.0, 40.0)  # eskalierende Recovery-Sleeps (s)
THROTTLE_BUDGET_DEFAULT = 150.0       # max. Gesamt-Backoff-Zeit (s), dann Per-Ticker-Mop-up


def _download_chunk(chunk: list[str], start, end, downloader) -> dict:
    """Ein yfinance-Batch-Chunk -> {ticker: df}. Faengt Exceptions (429 etc.) ab."""
    out: dict = {}
    if not chunk:
        return out
    try:
        data = downloader(
            " ".join(chunk),
            start=start.date(),
            end=end.date() + timedelta(days=1),
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            # DATA-FETCH-RESILIENCE-1: Cap statt True (=1 Thread/Ticker).
            threads=8,
            timeout=30,
        )
        for t in chunk:
            try:
                if len(chunk) == 1:
                    df = data
                else:
                    df = data[t] if t in data.columns.levels[0] else None
                if df is not None and not df.empty:
                    out[t] = df
            except (KeyError, AttributeError):
                pass
    except Exception as e:
        print(f"  Chunk error: {str(e)[:100]}", file=sys.stderr)
    return out


def batch_fetch(tickers: list[str], period_days: int = 450,
                downloader=None, single_fetcher=None,
                chunk_sleep: float | None = None, sleeper=None,
                throttle_recover: bool = True,
                throttle_budget: float | None = None) -> dict:
    """Batch-Download mit yfinance + COVERAGE-THROTTLE-1-Pacing/Recovery.

    Dependency-Injection (downloader/single_fetcher/sleeper) macht die
    Throttle-Recovery offline testbar (tests/test_coverage_throttle.py).
    Default-Verhalten ist live-identisch, nur mit Pacing + Recovery ergaenzt.
    """
    import os, random, time
    if downloader is None:
        downloader = yf.download
    if single_fetcher is None:
        single_fetcher = _fetch_single_ticker
    if sleeper is None:
        sleeper = time.sleep
    if chunk_sleep is None:
        try:
            chunk_sleep = float(os.environ.get("SCAN_CHUNK_SLEEP", CHUNK_SLEEP_DEFAULT))
        except (TypeError, ValueError):
            chunk_sleep = CHUNK_SLEEP_DEFAULT
    if throttle_budget is None:
        try:
            throttle_budget = float(os.environ.get("SCAN_THROTTLE_MAX_S", THROTTLE_BUDGET_DEFAULT))
        except (TypeError, ValueError):
            throttle_budget = THROTTLE_BUDGET_DEFAULT

    # FIX-B 2026-05-08: proaktiver Cache-Reset VOR dem ersten Batch.
    _reset_yfinance_cache()

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=period_days)
    all_data: dict = {}
    chunk_size = 40
    consec_throttle = 0
    backoff_spent = 0.0

    n = len(tickers)
    for i in range(0, n, chunk_size):
        chunk = tickers[i:i+chunk_size]
        got = _download_chunk(chunk, start, end, downloader)
        all_data.update(got)
        is_last = (i + chunk_size) >= n
        rate = (len(got) / len(chunk)) if chunk else 1.0

        if (throttle_recover and rate < THROTTLE_RATE and not is_last
                and backoff_spent < throttle_budget):
            # Throttle-Signatur: eskalierender Backoff + Cache-Reset + EIN Chunk-Retry.
            backoff = THROTTLE_BACKOFF[min(consec_throttle, len(THROTTLE_BACKOFF) - 1)]
            consec_throttle += 1
            backoff_spent += backoff
            print(f"  [COVERAGE-THROTTLE-1] Chunk {i}: {len(got)}/{len(chunk)} ok "
                  f"-> throttle, backoff {backoff:.0f}s (+retry)", file=sys.stderr)
            _reset_yfinance_cache(force=True)
            sleeper(backoff)
            missing = [t for t in chunk if t not in got]
            retry = _download_chunk(missing, start, end, downloader)
            all_data.update(retry)
            if retry:
                print(f"    [COVERAGE-THROTTLE-1] recovery +{len(retry)} "
                      f"({len(all_data)} total OK)", file=sys.stderr)
        else:
            consec_throttle = 0
            if not is_last and chunk_sleep > 0:
                sleeper(chunk_sleep + random.uniform(0.0, chunk_sleep * 0.5))

    # COVERAGE-RESILIENCE-1: Per-Ticker-Mode (yfinance->Stooq) fuer die Resterampe.
    failed_tickers = [t for t in tickers if t not in all_data]
    if failed_tickers and len(failed_tickers) < len(tickers):  # nicht 100% fail (sonst Network down)
        print(f"  [COVERAGE-RESILIENCE-1] Per-Ticker-Retry fuer {len(failed_tickers)} failed", file=sys.stderr)
        for j, t in enumerate(failed_tickers):
            df = single_fetcher(t, start.date(), end.date() + timedelta(days=1))
            if df is not None:
                all_data[t] = df
            if j % 20 == 0 and j > 0:
                print(f"    [retry] {j}/{len(failed_tickers)} done, {len(all_data)} total OK", file=sys.stderr)
    elif failed_tickers and len(failed_tickers) == len(tickers):
        print(f"  [COVERAGE-RESILIENCE-1] 100% Batch-Fail: yfinance/Network down, kein Per-Ticker-Retry", file=sys.stderr)

    return all_data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Override scan date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of tickers scanned (debug)")
    ap.add_argument("--tier", default=None, help="Filter to tier A/B/C")
    ap.add_argument("--top", type=int, default=25, help="Print top N candidates")
    # SCAN-IMMUTABILITY-1 (2026-04-19): Write-once-Schutz fuer Morgen-Scan
    ap.add_argument("--force", action="store_true",
                    help="Existierendes Scan-Artefakt ueberschreiben (Notfall)")
    ap.add_argument("--rescan", action="store_true",
                    help="In separaten __RESCAN_HHMMSS-Namespace schreiben, "
                         "Morgen-Artefakt bleibt unangetastet")
    # SCAN-OF-ORCHESTRATION-1 (V7.9.10, 2026-04-27): Pipeline-Order-Fix.
    # Default 0/False/False = exakt altes Verhalten (freeze-konform).
    ap.add_argument("--wait-options-flow", type=int, default=0,
                    metavar="SEC",
                    help="Sekunden warten, bis options_flow_{date}.json "
                         "verfuegbar ist (0=kein Wait, default).")
    ap.add_argument("--require-options-flow", action="store_true",
                    help="Bei Timeout exit 2 (verhindert leeren OF-Join). "
                         "Erfordert --wait-options-flow > 0 fuer Wirkung.")
    ap.add_argument("--require-fresh-options-flow", action="store_true",
                    help="Stale OF-Datei (mtime > 18h alt) wie fehlend "
                         "behandeln. Schuetzt vor Vortags-Datei-Join.")
    # DATA-FETCH-RESILIENCE-1 (2026-06-10): laut failen statt Muell ranken.
    ap.add_argument("--no-env-guard", action="store_true",
                    help="Env-Guards (certifi/Canary/Coverage-Floor) NICHT "
                         "erzwingen: Notfall-Override, nur bewusst nutzen.")
    ap.add_argument("--coverage-floor", type=float, default=COVERAGE_FLOOR_DEFAULT,
                    metavar="0..1",
                    help=f"Min. Daten-Coverage nach batch_fetch "
                         f"(default {COVERAGE_FLOOR_DEFAULT}); darunter Abort Exit {EXIT_COVERAGE_FLOOR}.")
    # SCAN-CATCHUP-QUALITY-GATE-1 (2026-06-29): valid-results-Fill-Floor.
    ap.add_argument("--valid-fill-floor", type=float, default=VALID_FILL_FLOOR_DEFAULT,
                    metavar="0..1",
                    help=f"Min. Anteil GUELTIGER Scan-Ergebnisse nach Scoring "
                         f"(default {VALID_FILL_FLOOR_DEFAULT}, == data_health "
                         f"SCAN_FILL_LT_80); darunter Abort Exit {EXIT_VALID_FILL_FLOOR} "
                         f"ohne kanonisches Artefakt -> Catch-up-Battery ersetzt.")
    args = ap.parse_args()

    scan_date = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")

    # Load universe
    universe = []
    with open(UNIVERSE_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if args.tier and row["tier"] != args.tier:
                continue
            universe.append(row)
    if args.limit:
        universe = universe[:args.limit]
    tickers = [u["ticker"] for u in universe]
    tier_map = {u["ticker"]: u for u in universe}
    print(f"Scanning {len(tickers)} tickers ...", file=sys.stderr)

    # --- DATA-FETCH-RESILIENCE-1: Env-Guard VOR dem teuren Batch ---
    cert = certifi_status()
    if cert["ok"]:
        pin_ssl_to_certifi(cert)
    if not args.no_env_guard:
        if not cert["ok"]:
            p = write_scan_abort(scan_date, "CERTIFI_BROKEN", {"certifi": cert})
            print(f"\n[ENV-GUARD] ABORT: certifi-CA-Bundle kaputt ({cert.get('error')}). "
                  f"KEIN Scan geschrieben (Muell-Schutz). Details: {p.name}. "
                  f"Fix: pip force-reinstall certifi.", file=sys.stderr)
            sys.exit(EXIT_ENV_GUARD)
        # HOTFIX 2026-06-11: Cache-Reset MUSS vor dem ersten yfinance-Touch
        # passieren (Canary initialisiert yfinance), sonst zerstoert der
        # spaetere Reset in batch_fetch die offene tz-Cache-Connection.
        _reset_yfinance_cache()
        _canary_end = datetime.now(timezone.utc).replace(tzinfo=None)
        _canary_start = _canary_end - timedelta(days=15)
        probe = canary_probe(
            lambda t: _fetch_single_ticker(
                t, _canary_start.date(), _canary_end.date() + timedelta(days=1),
                max_retries=1)
        )
        if not probe["ok"]:
            p = write_scan_abort(scan_date, "CANARY_DEAD",
                                 {"probe": probe, "certifi": cert})
            print(f"\n[ENV-GUARD] ABORT: alle Canaries tot ({', '.join(probe['failed'])}) "
                  f"Datenumgebung down (Cert/Netz/Bot-Wall), nicht der Markt. "
                  f"KEIN Scan geschrieben. Details: {p.name}.", file=sys.stderr)
            sys.exit(EXIT_ENV_GUARD)
        print(f"[ENV-GUARD] ok: certifi {cert['size_bytes']}B, "
              f"canaries {probe['n_ok']}/{len(probe['canaries'])} "
              f"({', '.join(probe['ok_tickers'])})", file=sys.stderr)

    data = batch_fetch(tickers)
    print(f"Got data for {len(data)} tickers", file=sys.stderr)

    # --- DATA-FETCH-RESILIENCE-1: Coverage-Floor nach dem Batch ---
    cov = coverage_guard(len(data), len(tickers), args.coverage_floor)
    if not cov["ok"]:
        if args.no_env_guard:
            print(f"[ENV-GUARD] Coverage {cov['ratio']:.0%} unter Floor "
                  f"{cov['floor']:.0%}, Override aktiv, Scan laeuft weiter "
                  f"(BEWUSSTES RISIKO).", file=sys.stderr)
        else:
            p = write_scan_abort(scan_date, "COVERAGE_FLOOR", cov)
            print(f"\n[ENV-GUARD] ABORT: nur {cov['n_data']}/{cov['n_universe']} Ticker "
                  f"mit Daten ({cov['ratio']:.0%} < Floor {cov['floor']:.0%}). "
                  f"KEIN Scan geschrieben (Muell-Schutz). Details: {p.name}.",
                  file=sys.stderr)
            sys.exit(EXIT_COVERAGE_FLOOR)
    # Erfolgsfall: Abort-Marker des Tages aufraeumen (falls frueher Lauf failte)
    try:
        _abort_p = TOOLS_DIR / f"scan_abort_{scan_date}.json"
        if _abort_p.exists():
            _abort_p.unlink()
            print(f"[ENV-GUARD] scan_abort_{scan_date}.json geloescht (Lauf jetzt ok)",
                  file=sys.stderr)
    except Exception:
        pass

    # V6.4 E5: Sektor-RS-Enrichment (optional, wenn sector_rs_YYYY-MM-DD.json existiert)
    sector_rs_map = {}
    sector_rs_path = TOOLS_DIR / f"sector_rs_{scan_date}.json"
    if sector_rs_path.exists():
        try:
            with open(sector_rs_path, encoding="utf-8") as f:
                sector_rs_data = json.load(f)
            sector_rs_map = sector_rs_data.get("ticker_map", {})
            print(f"[V6.4] Sector-RS geladen: {len(sector_rs_map)} Ticker", file=sys.stderr)
        except Exception as e:
            print(f"[V6.4] Sector-RS konnte nicht gelesen werden: {e}", file=sys.stderr)

    # V6.7 E7F: Insider/Congress-Enrichment (optional, wenn insider_YYYY-MM-DD.json existiert)
    insider_map = {}
    insider_path = TOOLS_DIR / f"insider_{scan_date}.json"
    if insider_path.exists():
        try:
            with open(insider_path, encoding="utf-8") as f:
                insider_data = json.load(f)
            insider_map = insider_data.get("ticker_map", {})
            print(f"[V6.7] Insider-Feed geladen: {len(insider_map)} Ticker geflagged", file=sys.stderr)
        except Exception as e:
            print(f"[V6.7] Insider-Feed konnte nicht gelesen werden: {e}", file=sys.stderr)

    # V6.8 E10F: Options-Flow-Enrichment (optional, wenn options_flow_YYYY-MM-DD.json existiert)
    options_flow_map = {}
    market_pcr_tag = "unknown"
    market_pcr_adjust_val = 0.0
    market_pcr_reason = "no_data"
    market_pcr_raw = {}
    options_flow_path = TOOLS_DIR / f"options_flow_{scan_date}.json"

    # SCAN-OF-ORCHESTRATION-1 Fix (V7.9.10, 2026-04-27):
    # Vor dem Existenz-Check eine Wait-Phase (default 0s = altes Verhalten).
    # Bei aktiviertem --require-options-flow + Wait > 0 fail-hart, falls
    # OF nach Timeout immer noch fehlt oder stale ist.
    of_wait = wait_for_options_flow(
        options_flow_path,
        max_wait_s=args.wait_options_flow,
        require_fresh=args.require_fresh_options_flow,
        log_fn=lambda msg: print(msg, file=sys.stderr),
    )
    if args.wait_options_flow > 0:
        print(f"[V6.8] OF-Wait-Result: exists={of_wait.exists} "
              f"fresh={of_wait.fresh} waited={of_wait.waited_s:.1f}s "
              f"reason={of_wait.reason}", file=sys.stderr)
    fail_hard_if_required(
        of_wait,
        require_options_flow=args.require_options_flow,
        require_fresh=args.require_fresh_options_flow,
        of_path=options_flow_path,
        log_fn=lambda msg: print(msg, file=sys.stderr),
    )

    # S5 NEWS-SENTIMENT (2026-05-06 Mega-Audit Tier-1):
    # tools/news_sentiment_YYYY-MM-DD.json wird vom Cron via news_sentiment_fetch.py erzeugt.
    # ticker_map: { ticker: { sentiment_score (-1..+1), article_count } }
    # Score-Adjust: news_score * 0.05 (max ±0.05 auf pre_score), analog zu insider/flow-Logik.
    news_sentiment_map = {}
    news_sentiment_path = TOOLS_DIR / f"news_sentiment_{scan_date}.json"
    if news_sentiment_path.exists():
        try:
            with open(news_sentiment_path, encoding="utf-8") as f:
                news_data = json.load(f)
            news_sentiment_map = news_data.get("ticker_map", {})
            n_with_news = news_data.get("n_with_news", 0)
            print(f"[S5-NEWS] Sentiment geladen: {n_with_news}/{len(news_sentiment_map)} Tickers mit News", file=sys.stderr)
        except Exception as e:
            print(f"[S5-NEWS] Sentiment konnte nicht gelesen werden: {e}", file=sys.stderr)

    if options_flow_path.exists():
        try:
            with open(options_flow_path, encoding="utf-8") as f:
                of_data = json.load(f)
            options_flow_map = of_data.get("ticker_map", {})
            market_pcr_raw = of_data.get("market_pcr", {}) or {}
            market_pcr_tag = market_pcr_raw.get("tag", "unknown")
            mpa = of_data.get("market_pcr_adjust")
            # V6.8: market_pcr_adjust is saved as [value, reason] tuple in JSON
            if isinstance(mpa, (list, tuple)) and len(mpa) == 2:
                market_pcr_adjust_val = float(mpa[0])
                market_pcr_reason = str(mpa[1])
            print(f"[V6.8] Options-Flow geladen: {len(options_flow_map)} Ticker, PCR={market_pcr_tag} ({market_pcr_adjust_val:+.2f})", file=sys.stderr)
        except Exception as e:
            print(f"[V6.8] Options-Flow konnte nicht gelesen werden: {e}", file=sys.stderr)

    results = []
    for t in tickers:
        df = data.get(t)
        metrics = compute_metrics(t, df)
        if metrics is None:
            continue
        meta = tier_map[t]
        metrics.update({
            "name": meta["name"],
            "tier": meta["tier"],
            "asset_class": meta["asset_class"],
            "region": meta["region"],
            "sector": meta["sector"],
            "atr_multi_sl": float(meta["atr_multi_sl"]),
            "atr_multi_tp": float(meta["atr_multi_tp"]),
            "score_threshold": float(meta["score_threshold"]),
        })

        # V6.4 E5: Sektor-RS-Enrichment + Score-Adjust
        sr = sector_rs_map.get(t, {})
        sector_rs_score = float(sr.get("sector_rs_score", 0.0))
        metrics["sector_rs_score"] = sector_rs_score
        metrics["gics_sector"] = sr.get("gics_sector")
        metrics["sector_regime_tag"] = sr.get("regime_tag", "n/a")
        # Bonus/Malus auf pre_score: ±0.15 pro RS-Punkt (max ±0.30)
        rs_adjust = round(sector_rs_score * 0.15, 3)
        metrics["pre_score_raw"] = metrics["pre_score"]
        metrics["pre_score"] = round(metrics["pre_score"] + rs_adjust, 2)
        metrics["sector_rs_adjust"] = rs_adjust

        # V6.7 E7F: Insider/Congress-Score-Adjust (max ±0.20)
        insider_info = insider_map.get(t, {})
        insider_score = float(insider_info.get("insider_score", 0.0))
        insider_adjust = round(insider_score * 0.10, 3)  # max ±0.20 (score ∈ [-2,+2])
        if "insider" in DISABLED_LAYERS:
            insider_adjust = 0.0
        metrics["insider_score"] = insider_score
        metrics["insider_adjust"] = insider_adjust
        metrics["insider_breakdown"] = insider_info.get("score_breakdown", {})
        metrics["pre_score_pre_insider"] = metrics["pre_score"]
        metrics["pre_score"] = round(metrics["pre_score"] + insider_adjust, 2)

        # V6.8 E10F: Options-Flow-Score-Adjust (max ±0.20) + globaler Markt-PCR-Tilt (±0.10)
        of_info = options_flow_map.get(t, {})
        options_flow_score = float(of_info.get("options_flow_score", 0.0))
        options_flow_adjust = round(options_flow_score * 0.10, 3)  # max ±0.20 (score ∈ [-2,+2])
        if "options_flow" in DISABLED_LAYERS:
            options_flow_adjust = 0.0
        metrics["options_flow_score"] = options_flow_score
        metrics["options_flow_adjust"] = options_flow_adjust
        metrics["flow_tag"] = of_info.get("flow_tag", "no_data")
        metrics["options_flow_breakdown"] = of_info.get("breakdown", {})
        metrics["pre_score_pre_options"] = metrics["pre_score"]
        metrics["pre_score"] = round(metrics["pre_score"] + options_flow_adjust, 2)
        # Globaler Markt-PCR-Tilt
        if "market_pcr" in DISABLED_LAYERS:
            market_pcr_adjust_val = 0.0
        metrics["market_pcr_tag"] = market_pcr_tag
        metrics["market_pcr_adjust"] = market_pcr_adjust_val
        metrics["market_pcr_reason"] = market_pcr_reason
        if market_pcr_adjust_val != 0.0:
            metrics["pre_score"] = round(metrics["pre_score"] + market_pcr_adjust_val, 2)

        # NEWS-SENTIMENT-ADJUST (S5 KW20 / Hebel A KW20 / R1 KW21):
        # news_sentiment_score in [-1.0, +1.0]
        # Adjust-Multiplier HÄNGT VON METHODE AB (Recherche-validierte Konfidenz-Skala):
        # - sentiment_method='findpo'  → 0.12 (max ±0.12)  [R1 KW21: arXiv 2507.18417 +11% Accuracy]
        # - sentiment_method='finbert' → 0.10 (max ±0.10)  [Hebel A KW20: kontext-aware Mid-Tier]
        # - sentiment_method='keyword' → 0.05 (max ±0.05)  [S5 KW20: primitiver Keyword-Counter]
        news_info = news_sentiment_map.get(t, {})
        news_sentiment_score = float(news_info.get("sentiment_score", 0.0))
        news_method = news_info.get("sentiment_method", "keyword")
        if news_method == "findpo":
            news_multiplier = 0.12
        elif news_method == "finbert":
            news_multiplier = 0.10
        else:
            news_multiplier = 0.05
        news_adjust = round(news_sentiment_score * news_multiplier, 3)
        if "news" in DISABLED_LAYERS:
            news_adjust = 0.0
        metrics["news_sentiment_score"] = news_sentiment_score
        metrics["news_sentiment_method"] = news_method
        metrics["news_sentiment_model"] = news_info.get("sentiment_model")
        metrics["news_adjust"] = news_adjust
        metrics["news_article_count"] = int(news_info.get("article_count", 0))
        metrics["pre_score_pre_news"] = metrics["pre_score"]
        metrics["pre_score"] = round(metrics["pre_score"] + news_adjust, 2)

        # V6.6 E8 Placeholder (H1-Timing kommt im Post-Pass fuer Top-Kandidaten)
        metrics["h1_rsi2"] = None
        metrics["h1_state"] = "unchecked"
        metrics["mtf_tag"] = f"{metrics.get('weekly_trend','unknown')}|unchecked"
        metrics["mtf_adjust"] = 0.0

        metrics["passes_threshold"] = float(metrics.get("pre_score", 0)) >= float(meta["score_threshold"])
        results.append(metrics)

    # V6.6 E8: H1-Timing-Post-Pass NUR fuer Top-Kandidaten
    # (vermeidet 600 zusaetzliche yfinance-Calls; Top-40 nach pre_score)
    # FIX BUG-MTF-1 (2026-04-16): Filter war auf passes_threshold, Zirkelschluss:
    # H1 konnte nie einen Sub-Threshold-Ticker ueber die Schwelle heben.
    # Neu: immer Top-40 nach pre_score, unabhaengig vom Threshold-Status.
    mtf_candidates = sorted(
        [r for r in results if r["tier"] != "X"],
        key=lambda r: (r["connors_quality"], r["pre_score"]),
        reverse=True,
    )[:40]
    if mtf_candidates:
        print(f"[V6.6 E8] H1-Timing fuer {len(mtf_candidates)} Top-Kandidaten ...", file=sys.stderr)
    for r in mtf_candidates:
        h1 = h1_timing(r["ticker"])
        r["h1_rsi2"] = h1["h1_rsi2"]
        r["h1_state"] = h1["h1_state"]
        tag, adj = mtf_quality(r.get("weekly_trend", "unknown"), h1["h1_state"])
        r["mtf_tag"] = tag
        r["mtf_adjust"] = adj
        r["pre_score_pre_mtf"] = r["pre_score"]
        r["pre_score"] = round(r["pre_score"] + adj, 2)
        r["passes_threshold"] = float(r["pre_score"]) >= float(r["score_threshold"])

    # --- SCAN-CATCHUP-QUALITY-GATE-1 (2026-06-29): valid-results-Fill-Gate ---
    # Coverage-Floor (oben) hat bestanden (FETCH ok), aber das Scoring kann bei
    # transientem yfinance-Throttle (kurze/lueckenhafte Serien -> Indikatoren NaN)
    # trotzdem zu wenige GUELTIGE Ergebnisse liefern. Ein solcher degradierter-
    # aber-vorhandener Scan wuerde geschrieben, catch_up_skip() (prueft nur
    # Existenz) ueberlisten und einen Ganztages-NO-TRADE auf transientem Throttle
    # zementieren (Beweis 06-18..06-23: 24,4/44,6/27,7/50,1 %). Sub-Floor (< 80 %,
    # == data_health SCAN_FILL_LT_80/H) -> KEIN Artefakt (weder CSV noch JSON),
    # scan_abort-Marker, Exit 6 -> scan_<date>.json bleibt absent ->
    # catch_up_skip=False -> 08:45/09:29/10:30 laufen erneut (Self-Heal).
    # Gilt fuer den kanonischen Pfad; --rescan schreibt bei VORHANDENEM Kanon
    # forensisch in den Namespace (von catch_up_skip ignoriert) und bleibt ungated.
    #
    # SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1 (2026-07-06, V7.37): der Guard lief
    # bisher NUR im Nicht-Rescan-Pfad: Annahme: `--rescan` schreibt IMMER in den
    # forensischen Namespace. Diese Annahme bricht, wenn scan_initial per Gate
    # abgebrochen ist: dann ist das kanonische scan_<date>.json ABWESEND, und
    # write_immutable() nimmt den First-Write-Zweig -> der pipeline-eigene
    # `scan_rescore --rescan`-Schritt schreibt den DEGRADIERTEN Scan in den
    # KANONISCHEN Namespace (Live 07-01 36,8% / 07-02 66,0%), ueberlistet
    # catch_up_skip() und zementiert genau den Throttle-Zombie-NO-TRADE, den V7.32
    # verhindern sollte. Fix: der Guard laeuft AUCH bei --rescan, sobald das
    # kanonische Artefakt fehlt (rescan agiert dann als Ersatz-Producer). Ist der
    # Kanon bereits vorhanden (normaler Re-Score nach scan_initial-Erfolg), bleibt
    # --rescan ungated (Namespace, catch_up_skip-neutral). Erholt sich yfinance bis
    # zum Rescore (Fill >= Floor), wird der Rescan zum GESUNDEN kanonischen Producer
    # genau erwuenscht. --no-env-guard uebersteuert wie gehabt (manuelle Forensik).
    canonical_json = TOOLS_DIR / f"scan_{scan_date}.json"
    _canon_exists = canonical_json.exists()
    rescan_as_producer = bool(args.rescan) and not _canon_exists
    if fill_guard_applies(bool(args.rescan), _canon_exists):
        vfill = valid_fill_guard(len(results), len(tickers), args.valid_fill_floor)
        if not vfill["ok"]:
            if args.no_env_guard:
                print(f"[ENV-GUARD] Valid-Fill {vfill['ratio']:.0%} unter Floor "
                      f"{vfill['floor']:.0%}, Override aktiv, Scan wird trotzdem "
                      f"geschrieben (BEWUSSTES RISIKO).", file=sys.stderr)
            else:
                _guard_mode = "rescan-as-producer" if rescan_as_producer else "scan_initial"
                p = write_scan_abort(scan_date, "VALID_FILL_FLOOR",
                                     {**vfill, "guard_path": _guard_mode})
                print(f"\n[ENV-GUARD] ABORT ({_guard_mode}): nur {vfill['n_valid']}/"
                      f"{vfill['n_universe']} GUELTIGE Scan-Ergebnisse "
                      f"({vfill['ratio']:.0%} < Floor {vfill['floor']:.0%}). KEIN "
                      f"kanonischer Scan geschrieben (SCAN-CATCHUP-QUALITY-GATE-1): "
                      f"Catch-up-Battery ersetzt. Details: {p.name}.", file=sys.stderr)
                sys.exit(EXIT_VALID_FILL_FLOOR)

    # Save CSV (SCAN-IMMUTABILITY-1: write-once)
    out_csv = TOOLS_DIR / f"scan_{scan_date}.csv"
    actual_csv = out_csv
    if results:
        # Union aller Keys, weil MTF/Insider/Options-Flow-Fields nur bei Top-Kandidaten gesetzt werden
        fieldnames = []
        seen = set()
        for r in results:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)

        def _write_csv(p):
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(results)

        try:
            actual_csv = write_immutable(
                out_csv, _write_csv,
                writer="daily_scanner.py",
                writer_version=SCANNER_VERSION,
                force=args.force, rescan=args.rescan,
                metadata={"scan_date": scan_date, "universe_size": len(tickers),
                          "valid_results": len(results)},
            )
        except ImmutableArtifactError as e:
            print(f"\n[SCAN-IMMUTABILITY-1] {e}", file=sys.stderr)
            sys.exit(3)

    # Top setups: sort by connors_quality desc, then pre_score desc.
    # FIX 2026-06-01: User-Blacklist HART ausschliessen (optional, config/blacklist.txt).
    _ranked = sorted(
        [r for r in results if r["tier"] != "X"],
        key=lambda r: (r["connors_quality"], r["pre_score"]),
        reverse=True,
    )
    blacklist_hits = [r["ticker"] for r in _ranked if r["ticker"] in USER_BLACKLIST]
    _clean = [r for r in _ranked if r["ticker"] not in USER_BLACKLIST]
    if blacklist_hits:
        print(f"\n[N2-BLACKLIST] {len(blacklist_hits)} Ticker aus Top-Setups entfernt "
              f"(user blacklist): {', '.join(blacklist_hits)}", file=sys.stderr)

    # UNIVERSE-TRADABLE-1: Direkt-Slots nur fuer direkt kaufbare Titel; Rest -> Hebel-Watch.
    # Annotation auf ALLEN results (in-place), damit ticker_map das Feld mitbekommt.
    for r in results:
        r["tradability"] = tradability(r.get("ticker", ""), r.get("asset_class", ""))
    top = [r for r in _clean if r.get("tradability") == "direct"][:args.top]
    hebel_watch = [
        r for r in _clean
        if r.get("tradability") == "hebel_only"
        and float(r.get("pre_score") or 0) >= HEBEL_WATCH_MIN_SCORE
    ][:HEBEL_WATCH_MAX_N]
    _n_nondirect_ranked = sum(1 for r in _clean if r.get("tradability") == "hebel_only")
    if _n_nondirect_ranked:
        print(f"\n[TRADABLE-1] {_n_nondirect_ranked} nicht-direkte Titel aus Direkt-Top "
              f"ferngehalten; {len(hebel_watch)} davon als Hebel-Watch (pre_score>="
              f"{HEBEL_WATCH_MIN_SCORE}): {', '.join(r['ticker'] for r in hebel_watch) or '-'}",
              file=sys.stderr)

    print(f"\n=== TOP {args.top} V6.8 SETUPS ({scan_date}) ===")
    print(f"{'Ticker':<10} {'Tier':<4} {'Region':<6} {'CQ':<4} {'PreS':<5} {'RSI2':<6} {'RSI5':<6} {'ATR%':<6} {'Close':<10} {'WkTrend':<9} {'H1':<11}")
    for r in top:
        print(f"{r['ticker']:<10} {r['tier']:<4} {r['region']:<6} {r['connors_quality']:<4} {r['pre_score']:<5} "
              f"{r['rsi2']:<6.1f} {r['rsi5']:<6.1f} {r['atr_pct']:<6.2f} {r['close']:<10.2f} "
              f"{str(r.get('weekly_trend','?')):<9} {str(r.get('h1_state','?')):<11}")

    # V6.8 JSON Snapshot (erweitert + Ticker-Map fuer Empfehlung/Tracking/Journal)
    out_json = TOOLS_DIR / f"scan_{scan_date}.json"
    summary = {
        "scan_date": scan_date,
        "version": "V6.8",
        "universe_size": len(tickers),
        "valid_results": len(results),
        "blacklist_excluded": blacklist_hits,
        "market_pcr_tag": market_pcr_tag,
        "market_pcr_adjust": market_pcr_adjust_val,
        "market_pcr_reason": market_pcr_reason,
        "market_pcr_raw": market_pcr_raw,
        "top_setups": [
            {k: r.get(k) for k in [
                "ticker","name","tier","region","sector","asset_class","close",
                "rsi2","rsi5","rsi14","atr_pct",
                "connors_quality","connors_quality_scaled","triple_rsi",
                "cum_rsi2_3d","pullback_strength_3d",
                "pre_score","pre_score_raw","pre_score_pre_mtf",
                "sector_rs_score","sector_rs_adjust","gics_sector","sector_regime_tag",
                "weekly_trend","weekly_rsi14","h1_rsi2","h1_state","mtf_tag","mtf_adjust",
                "insider_score","insider_adjust","insider_breakdown","pre_score_pre_insider",
                "options_flow_score","options_flow_adjust","flow_tag","options_flow_breakdown","pre_score_pre_options",
                "market_pcr_tag","market_pcr_adjust","market_pcr_reason",
                "atr_multi_sl","atr_multi_tp","mom_1d","mom_5d","passes_threshold",
                "tradability",
            ]}
            for r in top
        ],
        # UNIVERSE-TRADABLE-1: nicht-direkte Hochscorer als Hebel-Kandidaten
        # (KO/Faktor ab Score 8.0), verdraengen keine Direkt-Slots mehr.
        "hebel_watch": [
            {k: r.get(k) for k in [
                "ticker","name","tier","region","sector","asset_class","close",
                "rsi2","rsi5","atr_pct","connors_quality","pre_score",
                "weekly_trend","h1_state","mom_1d","mom_5d",
                "passes_threshold","tradability",
            ]}
            for r in hebel_watch
        ],
        "n_nondirect_ranked": _n_nondirect_ranked,
        "ticker_map": {r["ticker"]: r for r in results},
    }
    def _write_json(p):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)

    try:
        actual_json = write_immutable(
            out_json, _write_json,
            writer="daily_scanner.py",
            writer_version=SCANNER_VERSION,
            force=args.force, rescan=args.rescan,
            metadata={"scan_date": scan_date, "top_count": len(top)},
        )
    except ImmutableArtifactError as e:
        print(f"\n[SCAN-IMMUTABILITY-1] {e}", file=sys.stderr)
        sys.exit(3)

    print(f"\nSaved: {actual_csv}")
    print(f"Saved: {actual_json}")
    if actual_csv != out_csv or actual_json != out_json:
        print("[SCAN-IMMUTABILITY-1] Rescan-Namespace aktiv: Morgen-Artefakt unangetastet.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
