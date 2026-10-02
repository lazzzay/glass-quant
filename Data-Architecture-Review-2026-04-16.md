# Data-Architecture-Review 2026-04-16

**Blickwinkel:** Bewährte Muster aus dem Data-Lake- und Lakehouse-Engineering (Databricks, Apache Hadoop/HDFS, Apache Spark, S3-Objektspeicher, Snowflake, Data Mesh nach dem Vorbild der SBB)
**Auftrag:** Die Datenstruktur von Daily Invest (Stand April 2026) mit diesen Mustern bewerten und einen priorisierten Upgrade-Pfad Richtung V10+ skizzieren, der zu den Randbedingungen passt (Free-Stack, lokal lauffähig, solo betrieben, Kosten-Veto).

---

## 0. Executive Summary

Der heutige Stand ist **zwei nebeneinanderlaufende Welten**, die sich nicht kennen:

- Eine **historische Welt** (`data_lake/`, 142 MB, 212 Parquet-Serien, 30-Jahre-Tiefe). Gut gemeint, aber **keine echte Lakehouse-Architektur**: keine Zonen (Bronze/Silver/Gold), keine Zeit-Partitionierung, kein ACID-Layer, keine Query-Engine, kein Schema-Contract.
- Eine **operative Welt** (`tools/*.json`, eine Datei pro Tag pro Source). Das ist **pre-Lake**: flacher Daten-Sumpf, schreibt täglich neue JSONs, kein Merge in den Lake, keine Retention, kein Contract.

Ergebnis: Alles funktioniert irgendwie, solange der Maintainer einmal pro Tag kurz reinschaut. Sobald das System auf V10+ skalieren soll (mehr Sources, Multi-Horizon, Backtest-Varianten, eigenständige Produktivität), wird diese Architektur zum Flaschenhals. Der heutige stille CBOE-/yfinance-Ausfall, den erst `data_health.py` gefunden hat, ist genau das Symptom.

**Kernempfehlung:** Ein pragmatischer, sehr lokaler Lakehouse-Refactor in drei Wellen, mit **DuckDB + Parquet** als Kern-Stack (stdlib-nah, MIT-Lizenz, keine Cloud). Kein Databricks, kein Hadoop-Cluster. Das Konzept der Großen, übersetzt in einen Single-Disk-Stack, der die "lokale Software"-Vision des Projekts aktiv stützt.

---

## 1. IST-Diagnose

### 1.1 Databricks (Lakehouse-Paradigma): Medaillon-Zonen fehlen

Databricks' Lakehouse-Grundidee: **eine einzige Speicherschicht**, strukturiert in Zonen nach Reinheit:

- **Bronze** = Raw, immutable, schema-as-captured (das was reinkam)
- **Silver** = Cleaned, conformed, one-schema-per-entity
- **Gold** = Business-ready, aggregiert, feature-ready für Konsumenten

**IST-Zustand:** Wir haben ausschließlich Bronze-ähnliche Artefakte, und selbst die nicht konsistent. `data_lake/prices/daily/AAPL.parquet` ist Bronze (direkt von yfinance). `tools/scan_2026-04-16.json` ist auch Bronze (Scanner-Output), aber es liegt in einem ganz anderen Verzeichnis, in einem anderen Format, unter anderen Regeln. **Silver existiert nicht.** **Gold existiert nicht.**

**Folge:** Jeder Scoring-Lauf muss die Rohdaten on-the-fly aus JSON parsen, joinen, normalisieren. Jeder Backtest muss die Parquets aus `data_lake/` laden und separat Feature-Engineering fahren. Kein Zwischenartefakt ist wiederverwendbar. Das verletzt Databricks-Prinzip #1.

### 1.2 Apache Hadoop / HDFS: Partitionierung fehlt

Hadoop lebt von **Time-based Directory-Partitioning**: `/data/events/year=2026/month=04/day=16/part-0000.parquet`. Der Vorteil ist nicht Hadoop-spezifisch: er gilt für **jedes** File-basierte Storage-Layout: Query-Engines können Partitions pruning machen, alte Partitions lassen sich archivieren, Re-Runs berühren nur ein Blatt.

