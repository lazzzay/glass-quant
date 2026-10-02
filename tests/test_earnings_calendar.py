"""EARNINGS-PRODUCER-OUTAGE-1 Output-Contract (Audit-Folge, 2026-06-14).

Pinnt die Status-Ableitung des Producers und den Contract „immer ein valides File":
- `derive_status` unterscheidet ok / degraded korrekt,
- main() schreibt unter allen Pfaden ein earnings_<date>.json (Platzhalter + final +
  error-Pfad) und exitet bei echtem Fehler non-zero.
daily_scanner-Stil: skippt ohne yfinance (Sandbox), läuft voll auf dem Mac.
"""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# earnings_calendar importiert yfinance am Modulkopf → ohne yfinance sauber skippen.
pytest.importorskip("yfinance")

from earnings_calendar import derive_status  # noqa: E402

EARN_SRC = (ROOT / "tools" / "earnings_calendar.py").read_text(encoding="utf-8")


def test_derive_status():
    assert derive_status(0, 0) == "ok"        # leeres Equity-Universe -> kein Problem
    assert derive_status(462, 300) == "ok"    # Dates gefunden
    assert derive_status(462, 1) == "ok"      # selbst 1 reicht
    assert derive_status(462, 0) == "degraded"  # Universe>0, 0 Dates -> Quelle weg
    assert derive_status(10, 0) == "degraded"


def test_output_contract_present_in_source():
    # Frueh-Platzhalter (File ab t~0), finaler Write, error-Pfad mit Non-Zero-Exit.
    assert '_base("pending"' in EARN_SRC          # Platzhalter vor dem langsamen Loop
    assert '_base("error"' in EARN_SRC            # error-File bei Exception
    assert "sys.exit(2)" in EARN_SRC              # echter Fehler -> Pipeline loggt FAIL
    assert "derive_status(len(universe), with_known)" in EARN_SRC
