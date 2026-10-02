#!/usr/bin/env python3
"""
S5 News-Sentiment-Fetcher (REC-RECOVERY-1 Mega-Audit 2026-05-06).

GDELT Doc 2.0 API + simple Headline-Sentiment für die heutige Top-30 + Watchlist.
GDELT ist 100% kostenfrei, keine Auth, JSON-API, 100+ Sprachen, rolling 3-Monats-Window.

Output: tools/news_sentiment_YYYY-MM-DD.json mit:
  ticker_map: { ticker: { article_count, sentiment_score (-1.0..+1.0), top_headlines } }

Score-Use im daily_scanner: News-Stub 5.0 → 5.0 + (sentiment * 2.0) → Range 3.0..7.0
Damit kann eine bullish-News-Story den Pre-Score um bis zu +0.10 (Gewicht 0.05 × +2.0) nach oben oder unten verschieben.

Usage:
    python3 tools/news_sentiment_fetch.py
    python3 tools/news_sentiment_fetch.py --tickers AAPL,MSFT,NVDA
    python3 tools/news_sentiment_fetch.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import urllib.request
import urllib.parse
import urllib.error

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
UNIVERSE_CSV = TOOLS_DIR / "universe.csv"

# GDELT-Endpoint (Doc 2.0)
GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# Sentinel: HTTP 200 mit leerem/zu-kurzem Body, typisch fuer GDELT-Query-Syntax-Reject
# ("phrase too short"). NICHT zu None konflatieren, damit Aufrufer Retry triggern kann.
_EMPTY_PAYLOAD = {"_empty_payload": True}
_EMPTY_BODY_THRESHOLD = 50  # Bytes; reale GDELT-JSON-Responses sind immer >200 Bytes

# Cache-Last-Good Fenster (Werktage): wenn live-Fetch komplett ausfaellt,
# darf der letzte gueltige Snapshot maximal so alt sein um noch genutzt zu werden.
_CACHE_LASTGOOD_MAX_AGE_BUSINESS_DAYS = 5

# Bullish/Bearish-Keywords für Headline-Sentiment (basic)
# Bewusst konservativ: wir wollen keine False-Positives
BULLISH_KEYWORDS = [
    "surge", "soar", "jump", "rally", "gain", "rise", "climb", "beats",
    "outperform", "raises", "boosts", "upgrade", "bullish", "strong",
    "record-high", "breakthrough", "approves", "green-light", "expands"
]
BEARISH_KEYWORDS = [
    "drop", "plunge", "tumble", "fall", "slip", "decline", "miss", "lose",
    "underperform", "cuts", "downgrade", "bearish", "weak", "warning",
    "lawsuit", "investigation", "delays", "halts", "recall", "fraud", "scandal"
]


def _http_get_json(url: str, timeout: float = 20.0) -> Optional[dict]:
    """HTTP GET → JSON parsen.

    History:
      N1-FIX (2026-05-19): Timeout 10→20s, mehr Headers, Error-Logging.
      NEWS-SENTIMENT-GDELT-QUERY-1 (2026-05-21):
        - HTTP 200 + Body < _EMPTY_BODY_THRESHOLD → return _EMPTY_PAYLOAD-Sentinel statt None.
          Differenziert echten Netz-Fail von GDELT-Query-Syntax-Reject ('phrase too short'),
          damit der Aufrufer Retry mit alternativem Query triggern kann.
        - JSONDecodeError mit empty body → ebenfalls _EMPTY_PAYLOAD (alte Pfade).
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 DailyInvest/1.2",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read().decode("utf-8", errors="replace")
        # GDELT antwortet auf 'phrase too short' mit HTTP 200 und leerem oder
        # winzigem Body. Differenziert vom Netz-Fail (None).
        if not data.strip() or len(data.strip()) < _EMPTY_BODY_THRESHOLD:
            return _EMPTY_PAYLOAD
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            # Auch nicht-JSON-Antworten (HTML-Error-Pages etc.) sind Query-Reject-Signale
            print(f"  [GDELT JSON-Parse-Fail] {str(e)[:60]} body[:80]={data[:80]!r}", file=sys.stderr)
            return _EMPTY_PAYLOAD
    except urllib.error.HTTPError as e:
        print(f"  [GDELT HTTP-Error] {e.code} {e.reason}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  [GDELT URL-Error] {e.reason}", file=sys.stderr)
        return None
    except TimeoutError as e:
        print(f"  [GDELT Timeout] {str(e)[:60]}", file=sys.stderr)
        return None


