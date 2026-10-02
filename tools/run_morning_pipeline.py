#!/usr/bin/env python3
"""
run_morning_pipeline.py: Python-Port der Morgen-Pipeline (CRON-TCC-HARDENING 2026-06-08).

WARUM Python statt bash:
  Der launchd-Morgen-Job lief über /bin/bash. macOS hat bash zweimal (02.06. + 08.06.)
  den Full-Disk-Access auf ~/Documents zurückgesetzt -> Job-Exit 126 ("Operation not
  permitted") -> kein Scan, Empfehlung las Stale-Daten. Die drei python3-basierten Jobs
  (news/gatekeeper/catalyst) überlebten BEIDE Resets mit Exit 0. Dieser Port läuft
  komplett über /usr/bin/python3 -> immun gegen bash-FDA-Resets.

Verhalten 1:1 wie run_morning_pipeline.sh:
  - gleiche Schritt-Reihenfolge (CRON-LATENCY-FIX: scan-kritischer Pfad zuerst,
    deaktivierte Score-Layer insider/options_flow ans Ende = non-blocking),
  - fail-soft (ein toter Schritt stoppt den Morgen nicht),
  - gleicher Log-Pfad tools/_logs/morning_pipeline_<date>.log + stdout (launchd),
  - Exit-Code = Anzahl fehlgeschlagener Schritte (0 = alles grün),
  - Wochenende wird übersprungen.

CLI:
    python3 tools/run_morning_pipeline.py            # echter Lauf (Mo-Fr)
    python3 tools/run_morning_pipeline.py --force    # auch am Wochenende
    python3 tools/run_morning_pipeline.py --selftest # offline Struktur-/Fail-Soft-Test
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

REPO_DIR = Path(os.environ.get("DAILYINVEST_REPO", Path(__file__).resolve().parents[1]))

# SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1 (2026-07-06): == daily_scanner.
# VALID_FILL_FLOOR_DEFAULT == data_health SCAN_FILL_LT_80/H. Ein kanonischer Scan
# unter diesem Fill gilt als NICHT fertig -> Catch-up-Battery darf ihn ersetzen.
CATCHUP_FILL_FLOOR = 0.80
# Bewusst /usr/bin/python3 als Default (der Interpreter MIT Full-Disk-Access);
# sys.executable, falls das Skript ohnehin damit gestartet wurde.
PYTHON = os.environ.get("DAILYINVEST_PYTHON", sys.executable or "/usr/bin/python3")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _log(pipelog: Path, msg: str) -> None:
    """Schreibt eine Zeile nach stdout (-> launchd_morning.log) UND ins Tageslog (wie bash tee)."""
    line = f"[{datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    with pipelog.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run_step(pipelog: Path, name: str, cmd: list[str]) -> bool:
    """Ein Pipeline-Schritt. stdout/stderr des Tools wird ans Tageslog angehängt
    (entspricht bash `"$@" >>"$PIPELOG" 2>&1`). Fail-soft: rc!=0 ODER Exception -> False,
    aber die Pipeline läuft weiter."""
    _log(pipelog, f"START {name} :: {' '.join(cmd)}")
    try:
        with pipelog.open("a", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, cwd=str(REPO_DIR),
                                stdout=fh, stderr=subprocess.STDOUT).returncode
    except Exception as e:  # noqa: BLE001 - ein toter Schritt darf den Morgen nicht killen
        _log(pipelog, f"FAIL  {name} (exc={e!r}): fail-soft, pipeline continues")
        return False
    if rc == 0:
        _log(pipelog, f"OK    {name}")
        return True
    _log(pipelog, f"FAIL  {name} (rc={rc}): fail-soft, pipeline continues")
    return False


def ordered_steps(today: str) -> list[tuple[str, list[str]]]:
    """RATE-BUDGET-ORDER (2026-06-11): Der SCAN laeuft jetzt VOR premarket.
    Live-Incident 11.06.: premarket (639 yfinance-Fetches, 5-40 Min) direkt vor
    scan_initial verbrannte das Yahoo-Rate-Budget -> Batch-429 -> ENV-GUARD-Abort
    (COVERAGE_FLOOR). Der Scan ist der kritischste Producer und bekommt das
    frische Budget; premarket/earnings danach (zur 10:00-Empfehlung trotzdem
    fertig); DISABLED_LAYERS insider/options_flow bleiben ganz am Ende."""
    p = PYTHON
    return [
        ("regime",         [p, "tools/regime_gate.py", "--save"]),
        ("sector_rs",      [p, "tools/sector_rs.py"]),
        ("scan_initial",   [p, "tools/daily_scanner.py", "--date", today]),
        ("scan_rescore",   [p, "tools/daily_scanner.py", "--date", today, "--rescan"]),
        ("momentum_watch", [p, "tools/momentum_watch.py", "--date", today, "--write"]),
        ("earnings",       [p, "tools/earnings_calendar.py", "--date", today]),
        ("premarket",      [p, "tools/premarket_scanner.py"]),
        # --- ab hier NON-BLOCKING: deaktivierte Score-Layer, nur informative Artefakte ---
        ("insider",        [p, "tools/insider_feed.py", "--date", today, "--enable-sec-edgar"]),
        ("options_flow",   [p, "tools/options_flow.py", "--date", today]),
    ]


def catch_up_skip(today: str, repo_dir: Path | None = None) -> bool:
    """MAC-SLEEP-CATCH-UP (2026-06-11): True, wenn der kritische Output des Tages
    (scan_<today>.json) bereits existiert UND gesund ist: der 10:30-Catch-up-Lauf
    wird dann zum No-op. Verhindert Doppel-Laeufe (scan_initial wuerde an
    SCAN-IMMUTABILITY-1 rc=3 scheitern) an Tagen, an denen der 08:00-Lauf normal
    durchkam.

    SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1 (2026-07-06, V7.37): Existenz ALLEIN
    genuegt nicht (Defense-in-Depth zum daily_scanner-Producer-Guard). Ein
    DEGRADIERTER kanonischer Scan (< CATCHUP_FILL_FLOOR, == data_health
    SCAN_FILL_LT_80) darf die Catch-up-Battery NICHT abwuergen, sonst wird der
    Throttle-Zombie-NO-TRADE zementiert (Live 07-01/07-02). Fill-Rate aus dem
    JSON lesen; sub-Floor / unlesbar / kaputt => nicht als fertiger Scan werten
    => False => Catch-up laeuft erneut (Self-Heal)."""
    base = repo_dir if repo_dir is not None else REPO_DIR
    scan_json = base / "tools" / f"scan_{today}.json"
    if not scan_json.exists():
        return False
    try:
        with open(scan_json, encoding="utf-8") as f:
            data = json.load(f)
        universe = int(data.get("universe_size") or 0)
        valid = int(data.get("valid_results") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Unlesbar/kaputt -> sicherheitshalber NICHT skippen (Catch-up darf laufen)
        return False
    if universe > 0 and (valid / universe) < CATCHUP_FILL_FLOOR:
        return False  # degradierter Scan -> Catch-up-Battery ersetzt ihn
    return True


def run_pipeline(force: bool = False) -> int:
    log_dir = REPO_DIR / "tools" / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = _today()
    pipelog = log_dir / f"morning_pipeline_{today}.log"

    dow = date.today().isoweekday()  # 1=Mo .. 7=So
    if not force and dow >= 6:
        _log(pipelog, f"SKIP weekend (dow={dow})")
        return 0

    # MAC-SLEEP-CATCH-UP (2026-06-11): zweiter launchd-Trigger 10:30 holt
    # verschlafene 08:00-Laeufe nach; lief der Morgen normal, ist das ein No-op.
    if not force and catch_up_skip(today):
        _log(pipelog, f"SKIP catch-up: scan_{today}.json existiert bereits (08:00-Lauf ok)")
        return 0

    fails = 0
    for name, cmd in ordered_steps(today):
        if not run_step(pipelog, name, cmd):
            fails += 1
    _log(pipelog, f"DONE morning pipeline: failed_steps={fails}")
    return fails


def selftest() -> int:
    """Offline (kein Netz, keine echten Tools): prüft run_step Erfolg/Fehler/Exception-
    fail-soft, Log-Schreiben und die Schritt-Reihenfolge (scan vor insider/options_flow)."""
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory() as d:
        pl = Path(d) / "test.log"
        if run_step(pl, "good", [sys.executable, "-c", "print('hi')"]) is not True:
            print("FAIL: good-Step sollte True sein"); ok = False
        if run_step(pl, "bad", [sys.executable, "-c", "import sys; sys.exit(1)"]) is not False:
            print("FAIL: bad-Step (rc=1) sollte False sein"); ok = False
        if run_step(pl, "missing", ["/nonexistent/cmd_xyz_42"]) is not False:
            print("FAIL: fehlendes Kommando sollte fail-soft False sein"); ok = False
        txt = pl.read_text(encoding="utf-8")
        for needle in ["START good", "OK    good", "FAIL  bad", "FAIL  missing"]:
            if needle not in txt:
                print(f"FAIL: Log-Zeile fehlt: {needle!r}"); ok = False

    names = [n for n, _ in ordered_steps("2026-01-01")]
    if not (names.index("scan_initial") < names.index("insider")
            and names.index("scan_rescore") < names.index("options_flow")
            and names.index("momentum_watch") < names.index("insider")):
        print("FAIL: Reihenfolge, scan/momentum müssen VOR insider/options_flow stehen"); ok = False
    # RATE-BUDGET-ORDER (2026-06-11): scan MUSS vor premarket laufen (429-Schutz)
    if not (names.index("scan_initial") < names.index("premarket")
            and names.index("sector_rs") < names.index("scan_initial")):
        print("FAIL: Reihenfolge, sector_rs < scan_initial < premarket (Rate-Budget)"); ok = False
    if names[0] != "regime" or names[-1] != "options_flow":
        print("FAIL: erwartet erste=regime, letzte=options_flow"); ok = False

    # MAC-SLEEP-CATCH-UP + SCAN-RESCORE-DEFEATS-VALIDFILL-SELFHEAL-1 (fill-aware)
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "tools").mkdir()
        if catch_up_skip("2099-12-31", repo_dir=repo) is not False:
            print("FAIL: catch_up_skip ohne Scan-Datei muss False sein"); ok = False
        # Legacy/leeres JSON (kein universe_size) -> als vorhanden werten (True)
        (repo / "tools" / "scan_2099-12-31.json").write_text("{}", encoding="utf-8")
        if catch_up_skip("2099-12-31", repo_dir=repo) is not True:
            print("FAIL: catch_up_skip mit (leerer) Scan-Datei muss True sein"); ok = False
        # Gesunder Scan (Fill 97% >= Floor 80%) -> skip=True
        (repo / "tools" / "scan_2099-12-30.json").write_text(
            json.dumps({"universe_size": 639, "valid_results": 620}), encoding="utf-8")
        if catch_up_skip("2099-12-30", repo_dir=repo) is not True:
            print("FAIL: catch_up_skip mit gesundem Scan (97%) muss True sein"); ok = False
        # Degradierter Scan (Fill 36,8% < Floor) -> skip=False (Catch-up laeuft, Self-Heal)
        (repo / "tools" / "scan_2099-12-29.json").write_text(
            json.dumps({"universe_size": 639, "valid_results": 235}), encoding="utf-8")
        if catch_up_skip("2099-12-29", repo_dir=repo) is not False:
            print("FAIL: catch_up_skip mit degradiertem Scan (36,8%) muss False sein"); ok = False
        # Kaputtes JSON -> sicherheitshalber NICHT skippen (False)
        (repo / "tools" / "scan_2099-12-28.json").write_text("{not valid json", encoding="utf-8")
        if catch_up_skip("2099-12-28", repo_dir=repo) is not False:
            print("FAIL: catch_up_skip mit kaputtem JSON muss False sein"); ok = False

    print(("PASS" if ok else "FAIL"), "selftest run_morning_pipeline")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Morgen-Pipeline (Python-Port, TCC-hart)")
    ap.add_argument("--force", action="store_true", help="auch am Wochenende laufen")
    ap.add_argument("--selftest", action="store_true", help="offline Struktur-/Fail-Soft-Test")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    return run_pipeline(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
