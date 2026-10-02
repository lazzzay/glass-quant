# Validation & Simulation Framework: 2026-04-16

**Typ:** Forschungs- und Planungsdokument (Stand 2026-04-16)
**Anlass:** die Anfrage, die Liste aktueller Validierungs-/Simulations-/Modellierungs-Ansätze aus der quantitativen Fachwelt (Monte Carlo, Bootstrap, GARCH, Copula, HMM, Jump-Diffusion, LOB-Simulation, ML-Challenger, TCA, VaR/CVaR, Stress-Tests, Factor Models, usw.) durch die Brille unseres konkreten Setups (Retail-Swing-Trader, Privat-Size, Free-Stack, Solo) zu filtern, und die überlebenden Ansätze in eine Roadmap mit Aufwand, Impact und konkreten SIM-Tickets zu gießen.
**Geschwister-Dokument:** `Data-Architecture-Review-2026-04-16.md` (Daten-Strang). Dieses Dokument ist der **Signal-/Modell-Strang**.

> Alle erwähnten Kennzahlen (zum Beispiel 440 Trades, WR 53,6 %) stammen aus dem hypothetischen Backtest-Korpus `v6_backtest_results_v61.csv` (pauschal 1 € Kosten je Trade). Sie sind keine Prognose. Keine Anlageberatung, keine Anlageempfehlung. / All figures mentioned are hypothetical backtest statistics, not a forecast and not investment advice.

---

## 0. Executive Summary

Unsere Strategie wird heute auf eine ehrliche, aber schmale Weise validiert: 794-Tage-Walk-Forward-Backtest, eine Handvoll Realisierungen, ein einziger Kostenansatz (flat broker fee, 1 €/Trade pauschal), ein Regime-Gate (VIX/Breadth), kein Slippage-Modell, kein Confidence-Interval auf WR/Sharpe, kein Stress-Test, kein Factor-Decomposing. Wenn das System morgen fremde Marktphasen trifft, höhere Vola-Regime, dünne Liquidität bei SmallCaps, Gap-Risiko zu Earnings, haben wir **keine numerische Grundlage**, die uns sagt, wie das System dort kippt. Wir sehen es erst, wenn es passiert.

Die quantitative Fachwelt hat dafür seit 40 Jahren Werkzeuge entwickelt. Aber nicht alle davon sind für uns relevant. **LOB-Simulation, Matching-Engines, Smart-Order-Routing, Impact-Modelle, Latency-Modelle sind für Privat-Size bei einem Retail-Broker schlicht irrelevant**, wir triggern keinen Impact, haben kein Routing-Problem, keine Latency, die über Millisekunden gewinnt. Wer das trotzdem baut, verbrennt Monate für Null Rendite. **Die sechs Werkzeuge, die für uns entscheidend sind, sind stochastische Robustheit (Monte Carlo + Bootstrap), realistische Kosten (Slippage + Spread), Risiko-Zerlegung (Factor Models), Tail-Risiko (VaR/CVaR), Regime-Awareness (HMM/Markov-Switching) und gezieltes Kaputt-Testen (Stress + Sensitivity).** Diese sechs sind das Muss. Vier weitere (Event-Driven Backtesting, TCA, ML-Challenger, Time-Series CV) sind ein klares Sollte. Der Rest ist für unsere Größenordnung Theater.

Daraus folgt eine **4-Wellen-Roadmap (V7.0 → V10.0)** parallel zum Daten-Strang:
Welle A etabliert stochastische Validierung + ehrliche Kosten (die beiden billigsten Gewinne mit dem höchsten Impact auf Vertrauen in Kennzahlen).
Welle B zerlegt Risiko (Faktoren, VaR/CVaR).
Welle C macht Regime explizit und testet das System gegen historische Schocks.
Welle D stellt einen ML-Challenger neben das Regel-System, damit wir sehen, ob unser Scoring dem ML noch überlegen ist: der stärkste Anti-Overfitting-Stress, den es gibt.

