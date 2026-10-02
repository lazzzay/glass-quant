#!/usr/bin/env python3
"""ticket_auto_resolve.py: V7.9.2 TICKET-HYGIENE-1 + V7.9.5 TICKET-HYGIENE-2

Automatisiert das Schliessen **historischer** und **timing-staler** auto-Tickets
in ops_tickets.md.

Hintergrund
-----------
`tools/data_health.py` und `tools/tripwire_monitor.py` schreiben auto-Tickets mit
pro-Tag eindeutigen Fingerprints (SHA1 ueber date|source|code bzw.
date|code|streak|count). Jeder Tagesauftritt desselben Symptoms erzeugt ein
neues Ticket: das alte bleibt ewig OPEN. Das blaeht den ops_escalation-Zaehler
auf und verwischt das wirkliche Ticket-Delta einer Woche.

Dieses Tool **historisiert** alte auto-Tickets, ohne Neu-Opens zu ermoeglichen:
- Status OPEN -> FIXED (mit Begruendung + heutigem Datum).
- Der `<!-- health-fp:... -->` / `<!-- tripwire-fp:... -->` Marker BLEIBT erhalten,
  sodass der jeweilige Writer beim naechsten Run nicht dasselbe Ticket erneut oeffnet.

Regelwerk (konservativ)
-----------------------
1. `ticket_date < today` UND entsprechendes data_health_/tripwire_YYYY-MM-DD.json
   EXISTIERT -> historisiert (Begruendung: "historisiert: tageseigener Fingerprint,
   neue Tage tragen neue fps").
2. `ticket_date == today` UND fp NICHT in den aktuellen findings des heutigen
   Reports -> stale-from-earlier-run (Begruendung: "stale: fp nicht in aktueller
   {source}_YYYY-MM-DD.json").
3. **NEU V7.9.5 TIMING-AWARE** `ticket_date == today` UND Ticket-Code ist ein
   `*_JSON_MISSING`-Symptom UND das referenzierte Upstream-Artefakt existiert
   inzwischen -> timing-stale (Begruendung: "timing-stale: {artifact}_{date}.json
   vorhanden seit {mtime}; data_health lief vor dem Upstream-Artefakt"). Diese
   Regel wird auch ohne data_health-Re-Run wirksam und adressiert
   DATA-HEALTH-TIMING-1 ops-seitig, solange SCAN-OF-ORCHESTRATION-1 offen ist.
4. Fuer tripwire WARN: zusaetzlich wenn am selben oder spaeteren Tag ein
   CRITICAL-Finding existiert, markieren als "superseded by TRIPWIRE_STREAK_CRITICAL
   am YYYY-MM-DD".

Sicherheit
----------
- Default-Modus ist **DRY-RUN**. Mit `--apply` wird in `ops_tickets.md` geschrieben.
- Vor jedem Apply wird eine Backup-Kopie `ops_tickets.md.bak_YYYYMMDD_HHMMSS`
  angelegt (bleibt 30 Tage liegen, dann per Cron aufraeumbar).
- Ergebnis wird als JSON nach `tools/ticket_auto_resolve_YYYY-MM-DD.json`
  geschrieben (Audit-Trail fuer den 29.04.-Abschluss).
- Keine Aenderungen an Scoring, daily_scanner, options_flow, insider_feed,
  trade_journal etc. Reine Ops-Hygiene.

CLI
---
  python3 tools/ticket_auto_resolve.py --dry-run      # Default: Preview
  python3 tools/ticket_auto_resolve.py --apply        # Tatsaechlich schreiben
  python3 tools/ticket_auto_resolve.py --since 2026-04-16 --apply
  python3 tools/ticket_auto_resolve.py --quiet

Exit-Codes
----------
  0: Lauf erfolgreich, ggf. N Tickets geschlossen
  1: Fehler beim Lesen oder Schreiben (siehe stderr)
  2: Setup-Problem (Datei/Pfade fehlen)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# Pfade
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
TICKETS_PATH = ROOT / "ops_tickets.md"
TOOLS_DIR = ROOT / "tools"

HEALTH_FILE_FMT = "data_health_{date}.json"
TRIPWIRE_FILE_FMT = "tripwire_{date}.json"

# V7.9.5 TICKET-HYGIENE-2: Mapping von *_JSON_MISSING-Codes auf das
# zugehoerige Upstream-Artefakt. Fehlt ein Code hier, greift die neue
# timing-aware-Regel nicht und das Ticket bleibt auf dem alten Pfad.
JSON_MISSING_ARTIFACT: dict[str, str] = {
    "REGIME_JSON_MISSING": "regime",
    "PREMARKET_JSON_MISSING": "premarket",
    "OPTIONS_FLOW_JSON_MISSING": "options_flow",
    "INSIDER_JSON_MISSING": "insider",
    "SECTOR_RS_JSON_MISSING": "sector_rs",
    "SCAN_JSON_MISSING": "scan",
    "DATA_HEALTH_JSON_MISSING": "data_health",
    "TRIPWIRE_JSON_MISSING": "tripwire",
}

# Regex fuer OPEN-Zeilen mit health-fp oder tripwire-fp Marker
RE_TICKET = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(?P<sev>[HML])\s*\|\s*"
    r"(?P<task>[^|]+?)\s*\|\s*"
    r"(?P<body>.*?)\s*\|\s*"
    r"OPEN\s*(?P<marker><!--\s*(?P<kind>health-fp|tripwire-fp):(?P<fp>[a-f0-9]+)\s*-->)\s*$"
)


# ----------------------------------------------------------------------------
# Helfer
# ----------------------------------------------------------------------------

def _today_iso() -> str:
    return dt.date.today().isoformat()


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _load_findings(path: Path) -> list[dict]:
    """Best-effort load of .findings[] aus data_health/tripwire JSON."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    f = data.get("findings", [])
    if not isinstance(f, list):
        return []
    return f


