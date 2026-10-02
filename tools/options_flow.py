"""
V6.8 E10F Options-Flow FREE-Stack.

Kostenfreier Ersatz für Unusual Whales API ($50/mo). Zwei Quellen:
  1. CBOE Public Put/Call-Ratio: Markt-weites Sentiment, Daily CSV (gratis)
  2. yfinance `Ticker.option_chain()`: ATM Call-OI-Spike vs 20d-Median (gratis)

Barchart Unusual-Options Free-Tier ist ohne Auth/Session-Cookie schwer scrapbar und bleibt
ausgeklammert. **V6.8.1 Shadow-Phase** bringt stattdessen den lokalen OI-Daily-Cache
(SQLite, oi_cache.sqlite): nach 20 Handelstagen liefert dieser einen echten OI-Spike-Score
ohne externes Scraping. Barchart wird auf **V6.8.2** verschoben und nur dann gebaut, wenn
der lokale Cache sich als unzureichend erweist (Entscheidung im 2026-04-29-Audit).

Output:
  tools/options_flow_YYYY-MM-DD.json
  {
    "date": "...",
    "version": "V6.8-E10F",
    "market_pcr": { "total": 0.94, "equity": 0.78, "index": 1.28, "tag": "neutral" },
    "sources": {...},
    "ticker_map": {
      "AAPL": {
        "options_flow_score": -2..+2,
        "call_oi_spike": 3.7,
        "put_oi_spike": 0.9,
        "iv_skew_tag": "call_bias",
        "flow_tag": "bullish_flow",
        "breakdown": {...}
      },
      ...
    }
  }

Score-Matrix (per Ticker, clipped [-2, +2]):
  +2  Call-OI-Spike > 5× 20d-Median                                  (extreme bullish flow)
  +1  Call-OI-Spike > 3× 20d-Median                                  (elevated bullish flow)
  +1  IV-Skew call-biased (ATM-Call-IV > ATM-Put-IV mit >2 pct-points) (call premium bid)
  -1  Put-OI-Spike  > 3× 20d-Median                                  (elevated bearish flow)
  -2  Put-OI-Spike  > 5× 20d-Median                                  (extreme bearish flow)

Markt-Tilt (gilt für alle Ticker separat, nicht im per-Ticker-Score):
  market_pcr.total >= 1.20  → "defensive_market" → Scanner wird MODERAT-Malus ziehen
  market_pcr.total <= 0.70  → "euphoric_market"  → Scanner zieht MODERAT-Malus (contrarian)
  0.70 < pcr < 1.20         → "neutral"          → no adjust

Politik:
  - Keine Paid-APIs (Projektregel, siehe CLAUDE.md Abschnitt 5).
  - Nur Top-K-Kandidaten aus scan_YYYY-MM-DD.json werden per-Ticker analysiert
    (1 option_chain-Call kostet 2-3 yfinance-Requests; bei 600 Tickern wäre das zu teuer).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import statistics
import sys
import time
import urllib.request
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

# Optional: curl_cffi (yfinance >=1.3 verlangt das) bzw. requests als Fallback
# OPTIONS-FLOW-CONSISTENCY-1 (2026-04-19)
try:
    from curl_cffi import requests as cffi_requests  # yfinance-kompatible Session
    _HAVE_CURL_CFFI = True
except ImportError:
    _HAVE_CURL_CFFI = False
try:
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry  # urllib3 >=1.26
    except ImportError:
        from requests.packages.urllib3.util.retry import Retry  # type: ignore
    _HAVE_REQUESTS = True
except ImportError:
    _HAVE_REQUESTS = False

TOOLS_DIR = Path(__file__).parent
# User-Agent for outbound HTTP (CBOE/Yahoo). Set DAILYINVEST_USER_AGENT to your
# own value (for example "daily-invest-oss/1.0 (you@example.com)").
# NOTE: the anti-bot measures below (curl_cffi Chrome impersonation, UA rotation,
# CBOE HTML scraping) may conflict with data provider terms. Use at your own risk.
UA_STRING = os.environ.get("DAILYINVEST_USER_AGENT") or "DailyInvest-OSS/1.0"

# OPTIONS-FLOW-CONSISTENCY-1 (2026-04-19) --------------------------------------
# Am 15./16.04. yfinance Hit-Rate = 0 %, am 17.04. = 67,5 % ohne Code-Aenderung.
# Hypothesen: (a) fehlende Session-Reuse, (b) einzelner UA wird rate-gelimitet,
# (c) zu strikte Expiry-Regel (>=7d), (d) kein Retry, (e) sporadische Spot-Aussetzer.
# Fix: Shared Session + Retry + UA-Pool + robustere Expiry- und Spot-Fallbacks +
# Circuit-Breaker. Kein Eingriff in daily_scanner.py (Feature-Freeze-konform).
UA_POOL = [
    UA_STRING,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
_YF_SESSION = None            # lazy
_YF_SESSION_KIND = None       # "curl_cffi" | "requests" | None
_YF_ERR_STREAK = {"n": 0}     # Circuit-Breaker
_YF_ERR_THRESHOLD = 8          # 8 aufeinanderfolgende Fehler -> abort


def _build_yf_session():
    """Shared Session fuer yfinance.

    yfinance >=1.3 verlangt curl_cffi-Session (kein 'requests.Session'),
    aeltere yfinance akzeptieren requests. Wenn keins verfuegbar: None
    zurueckgeben, dann regelt yfinance intern.

    Rotierender UA ist auf beiden Pfaden moeglich (curl_cffi und requests
    erlauben .headers-Set)."""
    global _YF_SESSION, _YF_SESSION_KIND
    if _YF_SESSION is not None:
        return _YF_SESSION

    # 1) curl_cffi bevorzugt (yfinance >=1.3 kompatibel)
    if _HAVE_CURL_CFFI:
        try:
            # impersonate=chrome laesst den Request wie ein echter Chrome aussehen
            s = cffi_requests.Session(impersonate="chrome124")
            s.headers.update({
                "User-Agent": random.choice(UA_POOL),
                "Accept-Language": "en-US,en;q=0.9",
            })
            _YF_SESSION = s
            _YF_SESSION_KIND = "curl_cffi"
            return s
        except Exception as e:
            print(f"[E10F] curl_cffi session failed, fallback: {e}", file=sys.stderr)

    # 2) requests-Session als Fallback (aeltere yfinance)
    if _HAVE_REQUESTS:
        s = requests.Session()
        s.headers.update({
            "User-Agent": random.choice(UA_POOL),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            retry = Retry(
                total=4, connect=3, read=3, status=3,
                backoff_factor=0.8,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                raise_on_status=False,
            )
        except TypeError:
            retry = Retry(total=4, connect=3, read=3, status=3, backoff_factor=0.8,
                          status_forcelist=(429, 500, 502, 503, 504))
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _YF_SESSION = s
        _YF_SESSION_KIND = "requests"
        return s

    # 3) Kein Session-Backend -> yfinance regelt intern
    _YF_SESSION_KIND = None
    return None


def _rotate_ua():
    """Laenge-erhaltenes UA-Rotate auf bestehender Session."""
    s = _YF_SESSION
    if s is None:
        return
    s.headers["User-Agent"] = random.choice(UA_POOL)


_YF_SESSION_ACCEPTED = None  # tri-state: None=unknown, True=yfinance akzeptiert unsere session, False=abgelehnt


def _yf_ticker(sym: str):
    """yfinance-Ticker mit Shared-Session wenn akzeptiert, sonst ohne.

    Yahoo/yfinance kann eine requests.Session ablehnen (>=1.3 verlangt curl_cffi).
    Wir probieren beim ersten Mal und cachen das Ergebnis."""
    global _YF_SESSION_ACCEPTED
    s = _build_yf_session()
    if s is None or _YF_SESSION_ACCEPTED is False:
        return yf.Ticker(sym)
    try:
        tk = yf.Ticker(sym, session=s)
        if _YF_SESSION_ACCEPTED is None:
            # smoke-test: hole .options, wenn das ohne Session-Ablehnung klappt, cachen wir True
            try:
                _ = tk.options
                _YF_SESSION_ACCEPTED = True
            except Exception as e:
                msg = str(e)
                if "curl_cffi" in msg or "stop setting session" in msg:
                    _YF_SESSION_ACCEPTED = False
                    return yf.Ticker(sym)
                # anderer Fehler: Session ok, Fehler wird von caller-Retry aufgefangen
                _YF_SESSION_ACCEPTED = True
        return tk
    except TypeError:
        # yfinance kennt kwarg 'session' nicht
        _YF_SESSION_ACCEPTED = False
        return yf.Ticker(sym)


def _circuit_tripped() -> bool:
    return _YF_ERR_STREAK["n"] >= _YF_ERR_THRESHOLD

# ---------- V6.8.1 SHADOW-MODE: OI-Daily-Cache ----------
# Ab V6.8.1 wird täglich ATM-Call-OI und ATM-Put-OI pro Ticker in eine lokale SQLite
# geschrieben. Nach ~15 Handelstagen Aufbau liefert `compute_oi_spike` einen echten
# Spike-Score (current / 20d-median).
# **WICHTIG SHADOW-MODE:** `oi_spike_score` wird NUR in das JSON-Output geschrieben,
# aber NICHT in den Composite `options_flow_score` addiert. Das bewahrt die
# Observation-Window-Integrität (2026-04-15 bis 2026-04-29). Erst nach Go-Greenlight
# am 2026-04-29 wird entschieden, ob `oi_spike_score` in V6.9 scoringrelevant wird.
OI_CACHE_PATH = TOOLS_DIR / "oi_cache.sqlite"
OI_MEDIAN_WINDOW = 20  # Handelstage
OI_MIN_HISTORY = 5     # minimum samples bevor Spike-Score sinnvoll ist


def _oi_cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(OI_CACHE_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS oi_history (
            date         TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            expiry       TEXT,
            atm_call_oi  INTEGER,
            atm_put_oi   INTEGER,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, ticker)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oi_ticker_date ON oi_history(ticker, date)")
    conn.commit()
    return conn


def oi_cache_write(date: str, ticker: str, expiry: str | None, atm_call_oi: int, atm_put_oi: int) -> None:
    """Idempotenter Insert (ON CONFLICT REPLACE) in oi_history."""
    try:
        conn = _oi_cache_conn()
        conn.execute(
            "INSERT OR REPLACE INTO oi_history (date, ticker, expiry, atm_call_oi, atm_put_oi) "
            "VALUES (?, ?, ?, ?, ?)",
            (date, ticker, expiry, int(atm_call_oi or 0), int(atm_put_oi or 0)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[E10F] oi_cache_write error for {ticker}: {e}", file=sys.stderr)


def compute_oi_spike(ticker: str, today_date: str, today_call_oi: int, today_put_oi: int,
                     window: int = OI_MEDIAN_WINDOW) -> dict:
    """
    Vergleicht die heutigen ATM-OI-Werte mit dem Median der letzten `window` Handelstage
    aus dem lokalen Cache. Returns dict mit call_oi_spike_ratio, put_oi_spike_ratio,
    oi_spike_score (SHADOW, -2..+2), n_history.

    SHADOW-MODE: Der `oi_spike_score` wird NICHT in den Live-Composite addiert.
    Zweck ist Datensammlung über das Observation-Fenster, damit am 2026-04-29 entschieden
    werden kann, ob die echten OI-Spikes zusätzlichen Alpha liefern.
    """
    try:
        conn = _oi_cache_conn()
        cur = conn.execute(
            "SELECT date, atm_call_oi, atm_put_oi FROM oi_history "
            "WHERE ticker = ? AND date < ? ORDER BY date DESC LIMIT ?",
            (ticker, today_date, window),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": f"cache_read: {e}", "n_history": 0}

    n = len(rows)
    if n < OI_MIN_HISTORY:
        return {
            "ok": True,
            "shadow_mode": True,
            "status": "warmup",
            "n_history": n,
            "min_required": OI_MIN_HISTORY,
            "oi_spike_score": 0.0,
            "note": f"Cache-Aufbau läuft ({n}/{OI_MIN_HISTORY} Tage). Spike-Score erst danach.",
        }

    call_hist = [r[1] for r in rows if r[1] is not None]
    put_hist = [r[2] for r in rows if r[2] is not None]
    call_med = statistics.median(call_hist) if call_hist else 0
    put_med = statistics.median(put_hist) if put_hist else 0

    call_ratio = (today_call_oi / call_med) if call_med > 0 else None
    put_ratio = (today_put_oi / put_med) if put_med > 0 else None

    # Shadow-Score-Matrix: gleiche Logik wie im Docstring vorgesehen
    shadow_score = 0.0
    reason: list[str] = []
    if call_ratio is not None:
        if call_ratio > 5.0:
            shadow_score += 2
            reason.append(f"call_spike_{call_ratio:.1f}x")
        elif call_ratio > 3.0:
            shadow_score += 1
            reason.append(f"call_elev_{call_ratio:.1f}x")
    if put_ratio is not None:
        if put_ratio > 5.0:
            shadow_score -= 2
            reason.append(f"put_spike_{put_ratio:.1f}x")
        elif put_ratio > 3.0:
            shadow_score -= 1
            reason.append(f"put_elev_{put_ratio:.1f}x")
    shadow_score = max(-2.0, min(2.0, shadow_score))

    return {
        "ok": True,
        "shadow_mode": True,
        "status": "active",
        "n_history": n,
        "call_median_20d": call_med,
        "put_median_20d": put_med,
        "call_oi_spike_ratio": round(call_ratio, 2) if call_ratio is not None else None,
        "put_oi_spike_ratio": round(put_ratio, 2) if put_ratio is not None else None,
        "oi_spike_score": round(shadow_score, 2),
        "reason": reason,
    }


# ---------- Source 1: CBOE Public Put/Call-Ratio ----------

# CBOE publishes daily PCR as CSV on their public market-statistics page.
# Primary URL: https://cdn.cboe.com/api/global/us_indices/daily_market_statistics/totals.json
# Fallback: https://www.cboe.com/us/options/market_statistics/
CBOE_TOTALS_URL = "https://cdn.cboe.com/api/global/us_indices/daily_market_statistics/totals.json"


def _fetch_market_pcr_cboe() -> dict:
    """Primärquelle: CBOE public API."""
    try:
        req = urllib.request.Request(CBOE_TOTALS_URL, headers={"User-Agent": UA_STRING})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        # Structure: { "data": { "totalPCRatio": "...", "equityPCRatio": "...", "indexPCRatio": "..." } }
        d = data.get("data", data)

        def parse(key: str) -> float | None:
            v = d.get(key)
            if v is None:
                return None
            try:
                return float(str(v).replace(",", ""))
            except ValueError:
                return None

        total = parse("totalPCRatio") or parse("totalPutCallRatio")
        equity = parse("equityPCRatio") or parse("equityPutCallRatio")
        index = parse("indexPCRatio") or parse("indexPutCallRatio")
        if total is None and equity is None:
            return {"ok": False, "error": "PCR-Keys nicht im JSON gefunden", "raw_keys": list(d.keys()), "source": "cboe"}

        tag = _pcr_tag(total if total is not None else equity)
        return {"ok": True, "total": total, "equity": equity, "index": index, "tag": tag, "source": "cboe"}
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "cboe"}


def _fetch_market_pcr_yfinance_fallback() -> dict:
    """Fallback wenn CBOE 403/down: aggregierter Put/Call aus SPY + QQQ option_chain
    (nearest expiry, alle Strikes). Liefert dieselbe Struktur wie _fetch_market_pcr_cboe."""
    try:
        totals = {"spy": None, "qqq": None}
        weighted_call_oi = 0.0
        weighted_put_oi = 0.0
        equity_call_oi = 0.0  # SPY ist Index-ähnlich; QQQ Tech-Index: bei Free-Stack behandeln wir beide als Index-Proxy
        equity_put_oi = 0.0
        for sym in ("SPY", "QQQ"):
            try:
                t = _yf_ticker(sym)
                exps = t.options or []
                if not exps:
                    continue
                exp = exps[0]  # nearest
                chain = t.option_chain(exp)
                calls_oi = float(chain.calls["openInterest"].fillna(0).sum())
                puts_oi = float(chain.puts["openInterest"].fillna(0).sum())
                totals[sym.lower()] = {"call_oi": calls_oi, "put_oi": puts_oi,
                                        "ratio": (puts_oi / calls_oi) if calls_oi else None,
                                        "expiry": exp}
                weighted_call_oi += calls_oi
                weighted_put_oi += puts_oi
            except Exception:
                continue

        if weighted_call_oi == 0:
            return {"ok": False, "error": "yfinance fallback: keine OI-Daten (calls=0)", "source": "yfinance_fallback"}

        # REC-RECOVERY-1 Bug 4 (2026-05-02): zusätzlich Puts=0 abfangen
        if weighted_put_oi == 0:
            return {"ok": False, "error": "yfinance fallback: puts=0 (likely intra-day-stub)", "source": "yfinance_fallback"}

        total = weighted_put_oi / weighted_call_oi

        # REC-RECOVERY-1 Bug 4 (2026-05-02): Sanity-Range, PCR sollte 0.10..10.0 sein
        if total < 0.10 or total > 10.0:
            return {"ok": False, "error": f"yfinance fallback: PCR out-of-range (total={total:.3f})", "source": "yfinance_fallback"}
        # Index-PCR approximiert aus SPY allein
        idx_r = (totals["spy"] or {}).get("ratio") if isinstance(totals["spy"], dict) else None
        # Equity-PCR approximiert aus QQQ (nicht wirklich Equity-breit, aber bester Free-Proxy)
        eq_r = (totals["qqq"] or {}).get("ratio") if isinstance(totals["qqq"], dict) else None

        return {
            "ok": True,
            "total": round(total, 3),
            "equity": round(eq_r, 3) if eq_r else None,
            "index": round(idx_r, 3) if idx_r else None,
            "tag": _pcr_tag(total),
            "source": "yfinance_fallback",
            "components": totals,
            "note": "Fallback: aggregierter P/C aus SPY+QQQ nearest-expiry option_chain (CBOE-API 403).",
        }
    except Exception as e:
        return {"ok": False, "error": f"yfinance fallback exception: {e}", "source": "yfinance_fallback"}


def _fetch_market_pcr_lastgood_cache(today: str | None = None,
                                      tools_dir: str = "tools",
                                      max_age_workdays: int = 5) -> dict:
    """V7.9.27 MARKET-PCR-RESILIENCE-1 (KW21-Bugfix-Tag, 2026-05-18).
    V7.9.31 MARKET-PCR-RESILIENCE-3 (KW21-vorgezogen, 2026-05-21): max_age_workdays Default 2→5.

    Dritte Resolver-Stage: liest den letzten gueltigen market_pcr-Wert aus
    options_flow_YYYY-MM-DD.json-Snapshots der vergangenen `max_age_workdays`
    Werktage. Adressiert den 5-Wochen-Streak (KW16..KW20) in dem CBOE-403 +
    yfinance-OI gleichzeitig kaputt sind und V6.8 market_pcr_adjust 0% des
    Score-Beitrags geliefert hat.

    Veto-Logik:
      - max_age_workdays Cap (Default 5; erweitert von 2 in V7.9.31 nach 8-Tage-Down-Streak):
        PCR-Auto-Korrelation ist auch 5 Werktage stale akzeptabel; bei laenger anhaltenden
        Bot-Wall-Phasen war 2 zu eng (V7.9.29 zeigte cache_saves=1 statt erwartet >5).
      - PCR-Plausibilitaet (0.10..10.0) wird gegen den Cache-Wert erneut geprueft.
      - Cache-of-Cache (`source='cache_lastknown'`) wird ausgeschlossen → keine
        Endlos-Cascade ueber mehrere Tage.
      - Sa/So werden uebersprungen (keine Markt-Daten).

    Returns:
        dict mit {ok, total, equity, index, tag, source='cache_lastknown',
                  staleness_workdays, original_source, original_date} oder
        {ok: False, error: ..., source: 'cache_lastknown_unavailable'}.
    """
    if today is None:
        today_d = datetime.now(timezone.utc).date()
    else:
        try:
            today_d = datetime.strptime(today, "%Y-%m-%d").date()
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": f"invalid today: {e}",
                    "source": "cache_lastknown_unavailable"}

    if max_age_workdays < 1:
        return {"ok": False, "error": "max_age_workdays<1",
                "source": "cache_lastknown_unavailable"}

    base = Path(tools_dir)
    candidate = today_d
    workdays_back = 0
    while workdays_back < max_age_workdays:
        candidate = candidate - timedelta(days=1)
        if candidate.weekday() >= 5:  # 5=Sa, 6=So
            continue
        workdays_back += 1
        path = base / f"options_flow_{candidate.isoformat()}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        mp = snapshot.get("market_pcr") or {}
        if not mp.get("ok"):
            continue
        total = mp.get("total")
        if total is None:
            continue
        if not isinstance(total, (int, float)) or total < 0.10 or total > 10.0:
            continue
        orig_src = mp.get("source", "unknown")
        if orig_src == "cache_lastknown":
            # Cache-of-Cache vermeiden (Drift-Risiko)
            continue
        return {
            "ok": True,
            "total": float(total),
            "equity": mp.get("equity"),
            "index": mp.get("index"),
            "tag": mp.get("tag") or _pcr_tag(total),
            "source": "cache_lastknown",
            "staleness_workdays": workdays_back,
            "original_source": orig_src,
            "original_date": candidate.isoformat(),
            "note": (f"V7.9.27 Cache-Last-Good ({workdays_back}d stale, "
                     f"orig={orig_src}, date={candidate.isoformat()})."),
        }

    return {
        "ok": False,
        "error": (f"Kein gueltiger market_pcr-Snapshot in den letzten "
                  f"{max_age_workdays} Werktagen vor {today_d.isoformat()}."),
        "source": "cache_lastknown_unavailable",
    }


def _fetch_market_pcr_spx_index(timeout: int = 15) -> dict:
    """V7.9.31 MARKET-PCR-RESILIENCE-3 (2026-05-21).

    Stage 4 Resolver: SPX/VIX Index-Options via yfinance.

    Hypothese aus V7.9.28 PCR-Source-Probe: SPY/QQQ-ETF-Options haben einen
    Bot-Wall (Yahoo retournt OI=0 fuer ETF-Optionen bei verdaechtigem Pattern).
    Index-Options auf ^SPX (S&P 500 Index direkt) und ^VIX (Volatility Index)
    sind professionellere Instrumente, oft weniger Retail-Bot-Wall.

    Cascade:
      a) ^SPX nearest expiry (Index-PCR: best signal)
      b) ^VIX nearest expiry (Vol-PCR: Backup)

    Returns dieselbe Struktur wie _fetch_market_pcr_cboe.
    """
    try:
        for sym in ("^SPX", "^VIX"):
            try:
                t = _yf_ticker(sym)
                exps = t.options or []
                if not exps:
                    continue
                exp = exps[0]
                chain = t.option_chain(exp)
                calls_oi = float(chain.calls["openInterest"].fillna(0).sum())
                puts_oi = float(chain.puts["openInterest"].fillna(0).sum())
                if calls_oi == 0 or puts_oi == 0:
                    continue
                total = puts_oi / calls_oi
                if total < 0.10 or total > 10.0:
                    continue
                return {
                    "ok": True,
                    "total": round(total, 3),
                    "equity": None,
                    "index": round(total, 3),  # SPX/VIX sind beide Index-Proxies
                    "tag": _pcr_tag(total),
                    "source": f"yfinance_index_{sym.lstrip('^').lower()}",
                    "components": {
                        "symbol": sym,
                        "expiry": exp,
                        "call_oi": calls_oi,
                        "put_oi": puts_oi,
                    },
                    "note": ("V7.9.31 Stage-4 Index-Options-Resolver. "
                             "Umgeht Retail-ETF-Bot-Wall durch direkte Index-Quotes."),
                }
            except Exception:
                continue
        return {"ok": False, "error": "yfinance_index_spx_vix: no usable OI for ^SPX or ^VIX",
                "source": "yfinance_index_unavailable"}
    except Exception as e:
        return {"ok": False, "error": f"yfinance_index exception: {e}",
                "source": "yfinance_index_unavailable"}


# CBOE Markets Daily-Statistics HTML-Page (Fallback wenn CDN-API tot)
CBOE_HTML_URL = "https://markets.cboe.com/us/options/market_statistics/daily/"


def _parse_cboe_html_pcr(html: str) -> dict:
    """Extrahiert PCR-Werte aus markets.cboe.com HTML-Page.

    CBOE rendert die Daten als embedded JSON innerhalb der React-Hydration-
    Payload. Beispiel-Struktur (gefunden in __NEXT_DATA__ oder als preloaded
    JSON):
      {"name":"TOTAL PUT/CALL RATIO","value":"0.75"}
      {"name":"INDEX PUT/CALL RATIO","value":"0.77"}
      {"name":"EQUITY PUT/CALL RATIO","value":"0.49"}
      ...
      "selectedDate":"2026-05-20"

    Returns: dict mit den Feldern total/equity/index/etp/vix/spx + selected_date
    oder {"_parse_error": "..."} bei Misserfolg.
    """
    import re
    # CBOE rendert das JSON als String inline und escaped die Quotes: \"name\":\"...\"
    # Wir akzeptieren beide Varianten: escaped (\") und plain (").
    pattern = re.compile(
        r'(?:\\?")name(?:\\?")\s*:\s*(?:\\?")([^"\\]+?\s*PUT/CALL\s*RATIO)(?:\\?")\s*,'
        r'\s*(?:\\?")value(?:\\?")\s*:\s*(?:\\?")([0-9]+\.?[0-9]*)(?:\\?")',
        re.IGNORECASE,
    )
    raw: dict[str, float] = {}
    for m in pattern.finditer(html):
        label = m.group(1).strip().upper()
        try:
            v = float(m.group(2))
        except ValueError:
            continue
        if not (0.10 <= v <= 10.0):
            continue
        raw[label] = v

    if not raw:
        return {"_parse_error": "no PCR pattern matched"}

    def find(key_substr: str) -> float | None:
        for k, v in raw.items():
            if key_substr in k:
                return v
        return None

    # selectedDate findet sich im HTML als "selectedDate":"YYYY-MM-DD" oder \"selectedDate\":\"...\"
    sel_m = re.search(
        r'(?:\\?")selectedDate(?:\\?")\s*:\s*(?:\\?")([0-9]{4}-[0-9]{2}-[0-9]{2})(?:\\?")',
        html,
    )
    selected_date = sel_m.group(1) if sel_m else None

    return {
        "total": find("TOTAL PUT/CALL RATIO"),
        "equity": find("EQUITY PUT/CALL RATIO"),
        "index": find("INDEX PUT/CALL RATIO"),
        "etp": find("EXCHANGE TRADED PRODUCTS"),
        "vix": find("VOLATILITY INDEX"),
        "spx": find("SPX"),  # matched 'SPX + SPXW PUT/CALL RATIO'
        "selected_date": selected_date,
        "n_ratios_parsed": len(raw),
    }


def _fetch_market_pcr_cboe_html(timeout: int = 15) -> dict:
    """V7.9.31 MARKET-PCR-RESILIENCE-3 (2026-05-21).

    Stage 4/5 Resolver: CBOE HTML-Statistics-Page als Fallback.

    Wenn die JSON-API auf cdn.cboe.com tot ist (Decommission HTTP 403),
    publiziert CBOE die Daten weiterhin auf der HTML-Statistics-Seite
    markets.cboe.com: als embedded JSON in der React-App.

    Verify (live 2026-05-21): HTML enthaelt TOTAL=0.75 / EQUITY=0.49 /
    INDEX=0.77 / ETP=1.18 / VIX=0.19 / SPX=1.00 fuer selectedDate 2026-05-20.

    Returns dieselbe Struktur wie _fetch_market_pcr_cboe.
    """
    try:
        req = urllib.request.Request(CBOE_HTML_URL, headers={
            "User-Agent": UA_POOL[0] if UA_POOL else UA_STRING,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="replace")
        if len(html) < 500:
            return {"ok": False, "error": f"HTML too short ({len(html)} bytes)",
                    "source": "cboe_html_unavailable"}

        parsed = _parse_cboe_html_pcr(html)
        if "_parse_error" in parsed:
            return {"ok": False, "error": f"CBOE HTML: {parsed['_parse_error']}",
                    "source": "cboe_html_unavailable",
                    "html_size": len(html)}

        total = parsed.get("total")
        equity = parsed.get("equity")
        index = parsed.get("index")
        primary = total if total is not None else (equity if equity is not None else index)
        if primary is None:
            return {"ok": False, "error": "CBOE HTML parsed aber keine valide total/equity/index",
                    "source": "cboe_html_unavailable",
                    "parsed_keys": list(parsed.keys())}

        return {
            "ok": True,
            "total": total,
            "equity": equity,
            "index": index,
            "tag": _pcr_tag(primary),
            "source": "cboe_html_scrape",
            "components": {
                "etp": parsed.get("etp"),
                "vix": parsed.get("vix"),
                "spx": parsed.get("spx"),
                "selected_date": parsed.get("selected_date"),
                "n_ratios_parsed": parsed.get("n_ratios_parsed"),
            },
            "note": ("V7.9.31 Stage-4 CBOE HTML-Scrape. JSON-CDN tot, "
                     "Daten aus markets.cboe.com embedded React-JSON."),
        }
    except Exception as e:
        return {"ok": False, "error": f"cboe_html exception: {str(e)[:120]}",
                "source": "cboe_html_unavailable"}


def fetch_market_pcr(today: str | None = None,
                     tools_dir: str = "tools",
                     cache_max_age_workdays: int = 5) -> dict:
    """Primär CBOE-JSON, dann yfinance ETF, dann SPX-Index, dann CBOE-HTML, dann Cache.

    History:
      V7.9.27 (2026-05-18): Stage 3 Cache-Last-Good hinzugefuegt (max 2 WT).
      V7.9.31 (2026-05-21): Stage 4 + Stage 5 hinzugefuegt, Cache-Window 2→5 WT.

    Reihenfolge (5 Stages):
      1) CBOE JSON-API (cdn.cboe.com: Primaer, oft 403 seit Mai 2026)
      2) yfinance SPY+QQQ ETF-Options (V7.9.27: oft Bot-Wall OI=0)
      3) yfinance ^SPX / ^VIX Index-Options (V7.9.31: umgeht ETF-Bot-Wall)
      4) CBOE HTML-Scrape (V7.9.31: markets.cboe.com Daily-Statistics)
      5) Cache-Last-Good aus options_flow_*.json (V7.9.27, max 5 WT stale)

    Stage 2 und 3 sind beide yfinance-basiert aber nutzen unterschiedliche
    Symbole: Stage 2 ETF (SPY/QQQ), Stage 3 Index direkt (^SPX/^VIX). Die
    Bot-Wall-Hypothese aus V7.9.28 PCR-Source-Probe besagt, dass Yahoo bei
    ETF-Optionen aggressiver throttled als bei Index-Optionen.
    """
    primary = _fetch_market_pcr_cboe()
    if primary.get("ok"):
        return primary
    # Stage 2: yfinance ETF
    fb = _fetch_market_pcr_yfinance_fallback()
    if fb.get("ok"):
        fb["cboe_error"] = primary.get("error")
        return fb
    # V7.9.31 Stage 3: yfinance Index ^SPX/^VIX
    spx = _fetch_market_pcr_spx_index()
    if spx.get("ok"):
        spx["cboe_error"] = primary.get("error")
        spx["yfinance_etf_error"] = fb.get("error")
        return spx
    # V7.9.31 Stage 4: CBOE HTML-Scrape
    html = _fetch_market_pcr_cboe_html()
    if html.get("ok"):
        html["cboe_error"] = primary.get("error")
        html["yfinance_etf_error"] = fb.get("error")
        html["yfinance_index_error"] = spx.get("error")
        return html
    # Stage 5: Cache-Last-Good
    cache = _fetch_market_pcr_lastgood_cache(today=today, tools_dir=tools_dir,
                                              max_age_workdays=cache_max_age_workdays)
    if cache.get("ok"):
        cache["cboe_error"] = primary.get("error")
        cache["yfinance_etf_error"] = fb.get("error")
        cache["yfinance_index_error"] = spx.get("error")
        cache["cboe_html_error"] = html.get("error")
        return cache
    # Alle Stages down → ehrliches Fehlerobjekt
    return {
        "ok": False,
        "error": (f"CBOE-JSON: {primary.get('error')} | yf-ETF: {fb.get('error')} "
                  f"| yf-Index: {spx.get('error')} | CBOE-HTML: {html.get('error')} "
                  f"| cache: {cache.get('error')}"),
        "source": "all_sources_down",
    }


def _pcr_tag(pcr: float | None) -> str:
    if pcr is None:
        return "unknown"
    # REC-RECOVERY-1 Bug 4 (2026-05-02): impossibly low PCR (≤0.10) ist kein "euphoric_market", sondern Daten-Korruption
    if pcr < 0.10 or pcr > 10.0:
        return "unknown"
    if pcr >= 1.20:
        return "defensive_market"
    if pcr <= 0.70:
        return "euphoric_market"
    return "neutral"


# ---------- Source 2: yfinance option_chain per Ticker ----------

# V7.9.19 OI-PAYLOAD-HEAL-1 (KW19-Bugfix-Tag, 2026-05-04):
# Adressiert OF-FALLBACK-CHAIN-EXHAUSTED-1 / silent_decommission aus V7.9.18-Diagnose.
# Wenn yfinance.option_chain `ok` zurueckgibt, aber `openInterest` komplett 0/NaN ist
# (ATM-Call-OI == 0 AND ATM-Put-OI == 0), probieren wir bis zu MAX_EXPIRY_FALLBACKS_OI
# zusaetzliche Expiries durch (jeweils eine Ebene weiter raus). Erst wenn ALLE
# Versuche OI=0 liefern, markieren wir das Resultat mit `oi_payload_ok=False`.
# Default-Verhalten bei OI>0 in der ersten Expiry bleibt rueckwaerts-kompatibel.
MAX_EXPIRY_FALLBACKS_OI = 3


def _get_spot_price(tk) -> float:
    """V7.9.19: Spot-Preis-Fallback-Kaskade extrahiert (fast_info -> info -> 5d history)."""
    try:
        fast = tk.fast_info
        for key in ("last_price", "regular_market_previous_close",
                    "previous_close", "regular_market_price"):
            val = getattr(fast, key, None)
            if val is not None:
                try:
                    spot = float(val)
                    if spot > 0:
                        return spot
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    try:
        info = tk.info or {}
        for key in ("regularMarketPrice", "currentPrice", "previousClose"):
            val = info.get(key)
            if val is not None:
                try:
                    spot = float(val)
                    if spot > 0:
                        return spot
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    try:
        hist = tk.history(period="5d", auto_adjust=False)
        if hist is not None and len(hist) > 0:
            spot = float(hist["Close"].iloc[-1])
            if spot > 0:
                return spot
    except Exception:
        pass
    return 0.0


def _measure_atm_block(chain, spot: float) -> dict:
    """V7.9.19: ATM-OI/IV-Messung +/-10% um Spot. Robust gegen fehlende openInterest-Spalte."""
    lo, hi = spot * 0.90, spot * 1.10
    try:
        calls = chain.calls[(chain.calls["strike"] >= lo) & (chain.calls["strike"] <= hi)]
        puts  = chain.puts [(chain.puts ["strike"] >= lo) & (chain.puts ["strike"] <= hi)]
    except Exception:
        return {
            "atm_call_oi": 0, "atm_put_oi": 0,
            "atm_call_iv": None, "atm_put_iv": None,
            "iv_skew": None, "iv_skew_tag": "unknown",
        }

    atm_call_oi = int(calls["openInterest"].fillna(0).sum()) if "openInterest" in calls.columns else 0
    atm_put_oi  = int(puts ["openInterest"].fillna(0).sum()) if "openInterest" in puts.columns  else 0

    def _nearest(df):
        if df.empty:
            return None
        idx = (df["strike"] - spot).abs().idxmin()
        return df.loc[idx]

    atm_call_row = _nearest(calls)
    atm_put_row  = _nearest(puts)

    atm_call_iv = float(atm_call_row["impliedVolatility"]) if atm_call_row is not None and pd.notna(atm_call_row.get("impliedVolatility")) else None
    atm_put_iv  = float(atm_put_row ["impliedVolatility"]) if atm_put_row  is not None and pd.notna(atm_put_row .get("impliedVolatility")) else None

    iv_skew = None
    iv_skew_tag = "unknown"
    if atm_call_iv is not None and atm_put_iv is not None:
        iv_skew = atm_call_iv - atm_put_iv
        if iv_skew > 0.02:
            iv_skew_tag = "call_bias"
        elif iv_skew < -0.02:
            iv_skew_tag = "put_bias"
        else:
            iv_skew_tag = "neutral"

    return {
        "atm_call_oi": atm_call_oi,
        "atm_put_oi": atm_put_oi,
        "atm_call_iv": atm_call_iv,
        "atm_put_iv": atm_put_iv,
        "iv_skew": iv_skew,
        "iv_skew_tag": iv_skew_tag,
    }


def _pick_expiry_candidates(expiries: list, today, max_n: int = MAX_EXPIRY_FALLBACKS_OI) -> list:
    """V7.9.19: bis zu max_n Expiries als geordnete Kandidatenliste fuer den OI-Heal-Loop.
    Praeferenz-Reihenfolge erhalten: 1) >=today+7, 2) >=today+3, 3) >=today, 4) letzte.
    Doppelte werden entfernt; max_n cap.
    """
    if not expiries:
        return []
    seen = set()
    ordered: list = []
    def _add(e):
        if e and e not in seen and len(ordered) < max_n:
            seen.add(e)
            ordered.append(e)

    parsed = []
    for e in expiries:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
            parsed.append((e, d))
        except ValueError:
            continue
    # Ebene 1: >= today+7
    for e, d in parsed:
        if (d - today).days >= 7:
            _add(e)
    # Ebene 2: >= today+3
    for e, d in parsed:
        if (d - today).days >= 3:
            _add(e)
    # Ebene 3: >= today
    for e, d in parsed:
        if d >= today:
            _add(e)
    # Ebene 4: letzte bekannte (Daten-Staleness)
    if parsed:
        _add(parsed[-1][0])
    elif expiries:
        _add(expiries[-1])
    return ordered[:max_n]


def fetch_option_oi_spike(ticker: str, median_window_days: int = 20) -> dict:
    """
    Zieht die aktuellste Expiry aus yfinance, summiert Call-OI bzw. Put-OI nahe ATM
    (±10 % um Spot-Preis), vergleicht mit 20d-Median der realisierten Call/Put-Volumina.

    Da yfinance kein historisches OI liefert, proxy'en wir "20d-Median" über
    average daily volume (ADV) der 20 letzten Tage für das Underlying × typischer
    Call/Put-Options-Ratio (Heuristik). Robusterer Ansatz wäre ein lokaler Daily-Cache,
    den wir in V6.8.1 einführen.

    V6.8.0-Kompromiss: Wir vergleichen aktuelle ATM-Call-OI vs aktuelle ATM-Put-OI
    und IV-Skew. Das ist kein OI-Spike im strengen Sinn, aber ein valides Flow-Proxy.

    Returns:
      {
        "ok": True,
        "underlying_price": float,
        "expiry": "YYYY-MM-DD",
        "atm_call_oi": int,
        "atm_put_oi": int,
        "call_put_oi_ratio": float,    # >1.5 bullish; <0.67 bearish
        "atm_call_iv": float,
        "atm_put_iv": float,
        "iv_skew": float,              # atm_call_iv - atm_put_iv (>0 call bias)
        "iv_skew_tag": "call_bias" / "put_bias" / "neutral",
      }
      or {"ok": False, "error": "..."}
    """
    # OPTIONS-FLOW-CONSISTENCY-1 (2026-04-19): Circuit-Breaker, Retry, Fallbacks
    if _circuit_tripped():
        return {"ok": False, "error": f"circuit_open_after_{_YF_ERR_THRESHOLD}_streak"}

    try:
        tk = _yf_ticker(ticker)

        # --- 1) Expiries mit Retry + UA-Rotate ---
        expiries = None
        last_err = None
        for attempt in range(3):
            try:
                expiries = tk.options
                if expiries:
                    break
            except Exception as e:
                last_err = e
            _rotate_ua()
            time.sleep(0.4 * (attempt + 1))
        if not expiries:
            _YF_ERR_STREAK["n"] += 1
            return {"ok": False, "error": f"no options listed ({last_err})" if last_err else "no options listed"}

        # --- 2) Expiry-Kandidaten mit Fallback-Kaskade (V7.9.19: bis zu MAX_EXPIRY_FALLBACKS_OI) ---
        today = datetime.now(timezone.utc).date()
        candidates = _pick_expiry_candidates(expiries, today, MAX_EXPIRY_FALLBACKS_OI)
        if not candidates:
            _YF_ERR_STREAK["n"] += 1
            return {"ok": False, "error": "no usable expiry"}

        # --- 3) Spot-Preis (einmal pro Ticker; Fallback-Kaskade extrahiert in _get_spot_price) ---
        spot = _get_spot_price(tk)
        if spot <= 0:
            _YF_ERR_STREAK["n"] += 1
            return {"ok": False, "error": "no spot price (fast_info+info+history all failed)"}

        # --- 4) OI-Heal-Loop (V7.9.19): bis zu MAX_EXPIRY_FALLBACKS_OI Expiries probieren,
        #     wenn ATM-OI komplett 0 ist (silent_decommission der yfinance-OI-Payload).
        tried_expiries: list = []
        last_attempt: dict = {}
        chosen = None
        chain = None
        for cand in candidates:
            tried_expiries.append(cand)
            chain = None
            for attempt in range(3):
                try:
                    chain = tk.option_chain(cand)
                    break
                except Exception as e:
                    last_err = e
                _rotate_ua()
                time.sleep(0.4 * (attempt + 1))
            if chain is None:
                last_attempt = {"ok": False, "error": f"option_chain_fetch_failed: {last_err}"}
                continue
            atm = _measure_atm_block(chain, spot)
            last_attempt = atm
            chosen = cand
            if (atm["atm_call_oi"] or 0) > 0 or (atm["atm_put_oi"] or 0) > 0:
                # OI-Payload ok -> abbrechen, dieser Kandidat wird verwendet
                break
            # OI ist 0 -> naechste Expiry probieren
        if chosen is None:
            _YF_ERR_STREAK["n"] += 1
            return {
                "ok": False,
                "error": last_attempt.get("error", "option_chain_fetch_failed"),
                "tried_expiries": tried_expiries,
                "oi_payload_ok": False,
                "oi_heal_attempts": len(tried_expiries),
            }

        atm_call_oi = last_attempt.get("atm_call_oi") or 0
        atm_put_oi  = last_attempt.get("atm_put_oi")  or 0
        atm_call_iv = last_attempt.get("atm_call_iv")
        atm_put_iv  = last_attempt.get("atm_put_iv")
        iv_skew     = last_attempt.get("iv_skew")
        iv_skew_tag = last_attempt.get("iv_skew_tag", "unknown")
        oi_payload_ok = (atm_call_oi > 0) or (atm_put_oi > 0)

        call_put_oi_ratio = atm_call_oi / atm_put_oi if atm_put_oi > 0 else float("inf")

        # OPTIONS-FLOW-CONSISTENCY-1: Erfolg -> Error-Streak zuruecksetzen
        _YF_ERR_STREAK["n"] = 0

        return {
            "ok": True,
            "underlying_price": round(spot, 2),
            "expiry": chosen,
            "atm_call_oi": atm_call_oi,
            "atm_put_oi":  atm_put_oi,
            "call_put_oi_ratio": round(call_put_oi_ratio, 3) if call_put_oi_ratio != float("inf") else None,
            "atm_call_iv": round(atm_call_iv, 4) if atm_call_iv is not None else None,
            "atm_put_iv":  round(atm_put_iv, 4) if atm_put_iv  is not None else None,
            "iv_skew":     round(iv_skew, 4) if iv_skew is not None else None,
            "iv_skew_tag": iv_skew_tag,
            # V7.9.19 OI-PAYLOAD-HEAL-1 Diagnostics:
            "oi_payload_ok": oi_payload_ok,
            "tried_expiries": tried_expiries,
            "oi_heal_attempts": len(tried_expiries),
        }
    except Exception as e:
        _YF_ERR_STREAK["n"] += 1
        return {"ok": False, "error": str(e)}


# ---------- Score Aggregation ----------

def compute_options_flow_score(flow: dict | None) -> tuple[float, str, dict]:
    """
    Composite score pro Ticker, clipped [-2, +2].
    Returns (score, flow_tag, breakdown).
    """
    if not flow or not flow.get("ok"):
        return 0.0, "no_data", {}

    score = 0.0
    breakdown: dict = {}

    cp_ratio = flow.get("call_put_oi_ratio")
    iv_skew_tag = flow.get("iv_skew_tag", "unknown")

    # Call/Put-OI-Ratio (proxy für Flow-Direction)
    if cp_ratio is not None:
        if cp_ratio >= 2.5:
            score += 2
            breakdown["call_oi_extreme"] = "+2"
        elif cp_ratio >= 1.5:
            score += 1
            breakdown["call_oi_elevated"] = "+1"
        elif cp_ratio <= 0.40:
            score -= 2
            breakdown["put_oi_extreme"] = "-2"
        elif cp_ratio <= 0.67:
            score -= 1
            breakdown["put_oi_elevated"] = "-1"

    # IV-Skew als Konfirmation
    if iv_skew_tag == "call_bias":
        score += 1
        breakdown["iv_call_bias"] = "+1"
    elif iv_skew_tag == "put_bias":
        score -= 1
        breakdown["iv_put_bias"] = "-1"

    score = max(-2.0, min(2.0, score))

    # Flow-Tag
    if score >= 1.5:
        flow_tag = "bullish_flow"
    elif score >= 0.5:
        flow_tag = "mild_bullish"
    elif score <= -1.5:
        flow_tag = "bearish_flow"
    elif score <= -0.5:
        flow_tag = "mild_bearish"
    else:
        flow_tag = "neutral"

    return round(score, 2), flow_tag, breakdown


def market_pcr_adjust(market_pcr: dict) -> tuple[float, str]:
    """
    Globaler Markt-Tilt basierend auf CBOE Total-PCR.
    Returns (adjust, reason_tag).
    """
    if not market_pcr.get("ok"):
        return 0.0, "unknown"
    tag = market_pcr.get("tag", "unknown")
    if tag == "defensive_market":
        return -0.10, "defensive_market_pcr>=1.20"
    if tag == "euphoric_market":
        return -0.10, "euphoric_market_pcr<=0.70_contrarian_malus"
    return 0.0, "neutral"


# ---------- Main ----------

def load_top_candidates_from_scan(scan_date: str, top_n: int = 40) -> list[str]:
    """Liest scan_YYYY-MM-DD.json und liefert Top-N Ticker nach pre_score."""
    p = TOOLS_DIR / f"scan_{scan_date}.json"
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        tm = data.get("ticker_map", {})
        # Sort by pre_score desc, skip Tier-X
        rows = [v for v in tm.values() if v.get("tier") != "X" and v.get("passes_threshold")]
        rows.sort(key=lambda r: r.get("pre_score", 0), reverse=True)
        return [r["ticker"] for r in rows[:top_n]]
    except Exception as e:
        print(f"[E10F] could not parse scan.json: {e}", file=sys.stderr)
        return []


def load_tier_a_fallback(top_n: int = 40) -> list[str]:
    """Fallback-Liste: Tier-A US-Ticker aus universe.csv (erste top_n)."""
    universe_csv = TOOLS_DIR / "universe.csv"
    if not universe_csv.exists():
        return []
    out: list[str] = []
    with open(universe_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("tier") != "A":
                continue
            # yfinance option_chain funktioniert primär für US-Ticker
            t = row["ticker"]
            if "." in t or "=" in t:  # skip .DE, =F etc.
                continue
            out.append(t)
            if len(out) >= top_n:
                break
    return out


def _run_selftest(delay: float = 0.6) -> int:
    """OPTIONS-FLOW-CONSISTENCY-1 Validation (2026-04-19).

    Laeuft gegen 10 grosse US-Ticker und meldet Hit-Rate. Erwartung nach Fix: >=70%.
    Schreibt NICHTS in options_flow_*.json und NICHTS in den OI-Cache."""
    probes = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "SPY", "QQQ", "AMD"]
    print(f"[E10F-selftest] probing {len(probes)} tickers, delay={delay}s ...", file=sys.stderr)
    ok = 0
    details = []
    for t in probes:
        r = fetch_option_oi_spike(t)
        is_ok = bool(r.get("ok"))
        if is_ok:
            ok += 1
            details.append(f"  OK  {t:<6} spot={r.get('underlying_price')} exp={r.get('expiry')} "
                           f"call_oi={r.get('atm_call_oi')} put_oi={r.get('atm_put_oi')}")
        else:
            details.append(f"  ERR {t:<6} {r.get('error')}")
        time.sleep(delay)
    hr = ok / len(probes) * 100
    print("\n".join(details), file=sys.stderr)
    print(f"\n[E10F-selftest] HIT-RATE: {ok}/{len(probes)} = {hr:.1f}%", file=sys.stderr)
    print(f"[E10F-selftest] session_kind={_YF_SESSION_KIND} "
          f"session_accepted={_YF_SESSION_ACCEPTED} "
          f"curl_cffi={_HAVE_CURL_CFFI} requests={_HAVE_REQUESTS} "
          f"final_err_streak={_YF_ERR_STREAK['n']}", file=sys.stderr)
    # Exit-Code: 0 wenn >=70% (GO-Schwelle), 1 wenn 50-70% (WARN), 2 wenn <50% (FAIL)
    if hr >= 70.0:
        return 0
    if hr >= 50.0:
        return 1
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--tickers", default=None, help="Comma-separated list (default: top-N from scan.json)")
    ap.add_argument("--top", type=int, default=40, help="Top-N candidates from scan.json (default 40)")
    ap.add_argument("--delay", type=float, default=0.8, help="Inter-request delay in sec")
    ap.add_argument("--skip-cboe", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="10-Ticker Hit-Rate-Probe, kein JSON-Write (OPTIONS-FLOW-CONSISTENCY-1)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_run_selftest(delay=args.delay))

    scan_date = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")

    sources_health = {"cboe_pcr": "skipped", "yfinance_option_chain": "skipped"}

    # 1. CBOE Market-PCR
    if args.skip_cboe:
        market_pcr = {"ok": False, "error": "skipped"}
    else:
        print("[E10F] Fetching CBOE Public Put/Call-Ratio ...", file=sys.stderr)
        market_pcr = fetch_market_pcr()
        sources_health["cboe_pcr"] = "ok" if market_pcr.get("ok") else f"error: {market_pcr.get('error','')}"

    # 2. Ticker-Liste
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_top_candidates_from_scan(scan_date, args.top)

    if not tickers:
        print("[E10F] keine Tickers (weder --tickers noch scan.json). Abbruch.", file=sys.stderr)
        out = {
            "date": scan_date, "version": "V6.8-E10F",
            "market_pcr": market_pcr, "market_pcr_adjust": market_pcr_adjust(market_pcr),
            "sources": sources_health, "ticker_map": {},
        }
        _save_output(scan_date, out)
        return

    print(f"[E10F] option_chain fetch für {len(tickers)} Kandidaten (delay {args.delay}s) ...", file=sys.stderr)

    ticker_map = {}
    errors = 0
    for i, t in enumerate(tickers):
        flow = fetch_option_oi_spike(t)
        if not flow.get("ok"):
            errors += 1
            time.sleep(args.delay * 0.5)
            continue
        score, tag, breakdown = compute_options_flow_score(flow)

        # V6.8.1 SHADOW: in OI-Cache schreiben
        oi_cache_write(
            date=scan_date,
            ticker=t,
            expiry=flow.get("expiry"),
            atm_call_oi=flow.get("atm_call_oi") or 0,
            atm_put_oi=flow.get("atm_put_oi") or 0,
        )
        # V6.8.1 SHADOW: Spike-Score berechnen, NICHT in `score` mergen
        shadow_spike = compute_oi_spike(
            ticker=t,
            today_date=scan_date,
            today_call_oi=flow.get("atm_call_oi") or 0,
            today_put_oi=flow.get("atm_put_oi") or 0,
        )

        ticker_map[t] = {
            "options_flow_score": score,            # LIVE: wird vom Scanner addiert
            "flow_tag": tag,
            "breakdown": breakdown,
            "shadow_oi_spike": shadow_spike,         # SHADOW, NUR Logging, kein Live-Adjust
            "raw": flow,
        }
        time.sleep(args.delay)
        if i % 10 == 0 and i > 0:
            print(f"  [E10F] {i}/{len(tickers)} ({errors} errors)", file=sys.stderr)

    sources_health["yfinance_option_chain"] = f"ok ({len(ticker_map)}/{len(tickers)}, {errors} errors)"

    # V6.8.1 SHADOW-Mode-Counts
    n_shadow_active = sum(
        1 for v in ticker_map.values()
        if v.get("shadow_oi_spike", {}).get("status") == "active"
    )
    n_shadow_warmup = sum(
        1 for v in ticker_map.values()
        if v.get("shadow_oi_spike", {}).get("status") == "warmup"
    )

    hit_rate = len(ticker_map) / len(tickers) if tickers else 0.0
    out = {
        "date": scan_date,
        "version": "V6.8.1-E10F+SessionFix",
        "n_candidates": len(tickers),
        "n_ok": len(ticker_map),
        "n_errors": errors,
        "hit_rate_pct": round(hit_rate * 100, 2),
        "n_flagged": sum(1 for v in ticker_map.values() if abs(v["options_flow_score"]) >= 1.0),
        "market_pcr": market_pcr,
        "market_pcr_adjust": market_pcr_adjust(market_pcr),
        "shadow_oi_cache": {
            "enabled": True,
            "n_active": n_shadow_active,
            "n_warmup": n_shadow_warmup,
            "min_history_required": OI_MIN_HISTORY,
            "median_window_days": OI_MEDIAN_WINDOW,
            "note": "oi_spike_score wird LIVE NICHT addiert. Re-Evaluation 2026-04-29.",
        },
        "session_fix": {
            "curl_cffi_available": _HAVE_CURL_CFFI,
            "requests_available": _HAVE_REQUESTS,
            "session_kind": _YF_SESSION_KIND,
            "session_accepted_by_yf": _YF_SESSION_ACCEPTED,
            "ua_pool_size": len(UA_POOL),
            "circuit_breaker_threshold": _YF_ERR_THRESHOLD,
            "circuit_tripped": _circuit_tripped(),
            "final_err_streak": _YF_ERR_STREAK["n"],
        },
        "sources": sources_health,
        "ticker_map": ticker_map,
    }
    _save_output(scan_date, out)

    # Summary
    print(f"\n=== OPTIONS-FLOW SUMMARY ({scan_date}) ===")
    if market_pcr.get("ok"):
        print(f"Market PCR: total={market_pcr.get('total')}, equity={market_pcr.get('equity')}, "
              f"index={market_pcr.get('index')}, tag={market_pcr.get('tag')}")
    top = sorted(ticker_map.items(), key=lambda kv: abs(kv[1]["options_flow_score"]), reverse=True)[:15]
    print(f"\nTop 15 Flow-Flags:")
    print(f"{'Ticker':<8} {'Score':<7} {'Tag':<15} {'Breakdown'}")
    for t, d in top:
        print(f"{t:<8} {d['options_flow_score']:+.2f}  {d['flow_tag']:<15} {d['breakdown']}")


def _save_output(scan_date: str, out: dict):
    p = TOOLS_DIR / f"options_flow_{scan_date}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"[E10F] saved: {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