Das Ganze passt 1:1 in den Projekt-Rahmen: **Free-Stack** (scipy, numpy, pandas, statsmodels, scikit-learn, alle pip-lokal, keine Paid-API, kein Cloud-Cluster), **lokale Software**, kompatibel mit dem im Architektur-Review beschriebenen Lakehouse (DuckDB/Parquet liefern später die Gold-Views als Input für Simulationen und ML). Der Rechenaufwand für alle sechs Muss-Werkzeuge bleibt auf einem Laptop <1 Minute pro Tag.

---

## 1. IST-Diagnose: Womit validieren wir heute, und wo die Lücken sind

### 1.1 Was wir heute haben

- **Walk-Forward-Backtest V6:** 794 Tage, 440 Trades, WR 53,6 %, Connors-SMA5 als Kernsignal, ATR-Stops. Ergebnis: **ein Pfad, eine Realisierung.**
- **Phase-C-Validierung (2026-04-15):** Walk-Forward ohne Hinweis auf starke Überanpassung im gewählten Split, Survivorship moderat getestet, International niedrig korreliert, CBs als eigenständige Assetklasse entlarvt-als-überflüssig. **Stichpunkthaft solide, aber kein kontinuierlicher Prozess.**
- **Observation-Window V6.8 (2026-04-15 bis 2026-04-29):** 2-Wochen-Live-Validation mit Feature-Freeze. **Gut gegen Tinkering, aber n=10 Tage ist statistisch zu klein, um die meisten Effekte signifikant zu sehen.** (Beobachtung des laufenden Systems, ohne Ergebnisse in diesem Repository.)
- **Regime-Gate:** VIX + Breadth → Long/Defensiv/No-Trade. Diskret (3-stufig), nicht probabilistisch.
- **Kosten-Modell:** flat broker fee 1 €/Trade. Spread & Slippage nicht modelliert.
- **Kennzahlen:** WR, Sharpe, Sortino, Max-DD, Calmar im Performance-Dashboard V6.5. Als **Punktschätzer**, ohne Konfidenzintervall.

### 1.2 Die sechs Blinden Flecken

**(A) Statistische Unsicherheit.** 440 Trades klingen viel, sind aber bei WR ≈ 54 % ein 95 %-Konfidenzintervall von etwa [49 %, 59 %]. Wir verhalten uns in der Kommunikation aber, als sei "53,6 % WR" ein Fels. **Konsequenz:** Kleine Verschlechterungen sehen wie Strategie-Zerfall aus, die in Wahrheit im Rauschen liegen, und umgekehrt.

**(B) Unrealistische Kosten.** Pauschale 1 € unterschlägt Spread (bei SmallCap-Deutschland 20-80 bps keine Seltenheit) und Slippage (bei 08:00 Open mit Market-Order durchaus 10-30 bps). In einer realistischen Rechnung **ist das zusätzlich 40-100 bps Reibung pro Round-Trip**: das zerlegt Sharpe und Calmar messbar. Heute haben wir darauf null Sicht.

**(C) Keine Risiko-Zerlegung.** Wir wissen nicht, ob unsere Rendite aus Skill kommt (Alpha) oder aus Faktor-Exposure (Small-Cap-Tilt, Momentum-Tilt, Volatility-Faktor). Wenn die Fama-French-Faktoren 70 % der Rendite erklären, handeln wir im Grunde einen Factor-ETF. **Das muss man wissen, bevor man irgendeine Kennzahl als Edge interpretiert.**

**(D) Kein Tail-Risiko-Modell.** Max-DD ist der maximale historische Einbruch: ein ex-post-Extremwert. VaR (Value-at-Risk) und insbesondere CVaR (Expected Shortfall) quantifizieren das Tail-Risiko ex-ante als Verteilung. Für jede Entscheidung über Positionsgrößen ist das die relevantere Größe.

**(E) Regime ist diskret.** Long/Defensiv/No-Trade als harte Umschaltung. In Wirklichkeit wechseln Regime fließend und mit Persistenz. Ein **HMM (Hidden-Markov-Model)** auf VIX + Breadth + Returns liefert Wahrscheinlichkeiten statt harter Labels ("62 % Bull-Regime, 31 % Transition, 7 % Bear") und macht die Regime-Detection deutlich robuster gegen Flackern.

