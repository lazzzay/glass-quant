"""
Smoke-Tests fuer SCAN-OF-ORCHESTRATION-1 Fix (V7.9.10, KW18-Bugfix-Tag).

Deckt:
  T1  OF-Datei existiert + frische mtime  -> Wait-Loop returned sofort (ok_immediate).
  T2  OF-Datei fehlt zunaechst, taucht innerhalb Wait-Fenster auf -> ok_after_wait.
  T3  OF-Datei fehlt nach Timeout -> reason=missing_timeout, exists=False.
  T4  OF-Datei stale (mtime alt), require_fresh=False -> akzeptiert (stale_no_wait).
  T5  OF-Datei stale, require_fresh=True, kein Wait -> nicht usable, kein Wait gemacht.
  T6  fail_hard_if_required exit 2 bei missing + require=True.
  T7  fail_hard_if_required exit 3 bei stale + require_fresh=True.
  T8  fail_hard_if_required schweigt bei usable+require=True.
  T9  Default (max_wait=0, require=False) ist rueckwaerts-kompatibel:
       fehlende Datei -> exists=False, kein Exit, kein Side-Effect.
  T10 OF-Datei stale + require_fresh=True + Wait -> auf frische Variante warten,
       wenn neue mtime auftaucht -> ok_after_wait.

Reine stdlib-Tests (unittest), kein pytest noetig.
Run:   python3 -m unittest tests.test_options_flow_wait -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Pfad-Setup -- tools/ liegt parallel zu tests/.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

from options_flow_wait import (  # noqa: E402
    DEFAULT_FRESH_MAX_AGE_S,
    OFWaitResult,
    fail_hard_if_required,
    wait_for_options_flow,
)


# ---------------------------------------------------------------------------
# Hilfen: deterministische Zeit-/Sleep-Mocks fuer Wait-Loop-Tests.
# ---------------------------------------------------------------------------

class FakeClock:
    """Deterministische Zeit ohne realen sleep -- testet Loop-Logik in 0 ms."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _touch(path: Path, mtime: float | None = None) -> None:
    path.write_text("{}", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWaitForOptionsFlow(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "options_flow_2026-04-27.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- T1 -----------------------------------------------------------------
    def test_t1_fresh_file_returns_immediately(self) -> None:
        clock = FakeClock()
        _touch(self.path, mtime=clock.now - 60)  # 60 s alt -> frisch.

        res = wait_for_options_flow(
            self.path, max_wait_s=180,
            _now_fn=clock.time, _sleep_fn=clock.sleep,
        )
        self.assertTrue(res.exists)
        self.assertTrue(res.fresh)
        self.assertEqual(res.reason, "ok_immediate")
        self.assertEqual(res.waited_s, 0.0)
        self.assertEqual(clock.sleeps, [], "kein sleep, da Datei sofort da")

    # ---- T2 -----------------------------------------------------------------
    def test_t2_file_appears_within_wait_window(self) -> None:
        clock = FakeClock()

        # Sleep-Hook: sobald Loop einmal gepollt hat, legen wir die Datei an.
        original_sleep = clock.sleep

        def sleep_then_create(seconds: float) -> None:
            original_sleep(seconds)
            if not self.path.exists():
                _touch(self.path, mtime=clock.now)  # frische mtime

        res = wait_for_options_flow(
            self.path, max_wait_s=60, poll_interval_s=5,
            _now_fn=clock.time, _sleep_fn=sleep_then_create,
        )
        self.assertTrue(res.exists)
        self.assertTrue(res.fresh)
        self.assertEqual(res.reason, "ok_after_wait")
        self.assertGreaterEqual(res.waited_s, 5.0)
        self.assertLessEqual(res.waited_s, 60.0)

    # ---- T3 -----------------------------------------------------------------
    def test_t3_missing_after_timeout(self) -> None:
        clock = FakeClock()
        res = wait_for_options_flow(
            self.path, max_wait_s=10, poll_interval_s=5,
            _now_fn=clock.time, _sleep_fn=clock.sleep,
        )
        self.assertFalse(res.exists)
        self.assertFalse(res.fresh)
        self.assertEqual(res.reason, "missing_timeout")
        self.assertGreaterEqual(res.waited_s, 10.0)

    # ---- T4 -----------------------------------------------------------------
    def test_t4_stale_file_accepted_when_not_require_fresh(self) -> None:
        clock = FakeClock()
        # Datei ist 30 h alt -> nicht frisch.
        _touch(self.path, mtime=clock.now - 30 * 3600)

        res = wait_for_options_flow(
            self.path, max_wait_s=0, require_fresh=False,
            _now_fn=clock.time, _sleep_fn=clock.sleep,
        )
        self.assertTrue(res.exists)
        self.assertFalse(res.fresh)
        # Snapshot/no-wait -> stale_no_wait
        self.assertIn(res.reason, ("stale_no_wait", "ok_immediate"))
        # Trotz fehlender Frische ist sie usable, wenn require_fresh=False.
        self.assertTrue(res.is_usable(require_fresh=False))
        self.assertFalse(res.is_usable(require_fresh=True))

    # ---- T5 -----------------------------------------------------------------
    def test_t5_stale_with_require_fresh_no_wait_not_usable(self) -> None:
        clock = FakeClock()
        _touch(self.path, mtime=clock.now - DEFAULT_FRESH_MAX_AGE_S - 100)

        res = wait_for_options_flow(
            self.path, max_wait_s=0, require_fresh=True,
            _now_fn=clock.time, _sleep_fn=clock.sleep,
        )
        # Datei existiert, aber nicht usable bei require_fresh=True.
        self.assertTrue(res.exists)
        self.assertFalse(res.fresh)
        self.assertFalse(res.is_usable(require_fresh=True))
        # max_wait=0 -> keine sleeps gemacht.
        self.assertEqual(clock.sleeps, [])

    # ---- T6 -----------------------------------------------------------------
    def test_t6_fail_hard_exits_2_when_missing_and_required(self) -> None:
        res = OFWaitResult(
            exists=False, fresh=False, mtime=None,
            waited_s=180.0, reason="missing_timeout",
        )
        exits: list[int] = []
        logs: list[str] = []
        fail_hard_if_required(
            res, require_options_flow=True, require_fresh=False,
            of_path=self.path, exit_fn=exits.append, log_fn=logs.append,
        )
        self.assertEqual(exits, [2])
        self.assertTrue(any("nicht vorhanden" in m for m in logs))

    # ---- T7 -----------------------------------------------------------------
    def test_t7_fail_hard_exits_3_when_stale_and_require_fresh(self) -> None:
        # Datei existiert, ist aber stale.
        import time as _time
        old_mtime = _time.time() - DEFAULT_FRESH_MAX_AGE_S - 3600
        res = OFWaitResult(
            exists=True, fresh=False, mtime=old_mtime,
            waited_s=180.0, reason="stale_after_wait",
        )
        exits: list[int] = []
        logs: list[str] = []
        fail_hard_if_required(
            res, require_options_flow=True, require_fresh=True,
            of_path=self.path, exit_fn=exits.append, log_fn=logs.append,
        )
        self.assertEqual(exits, [3])
        self.assertTrue(any("stale" in m for m in logs))

    # ---- T8 -----------------------------------------------------------------
    def test_t8_fail_hard_silent_when_usable(self) -> None:
        import time as _time
        res = OFWaitResult(
            exists=True, fresh=True, mtime=_time.time() - 120,
            waited_s=2.0, reason="ok_after_wait",
        )
        exits: list[int] = []
        logs: list[str] = []
        fail_hard_if_required(
            res, require_options_flow=True, require_fresh=True,
            of_path=self.path, exit_fn=exits.append, log_fn=logs.append,
        )
        self.assertEqual(exits, [])
        self.assertEqual(logs, [])

    # ---- T9 -----------------------------------------------------------------
    def test_t9_default_zero_wait_is_backward_compatible(self) -> None:
        clock = FakeClock()
        # Datei fehlt komplett. Default-Path: max_wait=0, require=False.
        res = wait_for_options_flow(
            self.path, max_wait_s=0,
            _now_fn=clock.time, _sleep_fn=clock.sleep,
        )
        self.assertFalse(res.exists)
        self.assertEqual(res.waited_s, 0.0)
        self.assertEqual(clock.sleeps, [])
        # fail_hard mit default-Flags exit-frei.
        exits: list[int] = []
        fail_hard_if_required(
            res, require_options_flow=False, require_fresh=False,
            of_path=self.path, exit_fn=exits.append, log_fn=lambda _m: None,
        )
        self.assertEqual(exits, [])

    # ---- T10 ----------------------------------------------------------------
    def test_t10_stale_then_fresh_appears_during_wait(self) -> None:
        clock = FakeClock()
        # Schritt 1: stale Datei legen.
        old_mtime = clock.now - DEFAULT_FRESH_MAX_AGE_S - 3600
        _touch(self.path, mtime=old_mtime)

        original_sleep = clock.sleep

        def sleep_then_refresh(seconds: float) -> None:
            original_sleep(seconds)
            # Nach dem ersten Sleep schreiben wir frische mtime.
            os.utime(self.path, (clock.now, clock.now))

        res = wait_for_options_flow(
            self.path, max_wait_s=60, poll_interval_s=5,
            require_fresh=True,
            _now_fn=clock.time, _sleep_fn=sleep_then_refresh,
        )
        self.assertTrue(res.exists)
        self.assertTrue(res.fresh)
        self.assertEqual(res.reason, "ok_after_wait")
        self.assertGreaterEqual(res.waited_s, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
