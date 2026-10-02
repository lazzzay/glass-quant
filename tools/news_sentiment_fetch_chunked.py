#!/usr/bin/env python3
"""Chunked-Wrapper für news_sentiment_fetch.py: fit für 45s-Bash-Sandbox.

Usage:
    python3 tools/news_sentiment_fetch_chunked.py --tier A,B --delay 0.1 --batch 40
    # mehrfach aufrufen: Resume aus partial-Datei
    python3 tools/news_sentiment_fetch_chunked.py --tier A,B --finalize  # finalisiere wenn alle done

Persistiert Zwischenzustand in tools/news_sentiment_YYYY-MM-DD.partial.json bis fertig,
dann atomic rename auf news_sentiment_YYYY-MM-DD.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_sentiment_fetch import (  # type: ignore
    fetch_gdelt_articles,
    score_articles_findpo,
    score_articles_keyword,
    load_universe_tickers,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"


def process_one_ticker(t: str, use_llm: bool = True) -> dict:
    articles = fetch_gdelt_articles(t, lookback_days=3, max_articles=30)
    sentiment = None
    method = "keyword"
    model_used = None
    if use_llm and articles:
        sentiment, model_used = score_articles_findpo(articles)
        if sentiment is not None and model_used:
            if "findpo" in (model_used or "").lower():
                method = "findpo"
            else:
                method = "finbert"
    if sentiment is None:
        sentiment = score_articles_keyword(articles)
        method = "keyword"
    top = [
        {"title": a.get("title"), "url": a.get("url"), "domain": a.get("domain")}
        for a in articles[:5]
    ]
    return {
        "article_count": len(articles),
        "sentiment_score": round(sentiment, 3),
        "sentiment_method": method,
        "sentiment_model": model_used,
        "top_headlines": top,
    }


def finalize(partial_path: Path, final_path: Path, scan_date: str, universe_size: int):
    with open(partial_path, encoding="utf-8") as f:
        state = json.load(f)
    results = state["ticker_map"]
    bullish = sum(1 for r in results.values() if r["sentiment_score"] > 0.1)
    bearish = sum(1 for r in results.values() if r["sentiment_score"] < -0.1)
    neutral = len(results) - bullish - bearish
    no_news = sum(1 for r in results.values() if r["article_count"] == 0)
    out = {
        "date": scan_date,
        "version": "S5-GDELT-v1-chunked",
        "n_universe": universe_size,
        "n_with_news": len(results) - no_news,
        "n_bullish": bullish,
        "n_bearish": bearish,
        "n_neutral": neutral,
        "source": "gdelt_doc_2.0",
        "ticker_map": results,
    }
    tmp = final_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, final_path)
    print(f"[FINALIZED] {final_path}")
    print(f"  n_universe={universe_size} n_with_news={out['n_with_news']} bullish={bullish} bearish={bearish} neutral={neutral}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="A,B")
    ap.add_argument("--delay", type=float, default=0.1)
    ap.add_argument("--batch", type=int, default=40, help="Max tickers per call")
    ap.add_argument("--time-budget", type=float, default=38.0, help="Wall-clock budget (s)")
    ap.add_argument("--finalize", action="store_true")
    args = ap.parse_args()

    scan_date = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    partial_path = TOOLS_DIR / f"news_sentiment_{scan_date}.partial.json"
    final_path = TOOLS_DIR / f"news_sentiment_{scan_date}.json"

    universe = load_universe_tickers(tier_filter=args.tier)
    print(f"[CHUNKED] Universe: {len(universe)} tickers (tier {args.tier})")

    # Load or init state
    if partial_path.exists():
        with open(partial_path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {"ticker_map": {}, "started": scan_date}

    done = set(state["ticker_map"].keys())
    todo = [t for t in universe if t not in done]
    print(f"[CHUNKED] Already done: {len(done)}, remaining: {len(todo)}")

    if args.finalize or not todo:
        if not todo:
            print("[CHUNKED] All done, finalizing.")
            finalize(partial_path, final_path, scan_date, len(universe))
        else:
            print(f"[CHUNKED] --finalize requested but {len(todo)} tickers remain. Aborting.")
            sys.exit(2)
        return

    # Process batch with time-budget guard
    start = time.time()
    processed = 0
    for t in todo[: args.batch]:
        if time.time() - start > args.time_budget:
            print(f"[CHUNKED] Time-budget hit at {processed} tickers, saving partial.")
            break
        try:
            state["ticker_map"][t] = process_one_ticker(t)
        except Exception as e:
            print(f"  [WARN] {t}: {type(e).__name__}: {str(e)[:60]}")
            state["ticker_map"][t] = {
                "article_count": 0,
                "sentiment_score": 0.0,
                "sentiment_method": "keyword",
                "sentiment_model": None,
                "top_headlines": [],
            }
        processed += 1
        if args.delay > 0:
            time.sleep(args.delay)

    # Save partial
    tmp = partial_path.with_suffix(".partial.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, partial_path)

    elapsed = time.time() - start
    n_done = len(state["ticker_map"])
    print(f"[CHUNKED] Processed {processed} this batch in {elapsed:.1f}s. Total done: {n_done}/{len(universe)}.")

    if n_done >= len(universe):
        finalize(partial_path, final_path, scan_date, len(universe))


if __name__ == "__main__":
    main()
