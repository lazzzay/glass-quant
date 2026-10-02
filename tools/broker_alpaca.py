#!/usr/bin/env python3
"""broker_alpaca.py: Alpaca REST adapter (PAPER-first) for EXEC-BOT-1 Phase 0.

HARD RULES (Anti-Pattern #1: no LLM in the order path, see CLAUDE.md):
  * This module is DETERMINISTIC transport code. No LLM anywhere in the order path.
  * PAPER-ONLY LOCK: refuses any non-paper base_url unless BOTH the config flag
    `allow_live` is true AND env EXEC_BOT_ALLOW_LIVE=1 are set (double lock).
    Phase 0 ships with allow_live=false: the lock cannot be passed by accident.
  * API keys are read from env (ALPACA_KEY_ID / ALPACA_SECRET_KEY) or tools/.env.
    Treat them like a password: anyone who has them can trade the account. Use paper
    keys, keep tools/.env private (chmod 600) and regenerate the keys after any leak.

Design:
  * Injectable `transport` callable for fully OFFLINE selftests (no network, no keys).
  * Idempotency via client_order_id "execbot-<date>-<TICKER>": Alpaca rejects
    duplicate client_order_ids; we additionally check the local orders namespace.
  * Bracket orders (order_class=bracket) are SERVER-SIDE: stop & target live at the
    broker and survive any local crash/sleep: the core safety upgrade vs. status quo.

CLI: --selftest (offline, no network) | --probe (auth check against paper endpoint)
Probe exit codes: 0 ok · 2 keys missing · 1 broker/API/network error (one line, no traceback)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ENV_FILE_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
]
USER_AGENT = "exec-bot-phase0/1.0 (deterministic; no-llm-in-order-path)"
TIMEOUT_S = 20


_LIVE_WARNING_SHOWN = False


def warn_live_mode(base_url: str = "", stream=None) -> None:
    """Print a clear, NON-blocking console warning when a live (non-paper)
    endpoint is configured or active. Shown once per process. It never stops
    execution; the double live lock in AlpacaBroker stays the actual guard."""
    global _LIVE_WARNING_SHOWN
    if _LIVE_WARNING_SHOWN:
        return
    _LIVE_WARNING_SHOWN = True
    out = stream or sys.stderr
    line = "!" * 72
    print("\n".join([
        line,
        "WARNING: LIVE TRADING ENDPOINT CONFIGURED: " + (base_url or "(non-paper)"),
        "Orders sent to a live account use REAL MONEY. You can lose real money.",
        "This software is NOT financial advice and comes WITHOUT ANY WARRANTY.",
        "Safety rails stay active: exposure caps, kill switch (tools/EXEC_BOT_KILL),",
        "and the double live lock (config allow_live=true AND EXEC_BOT_ALLOW_LIVE=1).",
        "For paper trading use base_url " + PAPER_BASE_URL,
        line,
    ]), file=out, flush=True)


class BrokerError(RuntimeError):
    """Raised for transport/API errors. Message is safe to log (no secrets)."""


class MissingCredentialsError(BrokerError):
    """ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set (tools/.env or environment)."""


class LiveLockError(BrokerError):
    """Raised when a non-paper endpoint is requested without the double unlock."""


def load_env_file() -> Dict[str, str]:
    """Parse simple KEY=VALUE lines from tools/.env (gitignored). Env wins over file."""
    out: Dict[str, str] = {}
    for path in ENV_FILE_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        out[k.strip()] = v.strip().strip('"').strip("'")
            except OSError:
                pass
    return out


def resolve_credentials() -> Dict[str, str]:
    file_env = load_env_file()
    key = os.environ.get("ALPACA_KEY_ID") or file_env.get("ALPACA_KEY_ID") or ""
    sec = os.environ.get("ALPACA_SECRET_KEY") or file_env.get("ALPACA_SECRET_KEY") or ""
    return {"key": key, "secret": sec}


def _default_transport(method: str, url: str, headers: Dict[str, str],
                       body: Optional[dict]) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return {"status": resp.status, "json": json.loads(raw) if raw else None}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"message": raw[:300]}
        return {"status": e.code, "json": payload}
    except Exception as e:  # DNS, timeout, TLS: surface as BrokerError upstream
        raise BrokerError(f"transport_failure method={method} err={type(e).__name__}: {e}")


class AlpacaBroker:
    """Thin, deterministic Alpaca v2 REST client. Paper-locked by default."""

    def __init__(self, base_url: str = PAPER_BASE_URL,
                 key_id: str = "", secret_key: str = "",
                 allow_live_cfg: bool = False,
                 transport: Optional[Callable] = None) -> None:
        base_url = (base_url or "").rstrip("/")
        is_paper = base_url == PAPER_BASE_URL
        env_unlock = os.environ.get("EXEC_BOT_ALLOW_LIVE") == "1"
        if not is_paper and not (allow_live_cfg and env_unlock):
            raise LiveLockError(
                "LIVE endpoint refused: Phase 0 is paper-only. "
                "Unlock requires config allow_live=true AND env EXEC_BOT_ALLOW_LIVE=1.")
        if not is_paper:
            warn_live_mode(base_url)  # non-blocking console warning
        self.base_url = base_url
        self.is_paper = is_paper
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._transport = transport or _default_transport

    # ── low level ────────────────────────────────────────────────────────────
    def _call(self, method: str, path: str, body: Optional[dict] = None,
              ok: tuple = (200,)) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._transport(method, url, dict(self._headers), body)
        status = resp.get("status")
        if status not in ok:
            msg = ""
            if isinstance(resp.get("json"), dict):
                msg = str(resp["json"].get("message", ""))[:200]
            raise BrokerError(f"api_error {method} {path} status={status} msg={msg}")
        return resp.get("json")

    # ── account / market ────────────────────────────────────────────────────
    def get_account(self) -> dict:
        return self._call("GET", "/v2/account")

    def get_clock(self) -> dict:
        return self._call("GET", "/v2/clock")

    def get_positions(self) -> List[dict]:
        return self._call("GET", "/v2/positions") or []

    def get_open_orders(self) -> List[dict]:
        return self._call("GET", "/v2/orders?status=open&limit=100") or []

    def list_orders_today(self) -> List[dict]:
        # SCHWARM-FIX: limit 200→500 (API-Max), 200er-Fenster hätte bei
        # wachsendem Ledger alte Fills aus dem Sync fallen lassen.
        return self._call("GET", "/v2/orders?status=all&limit=500") or []

    # ── orders ───────────────────────────────────────────────────────────────
    def submit_bracket_buy(self, symbol: str, qty: int, take_profit: float,
                           stop_loss: float, client_order_id: str,
                           limit_price: Optional[float] = None,
                           tif: str = "gtc") -> dict:
        """Long-only bracket. qty must be a positive int (Alpaca: no fractional brackets).

        TIF-LEKTION 09.07.2026 (Paper-Fund): mit tif="day" verfallen die
        Schutz-Legs am Handelsschluss, während die Position weiterlebt →
        Multi-Tage-Swings standen über Nacht OHNE Stop. Default daher GTC."""
        if qty <= 0:
            raise BrokerError(f"qty_must_be_positive symbol={symbol} qty={qty}")
        if not (stop_loss < (limit_price or take_profit) and stop_loss < take_profit):
            raise BrokerError(
                f"bracket_prices_invalid symbol={symbol} sl={stop_loss} tp={take_profit}")
        body = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": "buy",
            "type": "limit" if limit_price else "market",
            "time_in_force": tif,
            "order_class": "bracket",
            "client_order_id": client_order_id[:48],
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
        }
        if limit_price:
            body["limit_price"] = f"{limit_price:.2f}"
        return self._call("POST", "/v2/orders", body=body, ok=(200, 201))

    def submit_market_buy(self, symbol: str, qty: int, client_order_id: str,
                          tif: str = "day") -> dict:
        """Plain long entry (momentum sleeve): protection is attached post-fill
        via submit_trailing_stop_sell (see exec_bot.track)."""
        if qty <= 0:
            raise BrokerError(f"qty_must_be_positive symbol={symbol} qty={qty}")
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "buy",
                "type": "market", "time_in_force": tif,
                "client_order_id": client_order_id[:48]}
        return self._call("POST", "/v2/orders", body=body, ok=(200, 201))

    def submit_trailing_stop_sell(self, symbol: str, qty: int, trail_percent: float,
                                  client_order_id: str) -> dict:
        """Server-side trailing stop (GTC): the broker ratchets the stop as price
        rises: survives any local downtime. Core momentum exit."""
        if qty <= 0 or not (0.5 <= trail_percent <= 25):
            raise BrokerError(
                f"trail_invalid symbol={symbol} qty={qty} trail={trail_percent}")
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "sell",
                "type": "trailing_stop", "trail_percent": f"{trail_percent:.1f}",
                "time_in_force": "gtc", "client_order_id": client_order_id[:48]}
        return self._call("POST", "/v2/orders", body=body, ok=(200, 201))

    def submit_oco_sell(self, symbol: str, qty: int, take_profit: float,
                        stop_loss: float, client_order_id: str) -> dict:
        """Schutz-Nachrüstung für eine BESTEHENDE Position: OCO (Target+Stop,
        GTC). Genau eine der beiden Seiten führt aus, die andere storniert."""
        if qty <= 0 or not (stop_loss < take_profit):
            raise BrokerError(f"oco_invalid symbol={symbol} sl={stop_loss} tp={take_profit}")
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "sell",
                "type": "limit", "time_in_force": "gtc", "order_class": "oco",
                "client_order_id": client_order_id[:48],
                "take_profit": {"limit_price": f"{take_profit:.2f}"},
                "stop_loss": {"stop_price": f"{stop_loss:.2f}"}}
        return self._call("POST", "/v2/orders", body=body, ok=(200, 201))

    def close_position(self, symbol: str) -> dict:
        """Market-close a position (used by T+3 discipline exit). Cancels attached legs first."""
        for o in self.get_open_orders():
            if o.get("symbol") == symbol:
                try:
                    self._call("DELETE", f"/v2/orders/{o.get('id')}", ok=(200, 204, 404))
                except BrokerError:
                    pass  # leg may already be cancelled/filled; position close below decides
        return self._call("DELETE", f"/v2/positions/{symbol}", ok=(200, 207))

    def cancel_order(self, order_id: str) -> Any:
        return self._call("DELETE", f"/v2/orders/{order_id}", ok=(200, 204, 404))

    def cancel_all_orders(self) -> Any:
        return self._call("DELETE", "/v2/orders", ok=(200, 204, 207))


# ── offline selftest ─────────────────────────────────────────────────────────
class FakeTransport:
    """Canned-response transport for offline tests; records every call."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.routes: Dict[tuple, Dict[str, Any]] = {}
        self.fail_next: Optional[Dict[str, Any]] = None

    def route(self, method: str, path_prefix: str, status: int, payload: Any) -> None:
        self.routes[(method, path_prefix)] = {"status": status, "json": payload}

    def __call__(self, method: str, url: str, headers: Dict[str, str],
                 body: Optional[dict]) -> Dict[str, Any]:
        path = url.split(".markets", 1)[-1]
        self.calls.append((method, path, body))
        if self.fail_next is not None:
            resp, self.fail_next = self.fail_next, None
            return resp
        for (m, prefix), resp in self.routes.items():
            if m == method and path.startswith(prefix):
                return resp
        return {"status": 404, "json": {"message": f"no route {method} {path}"}}


