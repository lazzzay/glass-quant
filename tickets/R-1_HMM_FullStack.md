# R-1: HMM Full-Stack Regime-Classifier

**Status:** OPEN · **Priorität:** P1 · help wanted
**Vorgänger:** Q3 (HMM-Stub, KW17 Tag 3: `regime/hmm_regime_stub.py`, live ab 2026-04-23)
**Aufwand-Schätzung:** ~2.5 d (Feature-Engineering 0.5 d, Modell+Training 0.75 d, Backtest-Hook 0.5 d, Shadow-Switch + Tests 0.5 d, Docs 0.25 d)

---

## 1. Ziel

Vom **rein informativen Stub** (3 States, 3 Features, INFORMATIONAL) zum **produktiven Regime-Classifier** mit:
- 5 States (Calm-Bull, Trending-Bull, Normal, Choppy, Stress)
- 8-10 Features (SPY-Returns, VIX-Level + Vol-of-Vol, Breadth, Term-Structure, Credit-Spread-Proxy, USDX, Yield-Slope, RSI-of-RSI)
- Hook in `kelly_sizer.py` als zusätzlicher `regime_factor` (0.5 / 0.75 / 1.0 / 0.75 / 0.4 je State)
- Hook in Discipline-Layer (`tools/discipline_engine.py`): Stress-State erlaubt nur Tier-A-Setups, Calm-Bull öffnet Tier-D
- Hook im Tagesreport: Top-Linie + Confidence-Band + erwartete Übergänge (3-day-ahead)

---

## 2. Acceptance-Criteria (must-have)

1. `regime/hmm_regime.py` (nicht mehr `_stub`) als Production-Modul mit:
   - `fit(X, n_states=5, lookback_days=504)` mit hmmlearn `GaussianHMM` + `BIC`/`AIC`-Auto-Tuning für n_states ∈ {3,4,5,6}.
   - `predict_state(features_today)` → `{state_id, label, confidence, transition_prob_3d}`.
   - `state_metrics()` → mean/std/sharpe pro State auf Trainings-Window.
   - Persistenz: `regime/hmm_model.pkl` (joblib, versioned), `regime/hmm_state_history.parquet` (Daily-Append).
2. `regime_factor`-Hook in `tools/kelly_sizer.py`:
   - Multiplikativ on-top von Vol-Scaling und V7.7-Recovery-Faktor.
   - Konfigurierbar via `--regime-mode {off, shadow, live}` (Default: `shadow`).
   - Im Live-Mode: hard-clamp `regime_factor ∈ [0.4, 1.0]`.
3. Backtest-Hook: `tools/v6_backtest_simulation.py` akzeptiert `--regime-overlay` und schreibt Performance pro State in `sim/_reports/regime_overlay_{date}.json`.
4. Discipline-Hook: `tools/discipline_engine.py` liest `regime/hmm_regime_current.json` und blockiert/erlaubt Tier-Push pro State (siehe State-Matrix unten).
5. **Test-Pyramide ≥ 25 Tests grün** in `tests/test_hmm_regime.py`:
   - Unit (≥ 12): Feature-Pipeline, BIC-Selector, Persistenz-Roundtrip, Confidence-Math, Transition-Matrix-Symmetrie, Edge-Cases (NaN-Handling, weniger als 252 d).
   - Integration (≥ 8): Backtest-Hook, Kelly-Sizer-Hook, Discipline-Hook, Report-Renderer, State-History-Append.
   - Snapshot-Tests (Golden Master, ≥ 5): Auf `lake/silver/spy_daily.parquet` Snapshot fitten und State-Sequenz gegen Soft-Hash prüfen (ändern sich Features/Algo, schlägt Test fehl + zwingt zu Decision).
6. Report-Renderer schreibt **2 Zeilen** statt 1: aktueller State + Top-2-erwartete Übergänge mit Wahrscheinlichkeiten.
7. Cron-Slot 06:30 CET (Mo-Fr) + 14:30 CET Re-Check (Mid-Day) über einen Scheduler (cron oder launchd). Re-Fit 1× pro Woche (Sonntag 22:00 CET).
8. Roll-back-Plan: ENV `REGIME_OVERLAY=off` deaktiviert alle Hooks ohne Code-Change. Stub-Pfad bleibt als Fallback erhalten (`hmm_regime_stub.py` nicht löschen).

---

## 3. State-Matrix (initial; Re-Tune in Backtest-Phase)

