# Data-Lake V1: Architektur-Spezifikation
**Phase A der V3.1-Backtest-Foundation | Start 2026-04-15 | Ziel: 30-Jahre-Historie (1996-2026)**

---

## 1. Zielbild

Ein reproduzierbarer, lokal gespeicherter **Parquet-basierter Data-Lake** als Single-Source-of-Truth für alle zukünftigen Backtests (Phase B + C). Keine Abhängigkeit von kostenpflichtigen APIs. Jede Datenquelle ist versioniert, zeitlich geflaggt und dokumentiert.

**Grundsätze:**
1. **Free-only**, kein Quiver, Polygon, Unusual Whales. Nur FRED, yfinance, SEC-EDGAR, CBOE-Public, Capitol-Trades-Public.
2. **Ehrlichkeit über Daten-Start**: jede Serie hat ein `first_date`-Feld. Kein Backfill mit synthetischen Daten.
3. **Reproduzierbarkeit**: jedes Download-Skript ist deterministisch. `download_manifest.json` protokolliert Zeitstempel + Hash.
4. **Survivorship-bias-frei, soweit möglich**: delistete Aktien (Wirecard, Lehman, Enron) werden mit in den Pool genommen, wenn Daten verfügbar.
5. **Point-in-Time-Korrektheit**: Fundamentals werden mit Release-Datum gespeichert, nicht Fiscal-Date (kein Look-Ahead).

---

## 2. Verzeichnisstruktur

```
data_lake/
├── manifest.json                         # Master-Index aller Serien
├── README.md                              # Schema + Nutzungshinweise
│
├── prices/
│   ├── daily/
│   │   ├── AAPL.parquet                   # Date, Open, High, Low, Close, Adj_Close, Volume, Dividends, Splits
│   │   ├── LMT.parquet
│   │   └── ...                            # 150+ Tickers
│   └── intraday_h1/                       # nur letzte 3y (yfinance-Limit)
│       └── ...
│
├── macro_fred/
│   ├── HY_OAS.parquet                     # BAMLH0A0HYM2, seit 1996-12-31
│   ├── IG_OAS.parquet                     # BAMLC0A0CM
│   ├── T10Y3M.parquet                     # seit 1982
│   ├── T10Y2Y.parquet
│   ├── VIXCLS.parquet                     # VIX seit 1990
│   ├── DGS10.parquet                      # 10Y Treasury
│   ├── DGS2.parquet                       # 2Y Treasury
│   ├── DFF.parquet                        # Fed Funds Rate
│   ├── UNRATE.parquet                     # Unemployment
│   ├── CPIAUCSL.parquet                   # CPI
│   ├── PCEPI.parquet                      # PCE Price Index
│   ├── DEXUSEU.parquet                    # USD/EUR
│   ├── DCOILWTICO.parquet                 # WTI Crude
│   └── ... (40+ Serien, siehe fred_series.yaml)
│
├── macro_other/
│   ├── MOVE.parquet                       # via yfinance ^MOVE, seit 2002
│   ├── VVIX.parquet                       # ^VVIX, seit 2006
│   ├── DXY.parquet                        # ^DXY
│   └── fed_netliq_h41.parquet             # Fed NetLiq, seit 2009
│
├── sentiment/
│   ├── aaii_weekly.parquet                # AAII Bull/Bear seit 1987
│   ├── naaim_weekly.parquet               # NAAIM Exposure seit 2006
│   ├── cboe_pcr_daily.parquet             # Put/Call Ratio seit 2003
│   └── fear_greed_daily.parquet           # CNN Fear&Greed (ab 2011)
│
├── insider_sec_edgar/
│   ├── form4_aggregated.parquet           # seit 2003, aggregiert pro Ticker/Datum
│   └── form4_raw/                         # XML-Rohdateien (optional)
│
├── congress_capitol_trades/
│   ├── trades_2013_to_2026.parquet        # STOCK-Act-konform, seit 2013
│   └── members.parquet                    # Members-Metadata
│
├── fundamentals_yf/                       # via yfinance .financials etc.
│   ├── AAPL_quarterly.parquet
│   └── ...
│
├── universe/
│   ├── sp500_constituents_history.parquet # historische Membership
│   ├── nasdaq100_constituents_history.parquet
│   ├── watchlist_v5_snapshot_2026-04-15.parquet
│   └── delisted_tickers.parquet           # Survivorship-Pool
│
└── logs/
    ├── download_2026-04-15.log
    └── dq_check_2026-04-15.log
```

