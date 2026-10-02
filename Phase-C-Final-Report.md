# Phase C Abschlussbericht: Robustness-Checks & Circuit-Breaker

> **Hypothetische Forschungsergebnisse, keine Anlageberatung und keine Anlageempfehlung.** Alle Kennzahlen stammen aus Backtests auf 66 US-Large-Caps (C-1, C-2, C-4) und nationale Indizes bzw. Länder-ETFs (C-3) (Survivorship-Bias: nur heute noch existierende Werte), mit im Rückblick gewählten Parametern, pauschal 1 USD Kosten pro Trade und ohne Live-Track-Record. Sizing: 1 % Konto-Risiko je Trade auf 100.000 USD, höchstens 10 offene Positionen. Die Daten sind nicht im Repository enthalten. Die Zahlen sind keine Prognose.
>
> **Hypothetical research results, not investment advice and not a recommendation.** All figures come from backtests on 66 US large caps (C-1, C-2, C-4) and national indices or country ETFs (C-3) (survivorship bias: only names that still exist today), with parameters chosen in hindsight, a flat cost of 1 USD per trade and no live track record. Sizing: 1 % account risk per trade on 100,000 USD, at most 10 open positions. The data is not included in this repository. The numbers are not a forecast.

**Stand:** 2026-04-15
**Basis:** MR-Baseline-V6 (1997-2026) aus Phase B
**Vier Robustness-Checks parallel gelaufen:**

---

## Executive Summary

Phase C hat vier Robustheits-Checks auf den hypothetischen Backtests gerechnet. Keiner hat einen zusätzlichen Komplexitäts-Layer gerechtfertigt. Die bekannten Verzerrungen (Survivorship-Bias, im Rückblick gewählte Parameter) bleiben bestehen.

1. **Walk-Forward (C-1): kein Hinweis auf starke Überanpassung in diesem einen Split.** OOS-Sharpe ≥ IS-Sharpe. Ein einzelner IS/OOS-Split auf denselben 66 Überlebenden schließt Überanpassung aber nicht aus. (RSI=23, ATR=2,0) war in-sample etwas besser.

2. **Survivorship-Check (C-2): messbarer Bias klein, struktureller Bias offen.** Mit Index-Membership-Filter fällt die CAGR um 0,31 %-Punkte, der Sharpe von 1,25 auf 1,14. Delistete Werte fehlen weiterhin im Datenbestand.

3. **International (C-3): niedrige Korrelation (0,05-0,33) zur US-Baseline, aber zu wenige Trades.** Single-Asset-MR auf DAX/Nikkei/FTSE löste zu selten aus, um allein zu bestehen. Die niedrige Korrelation ist eine Beobachtung in diesem hypothetischen Backtest, kein Beleg für einen Diversifikationsnutzen.

4. **Circuit-Breaker (C-4): keiner brachte eine messbare Verbesserung.** Die SMA200-Bedingung der Baseline hielt die Strategie in den Crash-Jahren dieses Backtests weitgehend aus dem Markt.

---

## Weg C-1: Walk-Forward-Parametertuning

**Methode:** IS = 1997-2012 (16 J.) / OOS = 2013-2026 (13 J.). Parameter-Grid: RSI_ENTRY × ATR_MULT = 5 × 3 = 15 Kombis.

### In-Sample-Grid (Top 3 nach Sharpe)

| RSI | ATR | CAGR | Sharpe | Max DD |
|---:|---:|---:|---:|---:|
| **23** | **2,0** | 10,49 % | **1,394** | −9,73 % |
| 23 | 2,5 | 9,20 % | 1,366 | −8,74 % |
| 23 | 3,0 | 8,34 % | 1,366 | −8,03 % |
| *25* | *2,5* | *9,15 %* | *1,341* | *−8,62 %* | ← Baseline

### OOS-Validierung (Best-IS vs Baseline)

| Strategie | OOS-CAGR | OOS-Sharpe | OOS-DD | Degradation-Ratio |
|---|---:|---:|---:|---:|
| Best-IS (23/2,0) | 13,81 % | 1,45 | −9,05 % | 1,04 |
| Baseline (25/2,5) | 11,66 % | 1,286 | −8,73 % | 0,96 |

**Interpretation:** Eine Ratio OOS/IS nahe oder über 1,0 ist ein gutes Zeichen für diesen einen Split, aber kein Beweis gegen Overfitting: Universe, Strategie-Idee und Parameterraster wurden mit Wissen über die gesamte Historie gewählt.

