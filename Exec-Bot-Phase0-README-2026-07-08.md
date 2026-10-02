# Exec-Bot Phase 0 (Paper): Setup & Tagesablauf

> **Paper trading only. Not investment advice. No warranty.** Kein LLM im Order-Pfad. Kein echtes Geld in Phase 0.

## Was gebaut wurde

| Datei | Zweck |
|---|---|
| `tools/broker_alpaca.py` | Alpaca-REST-Adapter, **Live-URL doppelt gesperrt** (Config `allow_live` UND Env `EXEC_BOT_ALLOW_LIVE=1` nötig, Phase 0 kann Live nicht aus Versehen). Bei einem Live-Endpoint erscheint zusätzlich eine deutliche Konsolen-Warnung. Selftest offline. |
| `tools/exec_bot.py` | Plan → Approve → Run → Track → Reconcile. Konsumiert NUR bestehende SSoT-Artefakte (Scan, V7.36-Gate, Regime, universe.csv). Selftest offline. |
| `tools/exec_bot_config.json` | Harte Limits (Beispielwerte): max 3 Orders/Tag, max 3 Positionen, ~1.000 $/Trade × Regime-`size_multiplier`, long-only, `mode=paper`, `allow_live=false`. |
| `tools/.env.example` | Vorlage für deine Paper-Keys (`tools/.env` ist gitignored, niemals committen). |

Sicherheits-Kern: **Stop + Target liegen als Bracket serverseitig beim Broker** und überleben Mac-Schlaf und Bot-Absturz. Kill-Switch: `python3 tools/exec_bot.py --kill` (oder Datei `tools/EXEC_BOT_KILL` anlegen) blockt alles und storniert offene Orders.

## Dein Setup (einmalig, ~15 Min)

1. [alpaca.markets](https://alpaca.markets) → Sign up (Paper-Konto mit virtuellem Guthaben, keine Einzahlung nötig).
2. Dashboard → auf **„Paper"** umschalten → **API Keys → Generate New Keys** → Key ID + Secret kopieren. Die Keys wie ein Passwort behandeln: Wer sie hat, kann mit dem Konto handeln. `tools/.env` privat halten (`chmod 600 tools/.env`), Keys nach jedem Leak neu erzeugen, nie Live-Keys in Tests.
3. `tools/.env.example` nach `tools/.env` kopieren, Keys eintragen.
4. Test: `python3 tools/broker_alpaca.py --probe` → erwartete Ausgabe `PROBE OK paper account status=ACTIVE …` (braucht Netz und Keys, nicht in CI ausführen).

## Tagesablauf (Paper-Phase)

```bash
# MORGENS, nach dem Morgen-Scan (Gate V7.36 muss rc=0 sein):
python3 tools/exec_bot.py --plan                    # baut Orderplan, zeigt Kandidaten
python3 tools/exec_bot.py --approve --tickers AAA   # DEIN Ein-Tap: nur was du freigibst
python3 tools/exec_bot.py --run                     # submitted Bracket-Orders (paper)

# ABENDS:
python3 tools/exec_bot.py --track                   # Fills, Slippage, Hard-Exit T+3, Tagesbericht

# WÖCHENTLICH:
python3 tools/exec_bot.py --reconcile               # Ledger == Broker?
python3 tools/exec_bot.py --gate-report             # Fortschritt Richtung Paper-Gate
```

Der Plan-Schritt ist bewusst mechanisch: **deine Freigabe bleibt der Qualitäts-Filter.** Ein mechanisch korrekter Kandidat (Regime erlaubt das Tier, Score über der Schwelle) kann trotzdem ein Falling-Knife-Typ sein, den du im Zweifel verwirfst. Approve heißt: du bleibst der Kopf, der Bot die Hand.

## Paper-Gate (operative Prüfpunkte)

Diese Kriterien prüfen nur, ob der Bot technisch sauber läuft (`--gate-report`, Feld `paper_gate_ops_checks_passed`). Sie sagen nichts darüber aus, ob die Strategie Geld verdient, und sind kein Grund, live zu handeln.

- ≥ 15 gefüllte Paper-Trades
- 0 Order-Fehler im Ledger
- Reconcile: Ledger == Broker zu 100 %
- Slippage gemessen und dokumentiert (`--gate-report`)
- Mindestens 4 Wochen Laufzeit

Live-Handel ist technisch möglich, aber gesperrt: Er braucht die doppelte Freischaltung (`allow_live=true` in der Config und `EXEC_BOT_ALLOW_LIVE=1`, siehe README, Abschnitt "Live trading"). Er geschieht vollständig auf eigenes Risiko, mit echtem Geld, und nichts hier ist eine Anlageberatung.

*Selftests: `python3 tools/broker_alpaca.py --selftest` · `python3 tools/exec_bot.py --selftest` (beide offline, ohne Keys).*
