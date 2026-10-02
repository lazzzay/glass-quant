"""Sibling imports must work in script mode.

The tools run as scripts (`python3 tools/<name>.py`, also from launchd). Then tools/
is sys.path[0] and the repository root is NOT on sys.path, so `from tools.x import y`
fails there. pytest always puts the repository root on sys.path, which hides that bug
inside the test process. These tests therefore run the imports in a subprocess whose
working directory is tools/ (like sys.path[0] in script mode) and whose PYTHONPATH
does not contain the repository root. Offline: nothing is fetched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"


def _script_mode_env() -> dict:
    env = dict(os.environ)
    keep = []
    for part in env.get("PYTHONPATH", "").split(os.pathsep):
        if part and Path(part).resolve() != REPO:
            keep.append(part)
    env["PYTHONPATH"] = os.pathsep.join(keep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_in_tools_dir(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(TOOLS),
                          env=_script_mode_env(), capture_output=True, text=True,
                          timeout=180)


def test_scanner_stooq_fallback_import_in_script_mode():
    pytest.importorskip("yfinance")
    r = _run_in_tools_dir(
        "import daily_scanner as ds\n"
        "f = ds._import_stooq_fetcher()\n"
        "print('STOOQ_OK' if f is not None else 'STOOQ_NONE')\n")
    assert r.returncode == 0, r.stderr[-2000:]
    assert "STOOQ_OK" in r.stdout, r.stdout + r.stderr[-2000:]


def test_news_sentiment_scorer_imports_in_script_mode():
    r = _run_in_tools_dir(
        "import news_sentiment_fetch as n\n"
        "ok = all(n._import_scorer(m) is not None for m in ('findpo_scorer', 'finbert_scorer'))\n"
        "print('SCORERS_OK' if ok else 'SCORERS_MISSING')\n")
    assert r.returncode == 0, r.stderr[-2000:]
    assert "SCORERS_OK" in r.stdout, r.stdout + r.stderr[-2000:]
