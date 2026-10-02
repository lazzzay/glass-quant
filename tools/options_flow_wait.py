"""
SCAN-OF-ORCHESTRATION-1 Fix (V7.9.10, KW18-Bugfix-Tag 2026-04-27)
=================================================================

Helper-Modul fuer daily_scanner.py: blockierender Poll-Wait-Loop auf das
heutige `tools/options_flow_{scan_date}.json`-Artefakt.

Problem (siehe ops_tickets.md SCAN-OF-ORCHESTRATION-1, OPEN seit 2026-04-20):
  Der Cron-Runner started `daily_scanner.py` heute teils ein paar Sekunden
  bis Minuten BEVOR `options_flow.py` fertig ist (5/5 Werktage im
  Observation-Window). Der Scanner findet dann entweder
    (a) gar keine options_flow_{date}.json (frueh am Tag),
    (b) die Vortags-Datei (alt-mtime, falsche Daten),
  und joint stillschweigend leer -> alle Top-Setups bekommen
  options_flow_score=0, flow_tag='no_data'. V6.8-Layer ist tot, ohne dass
  der Cron auch nur ein Warn ausgibt. Auswirkung: Audit-Gate-5
  (AUDIT-VAL-2 am 2026-04-29) ist mechanisch unerreichbar.

Loesung Option (c) aus dem Ticket: Scanner wartet auf das OF-Manifest via
poll-Loop, BEVOR er weiterfaehrt. Wenn die Datei vor Timeout auftaucht und
frisch ist (mtime im heutigen Tagesfenster), startet das Scoring wie bisher
mit befuellter `options_flow_map`.

Default-Verhalten ist rueckwaertskompatibel:
  --wait-options-flow=0           -> kein Wait, alter Pfad (default)
  --wait-options-flow=180         -> warte bis zu 180 s
  --require-options-flow          -> bei Timeout exit-code=2 (kein leerer Scan)
  --require-fresh-options-flow    -> wenn Datei da, aber mtime alt, ebenfalls
                                     wie missing behandeln

Freeze-konform: das Scoring selbst bleibt unangetastet. Bei Default-Aufruf
(0/False/False) verhaelt sich der Scanner exakt wie vorher.

Recommended cron invocation:
  python3 tools/daily_scanner.py \\
      --wait-options-flow=180 --require-options-flow --require-fresh-options-flow

Reine stdlib (os, time, pathlib) -- kein dritte-Quelle-Code.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Wie alt darf options_flow_{scan_date}.json maximal sein, damit wir sie
# als "frisch" akzeptieren? 18 h deckt den Vorabend-Run (z. B. Sa 22:00 UTC
# fuer den Mo-Scan) sauber ab und filtert versehentliche Vortags-mtimes
# (>24 h) zuverlaessig aus.
DEFAULT_FRESH_MAX_AGE_S = 18 * 3600

# Default Poll-Interval. Reagiert schnell genug, ohne disk I/O zu fluten.
DEFAULT_POLL_INTERVAL_S = 5


@dataclass
class OFWaitResult:
    """Ergebnis eines Wait-Loops auf das OF-Artefakt."""

    exists: bool
    fresh: bool
    mtime: Optional[float]
    waited_s: float
    reason: str  # "ok_immediate" | "ok_after_wait" | "missing_timeout" |
                 # "stale_no_wait" | "stale_after_wait"

    def is_usable(self, *, require_fresh: bool) -> bool:
        """Datei ist konsumierbar fuer den Scanner?"""
        if not self.exists:
            return False
        if require_fresh and not self.fresh:
            return False
        return True


def _file_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None


def _is_fresh(mtime: Optional[float], *, now_ts: float, max_age_s: float) -> bool:
    if mtime is None:
        return False
    return (now_ts - mtime) <= max_age_s


def wait_for_options_flow(
    of_path: Path,
    *,
    max_wait_s: int = 0,
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    fresh_max_age_s: int = DEFAULT_FRESH_MAX_AGE_S,
    require_fresh: bool = False,
    log_fn: Optional[Callable[[str], None]] = None,
    _now_fn: Callable[[], float] = time.time,
    _sleep_fn: Callable[[float], None] = time.sleep,
) -> OFWaitResult:
    """Blockiere bis `of_path` existiert (und optional frisch ist) oder
    `max_wait_s` Sekunden vergangen sind.

    Args:
      of_path: erwarteter Pfad zu `options_flow_{date}.json`.
      max_wait_s: harte Obergrenze; 0 = kein Wait, sofortiger Return.
      poll_interval_s: wie oft prueft der Loop die Datei.
      fresh_max_age_s: mtime-Toleranz; aelter -> not fresh.
      require_fresh: bei True wird stale-Datei wie fehlend behandelt.
      log_fn: optionaler Logger (callable(str) -> None). None = stdout-stille.
      _now_fn / _sleep_fn: dependency injection fuer Tests.

    Returns:
      OFWaitResult mit exists/fresh/mtime/waited_s/reason.

    Side effects:
      Reine Lese-Operation (stat). Erzeugt KEINE Dateien.
    """
    log = log_fn or (lambda _msg: None)
    start_ts = _now_fn()
    mtime = _file_mtime(of_path)

    if mtime is not None:
        fresh = _is_fresh(mtime, now_ts=start_ts, max_age_s=fresh_max_age_s)
        if fresh or not require_fresh:
            reason = "ok_immediate" if fresh else "stale_no_wait"
            return OFWaitResult(
                exists=True, fresh=fresh, mtime=mtime,
                waited_s=0.0, reason=reason,
            )
        log(f"[OF-WAIT] Datei {of_path.name} stale (mtime={mtime:.0f}); "
            f"warte auf frische Version (max {max_wait_s}s).")

    if max_wait_s <= 0:
        # Kein Wait gewuenscht -> Snapshot zurueckgeben.
        if mtime is None:
            return OFWaitResult(
                exists=False, fresh=False, mtime=None,
                waited_s=0.0, reason="missing_timeout",
            )
        return OFWaitResult(
            exists=True,
            fresh=_is_fresh(mtime, now_ts=start_ts, max_age_s=fresh_max_age_s),
            mtime=mtime, waited_s=0.0, reason="stale_no_wait",
        )

    log(f"[OF-WAIT] {of_path.name} fehlt -- poll alle {poll_interval_s}s "
        f"bis zu {max_wait_s}s ...")
    deadline = start_ts + max_wait_s
    while True:
        now = _now_fn()
        if now >= deadline:
            mtime_final = _file_mtime(of_path)
            waited = now - start_ts
            if mtime_final is None:
                log(f"[OF-WAIT] Timeout nach {waited:.0f}s -- {of_path.name} "
                    f"weiterhin nicht vorhanden.")
                return OFWaitResult(
                    exists=False, fresh=False, mtime=None,
                    waited_s=waited, reason="missing_timeout",
                )
            fresh = _is_fresh(mtime_final, now_ts=now, max_age_s=fresh_max_age_s)
            log(f"[OF-WAIT] Timeout nach {waited:.0f}s -- {of_path.name} "
                f"vorhanden, fresh={fresh}.")
            return OFWaitResult(
                exists=True, fresh=fresh, mtime=mtime_final,
                waited_s=waited,
                reason="ok_after_wait" if fresh else "stale_after_wait",
            )

        mtime = _file_mtime(of_path)
        if mtime is not None:
            fresh = _is_fresh(mtime, now_ts=now, max_age_s=fresh_max_age_s)
            if fresh or not require_fresh:
                waited = now - start_ts
                reason = "ok_after_wait" if fresh else "stale_after_wait"
                log(f"[OF-WAIT] {of_path.name} verfuegbar nach {waited:.1f}s "
                    f"(fresh={fresh}).")
                return OFWaitResult(
                    exists=True, fresh=fresh, mtime=mtime,
                    waited_s=waited, reason=reason,
                )

        _sleep_fn(poll_interval_s)


def fail_hard_if_required(
    result: OFWaitResult,
    *,
    require_options_flow: bool,
    require_fresh: bool,
    of_path: Path,
    exit_fn: Callable[[int], None] = sys.exit,
    log_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """Bei `--require-options-flow` einen Hart-Exit ausloesen, falls die
    OF-Datei nicht konsumierbar ist. Sonst kein Effekt.

    Exit-Codes:
      2 = OF fehlt nach Wait                    (require_options_flow=True)
      3 = OF stale, frische Variante gefordert  (require_fresh=True)
    """
    log = log_fn or (lambda _msg: print(_msg, file=sys.stderr))
    if not require_options_flow:
        return
    if result.is_usable(require_fresh=require_fresh):
        return
    if not result.exists:
        log(f"[OF-WAIT][ABORT] {of_path.name} nicht vorhanden nach "
            f"{result.waited_s:.0f}s Wait. SCAN-OF-ORCHESTRATION-1 schuetzt "
            f"vor leerem OF-Join. Cron-Runner-Reihenfolge prueffen.")
        exit_fn(2)
        return
    if require_fresh and not result.fresh:
        age = "n/a" if result.mtime is None else f"{(time.time()-result.mtime)/3600:.1f}h"
        log(f"[OF-WAIT][ABORT] {of_path.name} stale (Alter {age}). "
            f"--require-fresh-options-flow gesetzt, breche ab.")
        exit_fn(3)


__all__ = [
    "DEFAULT_FRESH_MAX_AGE_S",
    "DEFAULT_POLL_INTERVAL_S",
    "OFWaitResult",
    "wait_for_options_flow",
    "fail_hard_if_required",
]