**IST-Zustand:** `data_lake/prices/daily/AAPL.parquet` ist **eine einzige Datei für 30 Jahre** AAPL. Sobald der Lake wächst, wird jeder Re-Download die komplette Serie neu schreiben. Und: `tools/scan_2026-04-16.json` ist zwar tageweise, aber nicht partitioniert: es ist ein Dateiname-Pattern, kein echtes Partitionsfeld.

**Folge:** DuckDB / Polars können die elegante `SELECT * FROM 'lake/**/*.parquet' WHERE year=2026` nicht fahren. Retention-Policies lassen sich nicht sauber formulieren. Die Bronze-Zone ist *nicht idempotent inkrementell*, sondern full-replace.

### 1.3 Apache Spark: Compute-Layer fehlt

Spark ist in-memory, lazy, spaltenorientiert. Im Solo-Kontext braucht niemand einen Spark-Cluster, aber **das Muster** ist relevant: keine Python-for-loop über tausende Ticker mehr, sondern deklarative Queries gegen eine Tabelle.

**IST-Zustand (April 2026):** Der Scoring-Code (`tools/daily_scanner.py`) ist Pandas-zentriert mit `for ticker in universe`-Schleifen, JSON-Ladezyklen und Merge-Loops. DuckDB, Polars und PyArrow waren zu diesem Zeitpunkt nicht installiert. (Inzwischen stehen DuckDB und PyArrow in `requirements.txt`; Polars nicht.)

**Folge:** Skalierung auf 2000+ Assets, 5+ neue Score-Layer (V7-V10) wird an Kompute-Performance scheitern, lange bevor sie an Daten-Qualität scheitert.

### 1.4 AWS S3 / Objektstorage: Separation of Storage & Compute fehlt

Die wichtigste AWS-Lektion: **Daten sind getrennt vom Code, der sie verarbeitet.** S3-Buckets sind dumme Blobs, Lambda/Athena/EMR sind Consumers. Das ermöglicht mehrere Consumers auf demselben Storage, ohne Doppel-Speicherung.

**IST-Zustand lokal:** `<repo-root>/` vermischt Code (`tools/*.py`), Rohdaten (`tools/*.json`), den Lake (`data_lake/`), Reports (`*.md`), Outputs (`Trading-Dashboard-V6.1.html`) und das Journal (`trade_journal.sqlite`). **Ein Verzeichnis für alles.** Wenn der Projekt-Ordner wandert oder umbenannt wird, bricht fast alles. Wenn ein zweiter Consumer (z.B. ein Web-Dashboard) auf dieselben Daten zugreifen soll, gibt es keinen sauberen Kontrakt.

**Folge:** Die Vision "lokale Software" scheitert daran, dass heute nicht klar ist, was die "Daten" sind (portabel mitnehmen) und was der "Code" ist (ersetzbar, versionsfähig). Es gibt kein klares Daten-Artefakt, das man in eine Electron/Tauri-App oder einen Docker-Container verfrachten könnte.

### 1.5 Snowflake: Query-Isolation & Zero-Copy-Cloning fehlen

Snowflake's Killer-Feature: jede Query ist transaktional konsistent, jeder "Branch" eines Datensatzes ist instantan verfügbar ohne Duplikat.

**IST-Zustand:** Wenn morgens mehrere Konsumenten (Tagesreport, Kandidatenliste, Dashboard) gleichzeitig Scanner-JSONs lesen und der Scanner sie parallel überschreibt, gibt es keine Garantie: es liegt reines Glück, dass bisher nichts kaputtging. Kein WAL, kein MVCC, keine Transaktion.

**Folge:** Sobald mehrere Tasks parallel laufen (was mit V7+ passieren wird: Scanner + Re-Scan + Backtest + Dashboard-Update), wird es Race-Conditions geben. **SQLite im Journal hat das ansatzweise**, aber der Rest der Daten hat es gar nicht.

### 1.6 Data Mesh (SBB-Schule): Domains & Contracts fehlen

Die SBB-Adaption von Data Mesh hat drei Kernelemente: **Data as Product** (jede Domain liefert ihre Daten mit SLA), **Domain-Ownership** (wer produziert, ist zuständig für Qualität), **Federated Governance** (zentrale Regeln, dezentrale Umsetzung).

