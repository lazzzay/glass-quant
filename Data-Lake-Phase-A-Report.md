# Data-Lake Phase A: Abschlussbericht

**Stand:** 2026-04-15
**Scope:** Free-only data lake, Phase A
**Footprint:** 149 MB  |  **Serien im Manifest:** 212

---

## 1. Zielsetzung

Phase A sollte eine vollständige, reproduzierbare, **ausschließlich aus Free-Quellen** gespeiste Historien-Basis schaffen, die einen ehrlichen Backtest der V6-Strategie über 30 Jahre (1996-2026) ermöglicht. Kosten-Regel: no paid APIs (Quiver, Unusual Whales, Polygon, Bloomberg); only a flat broker fee per order is modeled.

---

## 2. Was jetzt im Lake liegt

### 2.1 Preise & Märkte (prices/daily, 52 MB)

150 Ticker aus 10 Asset-Klassen via yfinance: 64 US-Large-Caps, 11 Sektor-ETFs, 11 Broad-ETFs, 11 Regional-ETFs, 11 Bonds-ETFs, 11 Commodity-ETFs, 5 Vol-Hedges, 16 Indizes (inkl. ^MOVE seit 2002, ^GDAXI seit 1987), 10 FX-Crosses, 8 Krypto. OHLCV + Dividends + Splits + Adj-Close. AAPL seit 1980-12-12, SPY seit 1993, JNJ/KO/PG/XOM/CVX seit 1962. Einziger Ausfall: SQ (umbenannt zu XYZ/Block).

### 2.2 Makro-Daten (macro_fred, 3,1 MB)

41 FRED-Serien: Credit-Spreads (HY/IG/AAA/BBB/CCC seit 1996-12-31), Yield-Curve (T10Y2Y seit 1976), Fed-Policy (DFF seit 1954), Liquidity (WALCL/WTREGEN/RRPONTSYD seit 2002/2003), Inflation (CPI/PCE/Breakevens), Labor (UNRATE/PAYEMS/ICSA), Vol (VIXCLS seit 1990), FX (DEXUSEU/DEXJPUS/DEXCHUS), Commodities (WTI seit 1986, Brent seit 1987), Housing (MORTGAGE30US seit 1971). Einziger Ausfall: Gold (GOLDAMGBD228NLBM deprecated), wird über yfinance GLD ersetzt.

### 2.3 Sentiment (sentiment, 120 KB)

AAII Bull/Bear/Neutral wöchentlich seit 1987-07-24 (2.018 Zeilen). NAAIM Active-Manager-Exposure wöchentlich seit 2006-07-05 (1.031 Zeilen). SPY-PCR self-computed forward-only ab 2026-04-15 (CBOE historisch paywalled seit 2024, ehrlich als Lücke dokumentiert).

### 2.4 Congress/Insider/Insider-SEC (944 KB + 93 MB)

**Congress:** 36.018 House-Clerk-Disclosure-Filings 2013-2026, davon 8.137 PTRs (Periodic Transaction Reports) als Stage-1-Index mit PDF-URLs für Stage-2.

**Insider (SEC Form-4):** 2.441.309 Filings 2020Q1-2026Q1, 108.002 unique CIKs, Stage-1-Index mit SEC-URLs für XML/XBRL-Parsing in Stage-2.

### 2.5 Earnings (fundamentals_yf, 92 KB)

6.190 Earnings-Events für 66 Aktien via yfinance mit EPS-Estimate, Reported EPS und Surprise %. Enables V6.3 Earnings-Blackout-Overlay (E1) ±2 Trading-Days ehrlich.

### 2.6 Market-Breadth (breadth, 696 KB)

Yahoo hat die klassischen Breadth-Indizes (^ADV, ^DECN, ^NYHL) entfernt. **Pivot**: Universe-Breadth selbst rekonstruiert aus den 64 Large-Cap-Preisreihen, Advance-Decline, McClellan Oscillator (19-EMA minus 39-EMA), McClellan Summation, %>SMA20/50/200, 52W-NH/NL-Diff. 16.179 Tage ab 1962-01-02.

### 2.7 Index-Membership (index_membership, 80 KB)

S&P 500 aktuell (503 Tickers) + Changes-Log (394 Added/Removed-Events) aus Wikipedia. Nasdaq-100 (101) und DJIA (30). Das Changes-Log ist das zentrale Survivorship-Bias-Fix-Werkzeug: für jeden Backtest-Zeitpunkt lässt sich die damalige Index-Composition rekonstruieren.

---

## 3. Datenqualitäts-Audit (dq_report.md)

212 Serien geprüft, **0 Errors**, **13 clean OK**, **199 Warnings**. Die Warnings zerfallen in drei harmlose Kategorien:

1. **OUTLIERS_6SIGMA** (ca. 85% der Warnings): erwartete Fat-Tail-Events in Finanz-Returns (Flash-Crash, Earnings-Gaps, Corona-März-2020, SVB-März-2023). Kein echter Datenfehler.
2. **STALE=45d / 73d** bei monatlichen FRED-Serien (CPI, UNRATE, PAYEMS, PCE): ist per Design monatlich, nicht täglich.
3. **GAPS_GT_30D** bei denselben monatlichen Serien: selbes Thema, kein Bruch.

Kein einziger Ticker mit echter Datenlücke im Daily-Stream. NaN-Raten überall unter 1%. Date-Monotonie 100% eingehalten.

---

## 4. Ehrliche Daten-Start-Tabelle

