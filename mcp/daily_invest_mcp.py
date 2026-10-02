#!/usr/bin/env python3
"""
R4: Daily-Invest MCP-Server (KW22-Rollout, vorgezogen 2026-05-19).

Lokaler FastMCP-Server, der einem MCP-Client (z. B. Claude Desktop/Code) direkten
Zugriff auf das Trading-System gibt, ohne dass der User die CLI bemühen muss.

PRIVACY: der Server liest dein lokales trade_journal.sqlite und kann (nur mit
confirm=True) Trades ins Journal schreiben. Er platziert KEINE Broker-Orders.

Tools:
  - get_price(ticker)           : last close + change% (yfinance)
  - get_history(ticker, days)   : OHLCV-DataFrame (yfinance)
  - get_indicators(ticker)      : RSI(2), SMA200, ATR(14), distance_to_sma
  - get_options_chain(ticker)   : nearest expiry chain (yfinance)
  - get_insider_feed(ticker)    : insider rows (local tools/insider_feed.py output)
  - get_journal_status()        : aktuelle Positionen aus trade_journal.sqlite
  - get_journal_summary()       : YTD live+external+disc (Cohort-Filter)
  - get_scan_candidates(date?)  : letzter daily_scanner Output (Kandidaten, keine Empfehlung)
  - get_recommendations(date?)  : veralteter Alias von get_scan_candidates (eine Version noch)
  - get_news_sentiment(ticker)  : letzter GDELT+FinDPO Score
  - run_quick_trade(action,...)  : wrappt tools/quick_trade.py (READ-ONLY default)
  - get_system_state()          : Sprint-State, letzter Win, offene Positions
  - search_universe(query)      : Tier/Sector-Filter über universe.csv

Setup (einmalig, Python 3.10+ in einer venv):
    python3 -m pip install "mcp>=1.2,<2" yfinance

Start (manuell oder via Claude Desktop config):
    python3 mcp/daily_invest_mcp.py              # startet den stdio-Server (wartet auf einen Client)
    python3 mcp/daily_invest_mcp.py --selftest   # prueft Import + Tool-Registrierung, beendet sich
    python3 mcp/daily_invest_mcp.py --help       # Kurzhilfe, beendet sich

Claude Desktop MCP-Config (~/Library/Application Support/Claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "daily-invest": {
          "command": "/path/to/daily-invest-oss/.mcp-venv/bin/python",
          "args": ["/path/to/daily-invest-oss/mcp/daily_invest_mcp.py"]
        }
      }
    }

Voraussetzungen:
  - Python 3.10+
  - mcp 1.x (mcp>=1.2,<2; mcp 2.x hat mcp.server.fastmcp entfernt)
  - yfinance (für Price/History/Options)
  - Repo-tools/ im sys.path

Alle Ausgaben sind Forschungsinformation, keine Anlageberatung und keine Anlageempfehlung.
Exit-Codes: 0 ok · 1 mcp nicht importierbar oder Selftest rot.
"""
from __future__ import annotations

import csv
import functools
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(REPO_ROOT))

NOT_ADVICE = ("Research information, not investment advice. Scan outputs are candidates "
              "for your own review, not recommendations to buy or sell.")

USAGE = """daily_invest_mcp.py: local MCP server (stdio) for the Daily Invest research system.

Usage:
  python3 mcp/daily_invest_mcp.py              start the stdio server (waits for an MCP client;
                                               stop it with Ctrl+C, or Ctrl+D to close stdin)
  python3 mcp/daily_invest_mcp.py --selftest   import check, list the registered tools, exit
  python3 mcp/daily_invest_mcp.py --help       this help

Needs Python 3.10+ and mcp 1.x:  python3 -m pip install "mcp>=1.2,<2" yfinance
All outputs are research information, not investment advice.
"""

if __name__ == "__main__" and any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(USAGE)
    sys.exit(0)


