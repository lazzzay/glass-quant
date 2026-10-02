# Drawdown-Eskalations-Protokoll V1

**Version:** V1 (2026-04-17)
**Zweck:** Mechanisches Drawdown-Management. Bei einer Strategie mit knappem Vorteil (Profit-Faktor nahe 1) ist DD-Kontrolle existenziell, ohne klare Stufen entscheidet Emotion statt Regel. Dieses Dokument macht die Eskalations-Logik zur **Default-Option**, nicht zur Ausnahme.


---

## 1. Grundprinzipien

1. **Stufen sind Hard-Switches, keine Vorschläge.** Erreicht der Kontostand die Stufe, wird die Regel automatisch aktiv.
2. **Kein Override durch "aber heute ist Setup gut".** Der Zweck der Stufe ist genau dieses Gefühl zu unterdrücken.
3. **Drei Bücher** werden separat gemessen: **Tagesbuch** (heutige PnL), **Wochenbuch** (Mo-Fr PnL), **Portfolio** (All-Time-High-Drawdown).
4. **Messung am Ende des Handelstages** (20:00 CET), nicht intraday, sonst werden normale Intraday-Schwankungen zu Fehl-Auslösern.
5. **Zurücksetzen nur mit Datum**, nicht mit Gefühl. Wenn Stufe 1 erreicht ist, bleibt sie bis zum nächsten Handelstag 20:00-Check aktiv.

---

## 2. Eskalations-Stufen

### Stufe 1: Tagesbuch −3 %

**Trigger:** Tagesbuch-PnL erreicht am Ende des Handelstages (oder intraday-realisiert) −3 % vom Start-Buchwert.

**Aktion:**
1. **Keine neuen Entries** bis morgen 08:00 CET.
2. **Pflicht-Review der letzten 5 geschlossenen Trades**: Wurde die Pre-Trade-Checkliste vollständig abgehakt? Wurde ein Kill-Switch ignoriert? Wurde Position-Size korrekt berechnet?
3. **Review-Dokument** `DevReport_YYYY-MM-DD-DD-Stufe1.md` anlegen mit: Trigger-Uhrzeit, PnL-Stand, Trade-IDs der letzten 5, eine Zeile pro Trade "Was lief planmäßig / was nicht".
4. **Bestehende offene Positionen** bleiben aktiv (Stops gelten weiter). Nicht in Panik glatt stellen.

**Reset:** Am nächsten Handelstag 08:00 CET automatisch. Kein manueller Reset nötig.

---

### Stufe 2: Wochenbuch −5 %

**Trigger:** Kumulierte PnL Mo-Fr (oder teilweise Woche) erreicht −5 % vom Wochen-Start-Buchwert.

**Aktion:**
1. **1 Handelstag Pause.** Keine Entries, kein Exits zusätzlich zum ATR-Stop.
2. **Weekly-DevReport** (`DevReport_YYYY-Wxx-Stufe2.md`): Trade-Liste der Woche, Hit-Rate, R-Multiplier-Verteilung, welcher Layer hat versagt (Scoring, Regime, Sizing, Timing)?
3. **Regime-Check:** Hat sich das Markt-Regime in der Woche geändert und wurde darauf reagiert? Prüfung gegen `regime_*.json` der 5 Tage.
4. **Wenn bereits 2 Wochen in Folge Stufe 2** → eskaliere zu Stufe 3 ohne weiteren Trigger.

**Reset:** Nach 1 Handelstag Pause UND Weekly-DevReport fertig. Erst dann Entries wieder möglich.

---

### Stufe 3: Portfolio-DD −8 %

**Trigger:** Gesamt-Portfolio-Buchwert liegt −8 % unter All-Time-High (dem höchsten bisher erreichten Buchwert).

**Aktion:**
1. **1-Wochen-Moratorium.** Keinerlei neue Entries für 5 Handelstage. Bestehende Positionen laufen mit Stops aus.
2. **Root-Cause-Analyse** (Dokument `Drawdown-Root-Cause-YYYY-MM-DD.md`):
   - Trade-by-Trade-Log der letzten 20 geschlossenen Trades.
   - Welche Makro-/Regime-/Strategie-Hypothese lag jedem Trade zugrunde?
   - Sind mehrere Trades derselben fehlerhaften Hypothese gefolgt (Cluster-Fehler)?
   - Hat sich eine Systemgröße geändert (Layer-Gewicht, Sizing, Regime-Klassifikation), die nicht dokumentiert wurde?
3. **Scoring-Sanity-Check:** Läuft der aktuelle `scan_*.json` mit realistischen Scores? Vergleich Top-30 heute vs. Top-30 am Tag des All-Time-High.
4. **Externes Sparring** mit einer externen Person: Eine Stunde strukturiertes Feedback-Gespräch über die RCA-Befunde.

**Reset:** Nach 5 Handelstagen Pause UND RCA fertig UND Scoring-Sanity-Check OK. Entries starten mit halber Position-Size für 5 weitere Tage (Stufe-3-Rekonvaleszenz).

---

### Stufe 4: Portfolio-DD −15 %

**Trigger:** Gesamt-Portfolio-Buchwert liegt −15 % unter All-Time-High.

