#!/usr/bin/env python3
"""
R1: FinDPO-Local-Sentiment-Scorer (REC-RECOVERY-1 Mega-Research-2026-05-19).

DPO-trainierter Finance-Sentiment-Classifier. Replacement für FinBERT (ProsusAI/finbert).

Quellen-Belege:
- arXiv 2507.18417 (Juli 2025): see the paper for the authors' reported
  classification and backtest results (not verified here, no performance claim)
- Free-Stack: HuggingFace, vollständig lokal, kein API-Call

Model-Selection (Fallback-Kaskade):
1. FINDPO_MODEL env-var (User-Override, z.B. ein lokaler Pfad)
2. Default: AventIQ-AI/FinDPO (oder Reservevariante)
3. Fallback bei Failure: ProsusAI/finbert (R0-Stand)

NOTE: the Hugging Face model ids listed in FINDPO_MODEL_CANDIDATES may not
exist, may be renamed or may be gated. If none loads, FinBERT is used as the
fallback. Check each model's license before use. --selftest downloads models
(network), so it is not an offline test.

CLI:
    python3 tools/findpo_scorer.py --selftest
    python3 tools/findpo_scorer.py --headlines "Apple beats but guidance cut"

Voraussetzungen auf User-Mac (einmalig):
    python3 -m pip install --user transformers torch  # falls noch nicht von FinBERT

Erster Run lädt Modell von HuggingFace (~500MB-1.5GB je nach Variant).
Danach lokaler Cache (~/.cache/huggingface/).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Model-Cascade: versucht jeden in Order, nimmt den ersten der lädt
FINDPO_MODEL_CANDIDATES = [
    os.environ.get("FINDPO_MODEL", "").strip() or None,  # User-Override hat Vorrang
    "AventIQ-AI/FinDPO",  # Wenn auf HF verfügbar (Stand Mai 2026)
    "bisaaca/FinDPO-7B",  # Paper-Reference (möglicherweise gated)
    "FinanceMTEB/FinDPO",  # Alternative-Pfad
]
# Final-Fallback wenn keiner der oben verfügbar
FINBERT_FALLBACK = "ProsusAI/finbert"

_PIPELINE = None
_MODEL_USED = None
_LOAD_FAILED = False  # a failed load is not retried within one process (no per-ticker retries)


def _get_pipeline():
    """Lädt FinDPO-Pipeline mit Fallback-Kaskade. Lazy-Loading."""
    global _PIPELINE, _MODEL_USED, _LOAD_FAILED
    if _PIPELINE is not None:
        return _PIPELINE
    if _LOAD_FAILED:
        return None

    try:
        from transformers import pipeline
    except ImportError:
        _LOAD_FAILED = True
        print("[FinDPO] transformers nicht installiert. pip install --user transformers torch", file=sys.stderr)
        return None

    for candidate in FINDPO_MODEL_CANDIDATES + [FINBERT_FALLBACK]:
        if not candidate:
            continue
        try:
            print(f"[FinDPO] Versuche Model: {candidate}", file=sys.stderr)
            _PIPELINE = pipeline(
                "sentiment-analysis",
                model=candidate,
                tokenizer=candidate,
                truncation=True,
                max_length=512,
            )
            _MODEL_USED = candidate
            print(f"[FinDPO] LOADED: {candidate}", file=sys.stderr)
            return _PIPELINE
        except Exception as e:
            print(f"[FinDPO] Failed {candidate}: {str(e)[:80]}", file=sys.stderr)
            continue

    _LOAD_FAILED = True
    print("[FinDPO] Alle Models failed, kein Sentiment-Backend verfügbar", file=sys.stderr)
    return None


def get_model_used() -> str | None:
    """Welches Model ist gerade aktiv?"""
    return _MODEL_USED


def is_findpo_active() -> bool:
    """True wenn FinDPO (nicht FinBERT-Fallback) aktiv."""
    if _MODEL_USED is None:
        _get_pipeline()
    return _MODEL_USED is not None and "FinDPO" in (_MODEL_USED or "").upper() or "findpo" in (_MODEL_USED or "").lower()


def score_headline(text: str) -> dict | None:
    """Klassifiziert eine Headline. Returns {label, score, model} oder None."""
    pipe = _get_pipeline()
    if pipe is None or not text:
        return None
    try:
        result = pipe(text)
        if result and len(result) > 0:
            label = result[0]["label"].lower()
            # Verschiedene Models geben verschiedene Label-Namen zurück
            # Normalisiere: positive/bullish → positive, negative/bearish → negative, sonst neutral
            if label in ("bullish", "positive", "label_2"):
                label = "positive"
            elif label in ("bearish", "negative", "label_0"):
                label = "negative"
            else:
                label = "neutral"
            return {
                "label": label,
                "score": float(result[0]["score"]),
                "model": _MODEL_USED,
            }
    except Exception as e:
        print(f"[FinDPO] Score-Error für '{text[:50]}...': {e}", file=sys.stderr)
    return None


def score_articles(articles: list[dict]) -> float:
    """Aggregiert Sentiment-Scores pro Article-Liste zu Ticker-Sentiment in [-1, +1]."""
    if not articles:
        return 0.0
    pipe = _get_pipeline()
    if pipe is None:
        return 0.0

    bullish_weighted = 0.0
    bearish_weighted = 0.0
    total_weight = 0

    for a in articles:
        title = (a.get("title") or "").strip()
        if not title or len(title) < 10:
            continue
        result = score_headline(title)
        if not result:
            continue
        confidence = result["score"]
        label = result["label"]
        if label == "positive" and confidence > 0.5:
            bullish_weighted += confidence
            total_weight += 1
        elif label == "negative" and confidence > 0.5:
            bearish_weighted += confidence
            total_weight += 1
        else:
            total_weight += 1

    if total_weight == 0:
        return 0.0
    return round((bullish_weighted - bearish_weighted) / total_weight, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="5-Headline-Test mit FinDPO vs FinBERT-Vergleich")
    ap.add_argument("--headlines", default=None,
                    help="Pipe-separated headlines: 'h1|h2|h3'")
    ap.add_argument("--model", default=None,
                    help="Override Model-Name (z.B. 'AventIQ-AI/FinDPO')")
    args = ap.parse_args()

    if args.model:
        os.environ["FINDPO_MODEL"] = args.model

    if args.selftest:
        # Test-Cases: speziell solche wo Keyword-Counter falsch klassifiziert
        test_headlines = [
            ("Apple beats Q3 estimates, revenue up 8%", "positive"),
            ("Meta plummets 7% on capex repricing fears", "negative"),
            ("Apple beats but guidance cut, shares fall", "negative"),  # Tricky: beats UND cut
            ("Microsoft announces $80B AI infrastructure investment", "positive"),
            ("Tesla recalls 50,000 vehicles over software glitch", "negative"),
            ("No surprise as Goldman maintains S&P target", "neutral"),  # Tricky: "no surprise"
        ]
        print("=== FinDPO Selftest (mit Tricky-Headlines) ===\n", file=sys.stderr)
        correct = 0
        total = 0
        for h, expected in test_headlines:
            r = score_headline(h)
            if r:
                ok = r["label"] == expected
                correct += 1 if ok else 0
                total += 1
                mark = "✓" if ok else "✗"
                print(f"  {mark} '{h[:55]:55}' → {r['label']:<10} ({r['score']:.3f}) [erwartet: {expected}]")
            else:
                print(f"  ?  '{h[:55]:55}' → FAILED")
        print(f"\nAccuracy auf 6 tricky test cases: {correct}/{total}", file=sys.stderr)
        print(f"Model used: {_MODEL_USED}", file=sys.stderr)

        # Aggregat-Test
        articles = [{"title": h} for h, _ in test_headlines]
        agg = score_articles(articles)
        print(f"Aggregat-Sentiment: {agg:+.3f}", file=sys.stderr)
        return

    if args.headlines:
        for h in args.headlines.split("|"):
            r = score_headline(h.strip())
            if r:
                print(f"{h.strip()[:60]}: {r['label']} ({r['score']:.3f}) [{r['model']}]")


if __name__ == "__main__":
    main()