---

## 3. Daten-Start-Tabelle (ehrlich)

| Quelle | Serie | Echter Daten-Start | 30y-Abdeckung |
|---|---|---|---|
| yfinance | US-Aktien Preis | 1990 ff. | ✅ |
| yfinance | Dividenden/Splits | 1990 ff. | ✅ |
| yfinance | ^VIX | 1990-01-02 | ✅ |
| yfinance | ^MOVE | 2002-04-05 | ⚠️ 24y |
| yfinance | ^VVIX | 2006-03-13 | ⚠️ 20y |
| FRED | T10Y3M, T10Y2Y | 1982 | ✅ |
| FRED | HY-OAS (BAMLH0A0HYM2) | **1996-12-31** | ⚠️ 29.3y |
| FRED | IG-OAS (BAMLC0A0CM) | **1996-12-31** | ⚠️ 29.3y |
| FRED | DGS10, DGS2, DFF | 1962 | ✅ |
| FRED | CPIAUCSL | 1947 | ✅ |
| FRED | DEXUSEU | 1999 | ⚠️ 27y |
| FRED | H.4.1 NetLiq (manuell zusammengesetzt) | **2009** | ⚠️ 17y |
| AAII | Bull/Bear Survey | 1987-07-24 | ✅ |
| NAAIM | Exposure Index | 2006-07-05 | ⚠️ 20y |
| CBOE | Equity Put/Call Ratio | 2003-10-17 | ⚠️ 23y |
| SEC EDGAR | Form-4 (Insider) | **2003-06-30** | ⚠️ 23y |
| Capitol Trades | Congress-Trades | 2013 (STOCK Act) | ⚠️ 13y |

**Konsequenz für Backtests:** Pro Overlay wird ein **Test-Fenster** definiert, das spätestens mit seiner kürzesten Eingangsserie beginnt. Keine synthetischen Backfills.

---

## 4. Manifest-Schema (`manifest.json`)

```json
{
  "version": "1.0",
  "created": "2026-04-15T00:00:00Z",
  "last_updated": "2026-04-15T00:00:00Z",
  "series": [
    {
      "id": "HY_OAS",
      "source": "FRED",
      "source_id": "BAMLH0A0HYM2",
      "path": "macro_fred/HY_OAS.parquet",
      "first_date": "1996-12-31",
      "last_date": "2026-04-14",
      "frequency": "daily",
      "unit": "percent",
      "rows": 7412,
      "sha256": "...",
      "download_utc": "2026-04-15T06:30:00Z"
    }
  ]
}
```

Jede neue Datenquelle wird nach dem Download ins Manifest geschrieben.

---

## 5. Data-Quality-Checks (vor Freigabe einer Serie)

Jedes Download-Skript ruft nach Abschluss den `dq_check.py` auf, der folgende Prüfungen fährt:

1. **Lücken-Check:** Keine Lücke > 5 Handelstage (Ausnahme: regulär bekannte Feiertage/Thanksgiving-Clusters)
2. **NaN-Rate:** < 0.5% der Datenpunkte dürfen NaN sein
3. **Stale-Check:** Letzter Datenpunkt ≤ 5 Business-Days alt (für laufende Serien)
4. **Outlier-Check:** Kein Tagesschritt > 6σ vom 30d-Rolling-Mean (Flag, nicht Abbruch)
5. **Monoton-Datum:** Index ist strikt aufsteigend, keine Duplikate
6. **Split/Dividend-Sanity:** Adj_Close und Close weichen nicht um > Faktor 10 voneinander ab

Ergebnis: `logs/dq_check_YYYY-MM-DD.log` mit PASS/WARN/FAIL je Serie.

