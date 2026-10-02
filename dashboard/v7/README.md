# Cockpit V7: Start-Anleitung

**Live ab:** 2026-04-16 (Nacht)

## Was das ist

Ein Single-File HTML-Cockpit, das den ganzen Stack (Regime, Scanner, Portfolio, Edge-Monitor, System-Health) in einem Browser-Tab live spiegelt und per lokaler Flask-API steuerbar macht.

Forschungswerkzeug, keine Anlage- oder Steuerberatung und keine Anlageempfehlung (research tool, not investment or tax advice). Siehe [DISCLAIMER.md](../../DISCLAIMER.md).

## Dateien in diesem Ordner

| Datei | Zweck |
|---|---|
| `index.html` | Das Cockpit selbst. Vanilla HTML/JS, Chart.js via CDN. |
| `state.example.json` | Vorlage für `state.json` (wird mitgeliefert). |
| `state.json` | Persistenter lokaler Zustand: `kill_switch` (on/off), `dd_level` (0/3/5/8). Gitignored; `tools/dashboard_api.py` legt die Datei beim ersten Start aus `state.example.json` an. Nie Kontowerte eintragen. |
| `system-explorer.html` | Handgepflegte Architektur-Übersicht (Werkzeuge, Datenartefakte, Zeitplan). |
| `heartbeat.json` | Letzte Ausführung von Morgen-/Abend-Scheduled-Tasks. |
| `trades_snapshot.json` | Projection aus `trade_journal.sqlite` für Portfolio-Block. |

## So startest du

### Schritt 1: Flask-API starten (einmal pro Session)

```bash
cd <repo-root>
python3 tools/dashboard_api.py
```

Port ist `5757`. Bindet nur an `127.0.0.1`. Erster Start braucht Flask: `pip install flask` (oder `pip install -r requirements.txt`).

**Sicherheit:** Die lokale API niemals ueber `127.0.0.1` hinaus exponieren (kein `0.0.0.0`, kein Port-Forwarding, kein Tunnel). Sie hat keine Authentifizierung.

### Schritt 2: Cockpit öffnen

Einfachster Weg:

```bash
open dashboard/v7/index.html
```

Das lädt die Seite im Standard-Browser. Alle fetch()-Calls gehen per relativem Pfad gegen die JSON-Dateien im Repo und per absolutem gegen `http://127.0.0.1:5757`.

### Schritt 3: Daten initial synchronisieren

Einmal beim ersten Start den Portfolio-Snapshot aus SQLite regenerieren:

```bash
curl -X POST http://127.0.0.1:5757/state/sync
```

Danach hat Block B eine echte Equity-Kurve und offene Positionen.

## Refresh-Intervalle (pro Widget)

| Block | Intervall |
|---|---|
| A · Heute | 60 s |
| B · Portfolio | 60 s |
| C · Edge-Monitor | 10 min |
| D · System-Health | 30 s |
| E · Next-Up | 10 min |
| API-Heartbeat | 30 s |

Jedes Widget hat einen eigenen Timestamp („↻ 22:14:05") oben rechts.

## Steuerung (Action-Bar)

| Knopf | Tut | Endpunkt |
|---|---|---|
| Morgen-Scan | Triggert `tools/run_morning.sh` oder `tools/morning_briefing.py` oder `tools/daily_scanner.py` (erstes vorhandenes) | `POST /run/morning` |
| Abend-Tracking | Triggert `tools/run_evening.sh` oder `tools/evening_tracking.py` oder `tools/evening_exit_tracking.py` (erstes vorhandenes) | `POST /run/evening` |
| Trade loggen | Prompt-Dialog → BUY = Insert (mit optionalem Stop/Target/Score/Rationale), SELL = schließt zuletzt offene Position gleichen Tickers (`exit_date`+`exit_price`). Auto-Sync von `trades_snapshot.json`, Block B refresht sofort. | `POST /trade_log` |
| DD-Level setzen | Setzt `dd_level` in `state.json` (0/3/5/8) | `POST /state/set` |
| Pause All | Setzt `kill_switch=on/off` in `state.json` | `POST /state/set` |

**Niemals:** eine Order bei deinem Broker aus dem Cockpit absetzen. Das Cockpit sendet keine Orders, `trade_log` ist ausschließlich Journaling. Der Paper-Bot (`tools/exec_bot.py`) läuft getrennt davon und nur nach deiner Freigabe pro Ticker.

## URL-Parameter

- `?date=2026-04-15`: erzwingt ein bestimmtes Datum (für Historien-Review)
- Standard: heutiges Datum per `new Date()`

## Bekannte Einschränkungen (Phase 1+2)

1. **Live-Kurse**: Portfolio-PnL zeigt noch End-of-Day, nicht Intraday. Kommt in Phase 3 (DASH-5).
2. **Routing-Entscheidung im Block A**: nutzt schmale Platzhalter-Logik (nur `pre_score`). Wird in Phase 3 auf die volle Position-Sizing-Routing-Matrix umgezogen.
3. **trade_log Schema (ab 2026-04-17)**: nutzt die Spalten `ticker, side, qty, entry_price, entry_date, exit_price, exit_date, stop, target, score, rationale, created_at_utc`. Fehlende Spalten werden beim ersten Aufruf per `ALTER TABLE ADD COLUMN` defensiv nachgezogen: bestehende Daten bleiben. Teilverkäufe werden (noch) nicht unterstützt, SELL schließt die Position immer vollständig (Toast warnt bei qty-Mismatch). Volle SIM-1b-Integration (Realized-PnL, Kosten-Model) bleibt DASH-4b.
4. **Auto-Sync (ab 2026-04-17)**: Jeder erfolgreiche `/trade_log`-Call regeneriert `trades_snapshot.json` automatisch. Der manuelle `curl -X POST /state/sync` ist nur noch für den Erststart oder nach Backend-Änderungen am SQLite nötig.
5. **CORS**: nur lokale Origins (`localhost`, `127.0.0.1`, `file://` = `null`) sind erlaubt. Requests mit fremdem Origin oder Host-Header bekommen `403`, bevor ein Handler laeuft.
6. **Keine Auth**: lokaler Zugang reicht, solange der Port nur auf 127.0.0.1 bindet. Nie nach aussen exponieren.

## Wenn was kaputt ist

| Symptom | Ursache | Fix |
|---|---|---|
| „API: off" oben rechts | Flask läuft nicht | `python3 tools/dashboard_api.py` in Terminal |
| Blöcke zeigen „:" | JSON-Dateien fehlen für heutiges Datum | Morgen-Scan laufen lassen |
| Heartbeat > 60 min | Scheduled-Task hat nicht geschrieben | Prüfe `tools/dashboard_api.py` Log + Heartbeat neu schreiben |
| Chart leer | `trades_snapshot.json` noch Stub | `curl -X POST http://127.0.0.1:5757/state/sync` |

## Verhältnis zu V6.10.2

Der fruehere statische V6.10.2-Spiegel ist nicht Teil dieses Repositories. V7 ist das einzige Cockpit.

## Weiterentwicklung

- **Phase 3 (DASH-5):** Live-Kurs-Poller für echtes Intraday-PnL.
- **Phase 4 (DASH-6):** Mobile-View + dunkleres Theme-Polish.
- **Niemals:** Mehr als 5 Kernblöcke. Mehr als 3 Klicks pro Information. Mehr Komplexität ohne kompensierendes Simplify-Ticket.