**(F) Keine gezielten Stress-Tests.** Wir wissen nicht, wie sich das System im März 2020 (COVID-Crash), Februar 2018 (Volmageddon), Dezember 2018 (Santa-Drawdown), August 2011 (Euro-Krise) verhalten hätte. Historische Szenarien sind im Data-Lake drin, aber nicht als Stress-Suite abrufbar.

### 1.3 Was außerhalb unseres Scopes liegt (und bleibt)

Die Fachwelt kennt eine zweite Liga an Werkzeugen, die für **institutionelle Market-Maker und HFT** lebenswichtig sind, für uns aber irrelevant:

- **Limit-Order-Book-Simulation (LOB):** wir handeln Market-Orders auf Retail-Size. Kein Relevanzhebel.
- **Matching-Engines, Queue-Position-Modelle:** nur relevant ab Liquidity-Provider-Skala.
- **Agent-Based-Markets (Farmer, LeBaron):** Forschungs-Spielwiese, kein Operatives.
- **Impact-Modelle (Almgren-Chriss, Kyle, Hasbrouck):** wir sind zu klein, um Impact zu erzeugen. Unser Impact ist ε.
- **Latency-Modelle, Smart-Order-Routing, Co-Location:** der Broker routet, wir nicht. Ms-Game spielt nicht mit uns.
- **Support-Vector-Machines, exotische Deep-Nets als Primär-Signal:** Overfitting-Falle bei unserer Sample-Größe; dazu Black-Box-Charakter, den wir bewusst vermeiden.

Jede Stunde, die wir in diese Kategorie investieren, fehlt bei den Muss-Werkzeugen.

---

## 2. Gap-Matrix: Lücken → Werkzeug → Priorität

| # | Lücke | Werkzeug | Kategorie | Aufwand | Impact | Priorität |
|---|-------|----------|-----------|---------|--------|-----------|
| 1 | Punktschätzer ohne Streuung | **Bootstrap** auf Trade-Verteilung → Konfidenzintervalle für WR/Sharpe/Sortino/Calmar/MaxDD | Stochastik | S | Hoch | **MUSS: Welle A** |
| 2 | Einziger historischer Pfad | **Monte-Carlo-Resampling** der Trade-Returns → 10 000 synthetische Equity-Kurven | Stochastik | S | Hoch | **MUSS: Welle A** |
| 3 | Unrealistische Execution-Kosten | **Slippage + Spread Model** pro Asset-Klasse (SmallCap/LargeCap/ETF/Hebel) | Execution | S | Hoch | **MUSS: Welle A** |
| 4 | Unklares Alpha vs. Faktor-Rendite | **Fama-French-3F / 5F Regression** auf Monats-Returns | Risiko-Attribution | M | Mittel-Hoch | **MUSS: Welle B** |
| 5 | Kein Tail-Risiko-Maß ex-ante | **VaR (95/99)** + **CVaR/Expected Shortfall** auf Portfolio-Returns, rolling | Risiko | S | Hoch | **MUSS: Welle B** |
| 6 | Diskretes Regime-Gate | **HMM (Hidden-Markov)** auf VIX + Breadth + S&P-Returns → Regime-Posteriors | Regime | M | Mittel-Hoch | **MUSS: Welle C** |
| 7 | Keine Crash-Szenarien | **Stress-Suite** (COVID-20, Volmageddon-18, Xmas-18, Lehman-08, Euro-11) + **Sensitivity** auf Scoring-Gewichte | Stress | S-M | Hoch | **MUSS: Welle C** |
| 8 | Tagesraster-Backtest | **Event-Driven Backtester** (Order-Intent an TickData, nicht an Close) | Realismus | M | Mittel | SOLLTE: Welle C |
| 9 | Keine Execution-Analyse | **TCA (Transaction Cost Analysis)** auf Live-Trade-Journal | Realismus | S | Mittel | SOLLTE: Welle B |
| 10 | Kein ML-Gegenmodell | **ML-Challenger** (XGBoost oder Logistic Regression) vs. Regel-Score | Overfitting-Stress | M | Mittel-Hoch | SOLLTE: Welle D |
| 11 | CV leakt Zukunft | **Purged Time-Series CV (López de Prado)** statt random k-fold | ML-Validierung | S | Mittel | SOLLTE: Welle D |
| - | LOB / Matching / Impact / Latency / Agent-Based / SVM | (s. §1.3) | - | - | - | **NEIN, nicht bauen** |

