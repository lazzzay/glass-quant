"""H2-Wiring-Test (2026-06-20): load_price_paths() baut Levels + Pfade korrekt.
Offline: stub bar_provider mit synthetischer OHLC, temp-CSV mit einem Trade."""
import csv
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import exit_forward_test as eft  # noqa: E402


def _provider(df):
    return lambda t: df if t == "TST" else None


def test_levels_and_path_build():
    dates = [f"2024-01-{d:02d}" for d in range(1, 11)]
    closes = [100, 99, 98, 101, 103, 104, 105, 106, 107, 108]
    df = pd.DataFrame({"date": dates, "open": closes,
                       "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
                       "close": closes})
    fd = tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", newline="")
    w = csv.writer(fd)
    w.writerow(["date", "ticker", "entry", "sl_pct", "tp_pct"])
    w.writerow(["2024-01-04", "TST", "101", "4", "6"])   # Entry T0 = 2024-01-04 @101
    fd.close()
    trades = eft.load_price_paths(csv_path=fd.name, bar_provider=_provider(df), lookahead=5)
    os.unlink(fd.name)

    assert len(trades) == 1, trades
    t = trades[0]
    # Levels: stop = entry*(1-4%), target = entry*(1+6%)
    assert abs(t["levels"]["stop"] - 101 * 0.96) < 1e-9
    assert abs(t["levels"]["target"] - 101 * 1.06) < 1e-9
    # T+1 ist der naechste Bar (2024-01-05, close 103), SMA5 vorhanden
    assert t["path"][0]["day"] == 1 and t["path"][0]["close"] == 103
    assert abs(t["path"][0]["sma5"] - (100 + 99 + 98 + 101 + 103) / 5) < 1e-9


def test_cutoff_filter():
    dates = [f"2024-01-{d:02d}" for d in range(1, 11)]
    closes = list(range(100, 110))
    df = pd.DataFrame({"date": dates, "open": closes, "high": closes, "low": closes, "close": closes})
    fd = tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", newline="")
    w = csv.writer(fd)
    w.writerow(["date", "ticker", "entry", "sl_pct", "tp_pct"])
    w.writerow(["2023-06-01", "TST", "100", "4", "6"])   # vor cutoff
    w.writerow(["2024-01-04", "TST", "101", "4", "6"])   # nach cutoff
    fd.close()
    trades = eft.load_price_paths(csv_path=fd.name, bar_provider=_provider(df),
                                  lookahead=4, cutoff="2024-01-01")
    os.unlink(fd.name)
    assert len(trades) == 1 and trades[0]["date"] == "2024-01-04"


def test_evaluate_over_built_trades():
    dates = [f"2024-02-{d:02d}" for d in range(1, 12)]
    closes = [100, 99, 98, 101, 103, 104, 105, 106, 107, 108, 109]
    df = pd.DataFrame({"date": dates, "open": closes,
                       "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
                       "close": closes})
    fd = tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", newline="")
    w = csv.writer(fd)
    w.writerow(["date", "ticker", "entry", "sl_pct", "tp_pct"])
    w.writerow(["2024-02-05", "TST", "103", "4", "6"])
    fd.close()
    trades = eft.load_price_paths(csv_path=fd.name, bar_provider=_provider(df), lookahead=5)
    os.unlink(fd.name)
    agg = eft.evaluate(trades)
    assert set(agg) == set(eft.HYPOTHESES)
    assert all(agg[h]["n"] == len(trades) for h in agg)


if __name__ == "__main__":
    test_levels_and_path_build()
    test_cutoff_filter()
    test_evaluate_over_built_trades()
    print("OK: H2-Wiring Tests bestanden (Levels, Pfad, Cutoff, evaluate)")
