# SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1 (2026-07-06, V7.37): Defense-in-Depth
# fuer den Producer-Guard aus daily_scanner. catch_up_skip() darf einen
# DEGRADIERTEN kanonischen Scan (< Fill-Floor) NICHT als "fertigen" Scan werten,
# sonst wird die 08:45/09:15/10:30-Catch-up-Battery abgewuergt und der
# Throttle-Zombie-NO-TRADE zementiert (Live 07-01 36,8% / 07-02 66,0%).
#
# Vollstaendig offline und stdlib-only: run_morning_pipeline hat KEINE
# yfinance/pandas-Abhaengigkeit -> immer kollektierbar (im Gegensatz zum
# daily_scanner-Teil, der in test_scan_catchup_quality_gate.py wohnt).
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import run_morning_pipeline as rmp  # noqa: E402  (stdlib-only, kein Netz)


def _write_scan(repo: Path, day: str, *, universe=None, valid=None, raw=None) -> Path:
    (repo / "tools").mkdir(exist_ok=True)
    p = repo / "tools" / f"scan_{day}.json"
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
        return p
    payload = {}
    if universe is not None:
        payload["universe_size"] = universe
    if valid is not None:
        payload["valid_results"] = valid
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------- Konstante ----------

def test_catchup_fill_floor_mirrors_data_health():
    # == daily_scanner.VALID_FILL_FLOOR_DEFAULT == data_health SCAN_FILL_LT_80/H.
    assert rmp.CATCHUP_FILL_FLOOR == 0.80


# ---------- catch_up_skip: fill-aware ----------

def test_missing_scan_returns_false(tmp_path):
    (tmp_path / "tools").mkdir()
    assert rmp.catch_up_skip("2099-01-01", repo_dir=tmp_path) is False


def test_healthy_scan_returns_true(tmp_path):
    _write_scan(tmp_path, "2099-01-02", universe=639, valid=620)  # 97,0 %
    assert rmp.catch_up_skip("2099-01-02", repo_dir=tmp_path) is True


def test_boundary_exactly_at_floor_returns_true(tmp_path):
    # 80 % == Floor -> gesund (>= Floor), kein Catch-up.
    _write_scan(tmp_path, "2099-01-03", universe=100, valid=80)
    assert rmp.catch_up_skip("2099-01-03", repo_dir=tmp_path) is True


def test_just_below_floor_returns_false(tmp_path):
    # 79 % < Floor -> degradiert -> Catch-up laeuft.
    _write_scan(tmp_path, "2099-01-04", universe=100, valid=79)
    assert rmp.catch_up_skip("2099-01-04", repo_dir=tmp_path) is False


def test_live_07_01_368pct_returns_false(tmp_path):
    # Live-Reproduktion 07-01: 235/639 = 36,8 % -> Catch-up MUSS laufen.
    _write_scan(tmp_path, "2099-01-05", universe=639, valid=235)
    assert rmp.catch_up_skip("2099-01-05", repo_dir=tmp_path) is False


def test_live_07_02_660pct_returns_false(tmp_path):
    # Live-Reproduktion 07-02: 422/639 = 66,0 % -> ebenfalls unter Floor.
    _write_scan(tmp_path, "2099-01-06", universe=639, valid=422)
    assert rmp.catch_up_skip("2099-01-06", repo_dir=tmp_path) is False


def test_legacy_empty_json_returns_true(tmp_path):
    # Kein universe_size (Legacy/leer) -> nicht bewertbar -> als vorhanden werten
    # (Rueckwaertskompatibilitaet, kein Regress fuer Alt-Artefakte).
    _write_scan(tmp_path, "2099-01-07", raw="{}")
    assert rmp.catch_up_skip("2099-01-07", repo_dir=tmp_path) is True


def test_zero_universe_returns_true(tmp_path):
    # universe_size=0 -> Division vermieden -> als vorhanden werten (kein Crash).
    _write_scan(tmp_path, "2099-01-08", universe=0, valid=0)
    assert rmp.catch_up_skip("2099-01-08", repo_dir=tmp_path) is True


def test_broken_json_returns_false(tmp_path):
    # Unlesbar/kaputt -> sicherheitshalber NICHT skippen.
    _write_scan(tmp_path, "2099-01-09", raw="{not valid json")
    assert rmp.catch_up_skip("2099-01-09", repo_dir=tmp_path) is False


def test_non_numeric_fields_return_false(tmp_path):
    # Kaputte Typen (universe_size="abc") -> TypeError/ValueError-Guard -> False.
    _write_scan(tmp_path, "2099-01-10", raw=json.dumps(
        {"universe_size": "abc", "valid_results": 235}))
    assert rmp.catch_up_skip("2099-01-10", repo_dir=tmp_path) is False


def test_return_type_is_bool(tmp_path):
    _write_scan(tmp_path, "2099-01-11", universe=639, valid=620)
    assert isinstance(rmp.catch_up_skip("2099-01-11", repo_dir=tmp_path), bool)
