#!/usr/bin/env python3
"""notify.py: V7.40 G1: Push-Alerts via ntfy.sh (free, kein Account).

Setup (one time, about 2 minutes):
  1. Install the ntfy app on your phone (iOS/Android, free) and subscribe to
     your own topic.
  2. Choose a SECRET, random topic name (it works like a password), for example
     <your-random-topic>. Anyone who knows the topic can read your pushes.
  3. Put it into tools/.env:  NTFY_TOPIC=<your-random-topic>

Design: fire-and-forget. send() wirft NIEMALS (ein kaputter Push darf nie den
Bot/die Pipeline stoppen), gibt nur True/False zurück. Ohne NTFY_TOPIC: No-op.
CLI: --selftest | --test "Nachricht" (echter Push zum Verifizieren)
"""
from __future__ import annotations

import os
import sys
import urllib.request
from typing import Callable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from broker_alpaca import load_env_file  # noqa: E402  (gleicher .env-Loader)

NTFY_BASE = "https://ntfy.sh"
TIMEOUT_S = 10


def resolve_topic() -> str:
    return os.environ.get("NTFY_TOPIC") or load_env_file().get("NTFY_TOPIC", "")


def _default_transport(url: str, data: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.status


def send(title: str, message: str, priority: str = "default",
         tags: str = "", transport: Optional[Callable] = None) -> bool:
    """Send a push notification. NIE werfend; False bei fehlendem Topic/Fehler."""
    try:
        topic = resolve_topic()
        if not topic:
            return False
        headers = {"Title": title.encode("ascii", "ignore").decode(),
                   "Priority": priority}
        if tags:
            headers["Tags"] = tags
        tp = transport or _default_transport
        status = tp(f"{NTFY_BASE}/{topic}", message.encode("utf-8"), headers)
        return 200 <= int(status) < 300
    except Exception:  # noqa: BLE001, bewusst: Push darf nie etwas stoppen
        return False


# ── selftest ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("notify --selftest (offline)")
    calls = []

    def fake_tp(url, data, headers):
        calls.append((url, data, headers))
        return 200

    def broken_tp(url, data, headers):
        raise RuntimeError("network down")

    old = os.environ.get("NTFY_TOPIC")
    os.environ["NTFY_TOPIC"] = "test-topic-xyz"
    ok = send("Exec-Bot", "Fill: TEST 1@100.00", priority="high",
              tags="robot", transport=fake_tp)
    check("G1 send liefert True + POSTet aufs Topic",
          ok and calls and calls[0][0].endswith("/test-topic-xyz"))
    check("G1 Title/Priority/Tags-Header gesetzt",
          calls[0][2].get("Title") == "Exec-Bot"
          and calls[0][2].get("Priority") == "high"
          and calls[0][2].get("Tags") == "robot")
    check("G2 kaputtes Netz → False statt Exception",
          send("x", "y", transport=broken_tp) is False)
    os.environ.pop("NTFY_TOPIC", None)
    if old is None and "NTFY_TOPIC" in load_env_file():
        pass  # .env-Topic vorhanden, No-op-Test unten übersprungen
    if not load_env_file().get("NTFY_TOPIC") :
        check("G3 ohne Topic → No-op False", send("x", "y", transport=fake_tp) is False)
    if old is not None:
        os.environ["NTFY_TOPIC"] = old
    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--test" in sys.argv:
        msg = sys.argv[sys.argv.index("--test") + 1] if len(sys.argv) > sys.argv.index("--test") + 1 else "Test"
        ok = send("Daily Invest: Test", msg, tags="tada")
        print("PUSH OK" if ok else "PUSH FEHLGESCHLAGEN (NTFY_TOPIC in tools/.env gesetzt?)")
        sys.exit(0 if ok else 1)
    print(__doc__)
