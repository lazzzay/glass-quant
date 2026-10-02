#!/usr/bin/env python3
"""
Hebel A: FinBERT-Local-Sentiment-Scorer (REC-RECOVERY-1 Mega-Audit Tier-2).

HuggingFace ProsusAI/finbert lokal, ~440MB Modell, kein API-Call.
Erweitert tools/news_sentiment_fetch.py um echtes NLP-Sentiment statt nur Keyword-Counter.

Vorteil gegenüber GDELT-Keyword-Counter:
- "Apple beats but guidance cut" → korrekt als bearish klassifiziert (Keyword: würde "beats"=bullish + "cut"=bearish saldieren auf 0)
- Versteht Negation: "no surprise" → neutral statt missverstanden
- Versteht Finanz-Kontext: "miss" vs "beat", "raises" vs "cuts"

Output: pro Headline {label: positive/negative/neutral, score: 0.0..1.0}
Aggregation pro Ticker: bullish_share - bearish_share = -1.0..+1.0
Im daily_scanner: news_adjust = sentiment × 0.10 (max ±0.10), höher als GDELT ±0.05 wegen besserer Qualität.

CLI:
    python3 tools/finbert_scorer.py --selftest          # 5-Headline-Test
    python3 tools/finbert_scorer.py --headlines "..."   # ad-hoc

Voraussetzungen (auf User-Mac einmalig):
    python3 -m pip install --user transformers torch
    Model lädt beim ersten Run ~440MB von HuggingFace.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"

# Cache-Pfad für Modell: vermeidet erneutes Download bei jedem Run
MODEL_NAME = "ProsusAI/finbert"
MODEL_CACHE = REPO_ROOT / ".finbert_cache"


_PIPELINE = None  # Lazy-Loading
_LOAD_FAILED = False  # a failed load is not retried within one process (no per-ticker retries)


def _get_pipeline():
    """Lädt FinBERT-Pipeline einmalig (Lazy-Loading)."""
    global _PIPELINE, _LOAD_FAILED
    if _PIPELINE is None and _LOAD_FAILED:
        return None
    if _PIPELINE is None:
        try:
            from transformers import pipeline
            _PIPELINE = pipeline(
                "sentiment-analysis",
                model=MODEL_NAME,
                tokenizer=MODEL_NAME,
                # truncation=True um lange Texte zu kürzen
                truncation=True,
                max_length=512,
            )
        except ImportError:
            _LOAD_FAILED = True
            print("[FinBERT] transformers nicht installiert. Run: pip install --user transformers torch", file=sys.stderr)
            return None
        except Exception as e:
            _LOAD_FAILED = True
            print(f"[FinBERT] Model-Load-Error: {e}", file=sys.stderr)
            return None
    return _PIPELINE


def score_headline(text: str) -> dict | None:
    """Klassifiziert eine Headline. Returns {label, score} oder None bei Failure."""
    pipe = _get_pipeline()
    if pipe is None or not text:
        return None
    try:
        result = pipe(text)
        if result and len(result) > 0:
            return {
                "label": result[0]["label"].lower(),  # positive/negative/neutral
                "score": float(result[0]["score"]),    # 0.0..1.0 Konfidenz
            }
    except Exception as e:
        print(f"[FinBERT] Score-Error für '{text[:50]}...': {e}", file=sys.stderr)
    return None


def score_articles(articles: list[dict]) -> float:
    """Aggregiert FinBERT-Scores pro Article-Liste zu einem Ticker-Sentiment-Score.

    Returns: float in [-1.0, +1.0]
        +1.0 = alle articles bullish
        -1.0 = alle articles bearish
         0.0 = neutral / keine Daten
    """
    if not articles:
        return 0.0
    pipe = _get_pipeline()
    if pipe is None:
        # Fallback: keine FinBERT verfügbar
        return 0.0

    bullish_weighted = 0.0
    bearish_weighted = 0.0
    total_weight = 0

    for a in articles:
        title = (a.get("title") or "").strip()
        if not title or len(title) < 10:  # zu kurze Titles skippen
            continue
        result = score_headline(title)
        if not result:
            continue
        # Confidence-gewichtet aggregieren
        confidence = result["score"]
        label = result["label"]
        if label == "positive" and confidence > 0.5:
            bullish_weighted += confidence
            total_weight += 1
        elif label == "negative" and confidence > 0.5:
            bearish_weighted += confidence
            total_weight += 1
        else:
            total_weight += 1  # neutral oder low-confidence

    if total_weight == 0:
        return 0.0
    return round((bullish_weighted - bearish_weighted) / total_weight, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="5-Headline-Test (lädt Model beim ersten Run, ~440MB)")
    ap.add_argument("--headlines", default=None,
                    help="Pipe-separated headlines: 'h1|h2|h3'")
    args = ap.parse_args()

    if args.selftest:
        test_headlines = [
            "Apple beats Q3 estimates, revenue up 8%",
            "Meta plummets 7% on capex repricing fears",
            "Microsoft announces $80B AI infrastructure investment",
            "Tesla recalls 50,000 vehicles over software glitch",
            "Goldman raises S&P target to 7,200, cites strong earnings",
        ]
        print("=== FinBERT Selftest ===\n")
        for h in test_headlines:
            r = score_headline(h)
            if r:
                print(f"  '{h[:60]}...' → {r['label']:<10} ({r['score']:.3f})")
            else:
                print(f"  '{h[:60]}...' → FAILED")

        # Aggregat-Test
        articles = [{"title": h} for h in test_headlines]
        agg = score_articles(articles)
        print(f"\nAggregat-Sentiment: {agg:+.3f}")
        return

    if args.headlines:
        for h in args.headlines.split("|"):
            r = score_headline(h.strip())
            if r:
                print(f"{h.strip()[:60]}: {r['label']} ({r['score']:.3f})")


if __name__ == "__main__":
    main()
