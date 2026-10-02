# Phase B Abschlussbericht: Wege 1, 2 und 3

> **Hypothetische Forschungsergebnisse, keine Anlageberatung und keine Anlageempfehlung.** Alle Kennzahlen stammen aus Backtests auf 66 US-Large-Caps plus Sektor- und Broad-ETFs (Survivorship-Bias: nur heute noch existierende Werte), mit im Rückblick gewählten Parametern, pauschal 1 USD Kosten pro Trade und ohne Live-Track-Record. Sizing: MR-Baseline mit 1 % Konto-Risiko je Trade auf 100.000 USD; Trend-Modul gleichgewichtet über höchstens 5 Positionen; Blends als gewichtete Mischung der täglichen Renditen. Die Daten sind nicht im Repository enthalten. Die Zahlen sind keine Prognose.
>
> **Hypothetical research results, not investment advice and not a recommendation.** All figures come from backtests on 66 US large caps plus sector and broad ETFs (survivorship bias: only names that still exist today), with parameters chosen in hindsight, a flat cost of 1 USD per trade and no live track record. Sizing: MR baseline with 1 % account risk per trade on 100,000 USD; trend module equal-weighted over at most 5 positions; blends as weighted mixes of the daily returns. The data is not included in this repository. The numbers are not a forecast.

**Stand:** 2026-04-15
**Backtest-Zeitraum:** 1997-01-02 bis 2026-04-15 (29,3 Jahre, 7.367 Tage)
**Universe:** 66 US-Large-Caps + Sektor/Broad-ETFs
**Kosten:** 1 USD/Trade (flat fee assumption)

---

## Executive Summary

Drei parallele Auswertungen auf hypothetischen Backtests:

1. **Weg 1 (Overlay-Attribution) ist vorerst negativ**: keines der getesteten Makro-Overlays (O3 Liquidity, O4 Credit, E1 Earnings-Blackout, E8 Weekly-Trend) verbessert die Baseline. Sie alle kosten zwischen 0,01 % und 1,71 % CAGR. Lehre: Die SMA200-Filter-Logik der Baseline fängt das meiste schon selbst ab.

2. **Weg 2 (Blend Baseline + SPY) liefert eine klare Pareto-Front**, aber keinen echten Alpha-Sprung. Die reine Baseline dominiert bei Sharpe/DD, SPY dominiert beim CAGR. Mischungen sind lineare Kompromisse.

3. **Weg 3 (Dual-Book MR + Trend):** Die Kombination aus Mean-Reversion-Baseline und Trend-Following-Modul hatte in diesem hypothetischen Backtest die höchste risikoadjustierte Kennzahl der getesteten Varianten. Das ist ein In-Sample-Befund mit bekannten Verzerrungen, kein Beleg für künftige Ergebnisse.

---

## Die drei Kern-Zahlenreihen

### Einzelkomponenten 1997-2026

| Strategie | CAGR | Sharpe | Sortino | Max DD | Calmar |
|---|---:|---:|---:|---:|---:|
| **MR-Baseline** | 7,11 % | **1,25** | 1,64 | **−8,62 %** | **0,82** |
| **Trend-Module** | **14,01 %** | 0,71 | 0,95 | −52,60 % | 0,27 |
| SPY Buy&Hold | 9,86 % | 0,58 | 0,75 | −55,19 % | 0,18 |

### Weg 1: Overlay-Attribution auf MR-Baseline

| Overlay | CAGR | Sharpe | Max DD | Δ CAGR vs Baseline |
|---|---:|---:|---:|---:|
| Baseline (ohne Overlays) | 7,10 % | 1,249 | −8,62 % | - |
| + O3 Liquidity (NetLiq-Size-Tilt) | 6,05 % | 1,165 | −7,87 % | **−1,05 %** |
| + O4 Credit (HY-OAS-Gate) | 6,60 % | 1,191 | −8,62 % | −0,50 % |
| + E1 Earnings-Blackout | 6,94 % | 1,227 | −8,62 % | −0,16 % |
| + E8 Weekly-Trend | 7,09 % | 1,248 | −8,67 % | −0,01 % |
| + O3+O4+E1+E8 (alle) | 5,39 % | 1,099 | −9,02 % | **−1,71 %** |