def _build_gdelt_query(ticker: str, variant: str = "phrase2") -> str:
    """Konstruiere GDELT-Query je Variante.

    NEWS-SENTIMENT-GDELT-QUERY-1 (2026-05-21):
    GDELT lehnt Single-Word-Phrases wie `"AAPL"` mit HTTP 200 + leerem Body ab
    ('phrase too short' Reject: GDELT verlangt Phrasen aus mindestens zwei Woertern).
    Loesung: mehrwoertige Phrase als Default + Single-Phrase als Fallback.

    Varianten:
      phrase2 (default): `"TICKER stock" OR "TICKER shares" sourcelang:english`
                         → 2-Wort-Phrasen, vermeidet 'phrase too short'.
      phrase3:           `"TICKER stock" OR "TICKER shares" OR "TICKER earnings" sourcelang:english`
                         → breiter Finanz-Kontext, falls phrase2 zu eng ist.
      single:            `"TICKER" sourcelang:english`
                         → Legacy-Variante, fuer den Fall dass GDELT-Verhalten kippt.
                         Bei Tickern ≤2 Char wirkt das praktisch nie.
    """
    # GDELT-Syntax (entdeckt 2026-05-21 live-Probe):
    # "Queries containing OR'd terms must be surrounded by ()."
    # → OR-Phrasen MUESSEN in runde Klammern, sonst HTTP 200 + Plain-Text-Error-Message
    if variant == "phrase2":
        return f'("{ticker} stock" OR "{ticker} shares") sourcelang:english'
    if variant == "phrase3":
        return f'("{ticker} stock" OR "{ticker} shares" OR "{ticker} earnings") sourcelang:english'
    if variant == "single":
        return f'"{ticker}" sourcelang:english'
    raise ValueError(f"unknown gdelt query variant: {variant}")


def _fetch_gdelt_once(ticker: str, variant: str, lookback_days: int, max_articles: int) -> tuple[Optional[list[dict]], str]:
    """Ein einzelner GDELT-Call. Returns (articles | None, status).

    status ∈ {'ok', 'empty_payload', 'http_fail', 'no_articles'}
      ok            → articles ist eine non-empty list
      empty_payload → GDELT-Query-Syntax-Reject (HTTP 200 + leerer/winziger Body)
      http_fail     → Netz-Error, Timeout, URL-Fehler
      no_articles   → HTTP 200 + valides JSON, aber leere articles-Liste
    """
    end_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    start_dt = end_dt - timedelta(days=lookback_days)
    start_str = start_dt.strftime("%Y%m%d%H%M%S")
    end_str = end_dt.strftime("%Y%m%d%H%M%S")

    params = {
        "query": _build_gdelt_query(ticker, variant),
        "mode": "artlist",
        "maxrecords": max_articles,
        "sort": "date",
        "startdatetime": start_str,
        "enddatetime": end_str,
        "format": "json",
    }
    url = GDELT_BASE + "?" + urllib.parse.urlencode(params)

    data = _http_get_json(url)
    if data is None:
        return (None, "http_fail")
    if data is _EMPTY_PAYLOAD or (isinstance(data, dict) and data.get("_empty_payload")):
        return (None, "empty_payload")
    articles = data.get("articles", []) if isinstance(data, dict) else []
    if not articles:
        return ([], "no_articles")
    return (articles, "ok")