**IST-Zustand:** Es gibt keine definierten Domains. `insider_feed.py` schreibt `insider_*.json`, aber nirgendwo ist dokumentiert, was das **Schema-Contract** ist (welche Felder, welche Typen, welche Werte für `.sources`). `daily_scanner.py` liest es blind, und wenn das Schema drift'tet (z.B. `sources.openinsider` wird zu `sources.open_insider`), bricht alles still. Das ist exakt das Gegenteil von Data-Mesh-Contracts.

**Folge (bereits eingetreten):** Der heute entdeckte Insider-Merge-Mismatch (94 Signale, nur 4/30 matchen) ist ein klassischer Contract-Bruch zwischen Producer (`insider_feed.py`) und Consumer (`daily_scanner.py`). Mit einem Data-Mesh-Contract + Schema-Registry hätte der erste Drift-Tag ein rotes Ticket produziert, bevor er ins Scoring kam.

---

## 2. Gesamtbewertung

| Dimension | IST | Ziel (V10+) | Gap |
|---|---|---|---|
| Storage-Layout | Gemischt (JSON/Parquet/SQLite/CSV) in einem Verzeichnis | Klare Zonen Bronze/Silver/Gold, getrennt von Code | **Groß** |
| Partitionierung | Keine (1 Datei = 1 Serie, 1 Datei = 1 Tag) | `year=/month=/day=` oder Hive-style | **Groß** |
| Query-Engine | Pandas-Loops | DuckDB / Polars / PyArrow (Stand April 2026 nicht installiert; DuckDB und PyArrow inzwischen in `requirements.txt`) | **Mittel** (1 Tag) |
| Schema-Contracts | Implizit in Code | Explizit als YAML/JSON + Validator | **Mittel** |
| Transaktionalität | Raceable File-Writes | ACID-Layer (Delta-Lake / Iceberg / DuckDB-WAL) | **Mittel** |
| Lineage & Catalog | Keiner | Manifest + Graph (wer liest/schreibt was) | **Mittel** |
| DQ-Gates | Punktuell (`data_health.py` seit heute) | In jeder Zone (Bronze→Silver→Gold) | **Klein** (Framework da) |
| Retention/Archiv | Keine (alles bleibt ewig) | TTL-Policies pro Zone | **Klein** |
| Portabilität | Gebunden an absoluten Pfad | Relocierbar (env-var-basiert) | **Klein** |

**Zusammengefasst:** Die großen Lücken sind **Zonen, Partitionierung, Contracts**. Die mittleren sind **Query-Engine & ACID**. Die kleinen sind alles, wo `data_health.py` heute schon das Fundament legt.

---

## 3. Ziel-Architektur (V10-Vision)

```
daily-invest/
├── code/                                  # Python-Package, portabel
│   └── (was heute in tools/ und data_lake/ verstreut ist)
│
├── lake/                                  # Der echte Lakehouse-Ordner
│   ├── _catalog/
│   │   ├── manifest.json                  # Tabellen-Index + Schema-Version
│   │   ├── contracts/                     # Data-Mesh: Schema per Domain
│   │   │   ├── prices.yaml
│   │   │   ├── scanner.yaml
│   │   │   ├── insider.yaml
│   │   │   └── ...
│   │   └── lineage.json                   # wer liest/schreibt was
│   │
│   ├── bronze/                            # Raw, immutable, ingestion-as-captured
│   │   ├── prices_daily/
│   │   │   └── ticker=AAPL/year=2026/month=04/data.parquet
│   │   ├── scanner/
│   │   │   └── year=2026/month=04/day=16/scan.parquet
│   │   ├── insider_openinsider/
│   │   │   └── year=2026/month=04/day=16/data.parquet
│   │   └── ...
│   │
│   ├── silver/                            # Cleaned, normalized, joined-ready
│   │   ├── universe_daily/                # alle Assets × Tage, gecleaned
│   │   ├── signals_daily/                 # alle Scoring-Inputs, konsistent
│   │   ├── regime_daily/
│   │   └── ...
│   │
│   ├── gold/                              # Business-ready, aggregation
│   │   ├── top_setups_daily/              # Eingabe für den Tagesreport
│   │   ├── trade_candidates_daily/        # Kandidaten zur eigenen Prüfung
│   │   ├── kpi_rolling/                   # Sharpe/Sortino/Drawdown-Zeitreihen
│   │   └── trade_journal/                 # Journal-Trades aus SQLite exportiert
│   │
│   └── _logs/
│       ├── ingest_YYYY-MM-DD.log
│       ├── dq_YYYY-MM-DD.json             # data_health.py-Output, bleibt
│       └── transform_YYYY-MM-DD.log
│
└── reports/                               # Generated Artefakte (MD, XLSX, HTML)
    └── (was heute als DevReport/Audit/Dashboard durch die Gegend liegt)
```