| State | Label | Sizing-Factor | Discipline-Tier-Cap | Setup-Filter |
|---|---|---:|---|---|
| 0 | **Stress** | 0.40 | nur Tier-A | MR-Scanner geblockt, nur Catalyst+Hedge |
| 1 | **Choppy** | 0.75 | Tier-A bis B | MR-Scanner OK mit erhöhtem ATR-Stop |
| 2 | **Normal** | 1.00 | alle Tiers | unverändert (Baseline) |
| 3 | **Trending-Bull** | 1.00 | alle Tiers | Momentum/PEAD-Setups bevorzugt |
| 4 | **Calm-Bull** | 0.85 | alle Tiers | MR-Scanner gedämpft (zu wenig Vol für MR-Edge); Long-Trend bevorzugt |

Begründung Calm-Bull-Dämpfung: V6.1 ist MR-Setup → braucht Vola. In Calm-Bull keine Edge.

---

## 4. Shadow-vs-Live-Plan

**Phase A: Shadow (3 Handelstage):**
- `--regime-mode shadow`. Schreibt `regime_factor_shadow` neben Live-Sizing in `sim/_reports/regime_shadow_{date}.json`.
- Der Tagesreport zeigt State + erwartete Sizing-Δ als reine Info-Zeile.
- Unit + Integration Tests müssen 25/25 grün sein BEVOR Phase B startet.

**Phase B: Side-by-Side (2 Handelstage):**
- 2 Tage paralleler Live-Sizer-Output: HK-Live vs. HK+Regime-Shadow nebeneinander loggen.
- Kelly-Sizer schreibt `decisions/sizer_compare_{date}.json` mit beiden Pfaden.
- Decision-Gate am Ende von Phase B: Wenn Δ-Distribution sinnvoll **UND** keine Daten-Health-Issues → Phase C grünes Licht.

**Phase C: Live (nach Phase B):**
- `--regime-mode live`. Hard-Clamp aktiv, Roll-back per ENV bleibt scharf.
- Retro nach einer Woche: Hit-Rate State-Calls vs. realisierter SPY-Vol checken; bei <60 % Hit-Rate → Rollback und R-1.1 Re-Tune.

---

## 5. Dependencies

- ✅ Stub-Output `regime/hmm_regime_current.json` (KW17 Tag 3): Format wird erweitert, nicht ersetzt (rückwärts-kompatibel).
- ✅ `lake/silver/spy_daily.parquet` (V7.9 DATA-ARCH-1): als Trainings-Quelle.
- ⚠️ FRED-Connector für Yield-Slope/Credit-Spread-Proxy: neu, ~30 min zu wrappen.
- ⚠️ NYSE-Breadth-Daten: entweder via FRED `BREADTH_NYSE` oder manueller Fallback (CSV-Snapshot, 1×/Woche aktualisiert).
- ❌ Report-Integration: wird **nicht** automatisch gemacht, sondern als manueller Schritt.

---

## 6. Out-of-Scope (bewusst)

- Kein Hidden-Markov-Switching-GARCH (zu komplex für diesen Schritt, spaeter neu planen).
- Kein Reinforcement-Learning-State-Selector.
- Kein Multi-Asset-HMM (nur SPY-zentriert; FX/Commodity-Erweiterung später).

---

## 7. Risiken

- **Look-ahead Bias** in Feature-Engineering: alle Features mit `t-1` einlesen, nie `t`. Test #6 der Snapshot-Test-Suite muss das prüfen.
- **State-Label-Permutation** zwischen Re-Fits: hmmlearn vergibt State-IDs nicht stabil. Mitigation: Post-Fit-Mapping nach mean(spy_log_ret) sortieren (kleinster = Stress, größter = Trending-Bull).
- **Vol-Cluster im Trainingsfenster** verzerrt State-Mean. Mitigation: Re-Fit-Trigger bei Δ-mean(VIX) > 30 % zur letzten Fit-Run.

---

## 8. Definition-of-Done

- [ ] AC 1-8 erfüllt
- [ ] 25/25 Tests grün
- [ ] Phase A + B + C durchlaufen
- [ ] Retro mit Hit-Rate-Tabelle dokumentiert
- [ ] Commit: `feat(regime): R-1 HMM Full-Stack Classifier (5-State + Sizing/Discipline-Hook)`

---

**Kontext:** Diese Spec entstand am 2026-04-27 als Nachtrag zu einem kurzen Verbesserungs-Sprint.