**Nächster Test:** RSI=23 / ATR=2,0 gegen die Baseline auf neuen, noch nicht gesehenen Daten prüfen (Forward-Test, zum Beispiel im Paper-Modus). Der In-Sample-Vorteil kann Zufall sein.

---

## Weg C-2: Survivorship-Bias-Check

**Methode:** Für jeden der 66 US-Large-Caps wurde das Eintrittsdatum in den S&P 500 ermittelt. Variante "MIT Filter" blockiert Trades, die vor dem Eintrittsdatum stattfinden würden.

### Ergebnis 1997-2026

| Variante | CAGR | Sharpe | Max DD | Trades | Geblockt |
|---|---:|---:|---:|---:|---:|
| OHNE Filter (Status quo) | 7,10 % | 1,249 | −8,62 % | 14 646 | - |
| **MIT Filter** | **6,79 %** | **1,139** | **−10,44 %** | 14 255 | 2 555 |

**Interpretation:** Der *messbare* Bias ist klein (−0,31 % CAGR). Aber: Die strukturelle Einschränkung bleibt, wir haben nur die heute-überlebenden 66 Werte. Lehman Brothers, Bear Stearns, GM, Enron etc. sind nicht im Datenbestand.

**Forschungsnotiz:** Ein vollständiger Fix würde den historischen Download delisteter Ticker erfordern (aufwändig, aber machbar via Wikipedia-Liste `sp500_changes.parquet` + Yahoo-Historie). Bis dahin sind alle Kennzahlen nach oben verzerrt.

---

## Weg C-3: International Diversification

**Methode:** Gleiche MR-Baseline, aber single-Asset auf nationale Benchmarks.

### Single-Asset-Ergebnisse

| Asset | Typ | CAGR | Sharpe | Max DD | corr vs US-Baseline |
|---|---|---:|---:|---:|---:|
| IDX_GDAXI | Index | 0,85 % | 0,48 | −4,9 % | **0,25** |
| IDX_N225 | Index | 0,45 % | 0,30 | −3,9 % | **0,05** |
| IDX_FTSE | Index | 0,29 % | 0,16 | −10,1 % | 0,23 |
| EWG | ETF-DE | 1,02 % | 0,60 | −4,6 % | 0,33 |
| EWJ | ETF-JP | 0,34 % | 0,18 | −7,6 % | 0,26 |
| EWU | ETF-UK | 0,30 % | 0,19 | −7,7 % | 0,29 |

### Equal-Weight Global-Portfolio (25 % US / 25 % DE / 25 % JP / 25 % UK)

| Portfolio | CAGR | Sharpe | Max DD |
|---|---:|---:|---:|
| 25US/25DE/25JP/25UK | 2,18 % | **1,06** | **−3,64 %** |
| 100 % US (Ref) | 7,10 % | 1,25 | −8,62 % |

**Interpretation:** Die Korrelationen (0,05 Japan, 0,23 UK, 0,33 Deutschland) sind niedrig. Aber: single-Asset-Connors-MR löst pro Jahr zu wenig Trades aus, weshalb CAGR einbricht. Die **Stärke der US-Strategie liegt im Universe-Ranking** (aus 66 Werten immer die extremsten RSI-Werte wählen), nicht in der Connors-Logik allein.

**Nächster Test:** Eine internationale Variante bräuchte Länder-Aktien-Universen (DAX-40-Einzelwerte, Nikkei-225-Einzelwerte) statt einzelner Indizes.

---

## Weg C-4: Risk-Management-Overlays als Circuit-Breaker

**Methode:** Drei harte Schwellwert-Overlays, ausschließlich als Notfall-Ausschalter, nicht als Alpha-Filter.

| Circuit-Breaker | Trigger | Aktion |
|---|---|---|
| CB1_PANIC | VIX > 40 UND SPY < SMA200 | alle Positionen schließen |
| CB2_HY | HY-OAS > 800 bps UND steigt | Größe halbieren |
| CB3_DD | Portfolio-DD > −12 % | Paper-Modus bis Recovery |

### Ergebnisse

