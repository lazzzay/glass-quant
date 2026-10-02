# UNIVERSE-TRADABLE-1 (2026-06-10): EU retail broker tradability (PRIIPs/KID) im Scanner.
# Offline-faehig: Kern-Tests brauchen weder Netz noch Scan-Artefakte.
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from daily_scanner import (  # noqa: E402
    ASIA_LOCAL_SUFFIXES,
    ETF_ASSET_CLASSES,
    HEBEL_WATCH_MIN_SCORE,
    PERSONAL_BLACKLIST,
    tradability,
)


# ---------- Kern-Klassifikation ----------

@pytest.mark.parametrize("ticker,asset_class,expected", [
    # Futures -> hebel_only (Suffix UND asset_class greifen unabhaengig)
    ("ZS=F", "commodity", "hebel_only"),
    ("CL=F", "commodity", "hebel_only"),
    ("GC=F", "", "hebel_only"),               # Suffix allein reicht
    ("XYZ", "commodity", "hebel_only"),       # Klasse allein reicht
    # US-ETFs ohne Suffix (kein PRIIPs-KID) -> hebel_only
    ("SPY", "etf_broad", "hebel_only"),
    ("QQQ", "etf_broad", "hebel_only"),
    ("DBA", "etf_commodity", "hebel_only"),
    ("LIT", "etf_thematic", "hebel_only"),
    ("SOYB", "etf_commodity", "hebel_only"),
    ("XLE", "etf_sector", "hebel_only"),
    ("TQQQ", "etf_thematic", "hebel_only"),
    # UCITS-ETF mit Boersen-Suffix bliebe direct (future-proof)
    ("EUNL.DE", "etf_broad", "direct"),
    ("CSPX.L", "etf_broad", "direct"),
    # Asia-Lokallistings -> hebel_only
    ("0700.HK", "asia_mega", "hebel_only"),
    ("2330.TW", "asia_large", "hebel_only"),
    ("005930.KS", "asia_large", "hebel_only"),
    ("7203.T", "asia_large", "hebel_only"),
    ("600519.SS", "asia_large", "hebel_only"),
    ("000001.SZ", "asia_mid", "hebel_only"),
    # Normale Aktien -> direct
    ("AAPL", "us_large", "direct"),
    ("HON", "us_large", "direct"),
    ("PFE", "us_large", "direct"),
    ("SAP.DE", "dax", "direct"),
    ("S92.DE", "mdax", "direct"),
    ("NESN.SW", "eu_large", "direct"),
    ("AIR.PA", "eu_large", "direct"),
    # ADRs (US-Listing) -> direct
    ("TSM", "asia_adr", "direct"),
    ("SONY", "asia_adr", "direct"),
    # Krypto (crypto spot) -> direct
    ("BTC-USD", "crypto", "direct"),
    ("SOL-USD", "crypto", "direct"),
])
def test_tradability_classification(ticker, asset_class, expected):
    assert tradability(ticker, asset_class) == expected


def test_tradability_robust_inputs():
    # Defensive: None/leer/Casing duerfen nicht crashen und defaulten zu direct
    assert tradability("", "") == "direct"
    assert tradability("aapl", "US_LARGE") == "direct"
    assert tradability("zs=f", "Commodity") == "hebel_only"
    assert tradability("spy", "ETF_BROAD") == "hebel_only"
    assert tradability("0700.hk", "") == "hebel_only"


def test_suffix_no_false_positive_tw_vs_t():
    # ".TW" darf nicht faelschlich von ".T"-Regel abhaengen und umgekehrt;
    # ein .DE-Ticker darf von keiner Asia-Regel erfasst werden.
    assert tradability("2330.TW", "asia_large") == "hebel_only"
    assert tradability("7203.T", "asia_large") == "hebel_only"
    assert tradability("DTE.DE", "dax") == "direct"
    assert tradability("VOW3.DE", "dax") == "direct"