---

## 3. Ziel-Architektur: Der Validation- & Simulation-Layer

### 3.1 Modul-Struktur

```
sim/
├── bootstrap.py          # Block-Bootstrap auf Trade-Sequenz (5-day-block default)
├── monte_carlo.py        # Monte-Carlo-Pfad-Resampling aus Trade-Verteilung
├── costs.py              # Slippage + Spread Model pro AssetClass (SmallCap/LargeCap/ETF/Hebel)
├── factors.py            # Fama-French 3F/5F Regression auf Monats-Returns
├── risk.py               # VaR/CVaR rolling 252d, Portfolio-Ebene
├── regime_hmm.py         # HMM auf VIX+Breadth+SP500-Returns → Regime-Posteriors
├── stress.py             # Stress-Suite (Replay historischer Krisen-Fenster)
├── sensitivity.py        # Grid-Sweep über Scoring-Gewichte → Sharpe-Heatmap
├── event_backtest.py     # Event-Driven Backtester (optional, Welle C)
├── tca.py                # Transaction Cost Analysis auf trade_journal.sqlite
├── ml_challenger.py      # XGBoost/LogReg Challenger vs. rule-score
└── _reports/             # Monte-Carlo-Plots, Heatmaps, VaR-Kurven (PNG/HTML)
```

### 3.2 Daten-Input

Alle Simulations-Module konsumieren **Gold-Views** aus dem Lakehouse (Daten-Strang, V7.1+):

- `gold/trade_journal`: realisierte Trades mit Attribution
- `gold/equity_curve`: tägliche Kapital-Kurve
- `gold/daily_returns`: Pct-Returns pro Ticker
- `gold/regime_series`: tägliche VIX/Breadth/Sentiment-Ebene
- `gold/factors_ff`: vorberechnete Fama-French-Faktoren (aus Ken-French-Website oder lokal replikiert)

Vor V7.1 (Gold-Layer fehlt noch) arbeiten die Module direkt gegen `data_lake/prices/daily/*.parquet` und `tools/trade_journal.sqlite`: funktioniert, ist aber redundant. Sauber wird es erst mit Gold-Views.

### 3.3 Kosten- und Ressourcen-Rahmen

- **Stack:** `scipy`, `numpy`, `pandas`, `statsmodels`, `scikit-learn`, `hmmlearn`, optional `xgboost`. Alle pip-lokal, alle MIT/BSD. **Kein Paid-API, kein Cloud.**
- **Laufzeit pro Nightly-Run:** Bootstrap 10 000 Resamples <5 s; Monte Carlo 10 000 Pfade <10 s; HMM-Fit auf 5 000 Tagen <30 s; FF-Regression <1 s. Gesamt << 1 min auf einem Laptop.
- **Storage:** Reports als PNG/HTML in `sim/_reports/`, Numerik als JSON/Parquet in `lake/silver/sim/`.
- **Gate:** Simulations-Ergebnisse fließen in den Data-Health-Report als zusätzliche Audits (z. B. "Sharpe-CI95 = [0.41, 1.12]: Lower-Bound negativ → warnen").

---

## 4. Roadmap: 4 Wellen parallel zum Daten-Strang

Jede Welle entspricht einem oder mehreren Arbeitsblöcken und steht **nicht in Konkurrenz** zu Bugfixes.

### Welle A (V7.0 Sim-Core): ~1 Woche

**Ziel:** Wir haben Konfidenzintervalle auf alle Headline-Kennzahlen und eine ehrliche Kostenrechnung.

