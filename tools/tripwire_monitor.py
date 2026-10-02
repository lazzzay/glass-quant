#!/usr/bin/env python3
"""
tripwire_monitor.py: Null-Kandidaten-Wächter für das Observation-Window

Zweck
=====
Seit der V6.1-Umstellung am 2026-04-10 ist die Kandidaten-Rate von ~83 %
(V6-Ära) auf 17 % gefallen (1 Rec in 6 Handelstagen). Das Design-Soll sind
258 Trades / 3 Jahre ≈ 33 %. Der Verdacht: multiplikatives Filter-Stacking
aus V6.3-V6.8 erzeugt einen Score-Deckel, der in Trending-Regimes praktisch
nicht mehr erreicht wird.

Dieser Wächter beobachtet die tatsächliche Kandidaten-Rate und schlägt
automatisch Alarm, wenn wir unter harte Schwellen fallen:

    * 5+ aufeinanderfolgende Null-Kandidaten-Handelstage → CRITICAL
    * weniger als 2 Kandidaten in den letzten 14 Handelstagen → CRITICAL
    * 3-4 Null-Tage in Folge oder <4 Kandidaten in 14 Tagen → WARN

Er schlägt explizit KEINE Score-Änderungen vor: der Feature-Freeze bis
2026-04-29 bleibt unverändert. Die Aufgabe hier ist ausschließlich, die
Entscheidungs-Pflicht am 2026-04-30 (COHORT-AUDIT-1) unvermeidbar zu machen.

Input: parst die eigenen, lokal erzeugten Trading-Empfehlung-*.md-Dateien
(privates Format, gitignored, nie veroeffentlichen; dieses Projekt erzeugt sie
nicht). Ohne solche Dateien gibt es nichts auszuwerten. Die Ausgaben sind
Kandidaten-Statistik, keine Anlageempfehlung.

Usage
-----
    python3 tools/tripwire_monitor.py                     # heute auswerten
    python3 tools/tripwire_monitor.py --date 2026-04-17   # konkretes Datum
    python3 tools/tripwire_monitor.py --no-tickets        # kein Ticket-Write
    python3 tools/tripwire_monitor.py --quiet

Exit-Codes
----------
    0: OK  (Kandidaten-Rate nominell)
    1: WARN  (Degradiert, aber noch nicht über Schwelle)
    2: CRITICAL  (Tripwire gefeuert, Cohort-Audit zwingend)

Design-Prinzipien
-----------------
* Free-Stack-Only (stdlib), keine externen Deps.
* Idempotent: gleicher Input → gleiches Ticket-Fingerprint, keine Duplikate.
* Fail-Safe: niemals das Briefing blockieren.
* Transparent: jede Zahl ist im JSON-Report auf Quelldatei-Ebene zurückverfolgbar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
TICKETS_PATH = PROJECT_ROOT / "ops_tickets.md"
ARCHIV_DIR = PROJECT_ROOT / "Archiv"

# -----------------------------------------------------------------------------
# Konstanten: bewusst prominent, damit der Cohort-Audit sie als Stellschraube
# erkennt. Änderungen hier werden im Audit explizit protokolliert.
# -----------------------------------------------------------------------------

LOOKBACK_TRADING_DAYS = 14
DESIGN_TARGET_RATE = 0.33  # 258/3a ≈ 86/Jahr / ~260 Handelstage

STREAK_WARN = 3
STREAK_CRITICAL = 5
COUNT_CRITICAL_14D = 2
COUNT_WARN_14D = 4

# -----------------------------------------------------------------------------
# Rec-Counter: liest eine Empfehlungs-Markdown und gibt Anzahl neuer LONG-Entries
# -----------------------------------------------------------------------------

ZERO_PATTERNS = [
    r"0\s+neue?\s+Empfehlungen",
    r"0\s+Empfehlungen",
    r"0\s+neue?\s+Entries",
    r"KEIN\s+TRADE\s+HEUTE",
    r"🚫\s+KEIN\s+TRADE",
    r"Empfehlungs-Verdict\*{0,2}\s*\|\s*🟡\s*\*{0,2}Defensiv",
]

POSITIVE_PATTERNS = [
    # "### 🟢 **1 Empfehlung: XYZ (Example Corp), LONG**"  (synthetisches Beispiel)
    r"###\s*🟢?\s*\*{0,2}(\d+)\s+Empfehlung(?:en)?\b",
    # "0 neue Empfehlungen: Threshold..." (negative shape, handled elsewhere)
    r"(\d+)\s+neue?\s+Empfehlungen?\s*[—\-:]",
]

KAUF_HEADING = re.compile(r"^###\s+\*{0,2}(?:🟢\s*)?KAUF\s+\d", re.MULTILINE)
MARKET_CLOSED = re.compile(r"MÄRKTE\s+GESCHLOSSEN|markets?\s+closed|Karfreitag|Feiertag",
                            re.IGNORECASE)


def classify_rec_file(path: Path) -> dict:
    """Zählt neue LONG-Empfehlungen in einer Empfehlungs-Markdown.

    Returns dict mit:
        status: "market_closed" | "zero" | "positive" | "indeterminate"
        count:  int oder None
        evidence: str (erstes Match für Debugging)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "indeterminate", "count": None, "evidence": f"read_error: {e}"}

    # Reihenfolge bewusst: explizite Empfehlungs-Zählung hat Vorrang. Erst wenn
    # weder positive noch explizite Null-Signale auffindbar sind, werten wir
    # "MÄRKTE GESCHLOSSEN"-Texte, sonst werden normale Werktage fälschlich als
    # Feiertag klassifiziert, wenn das Template-Cross-Learning den Karfreitag
    # historisch referenziert.
    header = "\n".join(text.splitlines()[:200])
    opening = "\n".join(text.splitlines()[:20])  # echte Header-Zeilen

    # Primär: positive explizite Zählung
    for pat in POSITIVE_PATTERNS:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if n == 0:
                return {"status": "zero", "count": 0, "evidence": m.group(0)[:80]}
            return {"status": "positive", "count": n, "evidence": m.group(0)[:80]}

    # Sekundär: Null-Indikatoren
    for pat in ZERO_PATTERNS:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            return {"status": "zero", "count": 0, "evidence": m.group(0)[:80]}

    # Tertiär: Markt-geschlossen nur im echten File-Header (erste 20 Zeilen),
    # damit historische Cross-Learning-Notes über Karfreitag normale Tage nicht
    # als Feiertag fehlklassifizieren.
    if MARKET_CLOSED.search(opening):
        return {"status": "market_closed", "count": None, "evidence": "market_closed_header"}

    # Fallback: KAUF-N Headings in §Empfehlungen zählen
    kaufs = len(KAUF_HEADING.findall(text))
    if kaufs > 0:
        return {"status": "positive", "count": kaufs, "evidence": f"kauf_headings={kaufs}"}

    return {"status": "indeterminate", "count": None, "evidence": "no_pattern_matched"}