def _health_fps_by_date(dates: list[dt.date]) -> dict[str, set[str]]:
    """Laedt data_health-Findings pro Datum, returnt {date_iso: set(fingerprints)}."""
    out: dict[str, set[str]] = {}
    for d in dates:
        iso = d.isoformat()
        p = TOOLS_DIR / HEALTH_FILE_FMT.format(date=iso)
        if not p.exists():
            continue
        fps: set[str] = set()
        for f in _load_findings(p):
            fp = f.get("fingerprint")
            if fp:
                fps.add(fp)
        out[iso] = fps
    return out


def _tripwire_fps_and_codes(dates: list[dt.date]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Fuer Tripwire muessen wir die Fingerprints (aktuell) UND Codes (fuer
    Supersession-Logik) kennen."""
    fp_by_date: dict[str, set[str]] = {}
    codes_by_date: dict[str, set[str]] = {}
    for d in dates:
        iso = d.isoformat()
        p = TOOLS_DIR / TRIPWIRE_FILE_FMT.format(date=iso)
        if not p.exists():
            continue
        fps: set[str] = set()
        codes: set[str] = set()
        # Tripwire-fp wird nicht im JSON gespeichert: er wird beim Schreiben
        # gebildet. Wir koennen aber den Tripwire-Monitor-Fingerprint lokal
        # rekonstruieren, wenn wir date/code/streak/count kennen.
        data = {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
        streak = (data.get("summary") or {}).get("current_zero_streak", 0)
        total = (data.get("summary") or {}).get("total_recs_window", 0)
        import hashlib
        for f in _load_findings(p):
            code = f.get("code", "")
            if code:
                codes.add(code)
                raw = f"{iso}|{code}|{streak}|{total}"
                fp = hashlib.sha1(raw.encode()).hexdigest()[:10]
                fps.add(fp)
        fp_by_date[iso] = fps
        codes_by_date[iso] = codes
    return fp_by_date, codes_by_date


def _collect_health_dates(since: dt.date, until: dt.date) -> list[dt.date]:
    dates: list[dt.date] = []
    d = since
    while d <= until:
        if (TOOLS_DIR / HEALTH_FILE_FMT.format(date=d.isoformat())).exists():
            dates.append(d)
        d += dt.timedelta(days=1)
    return dates


def _collect_tripwire_dates(since: dt.date, until: dt.date) -> list[dt.date]:
    dates: list[dt.date] = []
    d = since
    while d <= until:
        if (TOOLS_DIR / TRIPWIRE_FILE_FMT.format(date=d.isoformat())).exists():
            dates.append(d)
        d += dt.timedelta(days=1)
    return dates


# ----------------------------------------------------------------------------
# Resolve-Logik
# ----------------------------------------------------------------------------

def _check_timing_stale(ticket_date: dt.date, ticket_code: str,
                        tools_dir: Path) -> tuple[bool, str]:
    """V7.9.5 TICKET-HYGIENE-2: Timing-aware Stale-Detection.

    Wenn der Ticket-Code ein `*_JSON_MISSING`-Symptom ist UND das
    referenzierte Upstream-Artefakt im tools/-Verzeichnis existiert,
    ist das Ticket ein reines Timing-Artefakt (data_health lief zu frueh).
    In dem Fall: resolve als "timing-stale" mit Upstream-mtime als Evidenz.

    Diese Regel braucht KEINEN data_health-Re-Run: sie wirkt auch dann,
    wenn data_health heute nur einmal (morgens) gelaufen ist und der
    Upstream erst danach angekommen ist. Adressiert DATA-HEALTH-TIMING-1
    solange SCAN-OF-ORCHESTRATION-1 nicht gefixt ist.
    """
    artifact = JSON_MISSING_ARTIFACT.get(ticket_code or "")
    if not artifact:
        return False, ""
    p = tools_dir / f"{artifact}_{ticket_date.isoformat()}.json"
    if not p.exists():
        return False, ""
    try:
        mtime_dt = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).replace(tzinfo=None)
        mtime_iso = mtime_dt.strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        mtime_iso = "unknown"
    reason = (
        f"timing-stale: {p.name} inzwischen vorhanden (mtime {mtime_iso}); "
        f"data_health lief vor dem Upstream-Artefakt (DATA-HEALTH-TIMING-1)"
    )
    return True, reason


def _decide_health(ticket_date: dt.date, fp: str, today: dt.date,
                   health_fps_by_date: dict[str, set[str]],
                   ticket_code: str = "",
                   tools_dir: Path | None = None) -> tuple[bool, str]:
    """Return (should_resolve, reason).

    V7.9.5: optionale ticket_code- und tools_dir-Argumente fuer die
    Timing-aware-Regel. Default-Argumente halten Rueckwaertskompat.
    """
    iso = ticket_date.isoformat()
    effective_tools = tools_dir if tools_dir is not None else TOOLS_DIR
    # Regel 1: ticket_date < today UND data_health vom Ticket-Tag existiert
    if ticket_date < today:
        if iso in health_fps_by_date:
            # per-Tag-FP ist historisiert; neue Tage tragen neue fps
            return True, f"historisiert (data_health_{iso}.json vorhanden; fp kann per Konstruktion nicht mehr auftauchen)"
        # Evidenz fehlt -> sicherheitshalber NICHT schliessen
        return False, f"skip: data_health_{iso}.json nicht vorhanden, keine Historisierungs-Evidenz"
    # Regel 2: ticket_date == today UND fp nicht mehr in heutiger findings
    if ticket_date == today:
        today_fps = health_fps_by_date.get(iso, set())
        if fp not in today_fps:
            return True, f"stale: fp nicht in aktueller data_health_{iso}.json (spaeterer Re-Run hat's rausgeworfen)"
        # Regel 3 NEU V7.9.5: timing-stale wenn das Upstream-Artefakt
        # inzwischen gelandet ist, selbst wenn data_health nicht re-ran.
        if ticket_code:
            ok, reason = _check_timing_stale(ticket_date, ticket_code, effective_tools)
            if ok:
                return True, reason
        return False, "skip: fp noch in heutigen findings, aktiv"
    # ticket_date > today sollte nie passieren
    return False, "skip: ticket in der Zukunft (ignoriert)"


def _decide_tripwire(ticket_date: dt.date, fp: str, ticket_code: str, today: dt.date,
                     trip_fps: dict[str, set[str]],
                     trip_codes: dict[str, set[str]]) -> tuple[bool, str]:
    iso = ticket_date.isoformat()
    is_warn = "WARN" in ticket_code
    # Supersession: wenn spaeter oder heute ein CRITICAL existiert, WARN -> closed
    if is_warn:
        for d_iso, codes in sorted(trip_codes.items()):
            if d_iso < iso:
                continue
            if any("CRITICAL" in c for c in codes):
                return True, f"superseded by TRIPWIRE_STREAK_CRITICAL am {d_iso}"
    if ticket_date < today:
        if iso in trip_fps:
            return True, f"historisiert (tripwire_{iso}.json vorhanden; tagesgenauer fp abgeschlossen)"
        return False, f"skip: tripwire_{iso}.json nicht vorhanden"
    if ticket_date == today:
        today_fps = trip_fps.get(iso, set())
        if fp not in today_fps:
            return True, f"stale: fp nicht in aktueller tripwire_{iso}.json"
        return False, "skip: fp noch aktiv in heutigen findings"
    return False, "skip: ticket in der Zukunft"


# ----------------------------------------------------------------------------
# File Transform
# ----------------------------------------------------------------------------

def scan_and_transform(since: dt.date | None, today: dt.date) -> tuple[list[str], list[dict]]:
    """Liest ops_tickets.md, entscheidet pro Zeile, baut neue Zeilen.
    Return: (new_lines, actions[])."""
    if not TICKETS_PATH.exists():
        raise FileNotFoundError(f"ops_tickets.md nicht gefunden: {TICKETS_PATH}")
    raw = TICKETS_PATH.read_text(encoding="utf-8").splitlines()

    # Date-Ranges sammeln
    earliest = since or dt.date(2026, 1, 1)
    latest = today
    health_fps = _health_fps_by_date(_collect_health_dates(earliest, latest))
    trip_fps, trip_codes = _tripwire_fps_and_codes(_collect_tripwire_dates(earliest, latest))

    new_lines: list[str] = []
    actions: list[dict] = []
    for line in raw:
        m = RE_TICKET.match(line)
        if not m:
            new_lines.append(line)
            continue
        ticket_date = _parse_date(m.group("date"))
        if since and ticket_date < since:
            new_lines.append(line)
            continue
        fp = m.group("fp")
        kind = m.group("kind")
        body = m.group("body")
        # Code aus body extrahieren (fuer tripwire-Supersession)
        code_match = re.search(r"\*\*\[auto:(?:data_health|tripwire)\]\s+([A-Z0-9_]+)\*\*", body)
        ticket_code = code_match.group(1) if code_match else ""

        if kind == "health-fp":
            should, reason = _decide_health(
                ticket_date, fp, today, health_fps,
                ticket_code=ticket_code, tools_dir=TOOLS_DIR,
            )
        elif kind == "tripwire-fp":
            should, reason = _decide_tripwire(ticket_date, fp, ticket_code, today, trip_fps, trip_codes)
        else:
            should, reason = False, "skip: unknown marker kind"

        if not should:
            new_lines.append(line)
            actions.append({
                "date": ticket_date.isoformat(),
                "code": ticket_code,
                "fp": fp,
                "kind": kind,
                "action": "keep_open",
                "reason": reason,
            })
            continue

        # Neue Zeile bauen: OPEN -> FIXED <today> (auto-resolved: <reason>) <marker>
        # Marker wird beibehalten, damit data_health.py keinen neuen fp eroeffnet.
        replacement = f"FIXED {today.isoformat()} (auto-resolved: {reason}) {m.group('marker')}"
        new_line = re.sub(
            r"OPEN\s*<!--.*?-->",
            replacement,
            line,
        )
        new_lines.append(new_line)
        actions.append({
            "date": ticket_date.isoformat(),
            "code": ticket_code,
            "fp": fp,
            "kind": kind,
            "action": "auto_resolved",
            "reason": reason,
        })

    return new_lines, actions


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="tatsaechlich in ops_tickets.md schreiben (sonst Dry-Run)")
    ap.add_argument("--dry-run", action="store_true", help="expliziter Dry-Run (Default)")
    ap.add_argument("--since", type=str, default=None,
                    help="nur Tickets mit date >= YYYY-MM-DD beruehren")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-report", action="store_true",
                    help="kein JSON-Audit-Trail schreiben")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        print("[auto_resolve] --apply und --dry-run sind widerspruechlich.", file=sys.stderr)
        return 2

    today = dt.date.today()
    since = None
    if args.since:
        try:
            since = _parse_date(args.since)
        except ValueError:
            print(f"[auto_resolve] --since: ungueltiges Datum '{args.since}'", file=sys.stderr)
            return 2

    try:
        new_lines, actions = scan_and_transform(since, today)
    except FileNotFoundError as e:
        print(f"[auto_resolve] {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[auto_resolve] Fehler: {e}", file=sys.stderr)
        return 1

    resolved = [a for a in actions if a["action"] == "auto_resolved"]
    kept = [a for a in actions if a["action"] == "keep_open"]

    if not args.quiet:
        print(f"[auto_resolve] today={today.isoformat()} since={since} candidates={len(actions)} resolved={len(resolved)} kept_open={len(kept)}")
        for a in resolved[:25]:
            print(f"  RESOLVE {a['date']} {a['kind']:12s} {a['fp']}  {a['code'] or '(no-code)':38s} :: {a['reason']}")
        if len(resolved) > 25:
            print(f"  ... ({len(resolved)-25} weitere RESOLVE)")

    if args.apply and resolved:
        # Backup
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = TICKETS_PATH.with_suffix(f".md.bak_{stamp}")
        try:
            shutil.copy2(TICKETS_PATH, backup)
        except Exception as e:
            print(f"[auto_resolve] Backup fehlgeschlagen: {e}", file=sys.stderr)
            return 1
        try:
            TICKETS_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[auto_resolve] Schreibfehler: {e}", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"[auto_resolve] applied -> {TICKETS_PATH.name} ({len(resolved)} resolved, backup={backup.name})")

    # Audit-JSON
    if not args.no_report:
        report_path = TOOLS_DIR / f"ticket_auto_resolve_{today.isoformat()}.json"
        report = {
            "date": today.isoformat(),
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "version": "ticket_auto_resolve/1.0",
            "mode": "apply" if args.apply else "dry_run",
            "since": since.isoformat() if since else None,
            "counts": {
                "candidates": len(actions),
                "resolved": len(resolved),
                "kept_open": len(kept),
            },
            "resolved": resolved,
            "kept_open_sample": kept[:20],
        }
        try:
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            if not args.quiet:
                print(f"[auto_resolve] audit -> {report_path.name}")
        except Exception as e:
            print(f"[auto_resolve] audit-write fehlgeschlagen: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