- **SIM-1 Bootstrap-CI:** `sim/bootstrap.py` implementiert Block-Bootstrap (5-day-blocks, 10 000 Resamples) auf Trade-Sequenz; liefert 95 %-CI für WR, Sharpe, Sortino, Calmar, MaxDD. Output in `sim/_reports/bootstrap_ci_YYYY-MM-DD.json`. **Aufwand S, Impact Hoch.**
- **SIM-2 Monte-Carlo-Pfade:** `sim/monte_carlo.py` sampelt 10 000 synthetische Equity-Kurven aus der Trade-Return-Verteilung; Output: Median-, 5-%-, 95-%-Quantil-Pfade als Plot + Drawdown-Verteilung. **Aufwand S, Impact Hoch.**
- **SIM-3 Slippage+Spread-Model:** `sim/costs.py` mit Asset-Class-Buckets: LargeCap 5 bps / SmallCap 25 bps / ETF 3 bps / Hebel 15 bps (kalibriert auf Realized-Fill vs. Close aus trade_journal.sqlite). Backtest-Engine ruft Kosten-Funktion. **Aufwand S, Impact Hoch.**
- **Abnahme:** Der Tagesreport zeigt neben WR/Sharpe jetzt auch "[WR 95%-CI: 49.1-59.0 %]"; Backtest-Re-Run mit realistischen Kosten reduziert WR um X bps: Differenz dokumentiert.

### Welle B (V7.5 Risiko-Attribution): ~1 Woche

**Ziel:** Wir wissen, wo unsere Rendite herkommt und wie groß das Tail-Risiko ist.

- **SIM-4 Fama-French-Regression:** `sim/factors.py` regressiert Monats-Returns des Portfolios gegen FF-3F (Mkt-Rf, SMB, HML) und FF-5F (+RMW, CMA). Alpha, Beta, R² als Output. **Aufwand M, Impact Mittel-Hoch.**
- **SIM-5 VaR + CVaR:** `sim/risk.py` berechnet rolling 252d VaR(95), VaR(99), CVaR(95) auf täglichen Portfolio-Returns; historisch + parametrisch (Student-t) + Monte-Carlo. **Aufwand S, Impact Hoch.**
- **SIM-6 TCA:** `sim/tca.py` liest trade_journal.sqlite, vergleicht realized fill vs. decision price, zerlegt in Spread/Slippage/Timing, speist zurück ins Kosten-Modell. **Aufwand S, Impact Mittel.**
- **Abnahme:** Monats-Report "Alpha nach FF5 = 0.X %/Monat, R² = 0.XX"; Weekly-Health zeigt VaR/CVaR-Trend.

### Welle C (V8.5 Regime + Stress): ~1-2 Wochen

**Ziel:** Das diskrete Regime-Gate wird probabilistisch, und wir testen das System gegen historische Krisen.

- **SIM-7 HMM-Regime:** `sim/regime_hmm.py` mit hmmlearn, 3-State-GaussianHMM auf [VIX, Breadth, SP500-5d-Return]. Output: posteriors pro Tag, persistiert in `lake/silver/regime_hmm/`. Scanner/Scoring-Layer liest Posterior statt harten Labels. **Aufwand M, Impact Mittel-Hoch.**
- **SIM-8 Stress-Suite:** `sim/stress.py` replayt System auf 5 Fenstern: 2020-02-04 (COVID), 2018-01-02 (Volmageddon), 2018-12 (Xmas-DD), 2011-07-08 (Euro-Krise), 2008-09-11 (Lehman). Report: MaxDD, Zeit-zur-Recovery, WR im Fenster pro Szenario. **Aufwand S, Impact Hoch.**
- **SIM-9 Sensitivity-Sweep:** `sim/sensitivity.py` fährt Grid über Scoring-Gewichte (Connors, MTF, Insider, OptionsFlow, Sector-RS, Regime) ± 25 %, misst Sharpe-Response als Heatmap: identifiziert Gewichte, die fragil reagieren (Überanpassung-Indikator). **Aufwand M, Impact Mittel-Hoch.**
- **SIM-10 Event-Driven-Backtester (optional):** wenn Zeit übrig, Tages-Raster auflösen auf 5-min-Bars, Order-Intent gegen TickData. Ansonsten in Welle D verschieben. **Aufwand M, Impact Mittel.**
- **Abnahme:** Stress-Report `sim/_reports/stress_suite.md` zeigt, wie das System sich in jeder historischen Krise (hypothetisch) verhalten hätte; Regime-HMM ersetzt die Ampel im Tagesreport.

