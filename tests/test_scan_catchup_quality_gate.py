# SCAN-CATCHUP-QUALITY-GATE-1 (2026-06-29, V7.32): Valid-Results-Fill-Gate des
# Scanners. Vollstaendig offline: testet nur die reinen Guard-/Abort-Helfer und
# die Konstanten/Exit-Codes (kein Netz, kein main()-Aufruf).
#
# Bug-Kontext (ops_tickets 2026-06-18 M): coverage_guard misst FETCH-Coverage und
# bestand an Throttle-Tagen, waehrend die VALID-RESULTS-Fill auf 24..50 % fiel
# (06-18 24,4 / 06-19 44,6 / 06-22 27,7 / 06-23 50,1 %). Der degradierte-aber-
# vorhandene Scan ueberlistete catch_up_skip() (Existenz-only) -> Ganztages-
# NO-TRADE. Fix: valid_fill_guard + Abort Exit 6 OHNE Artefakt -> Catch-up ersetzt.
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import daily_scanner as ds  # noqa: E402


# ---------- Konstanten / Exit-Codes ----------

def test_valid_fill_floor_default_mirrors_data_health_h_threshold():
    # data_health.py: `if fill < 0.80:` -> SEV_H. Der Gate-Floor MUSS dem
    # entsprechen, damit ein "data_health=CRITICAL"-Scan als fehlend gilt.
    assert ds.VALID_FILL_FLOOR_DEFAULT == 0.80


def test_exit_valid_fill_floor_is_six_and_distinct():
    assert ds.EXIT_VALID_FILL_FLOOR == 6
    # kollisionsfrei zu allen bestehenden Exit-Codes (2=OF, 3=immutability,
    # 4=env-guard, 5=coverage-floor).
    assert len({2, 3, ds.EXIT_ENV_GUARD, ds.EXIT_COVERAGE_FLOOR,
                ds.EXIT_VALID_FILL_FLOOR}) == 5


# ---------- valid_fill_guard ----------

@pytest.mark.parametrize("n_valid,n_uni,floor,ok", [
    (639, 639, 0.80, True),    # 100 %
    (615, 639, 0.80, True),    # 96,2 % (Live 06-29, healthy)
    (517, 639, 0.80, True),    # 80,9 % (06-24, knapp ueber Floor -> behalten)
    (511, 639, 0.80, False),   # 79,97 % (knapp unter Floor)
    (320, 639, 0.80, False),   # 50,1 % (06-23 throttle)
    (285, 639, 0.80, False),   # 44,6 % (06-19 throttle)
    (177, 639, 0.80, False),   # 27,7 % (06-22 throttle)
    (156, 639, 0.80, False),   # 24,4 % (06-18 throttle, Ursprungsfall)
])
def test_valid_fill_guard_incident_days(n_valid, n_uni, floor, ok):
    res = ds.valid_fill_guard(n_valid, n_uni, floor)
    assert res["ok"] is ok
    assert res["n_valid"] == n_valid
    assert res["n_universe"] == n_uni
    assert res["floor"] == floor


def test_valid_fill_guard_boundary_is_inclusive():
    # Genau auf dem Floor (>=) gilt als OK.
    assert ds.valid_fill_guard(80, 100, 0.80)["ok"] is True
    assert ds.valid_fill_guard(79, 100, 0.80)["ok"] is False


def test_valid_fill_guard_empty_universe_fails():
    res = ds.valid_fill_guard(0, 0, 0.80)
    assert res["ok"] is False
    assert res["ratio"] == 0.0


def test_valid_fill_guard_default_floor():
    res = ds.valid_fill_guard(600, 639)
    assert res["floor"] == ds.VALID_FILL_FLOOR_DEFAULT


def test_valid_fill_guard_ratio_rounded():
    res = ds.valid_fill_guard(156, 639, 0.80)
    assert res["ratio"] == round(156 / 639, 4)  # 0.2441


