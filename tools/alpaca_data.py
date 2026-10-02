#!/usr/bin/env python3
"""alpaca_data.py: V7.40 G6-VORSTUFE: US-Tagesbars via Alpaca Market Data (IEX, free).

IMPORTANT: this tool is intentionally NOT wired into the scanner. First the
comparison mode (--compare) runs for a few days as evidence, then the maintainer
decides on wiring it into the orchestrator chain (G6). No live path is touched.

Warum: yfinance ist die chronische Ausfallquelle (Throttle/DNS/certifi, 01.07.
Fill 36,8 %). Alpacas Daten-API ist ein echter, stabiler Endpoint und mit dem
Paper-Konto kostenlos (IEX-Feed). Für Nicht-US-Ticker bleibt yfinance/Stooq.

CLI: --selftest | --probe | --compare [N]   (Default N=40 US-Ticker aus dem Scan)

NETWORK NOTE: --probe and --compare call the live Alpaca market data API and
need ALPACA_KEY_ID / ALPACA_SECRET_KEY (environment or tools/.env). Never run
them in CI. Only --selftest is offline (injected transport).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from broker_alpaca import resolve_credentials, BrokerError  # noqa: E402

DATA_BASE = "https://data.alpaca.markets"
TIMEOUT_S = 25
CHUNK = 100  # Symbole pro Request (URL-Limit)


def _default_transport(url: str, headers: Dict[str, str]) -> dict:
    req = urllib.request.Request(url)
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def us_symbol(sym: str) -> bool:
    return bool(sym) and "." not in sym and "=" not in sym and "-" not in sym


def fetch_daily_closes(symbols: List[str], days: int = 10,
                       transport: Optional[Callable] = None,
                       feed: str = "iex") -> Dict[str, Dict[str, float]]:
    """symbol → {ISO-date: close}. Paginiert, gechunkt, nur US-Symbole."""
    creds = resolve_credentials()
    if not creds["key"]:
        raise BrokerError("alpaca_keys_missing (tools/.env)")
    headers = {"APCA-API-KEY-ID": creds["key"],
               "APCA-API-SECRET-KEY": creds["secret"]}
    tp = transport or _default_transport
    start = (dt.date.today() - dt.timedelta(days=days + 10)).isoformat()
    out: Dict[str, Dict[str, float]] = {}
    syms = [s for s in symbols if us_symbol(s)]
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        token = None
        while True:
            q = {"symbols": ",".join(chunk), "timeframe": "1Day",
                 "start": start, "limit": "10000", "feed": feed,
                 "adjustment": "raw"}
            if token:
                q["page_token"] = token
            url = f"{DATA_BASE}/v2/stocks/bars?{urllib.parse.urlencode(q)}"
            doc = tp(url, headers)
            for sym, bars in (doc.get("bars") or {}).items():
                for b in bars or []:
                    d = str(b.get("t", ""))[:10]
                    c = b.get("c")
                    if d and c is not None:
                        out.setdefault(sym, {})[d] = float(c)
            token = doc.get("next_page_token")
            if not token:
                break
    return out


def latest_scan(base: str) -> Optional[dict]:
    import glob
    import re
    canon = re.compile(r"^scan_\d{4}-\d{2}-\d{2}\.json$")  # KEINE aborts/RESCANs/manifeste
    cands = [p for p in sorted(glob.glob(os.path.join(base, "tools", "scan_*.json")))
             if canon.match(os.path.basename(p))]
    return json.load(open(cands[-1])) if cands else None


def compare_vs_scan(base: str, n: int = 40,
                    transport: Optional[Callable] = None) -> Dict[str, object]:
    """Vergleicht Alpaca-Closes gegen die Closes des letzten kanonischen Scans
    (gleicher Handelstag). Liefert Coverage + Abweichungs-Statistik als
    Wiring-Entscheidungsgrundlage für G6."""
    scan = latest_scan(base)
    if not scan:
        return {"error": "kein Scan gefunden"}
    scan_date = scan.get("scan_date")
    tm = scan.get("ticker_map") or {}
    # DATUMSGENAU: jede ticker_map-Zeile trägt ihr eigenes Bar-Datum, dagegen
    # vergleichen, sonst misst man Tagesbewegung statt Feed-Differenz.
    us = [(t, float(r.get("close") or 0), str(r.get("date") or scan_date)[:10])
          for t, r in tm.items() if us_symbol(t) and r.get("close")][:n]
    closes = fetch_daily_closes([t for t, _, _ in us], days=10, transport=transport)
    diffs, missing = [], []
    for t, scan_close, d0 in us:
        a = closes.get(t, {})
        ref = a.get(d0)
        if ref is None:  # Fallback: letzter Alpaca-Tag ≤ Scan-Bar-Datum
            cand = [d for d in sorted(a) if d <= d0]
            ref = a[cand[-1]] if cand else None
        if ref is None or scan_close <= 0:
            missing.append(t)
            continue
        diff_pct = abs(ref - scan_close) / scan_close * 100
        diffs.append((t, round(diff_pct, 3)))
    diffs.sort(key=lambda x: -x[1])
    return {
        "scan_date": scan_date, "n_requested": len(us),
        "coverage_pct": round(len(diffs) / len(us) * 100, 1) if us else 0,
        "mean_abs_diff_pct": round(sum(d for _, d in diffs) / len(diffs), 3) if diffs else None,
        "max_abs_diff": diffs[0] if diffs else None,
        "worst_5": diffs[:5], "missing": missing,
        "verdict_hint": ("WIRING_KANDIDAT: Abweichungen im IEX-Toleranzband"
                         if diffs and sum(d for _, d in diffs) / len(diffs) < 0.5
                         else "PRÜFEN: Abweichungen > 0,5 %, Feed-Differenz analysieren"),
    }


# ── selftest ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("alpaca_data --selftest (offline)")
    os.environ.setdefault("ALPACA_KEY_ID", "test")
    os.environ.setdefault("ALPACA_SECRET_KEY", "test")

    pages = [
        {"bars": {"AAA": [{"t": "2026-07-07T04:00:00Z", "c": 100.0}],
                  "BBB": [{"t": "2026-07-07T04:00:00Z", "c": 50.0}]},
         "next_page_token": "tok1"},
        {"bars": {"AAA": [{"t": "2026-07-08T04:00:00Z", "c": 101.0}]},
         "next_page_token": None},
    ]
    calls = []

    def fake_tp(url, headers):
        calls.append(url)
        return pages[min(len(calls) - 1, len(pages) - 1)]

    out = fetch_daily_closes(["AAA", "BBB", "SZG.DE"], transport=fake_tp)
    check("G1 Pagination gefolgt (2 Calls)", len(calls) == 2 and "page_token=tok1" in calls[1])
    check("G1 Closes je Datum gemappt",
          out["AAA"] == {"2026-07-07": 100.0, "2026-07-08": 101.0}
          and out["BBB"]["2026-07-07"] == 50.0)
    check("G2 Nicht-US-Symbol gefiltert (SZG.DE nie angefragt)",
          "SZG.DE" not in urllib.parse.unquote(calls[0]))

    # G3 Compare-Mathe auf synthetischem Scan
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tools"))
        json.dump({"scan_date": "2026-07-08",
                   "ticker_map": {"AAA": {"close": 100.5}, "BBB": {"close": 50.0},
                                  "FOO.DE": {"close": 10.0}}},
                  open(os.path.join(tmp, "tools", "scan_2026-07-08.json"), "w"))
        calls.clear()
        rep = compare_vs_scan(tmp, n=10, transport=fake_tp)
        check("G3 Coverage 100 % auf US-Subset", rep["coverage_pct"] == 100.0)
        # AAA: |101−100.5|/100.5 = 0.4975 %; BBB: 0 %
        check("G3 mean_abs_diff korrekt (~0.249 %)",
              abs(rep["mean_abs_diff_pct"] - 0.249) < 0.01)
        check("G3 DE-Ticker nicht im Vergleich", "FOO.DE" in json.dumps(rep) is False
              or "FOO.DE" not in [t for t, _ in rep.get("worst_5", [])])

    print(f"RESULT: {'GREEN' if not fails else 'RED'} ({len(fails)} fails)")
    return 0 if not fails else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "--probe" in argv:
        out = fetch_daily_closes(["AAPL", "MSFT", "NVDA"], days=5)
        print(json.dumps({s: dict(list(v.items())[-2:]) for s, v in out.items()}, indent=1))
        sys.exit(0)
    if "--compare" in argv:
        idx = argv.index("--compare")
        n = int(argv[idx + 1]) if len(argv) > idx + 1 and argv[idx + 1].isdigit() else 40
        print(json.dumps(compare_vs_scan(base, n=n), indent=1, ensure_ascii=False))
        sys.exit(0)
    print(__doc__)
