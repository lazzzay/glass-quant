#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoke-Tests fuer V7.9.5 TICKET-HYGIENE-2: timing-aware stale-Erkennung in
tools/ticket_auto_resolve.py.

Neu in V7.9.5: Regel 3 "timing-stale" resolve-t heutige `*_JSON_MISSING`-
Fingerprints, sobald das referenzierte Upstream-Artefakt im tools/-Verzeichnis
existiert: unabhaengig davon, ob data_health re-run wurde. Diese Tests pruefen
genau diesen neuen Pfad und stellen sicher, dass die alten Regeln 1 (historisiert)
und 2 (stale via data_health-Re-Run) weiterhin funktionieren.

Run: python3 tests/test_ticket_auto_resolve_timing.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from tools.ticket_auto_resolve import (  # noqa: E402
    JSON_MISSING_ARTIFACT,
    _check_timing_stale,
    _decide_health,
)


def _touch(path: Path, mtime_epoch: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    if mtime_epoch is not None:
        os.utime(path, (mtime_epoch, mtime_epoch))


# ---------------------------------------------------------------------------
# _check_timing_stale
# ---------------------------------------------------------------------------

def test_timing_stale_regime_present():
    """REGIME_JSON_MISSING heute + regime_{date}.json existiert -> timing-stale."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        day = dt.date(2026, 4, 22)
        _touch(tools / "regime_2026-04-22.json")
        ok, reason = _check_timing_stale(day, "REGIME_JSON_MISSING", tools)
        assert ok, f"erwartet timing-stale, got keep_open ({reason})"
        assert "timing-stale" in reason, reason
        assert "regime_2026-04-22.json" in reason, reason
        print(f"[OK] regime present -> timing-stale: {reason[:80]}")


def test_timing_stale_premarket_missing():
    """PREMARKET_JSON_MISSING heute + premarket-Datei existiert NICHT -> keep."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        day = dt.date(2026, 4, 22)
        ok, reason = _check_timing_stale(day, "PREMARKET_JSON_MISSING", tools)
        assert not ok, f"erwartet keep_open, got resolve ({reason})"
        print("[OK] premarket missing -> keep open (artifact noch nicht da)")


def test_timing_stale_unknown_code_ignored():
    """Nicht-JSON_MISSING-Code darf nie timing-stale triggern."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        day = dt.date(2026, 4, 22)
        # Selbst wenn ein "regime_*.json" da waere, OPTIONS_FLOW_INACTIVE
        # ist kein JSON_MISSING-Symptom -> keine timing-Resolve.
        _touch(tools / "regime_2026-04-22.json")
        ok, reason = _check_timing_stale(day, "OPTIONS_FLOW_INACTIVE", tools)
        assert not ok, f"OPTIONS_FLOW_INACTIVE darf nicht resolved werden ({reason})"
        print("[OK] non-MISSING-code -> keep open")


def test_mapping_covers_documented_codes():
    """Harte Zusicherung: die wichtigsten Orchestrations-Codes sind gemappt."""
    must_have = {
        "REGIME_JSON_MISSING",
        "PREMARKET_JSON_MISSING",
        "OPTIONS_FLOW_JSON_MISSING",
        "INSIDER_JSON_MISSING",
    }
    missing = must_have - set(JSON_MISSING_ARTIFACT.keys())
    assert not missing, f"Mapping unvollstaendig: {missing}"
    print(f"[OK] JSON_MISSING_ARTIFACT-Mapping enthaelt {len(JSON_MISSING_ARTIFACT)} Codes")


# ---------------------------------------------------------------------------
# _decide_health Integration
# ---------------------------------------------------------------------------

def test_decide_health_rule2_wins_when_datahealth_reran():
    """Wenn data_health re-run wurde und fp nicht mehr drinsteht,
    gewinnt die alte Regel 2 (stale via findings-Absenz)."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        day = dt.date(2026, 4, 22)
        _touch(tools / "regime_2026-04-22.json")  # Upstream existiert auch
        # fp ist NICHT in heutigen findings -> Rule 2 greift zuerst
        should, reason = _decide_health(
            ticket_date=day,
            fp="0fa4f9ddae",
            today=day,
            health_fps_by_date={"2026-04-22": set()},  # leer -> fp nicht drin
            ticket_code="REGIME_JSON_MISSING",
            tools_dir=tools,
        )
        assert should, reason
        assert "stale: fp nicht in aktueller" in reason, reason
        print(f"[OK] rule 2 Vorrang bei data_health-Re-Run: {reason[:70]}...")


def test_decide_health_rule3_when_datahealth_not_reran():
    """Wenn data_health NICHT re-run wurde (fp noch aktiv), aber das
    Upstream-Artefakt existiert, resolved die neue Regel 3."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        day = dt.date(2026, 4, 22)
        _touch(tools / "regime_2026-04-22.json")
        should, reason = _decide_health(
            ticket_date=day,
            fp="0fa4f9ddae",
            today=day,
            # fp IST noch in heutigen findings -> Rule 2 greift nicht
            health_fps_by_date={"2026-04-22": {"0fa4f9ddae"}},
            ticket_code="REGIME_JSON_MISSING",
            tools_dir=tools,
        )
        assert should, reason
        assert "timing-stale" in reason, reason
        assert "regime_2026-04-22.json" in reason, reason
        print(f"[OK] rule 3 (timing-stale) bei NICHT-re-run: {reason[:70]}...")


def test_decide_health_rule3_blocked_when_artifact_missing():
    """Kein Upstream-Artefakt + fp noch aktiv + kein data_health-Re-Run
    -> Ticket bleibt OPEN (sauberer Fail-Safe)."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        day = dt.date(2026, 4, 22)
        # KEIN regime_*.json angelegt
        should, reason = _decide_health(
            ticket_date=day,
            fp="0fa4f9ddae",
            today=day,
            health_fps_by_date={"2026-04-22": {"0fa4f9ddae"}},
            ticket_code="REGIME_JSON_MISSING",
            tools_dir=tools,
        )
        assert not should, reason
        assert "fp noch in heutigen findings" in reason, reason
        print("[OK] rule 3 blockiert solange Upstream fehlt")


def test_decide_health_rule1_backward_compat():
    """Historisierungs-Regel 1 darf sich durch neue Signatur nicht aendern."""
    with tempfile.TemporaryDirectory() as td:
        tools = Path(td)
        ticket_day = dt.date(2026, 4, 21)
        today = dt.date(2026, 4, 22)
        should, reason = _decide_health(
            ticket_date=ticket_day,
            fp="whatever",
            today=today,
            health_fps_by_date={"2026-04-21": {"whatever", "otherfp"}},
            ticket_code="INSIDER_COVERAGE_GAP",
            tools_dir=tools,
        )
        assert should, reason
        assert "historisiert" in reason, reason
        print("[OK] rule 1 rueckwaertskompatibel")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_timing_stale_regime_present,
        test_timing_stale_premarket_missing,
        test_timing_stale_unknown_code_ignored,
        test_mapping_covers_documented_codes,
        test_decide_health_rule2_wins_when_datahealth_reran,
        test_decide_health_rule3_when_datahealth_not_reran,
        test_decide_health_rule3_blocked_when_artifact_missing,
        test_decide_health_rule1_backward_compat,
    ]
    ok = 0
    for t in tests:
        try:
            t()
            ok += 1
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] {t.__name__}: {e.__class__.__name__}: {e}")
    print(f"\nTOTAL: {ok}/{len(tests)} gruen")
    return 0 if ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