# -----------------------------------------------------------------------------
# Dateisuche: Archiv + Working-Directory
# -----------------------------------------------------------------------------

def find_rec_file(date: dt.date) -> Path | None:
    """Findet die Empfehlungs-MD für ein konkretes Datum.
    Sucht zuerst im Archiv, dann im Projekt-Wurzelverzeichnis.
    """
    iso = date.isoformat()
    month_dir = ARCHIV_DIR / f"{date.year:04d}-{date.month:02d}" / "Empfehlungen"
    candidates: list[Path] = []
    if month_dir.exists():
        candidates.extend(month_dir.glob(f"Trading-Empfehlung-*{iso}*.md"))
        candidates.extend(month_dir.glob(f"Daily-Briefing-*{iso}*.md"))
    # Working-dir (heutige, noch nicht archiviert)
    candidates.extend(PROJECT_ROOT.glob(f"Trading-Empfehlung-*{iso}*.md"))
    candidates = [c for c in candidates if c.is_file()]
    if not candidates:
        return None
    # Bevorzuge V6.1 über V6 (neuere Version)
    candidates.sort(key=lambda p: (("V6.1" in p.name), p.stat().st_mtime), reverse=True)
    return candidates[0]


def trading_days_back(today: dt.date, n: int) -> list[dt.date]:
    """Liefert die letzten n Handelstage (Mo-Fr) inkl. heute."""
    days: list[dt.date] = []
    d = today
    while len(days) < n:
        if d.weekday() < 5:  # 0=Mo, 4=Fr
            days.append(d)
        d -= dt.timedelta(days=1)
    return days  # jüngst zuerst