### Welle D (V9.5 ML-Challenger): ~1 Woche

**Ziel:** Ein ML-Modell läuft parallel und wird regelmäßig gegen das Regel-System getestet. Wenn ML den Regel-Score signifikant schlägt, ist das ein Warnsignal (entweder ML fängt versteckte Signale ein, oder Regel-System ist über-angepasst); wenn nicht, ist das Regel-System robust.

- **SIM-11 ML-Challenger-XGB:** `sim/ml_challenger.py` trainiert XGBoost auf denselben Features wie Regel-Score (Connors-Flags, MTF-States, Insider-Score, OF-Score, Sector-RS, Regime-Posterior), target = 5d-Forward-Return-Sign. **Aufwand M, Impact Mittel-Hoch.**
- **SIM-12 Purged-Time-Series-CV:** López-de-Prado-Split (purge + embargo) statt random k-fold, um Look-Ahead-Leakage zu verhindern. **Aufwand S, Impact Mittel.**
- **SIM-13 Wöchentlicher Challenger-Report:** Der Weekly-Health-Report vergleicht Regel-Score vs. XGB-Prediction auf Hit-Rate, LogLoss, AUC; Ergebnisse fließen als ops_ticket, wenn ML den Regel-Score ≥50 bps/Monat schlägt (Strategie-Review-Trigger). **Aufwand S, Impact Mittel.**
- **Abnahme:** Challenger läuft sonntags, Output in `sim/_reports/ml_challenger_KWxx.md`, fütterte Regime-Discussion.

### Übersicht Aufwand / Impact

| Welle | Tickets | Dev-Aufwand | Kumulativ-Impact auf Kennzahl-Vertrauen |
|-------|---------|-------------|------------------------------------------|
| A (V7.0) | SIM-1/2/3 | ~1 Woche | +++  (Kennzahlen bekommen Streuung, Kosten werden ehrlich) |
| B (V7.5) | SIM-4/5/6 | ~1 Woche | ++   (Alpha-vs-Faktor klar, Tail-Risiko quantifiziert) |
| C (V8.5) | SIM-7/8/9/10 | ~1-2 Wochen | +++ (Regime probabilistisch, Stress-Suite etabliert) |
| D (V9.5) | SIM-11/12/13 | ~1 Woche | ++   (Overfitting-Stresstest durch ML-Challenger) |

**Gesamt-Aufwand:** ~4-5 Wochen Dev-Zeit, parallelisierbar zum Daten-Strang (DATA-ARCH-1/2/3 laufen in Welle I/II, SIM-1/2/3 könnten parallel laufen, weil sie direkt gegen trade_journal.sqlite gehen, kein Warten auf Gold-Layer nötig).

---

## 5. Konkrete nächste Schritte (3 Arbeitsblöcke)

**Block 1:** SIM-1 Bootstrap-CI + SIM-2 Monte-Carlo in einem Rutsch bauen. Beide operieren auf derselben Trade-Liste, teilen Infra. Output: `sim/_reports/bootstrap_ci_2026-04-XX.json` + `sim/_reports/monte_carlo_2026-04-XX.png`. Abnahme: der Tagesreport erhält eine Zeile "WR 53.6 % (95%-CI: 49.1-59.0 %)" (hypothetischer Korpus).

**Block 2:** SIM-3 Slippage+Spread-Model. Zunächst statische Buckets aus Erfahrungswerten, später (Welle B) mit TCA-kalibriert. Backtest-Engine neu durchlaufen, Differenz zu heute dokumentieren.

**Block 3:** SIM-5 VaR/CVaR (SIM-4 Fama-French ist Monatsfrequenz und kann später kommen). VaR/CVaR passt tages-täglich und liefert sofort Tail-Risiko-Zahlen für die Risikobewertung.

