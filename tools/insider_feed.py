"""
V6.7 E7F Insider / Congress FREE-Stack.

Kostenfreier Ersatz für Quiver Quant API ($30/mo). Drei Quellen:
  1. SEC EDGAR Form 4 RSS: Insider-Transaktionen (Officer/Director)
  2. openinsider.com: Cluster-Buy-Filter (3+ Insider / 30d)
  3. capitoltrades.com: Congress-Disclosures (Periodic Trade Reports)

Output:
  tools/insider_YYYY-MM-DD.json
  {
    "date": "...",
    "version": "V6.7-E7F",
    "sources": {...},                   # health pro Quelle
    "ticker_map": {
      "AAPL": {
        "insider_score": +1..-2..+2,    # Composite
        "edgar_signals":    [ {...}, ... ],
        "openinsider_tags": [ "cluster_buy", ... ],
        "congress_signals": [ {...}, ... ],
        "score_breakdown":  {...}
      },
      ...
    }
  }

Score-Matrix (additiv, hart geclippt in [-2, +2]):
  +2  Cluster-Buy 3+ Insider / 30d           (openinsider "cluster")
  +1  Einzel-Insider-Buy > 100 k$             (EDGAR oder openinsider)
  +1  Congress-Buy < 7d, size >= "$50k-$100k" (capitoltrades)
  +1  Mehrere Congress-Buys < 30d (2+)         (capitoltrades)
  -1  3+ Insider-Sells / 30d                  (EDGAR aggregation)
  -2  Einzel-Insider-Sell > 500 k$            (EDGAR)
  -1  Congress-Sell < 7d  (2+)                 (capitoltrades)

Politik:
  - Keine Paid-APIs.
  - User-Agent explizit (SEC_USER_AGENT = eigener Name + E-Mail) + respektvolle Rate-Limits.
  - openinsider.com und capitoltrades.com sind Website-Scraper und deshalb OPT-IN
    (--enable-openinsider / --enable-congress). Pruefe vorher die Nutzungsbedingungen
    der Anbieter; Nutzung auf eigenes Risiko. Ohne Flags laeuft nur, was explizit
    aktiviert ist (z. B. --enable-sec-edgar).
  - Jede Quelle failsoft: Ausfall einer Quelle legt die anderen nicht lahm.
  - Nur Ticker aus dem Universe (universe.csv) werden gehalten.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import urllib.request

TOOLS_DIR = Path(__file__).parent
UNIVERSE_CSV = TOOLS_DIR / "universe.csv"

# SEC fair-access policy: automated requests must carry a real contact of the
# person running the tool. Set SEC_USER_AGENT, e.g. "Jane Doe jane@example.com".
UA_STRING = (os.environ.get("SEC_USER_AGENT")
             or "daily-invest-oss/1.0 (set SEC_USER_AGENT to 'Your Name your@email')")
DEFAULT_TIMEOUT = 15


# ---------- HTTP helper ----------

def http_get(url: str, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Einfacher GET mit User-Agent, rate-limited auf 2/sec."""
    hdrs = {"User-Agent": UA_STRING, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- Source 1: SEC EDGAR Form 4 RSS ----------

EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=40&output=atom"

_EDGAR_CIK_CACHE: dict[str, str] = {}


def _edgar_build_cik_map() -> dict[str, str]:
    """Lädt company_tickers.json einmal pro Prozess und baut ticker->CIK Map (10-stellig zero-padded)."""
    global _EDGAR_CIK_CACHE
    if _EDGAR_CIK_CACHE:
        return _EDGAR_CIK_CACHE
    try:
        raw = http_get(EDGAR_TICKERS_URL, headers={"User-Agent": UA_STRING}, timeout=20)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        out: dict[str, str] = {}
        # Format: {"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}, ...}
        for v in data.values():
            t = str(v.get("ticker", "")).upper()
            cik = v.get("cik_str")
            if t and cik is not None:
                out[t] = str(cik).zfill(10)
        _EDGAR_CIK_CACHE = out
        return out
    except Exception as e:
        print(f"[EDGAR] CIK-Map fetch error: {e}", file=sys.stderr)
        return {}


def fetch_edgar_form4(tickers: Iterable[str], days: int = 30, delay: float = 0.15,
                      max_tickers: int | None = None) -> dict:
    """
    V6.8.1 SHADOW: Holt Form 4-Einträge via EDGAR Atom-Feed pro CIK (gratis, rate-limited).

    Ablauf:
      1. company_tickers.json -> ticker -> CIK
      2. https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=…&type=4&output=atom
      3. Aus dem Atom jedes <entry>: title (enthält Form + filer), updated (filed date), id (accession)

    SEC Fair-Access-Policy: <10 req/s, UA mit Kontakt. Wir nutzen 6-7 req/s (delay=0.15s).

    SHADOW-MODE: Ergebnisse landen nur unter `edgar_shadow` im JSON-Output und werden NICHT
    in den Live-`insider_score` addiert. Zweck: Datenbasis für 2026-04-29-Audit.

    Return: {ticker: {"cik": "...", "filings": [{accession, filed_date, title}, ...], "count_30d": int}}
    """
    cik_map = _edgar_build_cik_map()
    if not cik_map:
        return {}

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None).date() - timedelta(days=days)
    entry_re = re.compile(r'<entry>(.*?)</entry>', re.DOTALL)
    title_re = re.compile(r'<title>(.*?)</title>', re.DOTALL)
    updated_re = re.compile(r'<updated>([^<]+)</updated>')
    id_re = re.compile(r'<id>([^<]+)</id>')

    out: dict[str, dict] = {}
    seen = 0
    for t in tickers:
        if max_tickers is not None and seen >= max_tickers:
            break
        cik = cik_map.get(t.upper())
        if not cik:
            continue
        url = EDGAR_BROWSE_URL.format(cik=cik)
        try:
            xml = http_get(url, headers={"User-Agent": UA_STRING}, timeout=15).decode("utf-8", errors="replace")
        except Exception as e:
            out[t] = {"cik": cik, "error": str(e), "filings": [], "count_30d": 0}
            time.sleep(delay)
            continue

        filings: list[dict] = []
        for em in entry_re.finditer(xml):
            block = em.group(1)
            title_m = title_re.search(block)
            upd_m = updated_re.search(block)
            id_m = id_re.search(block)
            if not (title_m and upd_m):
                continue
            try:
                filed = datetime.fromisoformat(upd_m.group(1).replace("Z", "+00:00")).date()
            except Exception:
                continue
            if filed < cutoff:
                continue
            filings.append({
                "accession": (id_m.group(1) if id_m else "").split("/")[-1],
                "filed_date": filed.isoformat(),
                "title": title_m.group(1).strip(),
            })

        if filings:
            out[t] = {"cik": cik, "filings": filings, "count_30d": len(filings)}
        seen += 1
        time.sleep(delay)

    return out