def _mcp_import_hint() -> str:
    """Explains WHY mcp.server.fastmcp could not be imported (not installed vs. wrong major)."""
    if sys.version_info < (3, 10):
        return ("the MCP server needs Python 3.10 or newer (this is Python "
                f"{sys.version_info.major}.{sys.version_info.minor}). Create a 3.10+ venv.")
    try:
        from importlib import metadata as _md
        ver = _md.version("mcp")
    except Exception:  # noqa: BLE001
        return 'the "mcp" package is not installed: python3 -m pip install "mcp>=1.2,<2" yfinance'
    major = ver.split(".", 1)[0]
    if major.isdigit() and int(major) >= 2:
        return (f"mcp {ver} is installed, but this server needs mcp 1.x "
                '(mcp 2.x removed mcp.server.fastmcp): python3 -m pip install "mcp>=1.2,<2"')
    return f"mcp {ver} is installed, but importing mcp.server.fastmcp failed (see the error above)"


# FastMCP: klare Fehlermeldung mit dem echten Import-Fehler
try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _e:
    print(f"[ERROR] cannot import mcp.server.fastmcp: {_e}", file=sys.stderr)
    print(f"[ERROR] {_mcp_import_hint()}", file=sys.stderr)
    sys.exit(1)

# yfinance: lazy import
try:
    import yfinance as yf
except ImportError:
    yf = None
    print("[WARN] yfinance nicht installiert. Price/History/Options-Tools degradieren.", file=sys.stderr)


# ============================================================================
# Server Init
# ============================================================================
mcp = FastMCP("daily-invest")