def fetch_gdelt_articles(ticker: str, lookback_days: int = 3, max_articles: int = 30) -> list[dict]:
    """GDELT Doc 2.0: Headlines mit ticker-Mention.

    History:
      N1-FIX (2026-05-19): Lookback 1→3 Tage.
      NEWS-SENTIMENT-GDELT-QUERY-1 (2026-05-21):
        Multi-Variant-Retry-Loop. phrase2 (default) → phrase3 (breiter) → single (legacy).
        Bricht beim ersten 'ok' ab. Bei 'http_fail' kein Retry (Netz-Problem).
    """
    articles, status = _fetch_gdelt_once(ticker, "phrase2", lookback_days, max_articles)
    if status == "ok":
        return articles or []
    if status == "http_fail":
        return []  # Netz-Problem, kein Retry sinnvoll
    # status ∈ {empty_payload, no_articles} → versuche phrase3
    articles, status = _fetch_gdelt_once(ticker, "phrase3", lookback_days, max_articles)
    if status == "ok":
        return articles or []
    if status == "http_fail":
        return []
    # Last-resort Legacy-Variante, nur sinnvoll wenn GDELT-Verhalten kippt
    articles, status = _fetch_gdelt_once(ticker, "single", lookback_days, max_articles)
    if status == "ok":
        return articles or []
    return []


def fetch_gdelt_articles_with_status(ticker: str, lookback_days: int = 3, max_articles: int = 30) -> tuple[list[dict], str]:
    """Wie fetch_gdelt_articles, aber returnt zusaetzlich den Final-Status.

    Wird vom Caller benoetigt um zu entscheiden, ob Cache-Last-Good greifen soll
    (nur bei 'empty_payload' oder 'http_fail', NICHT bei 'no_articles':
    'no_articles' bedeutet GDELT antwortete sauber, aber zum Ticker gab's nichts).
    """
    articles, status = _fetch_gdelt_once(ticker, "phrase2", lookback_days, max_articles)
    if status == "ok":
        return (articles or [], "ok")
    if status == "http_fail":
        return ([], "http_fail")
    articles, status = _fetch_gdelt_once(ticker, "phrase3", lookback_days, max_articles)
    if status == "ok":
        return (articles or [], "ok")
    if status == "http_fail":
        return ([], "http_fail")
    articles, status = _fetch_gdelt_once(ticker, "single", lookback_days, max_articles)
    return (articles or [], status)


def _business_days_between(d1: datetime, d2: datetime) -> int:
    """Anzahl Werktage (Mo-Fr) zwischen d1 (alt) und d2 (neu). 0 = selber Werktag."""
    if d1 > d2:
        d1, d2 = d2, d1
    n = 0
    cur = d1
    while cur < d2:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:  # Mo=0..Fr=4
            n += 1
    return n