def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("broker_alpaca --selftest (offline)")

    # G1: live lock refuses non-paper URL
    try:
        AlpacaBroker(base_url="https://api.alpaca.markets", transport=FakeTransport())
        check("G1 live-lock refuses live URL", False)
    except LiveLockError:
        check("G1 live-lock refuses live URL", True)

    # G2: live lock needs BOTH unlocks (cfg alone insufficient)
    os.environ.pop("EXEC_BOT_ALLOW_LIVE", None)
    try:
        AlpacaBroker(base_url="https://api.alpaca.markets", allow_live_cfg=True,
                     transport=FakeTransport())
        check("G2 cfg-only unlock insufficient", False)
    except LiveLockError:
        check("G2 cfg-only unlock insufficient", True)

    ft = FakeTransport()
    ft.route("GET", "/v2/account", 200, {"status": "ACTIVE", "cash": "100000"})
    ft.route("GET", "/v2/clock", 200, {"is_open": False, "next_open": "x"})
    ft.route("GET", "/v2/positions", 200, [])
    ft.route("GET", "/v2/orders", 200, [])
    ft.route("POST", "/v2/orders", 200, {"id": "abc", "status": "accepted",
                                         "client_order_id": "execbot-2026-07-08-TEST"})
    br = AlpacaBroker(transport=ft)

    # G3: paper accepted + account read
    check("G3 paper base accepted", br.is_paper)
    check("G3 account read", br.get_account().get("status") == "ACTIVE")

    # G4: bracket submit builds correct body
    br.submit_bracket_buy("TEST", qty=10, take_profit=110.0, stop_loss=95.0,
                          client_order_id="execbot-2026-07-08-TEST")
    m, p, body = ft.calls[-1]
    check("G4 bracket order_class", body and body.get("order_class") == "bracket")
    check("G4 bracket qty str", body and body.get("qty") == "10")
    check("G4 bracket tp/sl set", body and "take_profit" in body and "stop_loss" in body)
    check("G4 long-only side buy", body and body.get("side") == "buy")

    # G5: invalid bracket prices refused (sl >= tp)
    try:
        br.submit_bracket_buy("TEST", qty=1, take_profit=90.0, stop_loss=95.0,
                              client_order_id="x")
        check("G5 invalid bracket refused", False)
    except BrokerError:
        check("G5 invalid bracket refused", True)

    # G6: qty<=0 refused
    try:
        br.submit_bracket_buy("TEST", qty=0, take_profit=110.0, stop_loss=95.0,
                              client_order_id="x")
        check("G6 zero qty refused", False)
    except BrokerError:
        check("G6 zero qty refused", True)

    # G7: api error surfaces as BrokerError (e.g. duplicate client_order_id 422)
    ft.fail_next = {"status": 422, "json": {"message": "client_order_id must be unique"}}
    try:
        br.get_account()
        check("G7 api error raises", False)
    except BrokerError as e:
        check("G7 api error raises", "422" in str(e))

    # G8: close_position cancels sibling open orders first
    ft.route("GET", "/v2/orders", 200, [{"id": "o1", "symbol": "TEST"}])
    ft.route("DELETE", "/v2/orders/o1", 204, None)
    ft.route("DELETE", "/v2/positions/TEST", 200, {"symbol": "TEST"})
    br.close_position("TEST")
    deletes = [c for c in ft.calls if c[0] == "DELETE"]
    check("G8 close cancels legs then position",
          len(deletes) >= 2 and deletes[-1][1].startswith("/v2/positions/TEST"))

    # G9: env file parser ignores comments/garbage
    parsed = load_env_file()
    check("G9 env parser returns dict", isinstance(parsed, dict))

    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


def _probe() -> int:
    creds = resolve_credentials()
    if not creds["key"] or not creds["secret"]:
        print("PROBE: keys missing, set ALPACA_KEY_ID/ALPACA_SECRET_KEY in tools/.env")
        return 2
    try:
        br = AlpacaBroker(key_id=creds["key"], secret_key=creds["secret"])
        acct = br.get_account()
        clock = br.get_clock()
    except BrokerError as e:
        print(f"PROBE FAILED: {str(e)[:300]} (check keys, network and the paper endpoint)")
        return 1
    print(f"PROBE OK paper account status={acct.get('status')} "
          f"market_open={clock.get('is_open')}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--probe" in sys.argv:
        sys.exit(_probe())
    print(__doc__)
