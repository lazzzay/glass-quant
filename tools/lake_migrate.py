"""lake_migrate.py: Welle I DATA-ARCH-1 Migration-Helper.

Wrappt `data_lake/manifest.json` (v1.0, 212 Serien) in das neue
`lake/_catalog/manifest_v2.json` (v2.0). Keine Datei-Moves. Legacy-Pfade
bleiben unter data_lake/ abrufbar und werden als `location_legacy` referenziert.

Requires lake/_catalog/schema/contract_v2.py (v2 catalog schema), which is
not included in this repository yet.

Usage:
    python3 tools/lake_migrate.py --dry-run
    python3 tools/lake_migrate.py --apply
    python3 tools/lake_migrate.py --inspect   # zeigt aktuelles manifest_v2 zusammengefasst

Exit-Codes:
    0  OK (oder dry-run ohne Fehler)
    1  Legacy-Manifest fehlt oder unlesbar
    2  Schema-Validierungsfehler
    3  Write-Error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Lokaler Import des V2-Schemas
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "lake" / "_catalog" / "schema"
sys.path.insert(0, str(SCHEMA_DIR))

try:
    from contract_v2 import CatalogManifestV2, DatasetEntry, Frequency, Zone  # type: ignore  # noqa: E402
except ImportError:  # pragma: no cover
    # NOTE: lake/_catalog/schema/contract_v2.py (pydantic schema of the v2 catalog) is
    # NOT part of this repository yet. Without it this migration helper cannot run.
    if __name__ == "__main__":
        print("lake_migrate: lake/_catalog/schema/contract_v2.py fehlt (nicht im Repo). "
              "Das v2-Catalog-Schema muss zuerst angelegt werden.", file=sys.stderr)
        sys.exit(2)
    raise


LEGACY_MANIFEST = REPO_ROOT / "data_lake" / "manifest.json"
V2_MANIFEST = REPO_ROOT / "lake" / "_catalog" / "manifest_v2.json"


def _freq_from_v1(raw: str | None) -> Frequency:
    """Mapt v1-frequency-Label auf v2-Enum. Unbekanntes wird SNAPSHOT."""
    if not raw:
        return Frequency.SNAPSHOT
    norm = raw.strip().lower()
    mapping = {
        "daily": Frequency.DAILY,
        "intraday_h1": Frequency.INTRADAY_H1,
        "intraday": Frequency.INTRADAY_H1,
        "event": Frequency.EVENT,
        "snapshot": Frequency.SNAPSHOT,
        "monthly": Frequency.MONTHLY,
        "yearly": Frequency.YEARLY,
        "annual": Frequency.YEARLY,
    }
    return mapping.get(norm, Frequency.SNAPSHOT)


def _domain_from_path(path: str) -> str:
    """Leitet ein fachliches Domain-Label aus dem relativen Pfad ab."""
    if not path:
        return "unknown"
    top = path.split("/", 1)[0]
    aliases = {
        "prices": "prices",
        "macro_fred": "macro",
        "macro_other": "macro",
        "fundamentals_yf": "fundamentals",
        "insider_sec_edgar": "insider",
        "congress_capitol_trades": "congress",
        "breadth": "breadth",
        "sentiment": "sentiment",
        "earnings": "earnings",
        "index_membership": "universe",
        "universe": "universe",
        "logs": "ops_log",
    }
    return aliases.get(top, top)


def _id_from_v1(series: dict) -> str:
    """Erzeugt eindeutigen v2-Dataset-Key."""
    base = series.get("id") or series.get("source_id") or "UNKNOWN"
    domain = _domain_from_path(series.get("path", "") or "")
    return f"{domain}::{base}"


def migrate(dry_run: bool = True, verbose: bool = False) -> tuple[int, CatalogManifestV2 | None, list[str]]:
    """Fuehrt die v1→v2-Migration durch.

    Returns:
        (exit_code, manifest_or_None, warnings)
    """
    warnings: list[str] = []

    if not LEGACY_MANIFEST.exists():
        return 1, None, [f"Legacy-Manifest fehlt: {LEGACY_MANIFEST}"]

    try:
        legacy = json.loads(LEGACY_MANIFEST.read_text())
    except Exception as exc:  # noqa: BLE001
        return 1, None, [f"Legacy-Manifest unlesbar: {exc}"]

    series_list = legacy.get("series") or []
    if not isinstance(series_list, list):
        return 1, None, ["Legacy-Manifest hat kein 'series'-Array."]

    datasets: list[DatasetEntry] = []
    seen_ids: set[str] = set()
    for raw in series_list:
        try:
            entry_id = _id_from_v1(raw)
            # Dedup-Protection (falls zwei v1-Serien den gleichen (domain, id) haben)
            if entry_id in seen_ids:
                entry_id = f"{entry_id}_{raw.get('source', 'x')}_{len(datasets)}"
            seen_ids.add(entry_id)

            dataset = DatasetEntry(
                id=entry_id,
                zone=Zone.BRONZE,
                domain=_domain_from_path(raw.get("path", "") or ""),
                source=raw.get("source", "unknown") or "unknown",
                source_id=raw.get("source_id"),
                description=raw.get("description", "") or "",
                unit=raw.get("unit"),
                location=None,  # Welle II befuellt
                location_legacy=raw.get("path") or None,
                frequency=_freq_from_v1(raw.get("frequency")),
                partitioning=None,
                rows=raw.get("rows"),
                first_date=raw.get("first_date"),
                last_date=raw.get("last_date"),
                contract_ref=None,
                schema_hash=raw.get("sha256_16"),
                retention_days=None,
                tags=["legacy_v1"],
                download_utc=raw.get("download_utc"),
                migrated_from_v1=True,
            )
            datasets.append(dataset)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Serie {raw.get('id', '?')} uebersprungen: {exc}")

    now = datetime.now(timezone.utc)
    manifest = CatalogManifestV2(
        created=now,
        last_updated=now,
        datasets=datasets,
    )
    manifest.recount()

    if verbose:
        print(f"[lake_migrate] Legacy-Serien eingelesen: {len(series_list)}")
        print(f"[lake_migrate] v2-Datasets erstellt:   {len(datasets)}")
        if warnings:
            print(f"[lake_migrate] {len(warnings)} Warnungen.")
        print(f"[lake_migrate] Zonen-Summen:            {manifest.counts_by_zone}")
        print(f"[lake_migrate] Domain-Summen (top 10):")
        for k, v in sorted(manifest.counts_by_domain.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {k:18s} {v:4d}")

    if dry_run:
        return 0, manifest, warnings

    try:
        V2_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        manifest.save(V2_MANIFEST)
    except Exception as exc:  # noqa: BLE001
        return 3, manifest, warnings + [f"Write-Error: {exc}"]

    # Log
    log_dir = REPO_ROOT / "lake" / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"migrate_{now.strftime('%Y%m%d_%H%M%S')}.log"
    log_file.write_text(
        json.dumps(
            {
                "event": "DATA-ARCH-1 migrate",
                "dry_run": False,
                "legacy_series": len(series_list),
                "v2_datasets": len(datasets),
                "warnings": warnings,
                "legacy_manifest_sha256_16": hashlib.sha256(LEGACY_MANIFEST.read_bytes()).hexdigest()[:16],
                "v2_manifest_sha256_16": hashlib.sha256(V2_MANIFEST.read_bytes()).hexdigest()[:16],
                "utc": now.isoformat(),
            },
            indent=2,
        )
    )
    return 0, manifest, warnings


def inspect() -> int:
    if not V2_MANIFEST.exists():
        print(f"[inspect] {V2_MANIFEST} existiert nicht. Erst --apply laufen lassen.")
        return 1
    manifest = CatalogManifestV2.load(V2_MANIFEST)
    manifest.recount()
    print(f"version          : {manifest.version}")
    print(f"created          : {manifest.created}")
    print(f"last_updated     : {manifest.last_updated}")
    print(f"datasets         : {len(manifest.datasets)}")
    print(f"counts_by_zone   : {manifest.counts_by_zone}")
    print(f"counts_by_domain : {dict(sorted(manifest.counts_by_domain.items(), key=lambda kv: -kv[1])[:15])}")
    print(f"legacy_root      : {manifest.legacy_root}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DATA-ARCH-1 Welle I Migration-Helper.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Zeigt Migration-Plan ohne zu schreiben.")
    mode.add_argument("--apply", action="store_true", help="Schreibt lake/_catalog/manifest_v2.json.")
    mode.add_argument("--inspect", action="store_true", help="Zeigt aktuelles manifest_v2-Summary.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.inspect:
        return inspect()

    code, manifest, warnings = migrate(dry_run=args.dry_run, verbose=args.verbose or args.apply)
    if args.apply and code == 0:
        print(f"[lake_migrate] apply OK → {V2_MANIFEST.relative_to(REPO_ROOT)}")
    for w in warnings:
        print(f"[WARN] {w}")
    return code


if __name__ == "__main__":
    sys.exit(main())
