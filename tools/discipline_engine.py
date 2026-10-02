"""
V7.4 Discipline-Layer Engine
----------------------------
Berechnet den Discipline-State für das Cockpit:
  - revenge_streak      : Anzahl Loss-Trades in Folge (heute geschlossen)
  - todays_opens        : Anzahl BUY-Trades heute
  - cooldown_until      : ISO-Timestamp bis Modal gesperrt
  - fomo_whitelist      : Heutige Scanner-Top-10-Ticker (aus Trading-Empfehlung-V6.1-*.md)
  - flags               : {revenge, overtrade, cooldown, no_whitelist}

Parser ist rein lesend, kein State-Change im Journal.
Reine Funktionen, keine Side-Effects (außer SQLite-Read).

Verwendet von dashboard_api.py (/discipline/state).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# Cooldown-Dauer nach Revenge-Trigger (2 Losses in Folge)
COOLDOWN_MINUTES = 30
REVENGE_LOSS_THRESHOLD = 2      # 2 Losses in Folge = Trigger
OVERTRADE_WARN = 3              # >3 Opens heute = gelb
OVERTRADE_BLOCK = 5             # >5 Opens heute = rot/blocked


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    # Lokale Datumsgrenze (CET/CEST), nicht UTC: Trader-relevant
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Briefing-Parser: Top-10-Ticker aus Trading-Empfehlung-V6.1-YYYY-MM-DD.md
# ---------------------------------------------------------------------------
def parse_briefing_top10(date: Optional[str] = None) -> dict:
    """Extrahiert die Scanner-Top-10-Ticker aus dem heutigen Empfehlungs-File.
    Fallback: Global-Market-Briefing falls Empfehlung fehlt.

    Return: {
        "date": "YYYY-MM-DD",
        "source": "empfehlung" | "briefing" | "none",
        "tickers": ["AAPL", "MSFT", ...],
        "note": str
    }
    """
    date = date or _today_str()

    candidates = [
        ROOT / f"Trading-Empfehlung-V6.1-{date}.md",
        ROOT / f"Trading-Empfehlung-{date}.md",
        ROOT / f"V6.1-Global-Market-Briefing-{date}.md",
        ROOT / f"Global-Market-Briefing-{date}.md",
    ]

    for p in candidates:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        tickers = _extract_tickers_from_md(text)
        if tickers:
            src = "empfehlung" if "Empfehlung" in p.name else "briefing"
            return {
                "date": date,
                "source": src,
                "tickers": tickers[:10],          # Hard-Cap 10
                "file": p.name,
                "note": f"Top-{len(tickers[:10])} aus {p.name}",
            }

    return {
        "date": date,
        "source": "none",
        "tickers": [],
        "note": f"Kein Empfehlungs- oder Briefing-File für {date} gefunden.",
    }


_TICKER_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([A-Z][A-Z0-9._-]{0,12})\s*\|",
    re.MULTILINE,
)


def _extract_tickers_from_md(text: str) -> list[str]:
    """Sucht in Markdown-Tabellen nach Zeilen der Form `| <num> | <TICKER> | ...`.
    Heuristisch: erster Ticker-Spaltenwert pro Zeile, deduped, max 30."""
    seen = []
    for m in _TICKER_ROW_RE.finditer(text):
        sym = m.group(1).strip()
        # Filter: keine reinen Zahlen, keine erkennbar-nicht-Ticker Tokens
        if sym and sym not in seen and not sym.isdigit():
            if sym.upper() in {"RSI2", "RSI14", "MTF", "CQ", "PS3D", "ATR", "GICS"}:
                continue
            seen.append(sym)
        if len(seen) >= 30:
            break
    return seen


# ---------------------------------------------------------------------------
# Discipline-State aus Journal
# ---------------------------------------------------------------------------
def compute_state(con: sqlite3.Connection, today: Optional[str] = None) -> dict:
    """Berechnet aktuellen Discipline-State.
    Nutzt bestehende Connection, öffnet nichts neues.
    Return: siehe Modul-Docstring.
    """
    today = today or _today_str()
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # ---- 1. Revenge-Streak (heutige Closed-Trades in chronologischer Reihenfolge) ----
    # Wir betrachten CLOSED trades heute (exit_date=today), sortiert nach updated_at_utc.
    closed_today = cur.execute("""
        SELECT id, ticker, entry_price, exit_price, exit_date, updated_at_utc
          FROM trades
         WHERE exit_date = ?
           AND exit_price IS NOT NULL
           AND entry_price IS NOT NULL
         ORDER BY COALESCE(updated_at_utc, exit_date) ASC
    """, (today,)).fetchall()

    revenge_streak = 0
    for r in reversed(closed_today):
        pnl = (r["exit_price"] or 0) - (r["entry_price"] or 0)
        if pnl < 0:
            revenge_streak += 1
        else:
            break  # Streak bricht bei Gewinn

    # ---- 2. Opens heute ----
    opens_today = cur.execute("""
        SELECT COUNT(*) FROM trades
         WHERE side = 'BUY' AND entry_date = ?
    """, (today,)).fetchone()[0]

    # ---- 3. Cooldown aus letzter Override-Aktion ODER letztem Loss-Paar ----
    cooldown_until = None
    cooldown_reason = None

    # 3a) Wenn Revenge-Streak >= Threshold, Cooldown bis X Minuten nach letztem Loss-Close
    if revenge_streak >= REVENGE_LOSS_THRESHOLD and closed_today:
        last_loss = closed_today[-1]
        last_ts = last_loss["updated_at_utc"]
        if last_ts:
            try:
                ts = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                cooldown_end = ts + timedelta(minutes=COOLDOWN_MINUTES)
                if cooldown_end > _now_utc():
                    cooldown_until = cooldown_end.strftime("%Y-%m-%dT%H:%M:%SZ")
                    cooldown_reason = f"Revenge-Streak {revenge_streak} Losses"
            except Exception:
                pass

    # ---- 4. FOMO-Whitelist ----
    wl = parse_briefing_top10(today)
    fomo_whitelist = wl["tickers"]

    # ---- 5. Flags ----
    flags = {
        "revenge":       revenge_streak >= REVENGE_LOSS_THRESHOLD,
        "overtrade_warn": opens_today >= OVERTRADE_WARN,
        "overtrade_block": opens_today >= OVERTRADE_BLOCK,
        "cooldown":      cooldown_until is not None,
        "no_whitelist":  len(fomo_whitelist) == 0,
    }

    return {
        "today": today,
        "revenge_streak": revenge_streak,
        "todays_opens": opens_today,
        "cooldown_until": cooldown_until,
        "cooldown_reason": cooldown_reason,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "fomo_whitelist": fomo_whitelist,
        "fomo_whitelist_source": wl.get("source"),
        "fomo_whitelist_file": wl.get("file"),
        "thresholds": {
            "revenge_losses": REVENGE_LOSS_THRESHOLD,
            "overtrade_warn": OVERTRADE_WARN,
            "overtrade_block": OVERTRADE_BLOCK,
        },
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Override-Tabelle (Schema + Insert-Helper)
# ---------------------------------------------------------------------------
DISCIPLINE_OVERRIDES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS discipline_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL,              -- COOLDOWN_OVERRIDE | FOMO_OVERRIDE | OVERTRADE_OVERRIDE
    ticker TEXT,
    reason TEXT,                     -- Pflicht-Freitext "warum bypass?"
    meta_json TEXT,
    state_snapshot_json TEXT         -- State zum Zeitpunkt des Overrides (für Retro-Analyse)
)
"""