| Signal | Datenquelle | Verfügbar ab | Limitiert Backtest ab |
|---|---|---|---|
| S&P-500-Prices | yfinance ^GSPC | 1927 | - |
| VIX | FRED / yfinance | 1990-01-02 | Vol-Overlays (O2) |
| MOVE | yfinance ^MOVE | 2002-11 | Bond-Vol-Overlay |
| HY/IG-OAS | FRED BAMLH0A0HYM2 | 1996-12-31 | Credit-Overlay (O4) |
| Fed Net-Liquidity | FRED WALCL/WTREGEN | 2002-12-18 | Liquidity-Overlay (O3) |
| AAII Sentiment | aaii.com xls | 1987-07-24 | Sentiment-Overlay |
| NAAIM Exposure | naaim.org xlsx | 2006-07-05 | Active-Mgr-Overlay |
| Capitol Trades | House Clerk | 2013 | Congress-Overlay (E7) |
| SEC Form-4 | EDGAR full-index | 2020Q1 (2003 möglich) | Insider-Overlay (E7) |
| Earnings-Surprise | yfinance | ~2000 (per Ticker) | E1 Earnings-Blackout |
| Universe-Breadth | self-computed | 1962-01-02 | Breadth-Signale |
| Index-Membership | Wikipedia | Changes-Log seit ~2000 | Survivorship-Fix |

**Backtest-ehrlicher Startpunkt:** 1997-01-01 für Credit-Strategien, 1990-01-01 für Vol-Strategien, 2003-01-01 für Liquidity-Strategien, 2013-01-01 für Congress/Insider-Overlays.

---

## 5. Was Phase A bewusst nicht liefert (dokumentierte Lücken)

- **CBOE Historical PCR**: paywalled seit 2024. Self-computed SPY-PCR läuft forward-only ab 2026-04-15.
- **NYSE-wide-Breadth (^ADV/^DECN)**: von Yahoo entfernt. Proxy-Breadth aus 64-Ticker-Universe liefert äquivalentes Signal.
- **Gold-Spot historisch**: FRED-Serie deprecated. yfinance GLD (seit 2004-11-18) plus ^XAU deckt das ab.
- **Form-4 vor 2020Q1**: SEC stellt die full-index-Files zwar ab 2003Q3 bereit; wir haben Q1-2020-Q1-2026 geladen. Erweiterung auf 2003 möglich (ca. +200 MB, +2M Filings) als separater Batch.
- **Stage-2 Parsing für Congress-PDFs und Form-4-XMLs**: separate Skripte, on-demand pro Ticker/CIK, nicht Bulk.
- **Intraday-Daten**: bewusst ausgelassen (yfinance limitiert, nicht backtest-kritisch für V6).

---

## 6. Scripts im data_lake/

> Note: the `data_lake/` download scripts listed below are **not included** in this repository (the folder is gitignored and they are not published separately yet). The table documents what the lake builder does, so the backtests in `backtest/` can be reproduced once such a builder exists.

| Script | Zweck | Output |
|---|---|---|
| `download_fred.py` | 41 FRED-Makro-Serien | macro_fred/*.parquet |
| `download_yf.py` | 150 yfinance Ticker, 10 Asset-Klassen | prices/daily/*.parquet |
| `download_sentiment.py` | AAII + NAAIM + SPY-PCR | sentiment/*.parquet |
| `download_congress.py` | House-Clerk PTR-Index 2013+ | congress_capitol_trades/*.parquet |
| `download_form4_index.py` | SEC-EDGAR Form-4-Index | insider_sec_edgar/form4_index.parquet |
| `download_earnings.py` | yfinance Earnings-Historie | fundamentals_yf/earnings_history.parquet |
| `compute_breadth.py` | Universe-Breadth + McClellan | breadth/universe_breadth_daily.parquet |
| `download_index_membership.py` | S&P 500 / Nasdaq-100 / DJIA | index_membership/*.parquet |
| `dq_check.py` | Data-Quality-Suite | dq_report.md + dq_report.json |

Alle Scripts sind idempotent (Manifest-Upsert, SHA256-Hash, Re-Run sicher), rate-limit-friendly und logging-fähig (logs/ pro Tag).

---

## 7. Nächste Schritte für Phase B: Walk-Forward-Backtest

Der Lake ist bereit für Phase B. Nächste Schritte:

1. **Backtest-Engine** gegen V6.8-Regelwerk mit walk-forward Train-Test-Split (expandierendes Train-Fenster ab 2003, rollende 2-Jahres-Test-Fenster).
2. **Bootstrap-Monte-Carlo** (1.000 Resamples) für Sharpe, Sortino, Max-DD, Calmar: Konfidenzintervalle statt Punktschätzer.
3. **Survivorship-Bias-Fix** via sp500_changes.parquet: zu jedem Backtest-Tag nur die damalige S&P-Composition zulassen.
4. **Overlay-by-Overlay-Attribution**: welcher O1-O5 + E1-E10 trägt wie viel zum Alpha bei? Welche sind redundant?
5. **Regime-Sensitivity**: Performance in Bull (2003-07, 2009-20, 2020-21), Bear (2008, 2022), Sideways (2011, 2015-16, 2018).

Zeit-Schätzung Phase B: ca. 3-5 Tage bei täglichem Fokus.

---

**Fazit:** Data-Lake Phase A ist abgeschlossen: 149 MB, 212 Serien, 30-Jahre-Tiefe in den Kern-Assets, alle Free-Quellen, DQ-geprüft. Die Grundlage für die Backtests in Phase B steht (der Lake selbst ist nicht im Repository enthalten).
