# Trading-System V6.1: Regel-Spezifikation

**Version:** 6.1 · **Stand:** 2026-04-09 (für die Open-Source-Fassung neutral überarbeitet) · **Vorgänger:** V6

> **Hypothetische Backtest-Ergebnisse, keine Anlageberatung, keine Anlageempfehlung.** Dieses Dokument beschreibt Regeln eines Forschungssystems. Es enthält keine Renditeziele, keine Aussagen über einzelne Wertpapiere und keine Handlungsaufforderung. Maßgeblich ist immer der Code (`tools/daily_scanner.py`, `tools/exit_engine.py`, `tools/regime_gate.py`, `tools/universe.csv`).
>
> **Hypothetical backtest results, not investment advice, not a recommendation.** This document describes the rules of a research system. It contains no return targets, no statements about individual securities and no call to action. The code is always authoritative.

---

## 0. Worum es geht

V6.1 ist keine neue Strategie, sondern eine Regeländerung an drei Stellen der V6-Mean-Reversion (Connors-Logik). Anlass war eine Auswertung des hypothetischen Backtest-Korpus `v6_backtest_results_v61.csv` (440 Trades, pauschale Kostenannahme pro Trade): Zwei Exit-Gruppen, ATR-Stop-Loss-Exits und Exits nach Ablauf der maximalen Haltedauer, zehrten im Backtest den größten Teil der Gewinne aus den Connors-SMA5-Exits auf.

Die drei Änderungen:

1. **Maximale Haltedauer von 4 auf 3 Handelstage** (harter Exit).
2. **ATR-Multiplikatoren je Asset-Klasse** statt eines einheitlichen Werts.
3. **Höhere Score-Schwelle für strukturell schwache Werte** (Tier X).

Die übrigen Regeln (Regime-Gate, Intermarket-Veto, Score-Schwellen, Connors-SMA5 als Primär-Exit) blieben unverändert.

Alle Effekte dieser Änderungen wurden auf denselben historischen Daten geschätzt, auf denen die Schwächen gefunden wurden (In-Sample). Sie sind deshalb nur Hypothesen für weitere Tests, kein Nachweis.

---

## 1. Die drei Regeländerungen

### 1.1 Maximale Haltedauer: 3 Handelstage (hart)

**V6:** Trades ohne Connors-SMA5-Signal wurden am Ende von Tag 4 geschlossen.

**V6.1:**

```
Max-Hold = 3 Handelstage ab Entry-Close
Ende Tag 3: kein Connors-SMA5-Trigger -> Exit zur Eröffnung von Tag 4
Ausnahme: PEAD-Trades mit bestätigtem Earnings-Drift (bis 15 Tage)
```

Im Code: harter Exit T+3 in `tools/exit_engine.py` (Exit, wenn der Trade nach 3 Handelstagen nicht mindestens +1 % im Plus ist).

### 1.2 ATR-Multiplikator je Asset-Klasse

**V6:** `SL = Entry − 2,0 × ATR(14)` für alle Assets.

**V6.1:** Multiplikatoren je Asset-Klasse, das Chance-Risiko-Verhältnis bleibt 1 : 1,5 (das Ziel wird proportional mitgezogen):

| Asset-Klasse | ATR-Multi SL | ATR-Multi TP | Begründung |
|---|---|---|---|
| Crypto | 2,5 × | 3,75 × | hohe Intraday-Volatilität, sonst zu viele Stop-Auslösungen |
| Rohstoffe | 2,0 × | 3,0 × | unverändert |
| US-Large-Cap | 1,8 × | 2,7 × | etwas enger |
| DAX-Werte | 1,6 × | 2,4 × | geringere Volatilität |
| Default / Small- und Mid-Cap | 2,2 × | 3,3 × | vorsichtiger |

Im Code: Spalten `atr_multi_sl` und `atr_multi_tp` je Ticker in `tools/universe.csv`.

### 1.3 Tier X: höhere Schwelle für strukturell schwache Werte