---

## 6. Universe-Definition (Survivorship-Pool)

Für faire Backtests brauchen wir die **historisch existierenden** S&P500/Nasdaq100-Member, nicht die heutigen. Sonst overfitten wir auf Überlebende.

**Approach:**
- **S&P500-History:** Wikipedia-Scrape der "List of S&P 500 companies" + "Historical changes" → rekonstruiert Membership pro Tag
- **Nasdaq100-History:** Wikipedia + WebArchive-Snapshots
- **Delisted Pool (bekannte Problemfälle):** manuelle Liste mit Wirecard, Lehman, Enron, Bear Stearns, WorldCom, AIG-Phasen, GM alte, Chrysler alte, Kodak, Blockbuster, Sears, Toys'R'Us etc. → yfinance-Download (viele haben Tickers noch verfügbar)
- **Watchlist V5 (aktuell 110+):** wird als separater Pool geführt für das "Heute-System"

---

## 7. Nutzungsbedingungen der Datenquellen

Jeder Anbieter hat eigene Nutzungsbedingungen, zum Beispiel FRED (und die Rechteinhaber einzelner Serien wie ICE BofA), Yahoo Finance über yfinance, SEC EDGAR, Capitol Trades, AAII, NAAIM, CBOE und Wikipedia. Diese Spezifikation hat sie nicht geprüft und sichert nicht zu, dass ein Download, eine Speicherung oder eine Weitergabe erlaubt ist. Prüfe die aktuellen Bedingungen jedes Anbieters selbst, bevor du Daten herunterlädst, speicherst oder weitergibst. Heruntergeladene Marktdaten gehören nie ins Repository (siehe `.gitignore`). Nichts hier ist eine Rechtsberatung. Siehe auch [DISCLAIMER.md](DISCLAIMER.md).

Technische Hinweise, keine Rechtsauskunft: SEC EDGAR verlangt einen User-Agent mit Kontaktangabe (`SEC_USER_AGENT` in `.env.example`) und begrenzt die Abfragerate. Scraper bleiben opt-in und respektieren `robots.txt`. Die historischen CBOE-Put/Call-Daten sind laut `Data-Lake-Phase-A-Report.md` seit 2024 nicht mehr frei verfügbar.

---

## 8. Ausführungsreihenfolge Phase A

1. ✅ **Spec dieses Dokument** (fertig)
2. Verzeichnisstruktur anlegen + leeres Manifest
3. FRED-Downloader (40+ Serien, ~30 Min Runtime)
4. yfinance-Downloader (150 Tickers × 30y, ~60 Min Runtime)
5. Sentiment-Downloader (AAII, NAAIM, PCR)
6. SEC-EDGAR Form-4 Aggregator (Top-200-Tickers, ab 2003)
7. Capitol Trades Scraper (ab 2013)
8. Universe-Rekonstruktion (S&P500-History)
9. DQ-Check-Run über alles
10. Manifest finalisieren + Phase-A-Abschlussbericht

**Geschätzte Gesamtzeit:** ~8-10 Stunden Rechenzeit verteilt auf 2 Wochen (Rate-Limits respektiert).

---

## 9. Was Phase A **nicht** liefert

- Keine Intraday-Tick-Daten (yfinance liefert nur Minute-Data für letzte 7 Tage, H1 für 730 Tage: reicht für V6.6 Multi-Timeframe-Overlay)
- Keine granularen Options-Chains historisch (CBOE liefert nur aggregiertes PCR free, nicht Strike-level)
- Keine echte Alternative-Data (Satelliten, Credit-Card-Panels etc.): Kosten-Veto
- Keine Fundamentals pre-2006 (yfinance-Limit, SEC-EDGAR-XBRL startet ~2009 richtig)

Diese Lücken werden im Backtest-Design berücksichtigt (Test-Fenster-Definition).

---

*Diese Spec ist Grundlage für alle folgenden Download-Skripte in Phase A. Bei Anpassungen Versionsnummer hochziehen und im Manifest dokumentieren.*
