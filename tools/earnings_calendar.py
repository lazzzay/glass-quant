"""
V6.3 Earnings-Calendar & Blackout-Tool

Für jeden Ticker im Universe:
- Hole nächstes Earnings-Datum via yfinance
- Markiere Assets mit Earnings in < N Tagen (default 3) als BLACKOUT
- Output: tools/earnings_YYYY-MM-DD.json + CSV

Nutzt yfinance (kein API-Key nötig). Fallback: wenn yfinance kein Datum liefert,
wird der Ticker als "unknown" markiert (kein Blackout: lieber keine Falsch-Sperre).
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    if __name__ == "__main__":
        print("ERROR: pip install yfinance pandas --break-system-packages", file=sys.stderr)
        sys.exit(1)
    raise  # FIX 2026-06-01: Import-safe (kein Prozess-Kill bei fehlendem Dep)

TOOLS_DIR = Path(__file__).parent
UNIVERSE_CSV = TOOLS_DIR / "universe.csv"


def get_next_earnings(ticker: str) -> dict:
    """
    Liefert für einen Ticker:
    {
      "ticker": ..., "next_date": "YYYY-MM-DD" or None,
      "days_until": int or None,
      "blackout": bool,
      "status": "ok"|"unknown"|"error"
    }
    """
    out = {"ticker": ticker, "next_date": None, "days_until": None, "blackout": False, "status": "unknown"}
    try:
        t = yf.Ticker(ticker)
        # .calendar is the most reliable path for upcoming earnings
        cal = None
        try:
            cal = t.calendar
        except Exception:
            cal = None

        next_dt = None
        if cal is not None:
            # yfinance can return dict or DataFrame depending on version
            if isinstance(cal, dict):
                # Keys vary: 'Earnings Date' commonly
                ed = cal.get("Earnings Date") or cal.get("earningsDate") or cal.get("Earnings Dates")
                if isinstance(ed, list) and ed:
                    next_dt = ed[0]
                elif ed is not None:
                    next_dt = ed
            else:
                # DataFrame
                try:
                    if "Earnings Date" in cal.index:
                        val = cal.loc["Earnings Date"]
                        if hasattr(val, "iloc"):
                            next_dt = val.iloc[0]
                        else:
                            next_dt = val
                except Exception:
                    pass

        # Fallback: earnings_dates attribute
        if next_dt is None:
            try:
                ed = t.earnings_dates
                if ed is not None and not ed.empty:
                    today = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None).date())
                    # earnings_dates index is a DatetimeIndex; filter future
                    try:
                        future = ed[ed.index >= today]
                    except TypeError:
                        # Tz-aware index
                        future = ed[ed.index.date >= today.date()]
                    if not future.empty:
                        next_dt = future.index.min()
            except Exception:
                pass

        if next_dt is not None:
            # Normalize to date
            if hasattr(next_dt, "date"):
                nd = next_dt.date()
            elif isinstance(next_dt, (datetime, date)):
                nd = next_dt if isinstance(next_dt, date) and not isinstance(next_dt, datetime) else next_dt.date()
            else:
                # Try parse string
                try:
                    nd = pd.Timestamp(next_dt).date()
                except Exception:
                    return out
            today = datetime.now(timezone.utc).replace(tzinfo=None).date()
            days = (nd - today).days
            out["next_date"] = nd.isoformat()
            out["days_until"] = days
            out["status"] = "ok"
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:80]
    return out


def derive_status(universe_size: int, with_known_dates: int) -> str:
    """Output-Contract-Status (EARNINGS-PRODUCER-OUTAGE-1, 2026-06-14).
    'ok'       : Lauf ok, Earnings-Dates gefunden ODER leeres Equity-Universe.
    'degraded' : Universe>0 aber 0 Dates aufgeloest → Quelle vermutlich nicht erreichbar
                 (yfinance-Throttle/cert). File wird TROTZDEM geschrieben (kein MISSING),
                 der Status signalisiert die Unsicherheit ehrlich statt sie zu verstecken.
    """
    if universe_size == 0:
        return "ok"
    return "ok" if with_known_dates > 0 else "degraded"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="Scan date (YYYY-MM-DD)")
    ap.add_argument("--blackout-days", type=int, default=3, help="Days-until threshold (default 3)")
    ap.add_argument("--tier", default=None, help="Filter to tier A/B/C")
    ap.add_argument("--limit", type=int, default=None, help="Limit tickers (debug)")
    args = ap.parse_args()

    scan_date = args.date or datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    out_csv = TOOLS_DIR / f"earnings_{scan_date}.csv"
    out_json = TOOLS_DIR / f"earnings_{scan_date}.json"

    # EARNINGS-PRODUCER-OUTAGE-1-FIX (2026-06-14): Output-Contract. Es wird IMMER ein
    # valides earnings_<date>.json hinterlassen (nie silent-no-file), sonst flaggt
    # data_health taeglich EARNINGS_JSON_MISSING (SEV_H), obwohl der Producer nur
    # frueh-morgens leer/gekillt war.
    def _write_json(summary: dict):
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)

    def _base(status: str, **kw) -> dict:
        s = {"scan_date": scan_date, "status": status,
             "blackout_days_threshold": args.blackout_days, "universe_size": 0,
             "with_known_dates": 0, "blackout_count": 0,
             "blackout_tickers": [], "blackout_details": []}
        s.update(kw)
        return s

    try:
        universe = []
        with open(UNIVERSE_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if args.tier and row["tier"] != args.tier:
                    continue
                # Crypto + Commodities + FX + Index haben keine Earnings
                if row["asset_class"] in ("crypto", "commodity", "fx", "index"):
                    continue
                universe.append(row)
        if args.limit:
            universe = universe[:args.limit]

        # Frueh-Platzhalter: File existiert ab t~0 → kein EARNINGS_JSON_MISSING-Race,
        # selbst wenn der langsame yfinance-Loop abgebrochen/gekillt wird.
        _write_json(_base("pending", universe_size=len(universe)))
        print(f"Checking earnings for {len(universe)} tickers ...", file=sys.stderr)

        results = []
        blackout_list = []
        for i, meta in enumerate(universe):
            if i % 50 == 0 and i > 0:
                print(f"  [{i}/{len(universe)}] done", file=sys.stderr)
            info = get_next_earnings(meta["ticker"])
            info["name"] = meta["name"]
            info["tier"] = meta["tier"]
            info["region"] = meta["region"]
            if info["status"] == "ok" and info["days_until"] is not None:
                if 0 <= info["days_until"] < args.blackout_days:
                    info["blackout"] = True
                    blackout_list.append(info)
            results.append(info)

        # Save CSV
        if results:
            fieldnames = ["ticker","name","tier","region","next_date","days_until","blackout","status"]
            with open(out_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                for r in results:
                    w.writerow(r)

        # Final-JSON mit ehrlichem Status
        with_known = sum(1 for r in results if r["status"] == "ok")
        summary = _base(
            derive_status(len(universe), with_known),
            universe_size=len(universe),
            with_known_dates=with_known,
            blackout_count=len(blackout_list),
            blackout_tickers=[r["ticker"] for r in blackout_list],
            blackout_details=blackout_list,
        )
        _write_json(summary)

        print(f"\n=== EARNINGS BLACKOUT REPORT ({scan_date}) [{summary['status']}] ===")
        print(f"Universe: {len(universe)} | Known dates: {with_known} | BLACKOUT: {len(blackout_list)}")
        if blackout_list:
            print(f"\nBlackout Tickers (Earnings in < {args.blackout_days} days):")
            for r in sorted(blackout_list, key=lambda x: x.get("days_until") or 99):
                print(f"  {r['ticker']:<10} {r['tier']} {r['region']:<4} -> {r['next_date']} (in {r['days_until']}d) [{r['name']}]")
        print(f"\nSaved: {out_csv}")
        print(f"Saved: {out_json}")
        if summary["status"] == "degraded":
            print("WARN: 0 Earnings-Dates trotz nicht-leerem Universe, Quelle vermutlich "
                  "nicht erreichbar. File geschrieben (kein MISSING), Status=degraded.",
                  file=sys.stderr)
    except SystemExit:
        raise
    except Exception as e:
        # Echter Fehler (z.B. universe.csv unlesbar): valides error-File + Non-Zero,
        # damit die Pipeline ehrlich FAIL loggt (statt OK), aber kein MISSING.
        _write_json(_base("error", error=str(e)[:200]))
        print(f"ERROR: earnings_calendar failed: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
