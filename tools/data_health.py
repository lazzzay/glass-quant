#!/usr/bin/env python3
"""
data_health.py: Data-Health-Monitor V1 für das Daily-Invest-System

Zweck
=====
Erkennt proaktiv stille Degradationen in den täglichen Live-Daten (Scanner,
Insider, Options-Flow, Sector-RS, Regime, Premarket, Earnings), BEVOR das
Briefing und die Empfehlung auf kaputten Daten laufen.

Motivation
----------
Am 2026-04-16 liefen Briefing (06:38) und Empfehlung (09:07), obwohl seit
2026-04-15 die CBOE-PCR-Quelle HTTP 403 lieferte und yfinance Option-Chains
100 % Fehler hatten. Beide V6.8-Scoring-Layer (market_pcr + options_flow)
waren still auf 0: niemand merkte es. Dieser Monitor schließt genau diese
Lücke: er liest die selben JSONs, die das Scoring liest, auditet sie gegen
definierte Gesundheitsregeln, schreibt einen strukturierten Report und
erzeugt automatisch OPS-Tickets für H+M-Severity-Findings (Dedup via
Fingerprint, damit er nicht täglich dasselbe Ticket neu schreibt).

Usage
-----
    python3 tools/data_health.py                     # heute auditieren
    python3 tools/data_health.py --date 2026-04-16   # konkretes Datum
    python3 tools/data_health.py --no-tickets        # nur Report, keine Tickets
    python3 tools/data_health.py --quiet             # nur Exit-Code

Exit-Codes
----------
    0: alles OK
    1: M-Findings (Degradation, aber weiterlauffähig)
    2: H-Findings (Critical, Briefing/Empfehlung sollten pausieren)

Design-Prinzipien
-----------------
* Free-Stack-Only (stdlib), keine Paid-APIs, keine externen Deps.
* Idempotent: gleicher Input → gleiches Ticket-Fingerprint, keine Duplikate.
* Fail-Safe: Monitor-Fehler darf nie das Briefing blockieren (außer --strict).
* Transparent: jedes Finding referenziert die Daten-Zeile, die es ausgelöst hat.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
TICKETS_PATH = PROJECT_ROOT / "ops_tickets.md"

# ----------------------------------------------------------------------------
# Severity & Status
# ----------------------------------------------------------------------------

SEV_H = "H"
SEV_M = "M"
SEV_L = "L"

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_MISSING = "MISSING"
STATUS_DISABLED = "DISABLED"  # Layer ist by design aus (daily_scanner.DISABLED_LAYERS)
STATUS_PENDING = "PENDING"             # aktiver Producer noch nicht gelaufen (Pre-Production-Fenster)
STATUS_ENV_UNREADABLE = "ENV_UNREADABLE"  # Datei existiert, Read scheitert (Sandbox-Mount-Artefakt)


# ----------------------------------------------------------------------------
# Timing-/Env-Resilienz  (Fix DATA-HEALTH-TIMING-RESILIENCE-1, KW24 / V7.17)
# ----------------------------------------------------------------------------
#
# Problem (Folge-FP-Klasse nach V7.16): der Morgen-Cron (06:40 CET) laeuft VOR
# den aktiven Producern (scan/regime/premarket/sector_rs ~08:05 CET) → er flaggt
# deren noch-nicht-geschriebene JSONs als SEV_H *_JSON_MISSING. Bis zum 09:03-Lauf
# sind die Dateien da ("erholt"), die H-Tickets bleiben aber OPEN → taegliche
# Falsch-Eskalation (heute 2026-06-09: 4× *_JSON_MISSING). Zweite Quelle: in der
# Cowork-Sandbox scheitert der Read grosser/gelockter JSONs ueber den Mac-Mount
# mit [Errno 35] Resource deadlock avoided (EDEADLK) → SEV_H *_PARSE_ERROR,
# obwohl die Datei gesund ist und der native Mac sie problemlos liest.
#
# Fix (zwei NEUE Faehigkeiten, KEINE Maskierung echter Defekte):
#   (A) Pre-Production-Timing-Grace: fehlt ein aktiver Producer waehrend einer
#       KOORDINIERTEN Komplett-Abwesenheit (kein einziger aktiver Producer da)
#       am HEUTIGEN Tag VOR GRACE_CUTOFF_HOUR (lokal) → SEV_L AWAITING_PRODUCER
#       (Status PENDING, kein Ticket). Sobald EIN Producer da ist (Produktion
#       gestartet) ODER nach dem Cutoff ODER an einem Alt-Datum → unveraendert
#       SEV_H *_JSON_MISSING. Ein echter Komplett-Ausfall (Cron tot) bleibt nach
#       dem Cutoff voll SEV_H sichtbar: wird NICHT versteckt.
#   (B) EDEADLK-resilienter Reader: Read mit Retry; bleibt er bei stat-OK-Datei
#       mit OSError haengen → SEV_L UNREADABLE_ENV (Status ENV_UNREADABLE, kein
#       Ticket, Umgebungs-Artefakt). Echte Korruption (JSONDecodeError) bleibt
#       unveraendert SEV_H *_PARSE_ERROR.
#
# Invarianten: Disabled-Layer-Policy (V7.16) unberuehrt; Free-Stack/stdlib;
# read-only ggue. Journal; keine Score-/Live-Aenderung.

# Aktive Tages-Producer (nicht-disabled), die pro Handelstag ein JSON erzeugen.
# Basis der "coordinated absence"-Heuristik fuer das Pre-Production-Fenster.
_ACTIVE_PRODUCERS = ("scan", "sector_rs", "regime", "premarket", "earnings")
# Vor dieser lokalen Stunde gilt eine koordinierte Komplett-Abwesenheit als
# "Producer noch nicht gelaufen" (Pre-Production) statt als Ausfall.
GRACE_CUTOFF_HOUR = 10

# --- LAGGING-PRODUCER-GRACE (Fix DATA-HEALTH-PREMARKET-TIMING-FP-1, V7.24) ----
# Folge-FP-Klasse nach V7.17: die Pre-Production-Grace (A) greift NUR bei
# KOORDINIERTER Komplett-Abwesenheit (kein einziger Producer da). Sobald aber EIN
# schneller Producer (scan ~04:15 / sector_rs / regime / earnings) geschrieben
# hat, gilt die Pipeline als "gestartet" → ein noch fehlender Producer wird H.
# Das bestraft den BEKANNT LANGSAMEN Producer `premarket`: premarket_scanner.py
# braucht 39-76 Min fuer 639 Namen (PREMARKET-LATENCY-1) und ist nativ erst
# ~11:16 CET fertig: laaange nachdem die schnellen Producer da sind. Der
# data_health-Snapshot dazwischen (~07-09 CET) flaggt das noch-produzierende
# premarket_<date>.json faelschlich als SEV_H PREMARKET_JSON_MISSING → overall
# CRITICAL → Risiko eines falschen Morgen-NO-TRADE. Beobachtete FP-Tage:
# 2026-06-10 und 2026-06-16.
#
# Fix: fuer bekannt langsame Producer gilt eine eigene, SPAETERE Grace. Fehlt ihr
# JSON heute vor LAGGING_PRODUCER_CUTOFF_HOUR (lokal), waehrend mindestens EIN
# anderer aktiver Producer bereits da ist (Pipeline laeuft also → kein
# Komplett-Ausfall), wird es SEV_L AWAITING_PRODUCER (Status PENDING, kein
# Ticket). Nach dem Cutoff ODER bei Komplett-Ausfall (kein Sibling da) bleibt es
# voll sichtbar: echtes spaetes premarket-Versagen → SEV_H, Komplett-Ausfall →
# PIPELINE_NOT_RUN. Keine Maskierung echter Defekte.
# premarket ist nativ erst ~11:16 CET fertig → Cutoff mit Sicherheitsmarge 12:00.
LAGGING_PRODUCER_CUTOFF_HOUR = 12

# --- SCAN-WRITE-LAG-GRACE (Fix DATA-HEALTH-PIPELINE-ROLLUP-TIMING-FP-1, V7.28) --
# Folge-FP-Klasse nach V7.24: der data_health-Morgen-Snapshot laeuft als EIGENER
# launchd-Job (~06:40 CET) VOR der 08:00-Haupt-Pipeline. Die frueh/separat
# geschriebenen Producer (regime/sector_rs/earnings/insider) sind dann schon da,
# der HEAVY scan (daily_scanner.py, 639 Namen; Haupt-Pipeline-Step ~08:01 CET)
# aber noch NICHT → coordinated_absence=False (Siblings da), und scan war bisher
# KEIN Lagging-Producer → SCAN_MISSING faellt auf SEV_H durch → persistentes
# FP-Ticket (Belegte FP-Tage 2026-06-18/-19/-22), das die OPS-Eskalations-Serie
# speist. Beweis: scan_<date>.json existiert spaeter; das autoritative (abendliche)
# data_health fuehrt KEIN SCAN_MISSING (hoechstens SCAN_FILL_*). Dieselbe Wurzel
# erzeugte am 2026-06-17 ein FP PIPELINE_NOT_RUN (scan+premarket gleichzeitig im
# Snapshot fehlend → Rollup).
#
# Fix: scan wird wie premarket als Lagging-Producer behandelt, aber mit einem
# FRUEHEREN, producer-spezifischen Cutoff (scan ~08:01 fertig → GRACE_CUTOFF_HOUR=10;
# premarket ~11:16 → 12). Fehlt ein Lagging-Producer heute vor SEINEM Cutoff,
# waehrend ein anderer aktiver Producer bereits da ist (Pipeline laeuft → kein
# Komplett-Ausfall) → SEV_L AWAITING_PRODUCER (PENDING, kein Ticket). NACH dem
# Cutoff ODER bei Komplett-Ausfall (kein Sibling) bleibt es voll sichtbar: echtes
# spaetes Versagen → SEV_H *_MISSING; Komplett-Ausfall → PIPELINE_NOT_RUN.
# Schnelle/frueh-separate Producer (sector_rs/regime/earnings) bleiben bei
# selektivem Fehlen unveraendert SEV_H (Invariante V7.17/V7.24, kein Masking).
_LAGGING_PRODUCER_CUTOFFS: dict[str, int] = {
    "premarket": LAGGING_PRODUCER_CUTOFF_HOUR,  # PREMARKET-LATENCY-1, ~11:16 CET fertig
    "scan": GRACE_CUTOFF_HOUR,                    # heavy scan ~08:01 CET → frueherer Cutoff (10)
}
# Back-compat: bisheriger Tuple-Name bleibt als Keys-View der Cutoff-Map bestehen.
_LAGGING_PRODUCERS = tuple(_LAGGING_PRODUCER_CUTOFFS)

# --- PIPELINE-NOT-RUN-Rollup (V7.19) ----------------------------------------
# Wenn MEHRERE aktive Producer GLEICHZEITIG fehlen, ist das fast immer EIN
# Ereignis (die Morgen-Pipeline lief nicht / nicht vollstaendig: Mac schlief,
# launchd-Job failte, oder der Sandbox-Producer konnte strukturell nicht
# schreiben). Bisher erzeugte das N separate SEV_H-Findings (N Tickets) fuer
# eine einzige Wurzel → Ticket-Rauschen (vgl. ops_tickets PIPELINE-NOT-RUN-
# FINDING). Ab >= PIPELINE_ROLLUP_MIN fehlenden aktiven Producern werden die
# Einzel-Findings zu EINEM Sammel-Finding PIPELINE_NOT_RUN (H) gebuendelt:
# weiterhin genau 1 H → overall_status CRITICAL → NO-TRADE bleibt erhalten,
# aber nur 1 Ticket statt N. Fehlt nur EIN Producer (z. B. earnings), bleibt
# das spezifische Einzel-Finding bestehen (genuines Signal sichtbar).
PIPELINE_ROLLUP_MIN = 2
TASK_PIPELINE = "pipeline"


# ----------------------------------------------------------------------------
# Finding-Helper
# ----------------------------------------------------------------------------

def _fingerprint(date: str, source: str, code: str) -> str:
    """Stabiler Hash für Dedup im Ticket-Log (Datum+Source+Code)."""
    raw = f"{date}|{source}|{code}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def make_finding(severity: str, source: str, code: str, message: str,
                 affects: list[str] | None = None, detail: dict | None = None) -> dict:
    return {
        "severity": severity,
        "source": source,
        "code": code,
        "message": message,
        "affects": affects or [],
        "detail": detail or {},
    }


# ----------------------------------------------------------------------------
# Disabled-Layer-Policy  (Fix DATA-HEALTH-DISABLED-LAYER-BLINDNESS-1, KW24)
# ----------------------------------------------------------------------------
#
# Problem: data_health.py kannte `daily_scanner.DISABLED_LAYERS` nicht und
# flaggte absichtlich deaktivierte Scoring-Layer (H2-Ablation: news /
# options_flow / insider / market_pcr) jeden Tag als SEV_H "INACTIVE" bzw.
# "JSON_MISSING" → overall_status faelschlich CRITICAL → chronische Falsch-
# Eskalation (9 ISO-Wochen Streak, ~36 H/M-FP pro Tag im ops_tickets-Log).
#
# Fix: data_health liest DISABLED_LAYERS als Single-Source-of-Truth und stuft
# Findings, die zu einem by-design abgeschalteten Layer gehoeren, auf SEV_L
# (informativ) zurueck. Sie bleiben fuer Transparenz im JSON-Report (Flag
# `intentionally_disabled`), treiben aber weder overall_status noch erzeugen
# sie OPEN-Tickets. Aktive Layer (scan/mtf/sector_rs/regime/premarket/
# earnings) sind unberuehrt: ein echter SCAN_MISSING bleibt H.

_DEFAULT_DISABLED_LAYERS = {"news", "options_flow", "insider", "market_pcr"}


def get_disabled_layers() -> set[str]:
    """SSoT = daily_scanner.DISABLED_LAYERS, per Source-Parse gelesen (kein
    Import → keine yfinance-Side-Effects / kein Speed-Hit im Health-Monitor).
    Fallback auf den dokumentierten Default, falls die Datei fehlt/anders ist.
    """
    scanner_path = TOOLS_DIR / "daily_scanner.py"
    try:
        txt = scanner_path.read_text(encoding="utf-8")
        m = re.search(r"^DISABLED_LAYERS\s*=\s*\{([^}]*)\}", txt, re.MULTILINE)
        if m:
            layers = set(re.findall(r"['\"]([a-z_]+)['\"]", m.group(1)))
            if layers:
                return layers
    except Exception:
        pass
    return set(_DEFAULT_DISABLED_LAYERS)


def _finding_layer(finding: dict) -> str | None:
    """Canonical-Layer-Name eines Findings, vergleichbar mit DISABLED_LAYERS.
    None = nicht an genau einen schaltbaren Layer gebunden (bleibt aktiv).
    Reihenfolge wichtig: market_pcr vor options_flow (OPTIONS_FLOW_MARKET_PCR_DOWN).
    """
    code = (finding.get("code") or "").upper()
    source = (finding.get("source") or "").lower()
    if "MARKET_PCR" in code:
        return "market_pcr"
    if code.startswith("OPTIONS_FLOW") or source == "options_flow":
        return "options_flow"
    if code.startswith("INSIDER") or source in ("insider", "cross_insider_scan"):
        return "insider"
    if code.startswith("NEWS") or source == "news":
        return "news"
    return None


def _source_layer(source: str) -> str | None:
    """Layer-Name fuer einen data_health-*Source*-Bucket (Status-Reconciliation).
    Nur 1:1-schaltbare Buckets; scan/sector_rs/regime/premarket/earnings = None.
    """
    s = (source or "").lower()
    if s == "options_flow":
        return "options_flow"
    if s in ("insider", "cross_insider_scan"):
        return "insider"
    if s == "news":
        return "news"
    return None


def apply_disabled_layer_policy(findings: list[dict],
                                disabled: set[str] | None = None) -> int:
    """Stuft alle Findings deaktivierter Layer auf SEV_L zurueck (in-place).
    Gibt die Anzahl der zurueckgestuften Findings zurueck.
    """
    if disabled is None:
        disabled = get_disabled_layers()
    n = 0
    for f in findings:
        layer = _finding_layer(f)
        if layer and layer in disabled:
            f["intentionally_disabled"] = True
            f["disabled_layer"] = layer
            if f.get("severity") != SEV_L:
                f["original_severity"] = f.get("severity")
                f["severity"] = SEV_L
                f["message"] = (f"[Layer '{layer}' by design deaktiviert: "
                                f"informativ, kein Incident] {f.get('message', '')}")
                n += 1
    return n


# ----------------------------------------------------------------------------
# Audit-Regeln: pro Source eine Check-Funktion
# ----------------------------------------------------------------------------

def _read_text_resilient(path: Path, attempts: int = 3,
                         sleep_s: float = 0.12) -> tuple[str | None, OSError | None]:
    """Liest Text mit Retry. Transiente Mount-Read-Fehler (EDEADLK errno 35 /
    EAGAIN errno 11 in der Cowork-Sandbox) sind oft beim 2./3. Versuch weg.
    Gibt (text, None) bei Erfolg, sonst (None, last_oserror).
    """
    last: OSError | None = None
    for i in range(max(1, attempts)):
        try:
            return path.read_text(encoding="utf-8"), None
        except OSError as e:  # EDEADLK/EAGAIN/…: Umgebungs-/Mount-Artefakt, NICHT json-corrupt
            last = e
            if i < attempts - 1:
                try:
                    time.sleep(sleep_s)
                except Exception:
                    pass
    return None, last


def _load_json(path: Path) -> tuple[dict | list | None, str | None]:
    """Laedt JSON und unterscheidet drei Fehlerklassen ueber ein Prefix im
    err-String (back-compat: Rueckgabe bleibt (doc|None, err|None)):
      * 'file_not_found: …': Datei existiert nicht
      * 'unreadable_env: …': Datei existiert (stat ok), Read scheitert mit
                               OSError trotz Retry → Umgebungs-Artefakt
      * 'parse_error: …': Bytes gelesen, aber json.loads scheitert → echte
                               Korruption
    """
    if not path.exists():
        return None, f"file_not_found: {path.name}"
    text, read_exc = _read_text_resilient(path)
    if read_exc is not None:
        try:
            sz = path.stat().st_size
        except OSError:
            sz = -1
        errno = getattr(read_exc, "errno", None)
        return None, (f"unreadable_env: [{type(read_exc).__name__} errno={errno}] "
                      f"{read_exc} (stat_size={sz})")
    try:
        return json.loads(text), None
    except ValueError as e:  # json.JSONDecodeError ⊂ ValueError → echte Korruption
        return None, f"parse_error: {e}"


def _classify_load_err(err: str | None) -> str | None:
    """Mappt den err-String von _load_json auf eine Fehlerklasse."""
    if not err:
        return None
    if err.startswith("file_not_found"):
        return "missing"
    if err.startswith("unreadable_env"):
        return "env_unreadable"
    return "parse"


# --- Pre-Production-Timing-Fenster (Fix A) ----------------------------------

def _active_producer_present(date: str, tools_dir: Path | None = None) -> dict[str, bool]:
    """Praesenz-Map der aktiven Tages-Producer-JSONs (existiert die Datei?)."""
    td = tools_dir or TOOLS_DIR
    present: dict[str, bool] = {}
    for src in _ACTIVE_PRODUCERS:
        cands = [f"{src}_{date}.json", f"{src.replace('_', '')}_{date}.json"]
        present[src] = any((td / n).exists() for n in cands)
    return present


def _coordinated_absence(date: str, tools_dir: Path | None = None) -> bool:
    """True, wenn KEIN einziger aktiver Producer fuer den Tag existiert
    (= Cron hat noch gar nicht geschrieben → Pre-Production, kein Teil-Ausfall).
    """
    return not any(_active_producer_present(date, tools_dir).values())


def _within_production_grace(date: str, now: dt.datetime | None = None,
                             cutoff_hour: int = GRACE_CUTOFF_HOUR) -> bool:
    """Grace gilt NUR fuer den heutigen Tag und NUR vor dem lokalen Cutoff.
    Alt-Daten (Backfill) oder Laeufe nach dem Cutoff bekommen nie Grace.
    """
    now = now or dt.datetime.now()
    if date != now.date().isoformat():
        return False
    return now.hour < cutoff_hour


def _is_pre_production(date: str, now: dt.datetime | None = None,
                       tools_dir: Path | None = None,
                       cutoff_hour: int = GRACE_CUTOFF_HOUR) -> bool:
    """Pre-Production-Fenster = heutiger Tag, vor Cutoff, UND koordinierte
    Komplett-Abwesenheit aller aktiven Producer. Nur dann ist ein fehlendes
    JSON ein Timing-Artefakt statt eines Ausfalls.
    """
    return (_within_production_grace(date, now, cutoff_hour)
            and _coordinated_absence(date, tools_dir))


def _within_lagging_grace(date: str, source: str, now: dt.datetime | None = None,
                          tools_dir: Path | None = None,
                          cutoff_hour: int | None = None) -> bool:
    """Lagging-Producer-Grace (V7.24 premarket; V7.28 zusaetzlich scan).

    True, wenn `source` ein bekannt SPAET schreibender Producer
    (`_LAGGING_PRODUCER_CUTOFFS`) ist, sein JSON heute vor SEINEM producer-
    spezifischen Cutoff (lokal) noch fehlt UND mindestens ein ANDERER aktiver
    Producer bereits geschrieben hat (Pipeline laeuft → nur der spaete haengt
    nach). Ein echter Komplett-Ausfall (kein Sibling da) faellt NICHT hierunter,
    sondern in die koordinierte-Abwesenheits-Logik (PENDING vor GRACE_CUTOFF_HOUR
    / PIPELINE_NOT_RUN danach). Producer-Cutoffs: premarket=12 (~11:16 CET fertig),
    scan=10 (~08:01 CET fertig, laeuft aber hinter dem ~06:40 data_health-Snapshot).
    Frueh/separat schreibende Producer (sector_rs/regime/earnings) sind NICHT
    gelistet → fuer sie bleibt ein selektives Fehlen unveraendert SEV_H.

    `cutoff_hour` override (Test-Hook): wenn None, gilt der producer-spezifische
    Cutoff aus `_LAGGING_PRODUCER_CUTOFFS`.
    """
    if source not in _LAGGING_PRODUCER_CUTOFFS:
        return False
    eff_cutoff = cutoff_hour if cutoff_hour is not None else _LAGGING_PRODUCER_CUTOFFS[source]
    if not _within_production_grace(date, now, eff_cutoff):
        return False
    present = _active_producer_present(date, tools_dir)
    return any(v for k, v in present.items() if k != source)


# Pre-kompilierte Patterns fuer Source-Text-Klassifikation (s. _text_has_real_error).
_ERR_HTTP_STATUS = re.compile(r"\b([45]\d{2})\b")
_ERR_COUNT = re.compile(r"\b(\d+)\s+errors?\b")


def _text_has_real_error(text: str) -> bool:
    """True wenn der Source-Status-String auf einen realen Fehler hindeutet.

    Historie: bis 2026-04-20 war die Heuristik naiv `"error" in text`. Das warf
    False-Positives wie "ok (13/13, 0 errors)" als SEV_H auf: der Substring
    "error" matched in "0 errors". Fix:
    * HTTP-Fehler-Label ("http error", "error: ") → echter Fehler.
    * HTTP-Status-Codes 4xx/5xx mit Wortgrenze → echter Fehler (verhindert dass
      "4030 trades" als "403" missklassifiziert wird).
    * "<N> errors" nur wenn N > 0 → echter Fehler.
    * "failed"/"failure"/"errored" → echter Fehler.
    * "0 errors" / "no errors" / "no error" → gesund.
    """
    t = (text or "").lower()
    if not t:
        return False

    # Expliziter Fehler-Label-Match
    if "http error" in t or "error:" in t or "errored" in t:
        return True
    if "failed" in t or "failure" in t:
        return True

    # HTTP-Statuscode 4xx/5xx als eigenstaendiges Token
    if _ERR_HTTP_STATUS.search(t):
        return True

    # "<N> errors", nur wenn N > 0 zaehlen
    m = _ERR_COUNT.search(t)
    if m:
        try:
            return int(m.group(1)) > 0
        except ValueError:
            return False

    # Letzte Verteidigung: generisches "error" als eigenstaendiges Wort
    # (nicht als Teil von "0 errors", "no error" etc.)
    if re.search(r"\berror\b", t) and "no error" not in t and "0 error" not in t:
        return True

    return False


def check_scan(date: str, now: dt.datetime | None = None) -> tuple[dict, list[dict]]:
    path = TOOLS_DIR / f"scan_{date}.json"
    doc, err = _load_json(path)
    findings: list[dict] = []
    summary = {"status": STATUS_OK, "fill_rate": None, "top_n": None,
               "null_rates": {}, "path": str(path.name)}

    if err:
        kind = _classify_load_err(err)
        if kind == "env_unreadable":
            # Datei existiert (stat ok), nur im aktuellen Lauf nicht lesbar → kein Daten-Defekt.
            summary["status"] = STATUS_ENV_UNREADABLE
            findings.append(make_finding(
                SEV_L, "scan", "SCAN_UNREADABLE_ENV",
                f"scan_{date}.json existiert, ist im aktuellen Lauf aber nicht lesbar "
                f"(Umgebungs-/Mount-Artefakt, kein Daten-Defekt): {err}",
                detail={"kind": "env_unreadable"}))
            return summary, findings
        if kind == "missing" and _is_pre_production(date, now=now):
            # Pre-Production-Fenster: Producer laeuft gleich, noch kein Ausfall.
            summary["status"] = STATUS_PENDING
            findings.append(make_finding(
                SEV_L, "scan", "SCAN_AWAITING_PRODUCER",
                f"scan_{date}.json noch nicht geschrieben: Pre-Production-Fenster "
                f"(alle aktiven Producer abwesend, lokal vor {GRACE_CUTOFF_HOUR}:00). "
                f"Kein Ausfall.",
                detail={"kind": "pending"}))
            return summary, findings
        # SCAN-WRITE-LAG-GRACE (V7.28): der heavy scan-Producer (Haupt-Pipeline
        # ~08:01) haengt hinter dem fruehen data_health-Snapshot (~06:40), waehrend
        # frueh/separat geschriebene Sibling-Producer schon da sind → Timing-Artefakt
        # (vor scan-Cutoff = GRACE_CUTOFF_HOUR), kein Ausfall. Nach Cutoff ODER
        # Komplett-Ausfall (kein Sibling) bleibt es SEV_H (echtes spaetes Versagen).
        if kind == "missing" and _within_lagging_grace(date, "scan", now=now):
            summary["status"] = STATUS_PENDING
            findings.append(make_finding(
                SEV_L, "scan", "SCAN_AWAITING_PRODUCER",
                f"scan_{date}.json noch nicht geschrieben: schwerer/spaet schreibender "
                f"Producer (daily_scanner, 639 Namen, Haupt-Pipeline ~08:01 CET) haengt "
                f"hinter dem fruehen data_health-Snapshot; andere aktive Producer sind "
                f"bereits da (Pipeline laeuft), lokal vor "
                f"{_LAGGING_PRODUCER_CUTOFFS['scan']}:00. Kein Ausfall.",
                detail={"kind": "pending", "lagging": True}))
            return summary, findings
        # echtes Fehlen (post-window/selektiv) ODER echte Korruption → SEV_H
        if kind == "parse":
            summary["status"] = STATUS_FAIL
            findings.append(make_finding(SEV_H, "scan", "SCAN_PARSE_ERROR",
                                         f"Scan-JSON korrupt: {err}"))
        else:
            summary["status"] = STATUS_MISSING
            findings.append(make_finding(SEV_H, "scan", "SCAN_MISSING",
                                         f"Scan-JSON fehlt: {err}"))
        return summary, findings

    universe = doc.get("universe_size", 0) or 0
    valid = doc.get("valid_results", 0) or 0
    fill = (valid / universe) if universe else 0.0
    summary["fill_rate"] = round(fill, 4)

    if fill < 0.80:
        findings.append(make_finding(SEV_H, "scan", "SCAN_FILL_LT_80",
                                     f"Scanner-Fill-Rate kritisch: {fill:.1%} ({valid}/{universe})",
                                     affects=["gesamtes scoring"]))
    elif fill < 0.95:
        findings.append(make_finding(SEV_M, "scan", "SCAN_FILL_LT_95",
                                     f"Scanner-Fill-Rate degradiert: {fill:.1%} ({valid}/{universe})"))
    elif fill < 0.99:
        findings.append(make_finding(SEV_L, "scan", "SCAN_FILL_LT_99",
                                     f"Kleine Scanner-Fill-Loss: {fill:.1%} ({valid}/{universe})"))

    # Null-Rates auf Top-Setups (echtes Scoring-Input)
    top = doc.get("top_setups") or []
    summary["top_n"] = len(top)
    tracked_fields = [
        "pre_score", "sector_rs_score", "mtf_adjust", "h1_state",
        "insider_score", "options_flow_score", "market_pcr_adjust",
        "pre_score_pre_mtf",
    ]
    null_rates: dict[str, float] = {}
    if top:
        for f in tracked_fields:
            empties = sum(
                1 for t in top
                if t.get(f) in (None, 0, 0.0, "", "unchecked", "no_data", {}, [])
            )
            null_rates[f] = round(empties / len(top), 3)
    summary["null_rates"] = null_rates

    # Layer-Deaktivierungs-Checks (100 % null in einem Scoring-Input)
    layer_map = {
        "h1_state": ("MTF_H1_INACTIVE", "V6.6 MTF-H1", ["mtf_adjust"]),
        "options_flow_score": ("OPTIONS_FLOW_INACTIVE", "V6.8 Options-Flow-Layer",
                               ["options_flow_adjust"]),
        "market_pcr_adjust": ("MARKET_PCR_INACTIVE", "V6.8 Market-PCR-Tilt", ["empfehlung"]),
    }
    for field, (code, layer, affect) in layer_map.items():
        rate = null_rates.get(field, 0.0)
        if rate >= 0.99:
            findings.append(make_finding(
                SEV_H, "scan", code,
                f"Scoring-Layer still inaktiv: '{field}' zu {rate:.0%} null/neutral im Top-{len(top)}. {layer} trägt heute nichts zum Score bei.",
                affects=affect,
                detail={"field": field, "null_rate": rate},
            ))
        elif rate >= 0.75:
            findings.append(make_finding(
                SEV_M, "scan", f"{code}_PARTIAL",
                f"Scoring-Layer stark degradiert: '{field}' zu {rate:.0%} null im Top-{len(top)}.",
                affects=affect,
            ))

    # Market-PCR-Tag check
    mpcr_tag = doc.get("market_pcr_tag")
    mpcr_reason = doc.get("market_pcr_reason", "")
    if mpcr_tag in (None, "unknown"):
        findings.append(make_finding(
            SEV_H, "scan", "SCAN_MARKET_PCR_TAG_UNKNOWN",
            f"scan.market_pcr_tag='{mpcr_tag}' (reason={mpcr_reason}): PCR-Tilt wurde nicht auf Scan angewandt.",
            affects=["market_pcr_adjust"],
        ))

    if any(f["severity"] == SEV_H for f in findings):
        summary["status"] = STATUS_FAIL
    elif any(f["severity"] == SEV_M for f in findings):
        summary["status"] = STATUS_WARN

    return summary, findings


def check_source_json(date: str, source: str, source_keys: list[str],
                      now: dt.datetime | None = None) -> tuple[dict, list[dict]]:
    """Generischer Source-Health-Check für insider/options_flow/sector_rs/regime etc.

    Prüft: existiert die JSON, sind Pflicht-Source-Keys OK, sind Fehler-Strings drin.
    """
    # Datei-Lookup
    candidate_names = [f"{source}_{date}.json", f"{source.replace('_', '')}_{date}.json"]
    path = None
    for name in candidate_names:
        p = TOOLS_DIR / name
        if p.exists():
            path = p
            break
    summary = {"status": STATUS_OK, "sources": {}, "path": path.name if path else None}
    findings: list[dict] = []

    if path is None:
        # Pre-Production-Grace nur fuer aktive Tages-Producer (nicht fuer disabled
        # Layer: die uebernimmt ohnehin die V7.16-Disabled-Policy).
        if source in _ACTIVE_PRODUCERS and _is_pre_production(date, now=now):
            summary["status"] = STATUS_PENDING
            findings.append(make_finding(
                SEV_L, source, f"{source.upper()}_AWAITING_PRODUCER",
                f"Kein {source}_{date}.json: Pre-Production-Fenster (alle aktiven "
                f"Producer abwesend, lokal vor {GRACE_CUTOFF_HOUR}:00). Kein Ausfall.",
                detail={"kind": "pending"}))
            return summary, findings
        # Lagging-Producer-Grace (V7.24): bekannt langsamer Producer haengt nach,
        # waehrend schnelle Siblings schon da sind → Timing-Artefakt, kein Ausfall.
        if source in _ACTIVE_PRODUCERS and _within_lagging_grace(date, source, now=now):
            summary["status"] = STATUS_PENDING
            findings.append(make_finding(
                SEV_L, source, f"{source.upper()}_AWAITING_PRODUCER",
                f"Kein {source}_{date}.json: langsamer Producer (PREMARKET-LATENCY-1) "
                f"haengt nach, andere aktive Producer sind bereits da "
                f"(Pipeline laeuft), lokal vor {LAGGING_PRODUCER_CUTOFF_HOUR}:00. "
                f"Kein Ausfall.",
                detail={"kind": "pending", "lagging": True}))
            return summary, findings
        summary["status"] = STATUS_MISSING
        findings.append(make_finding(
            SEV_H, source, f"{source.upper()}_JSON_MISSING",
            f"Kein {source}_{date}.json im tools/ gefunden.",
        ))
        return summary, findings

    doc, err = _load_json(path)
    if err:
        kind = _classify_load_err(err)
        if kind == "env_unreadable":
            # Datei existiert (stat ok), Read scheitert → Umgebungs-Artefakt, kein Defekt.
            summary["status"] = STATUS_ENV_UNREADABLE
            findings.append(make_finding(
                SEV_L, source, f"{source.upper()}_UNREADABLE_ENV",
                f"{path.name} existiert, ist im aktuellen Lauf aber nicht lesbar "
                f"(Umgebungs-/Mount-Artefakt, kein Daten-Defekt): {err}",
                detail={"kind": "env_unreadable"}))
            return summary, findings
        summary["status"] = STATUS_FAIL
        findings.append(make_finding(
            SEV_H, source, f"{source.upper()}_PARSE_ERROR",
            f"{path.name} konnte nicht geparst werden: {err}",
        ))
        return summary, findings

    # Sources-Block (konvention in allen v6.x-JSONs)
    src_status = doc.get("sources") if isinstance(doc, dict) else None
    if src_status and isinstance(src_status, dict):
        summary["sources"] = src_status
        for skey, svalue in src_status.items():
            text = str(svalue).lower()
            if _text_has_real_error(text):
                findings.append(make_finding(
                    SEV_H, source, f"{source.upper()}_{skey.upper()}_ERROR",
                    f"{source}.sources.{skey} = {svalue!r}",
                    detail={"source_key": skey, "raw": svalue},
                ))
            elif "0/" in text and "ok (0/" in text:
                # Pattern: "ok (0/25, 25 errors)" → 100 % Fehler obwohl "ok"
                findings.append(make_finding(
                    SEV_H, source, f"{source.upper()}_{skey.upper()}_ZERO_HIT",
                    f"{source}.sources.{skey} = {svalue!r} (alle Requests failten)",
                    detail={"source_key": skey, "raw": svalue},
                ))
            elif "disabled" in text:
                pass  # by design, kein Finding
            elif "ok" in text:
                pass  # gesund

    # Source-spezifische Zusatz-Checks
    if source == "options_flow":
        mpcr = doc.get("market_pcr") if isinstance(doc, dict) else None
        if isinstance(mpcr, dict) and mpcr.get("ok") is False:
            findings.append(make_finding(
                SEV_H, source, "OPTIONS_FLOW_MARKET_PCR_DOWN",
                f"options_flow.market_pcr.ok=False → {mpcr.get('error', 'unbekannt')}",
                affects=["market_pcr_tilt", "empfehlung"],
                detail=mpcr,
            ))

    if source == "regime" and isinstance(doc, dict) \
            and str(doc.get("status", "")).lower() == "degraded":
        # regime_gate.py: VIX oder SPY fehlten -> kein Regime klassifiziert. Nie mit
        # erfundenen Default-Werten weiterhandeln: SEV_H -> CRITICAL -> Gate rc=4.
        findings.append(make_finding(
            SEV_H, source, "REGIME_DEGRADED",
            f"regime_{date}.json status=degraded (fehlend: "
            f"{', '.join(doc.get('fallback_metrics') or []) or '?'}) -> kein Regime, NO TRADE.",
            affects=["regime_gate", "exec_bot"],
            detail={"fallback_metrics": doc.get("fallback_metrics"),
                    "sources": doc.get("sources")},
        ))

    if source == "insider":
        n_flag = doc.get("n_flagged", 0) if isinstance(doc, dict) else 0
        if isinstance(n_flag, int) and n_flag == 0:
            findings.append(make_finding(
                SEV_L, source, "INSIDER_NO_FLAGS",
                "insider.n_flagged=0, kein einziger Ticker mit Insider-Signal (selten, aber möglich).",
            ))

    if any(f["severity"] == SEV_H for f in findings):
        summary["status"] = STATUS_FAIL
    elif any(f["severity"] == SEV_M for f in findings):
        summary["status"] = STATUS_WARN

    return summary, findings


def check_cross_insider_scan(date: str) -> list[dict]:
    """Cross-Source-Integritaet: misst die Coverage-Luecke zwischen abgefragtem
    Insider-Pool (ticker_map) und Scan-Top-Setups.

    Historie: Bis 2026-04-19 war diese Regel als "MERGE_MISMATCH" formuliert und
    verglich n_flagged (openinsider-Hits) mit Top-30-non-zero. Das produzierte
    False-Positives, weil der insider_feed-Default nur Tier A,B abfragte und
    internationale Ticker ueberhaupt nicht im Pool waren: die "Non-Matches"
    waren schlicht nicht abgefragt. Jetzt ehrlich: wir messen Pool-Overlap.
    """
    findings: list[dict] = []

    ins_path = TOOLS_DIR / f"insider_{date}.json"
    scan_path = TOOLS_DIR / f"scan_{date}.json"
    ins_doc, _ = _load_json(ins_path)
    scan_doc, _ = _load_json(scan_path)

    if not (isinstance(ins_doc, dict) and isinstance(scan_doc, dict)):
        return findings

    ticker_map = ins_doc.get("ticker_map") or {}
    if not ticker_map:
        return findings

    top = scan_doc.get("top_setups") or []
    if not top:
        return findings

    pool_keys = set(ticker_map.keys())
    top_tickers = [t.get("ticker") for t in top if t.get("ticker")]
    in_pool = [t for t in top_tickers if t in pool_keys]
    coverage_rate = len(in_pool) / len(top_tickers) if top_tickers else 0.0

    # Schwelle: Wenn < 30% der Top-Setups ueberhaupt im Insider-Pool sind,
    # leidet V6.5/V6.7-Scoring unter strukturellem Coverage-Gap.
    if coverage_rate < 0.30:
        findings.append(make_finding(
            SEV_M, "cross_insider_scan", "INSIDER_COVERAGE_GAP",
            (f"Nur {len(in_pool)}/{len(top_tickers)} ({coverage_rate:.0%}) der Top-Setups "
             f"sind im Insider-Feed-Pool (ticker_map hat {len(pool_keys)} Keys). "
             f"Moegliche Ursachen: (a) insider_feed --tier-Filter zu eng, "
             f"(b) zu viele internationale Ticker im Top-30 (openinsider US-only), "
             f"(c) openinsider-Source-Ausfall. "
             f"V6.5/V6.7 Insider-Layer ist fuer diese Setups neutral."),
            affects=["insider_adjust"],
            detail={"insider_pool_size": len(pool_keys),
                    "top_setups": len(top_tickers),
                    "in_pool": len(in_pool),
                    "coverage_rate": round(coverage_rate, 3),
                    "top_setups_missing_from_pool": [
                        t for t in top_tickers if t not in pool_keys
                    ][:10]},
        ))

    return findings


# ----------------------------------------------------------------------------
# Ticket-Writer (Dedup per Fingerprint in Markdown-Kommentar)
# ----------------------------------------------------------------------------

TICKET_FP_TAG = "<!-- health-fp:"


def _existing_fingerprints() -> set[str]:
    if not TICKETS_PATH.exists():
        return set()
    txt = TICKETS_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"<!--\s*health-fp:([a-f0-9]+)\s*-->", txt))


def _iso_week_header(date: str) -> str:
    y, m, d = map(int, date.split("-"))
    dd = dt.date(y, m, d)
    iso = dd.isocalendar()  # (year, week, weekday)
    week_start = dd - dt.timedelta(days=dd.weekday())
    week_end = week_start + dt.timedelta(days=6)
    return f"## KW {iso[1]:02d} ({week_start.isoformat()} bis {week_end.isoformat()})"


def append_tickets(findings: list[dict], date: str) -> int:
    if not findings:
        return 0
    tickets_written = 0
    existing = _existing_fingerprints()
    week_header = _iso_week_header(date)

    if not TICKETS_PATH.exists():
        TICKETS_PATH.write_text(
            "# Ops Ticket Log: Daily Invest\n\n"
            "> Format: `YYYY-MM-DD | Severity (L/M/H) | Task | Beschreibung | Status (OPEN/FIXED)`\n"
            "> Maintained by: tools/data_health.py (local ops log, gitignored)\n\n---\n\n",
            encoding="utf-8",
        )

    txt = TICKETS_PATH.read_text(encoding="utf-8")
    additions: list[str] = []

    for f in findings:
        if f["severity"] == SEV_L:
            continue  # L-Findings nicht ins Ticket-Log (Rauschen)
        fp = _fingerprint(date, f["source"], f["code"])
        if fp in existing:
            continue
        line = (f"{date} | {f['severity']} | daily-universe-scanner | "
                f"**[auto:data_health] {f['code']}**: {f['message']} "
                f"| OPEN {TICKET_FP_TAG}{fp} -->")
        additions.append(line)
        tickets_written += 1

    if not additions:
        return 0

    if week_header not in txt:
        txt = txt.rstrip() + "\n\n" + week_header + "\n\n" + "\n\n".join(additions) + "\n"
    else:
        txt = txt.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"

    TICKETS_PATH.write_text(txt, encoding="utf-8")
    return tickets_written


# ----------------------------------------------------------------------------
# PIPELINE-NOT-RUN-Rollup (V7.19)
# ----------------------------------------------------------------------------

def _launchd_morning_ran(date: str, tools_dir: Path | None = None) -> bool | None:
    """Best-effort: lief der native Morgen-launchd-Job am `date`?

    Liest die mtime von tools/_logs/launchd_morning.log (kumulatives Log).
    Gibt True/False zurueck wenn das Log existiert, sonst None (unbekannt:
    z. B. Sandbox ohne Log-Sync). Robust: jede Ausnahme → None, nie Crash.
    """
    td = tools_dir or TOOLS_DIR
    log = td / "_logs" / "launchd_morning.log"
    try:
        if not log.exists():
            return None
        mtime = dt.datetime.fromtimestamp(log.stat().st_mtime).date().isoformat()
        return mtime == date
    except Exception:
        return None


def _is_missing_active_producer_finding(f: dict) -> bool:
    """H-Finding eines AKTIVEN Producers, das ein fehlendes JSON meldet
    (nicht PENDING/ENV_UNREADABLE/disabled: die sind bereits SEV_L)."""
    return (
        f.get("severity") == SEV_H
        and f.get("source") in _ACTIVE_PRODUCERS
        and (f.get("code", "").endswith("_MISSING"))
    )


def apply_pipeline_rollup(findings: list[dict], date: str,
                          tools_dir: Path | None = None,
                          min_missing: int = PIPELINE_ROLLUP_MIN) -> dict:
    """Buendelt mehrere gleichzeitig fehlende aktive Producer zu EINEM
    PIPELINE_NOT_RUN-Finding (H). Mutiert `findings` in-place.

    Returns Telemetrie-Dict: {"rolled_up": int, "missing": [...], "present": [...]}.
    Greift NICHT, wenn < min_missing aktive Producer fehlen (Einzel-Ausfall
    bleibt als spezifisches Finding sichtbar).
    """
    missing_findings = [f for f in findings if _is_missing_active_producer_finding(f)]
    missing_sources = sorted({f["source"] for f in missing_findings})
    telemetry = {"rolled_up": 0, "missing": missing_sources, "present": []}

    if len(missing_sources) < min_missing:
        return telemetry

    present = sorted(
        src for src, ok in _active_producer_present(date, tools_dir).items() if ok
    )
    telemetry["present"] = present
    rolled_codes = sorted({f["code"] for f in missing_findings})

    # Einzel-Findings entfernen …
    for f in missing_findings:
        findings.remove(f)

    # … und durch EIN Sammel-Finding ersetzen.
    log_ran = _launchd_morning_ran(date, tools_dir)
    if log_ran is False:
        hint = ("launchd_morning.log wurde HEUTE nicht aktualisiert → "
                "Morgen-Job lief nicht (Mac-Sleep/launchd-Ausfall: "
                "§7 zuerst pruefen: pmset/launchd-Log).")
    elif log_ran is True:
        hint = ("launchd_morning.log ist von heute → Job startete, aber "
                "Heavy-Producer schrieben kein Artefakt (Producer-Crash/Abort "
                "oder Sandbox-EDEADLK auf Quelldateien).")
    else:
        hint = ("launchd_morning.log nicht einsehbar (Sandbox) → native "
                "Ausfuehrung/Log pruefen, bevor Code verdaechtigt wird.")

    msg = (
        f"Morgen-Pipeline nicht (vollstaendig) gelaufen: {len(missing_sources)} "
        f"aktive Producer fehlen gleichzeitig: {', '.join(missing_sources)}. "
        f"Vorhanden: {', '.join(present) if present else 'keiner'}. {hint} "
        f"NO-TRADE bis valider nativer Scan. (Sammel-Finding statt "
        f"{len(missing_sources)} Einzel-Tickets.)"
    )
    rollup = make_finding(
        SEV_H, TASK_PIPELINE, "PIPELINE_NOT_RUN", msg,
        affects=["empfehlung", "scan", "premarket"],
        detail={
            "missing": missing_sources,
            "present": present,
            "rolled_up_codes": rolled_codes,
            "rolled_up_count": len(missing_findings),
            "launchd_morning_ran_today": log_ran,
        },
    )
    findings.append(rollup)
    telemetry["rolled_up"] = len(missing_findings)
    return telemetry


# ----------------------------------------------------------------------------
# Report-Builder
# ----------------------------------------------------------------------------

def build_report(date: str, now: dt.datetime | None = None) -> dict:
    report: dict[str, Any] = {
        "date": date,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "version": "data_health/1.1",
        "overall_status": STATUS_OK,
        "summary": {},
        "sources": {},
        "findings": [],
    }

    all_findings: list[dict] = []

    # 1) scan
    s, f = check_scan(date, now=now)
    report["sources"]["scan"] = s
    all_findings.extend(f)

    # 2) generic sources
    for src in ["insider", "options_flow", "sector_rs", "regime", "premarket", "earnings"]:
        s, f = check_source_json(date, src, [], now=now)
        report["sources"][src] = s
        all_findings.extend(f)

    # 3) cross-checks
    all_findings.extend(check_cross_insider_scan(date))

    # 3.5) Disabled-Layer-Policy (Fix DATA-HEALTH-DISABLED-LAYER-BLINDNESS-1):
    # Findings deaktivierter Layer auf SEV_L zurueckstufen, BEVOR aggregiert
    # wird, sonst treibt ein by-design abgeschalteter Layer overall_status
    # faelschlich auf CRITICAL und erzeugt taeglich Falsch-Tickets.
    disabled = get_disabled_layers()
    suppressed = apply_disabled_layer_policy(all_findings, disabled)
    report["disabled_layers"] = sorted(disabled)
    report["suppressed_disabled_findings"] = suppressed

    # Per-Source-Status nach dem Downgrade neu ableiten, damit z. B. die
    # scan-Source nicht FAIL zeigt, nur weil options_flow/market_pcr null sind.
    # WICHTIG: aktive Sources mit echten H/M-Findings (z. B. ein realer
    # SECTOR_RS_JSON_MISSING) bleiben unangetastet: wir stufen NUR Sources um,
    # die ausschliesslich wegen Disabled-Layer-Findings rot waren.
    for src_name, src_info in report["sources"].items():
        src_find = [x for x in all_findings if x["source"] == src_name]
        if any(x["severity"] in (SEV_H, SEV_M) for x in src_find):
            continue  # echte Findings → Original-Status (FAIL/WARN/MISSING) lassen
        if src_info.get("status") in (STATUS_FAIL, STATUS_WARN, STATUS_MISSING):
            src_info["status"] = (STATUS_DISABLED
                                  if _source_layer(src_name) in disabled
                                  else STATUS_OK)

    # 3.6) PIPELINE-NOT-RUN-Rollup (V7.19): mehrere gleichzeitig fehlende
    # aktive Producer = EIN Ereignis → ein Sammel-Finding statt N Tickets.
    # NACH der Disabled-Policy (deaktivierte Layer sind schon SEV_L) und vor
    # der Aggregation, damit count_h die Buendelung widerspiegelt.
    pipeline_rollup = apply_pipeline_rollup(all_findings, date)
    report["pipeline_rollup"] = pipeline_rollup

    # Aggregate
    count_h = sum(1 for x in all_findings if x["severity"] == SEV_H)
    count_m = sum(1 for x in all_findings if x["severity"] == SEV_M)
    count_l = sum(1 for x in all_findings if x["severity"] == SEV_L)

    if count_h:
        report["overall_status"] = "CRITICAL"
    elif count_m:
        report["overall_status"] = "DEGRADED"
    else:
        report["overall_status"] = STATUS_OK

    report["summary"] = {
        "sources_total": len(report["sources"]),
        "sources_ok": sum(1 for v in report["sources"].values() if v["status"] == STATUS_OK),
        "sources_warn": sum(1 for v in report["sources"].values() if v["status"] == STATUS_WARN),
        "sources_fail": sum(1 for v in report["sources"].values() if v["status"] == STATUS_FAIL),
        "sources_missing": sum(1 for v in report["sources"].values() if v["status"] == STATUS_MISSING),
        "sources_disabled": sum(1 for v in report["sources"].values() if v["status"] == STATUS_DISABLED),
        "sources_pending": sum(1 for v in report["sources"].values() if v["status"] == STATUS_PENDING),
        "sources_env_unreadable": sum(1 for v in report["sources"].values() if v["status"] == STATUS_ENV_UNREADABLE),
        "findings_h": count_h,
        "findings_m": count_m,
        "findings_l": count_l,
    }

    # Advisory: alles gruen ausser noch-nicht-fertigen Producern → "warming_up"
    # (kein Incident; der Morgen-Cron darf das Briefing NICHT pausieren).
    if count_h == 0 and count_m == 0 and report["summary"]["sources_pending"] > 0:
        report["advisory"] = "warming_up"

    # Fingerprint pro Finding anhängen (nicht ins Ticket, aber zum Querverweis im JSON)
    for f in all_findings:
        f["fingerprint"] = _fingerprint(date, f["source"], f["code"])
    report["findings"] = all_findings

    return report


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def print_report(report: dict) -> None:
    s = report["summary"]
    advisory = f" · {report['advisory']}" if report.get("advisory") else ""
    print(f"\n╔══ DATA-HEALTH {report['date']} ({report['overall_status']}{advisory}) ══╗")
    print(f"  Sources: {s['sources_ok']} OK / {s['sources_warn']} WARN / {s['sources_fail']} FAIL / "
          f"{s['sources_missing']} MISSING / {s.get('sources_disabled', 0)} DISABLED / "
          f"{s.get('sources_pending', 0)} PENDING / {s.get('sources_env_unreadable', 0)} ENV_UNREADABLE")
    print(f"  Findings: {s['findings_h']} H | {s['findings_m']} M | {s['findings_l']} L")
    if report.get("disabled_layers"):
        print(f"  Disabled-Layer (by design): {', '.join(report['disabled_layers'])} "
              f"→ {report.get('suppressed_disabled_findings', 0)} Findings auf L zurueckgestuft")
    print(f"╚{'═' * 40}╝")
    for name, info in report["sources"].items():
        extra = ""
        if info.get("fill_rate") is not None:
            extra = f" fill={info['fill_rate']:.1%}"
        print(f"  [{info['status']:>7}] {name:<14}{extra}")
    if report["findings"]:
        print("\n  Findings:")
        for f in report["findings"]:
            print(f"    [{f['severity']}] {f['source']:<20} {f['code']:<32} {f['message']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Data-Health-Monitor für Daily Invest")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="ISO-Datum (default: heute)")
    ap.add_argument("--no-tickets", action="store_true", help="Tickets nicht in ops_tickets.md schreiben")
    ap.add_argument("--quiet", action="store_true", help="Kein stdout, nur Exit-Code")
    ap.add_argument("--report-out", default=None, help="Report-Pfad override (default: tools/data_health_DATE.json)")
    args = ap.parse_args()

    report = build_report(args.date)

    out_path = Path(args.report_out) if args.report_out else TOOLS_DIR / f"data_health_{args.date}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    tickets_written = 0
    if not args.no_tickets:
        tickets_written = append_tickets(report["findings"], args.date)

    if not args.quiet:
        print_report(report)
        print(f"Report: {out_path.relative_to(PROJECT_ROOT)}")
        if tickets_written:
            print(f"+ {tickets_written} neue Tickets in ops_tickets.md geschrieben (Rest dedup'd).")
        elif not args.no_tickets:
            print("Keine neuen Tickets: alle Findings bereits im Log (Dedup).")

    if report["overall_status"] == "CRITICAL":
        return 2
    if report["overall_status"] == "DEGRADED":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
