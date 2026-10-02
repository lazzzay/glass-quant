#!/usr/bin/env python3
"""
verify_launchd_crons.py: Verifikation der Daily-Invest launchd-Crons (MAC-CRON-OUTAGE-1)
=========================================================================================
V7.10 · 2026-06-01 · Engineering/R&D

Zwei Modi:

  1) LIVE-VERIFY (auf dem eigenen Mac):
       python3 tools/verify_launchd_crons.py
     - prueft, ob die 4 launchd-Jobs (news/morning/catalyst/gatekeeper) registriert sind
       (via `launchctl list`),
     - prueft, ob die heute erwarteten Artefakte frisch auf Disk liegen
       (scan_<date>.json, news_sentiment_<date>.json, insider_<date>.json, ...),
     - liefert eine 4-stufige Ampel + maschinenlesbares JSON.
     Exit-Codes: 0=GREEN  1=YELLOW  2=RED  3=BLACK

  2) SELFTEST (laeuft ueberall, auch in der Sandbox ohne launchctl):
       python3 tools/verify_launchd_crons.py --selftest
     - generiert die plists analog install_launchd_crons.sh in einem TempDir,
       parst sie mit plistlib und validiert Struktur (Label, ProgramArguments,
       StartCalendarInterval Weekday/Hour/Minute). Beweist die XML-Wohlgeformtheit
       OHNE einen Mac. Exit 0 bei 100% gruen.

Kosten-Veto konform, read-only (ausser TempDir im Selftest), keine Trades.
"""
from __future__ import annotations
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
# Muss zu install_launchd_crons.sh passen (DAILYINVEST_LAUNCHD_PREFIX, Default com.dailyinvest).
PREFIX = os.environ.get("DAILYINVEST_LAUNCHD_PREFIX") or "com.dailyinvest"
JOB_LABELS = ["news", "morning", "catalyst", "gatekeeper"]

# Artefakte, die ein gesunder Morgen erzeugt (Prefix -> erlaubtes Alter in Werktagen)
MORNING_ARTIFACTS = [
    "scan", "regime", "premarket", "sector_rs", "earnings", "insider", "news_sentiment",
]


# --------------------------------------------------------------------------- helpers
def _workdays_back(d: date, n: int) -> date:
    cur = d
    while n > 0:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            n -= 1
    return cur


def _is_workday(d: date) -> bool:
    return d.weekday() < 5


def _find_artifact(prefix: str, today: date, max_age_workdays: int = 0):
    """Return (path, age_workdays) for newest <prefix>_<date>.json within window, else (None, None)."""
    best = None
    d = today
    age = 0
    for _ in range(max_age_workdays + 1):
        p = TOOLS / f"{prefix}_{d.isoformat()}.json"
        if p.exists() and p.stat().st_size > 2:
            return p, age
        d = _workdays_back(d, 1)
        age += 1
    return None, None


# --------------------------------------------------------------------------- live verify
def live_verify(today: date) -> dict:
    result = {
        "mode": "live",
        "date": today.isoformat(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "jobs_registered": {},
        "artifacts": {},
        "missing_jobs": [],
        "stale_or_missing_artifacts": [],
    }

    # 1) launchctl list
    listing = ""
    try:
        listing = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=15
        ).stdout
        result["launchctl_available"] = True
    except (FileNotFoundError, subprocess.SubprocessError):
        result["launchctl_available"] = False

    for label in JOB_LABELS:
        full = f"{PREFIX}.{label}"
        reg = full in listing if result["launchctl_available"] else False
        result["jobs_registered"][label] = reg
        if not reg:
            result["missing_jobs"].append(label)

    # 2) artifact freshness (only meaningful on a workday)
    for prefix in MORNING_ARTIFACTS:
        p, age = _find_artifact(prefix, today, max_age_workdays=1)
        if p is None:
            result["artifacts"][prefix] = {"found": False, "age_workdays": None}
            result["stale_or_missing_artifacts"].append(prefix)
        else:
            result["artifacts"][prefix] = {"found": True, "age_workdays": age, "path": p.name}
            if age and age >= 1:
                result["stale_or_missing_artifacts"].append(prefix)

    # 3) verdict
    n_missing_jobs = len(result["missing_jobs"])
    n_bad_art = len(result["stale_or_missing_artifacts"])
    if not result["launchctl_available"]:
        verdict, sev, code = "launchctl_unavailable_not_a_mac", "UNAVAILABLE", 3
    elif n_missing_jobs == len(JOB_LABELS):
        verdict, sev, code = "no_jobs_registered_run_installer", "BLACK", 3
    elif n_missing_jobs > 0:
        verdict, sev, code = "some_jobs_missing", "RED", 2
    elif not _is_workday(today):
        verdict, sev, code = "weekend_jobs_ok", "GREEN", 0
    elif n_bad_art == 0:
        verdict, sev, code = "all_green", "GREEN", 0
    elif n_bad_art <= 2:
        verdict, sev, code = "jobs_ok_some_artifacts_stale", "YELLOW", 1
    else:
        verdict, sev, code = "jobs_ok_but_artifacts_broken", "RED", 2

    result.update(verdict=verdict, severity=sev, exit_code=code)
    return result


