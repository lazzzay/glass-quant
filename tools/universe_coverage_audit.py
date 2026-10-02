"""
V7.9.12 UNIVERSE-COVERAGE-DIAGNOSTIC (FEATURE, KW18 Mo-Abend 2026-04-27)
======================================================================

Read-only Diagnose-Werkzeug fuer den offenen H-Ticket
**CRO-UNIVERSE-COVERAGE-1** (ops_tickets.md, 2026-04-27).

Kontext:
  Der Morgen-Cron 04:20 UTC am 27.04. lieferte scan_2026-04-27.json mit
  universe_size=320 statt 610 (52 % Coverage). insider_2026-04-27.json
  fiel parallel von 279 auf 44 n_universe. Ohne Tool muesste man die
  Luecke manuell aus den JSONs gegen tools/universe.csv ausspielen, um
  herauszufinden, welche Tier-/Region-/Asset-Cluster betroffen sind und
  ob das Audit am 29.04. auf reduzierter Datenbasis blockiert ist.

Was dieses Tool macht (PURE READING, kein Schreibzugriff auf Live-Daten):
  1. Liest tools/universe.csv (611 Zeilen, 1 Header + 610 Tickers).
  2. Liest tools/scan_<DATE>.json und nimmt die Vereinigungsmenge aus
     `top_setups[*].ticker` und Keys von `ticker_map` als
     "tatsaechlich gescannte Tickers".
  3. Vergleicht beides und produziert:
        - Coverage-Quote (%) gesamt
        - Aufriss nach tier (A/B/C/X)
        - Aufriss nach region (US/EU-Laender/Global/CN/...)
        - Aufriss nach asset_class
        - Liste der ersten 25 fehlenden Tickers (alphabetisch)
        - Gap-Cluster: zusammenhaengende Bloecke in alphabetischer Reihe,
          die strikt > 5 Eintraege haben (Indiz fuer Tier-Walk-Cutoff
          statt Random-Skip)
        - Severity-Ampel:
            GREEN  >= 95 % Coverage
            YELLOW 80 - 95 %
            RED    50 - 80 %
            BLACK  < 50 %  (Audit-blockierend)
  4. Schreibt ein JSON-Artefakt nach
        tools/universe_coverage_<DATE>.json
     und ein Markdown-Briefing nach
        Briefings/Universe-Coverage-Audit-<DATE>.md

Bewusst KEIN Live-Patch:
  Das Tool aendert weder den Scanner noch die universe.csv. Es liefert
  rein Reporting-Output. Damit ist es freeze-konform und verbraucht
  KEINE KW18-Bugfix-Quote (die ging morgens an V7.9.10).

CLI:
  python3 tools/universe_coverage_audit.py
  python3 tools/universe_coverage_audit.py --date 2026-04-23
  python3 tools/universe_coverage_audit.py --strict
  python3 tools/universe_coverage_audit.py --threshold 0.85

Exit-Codes:
   0  GREEN oder YELLOW
   0  RED   (im Default-Modus, damit Cron nicht hard-failt)
   2  RED oder BLACK mit --strict (Cron-Gate-Modus)

Reine stdlib (csv, json, argparse, datetime, pathlib).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# Severity-Schwellen. Bewusst eher konservativ: alles unter 80 % ist
# "RED" und meldet sich laut, weil das Audit dann auf reduzierter Basis
# laeuft. < 50 % ist "BLACK" weil der V6.8-Layer dann strukturell tot ist.
# --------------------------------------------------------------------------
TH_GREEN = 0.95
TH_YELLOW = 0.80
TH_RED = 0.50

VERSION = "1.0"
DEFAULT_GAP_CLUSTER_MIN = 6  # ein "Gap-Cluster" hat mind. 6 fehlende in Folge


# --------------------------------------------------------------------------
# Datenklassen
# --------------------------------------------------------------------------

@dataclass
class TickerRow:
    ticker: str
    name: str = ""
    tier: str = ""
    asset_class: str = ""
    region: str = ""
    sector: str = ""

    @classmethod
    def from_csv_row(cls, row: Dict[str, str]) -> "TickerRow":
        return cls(
            ticker=row.get("ticker", "").strip(),
            name=row.get("name", "").strip(),
            tier=row.get("tier", "").strip(),
            asset_class=row.get("asset_class", "").strip(),
            region=row.get("region", "").strip(),
            sector=row.get("sector", "").strip(),
        )


@dataclass
class CoverageBreakdown:
    """Aufriss der Coverage nach einer Dimension (tier/region/asset_class)."""
    dimension: str
    rows: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # rows[key] = {"total": N, "scanned": M, "missing": N-M, "pct": M/N}

    def add(self, key: str, scanned: bool) -> None:
        bucket = self.rows.setdefault(key, {"total": 0, "scanned": 0, "missing": 0})
        bucket["total"] += 1
        if scanned:
            bucket["scanned"] += 1
        else:
            bucket["missing"] += 1

    def finalize(self) -> None:
        for k, b in self.rows.items():
            tot = b["total"]
            b["pct"] = round(b["scanned"] / tot, 4) if tot else 0.0


@dataclass
class CoverageReport:
    scan_date: str
    generated_at_utc: str
    version: str = VERSION
    universe_total: int = 0
    scanned_total: int = 0
    coverage_pct: float = 0.0
    severity: str = ""  # GREEN/YELLOW/RED/BLACK
    universe_size_in_scan_json: Optional[int] = None
    valid_results_in_scan_json: Optional[int] = None
    by_tier: Dict[str, Dict[str, float]] = field(default_factory=dict)
    by_region: Dict[str, Dict[str, float]] = field(default_factory=dict)
    by_asset_class: Dict[str, Dict[str, float]] = field(default_factory=dict)
    missing_first_25: List[str] = field(default_factory=list)
    missing_total: int = 0
    gap_clusters: List[Dict[str, object]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------

def load_universe(csv_path: Path) -> List[TickerRow]:
    if not csv_path.exists():
        raise FileNotFoundError(f"universe.csv nicht gefunden: {csv_path}")
    rows: List[TickerRow] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        for raw in rdr:
            tr = TickerRow.from_csv_row(raw)
            if tr.ticker:
                rows.append(tr)
    return rows


def load_scan(json_path: Path) -> Dict:
    if not json_path.exists():
        raise FileNotFoundError(f"scan-JSON nicht gefunden: {json_path}")
    with json_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def derive_scanned_set(scan_payload: Dict) -> Set[str]:
    """Vereinigung aus top_setups[*].ticker und ticker_map-Keys.

    Begruendung: ticker_map enthaelt nach Pipeline-Kontrakt jeden
    Ticker, der das Scoring durchlaufen hat (auch wenn er nicht in
    Top-30 landete). top_setups deckt die Top-30 ab. Beides zusammen
    ist die ehrliche Untergrenze fuer "tatsaechlich gescannt".
    """
    scanned: Set[str] = set()
    for entry in scan_payload.get("top_setups", []) or []:
        t = entry.get("ticker")
        if t:
            scanned.add(t)
    tmap = scan_payload.get("ticker_map") or {}
    if isinstance(tmap, dict):
        scanned.update(tmap.keys())
    return scanned


# --------------------------------------------------------------------------
# Coverage-Logik
# --------------------------------------------------------------------------

def severity_for(coverage_pct: float) -> str:
    if coverage_pct >= TH_GREEN:
        return "GREEN"
    if coverage_pct >= TH_YELLOW:
        return "YELLOW"
    if coverage_pct >= TH_RED:
        return "RED"
    return "BLACK"


def _find_gap_clusters(
    universe_sorted: List[TickerRow],
    missing: Set[str],
    min_size: int = DEFAULT_GAP_CLUSTER_MIN,
) -> List[Dict[str, object]]:
    """Findet zusammenhaengende Lueckenbloecke in alphabetisch
    sortierter Universe.csv. Hilft, Tier-Walk-Cutoffs (Scanner bricht
    nach N Tickern ab) von zufaelligem Skip zu unterscheiden.
    """
    clusters: List[Dict[str, object]] = []
    run_start: Optional[str] = None
    run_items: List[str] = []
    for tr in universe_sorted:
        if tr.ticker in missing:
            if not run_items:
                run_start = tr.ticker
            run_items.append(tr.ticker)
        else:
            if len(run_items) >= min_size:
                clusters.append({
                    "first": run_start,
                    "last": run_items[-1],
                    "size": len(run_items),
                })
            run_items = []
            run_start = None
    if len(run_items) >= min_size:
        clusters.append({
            "first": run_start,
            "last": run_items[-1],
            "size": len(run_items),
        })
    return clusters


def build_report(
    universe: List[TickerRow],
    scan_payload: Dict,
    scan_date: str,
    gap_cluster_min: int = DEFAULT_GAP_CLUSTER_MIN,
    now_utc: Optional[datetime] = None,
) -> CoverageReport:
    now_utc = now_utc or datetime.now(timezone.utc)
    scanned = derive_scanned_set(scan_payload)
    universe_set = {tr.ticker for tr in universe}

    universe_total = len(universe_set)
    scanned_in_universe = len(universe_set & scanned)
    coverage_pct = round(scanned_in_universe / universe_total, 4) if universe_total else 0.0

    by_tier = CoverageBreakdown("tier")
    by_region = CoverageBreakdown("region")
    by_asset_class = CoverageBreakdown("asset_class")
    for tr in universe:
        was_scanned = tr.ticker in scanned
        by_tier.add(tr.tier or "?", was_scanned)
        by_region.add(tr.region or "?", was_scanned)
        by_asset_class.add(tr.asset_class or "?", was_scanned)
    by_tier.finalize()
    by_region.finalize()
    by_asset_class.finalize()

    missing_set = universe_set - scanned
    universe_sorted = sorted(universe, key=lambda x: x.ticker)
    missing_sorted = [tr.ticker for tr in universe_sorted if tr.ticker in missing_set]

    clusters = _find_gap_clusters(universe_sorted, missing_set, min_size=gap_cluster_min)

    notes: List[str] = []
    if "merge_note" in scan_payload:
        notes.append(f"merge_note (scan_payload): {scan_payload['merge_note']}")
    else:
        notes.append("Kein merge_note im scan-JSON (Tier-Merge-Pfad evtl. nicht gelaufen)")
    if scan_payload.get("universe_size") and scan_payload["universe_size"] != scanned_in_universe:
        notes.append(
            f"universe_size im scan-JSON ({scan_payload['universe_size']}) weicht von "
            f"verifizierter Schnittmenge ({scanned_in_universe}) ab"
        )

    return CoverageReport(
        scan_date=scan_date,
        generated_at_utc=now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        version=VERSION,
        universe_total=universe_total,
        scanned_total=scanned_in_universe,
        coverage_pct=coverage_pct,
        severity=severity_for(coverage_pct),
        universe_size_in_scan_json=scan_payload.get("universe_size"),
        valid_results_in_scan_json=scan_payload.get("valid_results"),
        by_tier=by_tier.rows,
        by_region=by_region.rows,
        by_asset_class=by_asset_class.rows,
        missing_first_25=missing_sorted[:25],
        missing_total=len(missing_sorted),
        gap_clusters=clusters,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

def _fmt_table(rows: Dict[str, Dict[str, float]], dim_label: str) -> str:
    lines = [f"| {dim_label} | scanned | total | missing | pct |",
             f"|---|---:|---:|---:|---:|"]
    for key in sorted(rows.keys()):
        b = rows[key]
        pct = b.get("pct", 0.0) * 100
        lines.append(
            f"| {key} | {b['scanned']} | {b['total']} | {b['missing']} | {pct:.1f}% |"
        )
    return "\n".join(lines)


def render_markdown(report: CoverageReport) -> str:
    pct = report.coverage_pct * 100
    sev_emoji = {
        "GREEN": "🟢",
        "YELLOW": "🟡",
        "RED": "🔴",
        "BLACK": "⚫",
    }.get(report.severity, "?")
    md = []
    md.append(f"# Universe Coverage Audit {report.scan_date}")
    md.append("")
    md.append(f"_Version {report.version} · generated {report.generated_at_utc}_")
    md.append("")
    md.append(f"## Status: {sev_emoji} **{report.severity}**: {pct:.1f}% Coverage "
              f"({report.scanned_total}/{report.universe_total})")
    md.append("")
    md.append("## 1. Aufriss nach Tier")
    md.append("")
    md.append(_fmt_table(report.by_tier, "Tier"))
    md.append("")
    md.append("## 2. Aufriss nach Region")
    md.append("")
    md.append(_fmt_table(report.by_region, "Region"))
    md.append("")
    md.append("## 3. Aufriss nach Asset-Class")
    md.append("")
    md.append(_fmt_table(report.by_asset_class, "Asset-Class"))
    md.append("")
    md.append(f"## 4. Erste {len(report.missing_first_25)} fehlende Ticker (von {report.missing_total})")
    md.append("")
    if report.missing_first_25:
        md.append(", ".join(report.missing_first_25))
    else:
        md.append("_(keine Lücken)_")
    md.append("")
    md.append("## 5. Gap-Cluster (>= 6 fehlende in Folge, alphabetisch)")
    md.append("")
    if report.gap_clusters:
        md.append("| # | first | last | size |")
        md.append("|---:|---|---|---:|")
        for i, c in enumerate(report.gap_clusters, 1):
            md.append(f"| {i} | {c['first']} | {c['last']} | {c['size']} |")
    else:
        md.append("_(keine größeren zusammenhängenden Lücken)_")
    md.append("")
    md.append("## 6. Notes")
    md.append("")
    for n in report.notes:
        md.append(f"- {n}")
    md.append("")
    md.append("---")
    md.append("_Tool: tools/universe_coverage_audit.py · read-only · "
              "FEATURE-Lieferung V7.9.12 · KW18-Bugfix-Quote NICHT angefasst._")
    return "\n".join(md)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V7.9.12 Universe-Coverage-Diagnostic (read-only).",
    )
    p.add_argument("--date", default=None,
                   help="Scan-Datum YYYY-MM-DD (Default: heute UTC).")
    p.add_argument("--scan-dir", default="tools",
                   help="Verzeichnis mit scan_<DATE>.json (Default: tools).")
    p.add_argument("--universe", default="tools/universe.csv",
                   help="Pfad zur universe.csv (Default: tools/universe.csv).")
    p.add_argument("--out-dir", default="tools",
                   help="Output-Verzeichnis fuer JSON-Artefakt (Default: tools).")
    p.add_argument("--briefings-dir", default="Briefings",
                   help="Output-Verzeichnis fuer Markdown (Default: Briefings).")
    p.add_argument("--strict", action="store_true",
                   help="Exit 2 bei RED oder BLACK (Cron-Gate).")
    p.add_argument("--threshold", type=float, default=None,
                   help="Optionaler harter Coverage-Cutoff [0..1]; "
                        "darunter exit 2 (zusaetzlich zu --strict).")
    p.add_argument("--gap-cluster-min", type=int, default=DEFAULT_GAP_CLUSTER_MIN,
                   help=f"Minimum-Groesse fuer Gap-Cluster (Default {DEFAULT_GAP_CLUSTER_MIN}).")
    p.add_argument("--no-write", action="store_true",
                   help="Nur stdout-Summary, keine Dateien schreiben.")
    return p.parse_args(argv)


def _resolve_date(date_str: Optional[str]) -> str:
    if date_str:
        # validate format
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    scan_date = _resolve_date(args.date)

    scan_path = Path(args.scan_dir) / f"scan_{scan_date}.json"
    universe_path = Path(args.universe)

    universe = load_universe(universe_path)
    scan_payload = load_scan(scan_path)
    report = build_report(
        universe=universe,
        scan_payload=scan_payload,
        scan_date=scan_date,
        gap_cluster_min=args.gap_cluster_min,
    )

    # stdout-Kurzsummary (immer)
    pct = report.coverage_pct * 100
    sys.stdout.write(
        f"[universe_coverage_audit] {scan_date} severity={report.severity} "
        f"coverage={pct:.1f}% scanned={report.scanned_total}/{report.universe_total} "
        f"missing={report.missing_total} clusters={len(report.gap_clusters)}\n"
    )

    if not args.no_write:
        out_json = Path(args.out_dir) / f"universe_coverage_{scan_date}.json"
        out_md = Path(args.briefings_dir) / f"Universe-Coverage-Audit-{scan_date}.md"
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        with out_json.open("w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        with out_md.open("w", encoding="utf-8") as f:
            f.write(render_markdown(report))
        sys.stdout.write(f"  -> {out_json}\n  -> {out_md}\n")

    # Exit-Code
    rc = 0
    if args.strict and report.severity in ("RED", "BLACK"):
        rc = 2
    if args.threshold is not None and report.coverage_pct < float(args.threshold):
        rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