Danach neu priorisieren: Welle B fortsetzen oder direkt zu Welle C springen.

---

## 6. Was wir bewusst nicht bauen

1. **Keine LOB-Simulation, keine Matching-Engine, keine Queue-Position-Modelle.** Retail-Market-Order bei einem Retail-Broker, Privat-Size, nicht unsere Welt.
2. **Kein Impact-Modell (Almgren-Chriss, Kyle, Obizhaeva-Wang).** Unsere Tickets sind zu klein für messbaren Impact.
3. **Kein Latency-Modell, kein Smart-Order-Routing, keine Co-Location-Simulation.** Der Broker routet, wir nicht. Ms-Game irrelevant.
4. **Keine Agent-Based-Markets (Farmer, LeBaron, Lux-Marchesi).** Forschungs-Sandbox, kein Operatives.
5. **Kein Deep-Neural-Net als Primär-Signal, keine SVM.** Overfitting-Risiko bei unserer Sample-Größe zu hoch, Black-Box-Charakter widerspricht Transparenz-Anforderung. XGBoost und Logistic Regression reichen als Challenger vollkommen.
6. **Keine Options-Pricing-Modelle (Black-Scholes-Heston, SABR).** Der Code bildet weder Optionen noch Hebelprodukte ab.
7. **Keine GARCH/EGARCH als Primär-Volatilitätsmodell.** HMM für Regime ist ausreichend und interpretierbarer. GARCH könnten wir später in Welle E optional ergänzen, ist aber nicht auf dem kritischen Pfad.

Wir adaptieren das Konzept "Distribution statt Punktschätzer" aus der quantitativen Fachwelt, nicht ihre institutionelle Werkzeug-Kultur.

---

## 7. Verzahnung mit bestehenden Strukturen

- **Tickets:** SIM-1 bis SIM-13 sind als Arbeitspakete gedacht (Kategorie `simulation-framework`), analog zu DATA-ARCH-1/2/3.
- **Weekly-Health-Report:** wird um Bootstrap-CI-Check und VaR-Breach-Check erweitert (ab Welle A bzw. B).
- **Data-Health-Monitor:** bekommt zusätzliche `check_sim_reports()`: wenn Monte-Carlo-Drawdown-95-% jenseits 2× historischer MaxDD liegt, schreibt er ein M-Ticket `SIM_MC_TAIL_RISK_ELEVATED`.
- **Lakehouse (Daten-Strang):** Simulations-Module lesen ab Welle B Gold-Views statt Raw-JSONs. Zeitliche Verzahnung: DATA-ARCH-1/2/3 sollten vor SIM-4/5 laufen. SIM-1/2/3 können parallel zur Daten-Migration laufen, weil sie trade_journal.sqlite als Quelle haben.
- **Scheduled-Tasks:** Nach Welle A bekommt der Abend-Tracking-Task einen zusätzlichen Aufruf `python3 sim/bootstrap.py --date X --quiet`, der die Bootstrap-CIs täglich aktualisiert.

---

## 8. Schluss

Das Validation- & Simulation-Framework ist der **zweite Strang** neben der Daten-Architektur. Während der Daten-Strang die **Qualität des Inputs** absichert, sichert der Simulations-Strang die **Ehrlichkeit des Outputs**: wir wissen dann, wie sicher unsere Zahlen sind, wo Renditen herkommen, wie das System in Krisen kippt, und ob unser Regel-System dem ML noch standhält. Beide Stränge zusammen sind die Voraussetzung dafür, Kennzahlen überhaupt belastbar zu interpretieren.

Startzeitpunkt: **der nächste freie Arbeitsblock**. Erstes Liefer-Ziel: Report mit Bootstrap-CI neben WR. Zweites: realistische Kosten. Danach Welle B.

---

*Referenzdokumente:*
*- `Data-Architecture-Review-2026-04-16.md` (Daten-Strang)*
*- `tools/data_health.py` (Data-Health-Monitor V6.9)*
