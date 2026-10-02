"""COVERAGE-THROTTLE-1 (2026-06-20): offline-Test fuer batch_fetch Pacing/Recovery.

Befund Scan 06-19: Yahoo rate-limitet nach ~240 Namen -> ab Chunk 240 82-92% fail
(US/Asia, weil spaeter in universe.csv) -> nur 44,6% Coverage. Dieser Test stubt
yfinance, sodass das ECHTE daily_scanner.batch_fetch ohne Netz lauffaehig ist, und
beweist, dass die adaptive Backoff-Recovery die Coverage ueber die Throttle-Grenze hebt.
"""
import importlib
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "..", "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


class _FakeTicker:
    def __init__(self, *a, **k):
        pass

    def history(self, *a, **k):
        return pd.DataFrame()


def _make_yf_stub():
    """Minimaler yfinance-Stub (nur noetig, wenn yfinance nicht installiert ist)."""
    _yf = types.ModuleType("yfinance")
    _yf.download = lambda *a, **k: None
    _yf.set_tz_cache_location = lambda *a, **k: None
    _yf.Ticker = _FakeTicker
    return _yf


def _no_cache_reset(force=False):
    """Tests duerfen NIE echte yfinance-Caches (Home-Verzeichnis) anfassen."""
    return None


@pytest.fixture()
def ds(monkeypatch):
    # yfinance-Stub nur fuer die Dauer des Tests (kein Leak in sys.modules).
    try:
        import yfinance  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "yfinance", _make_yf_stub())
    _ds = importlib.import_module("daily_scanner")
    # Auch der Throttle-Recovery-Pfad ruft _reset_yfinance_cache(force=True):
    # deshalb die Funktion selbst ersetzen (das Flag allein reicht nicht).
    monkeypatch.setattr(_ds, "_reset_yfinance_cache", _no_cache_reset)
    return _ds


def _chunk_df(chunk):
    """Baut einen yfinance-aehnlichen MultiIndex-Frame (ticker, field)."""
    cols = pd.MultiIndex.from_product([chunk, ["Open", "High", "Low", "Close", "Volume"]])
    return pd.DataFrame(np.ones((3, len(cols))), columns=cols)


class ThrottleSim:
    """Simuliert Yahoo: liefert Daten bis `limit` Namen, danach 429-Exception.
    Ein Backoff-Sleep (>=5 s) leert das Rate-Fenster (reset)."""

    def __init__(self, limit=240):
        self.limit = limit
        self.count = 0
        self.raised = 0

    def reset(self):
        self.count = 0

    def __call__(self, symbols, **k):
        chunk = symbols.split(" ")
        if self.count >= self.limit:
            self.raised += 1
            raise RuntimeError("429 Client Error: Too Many Requests")
        self.count += len(chunk)
        return _chunk_df(chunk)


def test_recovery_beats_throttle(ds):
    sim = ThrottleSim(limit=240)
    tickers = [f"T{i}" for i in range(639)]

    def sleeper(s):
        if s >= 5:           # echter Backoff -> Rate-Fenster geleert
            sim.reset()

    got = ds.batch_fetch(tickers, downloader=sim,
                         single_fetcher=lambda *a, **k: None,
                         chunk_sleep=0.0, sleeper=sleeper)
    assert sim.raised >= 1, "Throttle wurde nicht ausgeloest: Test ungueltig"
    assert len(got) >= 560, f"recovery zu schwach: {len(got)}/639"


def test_baseline_without_recovery_stays_throttled(ds):
    sim = ThrottleSim(limit=240)
    tickers = [f"T{i}" for i in range(639)]
    got = ds.batch_fetch(tickers, downloader=sim,
                         single_fetcher=lambda *a, **k: None,
                         chunk_sleep=0.0, sleeper=lambda s: None,
                         throttle_recover=False)
    assert len(got) <= 280, f"erwartet ~240 (throttled), bekam {len(got)}"


def test_clean_run_full_coverage(ds):
    sim = ThrottleSim(limit=10_000)   # kein Throttle
    tickers = [f"T{i}" for i in range(639)]
    got = ds.batch_fetch(tickers, downloader=sim,
                         single_fetcher=lambda *a, **k: None,
                         chunk_sleep=0.0, sleeper=lambda s: None)
    assert len(got) == 639


def test_budget_zero_blocks_recovery(ds):
    sim = ThrottleSim(limit=240)
    tickers = [f"T{i}" for i in range(639)]
    got = ds.batch_fetch(tickers, downloader=sim,
                         single_fetcher=lambda *a, **k: None,
                         chunk_sleep=0.0, sleeper=lambda s: None,
                         throttle_budget=0.0)
    assert len(got) <= 280, f"budget=0 darf nicht recovern, bekam {len(got)}"


def test_per_ticker_fallback_still_runs(ds):
    # Batch liefert nur die ersten 240; single_fetcher rettet 5 gezielte Namen.
    sim = ThrottleSim(limit=240)
    rescue = {"T300", "T400", "T500", "T600", "T620"}

    def single(t, *a, **k):
        return _chunk_df([t]) if t in rescue else None

    got = ds.batch_fetch([f"T{i}" for i in range(639)], downloader=sim,
                         single_fetcher=single, chunk_sleep=0.0,
                         sleeper=lambda s: None, throttle_recover=False)
    assert rescue.issubset(set(got)), "Per-Ticker-Fallback hat die Rettungs-Namen nicht geholt"


if __name__ == "__main__":
    try:
        import yfinance  # noqa: F401
    except ImportError:
        sys.modules["yfinance"] = _make_yf_stub()
    _ds = importlib.import_module("daily_scanner")
    _ds._reset_yfinance_cache = _no_cache_reset
    test_recovery_beats_throttle(_ds)
    test_baseline_without_recovery_stays_throttled(_ds)
    test_clean_run_full_coverage(_ds)
    test_budget_zero_blocks_recovery(_ds)
    test_per_ticker_fallback_still_runs(_ds)
    print("OK: alle COVERAGE-THROTTLE-1 Selftests bestanden")