**Ehrlicher Befund:** Kein einziger Overlay brachte Alpha. Das ist unbequem, aber wertvoll: es bewahrt uns davor, Live-Geld auf eine ungetestete Komplexität zu setzen. Die V6.x-Upgrades bleiben gültig als **Live-Filter** gegen bestimmte Risiken (z. B. Earnings-Vola), aber sie liefern keinen historisch messbaren Alpha-Beitrag auf den 30 Jahren.

### Weg 2: Baseline + SPY Blend (Pareto-Highlights)

| Mix | Rebal | CAGR | Sharpe | Max DD |
|---|---|---:|---:|---:|
| 100 % Baseline | - | 7,06 % | 1,24 | −8,6 % |
| 70 MR / 30 SPY | quarterly | 7,77 % | 1,03 | −19,9 % |
| 50 MR / 50 SPY | none | 8,67 % | 0,82 | −25,8 % |
| 100 % SPY (Referenz) | - | 9,86 % | 0,58 | −55,2 % |

### Weg 3: Dual-Book MR + Trend

| Mix MR/Trend | Rebal | CAGR | Sharpe | Sortino | Max DD | Calmar |
|---|---|---:|---:|---:|---:|---:|
| 80 / 20 | yearly | 8,84 % | **1,12** | 1,51 | −13,1 % | 0,68 |
| 80 / 20 | quarterly | 8,44 % | 1,08 | 1,46 | −11,3 % | **0,75** |
| **70 / 30** | **yearly** | **9,69 %** | **1,03** | 1,39 | **−15,4 %** | 0,63 |
| 70 / 30 | quarterly | 9,26 % | 1,00 | 1,36 | −14,7 % | 0,63 |
| 60 / 40 | yearly | 10,47 % | 0,96 | 1,29 | −19,4 % | 0,54 |
| 50 / 50 | yearly | 11,19 % | 0,90 | 1,21 | −25,7 % | 0,44 |

Die Zeile **70 MR / 30 Trend, yearly rebalanced** ist nur eine von mehreren getesteten Mischungen. Die Gewichte wurden im Rückblick auf denselben Daten gewählt, auf denen sie gemessen wurden (In-Sample). Die Tabelle ist deshalb eine Forschungsnotiz und weder eine Allokation noch ein Vergleich, der eine Strategie empfiehlt.

---

## Interpretation

Die Mean-Reversion-Baseline und das Trend-Modul lösen in unterschiedlichen Marktphasen aus: Baseline-Signale entstehen nach Pullbacks (eher in ängstlichen Märkten), Trend-Signale nach Ausbrüchen (eher in bullischen Märkten). Das ist eine mögliche Erklärung für die niedrigere Schwankung der Mischung im Backtest, kein Nachweis.

Mehr Overlays auf einem Stil haben in diesen Tests nicht geholfen. Diversifikation über Strategie-Stile ist die Richtung, die weiter getestet werden sollte (out of sample).

---

## Nächste Tests (Phase C)

1. **Walk-Forward-Parametertuning:** Bislang sind Baseline- und Trend-Parameter fix. Ein ehrliches Walk-Forward mit Expanding-Training prüft, ob die Ergebnisse out of sample halten.
2. **Survivorship-Bias-Fix** mit dem geladenen `sp500_changes.parquet`: die 66 aktuellen Large-Caps sind survivorship-positiv. Eine Re-Evaluation mit historischer Index-Composition prüft die Robustheit.
3. **International diversification:** DAX, Nikkei, FTSE auf gleichen Regeln: die Trend-Komponente sollte in nicht-US-Märkten genauso funktionieren.
4. **Risk-Management-Overlays** als Notfall-Circuit-Breaker im Backtest prüfen (nicht als Alpha), zum Beispiel eine Regel, die bei HY-OAS > 800 bps die Positionsgröße halbiert.

---

## Artefakte (alle unter /backtest/)

- `results_v1/`: Baseline (CAGR 7,1 %, Sharpe 1,25, DD −8,6 %)
- `results_blend/`: Baseline+SPY-Pareto-Front mit Pareto-Chart
- `results_overlay/`: Overlay-Attribution mit Delta-Chart + Report
- `results_trend/`: Trend-Module Standalone
- `results_dualbook/`: Dual-Book Blends + Triple-Blends + Final-Chart

Phase B ist damit abgeschlossen. Alle Zahlen sind hypothetische historische Backtest-Werte mit Survivorship-Bias und im Rückblick gewählten Parametern. Sie sind keine Empfehlung, keine Allokation und keine Garantie für die Zukunft.
