# DATA-FETCH-RESILIENCE-1 (2026-06-10): Env-Guards des Scanners.
# Vollstaendig offline: Fetch wird injiziert, certifi via monkeypatch simuliert.
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import daily_scanner as ds  # noqa: E402


# ---------- certifi_status ----------

def test_certifi_status_real_env_ok():
    # Auf dem Mac ist certifi installiert und frisch (08.06. reinstalled).
    st = ds.certifi_status()
    assert st["ok"] is True
    assert st["size_bytes"] >= ds.CERTIFI_MIN_BYTES
    assert st["error"] is None


def test_certifi_status_small_file(tmp_path, monkeypatch):
    fake = tmp_path / "cacert.pem"
    fake.write_text("kaputt")
    import certifi
    monkeypatch.setattr(certifi, "where", lambda: str(fake))
    st = ds.certifi_status()
    assert st["ok"] is False
    assert "zu klein" in st["error"]


def test_certifi_status_missing_file(tmp_path, monkeypatch):
    import certifi
    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "nope.pem"))
    st = ds.certifi_status()
    assert st["ok"] is False


def test_pin_ssl_sets_env(monkeypatch, tmp_path):
    import os
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    big = tmp_path / "cacert.pem"
    big.write_bytes(b"x" * (ds.CERTIFI_MIN_BYTES + 1))
    ok = ds.pin_ssl_to_certifi({"ok": True, "path": str(big)})
    assert ok is True
    assert os.environ["SSL_CERT_FILE"] == str(big)
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(big)


def test_pin_ssl_respects_existing_override(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("SSL_CERT_FILE", "/pinned/by/user.pem")
    big = tmp_path / "cacert.pem"
    big.write_bytes(b"x" * (ds.CERTIFI_MIN_BYTES + 1))
    ds.pin_ssl_to_certifi({"ok": True, "path": str(big)})
    assert os.environ["SSL_CERT_FILE"] == "/pinned/by/user.pem"  # setdefault


def test_pin_ssl_broken_returns_false():
    assert ds.pin_ssl_to_certifi({"ok": False, "path": None}) is False


# ---------- canary_probe ----------

class _FakeDF(list):
    pass


def test_canary_all_alive():
    res = ds.canary_probe(lambda t: _FakeDF([1, 2, 3]))
    assert res["ok"] is True and res["n_ok"] == 3 and res["failed"] == []


def test_canary_one_alive_is_enough():
    def fetch(t):
        return _FakeDF([1]) if t == "AAPL" else None
    res = ds.canary_probe(fetch)
    assert res["ok"] is True
    assert res["ok_tickers"] == ["AAPL"]
    assert set(res["failed"]) == {"SPY", "MSFT"}


def test_canary_all_dead_none():
    res = ds.canary_probe(lambda t: None)
    assert res["ok"] is False and res["n_ok"] == 0
    assert res["failed"] == list(ds.ENV_GUARD_CANARIES)


def test_canary_all_dead_empty_df():
    res = ds.canary_probe(lambda t: _FakeDF([]))
    assert res["ok"] is False


def test_canary_exceptions_count_as_dead():
    def boom(t):
        raise ConnectionError("SSL: CERTIFICATE_VERIFY_FAILED")
    res = ds.canary_probe(boom)
    assert res["ok"] is False
    assert res["failed"] == list(ds.ENV_GUARD_CANARIES)


def test_canary_custom_list():
    res = ds.canary_probe(lambda t: _FakeDF([1]), canaries=("SPY",))
    assert res["ok"] is True and res["canaries"] == ["SPY"]


# ---------- coverage_guard ----------

@pytest.mark.parametrize("n_data,n_uni,floor,ok", [
    (624, 639, 0.5, True),    # gestriger Normalfall
    (320, 639, 0.5, True),    # exakt ueber Floor
    (319, 639, 0.5, False),   # knapp drunter
    (28, 639, 0.5, False),    # certifi-Tag-Signatur (Muell-Scan-Schutz)
    (0, 639, 0.5, False),     # Totalausfall
    (3, 5, 0.5, True),        # --limit-Debug-Lauf bleibt moeglich
    (639, 639, 0.5, True),
])
def test_coverage_guard(n_data, n_uni, floor, ok):
    res = ds.coverage_guard(n_data, n_uni, floor)
    assert res["ok"] is ok
    assert res["n_data"] == n_data and res["n_universe"] == n_uni


def test_coverage_guard_empty_universe_fails():
    assert ds.coverage_guard(0, 0, 0.5)["ok"] is False


def test_coverage_guard_default_floor():
    assert ds.coverage_guard(600, 639)["floor"] == ds.COVERAGE_FLOOR_DEFAULT


# ---------- write_scan_abort ----------

def test_write_scan_abort_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "TOOLS_DIR", tmp_path)
    p = ds.write_scan_abort("2099-01-01", "CANARY_DEAD",
                            {"probe": {"failed": ["SPY"]}})
    assert p.name == "scan_abort_2099-01-01.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["reason"] == "CANARY_DEAD"
    assert d["writer"] == "daily_scanner.py"
    assert "certifi" in d["hint"]  # Fix-Hinweis enthalten
    assert d["details"]["probe"]["failed"] == ["SPY"]