# -----------------------------------------------------------------------------
# Analyse-Kern
# -----------------------------------------------------------------------------

def analyze(today: dt.date) -> dict:
    days = trading_days_back(today, LOOKBACK_TRADING_DAYS)

    entries: list[dict] = []
    for d in days:
        path = find_rec_file(d)
        entry: dict[str, Any] = {
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "file": path.name if path else None,
            "status": "missing" if not path else None,
            "count": None,
            "evidence": None,
        }
        if path:
            info = classify_rec_file(path)
            entry["status"] = info["status"]
            entry["count"] = info["count"]
            entry["evidence"] = info["evidence"]
        entries.append(entry)

    # Streak berechnen (aufeinanderfolgende Null-Tage inkl. heute, rückwärts)
    streak = 0
    for e in entries:
        if e["status"] == "zero":
            streak += 1
        elif e["status"] in ("market_closed", "missing"):
            # Weder Null noch Positive: Streak pausiert, nicht zurückgesetzt
            continue
        else:
            break

    # Anzahl Empfehlungen + Handelstage im 14-Fenster
    total_recs = sum((e["count"] or 0) for e in entries if e["status"] == "positive")
    counted_days = sum(1 for e in entries if e["status"] in ("zero", "positive"))
    rec_rate = (total_recs / counted_days) if counted_days else None

    # Severity-Bestimmung
    findings: list[dict] = []
    if streak >= STREAK_CRITICAL:
        findings.append({
            "severity": "H",
            "code": "TRIPWIRE_STREAK_CRITICAL",
            "message": f"{streak} aufeinanderfolgende Null-Kandidaten-Handelstage (Schwelle ≥{STREAK_CRITICAL}). Cohort-Audit ist jetzt zwingend.",
        })
    elif streak >= STREAK_WARN:
        findings.append({
            "severity": "M",
            "code": "TRIPWIRE_STREAK_WARN",
            "message": f"{streak} aufeinanderfolgende Null-Kandidaten-Handelstage (Warn-Schwelle ≥{STREAK_WARN}). Noch nicht zwingend, aber Pre-Audit-Vorbereitung beschleunigen.",
        })

    if counted_days >= 5:  # nicht feuern, wenn wir zu wenig Daten haben
        if total_recs < COUNT_CRITICAL_14D:
            findings.append({
                "severity": "H",
                "code": "TRIPWIRE_RATE_CRITICAL",
                "message": f"Nur {total_recs} Kandidat(en) in den letzten {counted_days} Handelstagen (<{COUNT_CRITICAL_14D}). Rate {rec_rate:.1%} vs. Design {DESIGN_TARGET_RATE:.0%}.",
            })
        elif total_recs < COUNT_WARN_14D:
            findings.append({
                "severity": "M",
                "code": "TRIPWIRE_RATE_WARN",
                "message": f"Nur {total_recs} Kandidaten in {counted_days} Handelstagen (<{COUNT_WARN_14D}). Rate {rec_rate:.1%} vs. Design {DESIGN_TARGET_RATE:.0%}.",
            })

    if any(f["severity"] == "H" for f in findings):
        overall = "CRITICAL"
    elif any(f["severity"] == "M" for f in findings):
        overall = "WARN"
    else:
        overall = "OK"

    return {
        "date": today.isoformat(),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "version": "tripwire_monitor/1.0",
        "lookback_trading_days": LOOKBACK_TRADING_DAYS,
        "design_target_rate": DESIGN_TARGET_RATE,
        "overall_status": overall,
        "summary": {
            "current_zero_streak": streak,
            "total_recs_window": total_recs,
            "counted_trading_days": counted_days,
            "rec_rate_window": round(rec_rate, 4) if rec_rate is not None else None,
            "rec_rate_vs_design": round((rec_rate / DESIGN_TARGET_RATE), 3) if rec_rate else None,
        },
        "findings": findings,
        "entries": entries,
    }


