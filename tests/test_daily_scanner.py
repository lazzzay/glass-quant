"""Golden-Master für die V6.1-Kern-Scoring-Formel (Audit M3, 2026-06-14).

`daily_scanner.py` ist die wichtigste Datei (Pre-Score), war aber ohne dedizierten
Test: eine stille Regression der Punkte-Tabellen oder Gewichte wäre von der Suite
nicht gefangen worden. Dieser Test pinnt:
  1. `triple_rsi_score` und `connors_setup_quality` mit fixen Inputs → erwartete Punkte
     (fängt jede Änderung der Schwellen/Punkte-Tabellen),
  2. die Pre-Score-Mathematik (connors_scaled = min(q,14)/14*10; pre = 0.667*triple_rsi
     + 0.333*connors_scaled) per Quell-Assertion (fängt eine Änderung der Gewichte/Rescale).
Offline-fähig (kein Netz, keine yfinance-Importe am Modulkopf).
"""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# daily_scanner importiert yfinance am Modulkopf (re-raise ohne yfinance). In Umgebungen
# ohne yfinance (CI-Sandbox) sauber SKIPPEN statt Collection-Error; auf dem Mac laeuft's voll.
pytest.importorskip("yfinance")

from daily_scanner import triple_rsi_score, connors_setup_quality  # noqa: E402

SCANNER_SRC = (ROOT / "tools" / "daily_scanner.py").read_text(encoding="utf-8")


# ---------- triple_rsi_score: r2(<5/10/20/30) + r5(<15/25/35) + r14(<30/40/50), cap 10 ----------

def test_triple_rsi_score_golden():
    # max: 4 + 3 + 3 = 10
    assert triple_rsi_score({"rsi2": 3,  "rsi5": 12, "rsi14": 28}) == 10
    # 3 + 2 + 2 = 7
    assert triple_rsi_score({"rsi2": 8,  "rsi5": 20, "rsi14": 35}) == 7
    # 1 + 0 + 0 = 1 (r5=40 nicht <35, r14=55 nicht <50)
    assert triple_rsi_score({"rsi2": 25, "rsi5": 40, "rsi14": 55}) == 1
    # kein Edge: 0
    assert triple_rsi_score({"rsi2": 50, "rsi5": 60, "rsi14": 70}) == 0
    # Cap greift: 4 + 3 + 3 = 10 (nicht mehr)
    assert triple_rsi_score({"rsi2": 1,  "rsi5": 1,  "rsi14": 1}) == 10


# ---------- connors_setup_quality: rsi2/rsi5 + close vs sma200/sma5 + cum_rsi2_3d + pullback ----------

def _c(rsi2, rsi5, close, sma200, sma5, **extra):
    row = {"rsi2": rsi2, "rsi5": rsi5, "close": close, "sma200": sma200, "sma5": sma5}
    row.update(extra)
    return row


def test_connors_setup_quality_golden():
    # max 14: 4(rsi2<10) +3(rsi5<25) +2(close>sma200) +1(close<sma5) +2(cum<30) +2(pullback>=3)
    assert connors_setup_quality(
        _c(3, 12, 100, 90, 105, cum_rsi2_3d=20, pullback_strength_3d=3)) == 14
    # 2(rsi2<15) +1(rsi5<30) +0 +0 +1(cum<45) +1(pullback>=2) = 5
    assert connors_setup_quality(
        _c(12, 28, 50, 55, 48, cum_rsi2_3d=40, pullback_strength_3d=2)) == 5
    # nichts trifft -> 0
    assert connors_setup_quality(
        _c(50, 60, 100, 110, 95, cum_rsi2_3d=100, pullback_strength_3d=0)) == 0
    # Grenzfälle: rsi2=14<15 ->2; rsi5=29<30 ->1; close==sma200 ->0; close==sma5 ->0;
    # cum=44<45 ->1; pullback=1 (<2) ->0  => 4
    assert connors_setup_quality(
        _c(14, 29, 100, 100, 100, cum_rsi2_3d=44, pullback_strength_3d=1)) == 4
    # .get-Defaults: ohne cum_rsi2_3d/pullback_strength_3d -> keine Boni
    # 4(rsi2<10) +3(rsi5<25) +2(close>sma200) +1(close<sma5) +0 +0 = 10
    assert connors_setup_quality(_c(5, 20, 100, 90, 110)) == 10


# ---------- Pre-Score-Mathematik (Gewichte + Rescale) per Quell-Pin ----------

def test_pre_score_weights_and_rescale_unchanged():
    assert 'min(row["connors_quality"], 14) / 14.0 * 10.0' in SCANNER_SRC
    assert '0.667 * row["triple_rsi"]' in SCANNER_SRC
    assert '0.333 * connors_scaled' in SCANNER_SRC


def test_pre_score_end_to_end_formula():
    # Repliziert die Scanner-Rechnung mit den oben gepinnten Konstanten und prüft,
    # dass max-Setup -> 10.0 und ein moderates Setup -> erwarteter Wert.
    def pre_score(row):
        q = connors_setup_quality(row)
        connors_scaled = min(q, 14) / 14.0 * 10.0
        return round(0.667 * triple_rsi_score(row) + 0.333 * connors_scaled, 2)

    max_row = _c(3, 12, 100, 90, 105, rsi14=28, cum_rsi2_3d=20, pullback_strength_3d=3)
    assert pre_score(max_row) == 10.0

    mod_row = _c(12, 28, 50, 55, 48, rsi14=45, cum_rsi2_3d=40, pullback_strength_3d=2)
    # triple_rsi=4, connors=5 -> connors_scaled=3.5714.., pre=2.668+1.18928..=3.86
    assert pre_score(mod_row) == 3.86