Werte, die im hypothetischen Backtest über mindestens 20 Trades ein strukturell negatives Ergebnis im Mean-Reversion-Setup hatten, wurden in V6.1 in Tier X eingestuft. Für Tier X galt eine Score-Schwelle von 9,0 statt 7,5, also praktisch nur bei perfekter Konfluenz.

Diese Einstufung beruht auf einer kleinen, rückblickenden Stichprobe je Wert und sagt nichts über die Zukunft eines Wertpapiers aus. Ticker-genaue Ergebnisse sind hier bewusst nicht dokumentiert. Im heutigen Code steuert die Spalte `tier` in `tools/universe.csv` die Einstufung, und der Scanner nimmt Tier-X-Werte ganz aus den Kandidaten heraus (siehe `E11F-Inverse-ETF-Filter-2026-04-17.md`).

---

## 2. Unverändert aus V6

- **Entry-Score** (0 bis 10) aus mehreren Komponenten, mit der Anpassung in 2.1
- **Regime-Gate** (`tools/regime_gate.py`, sechs Klassen; fehlen VIX- oder SPY-Daten, gibt es kein Regime und keinen Trade)
- **Intermarket-Veto** (Öl +2 %, DXY +0,5 %, 10Y +5 bps, VIX +20 %)
- **Connors-SMA5 als Primär-Exit**
- **Trailing-Stop-Logik** (Tag 1: Break-even + 0,3 × ATR, Tag 2: Hoch − 1,0 × ATR, Tag 3: Hoch − 0,7 × ATR)
- **Uhrzeit-Regeln** (DE nach 09:15, US nach 15:45, nie in den ersten 15 Minuten)
- **Sektorrotations-Overlay** (±0,5 Bonus/Malus)

### 2.1 Score-Gewichte in V6.1

Solange Options-Flow, Insider und PEAD nicht mit echten Daten angebunden waren, galten reduzierte Gewichte. (Heute sind News, Options-Flow, Insider und Market-PCR nach der Layer-Ablation ganz abgeschaltet, siehe `tools/daily_scanner.py`, `DISABLED_LAYERS`.)

| Komponente | V6 Gewicht | V6.1 Gewicht |
|---|---|---|
| Triple-RSI | 15 % | 20 % |
| MACD+RSI-Kombination | 15 % | 15 % |
| VWAP-Konfluenz | 5 % | 8 % |
| Options-Flow (simuliert) | 20 % | 10 % |
| Corporate Insider (simuliert) | 10 % | 5 % |
| PEAD (simuliert) | 5 % | 2 % |
| Put/Call-Ratio | 5 % | 5 % |
| News/Sentiment | 5 % | 5 % |
| Connors-Setup-Qualität (neu) | 0 % | 10 % |
| Intermarket-Veto | −1,0 | −1,0 |
| Sektorrotation Bonus/Malus | ±0,5 | ±0,5 |

**Connors-Setup-Qualität:** RSI(2) < 10, Close > SMA(200) und RSI(5) < 25 ergeben 10 von 10 Punkten.

**Score-Schwellen:** 7,5 für Tier A und B, 8,0 für Tier C, 9,0 für Tier X (heute: je Ticker in `tools/universe.csv`, Spalte `score_threshold`).

---

## 3. Regelwerk V6.1 (Kurzreferenz)

