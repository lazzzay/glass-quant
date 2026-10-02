#!/usr/bin/env python3
"""Tests fuer V7.29 ENTRY-FILTER-FORWARD-TEST-1 (tools/entry_filter_forward_test.py).

Read-only OOS-Forward-Validierung der V7.27-Robust-Kandidaten. Die Tests pruefen:
den OOS-Split + geteilten v61_skip-Filterkontrakt, die numerische/kategoriale
Verdikt-Logik (CONFIRMED/WEAKENED/REFUTED/INSUFFICIENT_DATA), die Aggregation
(oos_proxy_holds nur bei atr_pct CONFIRMED UND keinem REFUTED), das fail-soft
Live-Forward-Geruest, den eingefrorenen Kandidaten-Kontrakt sowie das echte
Backtest-Panel (alle 3 V7.27-Kandidaten halten out-of-sample). Keine
Live-Regel-Aenderung.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import entry_filter_forward_test as fwd  # noqa: E402


# --------------------------------------------------------------------------- #
# OOS-Split + Skip-Filter
# --------------------------------------------------------------------------- #
def test_split_oos_partitions_by_cutoff():
    rows = [
        {"date": "2024-06-01"}, {"date": "2024-12-31"},
        {"date": "2025-01-01"}, {"date": "2026-03-25"},
    ]
    train, test = fwd.split_oos(rows, "2025-01-01")
    assert [r["date"] for r in train] == ["2024-06-01", "2024-12-31"]
    assert [r["date"] for r in test] == ["2025-01-01", "2026-03-25"]


def test_split_oos_drops_undated_rows():
    rows = [{"date": ""}, {"date": "bad"}, {"date": "2025-05-01"}]
    train, test = fwd.split_oos(rows, "2025-01-01")
    assert train == []
    assert len(test) == 1


def test_skip_filter_contract_via_loader():
    # v61_skip == True faellt raus (geteilte load_trades aus V7.26).
    from entry_filter_orthogonality import load_trades
    act = load_trades(fwd._syn_rows(+1.0))
    assert len(act) == 120  # 60 IS + 60 OOS, die Skip-Zeile faellt raus


# --------------------------------------------------------------------------- #
# Numerisches Verdikt
# --------------------------------------------------------------------------- #
def test_verdict_numeric_confirmed():
    assert fwd._verdict_numeric("high_better", 0.20, 100, 40) == "CONFIRMED"
    assert fwd._verdict_numeric("low_better", -0.20, 100, 40) == "CONFIRMED"


def test_verdict_numeric_refuted_on_sign_flip():
    assert fwd._verdict_numeric("high_better", -0.20, 100, 40) == "REFUTED"
    assert fwd._verdict_numeric("low_better", 0.20, 100, 40) == "REFUTED"


def test_verdict_numeric_weakened_below_gate():
    assert fwd._verdict_numeric("high_better", 0.03, 100, 40) == "WEAKENED"
    # auch ein schwaches Gegen-Vorzeichen ist WEAKENED (nicht REFUTED)
    assert fwd._verdict_numeric("high_better", -0.03, 100, 40) == "WEAKENED"


def test_verdict_numeric_insufficient_data():
    assert fwd._verdict_numeric("high_better", 0.50, 10, 40) == "INSUFFICIENT_DATA"
    assert fwd._verdict_numeric("high_better", None, 100, 40) == "INSUFFICIENT_DATA"


# --------------------------------------------------------------------------- #
# Kategoriales Verdikt
# --------------------------------------------------------------------------- #
def test_verdict_categorical_confirmed():
    d = {"n_preferred": 30, "n_avoided": 20, "spread_pp": 3.0}
    assert fwd._verdict_categorical(d, 8) == "CONFIRMED"


def test_verdict_categorical_weakened_small_positive_spread():
    d = {"n_preferred": 30, "n_avoided": 20, "spread_pp": 0.8}
    assert fwd._verdict_categorical(d, 8) == "WEAKENED"


def test_verdict_categorical_refuted_negative_spread():
    d = {"n_preferred": 30, "n_avoided": 20, "spread_pp": -1.0}
    assert fwd._verdict_categorical(d, 8) == "REFUTED"


def test_verdict_categorical_insufficient_small_cell():
    d = {"n_preferred": 3, "n_avoided": 30, "spread_pp": 5.0}
    assert fwd._verdict_categorical(d, 8) == "INSUFFICIENT_DATA"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_decide_holds_requires_atr_confirmed_and_no_refuted():
    per = [
        {"name": "atr_pct", "verdict": "CONFIRMED"},
        {"name": "regime", "verdict": "CONFIRMED"},
        {"name": "rsi14", "verdict": "WEAKENED"},
    ]
    d = fwd.decide(per)
    assert d["oos_proxy_holds"] is True
    assert d["ready_for_true_forward"] is True
    assert d["is_true_forward_test"] is False


def test_decide_blocks_on_any_refuted():
    per = [
        {"name": "atr_pct", "verdict": "CONFIRMED"},
        {"name": "regime", "verdict": "REFUTED"},
        {"name": "rsi14", "verdict": "CONFIRMED"},
    ]
    assert fwd.decide(per)["oos_proxy_holds"] is False


def test_decide_blocks_when_atr_not_confirmed():
    per = [
        {"name": "atr_pct", "verdict": "WEAKENED"},
        {"name": "regime", "verdict": "CONFIRMED"},
        {"name": "rsi14", "verdict": "CONFIRMED"},
    ]
    assert fwd.decide(per)["oos_proxy_holds"] is False


# --------------------------------------------------------------------------- #
# Live-Forward-Geruest (fail-soft)
# --------------------------------------------------------------------------- #
def test_live_forward_not_ready_without_ledger():
    # Hermetisch: expliziter nicht-existenter Pfad, unabhaengig vom realen
    # signals/entry_filter_forward_ledger.json (das der V7.30-Producer schreibt).
    lf = fwd.assess_live_forward("2025-01-01",
                                 ledger_path=Path("/nonexistent/__no_ledger__.json"))
    assert lf["live_forward_ready"] is False
    assert lf["live_forward_n"] == 0
    assert lf["live_forward_eligible_n"] == 0          # V7.31: auch eligible 0
    assert lf["per_candidate"] == []                   # V7.31: kein Ledger -> leer
    assert "COHORT-TAGGING-BACKLOG" in lf["reason"] or "Ledger" in lf["reason"] \
        or "ledger" in lf["reason"].lower()


# --------------------------------------------------------------------------- #
# Eingefrorener Kandidaten-Kontrakt (Werte aus V7.27)
# --------------------------------------------------------------------------- #
def test_registered_candidates_frozen():
    names = [c["name"] for c in fwd.REGISTERED_CANDIDATES]
    assert names == ["atr_pct", "regime", "rsi14"]
    atr = fwd.REGISTERED_CANDIDATES[0]
    assert atr["direction"] == "high_better" and atr["rank"] == 1
    assert abs(atr["is_rank_ic"] - 0.1969) < 1e-6
    reg = fwd.REGISTERED_CANDIDATES[1]
    assert reg["preferred"] == "Trending-Divergent" and reg["avoided"] == "Bullish-Confirmed"
    rsi = fwd.REGISTERED_CANDIDATES[2]
    assert rsi["direction"] == "low_better"


# --------------------------------------------------------------------------- #
# Synthetik-Pfade (kontrolliert)
# --------------------------------------------------------------------------- #
def test_synthetic_holds_path_all_confirmed():
    rep = fwd._build_from_text(fwd._syn_rows(+1.0), "2025-01-01", "t", min_test_n=30)
    v = rep["verdict"]["verdicts"]
    assert v["atr_pct"] == "CONFIRMED"
    assert v["rsi14"] == "CONFIRMED"
    assert v["regime"] == "CONFIRMED"
    assert rep["verdict"]["oos_proxy_holds"] is True


def test_synthetic_refuted_path_atr_flips():
    rep = fwd._build_from_text(fwd._syn_rows(-1.0), "2025-01-01", "t", min_test_n=30)
    assert rep["verdict"]["verdicts"]["atr_pct"] == "REFUTED"
    assert rep["verdict"]["oos_proxy_holds"] is False


# --------------------------------------------------------------------------- #
# Echtes Backtest-Panel: die 3 V7.27-Kandidaten halten out-of-sample
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (ROOT / "v6_backtest_results_v61.csv").exists(),
                    reason="backtest panel not shipped")
def test_real_panel_oos_proxy_holds():
    csv = ROOT / "v6_backtest_results_v61.csv"
    rep = fwd.build_report(csv, cutoff="2025-01-01", run_date="2026-06-23")
    assert "error" not in rep
    assert rep["n_active"] == 258
    assert rep["n_in_sample"] + rep["n_oos_test"] == 258
    v = rep["verdict"]
    # Das genuine H2-Resultat: alle 3 Kandidaten bestaetigen sich OOS.
    assert v["verdicts"]["atr_pct"] == "CONFIRMED"
    assert v["oos_proxy_holds"] is True
    assert v["is_true_forward_test"] is False  # Ehrlichkeits-Grenze maschinenlesbar


@pytest.mark.skipif(not (ROOT / "v6_backtest_results_v61.csv").exists(),
                    reason="backtest panel not shipped")
def test_build_report_is_deterministic():
    csv = ROOT / "v6_backtest_results_v61.csv"
    a = fwd.build_report(csv, cutoff="2025-01-01", run_date="x")
    b = fwd.build_report(csv, cutoff="2025-01-01", run_date="x")
    assert a["verdict"] == b["verdict"]
    assert a["per_candidate"] == b["per_candidate"]


# --------------------------------------------------------------------------- #
# Render + Selftest
# --------------------------------------------------------------------------- #
def test_render_md_has_sections():
    rep = fwd._build_from_text(fwd._syn_rows(+1.0), "2025-01-01", "t", min_test_n=30)
    md = fwd.render_md({**rep, "registered_source": "synthetic"})
    assert "Verdikt" in md and "Forward-Test" in md and "Kandidaten" in md


def test_selftest_passes():
    assert fwd.selftest() == 0


def test_build_report_handles_missing_csv():
    rep = fwd.build_report(ROOT / "does_not_exist_xyz.csv", cutoff="2025-01-01",
                           run_date="x")
    assert "error" in rep


# --------------------------------------------------------------------------- #
# V7.31 LIVE-FORWARD-READINESS-SEMANTICS-1 (Consumer == Producer)
# --------------------------------------------------------------------------- #
def _write_ledger(tmp_path, trades):
    p = tmp_path / "entry_filter_forward_ledger.json"
    p.write_text(json.dumps({"trades": trades}), encoding="utf-8")
    return p


def test_live_forward_gates_on_eligible_not_raw(tmp_path):
    # Kern-Fix: 3 post-cutoff Zeilen, aber nur 1 forward_eligible. Das Gate MUSS
    # auf feature-nutzbare zaehlen (== cohort_tag_ledger.forward_ready), nicht roh.
    led = _write_ledger(tmp_path, [
        {"date": "2025-03-01", "atr_pct": 3.0, "rsi14": 40.0, "return_pct": 2.0,
         "regime": "Trending-Divergent", "forward_eligible": True},
        {"date": "2025-03-02", "atr_pct": None, "rsi14": None, "return_pct": 1.0,
         "regime": "Trending", "forward_eligible": False},
        {"date": "2025-03-03", "atr_pct": None, "rsi14": None, "return_pct": 1.0,
         "regime": "Trending", "forward_eligible": False},
    ])
    lf = fwd.assess_live_forward("2025-01-01", ledger_path=led, min_n=2)
    assert lf["live_forward_n"] == 3            # erfasst (roh)
    assert lf["live_forward_eligible_n"] == 1   # feature-nutzbar (Gating-Numerator)
    assert lf["live_forward_ready"] is False    # 1 < 2 -> NICHT ready trotz raw 3>=2


def test_live_forward_ready_when_eligible_meets_gate(tmp_path):
    led = _write_ledger(tmp_path, [
        {"date": "2025-03-01", "atr_pct": 3.0, "rsi14": 40.0, "return_pct": 2.0,
         "regime": "Trending-Divergent", "forward_eligible": True},
        {"date": "2025-03-02", "atr_pct": 2.0, "rsi14": 30.0, "return_pct": -1.0,
         "regime": "Bullish-Confirmed", "forward_eligible": True},
    ])
    lf = fwd.assess_live_forward("2025-01-01", ledger_path=led, min_n=2)
    assert lf["live_forward_eligible_n"] == 2
    assert lf["live_forward_ready"] is True
    assert lf["lead_ready"] is True             # atr_pct 2 usable >= 2


def test_live_forward_per_candidate_usable_counts(tmp_path):
    led = _write_ledger(tmp_path, [
        {"date": "2025-03-01", "atr_pct": 3.0, "rsi14": 40.0, "return_pct": 2.0,
         "regime": "Trending-Divergent", "forward_eligible": True},
        # nur atr, kein rsi14; Regime grob 'Trending' (NICHT edge-Zelle)
        {"date": "2025-03-02", "atr_pct": 2.0, "rsi14": None, "return_pct": -1.0,
         "regime": "Trending", "forward_eligible": True},
    ])
    lf = fwd.assess_live_forward("2025-01-01", ledger_path=led, min_n=40)
    pc = {c["name"]: c["usable_n"] for c in lf["per_candidate"]}
    assert pc["atr_pct"] == 2          # beide haben atr + return
    assert pc["rsi14"] == 1            # nur die erste hat rsi14
    assert pc["regime"] == 1           # nur Trending-Divergent zaehlt, 'Trending' nicht
    assert lf["lead_candidate"] == "atr_pct"


def test_live_forward_consumer_matches_producer_gate(tmp_path):
    # Konsistenz-Kontrakt: eligible_n des Consumers == Anzahl forward_eligible-Zeilen
    # (genau die Groesse, auf die cohort_tag_ledger.forward_ready gatet).
    trades = [
        {"date": "2025-02-01", "atr_pct": 4.0, "rsi14": 33.0, "return_pct": 3.0,
         "regime": "Trending-Divergent", "forward_eligible": True},
        {"date": "2025-02-02", "atr_pct": None, "rsi14": None, "return_pct": 1.0,
         "regime": "Trending", "forward_eligible": False},
        {"date": "2024-06-01", "atr_pct": 5.0, "rsi14": 20.0, "return_pct": 9.0,
         "regime": "Trending-Divergent", "forward_eligible": True},  # pre-cutoff
    ]
    led = _write_ledger(tmp_path, trades)
    lf = fwd.assess_live_forward("2025-01-01", ledger_path=led, min_n=40)
    producer_eligible_post = sum(
        1 for t in trades if t["date"] >= "2025-01-01" and t["forward_eligible"])
    assert lf["live_forward_eligible_n"] == producer_eligible_post == 1
    assert lf["live_forward_n"] == 2   # nur die beiden post-cutoff Zeilen
