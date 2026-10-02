"""Missing regime inputs must fail loud instead of inventing a regime (offline).

build_snapshot() is pure: it gets already fetched frames (or None) and never
downloads anything. Boundary tests for classify() itself are a starter task in
GOOD_FIRST_ISSUES.md (#4).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

pytest.importorskip("yfinance")  # regime_gate imports yfinance at module level
import regime_gate as rg  # noqa: E402


def _frame(n: int, start: float = 100.0, step: float = 0.2) -> pd.DataFrame:
    close = [start + i * step for i in range(n)]
    return pd.DataFrame({"High": [c + 1 for c in close], "Low": [c - 1 for c in close],
                         "Close": close})


def test_missing_vix_gives_degraded_snapshot_without_invented_values():
    data = {"vix": None, "spy": _frame(30), "tnx": _frame(30, 4.0, 0.01),
            "dxy": _frame(30), "wti": None, "gold": None}
    out, rc = rg.build_snapshot(data, "2099-01-05", "2099-01-05T08:00:00")
    assert rc == rg.RC_DEGRADED == 3
    assert out["status"] == "degraded"
    assert out["regime"] == "Unknown"
    assert out["allowed_tiers"] == [] and out["size_multiplier"] == 0.0
    assert "vix" in out["fallback_metrics"]
    assert "vix" not in out["metrics"]          # no invented VIX value
    assert out["sources"]["vix"].startswith("missing")


def test_missing_spy_also_degrades():
    data = {"vix": _frame(30, 15.0, 0.0), "spy": _frame(5)}
    out, rc = rg.build_snapshot(data, "2099-01-05", "2099-01-05T08:00:00")
    assert rc == 3 and out["status"] == "degraded" and "spy" in out["fallback_metrics"]


def test_only_informational_inputs_missing_is_partial_and_classified():
    data = {"vix": _frame(30, 15.0, 0.0), "spy": _frame(30), "tnx": None, "dxy": None}
    out, rc = rg.build_snapshot(data, "2099-01-05", "2099-01-05T08:00:00")
    assert rc == 0
    assert out["status"] == "partial"
    assert out["regime"] != "Unknown" and out["allowed_tiers"]
    assert set(out["fallback_metrics"]) == {"tnx", "dxy"}
    assert "informational only" in out["sources"]["tnx"]


def test_all_inputs_present_is_ok():
    data = {k: _frame(30, 15.0 if k == "vix" else 100.0, 0.0 if k == "vix" else 0.2)
            for k in rg.TICKERS}
    out, rc = rg.build_snapshot(data, "2099-01-05", "2099-01-05T08:00:00")
    assert rc == 0 and out["status"] == "ok" and out["fallback_metrics"] == []


def test_data_health_flags_degraded_regime_as_critical(tmp_path, monkeypatch):
    import data_health as dh
    monkeypatch.setattr(dh, "TOOLS_DIR", tmp_path)
    data = {"vix": None, "spy": None}
    out, _ = rg.build_snapshot(data, "2099-01-05", "2099-01-05T08:00:00")
    (tmp_path / "regime_2099-01-05.json").write_text(json.dumps(out), encoding="utf-8")
    summary, findings = dh.check_source_json("2099-01-05", "regime", [])
    codes = [f["code"] for f in findings if f["severity"] == dh.SEV_H]
    assert codes == ["REGIME_DEGRADED"]
    assert summary["status"] == dh.STATUS_FAIL
