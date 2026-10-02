#!/usr/bin/env bash
# Test runner for daily-invest-oss.
# Solves the two common stumbling blocks in one step:
#   1) wrong working directory  -> always cd into the repo root, wherever it is called from
#   2) missing test dependencies -> prints an install hint instead of installing anything
#
# Usage (from anywhere):
#   bash ./run_tests.sh
# or inside the repo:  ./run_tests.sh
# Arguments are passed through to pytest, for example:  ./run_tests.sh -k journal -v
#
# The suite is offline: no network access, no broker calls, no orders.
set -euo pipefail

cd "$(dirname "$0")"   # always work from the repo root

missing=0
for pkg in pytest numpy pandas; do
  if ! python3 -c "import $pkg" >/dev/null 2>&1; then
    echo "[setup] python package '$pkg' is missing."
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "[setup] Install the dependencies first, for example:"
  echo "          python3 -m pip install -r requirements-dev.txt"
  exit 1
fi

echo "[run] python3 -m pytest tests/ ..."
# --continue-on-collection-errors: one test module with a missing optional dependency
# does not stop the whole suite.
python3 -m pytest tests/ -q --continue-on-collection-errors "$@"

# Quick smoke test of the journal fixes WITHOUT pytest (has its own __main__ runner):
#   python3 tests/test_quick_trade_journal.py