**Aktion:** **System-Stop.** Das Trading-System wird für mindestens 30 Kalendertage außer Betrieb genommen. In dieser Zeit:
- Keine Live-Trades.
- Scanner läuft weiter (Shadow-Mode), Daten werden weiter gesammelt.
- Audit-Workshop mit externer Perspektive.
- Entscheid: System reparieren, pausieren, oder einstellen.

Diese Stufe sollte **nie** aktiviert werden. Sie existiert als letzte Sicherheitsschranke. Wenn sie einmal aktiv ist, wird das Verfahren (inkl. Sparring mit einer externen Person, Reanalyse aller Layer) einmalig dokumentiert und dient ab dann als Blueprint.

---

## 3. Messung und Buchhaltung

### 3.1. Buchwerte

| Buch | Definition | Messpunkt |
|------|-----------|-----------|
| Tagesbuch | Σ Realisierte + unrealisierte PnL heute | täglich 20:00 CET |
| Wochenbuch | Σ Realisierte PnL Mo 08:00 bis Fr 22:00 | täglich 20:00 CET (rolling) |
| Portfolio | Gesamt-Kapital auf dem Konto + offene Positionen (MtM) | täglich 20:00 CET |
| All-Time-High | max(Portfolio) seit Systemstart | fortlaufend upgedatet |

### 3.2. Berechnungs-Automatisierung

- `tools/trade_journal.py` führt die Portfolio-Serie.
- Neues Kommando `tools/dd_check.py --today` (noch zu bauen) liefert die 4 Stufen-Status auf einem Blick. Aufwand S, passt in V6.9.
- Bis Skript existiert: Manuell in `DevReport_YYYY-MM-DD.md` berechnen und eintragen.

### 3.3. Historischer Check

Die Logik wurde **nicht rückwirkend** auf 1997-2026-Backtest angewendet (das käme mit Task #14 Walk-Forward). Daher ist die "5 % / 8 %"-Kalibrierung derzeit **theoriebasiert**, nicht datengetrieben. Nach 6 Monaten Live-Betrieb re-kalibrieren:

- Wenn Stufe 1 mehr als 15 × pro Jahr auslöst → Schwelle zu eng.
- Wenn Stufe 3 nie in 12 Monaten auslöst obwohl DD-Phasen sichtbar → Schwelle zu weit.

---

## 4. Integration

Die Stufen lassen sich in das Tooling einbauen (zum Beispiel ein Drawdown-Check in `tools/` und eine Kachel "DD-Status" in `tools/dashboard_api.py`). Bis dahin werden sie manuell angewendet.

---

## 5. Beispiel-Szenario

**Starttag 2026-05-04, Portfolio indexiert auf 100, All-Time-High 105.**

- 2026-05-05: −2,5 % Tag → **keine Stufe**.
- 2026-05-06: −3,2 % Tag → **Stufe 1** ausgelöst. Keine neuen Entries. Review läuft.
- 2026-05-07: Reset (neuer Tag). Tag +0,5 %.
- 2026-05-08: Woche kumuliert −5,2 % → **Stufe 2** ausgelöst. 1 Tag Pause (05-11).
- 2026-05-11: Pause-Tag. Weekly-DevReport fertig.
- 2026-05-12: Entries wieder möglich, 50 % Size (Rekonvaleszenz).
- 2026-05-15: Portfolio bei 96 → DD vom ATH (105) = −8,6 % → **Stufe 3** ausgelöst. 5 Tage Pause, RCA in Arbeit.

---

## 6. Anti-Muster

- **"Nur dieses eine Setup ist zu gut, überspringe ich die Stufe 1."** Nein. Die Stufe **existiert** für solche Momente.
- **"Stufe 1 ist zu streng, ich mach sie auf −4 %."** Kein Ad-hoc-Änderungen. Änderung nur über das Rekalibrierungs-Protokoll (Abschnitt 3.3), nach 6 Monaten mit Daten.
- **"Intraday bin ich −3,5 %, aber bis zum Close ist es sicher wieder besser."** Lass es. Die Regel greift am Close, aber intraday über die Stufe zu fliegen ist das Frühwarn-Signal, dass Sizing zu groß war.
- **"Mein Wochenbuch ist −5 % weil einer meiner Stops hart getroffen wurde, nicht weil System kaputt ist."** Irrelevant für die Regel. Stop-Treffer ist Teil des Systems. Wenn −5 % erreicht sind, Pause kommt, unabhängig vom Grund.

---

## 7. Zusammenfassung

- **4 Stufen:** −3 % Tag (Review), −5 % Woche (Pause-Tag), −8 % Portfolio (1-Woche + RCA), −15 % Portfolio (System-Stop).
- **Messpunkt 20:00 CET**, Reset automatisch oder mit Dokument.
- **Kein manueller Override.** Die Regel ersetzt das Bauchgefühl, sie ist nicht Diskussionsgegenstand.
- **Rekalibrierung** nach 6 Monaten Live-Betrieb, vorher theoriebasiert.
- **Integration** in Tooling und Reports, sobald vorhanden.

---

**Querverweise:**
- `tools/trade_journal.py`: PnL-Serie
- Idee: `tools/dd_check.py` (noch nicht umgesetzt)
