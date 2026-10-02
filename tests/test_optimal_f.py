"""
Unit-Tests fuer sizing.optimal_f + Kelly-Sizer Vol-Scaling / Sizing-Mode.

Ausfuehrung:
    python3 -m pytest tests/test_optimal_f.py -q
oder minimal:
    python3 tests/test_optimal_f.py
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sizing.optimal_f import (  # noqa: E402
    compute_optimal_f,
    vol_scaling_factor,
    realized_vol,
    twr,
    _downside_upside_dev,
    _max_drawdown_on_twr_path,
)


class TestVinceOptimalF(unittest.TestCase):
    """Vince-2007 TWR / Optimal-f-Grid-Maximizer."""

    def test_positive_edge_yields_positive_f(self):
        # 60% Wins bei +1.5R, 40% Losses bei -1R → positiver Edge
        rng = np.random.default_rng(42)
        draws = rng.random(500)
        returns = np.where(draws < 0.6, 0.015, -0.01)
        res = compute_optimal_f(returns, drawdown_penalty=0.5)
        self.assertEqual(res.reason, "ok")
        self.assertGreater(res.f_applied, 0.0)
        self.assertLessEqual(res.f_applied, res.f_sortino)
        self.assertLessEqual(res.f_sortino, res.f_raw)

    def test_negative_edge_zero_f(self):
        rng = np.random.default_rng(1)
        draws = rng.random(500)
        returns = np.where(draws < 0.4, 0.01, -0.02)   # 40% Wins, -2R
        res = compute_optimal_f(returns)
        self.assertEqual(res.f_applied, 0.0)
        self.assertEqual(res.reason, "no_edge")

    def test_undersampled_undersampled_flag(self):
        res = compute_optimal_f([0.01] * 10)
        self.assertEqual(res.reason, "undersampled")
        self.assertEqual(res.f_applied, 0.0)

    def test_degenerate_no_losses_flagged(self):
        # Keine Verluste: TWR divergiert → reason "degenerate"
        res = compute_optimal_f([0.01, 0.02, 0.005] * 50)
        self.assertEqual(res.reason, "degenerate")
        self.assertEqual(res.f_applied, 0.0)

    def test_twr_monotone_to_f_raw_for_positive_edge(self):
        # TWR bei f_raw muss >= TWR bei f_raw/2 sein (grobe Monotonie)
        rng = np.random.default_rng(7)
        draws = rng.random(300)
        returns = np.where(draws < 0.55, 0.02, -0.012)
        arr = np.asarray(returns)
        res = compute_optimal_f(returns, drawdown_penalty=0.0)
        self.assertGreaterEqual(twr(res.f_raw, arr), twr(res.f_raw / 2.0, arr))

    def test_drawdown_penalty_reduces_f(self):
        rng = np.random.default_rng(11)
        draws = rng.random(400)
        returns = np.where(draws < 0.6, 0.018, -0.013)
        # penalty=0 → f_applied == f_sortino; penalty=1 → f_applied < f_sortino
        res0 = compute_optimal_f(returns, drawdown_penalty=0.0)
        res1 = compute_optimal_f(returns, drawdown_penalty=1.0)
        self.assertAlmostEqual(res0.f_applied, res0.f_sortino, places=6)
        self.assertLess(res1.f_applied, res1.f_sortino + 1e-9)


class TestSortinoDecay(unittest.TestCase):
    """Sortino-Decay-Adjustment: asymmetrische Downside reduziert f."""

    def test_symmetric_returns_minor_decay(self):
        rng = np.random.default_rng(21)
        # Positiver Edge mit symmetrischer Vola (gleiche Amplituden)
        returns = rng.choice([0.01, -0.01], size=400, p=[0.6, 0.4])
        res = compute_optimal_f(returns, drawdown_penalty=0.0)
        # Downside + Upside-Dev sind etwa gleich → scale ~ 0.5
        ratio = res.f_sortino / res.f_raw if res.f_raw > 0 else 0.0
        self.assertTrue(0.3 < ratio < 0.7, f"ratio={ratio}")

    def test_fat_tail_downside_shrinks_f(self):
        rng = np.random.default_rng(33)
        # 70% kleine Wins, 30% fat Verluste → positiver Edge, aber stark
        # asymmetrisch zur Downside. f_sortino muss < f_raw rauskommen.
        returns = np.where(rng.random(600) < 0.70, 0.025, -0.050)
        res = compute_optimal_f(returns, drawdown_penalty=0.0)
        self.assertGreater(res.f_raw, 0.0)
        self.assertLess(res.f_sortino, res.f_raw * 0.85,
                        f"f_sortino={res.f_sortino} f_raw={res.f_raw}")

    def test_downside_dev_components(self):
        arr = np.array([0.02, -0.01, 0.005, -0.03, 0.01])
        dd, ud = _downside_upside_dev(arr, mar=0.0)
        self.assertGreater(dd, 0)
        self.assertGreater(ud, 0)
        self.assertGreater(dd, ud)  # -0.03 ueberwiegt im Sample


class TestVolScaling(unittest.TestCase):
    """Q2 Vol-Scaling: Scaler <= 1.0, clamp-Verhalten, edge-cases."""

    def test_high_vol_scales_below_one(self):
        rng = np.random.default_rng(5)
        returns = rng.normal(0.0005, 0.05, size=252)  # 80%+ annualisiert
        s = vol_scaling_factor(returns, target_vol=0.15)
        self.assertGreater(s, 0.0)
        self.assertLess(s, 1.0)

    def test_low_vol_clamped_at_one(self):
        rng = np.random.default_rng(8)
        returns = rng.normal(0.0, 0.001, size=252)  # sehr niedrige Vola
        s = vol_scaling_factor(returns, target_vol=0.15)
        self.assertEqual(s, 1.0)

    def test_zero_vol_returns_one(self):
        # Flat-Line Returns -> current_vol=0 -> Scaler=1.0
        s = vol_scaling_factor([0.0] * 50, target_vol=0.15)
        self.assertEqual(s, 1.0)

    def test_window_slicing(self):
        rng = np.random.default_rng(13)
        # Erste 200 flat, letzten 52 hochvola
        flat = np.zeros(200)
        noisy = rng.normal(0.0, 0.05, size=52)
        series = np.concatenate([flat, noisy])
        s_all = vol_scaling_factor(series, target_vol=0.15, window=None)
        s_tail = vol_scaling_factor(series, target_vol=0.15, window=52)
        self.assertLess(s_tail, s_all)

    def test_realized_vol_nonnegative(self):
        rng = np.random.default_rng(2)
        v = realized_vol(rng.normal(0, 0.01, 100))
        self.assertGreaterEqual(v, 0.0)


def _synthetic_journal(path: Path, n: int = 40) -> Path:
    """Temporaeres Journal mit n geschlossenen, synthetischen 'V6.1'-Trades
    (deterministisch, Seed 42). Die echte trade_journal.sqlite wird nie gelesen."""
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, "
        "system_version TEXT, ticker TEXT, entry_date TEXT, exit_date TEXT, "
        "entry_price REAL, exit_price REAL, return_pct REAL)")
    rng = np.random.default_rng(42)
    for i in range(n):
        ret = float(rng.normal(0.004, 0.02))
        day = f"2099-{1 + i // 28:02d}-{1 + i % 28:02d}"
        con.execute(
            "INSERT INTO trades (source, system_version, ticker, entry_date, exit_date, "
            "entry_price, exit_price, return_pct) VALUES (?,?,?,?,?,?,?,?)",
            ("backtest", "V6.1", f"T{i:03d}", day, day, 100.0,
             round(100.0 * (1.0 + ret), 4), ret * 100.0))
    con.commit()
    con.close()
    return path


class TestKellySizerIntegration(unittest.TestCase):
    """Sizing-Mode-Schalter + Vol-Scaling im Kelly-Sizer (hermetisch, Temp-Journal)."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = _synthetic_journal(Path(self._tmpdir.name) / "journal.sqlite")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_sizing_mode_validation(self):
        from tools.kelly_sizer import size_for_setup, SIZING_MODES
        self.assertIn("half_kelly", SIZING_MODES)
        self.assertIn("optimal_f", SIZING_MODES)
        with self.assertRaises(ValueError):
            size_for_setup("V6.1", 10_000, None,
                           db_path=self.db,
                           sizing_mode="bogus")

    def test_half_kelly_and_optimal_f_produce_results(self):
        from tools.kelly_sizer import (
            size_for_setup, SIZING_MODE_HALF_KELLY, SIZING_MODE_OPTIMAL_F,
        )
        db = self.db
        hk = size_for_setup("V6.1", 10_000, None, db_path=db,
                            sizing_mode=SIZING_MODE_HALF_KELLY,
                            target_vol=0.15)
        optf = size_for_setup("V6.1", 10_000, None, db_path=db,
                              sizing_mode=SIZING_MODE_OPTIMAL_F,
                              target_vol=0.15)
        self.assertEqual(hk.sizing_mode, "half_kelly")
        self.assertEqual(optf.sizing_mode, "optimal_f")
        self.assertGreaterEqual(hk.n_trades, 30)
        self.assertGreaterEqual(optf.n_trades, 30)
        # Beide groessen duerfen nie negativ sein und nie > cap*1.0
        for kr in (hk, optf):
            self.assertGreaterEqual(kr.size_eur, 0.0)
            self.assertLessEqual(kr.size_eur, 10_000.0)

    def test_vol_scaling_clamped_to_one(self):
        from tools.kelly_sizer import size_for_setup
        db = self.db
        # Sehr hohe target_vol → Vol-Scaler muss auf 1.0 clampen
        kr = size_for_setup("V6.1", 10_000, None, db_path=db,
                            sizing_mode="half_kelly",
                            target_vol=10.0)  # absurd hoch
        self.assertEqual(kr.vol_scaling, 1.0)


class TestTwrPathHelpers(unittest.TestCase):
    """Interne Helfer (twr, max_drawdown_on_twr_path)."""

    def test_twr_at_zero_f_is_one(self):
        self.assertEqual(twr(0.0, np.array([0.01, -0.01])), 1.0)

    def test_max_dd_negative_or_zero(self):
        arr = np.array([0.02, -0.05, 0.01, -0.08, 0.03])
        dd = _max_drawdown_on_twr_path(0.2, arr)
        self.assertLessEqual(dd, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
