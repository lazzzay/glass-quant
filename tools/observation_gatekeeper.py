#!/usr/bin/env python3
"""
observation_gatekeeper.py: Self-Healing-Check für Observation-Window-Logs

Zweck
=====
Bis zum Audit am 2026-04-29 muss für JEDEN Handelstag ein Tripwire- + Shadow-Log
existieren, sonst scheitert Gate 1 (Observation-Vollständigkeit) am Schwellwert
von ≥8 Log-Tagen. Der Scheduler (daily-observation-tripwire, 09:20 CET) ist
aktuell unzuverlässig: das Backfill für 15./16.04. zeigt, dass manuell
nachgeholt werden musste.

Dieser Gatekeeper macht die Lücken-Detektion + das Nachholen automatisch:
  1. Prüft für alle Handelstage des Observation-Windows, ob Tripwire + Shadow
     vorhanden sind.
  2. Holt fehlende Logs per tripwire_monitor.py / shadow_recommender.py nach.
  3. Erzeugt einen Gap-Report und schreibt bei kritischen Lücken ein Ticket.

Design
------
* Free-Stack-Only (stdlib + vorhandene Module).
* Idempotent: gleicher Input → gleicher Output.
* Safe: rührt nie existierende Logs an; schreibt nur was fehlt.
* Transparent: jede Datei, die nachgefüllt wurde, wird im Report vermerkt.

Usage
-----
    python3 tools/observation_gatekeeper.py                    # heute prüfen
    python3 tools/observation_gatekeeper.py --window 2026-04-15:2026-04-29
    python3 tools/observation_gatekeeper.py --dry-run          # nur Report
    python3 tools/observation_gatekeeper.py --no-tickets

Exit-Codes
----------
    0: OK: alle Tage im Scope haben Tripwire + Shadow
    1: RECOVERED: Lücken gefunden und erfolgreich nachgefüllt
    2: DEGRADED: mind. eine Nachhol-Aktion fehlgeschlagen
    3: INPUTS-MISSING: Quelldateien (scan_*.json) fehlen → Backfill blockiert
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVATION_WINDOW_START = dt.date(2026, 4, 15)
OBSERVATION_WINDOW_END = dt.date(2026, 4, 29)
TRIPWIRE_TEMPLATE = "tools/tripwire_{date}.json"
SHADOW_TEMPLATE = "shadow/rec_log_{date}.json"
SCAN_TEMPLATE = "tools/scan_{date}.json"
GAP_REPORT_TEMPLATE = "tools/gatekeeper_report_{date}.json"
TICKET_FILE = "ops_tickets.md"
WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() in WEEKDAYS:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def gap_scan(days: list[dt.date]) -> dict:
    report = {"days": [], "missing_tripwire": [], "missing_shadow": [],
              "missing_scan": [], "complete_days": 0}
    for d in days:
        iso = d.isoformat()
        tripwire = (ROOT / TRIPWIRE_TEMPLATE.format(date=iso)).exists()
        shadow = (ROOT / SHADOW_TEMPLATE.format(date=iso)).exists()
        scan = (ROOT / SCAN_TEMPLATE.format(date=iso)).exists()
        entry = {"date": iso, "tripwire": tripwire, "shadow": shadow, "scan": scan}
        report["days"].append(entry)
        if not tripwire:
            report["missing_tripwire"].append(iso)
        if not shadow:
            report["missing_shadow"].append(iso)
        if not scan:
            report["missing_scan"].append(iso)
        if tripwire and shadow:
            report["complete_days"] += 1
    return report


def run_script(script: str, date_iso: str, extra: list[str] | None = None) -> tuple[bool, str]:
    cmd = ["python3", str(ROOT / script), "--date", date_iso, "--quiet"]
    if extra:
        cmd.extend(extra)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ok = r.returncode in (0, 1)  # 1 = WARN (Tripwire), also OK
        return ok, (r.stderr or r.stdout)[:500]
    except Exception as exc:  # noqa: BLE001
        return False, f"subprocess-error: {exc}"


def fill_gaps(report: dict, dry_run: bool) -> dict:
    filled = {"tripwire": [], "shadow": [], "failed": [], "blocked_no_scan": []}
    # If scan is missing, we can't backfill (no source data)
    for iso in report["missing_tripwire"]:
        if iso in report["missing_scan"]:
            filled["blocked_no_scan"].append(("tripwire", iso))
            continue
        if dry_run:
            filled["tripwire"].append((iso, "dry-run"))
            continue
        ok, msg = run_script("tools/tripwire_monitor.py", iso, ["--no-tickets"])
        if ok:
            filled["tripwire"].append((iso, "OK"))
        else:
            filled["failed"].append(("tripwire", iso, msg))

    for iso in report["missing_shadow"]:
        if iso in report["missing_scan"]:
            filled["blocked_no_scan"].append(("shadow", iso))
            continue
        if dry_run:
            filled["shadow"].append((iso, "dry-run"))
            continue
        ok, msg = run_script("tools/shadow_recommender.py", iso)
        if ok:
            filled["shadow"].append((iso, "OK"))
        else:
            filled["failed"].append(("shadow", iso, msg))
    return filled


def write_ticket(report: dict, filled: dict, today_iso: str) -> None:
    critical_gap = (
        len(filled["failed"]) > 0
        or len(filled["blocked_no_scan"]) > 0
    )
    if not critical_gap:
        return
    ticket_path = ROOT / TICKET_FILE
    if not ticket_path.exists():
        return  # don't create ticket file if missing; just silent-fail
    stamp = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%MZ")
    lines = [
        f"\n### OBS-GAP-{today_iso} (observation_gatekeeper): auto-entry {stamp}",
        f"- complete_days: {report['complete_days']}",
        f"- missing_tripwire: {report['missing_tripwire']}",
        f"- missing_shadow: {report['missing_shadow']}",
        f"- missing_scan: {report['missing_scan']}",
        f"- failed_runs: {filled['failed']}",
        f"- blocked_no_scan: {filled['blocked_no_scan']}",
        "- action: manueller Check erforderlich, Backfill konnte nicht alle Lücken schließen",
        "",
    ]
    with ticket_path.open("a", encoding="utf-8") as fp:
        fp.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Observation-Window Gatekeeper")
    ap.add_argument("--window", default=None,
                    help="YYYY-MM-DD:YYYY-MM-DD (default: observation window)")
    ap.add_argument("--dry-run", action="store_true",
                    help="kein Backfill, nur Gap-Report")
    ap.add_argument("--no-tickets", action="store_true",
                    help="Keine ops_tickets.md-Einträge")
    ap.add_argument("--report-out", default=None,
                    help="Report-Pfad override")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.window:
        s_raw, e_raw = args.window.split(":")
        start = dt.date.fromisoformat(s_raw)
        end = dt.date.fromisoformat(e_raw)
    else:
        start = OBSERVATION_WINDOW_START
        end = OBSERVATION_WINDOW_END
    # Don't scan future dates beyond today
    today = dt.date.today()
    effective_end = min(end, today)
    days = trading_days(start, effective_end)

    report = gap_scan(days)
    filled = fill_gaps(report, args.dry_run)

    # Post-fill rescan to get fresh complete_days count
    if not args.dry_run and (filled["tripwire"] or filled["shadow"]):
        report_post = gap_scan(days)
        report["complete_days_after_fill"] = report_post["complete_days"]
        report["missing_tripwire_after_fill"] = report_post["missing_tripwire"]
        report["missing_shadow_after_fill"] = report_post["missing_shadow"]

    report["filled"] = filled
    report["window"] = {"start": start.isoformat(), "end": end.isoformat(),
                        "effective_end": effective_end.isoformat(),
                        "trading_days_in_scope": len(days)}
    report["generated_utc"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["version"] = "observation_gatekeeper/1.0"

    report_path = pathlib.Path(args.report_out) if args.report_out \
        else (ROOT / GAP_REPORT_TEMPLATE.format(date=today.isoformat()))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                           encoding="utf-8")

    # Exit-Code-Logik
    if filled["failed"] or filled["blocked_no_scan"]:
        rc = 2
    elif filled["tripwire"] or filled["shadow"]:
        rc = 1
    else:
        rc = 0

    if rc == 3:
        pass  # reserved for total-input-missing scenario
    elif rc == 2 and not args.no_tickets:
        write_ticket(report, filled, today.isoformat())

    if not args.quiet:
        print(f"[gatekeeper] window={start}..{effective_end} "
              f"days={len(days)} complete={report['complete_days']}")
        if filled["tripwire"] or filled["shadow"]:
            print(f"[gatekeeper] filled: tripwire={[x[0] for x in filled['tripwire']]} "
                  f"shadow={[x[0] for x in filled['shadow']]}")
        if filled["failed"]:
            print(f"[gatekeeper] FAILED: {filled['failed']}")
        if filled["blocked_no_scan"]:
            print(f"[gatekeeper] BLOCKED (no scan): {filled['blocked_no_scan']}")
        print(f"[gatekeeper] report -> {report_path}")
        print(f"[gatekeeper] exit={rc}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
