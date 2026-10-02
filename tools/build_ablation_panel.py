#!/usr/bin/env python3
"""
build_ablation_panel.py: erzeugt das Panel fuer layer_ablation.py.

Zieht Basis-Score + alle Layer-Adjusts je Ticker aus den vorhandenen
scan_YYYY-MM-DD.json (Feld `ticker_map`) und joint Forward-Returns aus
Kursdaten (yfinance auf dem Mac). Output -> panel.json fuer layer_ablation.py.

CLI (auf dem Mac, mit yfinance):
    python3 tools/build_ablation_panel.py --start 2025-06-01 --end 2026-05-31 \
        --horizon 5 --tiers A,B --out panel.json

Reine Logik (extract_rows, compute_forward) ist netz-/yfinance-frei und getestet;
nur der Kursabruf braucht yfinance.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

# panel-Layer-Name  ->  Feldname im scan ticker_map
LAYER_FIELDS = {
    "sector_rs": "sector_rs_adjust",
    "insider": "insider_adjust",
    "options_flow": "options_flow_adjust",
    "market_pcr": "market_pcr_adjust",
    "news": "news_adjust",
    "mtf": "mtf_adjust",
}
_DAILY_SCAN = re.compile(r"scan_(\d{4}-\d{2}-\d{2})\.json$")


def extract_rows(scan: dict) -> list[dict]:
    """Basis + Adjusts je Ticker aus EINEM scan_*.json (ohne forward_return)."""
    rows = []
    for tk, e in (scan.get("ticker_map") or {}).items():
        base = e.get("pre_score_raw")
        if base is None:
            base = e.get("pre_score")
        if base is None:
            continue
        rows.append({
            "date": e.get("date") or scan.get("scan_date"),
            "ticker": tk,
            "base_score": float(base),
            "adjusts": {name: float(e.get(f) or 0.0) for name, f in LAYER_FIELDS.items()},
            "regime_ok": str(e.get("weekly_trend", "")).lower() == "bullish",
            "tier": e.get("tier"),
            "region": e.get("region"),
            "forward_return": None,
        })
    return rows


def collect_scans(start: str, end: str, tiers: set[str] | None) -> list[dict]:
    rows = []
    for fp in sorted(glob.glob(str(TOOLS / "scan_*.json"))):
        m = _DAILY_SCAN.search(fp)
        if not m:
            continue
        d = m.group(1)
        if d < start or d > end:
            continue
        try:
            scan = json.load(open(fp, encoding="utf-8"))
        except Exception as ex:  # noqa
            print(f"[warn] {fp}: {ex}", file=sys.stderr)
            continue
        for r in extract_rows(scan):
            if tiers and r.get("tier") not in tiers:
                continue
            rows.append(r)
    return rows


def compute_forward(rows: list[dict], price_lookup: dict[str, list[tuple]], horizon: int) -> int:
    """Fuellt forward_return = close[t+horizon]/close[t]-1.

    price_lookup: {ticker: [(date_str, close), ... aufsteigend sortiert]}
    Gibt Anzahl gefuellter Zeilen zurueck. Reine Logik (testbar mit Stub).
    """
    filled = 0
    idx = {tk: {d: i for i, (d, _) in enumerate(series)} for tk, series in price_lookup.items()}
    for r in rows:
        tk, d = r["ticker"], r["date"]
        series = price_lookup.get(tk)
        if not series or d not in idx.get(tk, {}):
            continue
        i = idx[tk][d]
        j = i + horizon
        if j >= len(series):
            continue
        c0 = series[i][1]
        c1 = series[j][1]
        if c0:
            r["forward_return"] = round(c1 / c0 - 1.0, 5)
            filled += 1
    return filled


def fetch_prices_yf(tickers: list[str], start: str, end: str) -> dict[str, list[tuple]]:
    """yfinance-Kursserien (nur Mac). Import-safe: bei fehlendem Dep ImportError."""
    try:
        import yfinance as yf
    except ImportError:
        if __name__ == "__main__":
            print("ERROR: pip install yfinance --break-system-packages", file=sys.stderr)
            sys.exit(1)
        raise
    out: dict[str, list[tuple]] = {}
    data = yf.download(tickers, start=start, end=end, progress=False, group_by="ticker", auto_adjust=True)
    for tk in tickers:
        try:
            ser = data[tk]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            out[tk] = [(ts.strftime("%Y-%m-%d"), float(v)) for ts, v in ser.items()]
        except Exception:
            continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Panel-Generator fuer layer_ablation.py")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--horizon", type=int, default=5, help="Hold-Horizont in Handelstagen (Connors 3-5)")
    ap.add_argument("--tiers", default="A,B", help="Komma-Liste; leer = alle")
    ap.add_argument("--out", default="panel.json")
    args = ap.parse_args(argv)

    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()} or None
    rows = collect_scans(args.start, args.end, tiers)
    print(f"[+] {len(rows)} Kandidaten-Zeilen aus Scans ({args.start}..{args.end}, tiers={tiers})", file=sys.stderr)
    if not rows:
        print("[!] Keine Zeilen: Datumsbereich/Scans pruefen.", file=sys.stderr)
        return 1
    tickers = sorted({r["ticker"] for r in rows})
    prices = fetch_prices_yf(tickers, args.start, _plus_buffer(args.end, args.horizon))
    filled = compute_forward(rows, prices, args.horizon)
    panel = [r for r in rows if r["forward_return"] is not None]
    print(f"[+] forward_return gefuellt: {filled}/{len(rows)} (Rest verworfen: keine Kursdaten)", file=sys.stderr)
    Path(args.out).write_text(json.dumps(panel, indent=2), encoding="utf-8")
    print(f"[+] Panel: {args.out} ({len(panel)} Zeilen)\n    -> python3 tools/layer_ablation.py --panel {args.out}", file=sys.stderr)
    return 0


def _plus_buffer(end: str, horizon: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=horizon * 2 + 7)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    raise SystemExit(main())