# ---------- Source 2: openinsider.com ----------

OPENINSIDER_URL = "http://openinsider.com/screener?s={ticker}&o=&pl=&ph=&ll=&lh=&fd=30&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&xs=1&vl=&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=100&page=1"
OPENINSIDER_CLUSTER_URL = "http://openinsider.com/top-insider-purchases-of-the-month"


def fetch_openinsider_for_ticker(ticker: str) -> dict:
    """
    Fetches openinsider.com table for a single ticker (last 30d).
    Returns: {'buys': int, 'sells': int, 'cluster_flag': bool,
              'max_buy_value': float, 'insiders_buying': int, 'insiders_selling': int}
    """
    try:
        url = OPENINSIDER_URL.format(ticker=urllib.parse.quote(ticker))
        html = http_get(url, timeout=10).decode("utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Parse simple table: rows with <td>$value</td> cells
    # We extract: transaction type (P=Purchase, S=Sale), reporting person, value
    rows_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
    tag_strip = re.compile(r'<[^>]+>')

    buys, sells = 0, 0
    buyers, sellers = set(), set()
    max_buy_value = 0.0
    transactions = []

    for m in rows_pattern.finditer(html):
        cells = [tag_strip.sub('', c).strip() for c in cell_pattern.findall(m.group(1))]
        if len(cells) < 12:
            continue
        # openinsider columns vary; we detect by heuristic:
        # typical layout: [x, filing_date, trade_date, ticker, company, insider, title, trans_type, price, qty, owned, diff_owned, value, ...]
        try:
            trans_type = next((c for c in cells if c in ("P - Purchase", "S - Sale", "S - Sale+OE", "P")), None)
            if trans_type is None:
                continue
            # Value is typically the last $-formatted cell
            value = 0.0
            for c in reversed(cells):
                if c.startswith("$") or c.startswith("+$") or c.startswith("-$"):
                    m_val = re.match(r'[+-]?\$([\d,]+)', c)
                    if m_val:
                        value = float(m_val.group(1).replace(",", ""))
                        break
            # Insider name heuristic: col 5 or 6
            insider = cells[5] if len(cells) > 5 else ""
            is_buy = trans_type.startswith("P")
            is_sell = trans_type.startswith("S")
            if is_buy:
                buys += 1
                buyers.add(insider)
                max_buy_value = max(max_buy_value, value)
            elif is_sell:
                sells += 1
                sellers.add(insider)
            transactions.append({
                "type": "buy" if is_buy else "sell",
                "insider": insider,
                "value_usd": value,
            })
        except Exception:
            continue

    cluster_flag = len(buyers) >= 3

    return {
        "ok": True,
        "buys": buys,
        "sells": sells,
        "insiders_buying": len(buyers),
        "insiders_selling": len(sellers),
        "cluster_flag": cluster_flag,
        "max_buy_value": max_buy_value,
        "transactions": transactions[:10],  # cap
    }


def fetch_openinsider_all(tickers: list[str], delay: float = 0.6) -> dict:
    """
    Iteriert Universe und holt openinsider pro Ticker.
    Delay zwischen Requests zur Rate-Limit-Schonung.

    Gibt dict {ticker: result_dict} zurück; ticker ohne Transaktionen werden ausgelassen.
    """
    out = {}
    errors = 0
    for i, t in enumerate(tickers):
        res = fetch_openinsider_for_ticker(t)
        if not res.get("ok"):
            errors += 1
            continue
        if res["buys"] == 0 and res["sells"] == 0:
            continue
        out[t] = res
        time.sleep(delay)
        if i % 50 == 0 and i > 0:
            print(f"  [openinsider] {i}/{len(tickers)} ({errors} errors)", file=sys.stderr)
    return out


# ---------- Source 3: capitoltrades.com (Congress) ----------

CAPITOL_URL = "https://www.capitoltrades.com/trades?pageSize=100&txDate=last30days"


def fetch_capitoltrades_recent(days: int = 30) -> list[dict]:
    """
    Scrape public capitoltrades.com last-N-days trades.

    Returns list of {ticker, politician, chamber, date, type, size_label}.
    Failt soft (leere Liste) bei HTML-Änderungen.
    """
    try:
        html = http_get(CAPITOL_URL, timeout=15).decode("utf-8", errors="replace")
    except Exception as e:
        return []

    out = []
    # Capitol Trades HTML uses Next.js: fall back to a simple JSON-in-script extraction
    m = re.search(r'self\.__next_f\.push\(\[1,"(.+?)"\]\)', html, re.DOTALL)
    if not m:
        return out
    blob = m.group(1).encode("utf-8").decode("unicode_escape", errors="replace")

    # Heuristic: find ticker-like patterns near trade objects
    # ({"issuerTicker":"AAPL","politician":...,"type":"buy"/"sell","size":"$1,001 - $15,000",...})
    trade_pattern = re.compile(
        r'"issuerTicker":"([A-Z]{1,5}(?::US)?)"[^}]*?"firstName":"([^"]+)"[^}]*?"lastName":"([^"]+)"[^}]*?"type":"([^"]+)"[^}]*?"sizeRange":"([^"]+)"[^}]*?"traded":"([^"]+)"',
        re.DOTALL,
    )
    for mt in trade_pattern.finditer(blob):
        tk = mt.group(1).replace(":US", "")
        out.append({
            "ticker": tk,
            "politician": f"{mt.group(2)} {mt.group(3)}",
            "type": mt.group(4).lower(),
            "size_range": mt.group(5),
            "date": mt.group(6)[:10],
        })
    return out


# ---------- Source aggregation ----------

LARGE_SIZE_KEYWORDS = ["$50,001", "$100,001", "$250,001", "$500,001", "$1,000,001", "$5,000,001"]


def size_is_large(size_label: str) -> bool:
    return any(k in size_label for k in LARGE_SIZE_KEYWORDS)


def compute_insider_score(ticker: str, openinsider: dict | None, congress: list[dict]) -> tuple[float, dict]:
    """
    Composite score, clipped to [-2, +2].
    Returns (score, breakdown).
    """
    breakdown: dict = {}
    score = 0.0

    # openinsider
    if openinsider and openinsider.get("ok"):
        if openinsider.get("cluster_flag"):
            score += 2
            breakdown["cluster_buy"] = "+2"
        if openinsider.get("max_buy_value", 0) >= 100_000 and not openinsider.get("cluster_flag"):
            score += 1
            breakdown["large_single_buy"] = "+1"
        if openinsider.get("insiders_selling", 0) >= 3:
            score -= 1
            breakdown["cluster_sell"] = "-1"
        if openinsider.get("max_buy_value", 0) == 0 and openinsider.get("sells", 0) >= 1:
            # Look for a big single sell in transactions
            big_sell = any(
                t.get("type") == "sell" and t.get("value_usd", 0) >= 500_000
                for t in openinsider.get("transactions", [])
            )
            if big_sell:
                score -= 2
                breakdown["large_single_sell"] = "-2"

    # congress
    congress_for_ticker = [c for c in congress if c["ticker"] == ticker]
    if congress_for_ticker:
        buys = [c for c in congress_for_ticker if c["type"] == "buy"]
        sells = [c for c in congress_for_ticker if c["type"] == "sell"]
        recent_buys = [c for c in buys if _days_since(c["date"]) <= 7]
        if len(recent_buys) >= 1 and any(size_is_large(c["size_range"]) for c in recent_buys):
            score += 1
            breakdown["congress_recent_big_buy"] = "+1"
        if len(buys) >= 2:
            score += 1
            breakdown["congress_multi_buy"] = "+1"
        recent_sells = [c for c in sells if _days_since(c["date"]) <= 7]
        if len(recent_sells) >= 2:
            score -= 1
            breakdown["congress_recent_sells"] = "-1"

    # Hard clip
    score = max(-2.0, min(2.0, score))
    return score, breakdown


def _days_since(iso_date: str) -> int:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
        return (datetime.now(timezone.utc).replace(tzinfo=None).date() - d).days
    except Exception:
        return 999


# ---------- Main ----------

def load_universe_tickers(tier_filter: str | None = None) -> list[str]:
    tickers = []
    with open(UNIVERSE_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("tier") == "X":
                continue
            if tier_filter and row["tier"] not in tier_filter.split(","):
                continue
            # Nur US-Ticker: openinsider und capitoltrades decken nur US-Markt
            if row.get("region") not in ("US",) and "." in row["ticker"]:
                continue
            tickers.append(row["ticker"])
    return tickers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--tier", default="A,B,C",
                    help="Comma-separated tier filter (default A,B,C, inkl. US-Small/Mid-Cap, "
                         "wo der Insider-Edge historisch am staerksten ist). "
                         "2026-04-19 erweitert von A,B (INSIDER_COVERAGE_GAP-Fix).")
    ap.add_argument("--limit", type=int, default=None, help="Max tickers (debug)")
    ap.add_argument("--enable-openinsider", action="store_true",
                    help="OPT-IN: openinsider.com scrapen (Website-Scraping; ToS des Anbieters "
                         "pruefen, Nutzung auf eigenes Risiko)")
    ap.add_argument("--enable-congress", action="store_true",
                    help="OPT-IN: capitoltrades.com scrapen (Website-Scraping; ToS des Anbieters "
                         "pruefen, Nutzung auf eigenes Risiko)")
    ap.add_argument("--skip-openinsider", action="store_true",
                    help="openinsider.com nicht abfragen (Default; ueberstimmt --enable-openinsider)")
    ap.add_argument("--skip-congress", action="store_true",
                    help="capitoltrades.com nicht abfragen (Default; ueberstimmt --enable-congress)")
    ap.add_argument("--enable-sec-edgar", action="store_true",
                    help="V6.8.1 SHADOW-Mode: aktiviert EDGAR Form-4 Atom-Feed-Fetch. "
                         "Ergebnisse landen in ticker_map.*.edgar_shadow, werden NICHT in insider_score addiert.")
    ap.add_argument("--edgar-max-tickers", type=int, default=120,
                    help="Maximale Ticker für EDGAR-Fetch (Default 120, SEC Fair-Access)")
    ap.add_argument("--delay", type=float, default=0.6, help="Inter-request delay in seconds")
    args = ap.parse_args()

    scan_date = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    tickers = load_universe_tickers(args.tier)
    if args.limit:
        tickers = tickers[: args.limit]

    print(f"[E7F] Insider-Feed für {len(tickers)} Ticker ({scan_date}) ...", file=sys.stderr)

    sources_health = {
        "openinsider": "skipped",
        "congress": "skipped",
        "edgar": "disabled (shadow-only when --enable-sec-edgar)",
    }

    openinsider_data = {}
    if args.enable_openinsider and not args.skip_openinsider:
        print("[E7F] Fetching openinsider.com ...", file=sys.stderr)
        try:
            openinsider_data = fetch_openinsider_all(tickers, delay=args.delay)
            sources_health["openinsider"] = f"ok ({len(openinsider_data)} tickers mit Signalen)"
        except Exception as e:
            sources_health["openinsider"] = f"error: {e}"

    congress_data: list[dict] = []
    if args.enable_congress and not args.skip_congress:
        print("[E7F] Fetching capitoltrades.com ...", file=sys.stderr)
        try:
            congress_data = fetch_capitoltrades_recent(days=30)
            sources_health["congress"] = f"ok ({len(congress_data)} Trades)"
        except Exception as e:
            sources_health["congress"] = f"error: {e}"

    # V6.8.1 SHADOW: optional EDGAR Form-4
    edgar_data: dict = {}
    if args.enable_sec_edgar:
        print(f"[E7F] SHADOW: Fetching SEC-EDGAR Form-4 (max {args.edgar_max_tickers} tickers) ...",
              file=sys.stderr)
        try:
            edgar_data = fetch_edgar_form4(
                tickers, days=30, delay=0.15, max_tickers=args.edgar_max_tickers,
            )
            sources_health["edgar"] = (
                f"shadow-ok ({len(edgar_data)} ticker mit Form-4 Filings, "
                f"{sum(len(v.get('filings', [])) for v in edgar_data.values())} Filings total)"
            )
        except Exception as e:
            sources_health["edgar"] = f"shadow-error: {e}"

    # Aggregate per-ticker
    ticker_map = {}
    shadow_only: dict = {}  # Ticker die NUR via EDGAR-Shadow erfasst wurden
    for t in tickers:
        oi = openinsider_data.get(t)
        congress_for_t = [c for c in congress_data if c["ticker"] == t]
        edgar_for_t = edgar_data.get(t) or edgar_data.get(t.upper())
        if not oi and not congress_for_t and not edgar_for_t:
            continue
        score, breakdown = compute_insider_score(t, oi, congress_data)  # EDGAR bewusst ausgeklammert
        entry = {
            "insider_score": round(score, 2),
            "openinsider": oi,
            "congress_signals": congress_for_t,
            "score_breakdown": breakdown,
        }
        if edgar_for_t:
            entry["edgar_shadow"] = {
                "cik": edgar_for_t.get("cik"),
                "filings_30d": edgar_for_t.get("count_30d", 0),
                "filings": edgar_for_t.get("filings", [])[:10],
                "note": "SHADOW-Mode: nicht in insider_score addiert. Re-Eval 2026-04-29.",
            }
        ticker_map[t] = entry
        if edgar_for_t and not oi and not congress_for_t:
            shadow_only[t] = entry["edgar_shadow"]

    out = {
        "date": scan_date,
        "version": "V6.8.1-E7F+EDGARShadow" if args.enable_sec_edgar else "V6.7-E7F",
        "n_universe": len(tickers),
        "n_flagged": len(ticker_map),
        "sources": sources_health,
        "edgar_shadow": {
            "enabled": bool(args.enable_sec_edgar),
            "n_tickers": len(edgar_data),
            "n_shadow_only": len(shadow_only),
            "max_tickers": args.edgar_max_tickers,
            "note": "Form-4 Filings werden NICHT live gescored (Observation-Freeze bis 2026-04-29).",
        },
        "ticker_map": ticker_map,
    }
    out_path = TOOLS_DIR / f"insider_{scan_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"[E7F] saved: {out_path}", file=sys.stderr)
    print(f"[E7F] sources: {sources_health}", file=sys.stderr)
    # Top flagged tickers summary
    top = sorted(ticker_map.items(), key=lambda kv: abs(kv[1]["insider_score"]), reverse=True)[:20]
    print(f"\n=== TOP 20 INSIDER-FLAGS ({scan_date}) ===")
    print(f"{'Ticker':<8} {'Score':<7} {'Breakdown'}")
    for t, d in top:
        print(f"{t:<8} {d['insider_score']:+.2f}  {d['score_breakdown']}")


if __name__ == "__main__":
    main()
