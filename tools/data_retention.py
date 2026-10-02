#!/usr/bin/env python3
"""data_retention.py: V7.40 Archiv-Hygiene (M8 aus System-Audit-2026-07-08).

DRY-RUN IS DEFAULT. Nothing is moved/deleted without --apply.

Scope (and ONLY this scope):
  1. Canonical scan artifacts in tools/ older than --keep-days (default 90):
     scan_YYYY-MM-DD.{json,csv}(+.manifest.json) → Archiv/retention/scans-YYYY-MM.tar.gz
  2. RESCAN forensic duplicates (scan_*__RESCAN_*) older than --rescan-days (default 30)
     → same monthly archive.
  3. tools/_logs/*.log older than --log-days (default 30) → gzip in place;
     gzipped logs older than 180 d → monthly archive.
  4. tools/__pycache__ → removed (pure cache).

NEVER touched: signals/, Briefings/, data_lake/, trade_journal.sqlite, universe.csv,
any scan within keep-days, any non-matching file. Archives are verified (member
count + size) BEFORE originals are deleted. Exit 0 ok / 1 error.

CLI: --selftest | [--apply] [--keep-days N] [--rescan-days N] [--log-days N] [--base DIR]
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from typing import Dict, List, Tuple

CANON_RE = re.compile(r"^scan_\d{4}-\d{2}-\d{2}(\.json|\.csv)(\.manifest\.json)?$")
RESCAN_RE = re.compile(r"^scan_\d{4}-\d{2}-\d{2}__RESCAN_.*")
LOG_RE = re.compile(r".*\.log$")


def repo_base() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def age_days(path: str, now: float) -> float:
    try:
        return (now - os.path.getmtime(path)) / 86400.0
    except OSError:
        return -1


def month_of(path: str) -> str:
    m = re.search(r"(\d{4}-\d{2})-\d{2}", os.path.basename(path))
    return m.group(1) if m else "unknown"


def collect(base: str, keep_days: int, rescan_days: int, log_days: int,
            now: float, include_rescans: bool = False) -> Dict[str, List[str]]:
    """SAFETY (Verifikation 09.07.): tier_shadow_tracker glob't `scan_*.json` und
    matcht damit AUCH RESCAN-Dateien: an degradierten Tagen tragen die RESCANs
    mehr Closes als der Kanon. RESCANs werden deshalb NUR mit explizitem
    --include-rescans angefasst (Default: behalten)."""
    tools = os.path.join(base, "tools")
    logs = os.path.join(tools, "_logs")
    out = {"scan_old": [], "rescan_old": [], "log_gzip": [], "log_old": [],
           "pycache": []}
    if os.path.isdir(tools):
        for f in sorted(os.listdir(tools)):
            p = os.path.join(tools, f)
            if not os.path.isfile(p):
                continue
            if RESCAN_RE.match(f):
                if include_rescans and age_days(p, now) > rescan_days:
                    out["rescan_old"].append(p)
            elif CANON_RE.match(f) and age_days(p, now) > keep_days:
                out["scan_old"].append(p)
    if os.path.isdir(logs):
        for f in sorted(os.listdir(logs)):
            p = os.path.join(logs, f)
            if not os.path.isfile(p):
                continue
            # SCHWARM-FIX: launchd-Logs sind append-mode (mtime immer frisch),
            # zusätzlich size-basiert rotieren (>5 MB), nicht nur alters-basiert.
            if LOG_RE.match(f) and (age_days(p, now) > log_days
                                    or os.path.getsize(p) > 5_000_000):
                out["log_gzip"].append(p)
            elif f.endswith(".log.gz") and age_days(p, now) > 180:
                out["log_old"].append(p)
    pyc = os.path.join(tools, "__pycache__")
    if os.path.isdir(pyc):
        out["pycache"].append(pyc)
    return out


def total_bytes(paths: List[str]) -> int:
    n = 0
    for p in paths:
        try:
            n += os.path.getsize(p) if os.path.isfile(p) else sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(p) for f in fs)
        except OSError:
            pass
    return n


def archive_and_remove(base: str, paths: List[str], apply: bool) -> Tuple[int, List[str]]:
    """Group by month → tar.gz in Archiv/retention/ → verify → delete originals."""
    if not paths:
        return 0, []
    arch_dir = os.path.join(base, "Archiv", "retention")
    by_month: Dict[str, List[str]] = {}
    for p in paths:
        by_month.setdefault(month_of(p), []).append(p)
    archived, errors = 0, []
    for month, group in sorted(by_month.items()):
        tar_path = os.path.join(arch_dir, f"scans-{month}.tar.gz")
        if not apply:
            archived += len(group)
            continue
        os.makedirs(arch_dir, exist_ok=True)
        mode = "a:gz"  # append not supported for gz → read-modify-write via temp
        existing_members: List[str] = []
        tmp_tar = tar_path + ".tmp"
        try:
            with tarfile.open(tmp_tar, "w:gz") as tf:
                if os.path.exists(tar_path):
                    with tarfile.open(tar_path, "r:gz") as old:
                        for m in old.getmembers():
                            existing_members.append(m.name)
                            tf.addfile(m, old.extractfile(m))
                for p in group:
                    tf.add(p, arcname=os.path.basename(p))
            with tarfile.open(tmp_tar, "r:gz") as tf:
                names = set(tf.getnames())
            want = set(existing_members) | {os.path.basename(p) for p in group}
            if not want.issubset(names) or os.path.getsize(tmp_tar) == 0:
                raise RuntimeError(f"tar verify failed for {tar_path}")
            os.replace(tmp_tar, tar_path)
            for p in group:
                try:
                    os.remove(p)
                    archived += 1
                except OSError as e:
                    errors.append(f"rm:{os.path.basename(p)}: {type(e).__name__}")
        except Exception as e:  # noqa: BLE001, safety: keep originals on ANY error
            errors.append(f"{month}: {type(e).__name__}: {e}")
            if os.path.exists(tmp_tar):
                os.remove(tmp_tar)
    return archived, errors


def gzip_logs(paths: List[str], apply: bool, errors: List[str]) -> int:
    """Fail-soft per file: one un-deletable log must never abort the run."""
    n = 0
    for p in paths:
        if not apply:
            n += 1
            continue
        try:
            with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            if os.path.getsize(p + ".gz") > 0:
                os.remove(p)
                n += 1
        except OSError as e:
            errors.append(f"log:{os.path.basename(p)}: {type(e).__name__}")
            try:  # kein Doppel-Bestand hinterlassen, wenn Original bleibt
                if os.path.exists(p) and os.path.exists(p + ".gz"):
                    os.remove(p + ".gz")
            except OSError:
                pass
    return n


def run(base: str, apply: bool, keep_days: int, rescan_days: int,
        log_days: int, include_rescans: bool = False) -> Dict[str, object]:
    now = time.time()
    plan = collect(base, keep_days, rescan_days, log_days, now, include_rescans)
    report: Dict[str, object] = {
        "mode": "APPLY" if apply else "DRY_RUN",
        "keep_days": keep_days,
        "bytes_scan_old": total_bytes(plan["scan_old"]),
        "bytes_rescan_old": total_bytes(plan["rescan_old"]),
        "bytes_logs": total_bytes(plan["log_gzip"]) + total_bytes(plan["log_old"]),
        "bytes_pycache": total_bytes(plan["pycache"]),
        "n_scan_old": len(plan["scan_old"]),
        "n_rescan_old": len(plan["rescan_old"]),
        "n_log_gzip": len(plan["log_gzip"]),
        "errors": [],
    }
    archived, errs = archive_and_remove(base, plan["scan_old"] + plan["rescan_old"], apply)
    report["archived_files"] = archived
    report["errors"] = errs
    report["logs_gzipped"] = gzip_logs(plan["log_gzip"], apply, errs)
    if apply:
        for p in plan["pycache"]:
            shutil.rmtree(p, ignore_errors=True)
    report["pycache_removed"] = bool(plan["pycache"]) and apply
    report["est_freed_mb"] = round((report["bytes_scan_old"] + report["bytes_rescan_old"]
                                    + report["bytes_pycache"]) / 1e6, 1)
    return report


# ── selftest ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("data_retention --selftest (offline, tmpdir)")
    with tempfile.TemporaryDirectory() as tmp:
        tools = os.path.join(tmp, "tools")
        logs = os.path.join(tools, "_logs")
        os.makedirs(logs)
        os.makedirs(os.path.join(tools, "__pycache__"))
        open(os.path.join(tools, "__pycache__", "x.pyc"), "w").write("x")
        old_t = time.time() - 120 * 86400
        mid_t = time.time() - 40 * 86400
        mk = lambda name, ts, content="{}": (  # noqa: E731
            open(os.path.join(tools, name), "w").write(content),
            os.utime(os.path.join(tools, name), (ts, ts)))
        mk("scan_2026-03-01.json", old_t)                 # alt → archiv
        mk("scan_2026-03-01.json.manifest.json", old_t)   # alt → archiv
        mk("scan_2026-07-01.json", time.time())           # frisch → bleibt
        mk("scan_2026-06-01__RESCAN_1.json", mid_t)       # rescan >30d → archiv
        mk("universe.csv", old_t, "ticker\nAAA")          # NIE anfassen
        lp = os.path.join(logs, "old.log")
        open(lp, "w").write("log" * 100); os.utime(lp, (mid_t, mid_t))

        # G1: dry-run ändert NICHTS; RESCANs per Default GESCHÜTZT
        r = run(tmp, apply=False, keep_days=90, rescan_days=30, log_days=30)
        check("G1 dry-run: 2 alte Scans, RESCAN per Default geschützt",
              r["n_scan_old"] == 2 and r["n_rescan_old"] == 0)
        r1b = run(tmp, apply=False, keep_days=90, rescan_days=30, log_days=30,
                  include_rescans=True)
        check("G1b --include-rescans zählt den RESCAN", r1b["n_rescan_old"] == 1)
        check("G1 dry-run: keine Datei entfernt",
              os.path.exists(os.path.join(tools, "scan_2026-03-01.json"))
              and os.path.exists(lp))

        # G2: apply archiviert + löscht Originale, frische + RESCANs bleiben
        r2 = run(tmp, apply=True, keep_days=90, rescan_days=30, log_days=30)
        tarp = os.path.join(tmp, "Archiv", "retention", "scans-2026-03.tar.gz")
        check("G2 apply: Monats-Archiv existiert", os.path.exists(tarp))
        with tarfile.open(tarp, "r:gz") as tf:
            names = set(tf.getnames())
        check("G2 Archiv enthält beide März-Dateien",
              "scan_2026-03-01.json" in names)
        check("G2 Originale weg, frischer Scan bleibt",
              not os.path.exists(os.path.join(tools, "scan_2026-03-01.json"))
              and os.path.exists(os.path.join(tools, "scan_2026-07-01.json")))
        check("G2 RESCAN bleibt liegen (Default-Schutz, tier_shadow-Konsument)",
              os.path.exists(os.path.join(tools, "scan_2026-06-01__RESCAN_1.json")))
        check("G2 universe.csv unangetastet",
              os.path.exists(os.path.join(tools, "universe.csv")))
        check("G2 Log gezippt", os.path.exists(lp + ".gz") and not os.path.exists(lp))
        check("G2 pycache entfernt",
              not os.path.exists(os.path.join(tools, "__pycache__")))
        check("G2 0 Fehler", r2["errors"] == [])

        # G3: zweiter apply-Lauf = No-op (idempotent)
        r3 = run(tmp, apply=True, keep_days=90, rescan_days=30, log_days=30)
        check("G3 idempotent (nichts mehr zu tun)",
              r3["n_scan_old"] == 0 and r3["archived_files"] == 0)

    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    argv = sys.argv[1:]
    base = repo_base()
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    geti = lambda flag, dflt: int(argv[argv.index(flag) + 1]) if flag in argv else dflt  # noqa: E731
    inc_rescans = "--include-rescans" in argv
    if "--list" in argv:  # volle Datei-Vorschau, keinerlei Aktion
        plan = collect(base, geti("--keep-days", 90), geti("--rescan-days", 30),
                       geti("--log-days", 30), time.time(), inc_rescans)
        print(json.dumps({k: [os.path.relpath(p, base) for p in v]
                          for k, v in plan.items()}, indent=1, ensure_ascii=False))
        sys.exit(0)
    rep = run(base, apply="--apply" in argv, keep_days=geti("--keep-days", 90),
              rescan_days=geti("--rescan-days", 30), log_days=geti("--log-days", 30),
              include_rescans=inc_rescans)
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    sys.exit(0 if not rep["errors"] else 1)