def test_all_historical_throttle_days_would_have_self_healed():
    # Regression-Anker: die 4 dokumentierten Throttle-Tage waeren mit dem Gate
    # KEIN geschriebenes Artefakt -> catch_up_skip=False -> Re-Run moeglich.
    throttle_valid_counts = [156, 285, 177, 320]  # 06-18/19/22/23
    for n in throttle_valid_counts:
        assert ds.valid_fill_guard(n, 639)["ok"] is False
    # ... waehrend die gesunden/grenzwertigen Tage behalten wuerden:
    for n in [517, 615, 623, 624, 639]:  # 80,9 / 96,2 / 97,5 / 97,7 / 100 %
        assert ds.valid_fill_guard(n, 639)["ok"] is True


# ---------- write_scan_abort mit VALID_FILL_FLOOR ----------

def test_write_scan_abort_valid_fill_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "TOOLS_DIR", tmp_path)
    details = ds.valid_fill_guard(156, 639, 0.80)
    p = ds.write_scan_abort("2099-03-03", "VALID_FILL_FLOOR", details)
    assert p.exists()
    payload = json.loads(p.read_text(encoding="utf-8"))
    assert payload["reason"] == "VALID_FILL_FLOOR"
    assert payload["details"]["n_valid"] == 156
    assert "VALID_FILL_FLOOR" in payload["hint"]
    assert payload["writer"] == "daily_scanner.py"


def test_write_scan_abort_filename_matches_date(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "TOOLS_DIR", tmp_path)
    p = ds.write_scan_abort("2099-03-04", "VALID_FILL_FLOOR",
                            ds.valid_fill_guard(100, 639))
    assert p.name == "scan_abort_2099-03-04.json"


# ---------- Parität zu coverage_guard (Spiegel-API) ----------

def test_valid_fill_guard_mirrors_coverage_guard_shape():
    v = ds.valid_fill_guard(300, 600, 0.5)
    c = ds.coverage_guard(300, 600, 0.5)
    # gleiche Schluessel-Semantik (ok/ratio/floor/n_universe), nur n_valid vs n_data
    assert v["ok"] == c["ok"]
    assert v["ratio"] == c["ratio"]
    assert v["floor"] == c["floor"]
    assert v["n_universe"] == c["n_universe"]
    assert v["n_valid"] == c["n_data"]


# ---------- fill_guard_applies (SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1, V7.37) ----------
# Bug-Kontext (ops_tickets 2026-07-01/02 M): scan_initial bricht per Gate ab
# (Exit 6, KEIN Kanon) -> `scan_rescore --rescan` nimmt in write_immutable() den
# First-Write-Zweig -> schreibt den DEGRADIERTEN Scan (36,8%/66,0%) in den
# KANONISCHEN Namespace -> catch_up_skip() (Existenz-only) SKIP -> Throttle-Zombie.
# Fix: der valid_fill_guard greift AUCH bei --rescan, sobald der Kanon fehlt.

def test_guard_applies_on_normal_scan_regardless_of_canon():
    # scan_initial (nicht rescan): Guard IMMER, unabhaengig von Kanon-Existenz.
    assert ds.fill_guard_applies(is_rescan=False, canonical_exists=False) is True
    assert ds.fill_guard_applies(is_rescan=False, canonical_exists=True) is True


def test_guard_applies_on_rescan_as_producer_when_canon_absent():
    # DER FIX: --rescan OHNE kanonischen Scan = Ersatz-Producer -> Guard greift.
    assert ds.fill_guard_applies(is_rescan=True, canonical_exists=False) is True


def test_guard_skipped_on_rescan_over_existing_canon():
    # Normaler Re-Score ueber gesundem Kanon -> ungated (forensischer Namespace,
    # von catch_up_skip ignoriert). Verhalten VOR dem Fix bleibt hier erhalten.
    assert ds.fill_guard_applies(is_rescan=True, canonical_exists=True) is False


def test_guard_predicate_pure_boolean():
    for r in (True, False):
        for c in (True, False):
            assert isinstance(ds.fill_guard_applies(r, c), bool)
