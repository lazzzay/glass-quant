#!/usr/bin/env python3
"""
X-1: Tax-Loss-Harvester Core (KW17 Rollout, Tag 1).

Proaktive Tax-Loss-Harvesting-Logik für das laufende deutsche Steuerjahr.

DISCLAIMER: NOT TAX ADVICE. Keine Steuerberatung. Die Logik ist eine vereinfachte,
informative Rechnung ueber das eigene Journal und kann falsch oder veraltet sein.
Vor jeder Entscheidung mit einer Steuerberatung pruefen.

Funktionen
----------
* Liest ``trade_journal.sqlite`` (cohort=all, geschlossene Positionen des Kalenderjahrs).
* Rechnet realisierten Gewinn/Verlust pro Steuer-Topf seit ``--year-start``:
    - Aktien-Topf  (Direkt-Käufe ohne Hebel)
    - Derivate-Topf (Hebel/Optionsscheine/Faktorzerts; Topf nach §20 Abs. 6 Satz 5 EStG).
      Der fruehere TFS-Verlustdeckel von 20.000 €/Jahr wurde Berichten zufolge durch das
      Jahressteuergesetz 2024 fuer alle offenen Faelle abgeschafft. Deshalb rechnet das
      Tool standardmaessig OHNE Deckel. Wer einen Deckel modellieren will, gibt ihn
      ausdruecklich an (--tfs-cap EUR). Rechtslage selbst pruefen.
* Identifiziert offene Positionen mit unrealisiertem Verlust > ``--loss-threshold``
  (Default 50 €) als Harvesting-Kandidaten.
* Gegenverrechnet Sparerpauschbetrag (Default 1 000 € Single) mit laufenden Gewinnen.
* Persistiert Output als JSON unter ``briefings/tax_loss_harvest_YYYY-MM-DD.json``.

Preis-Lookup für unrealisierten P&L
-----------------------------------
Sucht parquet-Dateien unter ``data_lake/prices/daily/{TICKER}.parquet`` (yfinance-Downloader)
und nimmt den letzten verfügbaren Close vor dem Stichtag. Fehlt der Preis, wird die
Position mit ``unrealized_pnl=null`` und ``warn="price_missing"`` geflaggt.

Free-Stack only: sqlite3, pandas, pyarrow (für parquet). Kein Paid-API, keine Netz-Calls.

"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "trade_journal.sqlite"
DEFAULT_PRICES_DIR = PROJECT_ROOT / "data_lake" / "prices" / "daily"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "briefings"

# Sparerpauschbetrag (ledig) 2023+ §20 Abs. 9 EStG.
DEFAULT_SPAREPAUSCHBETRAG_EUR = 1000.0
# Hebel-/Termingeschaefts-Topf Verlustdeckel: §20 Abs. 6 Satz 5 EStG (Stand vor
# dem Jahressteuergesetz 2024; Berichten zufolge abgeschafft, siehe Docstring).
# Default: KEIN Deckel (None). Nur mit explizitem --tfs-cap wird einer modelliert.
LEGACY_TFS_CAP_EUR = 20000.0
DEFAULT_TFS_CAP_EUR = None
# Mindestverlust für Harvesting-Kandidat: kleinere Positionen lohnen den 1€ Trade nicht.
DEFAULT_LOSS_THRESHOLD_EUR = 50.0

TAX_DISCLAIMER = ("Hinweis: keine Steuerberatung (not tax advice). Vereinfachte, "
                  "informative Rechnung; bitte mit einer Steuerberatung pruefen.")


# ---------------------------------------------------------------------------
# Topf-Routing
# ---------------------------------------------------------------------------
DERIVATE_ROUTE_MARKERS = {
    "KO", "OS", "OPTION", "OPTIONS", "FAKTOR", "WARRANT", "LEVERAGE",
    "KNOCKOUT", "KNOCK-OUT", "MINI-FUTURE", "MINI-FUTURES",
}


def classify_topf(row: dict[str, Any]) -> str:
    """Entscheidet Aktien- vs. Derivate-Topf aus Journal-Spalten.

    Prioritäten:
      1. ``route`` enthält Derivate-Marker → ``derivate``.
      2. ``hebel_factor`` gesetzt und ≠ 1 → ``derivate``.
      3. ``system_version`` enthält ``hebel`` / ``options`` → ``derivate``.
      4. sonst ``aktien``.
    """
    route = (row.get("route") or "").upper()
    if any(mark in route for mark in DERIVATE_ROUTE_MARKERS):
        return "derivate"
    hf = row.get("hebel_factor")
    if hf is not None and abs(float(hf) - 1.0) > 1e-9:
        return "derivate"
    sv = (row.get("system_version") or "").lower()
    if any(tok in sv for tok in ("hebel", "options", "faktor", "ko", "warrant")):
        return "derivate"
    return "aktien"


# ---------------------------------------------------------------------------
# Datenlader
# ---------------------------------------------------------------------------
def load_trades(
    db_path: Path,
    year_start: date,
    as_of: date,
    sources: tuple[str, ...] | None = ("live", "external"),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lädt geschlossene und offene Trades im Steuerjahr.

    Steuer-Relevanz: Default-Filter ``sources=('live','external')``, Backtest-Trades
    repräsentieren keine reale Kap-Erträge und würden den Pauschbetrag verfälschen.
    Übergib ``sources=None`` um alle Quellen einzubeziehen.

    Returns (closed_in_year, open_as_of).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"trade_journal.sqlite nicht gefunden: {db_path}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    if sources is None:
        src_clause = ""
        src_params: tuple = ()
    else:
        placeholders = ",".join("?" for _ in sources)
        src_clause = f" AND source IN ({placeholders})"
        src_params = tuple(sources)
    try:
        cur = con.cursor()
        # Geschlossene Positionen im Kalenderjahr (nach exit_date)
        cur.execute(
            f"""
            SELECT * FROM trades
            WHERE exit_date IS NOT NULL
              AND exit_date >= ?
              AND exit_date <= ?
              {src_clause}
            """,
            (year_start.isoformat(), as_of.isoformat(), *src_params),
        )
        closed = [dict(r) for r in cur.fetchall()]
        # Offene Positionen zum Stichtag (entry am/vor Stichtag, kein Exit)
        cur.execute(
            f"""
            SELECT * FROM trades
            WHERE exit_date IS NULL
              AND entry_date <= ?
              {src_clause}
            """,
            (as_of.isoformat(), *src_params),
        )
        open_pos = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()
    return closed, open_pos


def load_last_close(ticker: str, as_of: date, prices_dir: Path) -> float | None:
    """Lädt den letzten verfügbaren Close aus parquet (<= as_of).

    Importiert pandas lazily, damit das Modul in minimalen Test-Umgebungen ladbar bleibt.
    """
    parquet_path = prices_dir / f"{ticker}.parquet"
    if not parquet_path.exists():
        return None
    try:
        import pandas as pd  # lazy import
    except ImportError:  # pragma: no cover - Dev-Umgebung liefert pandas
        return None
    try:
        df = pd.read_parquet(parquet_path)
    except Exception:
        return None
    if df.empty:
        return None
    # Versuche Close-Spalte robust zu identifizieren.
    close_col = None
    for candidate in ("close", "Close", "adj_close", "Adj Close", "adjusted_close"):
        if candidate in df.columns:
            close_col = candidate
            break
    if close_col is None:
        return None
    # Datums-Index oder Datumsspalte
    if df.index.name and "date" in df.index.name.lower():
        df = df.reset_index()
    date_col = None
    for candidate in ("date", "Date", "datetime", "Datetime"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        # Index ist vermutlich DatetimeIndex
        try:
            import pandas as pd
            df = df.reset_index().rename(columns={df.index.name or "index": "date"})
            date_col = "date"
        except Exception:
            return None
    try:
        df[date_col] = __import__("pandas").to_datetime(df[date_col]).dt.date
    except Exception:
        return None
    df = df[df[date_col] <= as_of].sort_values(date_col)
    if df.empty:
        return None
    return float(df[close_col].iloc[-1])


# ---------------------------------------------------------------------------
# Kernlogik
# ---------------------------------------------------------------------------
@dataclass
class ToepfeResult:
    aktien_realized_eur: float = 0.0
    derivate_realized_eur: float = 0.0
    derivate_verluste_eur: float = 0.0  # nur negative Anteile (Teil des TFS-Deckels)
    trades_count_aktien: int = 0
    trades_count_derivate: int = 0

    @property
    def total_realized_eur(self) -> float:
        return self.aktien_realized_eur + self.derivate_realized_eur


@dataclass
class HarvestCandidate:
    ticker: str
    name: str | None
    topf: str
    entry_date: str
    entry_price: float | None
    current_price: float | None
    position_size_eur: float | None
    unrealized_pnl_eur: float | None
    warn: str | None = None


@dataclass
class HarvestReport:
    run_date: str
    year_start: str
    as_of: str
    spar_pauschbetrag_eur: float
    tfs_cap_eur: float | None   # None = kein Verlustdeckel modelliert (Default)
    loss_threshold_eur: float
    toepfe: dict[str, float]
    sparer_used_eur: float
    sparer_remaining_eur: float
    tfs_verlust_nutzbar_eur: float
    candidates: list[dict[str, Any]]
    actions: list[str]
    hook_trigger: bool  # True → Abend-Briefing soll Tax-Block zeigen
    disclaimer: str = TAX_DISCLAIMER


def compute_realized(closed_trades: list[dict[str, Any]]) -> ToepfeResult:
    """Aggregiert realisierten netto P&L pro Topf."""
    res = ToepfeResult()
    for row in closed_trades:
        topf = classify_topf(row)
        pnl = row.get("netto_pnl")
        if pnl is None:
            # Fallback: brutto - cost
            brutto = row.get("brutto_pnl")
            cost = row.get("cost") or 0.0
            if brutto is None:
                continue
            pnl = float(brutto) - float(cost)
        pnl = float(pnl)
        if topf == "aktien":
            res.aktien_realized_eur += pnl
            res.trades_count_aktien += 1
        else:
            res.derivate_realized_eur += pnl
            res.trades_count_derivate += 1
            if pnl < 0:
                res.derivate_verluste_eur += abs(pnl)
    return res


def find_candidates(
    open_trades: list[dict[str, Any]],
    prices_dir: Path,
    as_of: date,
    loss_threshold_eur: float,
) -> list[HarvestCandidate]:
    """Sucht offene Positionen mit unrealisiertem Verlust > threshold."""
    cands: list[HarvestCandidate] = []
    for row in open_trades:
        ticker = row.get("ticker")
        if not ticker:
            continue
        entry_price = row.get("entry_price")
        size_eur = row.get("position_size")
        if entry_price is None or size_eur in (None, 0):
            # ohne Entry-Preis oder Größe keine Bewertung möglich
            cands_maybe = HarvestCandidate(
                ticker=ticker,
                name=row.get("name"),
                topf=classify_topf(row),
                entry_date=row.get("entry_date") or "",
                entry_price=entry_price,
                current_price=None,
                position_size_eur=size_eur,
                unrealized_pnl_eur=None,
                warn="missing_entry_data",
            )
            # nur mitgeben, wenn Info potenziell relevant (z.B. Ticker + Entry fehlt)
            # Wir überspringen hier, keine Harvesting-Empfehlung ohne Daten.
            continue
        entry_price = float(entry_price)
        size_eur = float(size_eur)
        current = load_last_close(ticker, as_of, prices_dir)
        if current is None:
            cands.append(HarvestCandidate(
                ticker=ticker,
                name=row.get("name"),
                topf=classify_topf(row),
                entry_date=row.get("entry_date") or "",
                entry_price=entry_price,
                current_price=None,
                position_size_eur=size_eur,
                unrealized_pnl_eur=None,
                warn="price_missing",
            ))
            continue
        # Unrealized P&L in EUR: Ann ahme size_eur ist Einsatz in EUR (bei Aktien
        # EUR-Gegenwert), Hebel-Faktor verstärkt.
        # qty = size_eur / entry_price  (vereinfachend, Währung = EUR-nominal)
        if entry_price <= 0:
            continue
        qty = size_eur / entry_price
        hebel = row.get("hebel_factor") or 1.0
        try:
            hebel = float(hebel)
        except (TypeError, ValueError):
            hebel = 1.0
        unrealized = (current - entry_price) * qty * hebel
        if unrealized < -abs(loss_threshold_eur):
            cands.append(HarvestCandidate(
                ticker=ticker,
                name=row.get("name"),
                topf=classify_topf(row),
                entry_date=row.get("entry_date") or "",
                entry_price=entry_price,
                current_price=current,
                position_size_eur=size_eur,
                unrealized_pnl_eur=round(unrealized, 2),
            ))
    return cands


def build_report(
    closed: list[dict[str, Any]],
    open_pos: list[dict[str, Any]],
    *,
    year_start: date,
    as_of: date,
    spar_pauschbetrag: float,
    tfs_cap: float | None,
    loss_threshold: float,
    prices_dir: Path,
) -> HarvestReport:
    toepfe = compute_realized(closed)
    candidates = find_candidates(open_pos, prices_dir, as_of, loss_threshold)

    # Sparerpauschbetrag-Nutzung: nach deutschem Recht (§20 Abs. 9 EStG) auf die
    # Summe der positiven Kapitalerträge *vor* topfübergreifender Verlustverrechnung.
    # Verluste im Derivate-Topf bleiben als Verlustvortrag erhalten (bis TFS-Cap).
    positive_realized = max(toepfe.aktien_realized_eur, 0.0) + max(toepfe.derivate_realized_eur, 0.0)
    sparer_used = min(positive_realized, spar_pauschbetrag)
    sparer_remaining = max(spar_pauschbetrag - sparer_used, 0.0)

    # TFS-Deckel (nur wenn ausdruecklich gesetzt): nutzbarer Verlust im Derivate-Topf.
    # Ohne Deckel (Default, Rechtslage nach JStG 2024 laut Berichten) ist der volle
    # Derivate-Verlust verrechenbar.
    capped = tfs_cap is not None
    tfs_nutzbar = (min(toepfe.derivate_verluste_eur, tfs_cap) if capped
                   else toepfe.derivate_verluste_eur)

    actions: list[str] = []

    # Aktionen: nur die konkreten Steuer-Tipps
    if toepfe.total_realized_eur > spar_pauschbetrag and candidates:
        actions.append(
            f"Realisierte Gewinne {toepfe.total_realized_eur:+.2f} € übersteigen "
            f"Pauschbetrag {spar_pauschbetrag:.0f} €: Harvest-Kandidaten prüfen, "
            f"um steuerpflichtigen Anteil zu reduzieren."
        )
    if sparer_remaining > 0 and toepfe.total_realized_eur < spar_pauschbetrag:
        actions.append(
            f"Sparerpauschbetrag noch zu {sparer_remaining:.0f} € frei: "
            f"Gewinne mitnehmen bevor Jahresende ({as_of.year}-12-31), sonst verfällt der Freibetrag."
        )
    if capped and toepfe.derivate_verluste_eur > tfs_cap:
        actions.append(
            f"Derivate-Verluste {toepfe.derivate_verluste_eur:.2f} € überschreiten "
            f"TFS-Deckel {tfs_cap:.0f} €/Jahr: Übertrag ins Folgejahr notwendig."
        )
    if candidates:
        for c in candidates[:5]:
            actions.append(
                f"Harvest-Kandidat {c.ticker}: unrealisiert {c.unrealized_pnl_eur:+.2f} € "
                f"({c.topf}-Topf)."
            )

    actions.append(TAX_DISCLAIMER)

    hook_trigger = len(candidates) > 0 or (capped and toepfe.derivate_verluste_eur > tfs_cap)

    return HarvestReport(
        run_date=datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z") or datetime.now().isoformat(),
        year_start=year_start.isoformat(),
        as_of=as_of.isoformat(),
        spar_pauschbetrag_eur=spar_pauschbetrag,
        tfs_cap_eur=tfs_cap,
        loss_threshold_eur=loss_threshold,
        toepfe={
            "aktien_realized_eur": round(toepfe.aktien_realized_eur, 2),
            "derivate_realized_eur": round(toepfe.derivate_realized_eur, 2),
            "derivate_verluste_eur": round(toepfe.derivate_verluste_eur, 2),
            "total_realized_eur": round(toepfe.total_realized_eur, 2),
            "trades_count_aktien": toepfe.trades_count_aktien,
            "trades_count_derivate": toepfe.trades_count_derivate,
        },
        sparer_used_eur=round(sparer_used, 2),
        sparer_remaining_eur=round(sparer_remaining, 2),
        tfs_verlust_nutzbar_eur=round(tfs_nutzbar, 2),
        candidates=[asdict(c) for c in candidates],
        actions=actions,
        hook_trigger=hook_trigger,
    )


def write_report(report: HarvestReport, out_dir: Path, run_date: date) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"tax_loss_harvest_{run_date.isoformat()}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tax-Loss-Harvester DE (X-1). Not tax advice.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help="Pfad zu trade_journal.sqlite")
    p.add_argument("--prices-dir", type=Path, default=DEFAULT_PRICES_DIR,
                   help="Verzeichnis mit TICKER.parquet Dateien")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help="Ausgabeordner für JSON-Report")
    p.add_argument("--year-start", type=str, default=None,
                   help="Start des Steuerjahrs (YYYY-MM-DD), Default Jan-01 des aktuellen Jahres")
    p.add_argument("--as-of", type=str, default=None,
                   help="Stichtag (YYYY-MM-DD), Default heute")
    p.add_argument("--spar-pauschbetrag", type=float, default=DEFAULT_SPAREPAUSCHBETRAG_EUR)
    p.add_argument("--tfs-cap", type=float, default=DEFAULT_TFS_CAP_EUR,
                   help="Verlustdeckel Derivate-Topf in EUR. Default: kein Deckel, weil der "
                        "20.000-EUR-Deckel laut Berichten mit dem Jahressteuergesetz 2024 "
                        "entfallen ist. Nur setzen, wenn er fuer dich gilt (Rechtslage pruefen).")
    p.add_argument("--loss-threshold", type=float, default=DEFAULT_LOSS_THRESHOLD_EUR)
    p.add_argument("--sources", type=str, default="live,external",
                   help="Komma-Liste der Quellen (Default 'live,external'). "
                        "'all' für alle (inkl. backtest, nur für Debug/Analyse).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report auf stdout ohne Datei zu schreiben")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    today = date.today()
    as_of = date.fromisoformat(args.as_of) if args.as_of else today
    year_start = date.fromisoformat(args.year_start) if args.year_start else date(as_of.year, 1, 1)

    sources: tuple[str, ...] | None
    if args.sources.strip().lower() == "all":
        sources = None
    else:
        sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    closed, open_pos = load_trades(args.db, year_start, as_of, sources=sources)
    report = build_report(
        closed, open_pos,
        year_start=year_start,
        as_of=as_of,
        spar_pauschbetrag=args.spar_pauschbetrag,
        tfs_cap=args.tfs_cap,
        loss_threshold=args.loss_threshold,
        prices_dir=args.prices_dir,
    )

    if args.dry_run:
        print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
        return 0

    path = write_report(report, args.output_dir, today)
    print(f"[tax_loss_harvester] geschrieben: {path}")
    print(f"  toepfe: {report.toepfe}")
    print(f"  candidates: {len(report.candidates)} | hook_trigger: {report.hook_trigger}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