def _not_advice(fn):
    """Adds the not-advice note to every dict an MCP tool returns."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        if isinstance(out, dict):
            out.setdefault("disclaimer", NOT_ADVICE)
        return out
    return wrapper


# ============================================================================
# Helpers
# ============================================================================
def _journal_db_path() -> Path:
    # SANITY-FIX 2026-05-19: trade_journal.py persistiert nach Repo-Root, nicht tools/
    return REPO_ROOT / "trade_journal.sqlite"


def _safe_sqlite_connect(path: Path, retries: int = 3, timeout: float = 5.0):
    """Retry-Loop gegen Errno 35 / 'disk I/O error' (FUSE-Mount-Issue)."""
    import time
    last_err = None
    for i in range(retries):
        try:
            conn = sqlite3.connect(str(path), timeout=timeout)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.5 * (i + 1))
    raise last_err


def _load_universe() -> list[dict]:
    """Lade universe.csv als list of dicts."""
    out = []
    path = TOOLS_DIR / "universe.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(row)
    return out


# ============================================================================
# Market Data Tools
# ============================================================================
@mcp.tool()
@_not_advice
def get_price(ticker: str) -> dict:
    """Letzter Close + Tages-Change% via yfinance. Research information, not investment advice.

    Args:
        ticker: Aktien-Symbol (z.B. "AAPL", "MSFT", "SAP.DE")
    """
    if yf is None:
        return {"error": "yfinance nicht installiert"}
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if hist.empty:
            return {"error": f"keine Daten für {ticker}"}
        last_close = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last_close
        change_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
        return {
            "ticker": ticker,
            "last_close": round(last_close, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round(change_pct, 2),
            "as_of": hist.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
@_not_advice
def get_history(ticker: str, days: int = 30) -> dict:
    """OHLCV-Historie der letzten N Tage. Research information, not investment advice.

    Args:
        ticker: Aktien-Symbol
        days: Anzahl Tage (default 30)
    """
    if yf is None:
        return {"error": "yfinance nicht installiert"}
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{days}d")
        if hist.empty:
            return {"error": f"keine Daten für {ticker}"}
        rows = []
        for idx, row in hist.iterrows():
            rows.append({
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            })
        return {"ticker": ticker, "rows": rows, "n": len(rows)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
@_not_advice
def get_indicators(ticker: str) -> dict:
    """RSI(2), SMA200, ATR(14), distance_to_sma200. Connors-V6.1-Kern.

    Research information, not investment advice: connors_setup_active is a rule state.

    Args:
        ticker: Aktien-Symbol
    """
    if yf is None:
        return {"error": "yfinance nicht installiert"}
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="300d")
        if len(hist) < 200:
            return {"error": f"<200 Tage Daten für {ticker}"}

        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]

        # RSI(2): Connors Style
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(2).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(2).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi2 = 100 - (100 / (1 + rs))

        # SMA200
        sma200 = close.rolling(200).mean()

        # ATR(14)
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = tr1.combine(tr2, max).combine(tr3, max)
        atr14 = tr.rolling(14).mean()

        last_close = float(close.iloc[-1])
        last_sma = float(sma200.iloc[-1])
        last_rsi = float(rsi2.iloc[-1])
        last_atr = float(atr14.iloc[-1])

        return {
            "ticker": ticker,
            "close": round(last_close, 2),
            "rsi2": round(last_rsi, 2),
            "sma200": round(last_sma, 2),
            "atr14": round(last_atr, 3),
            "above_sma200": last_close > last_sma,
            "distance_to_sma200_pct": round((last_close - last_sma) / last_sma * 100, 2),
            "connors_setup_active": (last_rsi < 10) and (last_close > last_sma),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
@_not_advice
def get_options_chain(ticker: str) -> dict:
    """Nearest-Expiry Options-Chain inkl. ATM-Call/Put + PCR. Research information, not investment advice.

    Args:
        ticker: Aktien-Symbol
    """
    if yf is None:
        return {"error": "yfinance nicht installiert"}
    try:
        t = yf.Ticker(ticker)
        expiries = t.options
        if not expiries:
            return {"error": "keine Options für " + ticker}
        nearest = expiries[0]
        chain = t.option_chain(nearest)
        calls = chain.calls
        puts = chain.puts
        spot = float(t.history(period="1d")["Close"].iloc[-1])

        # PCR
        call_vol = calls["volume"].fillna(0).sum()
        put_vol = puts["volume"].fillna(0).sum()
        pcr = round(float(put_vol) / float(call_vol), 3) if call_vol > 0 else None

        # ATM
        atm_call_row = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
        atm_put_row = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]

        return {
            "ticker": ticker,
            "expiry": nearest,
            "spot": round(spot, 2),
            "pcr": pcr,
            "call_volume": int(call_vol),
            "put_volume": int(put_vol),
            "atm_call_strike": float(atm_call_row["strike"].iloc[0]),
            "atm_call_oi": int(atm_call_row["openInterest"].iloc[0] or 0),
            "atm_put_strike": float(atm_put_row["strike"].iloc[0]),
            "atm_put_oi": int(atm_put_row["openInterest"].iloc[0] or 0),
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Journal Tools
# ============================================================================
@mcp.tool()
@_not_advice
def get_journal_status() -> dict:
    """Aktuelle offene Positionen aus trade_journal.sqlite (lokal). Not investment advice."""
    db = _journal_db_path()
    if not db.exists():
        return {"error": "trade_journal.sqlite nicht gefunden"}
    try:
        conn = _safe_sqlite_connect(db)
        cur = conn.cursor()
        # SANITY-FIX 2026-05-19: Tabelle = `trades` (nicht trade_journal)
        cur.execute("""
            SELECT ticker, entry_price, entry_date, source, setup_fk, regime_at_entry, notes
            FROM trades
            WHERE exit_date IS NULL OR exit_date = ''
            ORDER BY entry_date DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return {
            "open_positions": [
                {
                    "ticker": r[0], "entry_price": r[1], "entry_date": r[2],
                    "source": r[3], "setup": r[4], "regime": r[5], "notes": r[6]
                }
                for r in rows
            ],
            "n_open": len(rows),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
@_not_advice
def get_journal_summary(source: str = "all") -> dict:
    """YTD-Summary, optional gefiltert nach Source (live/external/disc/all). Not investment advice.

    Args:
        source: "live", "external", "disc" (= DB-Label 'discretionary'), oder "all"
    """
    db = _journal_db_path()
    if not db.exists():
        return {"error": "trade_journal.sqlite nicht gefunden"}
    try:
        conn = _safe_sqlite_connect(db)
        cur = conn.cursor()
        # SANITY-FIX 2026-05-19: Tabelle = `trades`, PnL-Spalte = `netto_pnl` (nicht realized_pnl_eur)
        # K3/N8-FIX 2026-06-14: (a) Alias 'disc' -> DB-Label 'discretionary' (sonst las
        # source='disc' still 0 Rows -> discretionary Trades unsichtbar); (b) parametrisiertes
        # WHERE (kein f-string-Source mehr).
        _SOURCE_ALIASES = {"disc": "discretionary"}
        src_norm = _SOURCE_ALIASES.get(source, source)
        params: tuple = ()
        where = ""
        if src_norm != "all":
            where = " WHERE source = ?"
            params = (src_norm,)
        # FIX 2026-06-01: ehrliche Kennzahlen.
        #  - eff. P&L = netto_pnl, sonst brutto_pnl-cost  -> faengt Legacy-Rows mit NULL netto_pnl
        #    (die vorher als 0€ gelesen wurden).
        #  - Win-Rate NUR ueber geschlossene Trades (exit_date NOT NULL); is_win bevorzugt,
        #    Fallback eff. P&L > 0. Vorher: wins/ALLE Trades inkl. offen+backtest -> Phantom-37,3%.
        eff = "COALESCE(netto_pnl, brutto_pnl - COALESCE(cost,0))"
        win_expr = f"COALESCE(is_win, CASE WHEN {eff} > 0 THEN 1 ELSE 0 END)"
        cur.execute(f"""
            SELECT
                COUNT(*) as n_trades,
                SUM(CASE WHEN exit_date IS NOT NULL THEN 1 ELSE 0 END) as n_closed,
                SUM(CASE WHEN exit_date IS NOT NULL THEN {win_expr} ELSE 0 END) as wins,
                SUM(CASE WHEN exit_date IS NOT NULL THEN {eff} ELSE 0 END) as total_pnl,
                MAX(CASE WHEN exit_date IS NOT NULL THEN {eff} END) as best,
                MIN(CASE WHEN exit_date IS NOT NULL THEN {eff} END) as worst
            FROM trades
            {where}
        """, params)
        r = cur.fetchone()
        conn.close()
        n_trades = r[0] or 0
        n_closed = r[1] or 0
        wins = r[2] or 0
        total_pnl = r[3] or 0.0
        return {
            "source": source,
            "source_resolved": src_norm,
            "n_trades": n_trades,
            "n_closed": n_closed,
            "n_open": n_trades - n_closed,
            "wins": wins,
            "win_rate_pct": round(wins / n_closed * 100, 2) if n_closed > 0 else 0.0,
            "total_pnl_eur": round(total_pnl, 2),
            "avg_pnl_eur": round(total_pnl / n_closed, 2) if n_closed > 0 else 0.0,
            "best_eur": round(r[4] or 0.0, 2),
            "worst_eur": round(r[5] or 0.0, 2),
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Scanner-Kandidaten (keine Empfehlungen)
# ============================================================================
def _latest_daily_scans():
    """Echte Tages-Scans scan_YYYY-MM-DD.json, neueste zuerst.

    SURFACING-FIX 2026-06-20: EINE Quelle der Wahrheit fuer "letzter Scan".
    Schliesst aus: scan_freshness_*/scan_freshness_range_*, scan_abort_*,
    *.manifest.json UND Zukunfts-Sentinels (z.B. scan_2099-01-01.json, das
    bei reverse-sort sonst ganz oben landet). Verhindert, dass die Read-Tools
    leer/stale zurueckgeben, obwohl ein gueltiger Tages-Scan existiert.
    """
    import re
    from datetime import date as _date
    rx = re.compile(r"^scan_(\d{4}-\d{2}-\d{2})\.json$")
    today = _date.today().isoformat()
    out = []
    for f in TOOLS_DIR.glob("scan_*.json"):
        m = rx.match(f.name)
        if not m:
            continue
        d = m.group(1)
        if d > today:  # Zukunftsdatum = Sentinel/Testfile -> ignorieren
            continue
        out.append((d, f))
    out.sort(reverse=True)
    return [f for _, f in out]


def _scan_candidates_payload(date: Optional[str] = None) -> dict:
    """Gemeinsame Implementierung fuer get_scan_candidates und den Alias get_recommendations."""
    # SANITY-FIX 2026-05-19: Reale Files heißen scan_YYYY-MM-DD.json
    # SURFACING-FIX 2026-06-20: strikter Tages-Scan-Regex + Zukunftsdatum-Guard
    #   via _latest_daily_scans(). Vorher fing der naive glob scan_freshness_range_*
    #   (reverse-sort ganz oben) und das Sentinel scan_2099-01-01.json ab -> Tool gab
    #   dauerhaft LEER zurueck, obwohl der echte Tages-Scan Kandidaten hatte.
    if date is None:
        files = _latest_daily_scans()
    else:
        f = TOOLS_DIR / f"scan_{date}.json"
        files = [f] if f.exists() else []
    if not files:
        return {"error": f"keine Tages-Scanner-Outputs gefunden (date={date})"}
    try:
        with open(files[0], encoding="utf-8") as f:
            data = json.load(f)

        # K2-FIX 2026-06-14: korrekte Scan-Keys. Das Artefakt nutzt
        # scan_date/universe_size/top_setups (+ hebel_watch): die alten Keys
        # date/n_universe/recommendations existierten NICHT -> Tool gab immer leer zurueck.
        def _slim(s: dict) -> dict:
            return {
                "ticker": s.get("ticker"),
                "name": s.get("name"),
                "pre_score": s.get("pre_score"),
                "passes_threshold": s.get("passes_threshold"),
                "tradability": s.get("tradability"),
                "sector": s.get("sector"),
                "close": s.get("close"),
                "atr_pct": s.get("atr_pct"),
            }

        return {
            "file": files[0].name,
            "date": data.get("scan_date"),
            "n_universe": data.get("universe_size"),
            "valid_results": data.get("valid_results"),
            "market_pcr_tag": data.get("market_pcr_tag"),
            "top_candidates": [_slim(s) for s in data.get("top_setups", [])[:5]],
            "hebel_watch": [_slim(s) for s in data.get("hebel_watch", [])[:5]],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
@_not_advice
def get_scan_candidates(date: Optional[str] = None) -> dict:
    """Scan candidates from the latest daily_scanner output (JSON in tools/).

    Research scan output, not investment advice: candidates passed a rule-based
    filter and still need your own review. They are not recommendations.

    Args:
        date: Optional YYYY-MM-DD, default = letzter verfügbarer
    """
    return _scan_candidates_payload(date)


@mcp.tool()
@_not_advice
def get_recommendations(date: Optional[str] = None) -> dict:
    """DEPRECATED alias of get_scan_candidates (kept for one release).

    Research scan output, not investment advice. Use get_scan_candidates.

    Args:
        date: Optional YYYY-MM-DD, default = letzter verfügbarer
    """
    out = _scan_candidates_payload(date)
    if "top_candidates" in out:
        out["top_recommendations"] = out["top_candidates"]  # old key, same data
    out["deprecated"] = "get_recommendations is deprecated, use get_scan_candidates"
    return out


@mcp.tool()
@_not_advice
def get_news_sentiment(ticker: str, date: Optional[str] = None) -> dict:
    """Letztes News-Sentiment für einen Ticker (GDELT+FinDPO). Research information, not investment advice.

    Args:
        ticker: Symbol
        date: Optional YYYY-MM-DD
    """
    pattern = "news_sentiment_*.json" if date is None else f"news_sentiment_{date}.json"
    files = sorted(TOOLS_DIR.glob(pattern), reverse=True)
    if not files:
        return {"error": "kein News-Sentiment-File gefunden"}
    try:
        with open(files[0], encoding="utf-8") as f:
            data = json.load(f)
        ticker_data = data.get("ticker_map", {}).get(ticker.upper())
        if not ticker_data:
            return {"error": f"{ticker} nicht in {files[0].name}"}
        return {
            "ticker": ticker,
            "file": files[0].name,
            "date": data.get("date"),
            "article_count": ticker_data.get("article_count"),
            "sentiment_score": ticker_data.get("sentiment_score"),
            "sentiment_method": ticker_data.get("sentiment_method"),
            "sentiment_model": ticker_data.get("sentiment_model"),
            "top_headlines": ticker_data.get("top_headlines", [])[:3],
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Insider Tool (wrappt CLI)
# ============================================================================
@mcp.tool()
@_not_advice
def get_insider_feed(ticker: str) -> dict:
    """Letzte Insider-Trades für einen Ticker (lokaler insider_feed-Output). Research information, not investment advice.

    Args:
        ticker: Symbol
    """
    # SANITY-FIX 2026-05-19: Reale Files heißen insider_YYYY-MM-DD.json
    files = sorted(TOOLS_DIR.glob("insider_*.json"), reverse=True)
    if not files:
        return {"error": "kein insider_feed-File gefunden"}
    try:
        with open(files[0], encoding="utf-8") as f:
            data = json.load(f)
        rows = [r for r in data.get("rows", []) if r.get("ticker", "").upper() == ticker.upper()]
        return {
            "ticker": ticker,
            "file": files[0].name,
            "n_trades": len(rows),
            "trades": rows[:10],
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Universe Tool
# ============================================================================
@mcp.tool()
@_not_advice
def search_universe(query: str = "", tier: str = "", sector: str = "") -> dict:
    """Filter universe.csv nach Query/Tier/Sector. Research information, not investment advice.

    Args:
        query: Substring im Ticker oder Name
        tier: "A", "B", "C", oder "" für alle
        sector: Sector-Name oder "" für alle
    """
    rows = _load_universe()
    if not rows:
        return {"error": "universe.csv nicht ladbar"}

    q = query.upper()
    filtered = []
    for r in rows:
        if tier and r.get("tier", "") != tier:
            continue
        if sector and sector.lower() not in r.get("sector", "").lower():
            continue
        if q and q not in r.get("ticker", "").upper() and q not in r.get("name", "").upper():
            continue
        filtered.append(r)

    return {
        "n_total": len(rows),
        "n_matched": len(filtered),
        "matched": filtered[:50],
    }


# ============================================================================
# System State
# ============================================================================
def _parse_claude_md_state(text: str) -> dict:
    """STATE-STALENESS-1 (2026-06-20): Versions-/Sprint-Stand aus CLAUDE.md parsen
    statt hartkodieren. Vorher meldete get_system_state einen KW21-23-Geisterstand
    (system_version „V6.1 + KW21“, next_verify „Di 2026-06-02“), unabhaengig vom
    echten Projekt-Stand. Jetzt: Single-Source-of-Truth = CLAUDE.md-Header."""
    import re
    out = {"version": None, "last_update": None, "headline": None, "state": []}
    m = re.search(r"(?:Letztes Update|Last update):\s*(\d{4}-\d{2}-\d{2})\s*[—–-]\s*(V\d+\.\d+[^(\n]*)",
                  text, re.IGNORECASE)
    if m:
        out["last_update"] = m.group(1)
        head = m.group(2).strip().rstrip("—–- ").strip()
        out["headline"] = head
        vm = re.match(r"(V\d+\.\d+)", head)
        out["version"] = vm.group(1) if vm else None
    sm = re.search(r"\*\*STATE.*?\n(.*?)(?:\n---|\n##|\Z)", text, re.S)
    if sm:
        for line in sm.group(1).splitlines():
            s = line.strip()
            if s.startswith("-"):
                s = s.lstrip("-").strip()
                s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)            # bold
                s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)    # links
                s = re.sub(r"`([^`]*)`", r"\1", s)                # code
                if s:
                    out["state"].append(s[:240])
    return out


@mcp.tool()
@_not_advice
def get_system_state() -> dict:
    """Systemstand aus CLAUDE.md, letzter Scan, letzter Journal-Eintrag. Not investment advice."""
    # CLAUDE.md ist Source-of-Truth
    claude_md = REPO_ROOT / "CLAUDE.md"
    has_claude_md = claude_md.exists()

    # Letzter Scan: FIX 2026-06-01: nur echte Tages-Scans (scan_YYYY-MM-DD.json).
    # Vorher fing der Glob auch scan_freshness_range_*.json etc. ab -> stale last_scan_file.
    # SURFACING-FIX 2026-06-20: zentraler Helper _latest_daily_scans() schliesst
    # Zukunfts-Sentinels (scan_2099-01-01.json) UND scan_freshness_*/scan_abort_* aus.
    scans = _latest_daily_scans()
    last_scan = scans[0].name if scans else None

    # Letzte Journal-Activity
    db = _journal_db_path()
    last_trade = None
    if db.exists():
        try:
            conn = _safe_sqlite_connect(db)
            cur = conn.cursor()
            cur.execute("SELECT ticker, entry_date, source FROM trades ORDER BY entry_date DESC LIMIT 1")
            r = cur.fetchone()
            if r:
                last_trade = {"ticker": r[0], "date": r[1], "source": r[2]}
            conn.close()
        except Exception:
            pass

    # STATE-STALENESS-1 (2026-06-20): live aus CLAUDE.md geparst statt hartkodiert.
    parsed = {}
    if has_claude_md:
        try:
            parsed = _parse_claude_md_state(claude_md.read_text(encoding="utf-8"))
        except Exception:
            parsed = {}

    return {
        "system_version": parsed.get("headline") or "unbekannt (CLAUDE.md nicht lesbar)",
        "claude_md_version": parsed.get("version"),
        "claude_md_last_update": parsed.get("last_update"),
        "claude_md_present": has_claude_md,
        "last_scan_file": last_scan,
        "last_trade": last_trade,
        "state_summary": parsed.get("state", []),
        "note": "Stand live aus CLAUDE.md geparst (STATE-STALENESS-1 Fix 2026-06-20); "
                "Sprint-/Action-Item-Detail in CLAUDE.md §1/§9.",
    }


# ============================================================================
# Quick-Trade (READ-ONLY wrapper, safety-first)
# ============================================================================
@mcp.tool()
@_not_advice
def run_quick_trade(action: str, ticker: str = "", price: float = 0.0,
                    source: str = "live", date: str = "", note: str = "",
                    confirm: bool = False) -> dict:
    """Wrappt tools/quick_trade.py (lokales Journal, keine Broker-Order). WRITE-Aktionen brauchen confirm=True.

    Not investment advice. The journal only records what you decided yourself.

    Args:
        action: "status", "summary", "buy", "sell"
        ticker: Symbol (für buy/sell)
        price: Preis in EUR (für buy/sell)
        source: live/discretionary/external (default live)
        date: YYYY-MM-DD (default heute)
        note: Optional Notiz
        confirm: True NÖTIG für buy/sell (Safety-Gate)
    """
    qt = TOOLS_DIR / "quick_trade.py"
    if not qt.exists():
        return {"error": "quick_trade.py nicht gefunden"}

    if action in ("status", "summary"):
        try:
            r = subprocess.run(
                ["python3", str(qt), action],
                capture_output=True, text=True, timeout=15
            )
            return {"action": action, "stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
        except Exception as e:
            return {"error": str(e)}

    if action in ("buy", "sell"):
        if not confirm:
            return {
                "error": "Write-Action: confirm=True erforderlich",
                "preview_cmd": f"python3 tools/quick_trade.py {action} {ticker} {price} --source {source}",
            }
        if not ticker or price <= 0:
            return {"error": "ticker und price (>0) Pflicht"}
        cmd = ["python3", str(qt), action, ticker, str(price), "--source", source]
        if date:
            cmd += ["--date", date]
        if note:
            cmd += ["--note", note]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {"action": action, "stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"unbekannte action: {action}"}


# ============================================================================
# Resources (lesbare Docs für Claude)
# ============================================================================
@mcp.resource("daily-invest://claude-md")
def get_claude_md() -> str:
    """CLAUDE.md: Project-Anchor mit allen Regeln, Codepfaden, Anti-Patterns."""
    path = REPO_ROOT / "CLAUDE.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "CLAUDE.md nicht vorhanden (Repository unvollstaendig?)."


# ============================================================================
# Main
# ============================================================================
def _selftest() -> int:
    """Offline check: module imported, tools and resource registered. Starts no server."""
    import asyncio
    tools = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    resources = [str(r.uri) for r in asyncio.run(mcp.list_resources())]
    expected = {"get_price", "get_history", "get_indicators", "get_options_chain",
                "get_insider_feed", "get_journal_status", "get_journal_summary",
                "get_scan_candidates", "get_recommendations", "get_news_sentiment",
                "search_universe", "get_system_state", "run_quick_trade"}
    print(f"daily_invest_mcp --selftest: {len(tools)} tools, {len(resources)} resource(s)")
    for name in tools:
        print(f"  tool      {name}")
    for uri in resources:
        print(f"  resource  {uri}")
    missing = sorted(expected - set(tools))
    ok = not missing and "daily-invest://claude-md" in resources
    if missing:
        print(f"  MISSING: {missing}")
    print("RESULT: " + ("GREEN" if ok else "RED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    mcp.run()
