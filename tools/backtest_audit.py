#!/usr/bin/env python3
"""backtest_audit.py: V7.40: Kanonisches Kostenmodell (M12) + Panel-Wächter.

Beantwortet dauerhaft drei Fragen über v6_backtest_results_v61.csv:
  1. INTEGRITÄT: Dubletten, Nulls, Entry/Exit-vs-Return-Konsistenz, Ausreißer.
  2. EXPECTANCY unter dem EINEN kanonischen Kostenmodell (M12):
     Referenz-Notional 1.500 €, flat broker fee 2,00 €/Roundtrip
     → net_eur = return_pct/100 × 1500 − 2. Das alte CSV-`netto_pnl`
     (500-€-Modell, Ø 4,25 € Kosten) ist DEPRECATED und wird nur noch als
     Split-Brain-Diff berichtet; ebenso der DB-Wert (source='backtest').
  3. DECAY/STALENESS: Jahres-Trend, jüngste Scheibe vs. Panel, Panel-Alter.

Read-only. Schreibt signals/backtest_audit_<date>.json + Briefings/Backtest-Audit-<date>.md.
CLI: --selftest | [--write] [--csv PATH] [--db PATH] [--recent-n N] [--base DIR]
Exit: 0 ok · 2 Findings (Decay/Stale/Integrität) · 1 Fehler.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from statistics import mean
from typing import Any, Dict, List, Optional

CANONICAL_COST_MODEL = {"notional_eur": 1500.0, "cost_round_trip_eur": 2.0}
OUTLIER_ABS_RET = 15.0
STALE_DAYS = 45
DECAY_TOL_EUR = 2.0  # jüngste Scheibe darf max. so viel unter Panel-Mean liegen


def repo_base() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def net_eur(ret_pct: float) -> float:
    m = CANONICAL_COST_MODEL
    return ret_pct / 100.0 * m["notional_eur"] - m["cost_round_trip_eur"]


def load_rows(csv_path: str) -> List[dict]:
    with open(csv_path, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def integrity(rows: List[dict]) -> Dict[str, Any]:
    dupes = [k for k, c in Counter((r["date"], r["ticker"]) for r in rows).items() if c > 1]
    nulls = sum(1 for r in rows for f in ("return_pct", "entry", "exit") if not r.get(f))
    math_err, outliers = 0, []
    for r in rows:
        try:
            e, x, rp = float(r["entry"]), float(r["exit"]), float(r["return_pct"])
            if abs((x - e) / e * 100 - rp) > 0.05:
                math_err += 1
            if abs(rp) > OUTLIER_ABS_RET:
                outliers.append({"ticker": r["ticker"], "date": r["date"],
                                 "ret": round(rp, 1)})
        except (ValueError, ZeroDivisionError):
            math_err += 1
    return {"n": len(rows), "duplicates": len(dupes), "nulls": nulls,
            "math_errors": math_err, "outliers": outliers,
            "clean": not dupes and nulls == 0 and math_err == 0}


def bucket_stats(rows: List[dict], key) -> Dict[str, Dict[str, float]]:
    groups: Dict[str, List[float]] = {}
    for r in rows:
        try:
            groups.setdefault(key(r), []).append(float(r["return_pct"]))
        except ValueError:
            continue
    out = {}
    for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        nets = [net_eur(x) for x in v]
        out[k] = {"n": len(v), "mean_ret_pct": round(mean(v), 3),
                  "wr_pct": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
                  "mean_net_eur": round(mean(nets), 2),
                  "sum_net_eur": round(sum(nets), 2)}
    return out


def decay_verdict(rows: List[dict], recent_n: int, as_of: dt.date) -> Dict[str, Any]:
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    rets = [float(r["return_pct"]) for r in rows_sorted]
    panel_net = mean(net_eur(x) for x in rets)
    recent = rets[-recent_n:] if len(rets) >= recent_n else rets
    recent_net = mean(net_eur(x) for x in recent)
    last_date = dt.date.fromisoformat(rows_sorted[-1]["date"])
    stale_days = (as_of - last_date).days
    if recent_net < 0:
        verdict = "NEGATIVE_RECENT"
    elif recent_net < panel_net - DECAY_TOL_EUR:
        verdict = "DECAY_WATCH"
    else:
        verdict = "OK"
    return {"panel_mean_net_eur": round(panel_net, 2),
            "recent_n": len(recent), "recent_mean_net_eur": round(recent_net, 2),
            "panel_last_date": last_date.isoformat(), "stale_days": stale_days,
            "stale": stale_days > STALE_DAYS, "verdict": verdict}


def split_brain(rows: List[dict], db_path: Optional[str]) -> Dict[str, Any]:
    csv_legacy = round(sum(float(r.get("netto_pnl") or 0) for r in rows), 2)
    canonical = round(sum(net_eur(float(r["return_pct"])) for r in rows), 2)
    db_sum = None
    if db_path and os.path.exists(db_path):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            db_sum = con.execute(
                "SELECT ROUND(SUM(netto_pnl),2) FROM trades WHERE source='backtest'"
            ).fetchone()[0]
            con.close()
        except sqlite3.Error:
            db_sum = None
    return {"canonical_sum_net_eur": canonical,
            "csv_legacy_sum_eur (DEPRECATED 500€-Modell)": csv_legacy,
            "db_backtest_sum_eur (informativ)": db_sum,
            "canonical_model": CANONICAL_COST_MODEL}


def run_audit(base: str, csv_path: str, db_path: Optional[str], recent_n: int,
              as_of: Optional[dt.date] = None) -> Dict[str, Any]:
    as_of = as_of or dt.date.today()
    rows = load_rows(csv_path)
    rep: Dict[str, Any] = {"tool": "backtest_audit", "version": "V7.40",
                           "as_of": as_of.isoformat(),
                           "integrity": integrity(rows),
                           "cost_model": CANONICAL_COST_MODEL,
                           "by_year": bucket_stats(rows, lambda r: r["date"][:4]),
                           "by_regime": bucket_stats(rows, lambda r: r.get("regime", "?")),
                           "by_tier": bucket_stats(rows, lambda r: r.get("tier", "?")),
                           "decay": decay_verdict(rows, recent_n, as_of),
                           "split_brain": split_brain(rows, db_path)}
    flags = []
    if not rep["integrity"]["clean"]:
        flags.append("INTEGRITY")
    if rep["decay"]["verdict"] != "OK":
        flags.append(rep["decay"]["verdict"])
    if rep["decay"]["stale"]:
        flags.append("PANEL_STALE")
    rep["flags"] = flags
    return rep


def write_outputs(base: str, rep: Dict[str, Any]) -> None:
    date = rep["as_of"]
    sig = os.path.join(base, "signals", f"backtest_audit_{date}.json")
    os.makedirs(os.path.dirname(sig), exist_ok=True)
    with open(sig, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1, ensure_ascii=False)
    d = rep["decay"]
    lines = [f"# Backtest-Audit {date} (kanonisches Kostenmodell: 1.500 € / 2 € Roundtrip)",
             "",
             f"Integrität: n={rep['integrity']['n']}, Dubletten {rep['integrity']['duplicates']}, "
             f"Nulls {rep['integrity']['nulls']}, Math-Fehler {rep['integrity']['math_errors']} → "
             f"{'SAUBER' if rep['integrity']['clean'] else 'FINDINGS'}",
             f"Panel-Expectancy: Ø {d['panel_mean_net_eur']:+.2f} €/Trade · "
             f"jüngste {d['recent_n']}: Ø {d['recent_mean_net_eur']:+.2f} € → **{d['verdict']}**",
             f"Panel-Ende: {d['panel_last_date']} ({d['stale_days']} Tage alt"
             f"{' STALE, Walk-Forward fällig' if d['stale'] else ''})",
             "", "## Nach Jahr", ""]
    for y, s in rep["by_year"].items():
        lines.append(f"- {y}: n={s['n']} · Ø {s['mean_ret_pct']:+.2f} % · WR {s['wr_pct']} % · "
                     f"Ø netto {s['mean_net_eur']:+.2f} €")
    lines += ["", f"Flags: {', '.join(rep['flags']) or 'keine'}",
              "", "*Read-only. CSV-`netto_pnl` (500-€-Modell) ist deprecated; dieser Report ist die kanonische P&L-Wahrheit (M12).*"]
    with open(os.path.join(base, "Briefings", f"Backtest-Audit-{date}.md"),
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ── selftest ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("backtest_audit --selftest (offline, tmpdir)")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "bt.csv")
        hdr = "date,ticker,tier,regime,entry,exit,return_pct,netto_pnl\n"
        rows = []
        # 2024: 3 gute Trades (+1 %), 2026: 2 schlechte (−1 %)
        for i, (d, rp) in enumerate([("2024-01-10", 1.0), ("2024-02-10", 1.0),
                                     ("2024-03-10", 1.0), ("2026-01-10", -1.0),
                                     ("2026-02-10", -1.0)]):
            e = 100.0; x = e * (1 + rp / 100)
            rows.append(f"{d},T{i},A,Trending,{e},{x},{rp},0")
        open(p, "w").write(hdr + "\n".join(rows))

        rep = run_audit(tmp, p, db_path=None, recent_n=2,
                        as_of=dt.date(2026, 7, 8))
        check("G1 Integrität sauber", rep["integrity"]["clean"])
        # kanonisches Netto: +1 % → 15−2=+13 €; −1 % → −15−2=−17 €
        check("G2 kanonisches Netto korrekt (+13/−17)",
              abs(rep["by_year"]["2024"]["mean_net_eur"] - 13.0) < 1e-9
              and abs(rep["by_year"]["2026"]["mean_net_eur"] + 17.0) < 1e-9)
        check("G3 Decay: jüngste Scheibe negativ → NEGATIVE_RECENT",
              rep["decay"]["verdict"] == "NEGATIVE_RECENT")
        check("G4 Staleness erkannt (Panel endet Feb, as_of Jul)",
              rep["decay"]["stale"] is True and "PANEL_STALE" in rep["flags"])

        # G5 Dublette + Math-Fehler erkannt
        open(p, "a").write("\n2024-01-10,T0,A,Trending,100,105,1.0,0")
        rep2 = run_audit(tmp, p, None, 2, as_of=dt.date(2026, 7, 8))
        check("G5 Dublette + Entry/Exit-Mismatch erkannt",
              rep2["integrity"]["duplicates"] == 1
              and rep2["integrity"]["math_errors"] == 1
              and "INTEGRITY" in rep2["flags"])

        # G6 Outputs schreiben
        os.makedirs(os.path.join(tmp, "Briefings"), exist_ok=True)
        write_outputs(tmp, rep)
        check("G6 JSON + MD geschrieben",
              os.path.exists(os.path.join(tmp, "signals",
                                          "backtest_audit_2026-07-08.json"))
              and os.path.exists(os.path.join(tmp, "Briefings",
                                              "Backtest-Audit-2026-07-08.md")))

    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    base = repo_base()
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    csv_path = os.path.join(base, "v6_backtest_results_v61.csv")
    if "--csv" in argv:
        csv_path = argv[argv.index("--csv") + 1]
    db_path = os.path.join(base, "trade_journal.sqlite")
    if "--db" in argv:
        db_path = argv[argv.index("--db") + 1]
    recent_n = int(argv[argv.index("--recent-n") + 1]) if "--recent-n" in argv else 90
    rep = run_audit(base, csv_path, db_path, recent_n)
    if "--write" in argv:
        write_outputs(base, rep)
    print(json.dumps({k: rep[k] for k in ("as_of", "integrity", "decay",
                                          "split_brain", "flags")},
                     indent=1, ensure_ascii=False, default=str))
    sys.exit(0 if not rep["flags"] else 2)
