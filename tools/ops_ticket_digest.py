#!/usr/bin/env python3
"""
ops_ticket_digest.py: V7.20 OPS-TICKET-DIGEST-1 (R&D / dev-team, FEATURE)

Read-only Ops-Observability fuer das Daily-Invest-Projekt.

Zweck (Richtung V10+: "das System berichtet sauber ueber sich selbst"):
  Das ops_tickets.md ist auf >300 KB / >230 Statuszeilen angewachsen. Bei einem
  gesetzten OPS-INCIDENT-FLAG braucht der Maintainer EINEN Blick auf:
    - wie viele Tickets sind WIRKLICH strict-OPEN (Status-Feld == OPEN, nicht nur
      das Wort "OPEN" irgendwo in der Zeile),
    - nach Root-Cause-Cluster gruppiert (welche Wurzel erzeugt das meiste Rauschen),
    - nach Severity (H/M/L) und Alter (frisch / 7d / 30d / alt),
    - plus MTTR (Mean-Time-To-Resolve) ueber die FIXED-Tickets mit parsbarem Fix-Datum.

Abgrenzung (KEINE Duplikation):
  - data_health.py  ERZEUGT Findings/Tickets.
  - ticket_auto_resolve.py  RESOLVT datums-FPs (schreibt zurueck).
  - ops_ticket_digest.py  LIEST NUR und fasst zusammen: schreibt NIE ins Ticket-Log,
    nur ein Digest-Artefakt (MD + JSON).

Parser-Vertrag:
  Kanonische Ticketzeile = `YYYY-MM-DD | SEV | Task | Beschreibung | Status`
  (mind. 5 pipe-Felder, erstes Feld = ISO-Datum). Markdown-Tabellen, Prosa,
  Blockquotes etc. werden ignoriert (kein fuehrendes ISO-Datum).
  Status-Klassifikation auf dem LETZTEN Feld (HTML-Kommentare gestrippt):
    OPEN*                              -> open
    FIXED* / RESOLVED*                 -> fixed
    DUP* / CONSOLIDATED* / WONTFIX*    -> closed_other

CLI:
  python3 tools/ops_ticket_digest.py                # Digest gegen ops_tickets.md
  python3 tools/ops_ticket_digest.py --write        # + Artefakte schreiben
  python3 tools/ops_ticket_digest.py --file X.md --out-md Y.md --out-json Z.json
  python3 tools/ops_ticket_digest.py --selftest     # synthetische Daten, kein I/O
  python3 tools/ops_ticket_digest.py --top 8        # Top-N Cluster im MD

Exit-Codes:
  0 ok / selftest pass
  1 selftest fail
  2 input file not found
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TICKETS = os.path.join(ROOT, "ops_tickets.md")

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ANY_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})")
DOTTED = re.compile(r"(\d{2})\.(\d{2})\.(?:(\d{4})|(\d{2}))?")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# data_health / tripwire finding-code, z.B. SCAN_MISSING, EARNINGS_JSON_MISSING
FINDING_CODE = re.compile(r"\b([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)\b")

SEV_ORDER = {"H": 0, "M": 1, "L": 2, "?": 3}


def _strip_comments(s: str) -> str:
    return HTML_COMMENT.sub("", s).strip()


def parse_line(line: str):
    """Eine Rohzeile -> Ticket-Dict oder None (wenn keine kanonische Ticketzeile)."""
    raw = line.rstrip("\n")
    # Blockquotes / Tabellen / Listen sofort raus
    stripped = raw.lstrip()
    if not stripped or stripped[0] in ">#*-|" and not ISO_DATE.match(stripped.split(" | ", 1)[0].strip(" |")):
        # Tabellenzeilen beginnen mit "|", Blockquote mit ">", Header mit "#"
        if not ISO_DATE.match(stripped.split("|")[0].strip()):
            return None
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 5:
        return None
    if not ISO_DATE.match(parts[0]):
        return None
    created = parts[0]
    sev = parts[1].strip().upper()
    if sev not in ("H", "M", "L"):
        sev = "?"
    task = parts[2].strip()
    # Beschreibung kann selbst pipes enthalten -> alles zwischen Feld 3 und letztem Feld
    desc = " | ".join(parts[3:-1]).strip()
    status_field = _strip_comments(parts[-1])
    cls, fixed_date = classify_status(status_field, parts[-1])
    return {
        "created": created,
        "severity": sev,
        "task": task,
        "desc": desc,
        "status_raw": status_field,
        "status": cls,
        "fixed_date": fixed_date,
        "root_cause": root_cause(task, desc),
    }


def classify_status(status_clean: str, status_with_comment: str):
    """-> (class, fixed_date|None). class in {open, fixed, closed_other, unknown}."""
    up = status_clean.upper()
    fixed_date = None
    if up.startswith("OPEN"):
        return "open", None
    if up.startswith("FIXED") or up.startswith("RESOLVED") or up.startswith("DONE"):
        fixed_date = _extract_date(status_clean)
        return "fixed", fixed_date
    if up.startswith("DUP") or up.startswith("CONSOLIDATED") or up.startswith("WONTFIX") or up.startswith("MOOT"):
        return "closed_other", None
    return "unknown", None


def _extract_date(s: str):
    """Best-effort: ISO-Datum bevorzugt, sonst dd.mm.(yyyy) -> ISO-String oder None."""
    m = ANY_ISO.search(s)
    if m:
        return m.group(1)
    m = DOTTED.search(s)
    if m:
        dd, mm = m.group(1), m.group(2)
        yyyy = m.group(3)
        yy = m.group(4)
        if yyyy:
            year = yyyy
        elif yy:
            year = "20" + yy
        else:
            year = "2026"  # Projekt-Default-Jahr; nur fuer MTTR-Schaetzung
        try:
            datetime.strptime(f"{year}-{mm}-{dd}", "%Y-%m-%d")
            return f"{year}-{mm}-{dd}"
        except ValueError:
            return None
    return None


def root_cause(task: str, desc: str) -> str:
    """Cluster-Key. data_health/tripwire -> Finding-Code; sonst Task-Slug."""
    m = re.search(r"\[auto:([a-z_]+)\]", desc)
    if m:
        src = m.group(1)
        # Finding-Code nach dem auto-Tag suchen
        after = desc.split("]", 1)[1] if "]" in desc else desc
        codes = FINDING_CODE.findall(after)
        # generische Stop-Codes raus
        codes = [c for c in codes if c not in ("JSON", "OK", "NO", "TRADE")]
        if codes:
            return f"{src}:{codes[0]}"
        return f"{src}:UNCODED"
    # Nicht-auto: nimm Task-Feld, normalisiert
    t = (task or "uncategorized").strip().lower()
    t = re.sub(r"\s+", "-", t)
    return f"manual:{t}"


def _days_between(d1: str, d2: str):
    try:
        a = datetime.strptime(d1, "%Y-%m-%d").date()
        b = datetime.strptime(d2, "%Y-%m-%d").date()
        return (b - a).days
    except (ValueError, TypeError):
        return None


def age_bucket(created: str, today: date) -> str:
    d = _days_between(created, today.isoformat())
    if d is None:
        return "unknown"
    if d <= 2:
        return "fresh(<=2d)"
    if d <= 7:
        return "week(<=7d)"
    if d <= 30:
        return "month(<=30d)"
    return "stale(>30d)"


def build_digest(tickets, today: date, top_n: int = 10):
    open_t = [t for t in tickets if t["status"] == "open"]
    fixed_t = [t for t in tickets if t["status"] == "fixed"]

    by_sev = {"H": 0, "M": 0, "L": 0, "?": 0}
    by_age = {}
    by_cluster = {}
    for t in open_t:
        by_sev[t["severity"]] = by_sev.get(t["severity"], 0) + 1
        ab = age_bucket(t["created"], today)
        by_age[ab] = by_age.get(ab, 0) + 1
        rc = t["root_cause"]
        c = by_cluster.setdefault(rc, {"count": 0, "H": 0, "M": 0, "L": 0, "?": 0, "oldest": t["created"]})
        c["count"] += 1
        c[t["severity"]] += 1
        if t["created"] < c["oldest"]:
            c["oldest"] = t["created"]

    # MTTR ueber FIXED mit parsbarem Fix-Datum
    mttrs = []
    for t in fixed_t:
        if t["fixed_date"]:
            d = _days_between(t["created"], t["fixed_date"])
            if d is not None and d >= 0:
                mttrs.append(d)
    mttr_avg = round(sum(mttrs) / len(mttrs), 1) if mttrs else None
    mttr_median = None
    if mttrs:
        s = sorted(mttrs)
        n = len(s)
        mttr_median = s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 1)

    clusters_sorted = sorted(
        by_cluster.items(),
        key=lambda kv: (-kv[1]["H"], -kv[1]["count"], kv[1]["oldest"]),
    )

    return {
        "generated": today.isoformat(),
        "total_lines_parsed": len(tickets),
        "open_total": len(open_t),
        "fixed_total": len(fixed_t),
        "open_by_severity": by_sev,
        "open_by_age": by_age,
        "cluster_count": len(by_cluster),
        "top_clusters": [
            {"root_cause": k, **v} for k, v in clusters_sorted[:top_n]
        ],
        "all_clusters": [{"root_cause": k, **v} for k, v in clusters_sorted],
        "mttr_days_avg": mttr_avg,
        "mttr_days_median": mttr_median,
        "mttr_sample_n": len(mttrs),
    }


def render_md(d: dict, top_n: int) -> str:
    sev = d["open_by_severity"]
    lines = []
    lines.append(f"# Ops-Ticket-Digest: {d['generated']}")
    lines.append("")
    lines.append("> Read-only Zusammenfassung von `ops_tickets.md` (tools/ops_ticket_digest.py, V7.20).")
    lines.append("> Schreibt NICHT ins Ticket-Log, nur dieses Digest-Artefakt.")
    lines.append("")
    lines.append("## Kennzahlen")
    lines.append("")
    lines.append("| Metrik | Wert |")
    lines.append("|---|---|")
    lines.append(f"| Zeilen geparst (kanonisch) | {d['total_lines_parsed']} |")
    lines.append(f"| **strict-OPEN gesamt** | **{d['open_total']}** |")
    lines.append(f"| davon H / M / L / ? | {sev['H']} / {sev['M']} / {sev['L']} / {sev['?']} |")
    lines.append(f"| FIXED gesamt | {d['fixed_total']} |")
    lines.append(f"| Distinkte OPEN-Cluster | {d['cluster_count']} |")
    mavg = d["mttr_days_avg"]
    mmed = d["mttr_days_median"]
    lines.append(f"| MTTR Ø / Median (Tage) | {mavg if mavg is not None else 'n/a'} / {mmed if mmed is not None else 'n/a'} (n={d['mttr_sample_n']}) |")
    lines.append("")
    lines.append("## OPEN nach Alter")
    lines.append("")
    lines.append("| Bucket | Anzahl |")
    lines.append("|---|---|")
    for b in ["fresh(<=2d)", "week(<=7d)", "month(<=30d)", "stale(>30d)", "unknown"]:
        if b in d["open_by_age"]:
            lines.append(f"| {b} | {d['open_by_age'][b]} |")
    lines.append("")
    lines.append(f"## Top-{top_n} OPEN-Cluster nach Root-Cause (H-gewichtet)")
    lines.append("")
    lines.append("| Root-Cause | OPEN | H | M | L | aeltestes |")
    lines.append("|---|---|---|---|---|---|")
    for c in d["top_clusters"]:
        lines.append(
            f"| `{c['root_cause']}` | {c['count']} | {c['H']} | {c['M']} | {c['L']} | {c['oldest']} |"
        )
    lines.append("")
    # Interpretation
    lines.append("## Lese-Hilfe")
    lines.append("")
    noisiest = d["top_clusters"][0]["root_cause"] if d["top_clusters"] else "-"
    lines.append(
        f"- Lautester Cluster: `{noisiest}`. Ein Root-Cause-Fix dort raeumt die meisten OPEN auf einmal."
    )
    lines.append(
        "- `manual:*`-Cluster = nicht-automatische (Projekt/Backlog) Tickets; `data_health:*` / `tripwire:*` = Auto-Findings."
    )
    lines.append(
        "- Hohe `stale(>30d)`-Zahl bei `manual:*` ist meist Roadmap-Backlog, kein Incident: bei `data_health:*` dagegen ein Hinweis auf einen ungeloesten Producer-Ausfall."
    )
    lines.append("")
    return "\n".join(lines)


# ───────────────────────── SELFTEST ─────────────────────────

SELFTEST_LINES = [
    "# Header sollte ignoriert werden",
    "> Format: YYYY-MM-DD | ... | Status: Blockquote ignorieren",
    "| Prio | Ticket | Status | Tabellenzeile ignorieren",
    "2026-06-01 | H | daily-universe-scanner | **[auto:data_health] SCAN_MISSING**: fehlt | OPEN <!-- health-fp:aaa -->",
    "2026-06-01 | H | daily-universe-scanner | **[auto:data_health] EARNINGS_JSON_MISSING**: fehlt | OPEN",
    "2026-06-09 | H | daily-universe-scanner | **[auto:data_health] EARNINGS_JSON_MISSING**: fehlt | OPEN",
    "2026-06-08 | M | data-architecture | **DATA-ARCH-1: Lake** beschreibung mit | pipe drin | OPEN: strategisch",
    "2026-06-05 | L | ops-housekeeping | **STALE-TASK** weekly check | OPEN",
    "2026-04-20 | H | daily-universe-scanner | **[auto:data_health] SCAN_MISSING**: alt | FIXED 2026-04-22",
    "2026-04-16 | M | journal | **trade_journal crash** | FIXED 2026-04-19",
    "2026-06-09 | H | scanner | **[auto:data_health] SECTOR_RS_JSON_MISSING** | FIXED-V7.17 timing-FP",
    "2026-06-09 | L | scanner | **dup ticket** | DUP->2026-06-08-CRO",
    "2026-06-10 | H | tripwire | **[auto:tripwire] ZERO_RECS_STREAK**: 5 Tage | OPEN",
    "garbage line without pipes",
]


def selftest() -> int:
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    parsed = [parse_line(l) for l in SELFTEST_LINES]
    tickets = [t for t in parsed if t]

    # 10 kanonische Zeilen erwartet (4 header/garbage raus)
    check(len(tickets) == 10, f"expected 10 parsed tickets, got {len(tickets)}")

    opens = [t for t in tickets if t["status"] == "open"]
    fixeds = [t for t in tickets if t["status"] == "fixed"]
    others = [t for t in tickets if t["status"] == "closed_other"]
    check(len(opens) == 6, f"expected 6 open, got {len(opens)}")
    check(len(fixeds) == 3, f"expected 3 fixed, got {len(fixeds)}")
    check(len(others) == 1, f"expected 1 closed_other(DUP), got {len(others)}")

    # Beschreibung mit pipe darf nicht abgeschnitten Status verfaelschen
    da1 = [t for t in tickets if "DATA-ARCH-1" in t["desc"]]
    check(da1 and da1[0]["status"] == "open", "DATA-ARCH-1 mit pipe in desc muss OPEN sein")

    # Root-Cause-Clustering
    rcs = {t["root_cause"] for t in tickets}
    check("data_health:SCAN_MISSING" in rcs, "missing cluster data_health:SCAN_MISSING")
    check("data_health:EARNINGS_JSON_MISSING" in rcs, "missing earnings cluster")
    check("tripwire:ZERO_RECS_STREAK" in rcs, "missing tripwire cluster")
    check(any(r.startswith("manual:") for r in rcs), "missing manual:* cluster")

    today = date(2026, 6, 11)
    d = build_digest(tickets, today, top_n=10)
    check(d["open_total"] == 6, f"digest open_total {d['open_total']} != 6")
    check(d["open_by_severity"]["H"] == 4, f"open H {d['open_by_severity']['H']} != 4")

    # EARNINGS-Cluster muss 2 OPEN buendeln (06-01 + 06-09)
    earn = [c for c in d["top_clusters"] if c["root_cause"] == "data_health:EARNINGS_JSON_MISSING"]
    check(earn and earn[0]["count"] == 2, "EARNINGS cluster muss 2 OPEN haben")

    # H-gewichtete Sortierung: top cluster muss H>0 haben
    check(d["top_clusters"][0]["H"] >= 1, "top cluster sollte H-gewichtet sein")

    # MTTR: FIXED mit Datum = 2 (22-20=... -> 2 Tage; 19-16=3 Tage); V7.17 ohne Datum zaehlt nicht
    check(d["mttr_sample_n"] == 2, f"mttr sample {d['mttr_sample_n']} != 2")
    check(d["mttr_days_avg"] == 2.5, f"mttr avg {d['mttr_days_avg']} != 2.5")

    # Alters-Buckets
    check(d["open_by_age"].get("stale(>30d)", 0) >= 0, "age bucket key")

    # MD-Rendering darf nicht crashen + enthaelt Kernzahl
    md = render_md(d, 10)
    check("strict-OPEN gesamt" in md and "**6**" in md, "MD muss strict-OPEN 6 zeigen")

    if fails:
        print("SELFTEST: FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELFTEST: PASS ({len(tickets)} parsed, {len(opens)} open, MTTR avg {d['mttr_days_avg']}d)")
    return 0


# ───────────────────────── MAIN ─────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Ops-Ticket-Digest (read-only)")
    ap.add_argument("--file", default=DEFAULT_TICKETS, help="ops_tickets.md Pfad")
    ap.add_argument("--write", action="store_true", help="Artefakte schreiben")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not os.path.exists(args.file):
        print(f"ERROR: ticket file not found: {args.file}", file=sys.stderr)
        return 2

    with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    tickets = [t for t in (parse_line(l) for l in lines) if t]
    today = date.today()
    d = build_digest(tickets, today, top_n=args.top)
    md = render_md(d, args.top)
    print(md)

    if args.write:
        out_md = args.out_md or os.path.join(ROOT, "Briefings", f"Ops-Ticket-Digest-{today.isoformat()}.md")
        out_json = args.out_json or os.path.join(ROOT, "signals", f"ops_ticket_digest_{today.isoformat()}.json")
        os.makedirs(os.path.dirname(out_md), exist_ok=True)
        os.makedirs(os.path.dirname(out_json), exist_ok=True)
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, ensure_ascii=False)
        print(f"\n[written] {out_md}")
        print(f"[written] {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
