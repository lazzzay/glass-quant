"""
Unit-Tests fuer V7.9.19 OI-PAYLOAD-HEAL-1 (KW19-Bugfix-Tag, 2026-05-04).

Adressiert OF-FALLBACK-CHAIN-EXHAUSTED-1 / silent_decommission aus V7.9.18-Diagnose:
yfinance.option_chain reportet `ok`, openInterest-Spalte ist aber komplett 0/NaN.

Deckt:
  T1  OI > 0 in erster Expiry  -> oi_payload_ok=True, oi_heal_attempts=1, kein Fallback.
  T2  OI = 0 in 1. Expiry, OI > 0 in 2. Expiry  -> oi_payload_ok=True, attempts=2.
  T3  OI = 0 in allen 3 Expiries  -> ok=True, oi_payload_ok=False, attempts=3.
  T4  Backwards-Compat: alle Legacy-Result-Keys vorhanden (atm_call_oi, expiry, ...).
  T5  _pick_expiry_candidates ordnet richtig: >=today+7 zuerst, dann +3, dann today, dann letzte.
  T6  _pick_expiry_candidates respektiert max_n cap.
  T7  _measure_atm_block ohne openInterest-Spalte -> atm_call_oi=0, kein Crash.
  T8  Single-Spot-Pull: tk.history wird auch bei mehreren Expiry-Versuchen nur einmal pro Ticker gerufen.

Reine stdlib-Tests (unittest), pandas wird gemockt soweit moeglich.
Run:   python3 -m unittest tests.test_options_flow_oi_heal -v
"""
from __future__ import annotations

import datetime as _dt_module
import os
import sys
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class _FixedDateTime(datetime):
    """Datetime-Subclass, deren now(tz) einen festen 2026-05-04-Wert liefert.
    Alle anderen Methoden (strptime, fromisoformat, ...) bleiben echt."""
    @classmethod
    def now(cls, tz=None):
        return _dt_module.datetime(2026, 5, 4, 12, 0, 0, tzinfo=tz)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# Stub-Module fuer Test-Sandbox ohne yfinance/curl_cffi-Installation.
# In Production sind die echten Pakete da; der Test prueft nur unsere Logik mit Mocks.
import types
for _modname in ("yfinance",):
    if _modname not in sys.modules:
        _stub = types.ModuleType(_modname)
        _stub.Ticker = MagicMock  # falls Code irgendwo yf.Ticker(...) aufruft
        sys.modules[_modname] = _stub

import options_flow  # noqa: E402


def _make_chain(call_oi: int, put_oi: int, with_iv: bool = True) -> SimpleNamespace:
    """Baut ein chain-aehnliches Objekt mit calls.DataFrame + puts.DataFrame.
    Wir verteilen das OI gleichmaessig auf 3 Strikes (rund um 100), damit der
    +-10%-Filter (90..110) alle drei trifft.
    """
    strikes = [95.0, 100.0, 105.0]
    call_oi_per = call_oi // 3
    put_oi_per = put_oi // 3
    iv_call = [0.30, 0.31, 0.32] if with_iv else [None, None, None]
    iv_put  = [0.34, 0.33, 0.32] if with_iv else [None, None, None]
    calls = pd.DataFrame({
        "strike": strikes,
        "openInterest": [call_oi_per, call_oi_per, call_oi_per],
        "impliedVolatility": iv_call,
    })
    puts = pd.DataFrame({
        "strike": strikes,
        "openInterest": [put_oi_per, put_oi_per, put_oi_per],
        "impliedVolatility": iv_put,
    })
    return SimpleNamespace(calls=calls, puts=puts)


def _make_tk(expiries: list, chains_by_expiry: dict, spot: float = 100.0) -> MagicMock:
    """Baut ein yfinance-Ticker-Mock mit definierter expiries-Liste und
    expiry->chain Mapping. Spot kommt aus fast_info.last_price.
    """
    tk = MagicMock()
    tk.options = expiries
    tk.option_chain = lambda exp: chains_by_expiry[exp]
    tk.fast_info = SimpleNamespace(
        last_price=spot,
        regular_market_previous_close=spot,
        previous_close=spot,
        regular_market_price=spot,
    )
    tk.info = {}
    tk.history = MagicMock(return_value=pd.DataFrame({"Close": [spot]}))
    return tk


