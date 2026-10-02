# E11F: Inverse-ETF & VIX-Produkt Filter

**Version:** V1 (2026-04-17)
**Zweck:** Logic-Flaw beseitigen: Mean-Reversion-Scanner triggert auf "oversold" bei inversen ETFs und VIX-Produkten. Bei Long-Regime = Widerspruch.

**Freeze-Status:** Dieser Filter ist **freeze-konform**. Keine Scoring-Logik berührt. Nur Ticker-Ausschluss über bestehenden `tier = "X"`-Mechanismus. Der Scanner filtert `X`-Tier bereits in `tools/daily_scanner.py` heraus (Filter `r["tier"] != "X"` vor dem H1-Timing-Pass und vor der Top-Auswahl).

---

## 1. Problem (aus RETRO-SIGNAL-Analyse)

An mehreren Scan-Tagen im April 2026 erreichten inverse ETFs (SOXS, SPXU, SQQQ) vordere Ränge unter den Scan-Kandidaten, teils die ersten Plätze. Ticker-genaue Tageslisten sind hier bewusst nicht dokumentiert (keine Kandidatenlisten, keine Empfehlungen).

**Warum das ein Logic-Flaw ist:**
Inverse ETFs (3× Short Semi, Short S&P, Short Nasdaq) steigen, wenn der Markt fällt. Ein **oversold**-Signal (niedriger RSI2, hoher Connors-Score) bedeutet: der inverse ETF ist gefallen = der Markt ist gestiegen. In einem **Trending-Bullish-Regime** ist das **kein** sinnvolles Long-Setup für den inversen ETF, sondern das **Gegenteil** (der Markt läuft weiter bullisch, Inverse weiter runter).

Gleiche Logik bei VIX-Produkten (UVXY, SVXY): sie folgen nicht Preisbewegung einzelner Aktien, sondern Markt-Volatilität. Mean-Reversion-Scanner-Logik passt nicht.

Auch der Shadow-Recommender H3 (Setup-Override) zählte an einem dieser Tage überwiegend inverse ETFs zu seinen Passes.

---

## 2. Was geändert wurde

**Datei:** `tools/universe.csv`

**5 Ticker in Tier `C` → `X` umklassifiziert:**

| Ticker | Beschreibung | Grund |
|--------|--------------|-------|
| SOXS | 3× Short Semiconductor | Logic-Flaw bei Long-Regime |
| SPXU | 3× Short S&P 500 | Logic-Flaw bei Long-Regime |
| SQQQ | 3× Short Nasdaq 100 | Logic-Flaw bei Long-Regime |
| UVXY | 2× VIX Futures | MR-Logik passt nicht auf Vola-Produkte |
| SVXY | Short VIX | MR-Logik passt nicht auf Vola-Produkte |

**Was NICHT geändert wurde (absichtlich):**

| Ticker | Beschreibung | Warum drin bleibt |
|--------|--------------|-------------------|
| SOXL | 3× Long Semiconductor | Leveraged-Long: MR-Logik hat dieselbe Richtung wie der Basiswert |
| TQQQ | 3× Long Nasdaq 100 | Leveraged-Long: MR-Logik hat dieselbe Richtung wie der Basiswert |
| UPRO | 3× Long S&P 500 | Leveraged-Long: MR-Logik hat dieselbe Richtung wie der Basiswert |

---

## 3. Wirkung (Dry-Check)

Im Dry-Check des nächsten Scans fielen die inversen ETFs aus den vorderen Rängen; andere Ticker rückten nach. Ticker-genaue Ranglisten sind hier bewusst nicht dokumentiert (keine Empfehlungen).

---

## 4. Rollback-Plan

Wenn sich herausstellt, dass E11F falsch war (z. B. in einem Bear-Regime wären Inverse-ETFs tatsächlich relevant): in `tools/universe.csv` das Tier der 5 Ticker wieder von `X` auf `C` setzen (oder den entsprechenden Commit zurücknehmen).

**Rollback-Trigger:** wenn das Regime auf `high_vol_krise` oder `extreme_fear_thrust` kippt und der Shadow-Recommender für die 5 Ticker dokumentiert bessere forward_5d-Returns liefert als die Baseline.

---

## 5. Follow-Up

### E11F-PLUS (wenn benötigt, nicht akut)
- Dynamische Regime-Aktivierung: in Bullish-Regimes Inverse ausgeschlossen, in Bearish-Regimes wieder rein.
- Aufwand: M (Regime-abhängige Universe-Maskierung).
- Nur bauen, wenn ein bearishes Regime Inverse-ETFs erfordert.

---

## 6. Zusammenfassung

- **5 Ticker auf Tier X** gesetzt: SOXS, SPXU, SQQQ, UVXY, SVXY.
- **Leveraged-Long bleibt im Universe** (SOXL, TQQQ, UPRO).
- **Freeze-konform:** keine Scoring-Änderung, nur Ticker-Ausschluss über den bestehenden Tier-X-Mechanismus.
- **Rollback:** Tier in `tools/universe.csv` zurücksetzen.