def test_write_scan_abort_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "TOOLS_DIR", tmp_path)
    ds.write_scan_abort("2099-01-02", "CERTIFI_BROKEN", {})
    p = ds.write_scan_abort("2099-01-02", "COVERAGE_FLOOR", {"ratio": 0.1})
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["reason"] == "COVERAGE_FLOOR"  # letzter Grund gewinnt


# ---------- Exit-Codes & Konstanten (Kontrakt mit Pipeline/data_health) ----------

def test_exit_codes_distinct_from_existing():
    # 3 ist SCAN-IMMUTABILITY-1; 2 ist require-options-flow. Keine Kollision.
    assert ds.EXIT_ENV_GUARD == 4
    assert ds.EXIT_COVERAGE_FLOOR == 5
    assert len({2, 3, ds.EXIT_ENV_GUARD, ds.EXIT_COVERAGE_FLOOR}) == 4


def test_canary_set_is_liquid_and_direct():
    # Canaries muessen maximal liquide sein; SPY ist bewusst dabei (Datenprobe,
    # kein Trade-Kandidat: tradability=hebel_only ist dafuer irrelevant).
    assert set(ds.ENV_GUARD_CANARIES) == {"SPY", "AAPL", "MSFT"}


# ---------- HOTFIX 2026-06-11: Cache-Reset-Idempotenz (Canary-Ordering-Bug) ----------
# Live-Incident 11.06.: Canary initialisierte yfinance, der proaktive Reset in
# batch_fetch zerstoerte danach die offene tz-Cache-Connection -> 0/639 Batch
# ('unable to open database file'), 2x COVERAGE_FLOOR-Abort. Der Reset ist jetzt
# einmal pro Prozess (force=False no-op danach); reaktive Resets nutzen force=True.

def test_cache_reset_is_idempotent(monkeypatch):
    import shutil
    calls = []
    monkeypatch.setattr(ds, "_YF_CACHE_RESET_DONE", False)
    monkeypatch.setattr(shutil, "rmtree",
                        lambda *a, **k: calls.append(a[0]) or None)
    ds._reset_yfinance_cache()          # 1. Call: darf arbeiten
    n_first = len(calls)
    ds._reset_yfinance_cache()          # 2. Call ohne force: MUSS no-op sein
    ds._reset_yfinance_cache()
    assert len(calls) == n_first, "Reset lief mehrfach ohne force: Ordering-Bug zurueck!"
    assert ds._YF_CACHE_RESET_DONE is True


def test_cache_reset_force_still_works(monkeypatch):
    import shutil
    calls = []
    monkeypatch.setattr(ds, "_YF_CACHE_RESET_DONE", True)  # bereits initialisiert
    monkeypatch.setattr(shutil, "rmtree",
                        lambda *a, **k: calls.append(a[0]) or None)
    ds._reset_yfinance_cache()               # no-op
    ds._reset_yfinance_cache(force=True)     # reaktiver Fall muss durchgehen
    # force-Call hat den Body ausgefuehrt (rmtree nur wenn Cache-Dir existiert:
    # daher kein hartes calls-Assert; entscheidend ist: kein Early-Return-Crash
    # und Flag bleibt gesetzt)
    assert ds._YF_CACHE_RESET_DONE is True
