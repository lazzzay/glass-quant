#!/usr/bin/env python3
"""
Q1: Reddit/WSB-Trending-Shadow-Scanner (KW17 Rollout, Tag 4).

Retail-Sentiment-Signal als SHADOW-only Erweiterung. Sammelt Ticker-Mentions
aus den letzten 24h in r/wallstreetbets, r/stocks, r/investing, r/options
und ranked nach Mention-Velocity + Upvotes.

Nur Ticker, die in ``tools/universe.csv`` (Tier A/B) stehen, werden
uebernommen. Ausgabe: ``shadow_signals/reddit_trending_YYYY-MM-DD.json``
mit Top-20 + Roh-Metriken fuer Scanner-Annotation und Briefing-Hook.

Feature-Freeze-konform: SHADOW-only. Keine Aenderung am Live-Scoring,
keine Paid-APIs. Erfolgsfall (PRAW + Credentials) nutzt PRAW, Fallback
auf Reddit's oeffentliche JSON-Endpoints (kein Auth noetig, rate-limitiert).

Usage
-----
    python3 signals/reddit_trending.py
    python3 signals/reddit_trending.py --subreddits wallstreetbets,stocks
    python3 signals/reddit_trending.py --top-n 30 --lookback-hours 48
    python3 signals/reddit_trending.py --test
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Pfade + Defaults
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_CSV = REPO_ROOT / "tools" / "universe.csv"
SHADOW_DIR = REPO_ROOT / "shadow_signals"
BRIEFINGS_DIR = REPO_ROOT / "Briefings"

DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing", "options")
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_TOP_N = 20
DEFAULT_MAX_POSTS_PER_SUB = 100
DEFAULT_MAX_COMMENTS_PER_POST = 20
# Neutral default. Reddit asks API clients to identify themselves, so set your own
# REDDIT_USER_AGENT (for example "daily-invest-oss/1.0 by your-reddit-username").
DEFAULT_USER_AGENT = "daily-invest-oss/1.0 (set REDDIT_USER_AGENT)"


def _user_agent_default() -> str:
    """Same precedence everywhere: REDDIT_USER_AGENT env, else the neutral default."""
    return os.environ.get("REDDIT_USER_AGENT") or DEFAULT_USER_AGENT

# Tokens, die wie Ticker aussehen aber keine sind. Die Universe-Whitelist
# deckt den Grossteil schon ab; das hier ist nur eine Zusatzsicherung
# fuer Faelle wie "A" oder "C" (beide echte Tickers aber haeufig nur
# Einzelbuchstaben-Rauschen im Text).
HARD_BLACKLIST = frozenset({
    "A", "C", "DD", "AI", "AH", "AM", "PM", "CEO", "CFO", "USA", "US",
    "EU", "UK", "PR", "EPS", "DD", "YOLO", "WSB", "FOMO", "FUD", "ATH",
    "ATL", "ATM", "OTM", "ITM", "IV", "OI", "IPO", "CPI", "PPI", "ETF",
    "IT", "OK", "TV", "PC", "OP", "TLDR", "TLDR", "LOL", "LMAO", "IRS",
    "SEC", "NYSE", "NASDAQ", "GDP", "FED", "ECB", "BOJ", "FYI", "GG",
    "HODL", "MOASS", "YTD", "YOY", "QOQ", "P", "T", "S", "N",
})

# Regex: Cashtag ($TICK) oder freistehendes 1-5-Letter-Uppercase-Wort.
CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
PLAIN_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------
@dataclass
class MentionContext:
    """Einzelne Fundstelle eines Tickers."""
    ticker: str
    subreddit: str
    post_id: str
    post_title: str
    post_url: str
    source: str  # "title", "selftext" oder "comment"
    upvotes: int
    age_hours: float
    cashtag: bool  # True wenn mit $ geschrieben


@dataclass
class TickerAggregate:
    ticker: str
    mentions: int = 0
    cashtag_mentions: int = 0
    unique_posts: int = 0
    upvote_sum: int = 0
    subreddits: dict[str, int] = field(default_factory=dict)
    velocity_score: float = 0.0
    sample_posts: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Universe-Loader
# ---------------------------------------------------------------------------
def load_target_universe(csv_path: Path = UNIVERSE_CSV,
                         tiers: tuple[str, ...] = ("A", "B")) -> set[str]:
    """Laedt Ticker aus universe.csv, die in den gewuenschten Tiers liegen
    UND Reddit-kompatibel sind (US-style 1-5 Uppercase, kein Punkt/Komma/=/^).

    EU-Ticker wie NESN.SW werden ausgefiltert: Reddit-User nennen diese
    Assets meist nicht bei ihrem Exchange-Suffix, und eine Suche nach
    "NESN" allein waere zu noisy.
    """
    if not csv_path.exists():
        return set()
    out: set[str] = set()
    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            tier = (row.get("tier") or "").strip()
            tick = (row.get("ticker") or "").strip()
            if not tick or tier not in tiers:
                continue
            # Nur reine Uppercase-Ticker ohne Exchange-Suffix
            if not re.fullmatch(r"[A-Z]{1,5}", tick):
                continue
            out.add(tick)
    return out


# ---------------------------------------------------------------------------
# Ticker-Extraktion
# ---------------------------------------------------------------------------
def extract_tickers(text: str,
                    universe: set[str],
                    blacklist: frozenset[str] = HARD_BLACKLIST) -> list[tuple[str, bool]]:
    """Extrahiert Ticker aus Text. Rueckgabe: [(ticker, is_cashtag), ...]

    Strategy:
    1. Cashtags ($NVDA) sind starker Signal: immer akzeptieren, wenn im
       Universe. Hard-Blacklist zieht trotzdem (z.B. $A ist Rauschen).
    2. Plain uppercase Woerter werden nur akzeptiert, wenn sie im Universe
       UND nicht in der Hard-Blacklist sind.
    """
    if not text:
        return []
    found: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()

    for m in CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if t in blacklist or t not in universe:
            continue
        key = (t, True)
        if key not in seen:
            seen.add(key)
            found.append(key)

    # Plain-Ticker-Pass. Entferne $-prefixte Occurrences vorher, damit
    # sie nicht doppelt gezaehlt werden.
    cleaned = CASHTAG_RE.sub(" ", text)
    for m in PLAIN_TICKER_RE.finditer(cleaned):
        t = m.group(1).upper()
        if t in blacklist or t not in universe:
            continue
        key = (t, False)
        cashtag_key = (t, True)
        if key in seen or cashtag_key in seen:
            continue
        seen.add(key)
        found.append(key)

    return found


# ---------------------------------------------------------------------------
# Reddit-Fetcher: PRAW + Public-JSON-Fallback
# ---------------------------------------------------------------------------
def _try_import_praw():
    try:
        import praw  # type: ignore
        return praw
    except ImportError:
        return None


def _has_praw_credentials() -> bool:
    return all(os.environ.get(k) for k in
               ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"))


def fetch_via_praw(subreddits: Iterable[str],
                   lookback_hours: int,
                   max_posts: int,
                   max_comments: int,
                   now_utc: datetime) -> list[dict]:
    """Fetch via PRAW. Raises RuntimeError wenn PRAW/Credentials fehlen."""
    praw = _try_import_praw()
    if praw is None:
        raise RuntimeError("praw not installed")
    if not _has_praw_credentials():
        raise RuntimeError("REDDIT_CLIENT_ID/SECRET env vars missing")

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=_user_agent_default(),
    )
    reddit.read_only = True
    cutoff_ts = (now_utc - timedelta(hours=lookback_hours)).timestamp()
    out: list[dict] = []
    for sub in subreddits:
        try:
            for post in reddit.subreddit(sub).new(limit=max_posts):
                if post.created_utc < cutoff_ts:
                    continue
                comments: list[dict] = []
                try:
                    post.comments.replace_more(limit=0)
                    for c in post.comments[:max_comments]:
                        if getattr(c, "body", None):
                            comments.append({
                                "body": c.body,
                                "ups": int(getattr(c, "score", 0)),
                            })
                except Exception:
                    pass
                out.append({
                    "subreddit": sub,
                    "id": str(post.id),
                    "title": post.title or "",
                    "selftext": getattr(post, "selftext", "") or "",
                    "url": f"https://reddit.com{post.permalink}",
                    "created_utc": float(post.created_utc),
                    "ups": int(getattr(post, "score", 0)),
                    "comments": comments,
                })
        except Exception as exc:  # pragma: no cover
            print(f"[reddit_trending] PRAW-Fehler r/{sub}: {exc}", file=sys.stderr)
    return out


def fetch_via_public_json(subreddits: Iterable[str],
                          lookback_hours: int,
                          max_posts: int,
                          max_comments: int,
                          now_utc: datetime,
                          user_agent: str = DEFAULT_USER_AGENT,
                          sleep_s: float = 1.0) -> list[dict]:
    """Fetch via Reddit's oeffentliche .json-Endpoints (kein Auth, rate-limitiert).

    Robuste Fehlerbehandlung: einzelne Subreddit-/Post-Fehler unterbrechen
    den Lauf nicht. Rueckgabe ist immer ein Listen-Slice (ggf. leer).
    """
    try:
        import requests
    except ImportError:
        raise RuntimeError("requests nicht installiert; "
                           "pip install requests --break-system-packages")

    cutoff_ts = (now_utc - timedelta(hours=lookback_hours)).timestamp()
    headers = {"User-Agent": user_agent}
    out: list[dict] = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/new.json?limit={min(max_posts, 100)}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except Exception as exc:
            print(f"[reddit_trending] GET r/{sub} fail: {exc}", file=sys.stderr)
            continue
        if resp.status_code != 200:
            print(f"[reddit_trending] r/{sub} status={resp.status_code}", file=sys.stderr)
            time.sleep(sleep_s)
            continue
        try:
            data = resp.json()
        except Exception as exc:
            print(f"[reddit_trending] r/{sub} JSON-Parse-Fehler: {exc}", file=sys.stderr)
            continue
        children = data.get("data", {}).get("children", [])
        for ch in children:
            pd = ch.get("data", {})
            if not pd:
                continue
            if float(pd.get("created_utc", 0)) < cutoff_ts:
                continue
            post_id = pd.get("id", "")
            comments: list[dict] = []
            if post_id and max_comments > 0:
                curl = f"https://www.reddit.com/r/{sub}/comments/{post_id}.json?limit={max_comments}&sort=top"
                try:
                    cresp = requests.get(curl, headers=headers, timeout=15)
                    if cresp.status_code == 200:
                        cjson = cresp.json()
                        if isinstance(cjson, list) and len(cjson) > 1:
                            for cc in cjson[1].get("data", {}).get("children", []):
                                cd = cc.get("data", {})
                                body = cd.get("body")
                                if body:
                                    comments.append({
                                        "body": body,
                                        "ups": int(cd.get("ups", 0) or 0),
                                    })
                                if len(comments) >= max_comments:
                                    break
                    time.sleep(sleep_s)
                except Exception:
                    pass
            out.append({
                "subreddit": sub,
                "id": post_id,
                "title": pd.get("title", "") or "",
                "selftext": pd.get("selftext", "") or "",
                "url": "https://reddit.com" + (pd.get("permalink") or ""),
                "created_utc": float(pd.get("created_utc", 0)),
                "ups": int(pd.get("ups", 0) or 0),
                "comments": comments,
            })
        time.sleep(sleep_s)
    return out


# ---------------------------------------------------------------------------
# Aggregation + Velocity-Score
# ---------------------------------------------------------------------------
def aggregate_mentions(posts: list[dict],
                       universe: set[str],
                       now_utc: datetime,
                       lookback_hours: int,
                       max_sample_posts: int = 3) -> dict[str, TickerAggregate]:
    """Baut Ticker-Aggregate aus Post-Liste. Velocity-Score = mentions_pro_stunde *
    (1 + cashtag_anteil) * log(1 + upvotes_mean)."""
    import math
    aggregates: dict[str, TickerAggregate] = {}
    seen_post_ticker: set[tuple[str, str]] = set()

    for post in posts:
        age_h = max(0.0, (now_utc.timestamp() - post.get("created_utc", 0)) / 3600.0)
        sub = post.get("subreddit", "")
        post_id = post.get("id", "")
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        url = post.get("url", "")
        ups = int(post.get("ups", 0) or 0)

        for source_name, text in (("title", title), ("selftext", selftext)):
            for ticker, is_cashtag in extract_tickers(text, universe):
                agg = aggregates.setdefault(ticker, TickerAggregate(ticker=ticker))
                agg.mentions += 1
                if is_cashtag:
                    agg.cashtag_mentions += 1
                pair = (ticker, post_id)
                if pair not in seen_post_ticker:
                    seen_post_ticker.add(pair)
                    agg.unique_posts += 1
                    agg.upvote_sum += ups
                    agg.subreddits[sub] = agg.subreddits.get(sub, 0) + 1
                    if len(agg.sample_posts) < max_sample_posts:
                        agg.sample_posts.append({
                            "subreddit": sub,
                            "title": title[:140],
                            "url": url,
                            "ups": ups,
                            "age_hours": round(age_h, 1),
                        })

        # Top-Level-Kommentare (zaehlen schwaecher, kein eigener "unique_posts"-Bump)
        for c in post.get("comments", []):
            body = c.get("body", "")
            cups = int(c.get("ups", 0) or 0)
            for ticker, is_cashtag in extract_tickers(body, universe):
                agg = aggregates.setdefault(ticker, TickerAggregate(ticker=ticker))
                agg.mentions += 1
                if is_cashtag:
                    agg.cashtag_mentions += 1
                # Comment-Upvotes halb gewichtet
                agg.upvote_sum += cups // 2

    # Velocity-Score: mentions_pro_stunde * (1 + 0.5*cashtag_ratio) * log(1+mean_upvotes)
    for agg in aggregates.values():
        mentions_per_hour = agg.mentions / max(lookback_hours, 1)
        cashtag_ratio = agg.cashtag_mentions / max(agg.mentions, 1)
        mean_upvotes = agg.upvote_sum / max(agg.unique_posts, 1)
        agg.velocity_score = round(
            mentions_per_hour * (1.0 + 0.5 * cashtag_ratio) * math.log1p(max(mean_upvotes, 0)),
            4,
        )
    return aggregates


def top_n_summary(aggregates: dict[str, TickerAggregate], n: int = 20) -> list[dict]:
    ranked = sorted(
        aggregates.values(),
        key=lambda a: (a.velocity_score, a.mentions, a.unique_posts),
        reverse=True,
    )[:n]
    return [asdict(a) for a in ranked]


# ---------------------------------------------------------------------------
# Output + Briefing-Hook
# ---------------------------------------------------------------------------
def write_outputs(top_list: list[dict],
                  all_aggregates: dict[str, TickerAggregate],
                  as_of: datetime,
                  subreddits: tuple[str, ...],
                  lookback_hours: int,
                  source_mode: str,
                  n_posts: int,
                  shadow_dir: Path = SHADOW_DIR,
                  briefings_dir: Path = BRIEFINGS_DIR) -> tuple[Path, Path, Path]:
    shadow_dir.mkdir(parents=True, exist_ok=True)
    briefings_dir.mkdir(parents=True, exist_ok=True)
    date_str = as_of.strftime("%Y-%m-%d")
    out_path = shadow_dir / f"reddit_trending_{date_str}.json"
    payload = {
        "as_of": date_str,
        "generated_at_utc": as_of.astimezone(timezone.utc).isoformat(),
        "subreddits": list(subreddits),
        "lookback_hours": lookback_hours,
        "source_mode": source_mode,
        "post_count": n_posts,
        "universe_size": None,  # wird unten gesetzt
        "top_n": len(top_list),
        "top_tickers": top_list,
        "total_tickers_seen": len(all_aggregates),
        "schema_version": SCHEMA_VERSION,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    # Briefing-Einzeile: "Retail-Trending heute: $NVDA (+312%), …"
    briefing_line = _format_briefing_line(top_list[:5], date_str)
    briefing_path = briefings_dir / f"reddit_trending_line_{date_str}.md"
    briefing_path.write_text(briefing_line + "\n")
    (briefings_dir / "reddit_trending_line_latest.md").write_text(briefing_line + "\n")

    # Scanner-Annotation-Sidecar: SCAN-IMMUTABILITY-1-konform. Kein Mutieren
    # der Scan-CSVs. Dashboard / Empfehlung / Backtest koennen ueber Ticker
    # joinen und die Shadow-Spalte "retail_mention_rank" einlesen.
    annot_path = shadow_dir / f"retail_annotation_{date_str}.json"
    annotation = {
        "as_of": date_str,
        "generated_at_utc": as_of.astimezone(timezone.utc).isoformat(),
        "source": "reddit_trending",
        "source_mode": source_mode,
        "schema_version": SCHEMA_VERSION,
        "note": "SHADOW-ONLY Annotation. Kein Input fuer Live-Scoring.",
        "tickers": {
            t["ticker"]: {
                "retail_mention_rank": i + 1,
                "retail_velocity_score": t.get("velocity_score", 0.0),
                "retail_mentions": t.get("mentions", 0),
                "retail_cashtag_mentions": t.get("cashtag_mentions", 0),
                "retail_unique_posts": t.get("unique_posts", 0),
                "retail_subreddits": list(t.get("subreddits", {}).keys()),
            }
            for i, t in enumerate(top_list)
        },
    }
    annot_path.write_text(json.dumps(annotation, indent=2, ensure_ascii=False))
    # latest-Mirror fuer das Dashboard
    (shadow_dir / "retail_annotation_latest.json").write_text(
        json.dumps(annotation, indent=2, ensure_ascii=False)
    )
    return out_path, briefing_path, annot_path


def _format_briefing_line(top5: list[dict], date_str: str) -> str:
    if not top5:
        return (f"**Retail-Trending ({date_str}):** keine Mentions aus Universe "
                "Tier A/B in den letzten 24h erfasst (Shadow-Stub)")
    parts = []
    for item in top5:
        mentions = item.get("mentions", 0)
        ticker = item.get("ticker")
        score = item.get("velocity_score", 0.0)
        parts.append(f"${ticker} ({mentions}x, v={score})")
    return f"**Retail-Trending ({date_str}, Q1-Shadow):** " + ", ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reddit/WSB-Trending-Shadow-Scanner (Q1).")
    p.add_argument("--subreddits", default=",".join(DEFAULT_SUBREDDITS),
                   help="Kommagetrennt. Default: wallstreetbets,stocks,investing,options")
    p.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS_PER_SUB)
    p.add_argument("--max-comments", type=int, default=DEFAULT_MAX_COMMENTS_PER_POST)
    p.add_argument("--universe-csv", type=Path, default=UNIVERSE_CSV)
    p.add_argument("--output-dir", type=Path, default=SHADOW_DIR)
    p.add_argument("--briefings-dir", type=Path, default=BRIEFINGS_DIR)
    p.add_argument("--user-agent", default=_user_agent_default(),
                   help="HTTP User-Agent (default: $REDDIT_USER_AGENT, else a neutral placeholder)")
    p.add_argument("--prefer-praw", action="store_true",
                   help="Versucht PRAW zuerst und faellt bei Fehler auf Public-JSON zurueck.")
    p.add_argument("--as-of", default=None,
                   help="Override-Datum YYYY-MM-DD (Tests/Replay).")
    p.add_argument("--json-only", action="store_true",
                   help="Gibt Top-N JSON zusaetzlich auf stdout aus.")
    p.add_argument("--test", action="store_true",
                   help="Self-Test ohne Netzwerk (nutzt Fixtures).")
    args = p.parse_args(argv)

    if args.test:
        return _run_self_test()

    subreddits = tuple(s.strip() for s in args.subreddits.split(",") if s.strip())
    universe = load_target_universe(args.universe_csv)
    if not universe:
        print("[reddit_trending] ERROR universe leer: Abbruch", file=sys.stderr)
        return 2

    now_utc = datetime.now(timezone.utc)
    if args.as_of:
        # as_of interpretiert als Mitternacht-UTC des angegebenen Tages
        now_utc = datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc)

    posts: list[dict] = []
    source_mode = "public_json"
    tried_praw = False
    if args.prefer_praw and _try_import_praw() and _has_praw_credentials():
        tried_praw = True
        try:
            posts = fetch_via_praw(subreddits, args.lookback_hours,
                                   args.max_posts, args.max_comments, now_utc)
            source_mode = "praw"
        except Exception as exc:
            print(f"[reddit_trending] PRAW fail, falling back: {exc}", file=sys.stderr)
            posts = []
    if not posts:
        posts = fetch_via_public_json(
            subreddits, args.lookback_hours, args.max_posts,
            args.max_comments, now_utc, user_agent=args.user_agent,
        )
        if not tried_praw:
            source_mode = "public_json"
        else:
            source_mode = "public_json_after_praw_fail"

    aggregates = aggregate_mentions(posts, universe, now_utc, args.lookback_hours)
    top = top_n_summary(aggregates, args.top_n)

    out_path, briefing_path, annot_path = write_outputs(
        top, aggregates, now_utc, subreddits, args.lookback_hours,
        source_mode, len(posts), args.output_dir, args.briefings_dir,
    )

    # universe_size im JSON nachsetzen
    try:
        data = json.loads(out_path.read_text())
        data["universe_size"] = len(universe)
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        pass

    summary = (f"[reddit_trending] {now_utc.strftime('%Y-%m-%d %H:%M')} "
               f"posts={len(posts)} universe={len(universe)} "
               f"top={len(top)} source={source_mode}")
    print(summary, file=sys.stderr)

    if args.json_only:
        print(json.dumps(top, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Self-Test (netz-frei)
# ---------------------------------------------------------------------------
def _run_self_test() -> int:
    """Minimal-Self-Test ohne Netzwerk. Nutzt Fixtures + Universe-Stub."""
    universe = {"NVDA", "TSLA", "AAPL", "AMD", "SPY"}
    posts = [
        {
            "subreddit": "wallstreetbets",
            "id": "abc1",
            "title": "$NVDA to the moon, also watching AMD",
            "selftext": "TSLA looks weak here, might short.",
            "url": "https://reddit.com/r/wallstreetbets/abc1",
            "created_utc": (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp(),
            "ups": 1200,
            "comments": [
                {"body": "NVDA calls printing", "ups": 50},
                {"body": "SPY 500p gang", "ups": 20},
            ],
        },
        {
            "subreddit": "stocks",
            "id": "abc2",
            "title": "Thoughts on AAPL earnings next week?",
            "selftext": "",
            "url": "https://reddit.com/r/stocks/abc2",
            "created_utc": (datetime.now(timezone.utc) - timedelta(hours=5)).timestamp(),
            "ups": 340,
            "comments": [],
        },
    ]
    now = datetime.now(timezone.utc)
    agg = aggregate_mentions(posts, universe, now, 24)
    top = top_n_summary(agg, 20)
    assert len(top) >= 3, f"expected >=3 tickers, got {len(top)}: {top}"
    assert top[0]["velocity_score"] > 0, f"score should be positive: {top[0]}"
    assert any(t["ticker"] == "NVDA" for t in top), "NVDA must appear"
    print(f"[reddit_trending] self-test OK · top={[t['ticker'] for t in top]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
