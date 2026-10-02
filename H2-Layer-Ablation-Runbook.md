# H2-Runbook: Layer-Ablation (Edge validieren & vereinfachen)

**Zweck:** Hebel **H2** (Edge validieren, nach H1 = Daten-Coverage) ausführen: empirisch entscheiden, **welche der 6 Boost-Layer echten Edge tragen** und welche raus müssen, plus die Score-Schwellen (Gate- und Top-Schwelle) auf realisierten Returns neu eichen.

**Bausteine (gebaut + getestet, 7/7):**
- `tools/build_ablation_panel.py`: erzeugt das Daten-Panel aus `scan_*.json` + Forward-Returns.
- `tools/layer_ablation.py`: der Analyzer (KEEP/KILL je Layer + Threshold-Sweep).
- `tests/test_layer_ablation.py`: Selbsttests (Signal-vs-Rauschen wird korrekt erkannt).

> Braucht Netzwerkzugang (Forward-Returns kommen über `yfinance`). Die reine Logik ist offline getestet.

---

## Schritt 1: Panel erzeugen (mit Netzwerk)
Braucht möglichst viel Scan-Historie (akkumuliert täglich) + Kursdaten:
```bash
cd <repo-root>
python3 tools/build_ablation_panel.py --start 2025-06-01 --end 2026-05-31 \
    --horizon 5 --tiers A,B --out panel.json
```
- `--horizon 5` = Hold-Fenster in Handelstagen (Connors 3-5; ruhig auch 3 testen).
- `--tiers A,B` = nur liquide Namen (leer lassen = alle).
- Output: `panel.json` ({base_score, je-Layer-Adjust, forward_return, regime_ok} pro Kandidat).

## Schritt 2: Ablation rechnen
```bash
python3 tools/layer_ablation.py --panel panel.json --top-pct 20
```
Gibt drei Blöcke aus (Konsole + `tools/layer_ablation_<datum>.json`):
1. **Konfigurationen**: BASE / BASE+REGIME / BASE+je-Layer / FULL, je mit `corr(Score, Return)`, Top-20%-Expectancy, WR, Per-Trade-Sharpe.
2. **Layer-Verdikt**: Δ Top-K-Expectancy vs BASE → **KEEP / KILL / NEUTRAL** je Layer.
3. **Threshold-Sweep**: pro Score-Schwelle: n Trades, avg Return, WR.

## Schritt 3: Entscheiden (das ist der eigentliche H2-Schritt)
- **Score umbauen:** in `daily_scanner.py` nur die **KEEP**-Layer behalten, KILL-Layer-Gewicht auf 0. Faustregel Strategie: am Ende **2-3 Faktoren**, nicht 6.
- **Schwellen neu eichen:** aus dem Sweep den Punkt wählen, wo Expectancy **und** WR stark sind **und** genug Kandidaten pro Woche übrig bleiben, um die Schwelle statistisch zu prüfen. Das ersetzt die geratenen 7,0 / 8,5 (die aktuell über dem Backtest-Ceiling 7,3 liegen).
- **Gegencheck Over-Fitting:** ≤ 3-5 Parameter, Walk-Forward-Effizienz > 0,5; ein Layer, der nur in einem kurzen Fenster KEEP ist, ist verdächtig. Re-Run monatlich.

---

## Erwartung & ehrliche Grenzen
- **Prior aus den Daten:** `news` ist live auf 0 (GDELT dunkel) → **nicht evaluierbar**, faktisch KILL bis die Quelle lebt. `options_flow`/`market_pcr` degradiert (CBOE) → wahrscheinlich KILL. `sector_rs`, `insider`, `mtf` sind die echten Testkandidaten. Das deckt sich mit einer früheren statischen Ablation.
- **Datenqualität first:** Solange die Scans degradiert sind (Tier-A-only, News dunkel), ist das Panel dünn. Der Wert steigt, sobald **H1 (Coverage)** steht, dann je Tag ~250 statt 68 Zeilen. **Reihenfolge bleibt: erst H1, dann H2.**
- **Min. Stichprobe:** Pro Layer-Verdikt sollten im Top-K mind. ~30-50 Trades stehen, sonst ist das Δ Rauschen. Lieber `--top-pct 30` bei dünnem Panel.

## Definition of Done (H2)
- [ ] Panel ≥ 6 Monate Scan-Historie, Tier A+B, forward_return gefüllt.
- [ ] Ablation gerechnet; KEEP/KILL je Layer dokumentiert.
- [ ] Score in `daily_scanner.py` auf KEEP-Layer reduziert (Selbsttest grün).
- [ ] Schwellen aus Sweep neu gesetzt; im Walk-Forward gegen die alte Version verglichen.
- [ ] Erst danach H4/H5 (Positionsgröße) angehen.