**Kernstack (Free, lokal, stdlib-nah):**

- **DuckDB** (MIT-Lizenz, 20 MB Binary, SQL direkt auf Parquet/JSON/CSV, WAL, ACID, In-Process wie SQLite: ist quasi "SQLite für Analytics")
- **PyArrow** + **Polars** (Rust-based, 10× schneller als Pandas, lazy evaluation, spaltenorientiert)
- **Pydantic v2** für Schema-Contracts (+ automatische Validierung + Schema-Export)
- **Weiterhin:** yfinance, FRED-CSV, SEC-EDGAR-XBRL: alles Free-Stack, nichts ändert sich an den Quellen

Keine Cloud, kein Daemon, kein Server. Die ganze Architektur läuft als Python-Prozess auf einem Laptop. Das ist **genau** die lokale-Software-Vision.

---

## 4. Priorisierte Upgrade-Roadmap

| Welle | Baustein | Aufwand | Impact | Version |
|---|---|---|---|---|
| **I** | **Lake-Zonenstruktur** (Bronze/Silver/Gold-Verzeichnisse anlegen + leeres Catalog-Manifest) | 0.5 Tag | mittel (Struktur) | V7.0 |
| **I** | **DuckDB + Polars installieren** (requirements.txt updaten, Smoke-Test) | 0.25 Tag | hoch (Compute-Basis) | V7.0 |
| **I** | **Schema-Contracts für die 6 Kern-Domänen** in `lake/_catalog/contracts/*.yaml` (Pydantic-Modelle + YAML-Spec) | 1 Tag | hoch (Drift-Schutz) | V7.0 |
| **II** | **Bronze-Migration**: Live-JSONs (`tools/*.json`) → partitionierte Parquets in `lake/bronze/` (Writer-Patch in jeder Daily-Script) | 1-2 Tage | hoch (Storage-Qualität) | V7.1 |
| **II** | **Silver-Layer für `universe_daily` + `signals_daily`** (ein konsolidiertes Tabellen-Pendant zum heutigen Scanner-Output) | 1-2 Tage | hoch (Wiederverwendbarkeit) | V7.2 |
| **II** | **Gold-Views** (DuckDB-Views für top_setups_daily, trade_candidates_daily) + Konsument-Switch (Reports und Kandidatenliste lesen Gold statt Bronze-JSON) | 1 Tag | mittel (Speed) | V7.3 |
| **III** | **Data-Mesh-Contracts** mit Drift-Detection (Producer-Hash vs. Contract-Version, Fehler bei Bruch) | 1 Tag | hoch (Stabilität) | V8.0 |
| **III** | **Lineage-Graph** (auto-generiert aus Script-Imports, als Mermaid-Diagramm im Catalog) | 0.5 Tag | mittel (Transparenz) | V8.0 |
| **III** | **ACID-Writes für den Lake** (DuckDB-WAL für Silver/Gold-Transformations, atomare Parquet-Swaps für Bronze) | 1 Tag | mittel (Stabilität) | V8.1 |
| **IV** | **Retention-Policies** (TTL pro Zone, Bronze ≥5 Jahre, Silver ≥3 Jahre, Gold ≥1 Jahr, ältere in `archive/`) | 0.5 Tag | klein | V9.0 |
| **IV** | **Portabler Code-Ordner** (code/ als pip-installierbares Paket mit `pyproject.toml`, env-var für Lake-Pfad) | 1 Tag | mittel (Vision-Schritt) | V9.0 |
| **IV** | **Catalog-Web-UI** (kleine lokale FastAPI + HTMX, zeigt Tabellen/Schemas/Freshness/DQ-Status) | 1-2 Tage | mittel (Sichtbarkeit) | V9.5 |
| **V** | **Local-AI-Hook-Points** (Gold-Views als Input für einen lokalen LLM, z.B. Ollama, der Report-Entwürfe schreibt) | 2+ Tage | sehr hoch (Vision-Finale) | V10.0 |