```
ENTRY:
  1. Regime-Gate prüfen (nicht High-Vol-Krise, außer Tier A; kein Regime = kein Trade)
  2. Intermarket-Veto prüfen (−1,0 disqualifiziert)
  3. Score berechnen (Gewichte siehe 2.1)
  4. Tier-Schwelle prüfen (A/B: 7,5 | C: 8,0 | X: 9,0)
  5. Positionsgröße: über die eigene Konfiguration (Paper-Bot: tools/exec_bot_config.json)
  6. ATR-Multiplikator je Asset-Klasse (siehe 1.2)
  7. Uhrzeit: DE nach 09:15, US nach 15:45, nie in den ersten 15 Minuten
  8. Hard-Exit-Tag = Entry-Datum + 3 Handelstage

MANAGEMENT (täglich):
  - Tag 1: Stop -> Break-even + 0,3 × ATR
  - Tag 2: Stop -> Hoch − 1,0 × ATR, Connors-SMA5-Prüfung ab hier
  - Tag 3: Stop -> Hoch − 0,7 × ATR, Connors-SMA5-Prüfung, Hard-Exit am nächsten Morgen
  - Tag 4 Eröffnung: Exit (außer PEAD mit aktivem Drift)

EXIT-PRIORITÄT (im Code: tools/exit_engine.py):
  1. Connors-SMA5 (Close wieder auf oder über SMA5)
  2. ATR-Take-Profit
  3. ATR-Stop-Loss
  4. Hard-Exit T+3
  5. Intermarket-Veto ausgelöst -> flat
```

---

## 4. Prüfgrößen

Ob eine Regeländerung hält, lässt sich nur auf neuen Daten prüfen (Forward-Test, zum Beispiel im Paper-Modus). Prüfgrößen dafür, ohne Zielwerte:

- Anzahl Trades pro Jahr
- Trefferquote und Profit-Faktor, jeweils mit Bootstrap-Konfidenzintervall (`sim/bootstrap.py`)
- Anteil der Exits je Exit-Grund (Connors-SMA5, ATR-TP, ATR-SL, Hard-Exit)
- Ergebnisse in R-Vielfachen (Gewinn oder Verlust geteilt durch das Anfangsrisiko), nicht in Geldbeträgen

**Abbruchkriterium:** Läuft V6.1 nach einer ausreichenden Zahl neuer Trades schlechter als V6, werden die Änderungen einzeln zurückgenommen, beginnend mit den ATR-Multiplikatoren je Asset-Klasse (am wenigsten getestet).

---

## 5. Hebelprodukte

Der Code bildet keine Hebelprodukte ab (Knock-outs, Optionsscheine, Faktor-Zertifikate). Dieses Dokument enthält dazu keine Regeln.

---

## 6. Was V6.1 nicht löst

- **Regime-Drift über die Jahre** (V6.1 hilft nur indirekt; dafür gibt es das automatische Regime-Gate)
- **Simulierte Options-Flow- und Insider-Daten** (inzwischen nach der Ablation ganz abgeschaltet)
- **Walk-Forward-Validierung** (das In-Sample-Overfitting-Risiko dieser Änderungen bleibt)
- **Drawdown-Bremse und Disziplin-Layer** (siehe `Drawdown-Eskalations-Protokoll-V1.md`)
- **Vergleich mit einer einfachen Benchmark als Nullhypothese**

---

## 7. Changelog V6 -> V6.1

```
[CHANGED] Kostenannahme im Backtest: pauschaler Betrag pro Trade (statt eines höheren Pauschalwerts)
[CHANGED] Max-Hold: 4 Tage -> 3 Tage (hart, außer PEAD)
[CHANGED] ATR-Stop-Multiplikator: einheitlich 2,0 -> asset-klassen-spezifisch (1,6 bis 2,5)
[ADDED]   Tier X für strukturell schwache Werte (Score-Schwelle 9,0)
[ADDED]   Score-Komponente "Connors-Setup-Qualität" (10 %)
[CHANGED] Options-Flow-Gewicht: 20 % -> 10 % (simuliert, Übergang)
[CHANGED] Insider-Gewicht: 10 % -> 5 % (simuliert, Übergang)
[CHANGED] PEAD-Gewicht: 5 % -> 2 % (simuliert, Übergang)
[CHANGED] Triple-RSI-Gewicht: 15 % -> 20 % (Umverteilung)
[CHANGED] VWAP-Konfluenz: 5 % -> 8 % (Umverteilung)
[ADDED]   Hard-Exit-Tag als Pflichtfeld im Journal
```
