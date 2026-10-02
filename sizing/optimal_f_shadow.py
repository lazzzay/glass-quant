#!/usr/bin/env python3
"""
R-2 Shadow-Runner: Daily Delta zwischen half_kelly und optimal_f+Vol-Scaling.

Ziel:
    * Ein-Shot-Skript, das pro Setup *beide* Sizing-Modi auf derselben
      Journal-Grundlage rechnet und die Delta-Groesse (EUR + %-Abweichung)
      in `sizing/_shadow_logs/optimal_f_delta_YYYY-MM-DD.json` persistiert.
    * Kein Live-Scoring, kein Eingriff ins Sizing: Shadow-only.

Default-Laufumgebung:
    python3 sizing/optimal_f_shadow.py --cap 10000 --target-vol 0.15

Usage note:
    Run it once per day after the morning scan (for example via cron/launchd).
    It only writes a shadow log and never changes live sizing.

Free-Stack only. Keine externen Calls: reine Journal-Auswertung.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "sizing" / "_shadow_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Import des Sizers -----------------------------------------------------------
sys.path.insert(0, str(REPO_ROOT))
from tools.kelly_sizer import (  # noqa: E402
    size_for_setup,
    JOURNAL_DB,
    DEFAULT_TARGET_VOL,
    SIZING_MODE_HALF_KELLY,
    SIZING_MODE_OPTIMAL_F,
)


DEFAULT_SETUPS = ("V6.1", "V6")


def run_shadow(capital_eur: float,
               vix: float | None,
               setups: tuple[str, ...] = DEFAULT_SETUPS,
               target_vol: float = DEFAULT_TARGET_VOL,
               db_path: Path = JOURNAL_DB,
               out_dir: Path = LOG_DIR) -> dict:
    today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    rows = []
    for setup in setups:
        entry = {"setup": setup}
        try:
            hk = size_for_setup(
                setup, capital_eur, vix, db_path=db_path,
                sizing_factor=1.0, daily_loss_lock=False, risk_source="bypass",
                sizing_mode=SIZING_MODE_HALF_KELLY, target_vol=target_vol,
            )
            optf = size_for_setup(
                setup, capital_eur, vix, db_path=db_path,
                sizing_factor=1.0, daily_loss_lock=False, risk_source="bypass",
                sizing_mode=SIZING_MODE_OPTIMAL_F, target_vol=target_vol,
            )
            entry["half_kelly"] = asdict(hk)
            entry["optimal_f"] = asdict(optf)
            entry["delta_eur"] = round(optf.size_eur - hk.size_eur, 2)
            base = hk.size_eur if hk.size_eur > 0 else (optf.size_eur or 1.0)
            entry["delta_pct"] = round(
                100.0 * (optf.size_eur - hk.size_eur) / base, 2
            )
        except FileNotFoundError as e:
            entry["error"] = str(e)
        rows.append(entry)

    out = {
        "date": today,
        "capital_eur": capital_eur,
        "vix": vix,
        "target_vol": float(target_vol),
        "setups": rows,
        "meta": {
            "generator": "sizing/optimal_f_shadow.py",
            "note": "Shadow-only; keine Live-Sizing-Aenderung",
        },
    }
    target = out_dir / f"optimal_f_delta_{today}.json"
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    out["_shadow_file"] = str(target)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="R-2 Optimal-f Shadow-Runner")
    p.add_argument("--cap", type=float, default=10_000.0,
                   help="Kapital in EUR (generischer Beispielwert, default 10_000)")
    p.add_argument("--vix", type=float, default=None)
    p.add_argument("--target-vol", type=float, default=DEFAULT_TARGET_VOL)
    p.add_argument("--setup", action="append",
                   help="Setup-Namen (mehrfach verwendbar); ohne = Defaults")
    args = p.parse_args()
    setups = tuple(args.setup) if args.setup else DEFAULT_SETUPS
    out = run_shadow(args.cap, args.vix, setups, args.target_vol)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