Gesamtaufwand pro Welle (bei einem Arbeitsblock von etwa 2 h pro Werktag): Welle I ~1 Woche, Welle II ~2-3 Wochen, Welle III ~1 Woche, Welle IV ~1-2 Wochen, Welle V offen. Grob 8-12 Wochen bis V10 auf dem Pfad.

---

## 5. Konkrete nächste Schritte (erste 3 Arbeitsblöcke)

**Block 1 (der vorige Block war `data_health.py`):**

Bau `lake/` mit Zonen + leerem Catalog. Schreibe `lake/_catalog/manifest.json` als v2-Manifest (V6-kompatibel, aber erweitert mit `zone`, `contract_ref`, `retention_days`). Installiere `duckdb` und `polars` via `pip`. Smoke-Test: lies `data_lake/prices/daily/AAPL.parquet` mit DuckDB und mache eine Fenster-Query. Output: neues `DevReport_*.md` mit Migrations-Checkliste.

**Block 2:**

Schreibe Schema-Contracts für die sechs heutigen Daily-Sources (scan, insider, options_flow, sector_rs, regime, premarket, earnings) als Pydantic-Modelle. Exportiere sie parallel als YAML in `lake/_catalog/contracts/`. Integriere einen Lightweight-Validator in `data_health.py`: wenn das heutige JSON nicht gegen den Contract validiert, wird ein H-Ticket geschrieben.

**Block 3:**

Erster Producer-Migration-Patch: `tools/daily_scanner.py` bekommt einen optionalen Writer, der zusätzlich zum JSON auch `lake/bronze/scanner/year=.../data.parquet` schreibt. Konsument bleibt zunächst JSON-first. Schattenlauf 1 Woche, dann Gold-View in DuckDB bauen, dann Konsument-Switch.

Diese drei Schritte sind **umkehrbar**: wenn etwas nicht passt, bleibt der heutige JSON-Pfad aktiv. Das ist Databricks-Prinzip "Strangler-Migration".

---

## 6. Was wir bewusst **nicht** kopieren

- **Kein Hadoop-Cluster.** HDFS skaliert auf Petabyte; wir haben Gigabyte. YARN, NameNode, Zookeeper wären Overkill.
- **Kein Databricks-Runtime.** Unity Catalog, Delta Lake, MLflow sind großartige Produkte, aber mit Vendor-Lock-in und Kosten verbunden. Wir adaptieren die **Konzepte**, nicht das Produkt.
- **Kein Spark.** Für 610 Assets × 30 Tage × 60 Features passt alles in 500 MB RAM. DuckDB/Polars sind hier schneller.
- **Kein Kafka / Streaming.** Tages-Kadenz reicht. Event-Streaming würde Operational-Overhead bringen ohne Gewinn.
- **Kein Snowflake / BigQuery.** Cloud = Kosten + Lock-in. DuckDB + Parquet ist der 80/20-Ersatz für Einzelpersonen.

---

## 7. Tickets für die nächsten Schritte

Drei Tickets als Kandidaten für die nächsten Schritte (Severity L/M = "Strategisches Upgrade", nicht Bugfix):

1. `DATA-ARCH-1`: Lake-Zonen + DuckDB/Polars installieren + Catalog-v2-Manifest
2. `DATA-ARCH-2`: Schema-Contracts (Pydantic + YAML) für 6 Daily-Sources + Validator in `data_health.py`
3. `DATA-ARCH-3`: Bronze-Migration: Scanner-Dual-Write (JSON + Parquet) mit Schattenlauf

Details zur Umsetzung entstehen beim Bearbeiten des jeweiligen Tickets.

---

*Verfasst 2026-04-16 nachmittags, im Anschluss an den Data-Health-Monitor-Build. Grundlage für die V7-V10-Daten-Roadmap.*