def test_constants_sane():
    assert HEBEL_WATCH_MIN_SCORE == 8.0
    assert ".HK" in ASIA_LOCAL_SUFFIXES and ".T" in ASIA_LOCAL_SUFFIXES
    assert "etf_broad" in ETF_ASSET_CLASSES
    # Blacklist und Tradability sind getrennte Mechanismen; die Blacklist ist
    # user-konfigurierbar (config/blacklist.txt) und per Default leer.
    assert isinstance(PERSONAL_BLACKLIST, (set, frozenset))


# ---------- Split-Semantik (synthetisch, wie im Scanner-Main) ----------

# Lokale, synthetische Ausschlussliste (unabhaengig von der User-Config).
_TEST_BLACKLIST = {"EEE"}


def _split(rows, top_n=25, blacklist=frozenset(_TEST_BLACKLIST)):
    for r in rows:
        r["tradability"] = tradability(r.get("ticker", ""), r.get("asset_class", ""))
    clean = [r for r in rows if r["ticker"] not in blacklist]
    top = [r for r in clean if r["tradability"] == "direct"][:top_n]
    watch = [r for r in clean if r["tradability"] == "hebel_only"
             and float(r.get("pre_score") or 0) >= HEBEL_WATCH_MIN_SCORE]
    return top, watch


def test_split_kw24_signatur():
    # Nachbau der 09.06.-Signatur: Rohstoff-Wand oben, Aktien dahinter.
    rows = [
        {"ticker": "ZS=F", "asset_class": "commodity", "pre_score": 10.1},
        {"ticker": "DBA", "asset_class": "etf_commodity", "pre_score": 10.0},
        {"ticker": "LIT", "asset_class": "etf_thematic", "pre_score": 9.63},
        {"ticker": "SOYB", "asset_class": "etf_commodity", "pre_score": 9.48},
        {"ticker": "HON", "asset_class": "us_large", "pre_score": 8.77},
        {"ticker": "CT=F", "asset_class": "commodity", "pre_score": 8.62},
        {"ticker": "S92.DE", "asset_class": "mdax", "pre_score": 8.4},
        {"ticker": "PLUG", "asset_class": "us_small", "pre_score": 7.93},
        {"ticker": "EEE", "asset_class": "us_large", "pre_score": 9.9},  # Blacklist (synthetic)
    ]
    top, watch = _split(rows, top_n=3)
    assert [r["ticker"] for r in top] == ["HON", "S92.DE", "PLUG"]
    # Hebel-Watch: nur hebel_only >= 8.0, Blacklist greift weiterhin zuerst
    assert [r["ticker"] for r in watch] == ["ZS=F", "DBA", "LIT", "SOYB", "CT=F"]
    assert all(r["tradability"] == "hebel_only" for r in watch)


def test_split_low_score_nondirect_falls_through():
    rows = [
        {"ticker": "WEAT", "asset_class": "etf_commodity", "pre_score": 7.9},
        {"ticker": "MRK", "asset_class": "us_large", "pre_score": 6.0},
    ]
    top, watch = _split(rows)
    assert [r["ticker"] for r in top] == ["MRK"]
    assert watch == []  # 7.9 < 8.0 -> kein Hebel-Watch-Slot


# ---------- Real-Daten-Regression (skip wenn Artefakt fehlt) ----------

def test_against_universe_csv():
    p = ROOT / "tools" / "universe.csv"
    if not p.exists():
        pytest.skip("universe.csv fehlt")
    import csv as _csv
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) >= 600
    counts = {"direct": 0, "hebel_only": 0}
    for r in rows:
        counts[tradability(r["ticker"], r["asset_class"])] += 1
    # 16 Futures + 74 US-ETFs + 29 Asia-Lokal = 119 (Stand 10.06.; toleranter Korridor)
    assert 110 <= counts["hebel_only"] <= 130, counts
    assert counts["direct"] >= 500
