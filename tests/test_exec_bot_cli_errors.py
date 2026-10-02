"""CLI error handling of the paper bot and the broker probe (offline).

No test here talks to a broker: the broker factory and the account call are replaced
by fakes, so no request is ever built or sent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import broker_alpaca  # noqa: E402
import exec_bot  # noqa: E402


@pytest.fixture()
def base(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "signals").mkdir()
    return str(tmp_path)


def test_missing_keys_stop_before_any_request(base, monkeypatch, capsys):
    monkeypatch.setattr(exec_bot, "resolve_credentials", lambda: {"key": "", "secret": ""})

    def _no_request(*_a, **_k):  # would mean an unauthenticated request was attempted
        raise AssertionError("AlpacaBroker must not be built without keys")

    monkeypatch.setattr(exec_bot, "AlpacaBroker", _no_request)
    rc = exec_bot.main(["--reconcile", "--base", base])
    assert rc == exec_bot.RC_KEYS_MISSING == 6
    assert "KEYS_MISSING" in capsys.readouterr().out


def test_broker_error_is_one_line_not_a_traceback(base, monkeypatch, capsys):
    def _failing_broker(cfg, transport=None):
        raise broker_alpaca.BrokerError("transport_failure method=GET err=URLError: offline")

    monkeypatch.setattr(exec_bot, "make_broker", _failing_broker)
    rc = exec_bot.main(["--reconcile", "--base", base])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("BROKER_ERROR:")


def test_kill_still_sets_kill_file_without_keys(base, monkeypatch):
    monkeypatch.setattr(exec_bot, "resolve_credentials", lambda: {"key": "", "secret": ""})
    monkeypatch.setattr(exec_bot, "notify", lambda *a, **k: None)
    try:
        rc = exec_bot.main(["--kill", "--base", base])
    finally:
        if exec_bot._LOCK_FH is not None:  # release the process-wide flock handle
            exec_bot._LOCK_FH.close()
            exec_bot._LOCK_FH = None
    assert rc == 1  # problem reported (no broker), but the bot is blocked anyway
    assert os.path.exists(os.path.join(base, "tools", exec_bot.KILL_FILE))


def test_probe_reports_broker_error_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(broker_alpaca, "resolve_credentials",
                        lambda: {"key": "synthetic-key-id", "secret": "synthetic-secret"})

    def _fail(self):
        raise broker_alpaca.BrokerError("api_error status=401 unauthorized")

    monkeypatch.setattr(broker_alpaca.AlpacaBroker, "get_account", _fail)
    assert broker_alpaca._probe() == 1
    assert capsys.readouterr().out.startswith("PROBE FAILED:")


def test_probe_without_keys_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(broker_alpaca, "resolve_credentials", lambda: {"key": "", "secret": ""})
    assert broker_alpaca._probe() == 2