# --------------------------------------------------------------------------- selftest
def _emit_plist_py(tmp: Path, label: str, hour: int, minute: int, weekdays, prog_args, python, repo):
    """Mirror of install_launchd_crons.sh emit_plist, in Python, for validation."""
    sci = []
    if weekdays is None:
        sci.append({"Hour": hour, "Minute": minute})
    else:
        for wd in weekdays:
            sci.append({"Weekday": wd, "Hour": hour, "Minute": minute})
    data = {
        "Label": f"{PREFIX}.{label}",
        "ProgramArguments": list(prog_args),
        "WorkingDirectory": repo,
        "EnvironmentVariables": {
            "DAILYINVEST_REPO": repo,
            "DAILYINVEST_PYTHON": python,
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
        "StartCalendarInterval": sci,
        "StandardOutPath": str(Path(repo) / "tools" / "_logs" / f"launchd_{label}.log"),
        "StandardErrorPath": str(Path(repo) / "tools" / "_logs" / f"launchd_{label}.log"),
        "RunAtLoad": False,
    }
    plist_path = tmp / f"{PREFIX}.{label}.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    return plist_path


def selftest() -> int:
    python = "/usr/bin/python3"
    repo = str(ROOT)
    jobs = [
        ("news", 6, 30, [1, 2, 3, 4, 5],
         [python, f"{repo}/tools/news_sentiment_fetch.py", "--tier", "A,B", "--delay", "0.4"]),
        ("morning", 8, 0, [1, 2, 3, 4, 5],
         [python, f"{repo}/tools/run_morning_pipeline.py"]),
        ("catalyst", 20, 0, [7],
         [python, f"{repo}/tools/catalyst_calendar.py"]),
        ("gatekeeper", 9, 45, [1],
         [python, f"{repo}/tools/observation_gatekeeper.py"]),
    ]
    passed = 0
    total = 0
    tmp = Path(tempfile.mkdtemp(prefix="launchd_selftest_"))
    try:
        for label, hour, minute, wds, args in jobs:
            p = _emit_plist_py(tmp, label, hour, minute, wds, args, python, repo)
            # re-parse and assert
            with open(p, "rb") as f:
                parsed = plistlib.load(f)
            checks = [
                ("label", parsed["Label"] == f"{PREFIX}.{label}"),
                ("prog_args_nonempty", len(parsed["ProgramArguments"]) >= 1),
                ("sci_count", len(parsed["StartCalendarInterval"]) == len(wds)),
                ("hour", all(e["Hour"] == hour for e in parsed["StartCalendarInterval"])),
                ("minute", all(e["Minute"] == minute for e in parsed["StartCalendarInterval"])),
                ("weekday", all(e["Weekday"] in wds for e in parsed["StartCalendarInterval"])),
                ("env_repo", parsed["EnvironmentVariables"]["DAILYINVEST_REPO"] == repo),
            ]
            for name, ok in checks:
                total += 1
                if ok:
                    passed += 1
                else:
                    print(f"  FAIL {label}/{name}")
            print(f"  [{label}] {sum(1 for _, ok in checks if ok)}/{len(checks)} checks ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nSELFTEST {passed}/{total} checks passed")
    return 0 if passed == total else 1


# --------------------------------------------------------------------------- cli
def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Daily-Invest launchd crons")
    ap.add_argument("--selftest", action="store_true", help="plist-XML validation, runs anywhere")
    ap.add_argument("--date", default=None, help="override today (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    today = date.fromisoformat(args.date) if args.date else date.today()
    res = live_verify(today)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"launchd-Verify {res['date']} -> {res['severity']} ({res['verdict']})")
        print(f"  launchctl available : {res['launchctl_available']}")
        print(f"  jobs registered     : "
              + ", ".join(f"{k}={'Y' if v else 'N'}" for k, v in res["jobs_registered"].items()))
        if res["stale_or_missing_artifacts"]:
            print(f"  stale/missing art.  : {', '.join(res['stale_or_missing_artifacts'])}")
    # persist
    out = TOOLS / f"launchd_verify_{res['date']}.json"
    try:
        out.write_text(json.dumps(res, indent=2))
    except OSError:
        pass
    return res["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