| Config | CAGR | Sharpe | Max DD | Calmar | ΔCAGR |
|---|---:|---:|---:|---:|---:|
| **Baseline** | **7,10 %** | 1,249 | **−8,62 %** | **0,824** | - |
| CB1_PANIC | 6,98 % | 1,227 | −8,63 % | 0,809 | −0,12 % |
| CB2_HY | 7,08 % | **1,264** | −8,62 % | 0,821 | −0,02 % |
| CB3_DD | 7,10 % | 1,249 | −8,62 % | 0,824 | 0,00 % |
| CB1+CB2 | 6,95 % | 1,237 | −8,63 % | 0,806 | −0,15 % |
| ALL_CBS | 6,95 % | 1,237 | −8,63 % | 0,806 | −0,15 % |

### Crash-Jahres-Returns (Baseline vs. mit CBs)

| Jahr | Baseline | CB1+CB2 |
|---|---:|---:|
| 2000 | +15,0 % | +12,5 % |
| 2001 | +0,1 % | −0,7 % |
| 2002 | +2,8 % | +5,5 % |
| 2008 | −3,0 % | −2,1 % |
| 2020 | +3,7 % | +3,2 % |
| 2022 | −0,7 % | −0,8 % |
| Crash-MaxDD | −8,6 % | −8,6 % |

Eine frühere SPY-Spalte wurde entfernt: Sie stammte nicht aus dem Backtest-Skript und mischte Kurs- und Gesamtrendite. SPY-Jahreswerte aus `adj_close` (inkl. Dividenden) stehen im Schritt-1-Bericht.

**Interpretation:** CB3_DD triggerte nie (der Portfolio-DD kam nicht in die Nähe von −12 %). CB1/CB2 sparten 2008 etwa 1 %-Punkt und kosteten 2000 etwa drei %-Punkte. In diesem Backtest hatten die Circuit-Breaker neben der SMA200-Bedingung kaum zusätzliche Wirkung.

**Forschungsnotiz:** Die Circuit-Breaker werden nicht in den Code übernommen. Für den Umgang mit Drawdowns siehe `Drawdown-Eskalations-Protokoll-V1.md`.

---

## Konsolidierte Einsichten aus Phase A-B-C

Nach 4 Wochen Forschung (Data-Lake, Baseline, Overlay-Attribution, Dual-Book, Robustness-Suite) steht:

1. **Mehr Komplexität hat in diesen Tests nicht geholfen.** Overlay-Stacks, Makro-Gates und Circuit-Breaker verschlechterten die hypothetischen Kennzahlen der einfachen Baseline oder änderten wenig. Der Sharpe von 1,25 über 29 Jahre ist ein In-Sample-Wert auf Überlebenden, kein Out-of-Sample-Ergebnis.

2. **Diversifikation über Strategie-Stile (MR + Trend) zeigte im Backtest mehr Wirkung als über einzelne Länder-Indizes.** Welche Mischung out of sample hält, ist offen.

3. **Parameter-Space ist flach.** RSI 23/25/27 und ATR 2,0/2,5/3,0 liefern alle Sharpe > 1,3 In-Sample. Die Strategie ist nicht an einen Schraubendreher-Sweet-Spot gebunden.

4. **Survivorship-Bias:** Der messbare Teil war klein, der strukturelle Teil (fehlende delistete Werte) ist nicht behoben.

---

## Artefakte Phase C (alle unter /backtest/)

- `results_walkforward/`: IS/OOS-Gridsearch, `walkforward_summary.json`
- `results_survivorship/`: Date-Filter-Vergleich, `survivorship_summary.json`
- `results_international/`: DAX/Nikkei/FTSE-Single + Global-EW, `international_summary.json`
- `results_circuit_breakers/`: 6 CB-Configs × Jahres-Returns, `circuit_breakers_summary.json`

---

## Phasen-Fazit

| Phase | Status | Kern-Befund |
|---|---|---|
| A (Data-Lake) | ✔ | 212 Serien / 16k Tage / 0 DQ-Errors |
| B (Backtest) | ✔ | Overlays ohne messbaren Beitrag; Strategie-Mix als weitere Testrichtung |
| C (Robustness) | ✔ | Kein Hinweis auf starke Überanpassung im gewählten Split; CBs ohne Zusatznutzen |

**Damit ist die Backtest-Phase abgeschlossen.** Alle Kennzahlen sind hypothetische historische Backtest-Werte (Survivorship-Bias, vereinfachte Kosten); sie sind keine Prognose und keine Empfehlung.