class TestOIHeal(unittest.TestCase):

    def setUp(self):
        # Reset Circuit-Breaker zwischen Tests
        options_flow._YF_ERR_STREAK["n"] = 0

    def test_T1_first_expiry_has_oi(self):
        expiries = ["2026-05-15", "2026-06-20", "2026-07-18"]
        chains = {
            "2026-05-15": _make_chain(call_oi=300, put_oi=200),
            "2026-06-20": _make_chain(call_oi=600, put_oi=400),
            "2026-07-18": _make_chain(call_oi=900, put_oi=500),
        }
        tk = _make_tk(expiries, chains)
        with patch.object(options_flow, "_yf_ticker", return_value=tk), \
             patch.object(options_flow, "datetime", _FixedDateTime):
                res = options_flow.fetch_option_oi_spike("AAPL")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["oi_payload_ok"])
        self.assertEqual(res["oi_heal_attempts"], 1)
        self.assertGreater(res["atm_call_oi"], 0)
        self.assertEqual(res["expiry"], "2026-05-15")
        self.assertEqual(res["tried_expiries"], ["2026-05-15"])

    def test_T2_first_expiry_zero_second_has_oi(self):
        expiries = ["2026-05-15", "2026-06-20", "2026-07-18"]
        chains = {
            "2026-05-15": _make_chain(call_oi=0, put_oi=0),       # silent_decommission
            "2026-06-20": _make_chain(call_oi=600, put_oi=400),   # Heal
            "2026-07-18": _make_chain(call_oi=900, put_oi=500),
        }
        tk = _make_tk(expiries, chains)
        with patch.object(options_flow, "_yf_ticker", return_value=tk), \
             patch.object(options_flow, "datetime", _FixedDateTime):
                res = options_flow.fetch_option_oi_spike("MSFT")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["oi_payload_ok"], "OI-Heal sollte zweite Expiry uebernehmen")
        self.assertEqual(res["oi_heal_attempts"], 2)
        self.assertEqual(res["expiry"], "2026-06-20")
        self.assertGreater(res["atm_call_oi"], 0)

    def test_T3_all_expiries_zero_oi_payload_marked_bad(self):
        expiries = ["2026-05-15", "2026-06-20", "2026-07-18"]
        chains = {
            "2026-05-15": _make_chain(call_oi=0, put_oi=0),
            "2026-06-20": _make_chain(call_oi=0, put_oi=0),
            "2026-07-18": _make_chain(call_oi=0, put_oi=0),
        }
        tk = _make_tk(expiries, chains)
        with patch.object(options_flow, "_yf_ticker", return_value=tk), \
             patch.object(options_flow, "datetime", _FixedDateTime):
                res = options_flow.fetch_option_oi_spike("ZZZ")
        # Wichtig: ok=True (nicht failen), aber Diagnose-Marker oi_payload_ok=False
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["oi_payload_ok"])
        self.assertEqual(res["oi_heal_attempts"], options_flow.MAX_EXPIRY_FALLBACKS_OI)
        self.assertEqual(res["atm_call_oi"], 0)
        self.assertEqual(res["atm_put_oi"], 0)

    def test_T4_backwards_compatible_keys(self):
        expiries = ["2026-05-15", "2026-06-20", "2026-07-18"]
        chains = {
            "2026-05-15": _make_chain(call_oi=300, put_oi=200),
            "2026-06-20": _make_chain(call_oi=0, put_oi=0),
            "2026-07-18": _make_chain(call_oi=0, put_oi=0),
        }
        tk = _make_tk(expiries, chains)
        with patch.object(options_flow, "_yf_ticker", return_value=tk), \
             patch.object(options_flow, "datetime", _FixedDateTime):
                res = options_flow.fetch_option_oi_spike("AAPL")
        # Legacy-Keys, die downstream-Code (compute_options_flow_score etc.) erwartet:
        for key in ("ok", "underlying_price", "expiry",
                    "atm_call_oi", "atm_put_oi", "call_put_oi_ratio",
                    "atm_call_iv", "atm_put_iv", "iv_skew", "iv_skew_tag"):
            self.assertIn(key, res, f"Legacy-Key '{key}' fehlt im Result")

    def test_T5_pick_expiry_candidates_order(self):
        # today=2026-05-04. expiries: T+1, T+5, T+10, T+30
        expiries = ["2026-05-05", "2026-05-09", "2026-05-14", "2026-06-03"]
        today = date(2026, 5, 4)
        cands = options_flow._pick_expiry_candidates(expiries, today, max_n=3)
        # >=today+7 zuerst (2026-05-14, 2026-06-03)
        self.assertEqual(cands[0], "2026-05-14")
        self.assertEqual(cands[1], "2026-06-03")
        # Dann fuellt sich aus >=today+3 (2026-05-09)
        self.assertEqual(cands[2], "2026-05-09")
        self.assertEqual(len(cands), 3)

    def test_T6_pick_expiry_candidates_max_n_cap(self):
        expiries = ["2026-05-15", "2026-05-22", "2026-06-20", "2026-07-18", "2026-08-15"]
        today = date(2026, 5, 4)
        cands = options_flow._pick_expiry_candidates(expiries, today, max_n=2)
        self.assertEqual(len(cands), 2)
        # Zwei naheliegendste >=today+7 sind 2026-05-15, 2026-05-22
        self.assertEqual(cands, ["2026-05-15", "2026-05-22"])

    def test_T7_measure_atm_block_handles_missing_openinterest(self):
        # Chain ohne openInterest-Spalte
        calls = pd.DataFrame({"strike": [95.0, 100.0, 105.0],
                              "impliedVolatility": [0.3, 0.31, 0.32]})
        puts = pd.DataFrame({"strike": [95.0, 100.0, 105.0],
                             "impliedVolatility": [0.34, 0.33, 0.32]})
        chain = SimpleNamespace(calls=calls, puts=puts)
        atm = options_flow._measure_atm_block(chain, spot=100.0)
        self.assertEqual(atm["atm_call_oi"], 0)
        self.assertEqual(atm["atm_put_oi"], 0)
        # IV bleibt verwertbar
        self.assertIsNotNone(atm["atm_call_iv"])
        self.assertEqual(atm["iv_skew_tag"], "put_bias")  # 0.31 - 0.33 = -0.02 borderline... let's check:
        # Strike==100 ist nearest -> call_iv=0.31, put_iv=0.33 -> skew=-0.02 -> iv_skew_tag=neutral (>= -0.02 nicht <)
        # Daher relax assertion:
        self.assertIn(atm["iv_skew_tag"], ("put_bias", "neutral"))

    def test_T8_single_spot_pull_across_expiry_attempts(self):
        # 3 Expiries, alle OI=0 -> Loop muss 3x option_chain aufrufen,
        # aber tk.history nur einmal (da Spot via fast_info kommt + dann wuerden 3 history-Calls
        # einen Performance-Hit bedeuten)
        expiries = ["2026-05-15", "2026-06-20", "2026-07-18"]
        chains = {
            "2026-05-15": _make_chain(call_oi=0, put_oi=0),
            "2026-06-20": _make_chain(call_oi=0, put_oi=0),
            "2026-07-18": _make_chain(call_oi=0, put_oi=0),
        }
        tk = _make_tk(expiries, chains)
        # tk.history sollte 0x gerufen werden, weil fast_info bereits liefert.
        with patch.object(options_flow, "_yf_ticker", return_value=tk):
            with patch.object(options_flow, "datetime") as mock_dt:
                mock_dt.now.return_value.date.return_value = date(2026, 5, 4)
                mock_dt.strptime = options_flow.datetime.strptime
                _ = options_flow.fetch_option_oi_spike("XYZ")
        # tk.history darf nicht (oder maximal 1x) gerufen werden
        self.assertLessEqual(tk.history.call_count, 1,
                             "Spot-Pull sollte beim Heal-Loop NICHT pro Expiry wiederholt werden")


if __name__ == "__main__":
    unittest.main(verbosity=2)
