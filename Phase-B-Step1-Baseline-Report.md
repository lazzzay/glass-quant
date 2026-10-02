# Phase B Schritt 1: Baseline-Backtest auf dem neuen Data-Lake

> **Hypothetische Forschungsergebnisse, keine Anlageberatung und keine Anlageempfehlung.** Alle Kennzahlen stammen aus Backtests auf 66 US-Large-Caps (Survivorship-Bias: nur heute noch existierende Werte), mit im Rückblick gewählten Parametern, pauschal 1 USD Kosten pro Trade und ohne Live-Track-Record. Sizing: 1 % Konto-Risiko je Trade, höchstens 10 offene Positionen, 100.000 USD Startkapital. Die Daten sind nicht im Repository enthalten. Die Zahlen sind keine Prognose.
>
> **Hypothetical research results, not investment advice and not a recommendation.** All figures come from backtests on 66 US large caps (survivorship bias: only names that still exist today), with parameters chosen in hindsight, a flat cost of 1 USD per trade and no live track record. Sizing: 1 % account risk per trade, at most 10 open positions, 100,000 USD starting capital. The data is not included in this repository. The numbers are not a forecast.

**Stand:** 2026-04-15
**Universe:** 66 US-Large-Caps (stocks_us_large group)
**Zeitraum:** 1997-01-01 bis 2026-04-15  (**29,3 Jahre**)
**Startkapital:** 100.000 USD
**Gebühr:** 1 USD / Trade (flat fee assumption)

---

## 1. Strategie V6-Baseline

Mean-Reversion-Pullback mit klassischer Connors-Logik:

- **Entry Long:** Close > SMA200 **und** 2-period RSI < 25 **und** Tagesverlust > −5 %
- **Exit Long:** Close > SMA5 *oder* ATR(10) × 2.5 Trailing-Stop *oder* 10-Tage-Time-Stop
- **Risk-Sizing:** 1 % Account-Risk pro Trade, max 10 offene Positionen
- **Walk-Forward-OOS-Start:** 1997-01-01 (pragmatisch fix-parametrisiert, vollständiger Walk-Forward-Train kommt in Schritt 2)

---

## 2. Kennzahlen (29,3 Jahre)

| Metric | Wert | Bootstrap CI95 (N=1.000) |
|---|---:|---|
| CAGR | **7,10 %** | [4,95 %, 9,42 %] |
| Sharpe | **1,25** | [0,89, 1,63] |
| Sortino | 1,77 | - |
| Max Drawdown | **−8,62 %** | [−16,3 %, −6,9 %] |
| Calmar | 0,82 | [0,37, 1,24] |
| Trades | 14.646 | - |
| Win-Rate | **68,1 %** | - |
| Profit-Factor | 1,52 | - |
| Avg Win / Avg Loss | +2,1 % / −3,0 % | - |

Das 95-%-Bootstrap-Konfidenzintervall des Sharpe reicht von 0,89 bis 1,63. Das ist ein Intervall aus dem Resampling dieser einen historischen Stichprobe, keine Wahrscheinlichkeitsaussage über den "wahren" Sharpe und kein Beleg für zukünftige Erträge. Survivorship-Bias und im Rückblick gewählte Parameter stecken im Intervall nicht mit drin.

---

## 3. Regime-Analyse (Forschungsnotiz)

SPY-Werte: Kalenderjahresrendite aus SPY `adj_close` (inkl. Dividenden), berechnet in `backtest/bootstrap_and_regime.py`. Die Differenz ist rein beschreibend.

| Regime | Jahre | Strat-Return Ø | SPY-Return Ø | Differenz (Strategie minus SPY) |
|---|---:|---:|---:|---:|
| **BEAR** (SPY < −10 %) | 4 | **−0,2 %** | −22,1 % | **+21,9 %** |
| **SIDEWAYS** | 7 | +5,9 % | +0,2 % | +5,7 % |
| **BULL** (SPY > +10 %) | 19 | +9,2 % | +22,2 % | −13,0 % |

Beobachtung im hypothetischen Backtest: Die Baseline verhielt sich in Bärenjahren defensiv und blieb in Bullenjahren deutlich hinter SPY zurück, weil die Entry-Bedingung „RSI < 25" in Trend-Märkten selten triggert und das Kapital dann in Cash liegt.

Einzeljahre (hypothetisch, SPY inkl. Dividenden):

- **2008:** Strategie −3,0 %, SPY −36,8 %
- **2022:** Strategie −0,7 %, SPY −18,2 %
- **2002:** Strategie +2,8 %, SPY −21,6 %

---

## 4. Forschungsnotiz: nächste Tests

Die Baseline ist der Ausgangspunkt für die Overlay-Attribution. Zwei Forschungsrichtungen:

1. **Blend-Test:** Wie verhält sich eine Mischung aus Baseline und SPY in diesem hypothetischen Backtest? (Ausgewertet im Phase-B-Abschlussbericht.)

2. **Overlay-Test:** Overlays O1-O5 + E1-E10 gezielt so kombinieren, dass die Bull-Market-Capture steigt, ohne die Bear-Protection zu verlieren. Kern-Kandidaten aus dem Data-Lake:
   - **O3 Liquidity** (WALCL-WTREGEN-RRPONTSYD): Risk-On-Filter → Bull-Market-Position-Size hochskalieren
   - **O4 Credit** (HY-OAS): Risk-Off-Filter → Bear-Market-Schutz härten
   - **E5 Sektor-RS-Matrix**: Ticker-Ranking statt RSI-Sortierung
   - **E8 Multi-Timeframe**: Weekly-Trend als zweiten Filter zu SMA200

---

## 5. Artefakte

- `backtest/results_v1/equity_daily.parquet`: 7.367 Equity-Tage
- `backtest/results_v1/trades.parquet`: 14.646 Trades
- `backtest/results_v1/summary.json`: Kennzahlen
- `backtest/results_v1/bootstrap_regime_report.json`: Bootstrap + Regime
- `backtest/results_v1/yearly_performance.parquet`: 30 Jahreszeilen
- `backtest/results_v1/regime_attribution.parquet`: Bull/Bear/Sideways-Aggregat
- `backtest/results_v1/equity_and_dd.png`: Equity vs SPY + Drawdown-Chart
- `backtest/results_v1/bootstrap_histograms.png`: 4 Metric-Verteilungen

---

## 6. Nächster Forschungsschritt

Overlay-Attribution. Genau eine Änderung nach der anderen hinzufügen und die ΔCAGR / ΔSharpe / ΔMaxDD messen, um redundante von echten Alpha-Treibern zu trennen. Vorschlag Reihenfolge:

1. O3 Liquidity-Filter → Positions-Size-Tilt
2. O4 HY-OAS-Stop → Crash-Schutz
3. E8 Weekly-Trend → Entry-Verschärfung
4. E1 Earnings-Blackout → Trade-Quality
5. E7 Congress/Insider → Ticker-Boost
6. E5 Sektor-RS → Ticker-Selection

Nach jedem Overlay ein Ergebnis-Block mit Bootstrap-CI. Redundante Overlays fliegen raus. Das Ziel: klare Attribution statt „alles rein und hoffen".
