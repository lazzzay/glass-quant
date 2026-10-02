"""Import-level tests for mcp/daily_invest_mcp.py.

tests/test_mcp_tools.py only greps the source. These tests import the real server
module when a compatible mcp package is installed (mcp 1.x on Python 3.10+) and are
skipped otherwise. The --help test runs everywhere. Everything stays offline: no
server is started and no tool that fetches prices is called.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MCP_PATH = REPO / "mcp" / "daily_invest_mcp.py"


def test_help_exits_without_starting_the_server():
    """--help must print usage and exit, also without mcp installed (no stdio hang)."""
    r = subprocess.run([sys.executable, str(MCP_PATH), "--help"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "--selftest" in r.stdout


@pytest.fixture(scope="module")
def server():
    pytest.importorskip("mcp.server.fastmcp")
    spec = importlib.util.spec_from_file_location("daily_invest_mcp_under_test", MCP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_and_resource_registered(server):
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"get_scan_candidates", "get_recommendations", "get_price",
            "get_journal_status", "run_quick_trade"} <= names
    assert len(names) == 13
    uris = [str(r.uri) for r in asyncio.run(server.mcp.list_resources())]
    assert "daily-invest://claude-md" in uris


def test_tool_schema_survives_not_advice_wrapper(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["get_history"].inputSchema.get("properties", {})
    assert {"ticker", "days"} <= set(props)
    for t in tools.values():
        assert "not investment advice" in (t.description or "").lower(), t.name


def _write_scan(tools_dir: Path, date: str) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / f"scan_{date}.json").write_text(json.dumps({
        "scan_date": date, "universe_size": 3, "valid_results": 3,
        "top_setups": [{"ticker": "AAA", "pre_score": 8.1, "passes_threshold": True}],
        "hebel_watch": [],
    }), encoding="utf-8")


def test_scan_candidates_payload_and_deprecated_alias(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TOOLS_DIR", tmp_path / "tools")
    _write_scan(tmp_path / "tools", "2099-01-05")
    out = server.get_scan_candidates(date="2099-01-05")
    assert [c["ticker"] for c in out["top_candidates"]] == ["AAA"]
    assert "top_recommendations" not in out
    assert "not investment advice" in out["disclaimer"].lower()

    old = server.get_recommendations(date="2099-01-05")
    assert old["top_recommendations"] == old["top_candidates"]
    assert "deprecated" in old
    assert "not investment advice" in old["disclaimer"].lower()


def test_error_payload_also_carries_disclaimer(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TOOLS_DIR", tmp_path / "empty")
    out = server.get_scan_candidates(date="2099-01-06")
    assert "error" in out and "disclaimer" in out


def test_claude_md_header_parser_accepts_english_label(server):
    de = server._parse_claude_md_state("Letztes Update: 2099-01-05 - V9.1 test\n")
    en = server._parse_claude_md_state("Last update: 2099-01-05 - V9.1 test\n")
    assert de["version"] == en["version"] == "V9.1"
    assert de["last_update"] == en["last_update"] == "2099-01-05"
