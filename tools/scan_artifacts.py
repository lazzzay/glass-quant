#!/usr/bin/env python3
"""
SCAN-IMMUTABILITY-1: Write-once Scan-Artefakte + Hash-Manifest (2026-04-19).

Problem: daily_scanner.py und premarket_scanner.py schreiben nach
`scan_YYYY-MM-DD.{csv,json}` bzw. `premarket_YYYY-MM-DD.{csv,json}`. Ein zweiter
Run am selben Tag (z. B. Nachmittags-Rescan) bügelt das Morgen-Artefakt platt:
damit sind Empfehlungen, Tracking und Briefings nicht mehr reproduzierbar.

Loesung: Write-once + SHA256-Manifest. Beim ersten Schreiben entsteht
`<artifact>.manifest.json` mit Hash, Bytes, First-Write-UTC. Ein zweiter
Schreibversuch wird abgelehnt (Exit-Code 3), sofern nicht:
  --force   : Artefakt + Manifest explizit ueberschreiben
  --rescan  : In separaten `__RESCAN_HHMMSS`-Namespace schreiben

Downstream kann `verify_manifest(path)` aufrufen, um sicherzustellen, dass
das Artefakt seit dem ersten Schreiben nicht manipuliert wurde.

Nutzung im Scanner:

    from scan_artifacts import write_immutable, ImmutableArtifactError

    def _write_csv(p):
        with open(p, "w", encoding="utf-8", newline="") as f:
            ...

    actual_path = write_immutable(
        out_csv,
        _write_csv,
        writer="daily_scanner.py",
        writer_version="V6.8",
        force=args.force,
        rescan=args.rescan,
        metadata={"scan_date": scan_date},
    )
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


class ImmutableArtifactError(RuntimeError):
    """Raised when a write-once artefact already exists and --force/--rescan is not set."""


MANIFEST_SUFFIX = ".manifest.json"


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + MANIFEST_SUFFIX)


def _write_manifest(artifact: Path, *, writer: str, writer_version: str,
                    metadata: Optional[dict] = None) -> dict:
    data = {
        "artifact": artifact.name,
        "sha256": _sha256_of_file(artifact),
        "bytes": artifact.stat().st_size,
        "first_write_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "writer": writer,
        "writer_version": writer_version,
        "metadata": metadata or {},
    }
    mp = _manifest_path(artifact)
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def _rescan_path(artifact: Path) -> Path:
    """`scan_2026-04-19.csv` -> `scan_2026-04-19__RESCAN_HHMMSS.csv`."""
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return artifact.with_name(f"{artifact.stem}__RESCAN_{stamp}{artifact.suffix}")


def write_immutable(
    path: Path,
    writer_fn: Callable[[Path], None],
    *,
    writer: str,
    writer_version: str,
    force: bool = False,
    rescan: bool = False,
    metadata: Optional[dict] = None,
) -> Path:
    """
    Schreibt ein Write-once-Artefakt. Beim ersten Mal entsteht Artefakt +
    Manifest. Bei Folge-Runs:
      - rescan=True: separater Namespace-Suffix, kein Manifest-Touch.
      - force=True: Overwrite + Manifest-Neuschreibung (Notfall).
      - sonst: ImmutableArtifactError.

    Rueckgabe: der tatsaechlich geschriebene Pfad (original oder Rescan-Namespace).
    """
    path = Path(path)
    manifest = _manifest_path(path)

    # Erste Schreibung: kein Artefakt, kein Manifest
    if not path.exists() and not manifest.exists():
        writer_fn(path)
        _write_manifest(path, writer=writer, writer_version=writer_version, metadata=metadata)
        return path

    # Rescan: in separaten Namespace schreiben, Morgen-Artefakt unangetastet
    if rescan:
        rp = _rescan_path(path)
        while rp.exists():  # extrem unwahrscheinlich, aber defensiv
            rp = _rescan_path(path)
        writer_fn(rp)
        # Kein Manifest fuer Rescan-Files (sie sind explizit experimentell)
        return rp

    # Force: Overwrite + Manifest-Erneuerung
    if force:
        writer_fn(path)
        _write_manifest(path, writer=writer, writer_version=writer_version, metadata=metadata)
        return path

    # Default: refuse
    raise ImmutableArtifactError(
        f"Artefakt bereits vorhanden: {path.name}\n"
        f"  Manifest: {'ja' if manifest.exists() else 'NEIN (Legacy)'}\n"
        f"  Morgen-Artefakt bleibt write-once geschuetzt.\n"
        f"  Optionen:\n"
        f"    --rescan  -> neuer Namespace {path.stem}__RESCAN_HHMMSS{path.suffix}\n"
        f"    --force   -> Overwrite + Manifest-Neuschreibung (vorsichtig)"
    )


def verify_manifest(path: Path) -> dict:
    """
    Prueft Hash eines Artefakts gegen sein Manifest.

    Return-Dict:
      {"ok": bool, "reason": str, "manifest": dict or None, "current_sha256": str or None}

    - ok=True: Manifest existiert und Hash stimmt
    - ok=False mit reason="no-manifest": Legacy-File ohne Manifest
    - ok=False mit reason="hash-mismatch": Artefakt wurde seit Erstschreibung veraendert
    - ok=False mit reason="no-artifact": Datei fehlt komplett
    """
    path = Path(path)
    manifest = _manifest_path(path)
    if not path.exists():
        return {"ok": False, "reason": "no-artifact", "manifest": None, "current_sha256": None}
    if not manifest.exists():
        return {"ok": False, "reason": "no-manifest", "manifest": None,
                "current_sha256": _sha256_of_file(path)}
    try:
        with open(manifest, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "reason": f"manifest-read-error:{e}",
                "manifest": None, "current_sha256": _sha256_of_file(path)}
    current = _sha256_of_file(path)
    if current != m.get("sha256"):
        return {"ok": False, "reason": "hash-mismatch", "manifest": m,
                "current_sha256": current}
    return {"ok": True, "reason": "verified", "manifest": m, "current_sha256": current}


def cli_verify(paths: list[Path]) -> int:
    """Kommandozeilen-Verifier: fuer jeden Pfad JSON-Report + aggregierter Exit-Code."""
    results = []
    any_fail = False
    for p in paths:
        r = verify_manifest(Path(p))
        r["path"] = str(p)
        results.append(r)
        if not r["ok"] and r["reason"] != "no-manifest":
            any_fail = True
    print(json.dumps(results, indent=2, default=str))
    return 2 if any_fail else 0


if __name__ == "__main__":
    # Verify-Modus: `python3 scan_artifacts.py scan_2026-04-19.csv [...]`
    if len(sys.argv) < 2:
        print("usage: scan_artifacts.py <artifact> [<artifact>...]", file=sys.stderr)
        sys.exit(1)
    sys.exit(cli_verify([Path(a) for a in sys.argv[1:]]))