VALID_KINDS = {
    "COOLDOWN_OVERRIDE",
    "FOMO_OVERRIDE",
    "OVERTRADE_OVERRIDE",
    "FIT_CHECK_OVERRIDE",
    "DAILY_LOSS_OVERRIDE",
}
# V7.5: FIT_CHECK_OVERRIDE ergaenzt den Discipline-Layer um die schwache Flanke
# des Fit-Checks: fit_score < 5 plus >=2 Red-Flags triggert kuenftig denselben
# Override-Dialog wie Guardrails (Cooldown/FOMO/Overtrade). Fuer das
# Gate-3-Audit wird die Kategorie separat gezaehlt, damit Fit-Bypass-Quoten
# nicht in COOLDOWN einlaufen.
# V7.6: DAILY_LOSS_OVERRIDE ist der Hardlock-Bypass. Wenn die heutige R-Summe
# <= -2R faellt, blockt das Dashboard jeden weiteren BUY. Override nur mit
# echtem Grund; Gate-4-Audit zaehlt Hardlock-Bypass separat.


def ensure_overrides(con: sqlite3.Connection) -> None:
    con.execute(DISCIPLINE_OVERRIDES_TABLE_SQL)
    con.commit()


def log_override(
    con: sqlite3.Connection,
    kind: str,
    reason: str,
    ticker: Optional[str] = None,
    meta: Optional[dict] = None,
    state_snapshot: Optional[dict] = None,
) -> int:
    """Protokolliert einen Override-Bypass. Return Row-ID."""
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid override kind: {kind}")
    if not reason or not reason.strip():
        raise ValueError("reason is required for override logging")
    ensure_overrides(con)
    cur = con.execute(
        """
        INSERT INTO discipline_overrides
            (ts_utc, kind, ticker, reason, meta_json, state_snapshot_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind,
            (ticker or "").upper().strip() or None,
            reason.strip(),
            json.dumps(meta or {}, ensure_ascii=False),
            json.dumps(state_snapshot or {}, ensure_ascii=False),
        ),
    )
    con.commit()
    return cur.lastrowid