def _load_cache_last_good(today_date: str, max_business_days: int = _CACHE_LASTGOOD_MAX_AGE_BUSINESS_DAYS) -> tuple[Optional[dict], Optional[str], Optional[int]]:
    """Lade letzten gueltigen news_sentiment-Snapshot.

    NEWS-SENTIMENT-GDELT-QUERY-1 (2026-05-21):
    Wenn live-Fetch komplett ausfaellt (GDELT-Reject + Retry-Fail), fallback auf
    den letzten Snapshot der NICHT fail_soft markiert ist und max max_business_days Werktage
    alt ist. Analog zu V7.9.27 PCR-Cache-Last-Good Stage 3 Resolver.

    Returns: (ticker_map | None, cache_date_str | None, age_business_days | None)
    """
    try:
        today_dt = datetime.strptime(today_date, "%Y-%m-%d")
    except ValueError:
        return (None, None, None)

    # Sammle alle news_sentiment_YYYY-MM-DD.json Dateien (ohne partial / today)
    candidates: list[tuple[datetime, Path]] = []
    for f in TOOLS_DIR.glob("news_sentiment_*.json"):
        name = f.name
        if name.endswith(".partial.json"):
            continue
        # Erwartet news_sentiment_YYYY-MM-DD.json
        stem = f.stem  # news_sentiment_YYYY-MM-DD
        date_part = stem.replace("news_sentiment_", "")
        try:
            d = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        if d.date() >= today_dt.date():
            continue  # heute oder zukuenftig nicht
        candidates.append((d, f))

    if not candidates:
        return (None, None, None)

    # Sortiere absteigend (neueste zuerst)
    candidates.sort(key=lambda x: x[0], reverse=True)

    for cand_dt, cand_path in candidates:
        age = _business_days_between(cand_dt, today_dt)
        if age > max_business_days:
            return (None, None, None)  # alles weiter zurueck ist auch zu alt
        try:
            with open(cand_path, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        # Skip fail-soft snapshots: die haben selbst keine Daten
        if payload.get("fail_soft") is True:
            continue
        # Skip wenn n_with_news == 0 (also auch leer)
        if isinstance(payload.get("n_with_news"), int) and payload["n_with_news"] == 0:
            continue
        ticker_map = payload.get("ticker_map")
        if not isinstance(ticker_map, dict) or not ticker_map:
            continue
        return (ticker_map, cand_dt.strftime("%Y-%m-%d"), age)
    return (None, None, None)


_SCORER_IMPORT_WARNED: set = set()


def _import_scorer(name: str):
    """Imports the sibling scorer module tools/<name>.py.

    This tool runs as `python3 tools/news_sentiment_fetch.py` (also from launchd), so
    tools/ is on sys.path and the plain sibling import is the one that works there.
    The `tools.`-prefixed form stays as a fallback when the repo root is on sys.path.
    Returns the module or None, and logs an import failure once per module.
    """
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError as first_err:
        try:
            return importlib.import_module(f"tools.{name}")
        except ImportError:
            if name not in _SCORER_IMPORT_WARNED:
                _SCORER_IMPORT_WARNED.add(name)
                print(f"  [sentiment] {name} unavailable, keyword fallback: {first_err}",
                      file=sys.stderr)
            return None


def score_articles_findpo(articles: list[dict]) -> tuple[float | None, str | None]:
    """R1 (KW21, 2026-05-19): FinDPO-Local-Sentiment mit FinBERT-Fallback.
    Returns (sentiment_score, model_used) oder (None, None) bei Failure.

    Model-Cascade: FinDPO → FinBERT → None (dann Keyword-Fallback im Aufrufer).
    """
    mod = _import_scorer("findpo_scorer")
    if mod is None:
        return (None, None)
    try:
        score = mod.score_articles(articles)
        model = mod.get_model_used()
        if score is not None and model is not None:
            return (score, model)
    except Exception as e:  # noqa: BLE001  (model load or inference error)
        print(f"  [sentiment] FinDPO scoring failed: {str(e)[:120]}", file=sys.stderr)
    return (None, None)


def score_articles_finbert(articles: list[dict]) -> float | None:
    """Legacy FinBERT-Pfad (Hebel A KW20). Wird von FinDPO-Code als Fallback genutzt."""
    mod = _import_scorer("finbert_scorer")
    if mod is None:
        return None
    try:
        return mod.score_articles(articles)
    except Exception as e:  # noqa: BLE001  (model load or inference error)
        print(f"  [sentiment] FinBERT scoring failed: {str(e)[:120]}", file=sys.stderr)
        return None


def score_articles_keyword(articles: list[dict]) -> float:
    """Simple Keyword-Counter-basiertes Sentiment.
    Range: -1.0 (alles bearish) bis +1.0 (alles bullish), 0.0 = neutral / keine Daten.
    """
    if not articles:
        return 0.0

    bullish_hits = 0
    bearish_hits = 0
    total_articles = 0

    for a in articles:
        title = (a.get("title") or "").lower()
        if not title:
            continue
        total_articles += 1
        for kw in BULLISH_KEYWORDS:
            if kw in title:
                bullish_hits += 1
                break  # Maximal 1 bullish-Hit pro Artikel
        for kw in BEARISH_KEYWORDS:
            if kw in title:
                bearish_hits += 1
                break

    if total_articles == 0:
        return 0.0

    return (bullish_hits - bearish_hits) / total_articles


def fetch_sentiment_for_tickers(
    tickers: list[str],
    delay: float = 0.5,
    use_llm: bool = True,
    today_date: Optional[str] = None,
    enable_cache_fallback: bool = True,
) -> dict:
    """Hauptfunktion: pro Ticker GDELT-Fetch + Sentiment-Scoring.

    History:
      R1 (KW21, 2026-05-19): Method-Cascade FinDPO → FinBERT → Keyword.
      NEWS-SENTIMENT-GDELT-QUERY-1 (2026-05-21):
        - Nutzt fetch_gdelt_articles_with_status (3-Variant-Retry-Loop)
        - Cache-Last-Good Fallback bei empty_payload/http_fail
        - Per-Ticker fetch_status im Output

    Methoden im Output (sentiment_method):
      findpo / finbert    → live GDELT + LLM
      keyword             → live GDELT + Keyword-Counter
      cache_lastgood      → live GDELT komplett kaputt, alter Snapshot genutzt
      no_data             → live ok, aber 0 Artikel UND kein Cache (echtes neutral)

    daily_scanner appliziert news_adjust-Multipliers:
      findpo: ±0.12, finbert: ±0.10, keyword: ±0.05,
      cache_lastgood: ±0.05 (defensive, alt), no_data: 0.0
    """
    out = {}
    n = len(tickers)
    findpo_used = 0
    finbert_used = 0
    keyword_used = 0
    cache_used = 0
    no_data = 0

    # Lade Cache-Last-Good einmalig, wenn enabled
    cache_map: Optional[dict] = None
    cache_date: Optional[str] = None
    cache_age: Optional[int] = None
    if enable_cache_fallback:
        date_str = today_date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
        cache_map, cache_date, cache_age = _load_cache_last_good(date_str)
        if cache_map:
            print(f"  [CACHE] Last-Good snapshot vom {cache_date} geladen ({cache_age} Werktage alt, {len(cache_map)} Tickers)", file=sys.stderr)

    for i, t in enumerate(tickers):
        articles, fetch_status = fetch_gdelt_articles_with_status(t, lookback_days=3, max_articles=30)

        sentiment: Optional[float] = None
        method = "no_data"
        model_used = None
        cache_hit = False

        if use_llm and articles:
            sentiment, model_used = score_articles_findpo(articles)
            if sentiment is not None and model_used:
                if "findpo" in model_used.lower():
                    method = "findpo"
                    findpo_used += 1
                else:
                    method = "finbert"
                    finbert_used += 1

        if sentiment is None and articles:
            sentiment = score_articles_keyword(articles)
            method = "keyword"
            keyword_used += 1

        # Cache-Fallback nur bei echtem Live-Fail (nicht bei no_articles 'es gibt halt nichts')
        if sentiment is None and not articles and fetch_status in ("empty_payload", "http_fail") and cache_map is not None:
            entry = cache_map.get(t)
            if isinstance(entry, dict) and entry.get("article_count", 0) > 0:
                sentiment = entry.get("sentiment_score", 0.0)
                articles = []  # Headlines aus Cache nicht uebertragen: Frische ist eh weg
                method = "cache_lastgood"
                cache_hit = True
                cache_used += 1

        if sentiment is None:
            sentiment = 0.0
            no_data += 1

        top_headlines = [
            {"title": a.get("title"), "url": a.get("url"), "domain": a.get("domain")}
            for a in articles[:5]
        ]
        out[t] = {
            "article_count": len(articles),
            "sentiment_score": round(sentiment, 3),
            "sentiment_method": method,
            "sentiment_model": model_used,
            "fetch_status": fetch_status,
            "cache_hit": cache_hit,
            "cache_date": cache_date if cache_hit else None,
            "cache_age_business_days": cache_age if cache_hit else None,
            "top_headlines": top_headlines,
        }
        if i % 10 == 0 and i > 0:
            print(f"  [GDELT] {i}/{n} done (FinDPO:{findpo_used} FinBERT:{finbert_used} KW:{keyword_used} Cache:{cache_used} NoData:{no_data})", file=sys.stderr)
        time.sleep(delay)
    return out


def load_universe_tickers(tier_filter: Optional[str] = None, limit: Optional[int] = None) -> list[str]:
    """Lade Tickers aus universe.csv, optional gefiltert auf Tier."""
    out = []
    with open(UNIVERSE_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if tier_filter and row["tier"] not in tier_filter.split(","):
                continue
            if row["tier"] == "X":  # Tier X überspringen
                continue
            out.append(row["ticker"])
    if limit:
        out = out[:limit]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Override Datum (YYYY-MM-DD)")
    ap.add_argument("--tickers", default=None, help="Komma-separierte Liste, sonst Universe.")
    ap.add_argument("--tier", default="A,B", help="Tier-Filter (default: A,B, Top-Universum)")
    ap.add_argument("--limit", type=int, default=None, help="Max-Tickers (debug)")
    ap.add_argument("--delay", type=float, default=0.5, help="Sekunden zwischen Requests")
    ap.add_argument("--selftest", action="store_true", help="5-Ticker Smoke-Test, kein File-Write")
    args = ap.parse_args()

    scan_date = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")

    if args.selftest:
        print("=== GDELT-Sentiment Selftest (5 Tickers, kein Write) ===\n", file=sys.stderr)
        tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        results = fetch_sentiment_for_tickers(tickers, delay=args.delay, today_date=scan_date)
        for t, r in results.items():
            print(f"  {t}: articles={r['article_count']:>3} sentiment={r['sentiment_score']:>+.3f} method={r['sentiment_method']:<14} fetch={r['fetch_status']}")
            if r["top_headlines"]:
                print(f"    top: {r['top_headlines'][0]['title'][:80]}")
        sys.exit(0)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_universe_tickers(tier_filter=args.tier, limit=args.limit)

    print(f"[S5-NEWS] GDELT-Sentiment für {len(tickers)} Ticker ({scan_date}) ...", file=sys.stderr)
    results = fetch_sentiment_for_tickers(tickers, delay=args.delay, today_date=scan_date)

    # Stats
    bullish = sum(1 for r in results.values() if r["sentiment_score"] > 0.1)
    bearish = sum(1 for r in results.values() if r["sentiment_score"] < -0.1)
    neutral = len(results) - bullish - bearish
    no_news = sum(1 for r in results.values() if r["article_count"] == 0)
    n_cache_hits = sum(1 for r in results.values() if r.get("cache_hit"))
    n_empty_payload = sum(1 for r in results.values() if r.get("fetch_status") == "empty_payload")
    n_http_fail = sum(1 for r in results.values() if r.get("fetch_status") == "http_fail")
    n_findpo = sum(1 for r in results.values() if r["sentiment_method"] == "findpo")
    n_finbert = sum(1 for r in results.values() if r["sentiment_method"] == "finbert")
    n_keyword = sum(1 for r in results.values() if r["sentiment_method"] == "keyword")
    n_nodata = sum(1 for r in results.values() if r["sentiment_method"] == "no_data")

    # NEWS-SENTIMENT-GDELT-QUERY-1: fail_soft entscheidet sich an reality
    # Wenn kein einziger Ticker live-Daten hatte UND kein Cache-Hit → echtes Hard-Fail
    n_live_ok = sum(1 for r in results.values() if r.get("fetch_status") == "ok")
    fail_soft = (n_live_ok == 0 and n_cache_hits == 0)
    if fail_soft:
        status = "fail_soft_gdelt_query_reject" if n_empty_payload > 0 else "fail_soft_http"
    elif n_cache_hits > 0 and n_live_ok == 0:
        status = "cache_only_no_live"
    elif n_cache_hits > 0:
        status = "mixed_live_and_cache"
    else:
        status = "ok"

    out = {
        "date": scan_date,
        "version": "S5-GDELT-v2-NEWS-SENTIMENT-GDELT-QUERY-1",
        "n_universe": len(tickers),
        "n_with_news": len(tickers) - no_news,
        "n_bullish": bullish,
        "n_bearish": bearish,
        "n_neutral": neutral,
        "n_cache_hits": n_cache_hits,
        "n_live_ok": n_live_ok,
        "n_empty_payload": n_empty_payload,
        "n_http_fail": n_http_fail,
        "n_findpo": n_findpo,
        "n_finbert": n_finbert,
        "n_keyword": n_keyword,
        "n_no_data": n_nodata,
        "source": "gdelt_doc_2.0",
        "fail_soft": fail_soft,
        "status": status,
        "ticker_map": results,
    }

    out_path = TOOLS_DIR / f"news_sentiment_{scan_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"[S5-NEWS] saved: {out_path}", file=sys.stderr)
    print(f"[S5-NEWS] bullish={bullish} bearish={bearish} neutral={neutral} no_news={no_news} cache_hits={n_cache_hits} status={status}", file=sys.stderr)


if __name__ == "__main__":
    main()
