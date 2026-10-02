#!/usr/bin/env python3
"""
catalyst_calendar.py: Weekly Catalyst-Kalender (Sonntag 20:00 CET).

Listet fuer die kommende Woche:
  * Earnings-Termine aus der Watchlist (yfinance calendar / earnings_dates)
  * US-Macro-Events aus fester Liste (CPI, PPI, FOMC, NFP, Retail-Sales, GDP)

Watchlist: data/watchlist_global.txt (gitignored, ein Ticker pro Zeile; Vorlage:
data/watchlist_global.example.txt). Fehlt die Datei, nutzt das Tool eine kleine
eingebaute Liste US-Large-Caps und sagt das im Report.

Output (lokal, gitignored):
  Briefings/Catalyst-YYYY-KWww.md  (Kompakt-Kalender)
  shadow/catalyst_YYYY_www.json    (maschinenlesbar, mit status ok/partial/degraded)

Ehrlich bei Datenausfall: Schlagen ALLE Earnings-Abrufe fehl, steht im Report
"keine Aussage moeglich" (nie "keine Earnings"), status=degraded und Exit-Code 3.

Exit-Codes: 0 ok oder partial · 3 degraded (keine Earnings-Daten abrufbar) · 1 Fehler

Free-Stack: yfinance only. Keine bezahlten APIs. Keine Anlageberatung.

Usage:
    python3 tools/catalyst_calendar.py
    python3 tools/catalyst_calendar.py --week-offset 1   # naechste Woche
    python3 tools/catalyst_calendar.py --watchlist data/watchlist_global.txt
    python3 tools/catalyst_calendar.py --test
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIEFINGS = REPO_ROOT / "Briefings"   # created on write, not on import
SHADOW = REPO_ROOT / "shadow"
WATCHLIST_DEFAULT = REPO_ROOT / "data" / "watchlist_global.txt"
BUILTIN_WATCHLIST = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "JPM", "XOM", "UNH"]
RC_DEGRADED = 3


# --- US-Macro-Fixschedule ----------------------------------------------------
# Simplified: typische Release-Zeiten. Fuer Produktions-Schaerfe FRED-API
# anbinden (investpy/fred) - das ist H10 MCP-Integration. Heute: Pattern.

MACRO_PATTERNS = [
    ("CPI",            "~13. Kalendertag, 14:30 CET", "hoch"),
    ("PPI",            "~14. Kalendertag, 14:30 CET", "mittel"),
    ("Retail Sales",   "~15. Kalendertag, 14:30 CET", "mittel"),
    ("FOMC",           "8x jaehrlich, Mi 20:00 CET",  "sehr hoch"),
    ("NFP",            "1. Freitag im Monat, 14:30 CET", "sehr hoch"),
    ("GDP",            "Quartalsweise, letzte April-Woche", "hoch"),
    ("Jobless Claims", "Jeden Donnerstag 14:30 CET",  "niedrig-mittel"),
]


@dataclass
class EarningsEvent:
    ticker: str
    date: str
    time_hint: str       # "BMO" / "AMC" / ""
    eps_estimate: float | None
    watchlist_tier: str


@dataclass
class MacroEvent:
    name: str
    schedule_pattern: str
    impact: str


def load_watchlist(path: Path) -> list[str]:
    tickers, _source = load_watchlist_with_source(path)
    return tickers


def load_watchlist_with_source(path: Path) -> tuple[list[str], str]:
    """Returns (tickers, source). source is the file name or "builtin_fallback"."""
    if not path.exists():
        print(f"NOTE: watchlist {path} not found, using the built-in list of "
              f"{len(BUILTIN_WATCHLIST)} US large caps (copy "
              "data/watchlist_global.example.txt to data/watchlist_global.txt).",
              file=sys.stderr)
        return list(BUILTIN_WATCHLIST), "builtin_fallback"
    tickers = [l.strip() for l in path.read_text().splitlines()
               if l.strip() and not l.strip().startswith("#")]
    return tickers, path.name


def iso_week(dt: datetime) -> tuple[int, int]:
    y, w, _ = dt.isocalendar()
    return y, w


def week_range(offset: int = 0) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Kommende Woche beginnend Montag
    days_to_monday = (7 - now.weekday()) % 7 or 7
    start = (now + timedelta(days=days_to_monday + 7 * offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=6, hours=23, minutes=59)
    return start, end


def fetch_earnings(tickers: list[str], start: datetime, end: datetime) -> list[EarningsEvent]:
    events, _failed = fetch_earnings_with_stats(tickers, start, end)
    return events


def fetch_earnings_with_stats(tickers: list[str], start: datetime,
                              end: datetime) -> tuple[list[EarningsEvent], int]:
    """Returns (events, n_failed). Every ticker whose lookup raised counts as failed;
    without yfinance all tickers count as failed."""
    try:
        import yfinance as yf
    except ImportError:
        print("WARN: yfinance nicht verfuegbar, keine Earnings-Daten", file=sys.stderr)
        return [], len(tickers)
    import pandas as pd
    events: list[EarningsEvent] = []
    n_failed = 0
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            edf = tk.get_earnings_dates(limit=12)
            if edf is None or edf.empty:
                n_failed += 1  # no earnings data at all (rate limit, delisted, ETF ...)
                continue
            idx = edf.index
            if hasattr(idx, "tz") and idx.tz is not None:
                edf = edf.copy()
                edf.index = idx.tz_localize(None)
            mask = (edf.index >= start) & (edf.index <= end)
            hits = edf[mask]
            for ts, row in hits.iterrows():
                est = row.get("EPS Estimate")
                est_f = float(est) if (est is not None and not pd.isna(est)) else None
                events.append(EarningsEvent(
                    ticker=t,
                    date=ts.strftime("%Y-%m-%d %H:%M"),
                    time_hint="",
                    eps_estimate=est_f,
                    watchlist_tier="A",
                ))
        except Exception as e:  # noqa: BLE001  (counted, reported below)
            n_failed += 1
            print(f"WARN: earnings lookup failed for {t}: {str(e)[:100]}", file=sys.stderr)
            continue
    events.sort(key=lambda e: e.date)
    return events, n_failed


def earnings_status(n_tickers: int, n_failed: int) -> str:
    if n_tickers > 0 and n_failed >= n_tickers:
        return "degraded"
    return "partial" if n_failed else "ok"


def render_markdown(year: int, week: int, start: datetime, end: datetime,
                    earnings: list[EarningsEvent], macro: list[MacroEvent],
                    *, n_tickers: int | None = None, n_failed: int = 0,
                    watchlist_source: str = "") -> str:
    n_tickers = len(earnings) if n_tickers is None else n_tickers
    status = earnings_status(n_tickers, n_failed)
    lines = []
    lines.append(f"# Catalyst-Kalender KW {week}/{year}")
    lines.append("")
    lines.append(f"**Zeitraum:** {start.strftime('%Y-%m-%d')} bis {end.strftime('%Y-%m-%d')}")
    lines.append(f"**Generiert:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if watchlist_source:
        src = ("eingebaute Liste (keine data/watchlist_global.txt)"
               if watchlist_source == "builtin_fallback" else watchlist_source)
        lines.append(f"**Watchlist:** {src}, {n_tickers} Ticker")
    lines.append(f"**Datenstatus:** {status}"
                 + (f" ({n_failed} von {n_tickers} Earnings-Abrufen fehlgeschlagen)" if n_failed else ""))
    lines.append("")
    lines.append("## Earnings-Termine (Watchlist)")
    lines.append("")
    if status == "degraded":
        lines.append("**Earnings-Daten konnten nicht geladen werden. Keine Aussage moeglich, "
                     "ob diese Woche Watchlist-Earnings anstehen.**")
    elif not earnings:
        lines.append("*Keine Watchlist-Earnings in dieser Woche"
                     + (" (unvollstaendig: nicht alle Abrufe erfolgreich)." if n_failed else ".")
                     + "*")
    else:
        lines.append("| Datum (UTC) | Ticker | Tier | EPS Estimate | Hinweis |")
        lines.append("|---|---|---|---|---|")
        for e in earnings:
            est = f"{e.eps_estimate:.2f}" if e.eps_estimate is not None else "n/a"
            lines.append(f"| {e.date} | **{e.ticker}** | {e.watchlist_tier} | {est} | watch |")
    lines.append("")
    lines.append("## US-Macro-Muster (orientierend)")
    lines.append("")
    lines.append("| Event | Typisches Timing | Impact |")
    lines.append("|---|---|---|")
    for m in macro:
        lines.append(f"| {m.name} | {m.schedule_pattern} | {m.impact} |")
    lines.append("")
    lines.append("> *Feinkalender: investing.com / tradingeconomics.com manuell pruefen.*")
    lines.append("> *Fuer Produktions-Pipeline: FRED-API-Anbindung (H10 MCP-Roadmap).*")
    lines.append("")
    lines.append("## System-Regeln (Hinweis)")
    lines.append("")
    lines.append("- **Earnings-Blackout:** Das System plant am Earnings-Tag keine Mean-Reversion-Entries (Gap-Risk).")
    lines.append("- **STRAT-PEAD-1 Shadow-Scanner** am Folgetag 16:00 CET pruefen.")
    lines.append("")
    lines.append("---")
    lines.append("*Generiert von `tools/catalyst_calendar.py`. Forschungswerkzeug, keine Anlageberatung.*")
    return "\n".join(lines) + "\n"


def run(week_offset: int = 0, watchlist_path: Path = WATCHLIST_DEFAULT) -> dict:
    start, end = week_range(week_offset)
    year, week = iso_week(start)
    tickers, wl_source = load_watchlist_with_source(watchlist_path)
    earnings, n_failed = fetch_earnings_with_stats(tickers, start, end)
    status = earnings_status(len(tickers), n_failed)
    macro = [MacroEvent(*m) for m in MACRO_PATTERNS]
    md = render_markdown(year, week, start, end, earnings, macro,
                         n_tickers=len(tickers), n_failed=n_failed,
                         watchlist_source=wl_source)

    BRIEFINGS.mkdir(exist_ok=True)
    SHADOW.mkdir(exist_ok=True)
    md_out = BRIEFINGS / f"Catalyst-{year}-KW{week:02d}.md"
    md_out.write_text(md)
    json_out = SHADOW / f"catalyst_{year}_w{week:02d}.json"
    json_out.write_text(json.dumps({
        "year": year, "week": week,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "status": status,
        "watchlist_source": wl_source,
        "watchlist_size": len(tickers),
        "earnings_lookups_failed": n_failed,
        "earnings": [asdict(e) for e in earnings],
        "macro_patterns": [asdict(m) for m in macro],
    }, indent=2, ensure_ascii=False))
    return {
        "markdown": str(md_out), "json": str(json_out), "status": status,
        "earnings_count": len(earnings), "earnings_lookups_failed": n_failed,
        "year": year, "week": week,
    }


def _self_test() -> int:
    s, e = week_range(0)
    assert (e - s).days == 6
    macro = [MacroEvent(*m) for m in MACRO_PATTERNS]
    md = render_markdown(2026, 17, s, e, [], macro, n_tickers=8, n_failed=0)
    assert "Catalyst-Kalender KW 17" in md
    assert "US-Macro-Muster" in md
    assert "Keine Watchlist-Earnings in dieser Woche." in md
    # all lookups failed -> never claim "no earnings"
    md_bad = render_markdown(2026, 17, s, e, [], macro, n_tickers=8, n_failed=8)
    assert "Keine Aussage moeglich" in md_bad and "Keine Watchlist-Earnings" not in md_bad
    assert earnings_status(8, 8) == "degraded"
    assert earnings_status(8, 1) == "partial"
    assert earnings_status(8, 0) == "ok"
    print("catalyst_calendar self-test: PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--week-offset", type=int, default=0)
    p.add_argument("--watchlist", type=Path)
    p.add_argument("--test", action="store_true")
    a = p.parse_args()
    if a.test:
        return _self_test()
    res = run(a.week_offset, a.watchlist or WATCHLIST_DEFAULT)
    print(json.dumps(res, indent=2))
    if res["status"] == "degraded":
        print("CATALYST DEGRADED: no earnings data could be fetched (exit code 3). "
              "The report says so instead of claiming there are no earnings.",
              file=sys.stderr)
        return RC_DEGRADED
    return 0


if __name__ == "__main__":
    sys.exit(main())