# -----------------------------------------------------------------------------
# Ticket-Writer mit Dedup (kompatibel zu data_health.py-Format)
# -----------------------------------------------------------------------------

TICKET_FP_TAG = "<!-- tripwire-fp:"


def _fingerprint(date: str, code: str, streak: int, count: int) -> str:
    """Fingerprint: gleicher Befund am gleichen Tag → kein Duplikat.
    Streak+Count fließen mit ein, damit ein Eskalations-Schritt neu getriggert wird."""
    raw = f"{date}|{code}|{streak}|{count}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def _existing_fingerprints() -> set[str]:
    if not TICKETS_PATH.exists():
        return set()
    txt = TICKETS_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"<!--\s*tripwire-fp:([a-f0-9]+)\s*-->", txt))


def append_tickets(report: dict) -> int:
    findings = report.get("findings") or []
    if not findings:
        return 0
    existing = _existing_fingerprints()
    streak = report["summary"]["current_zero_streak"]
    total = report["summary"]["total_recs_window"]

    additions: list[str] = []
    for f in findings:
        fp = _fingerprint(report["date"], f["code"], streak, total)
        if fp in existing:
            continue
        line = (
            f"{report['date']} | {f['severity']} | tripwire_monitor | "
            f"**[auto:tripwire] {f['code']}**: {f['message']} "
            f"| OPEN {TICKET_FP_TAG}{fp} -->"
        )
        additions.append(line)

    if not additions:
        return 0

    txt = TICKETS_PATH.read_text(encoding="utf-8") if TICKETS_PATH.exists() else ""
    txt = txt.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"
    TICKETS_PATH.write_text(txt, encoding="utf-8")
    return len(additions)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def print_report(report: dict) -> None:
    s = report["summary"]
    print(f"\n╔══ TRIPWIRE {report['date']} ({report['overall_status']}) ══╗")
    print(f"  Null-Streak: {s['current_zero_streak']} Tage")
    rate = s.get("rec_rate_window")
    rate_str = f"{rate:.1%}" if rate is not None else "n/a"
    print(f"  Window: {s['total_recs_window']} Recs / {s['counted_trading_days']} Handelstage ({rate_str})")
    print(f"  Design-Soll: {report['design_target_rate']:.0%}, Ratio ist/soll: {s.get('rec_rate_vs_design')}")
    print(f"╚{'═' * 48}╝")
    for e in report["entries"]:
        status = e["status"] or "?"
        cnt = f"={e['count']}" if e["count"] is not None else ""
        file = e["file"] or "-"
        print(f"  {e['date']} {e['weekday']:>3}  [{status:>13}{cnt:>3}]  {file}")
    if report["findings"]:
        print("\n  Findings:")
        for f in report["findings"]:
            print(f"    [{f['severity']}] {f['code']:<30} {f['message']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Null-Kandidaten-Wächter (Tripwire, liest private Kandidaten-Markdown-Dateien)")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="ISO-Datum (default: heute)")
    ap.add_argument("--no-tickets", action="store_true", help="Tickets nicht ins ops_tickets.md schreiben")
    ap.add_argument("--quiet", action="store_true", help="Kein stdout, nur Exit-Code")
    ap.add_argument("--report-out", default=None, help="Report-Pfad override")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.date)
    report = analyze(today)

    out_path = Path(args.report_out) if args.report_out else TOOLS_DIR / f"tripwire_{args.date}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    written = 0
    if not args.no_tickets:
        written = append_tickets(report)

    if not args.quiet:
        print_report(report)
        print(f"Report: {out_path.relative_to(PROJECT_ROOT)}")
        if written:
            print(f"+ {written} neue Tripwire-Tickets in ops_tickets.md (Rest dedup'd).")
        elif not args.no_tickets:
            print("Keine neuen Tickets: Findings bereits im Log oder keine Schwellenverletzung.")

    if report["overall_status"] == "CRITICAL":
        return 2
    if report["overall_status"] == "WARN":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
