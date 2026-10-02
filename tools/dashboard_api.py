"""
Dashboard V7 Steuerungs-API
----------------------------
Dünner localhost-Microservice für das Cockpit V7. Bindet ausschließlich an
127.0.0.1, kein Netzwerk-Exposure. On-Demand starten, nicht als Scheduled-Task.

Start:     python3 tools/dashboard_api.py
Port:      5757 (überschreibbar via $DASHBOARD_API_PORT)

Endpunkte:
    GET  /ping                    -> {"ok": true}
    POST /run/morning             -> triggert Morgen-Briefing (run_morning.sh | morning_briefing.py | daily_scanner.py)
    POST /run/evening             -> triggert Abend-Tracking (run_evening.sh | evening_tracking.py)
    POST /trade_log               -> Trade eintragen; BUY = neue Zeile, SELL = schließt offene Position gleichen Tickers
                                     Pflicht: ticker, side, qty, price, entry_date
                                     Optional (BUY): stop, target, score, rationale
                                     Bei SELL + nicht vorhandener offener Position -> Fehler (statt still doppelt loggen)
                                     Auto-Sync von trades_snapshot.json nach erfolgreichem Eintrag
    POST /state/set               -> überschreibt state.json Felder (kill_switch, dd_level)
    POST /state/sync              -> regeneriert trades_snapshot.json aus trade_journal.sqlite

    --- V7.1 Dashboard-Krass-Edition (2026-04-17) ---
    GET  /benchmark               -> Performance vs. SPY + QQQ (%-Serie seit start_date oder 90d)
    GET  /history?limit=50        -> letzte geschlossene Trades mit PnL, Regime-Snapshot, Duration
    GET  /correlation             -> 30-Tage-Korrelationsmatrix der offenen Positionen
    GET  /sparks                  -> 30-Tage-Mini-Serien für WR, PF, DD, Equity (KPI-Sparklines)
    GET  /regime_history?days=60  -> letzte N Regime-Snapshots als Strip-Daten

Sicherheit:
    - Host-Bind auf 127.0.0.1, nie 0.0.0.0
    - Kein Auth (ist lokale Machine). Wer den Port erreicht, steht physisch am Rechner.
    - Trade-Log setzt KEINE Orders bei einem Broker ab, das bleibt manuell.
    - Requests mit fremdem Origin oder fremdem Host-Header werden VOR dem
      Handler abgewiesen (403), damit fremde Webseiten keine POSTs ausloesen.
    - Die API niemals ueber 127.0.0.1 hinaus exponieren (kein 0.0.0.0, kein Tunnel).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sqlite3
import threading
import time
from collections import OrderedDict
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:
    raise SystemExit("Flask nicht installiert. Installiere mit: pip install flask --break-system-packages")


# --- Event-Bus für SSE-Live-Push (Phase C) --------------------------------
class _EventBus:
    """Thread-safe Pub/Sub für SSE. Jeder Subscriber bekommt eine eigene Queue.
    publish() ist non-blocking: volle Queues werden einfach geskippt."""
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: str, data: dict) -> int:
        payload = {"event": event, "data": data,
                   "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        delivered = 0
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
                delivered += 1
            except queue.Full:
                pass
        return delivered

EventBus = _EventBus()

ROOT = Path(__file__).resolve().parent.parent
DASH_V7 = ROOT / "dashboard" / "v7"
STATE_PATH = DASH_V7 / "state.json"            # runtime file, gitignored
STATE_EXAMPLE_PATH = DASH_V7 / "state.example.json"  # shipped template
HEARTBEAT_PATH = DASH_V7 / "heartbeat.json"
TRADES_SNAPSHOT_PATH = DASH_V7 / "trades_snapshot.json"
TRADE_DB = ROOT / "trade_journal.sqlite"

PORT = int(os.environ.get("DASHBOARD_API_PORT", "5757"))

# Vollständiges trades-Schema (idempotent via CREATE IF NOT EXISTS + ALTER TABLE)
# V7.2 (2026-04-17): route, tp1, tp2, setup_fk, regime_at_entry, risk_eur,
#                    account_pct, max_hold_days, exit_reason, hebel_factor
TRADES_SCHEMA_COLUMNS = [
    # (name, sql_type, default_clause_or_None)
    ("ticker",          "TEXT",    None),
    ("side",            "TEXT",    None),
    ("qty",             "INTEGER", None),
    ("entry_price",     "REAL",    None),
    ("entry_date",      "TEXT",    None),
    ("exit_price",      "REAL",    None),
    ("exit_date",       "TEXT",    None),
    ("stop",            "REAL",    None),
    ("target",          "REAL",    None),
    ("tp1",             "REAL",    None),   # Teilziel 1 (partial exit)
    ("tp2",             "REAL",    None),   # Teilziel 2 (full exit)
    ("score",           "REAL",    None),
    ("rationale",       "TEXT",    None),
    ("route",           "TEXT",    None),   # DIRECT | HEBEL_KO | HEBEL_OS | FAKTOR
    ("hebel_factor",    "REAL",    None),   # z.B. 3 / 5 / 10 bei Faktor/Hebel-Routen
    ("setup_fk",        "TEXT",    None),   # Connors-SMA5 | Breakout | MeanRev | Insider | Congress | External | Custom
    ("regime_at_entry", "TEXT",    None),   # Snapshot aus regime_DATE.json
    ("risk_eur",        "REAL",    None),   # (entry - stop) * qty
    ("account_pct",     "REAL",    None),   # Position % des Equity zum Einstiegszeitpunkt
    ("max_hold_days",   "INTEGER", None),   # Default 5
    ("exit_reason",     "TEXT",    None),   # STOP_HIT | TP1 | TP2 | TIME_STOP | DISCRETION | REGIME_FLIP | EMERGENCY
    ("created_at_utc",  "TEXT",    None),
    ("updated_at_utc",  "TEXT",    None),
    # --- V7.4 "Discipline Layer" (2026-04-17) ---
    ("thesis_why",          "TEXT", None),  # "Warum dieser Trade?"
    ("thesis_confirm_if",   "TEXT", None),  # "Was muss passieren, damit die These stimmt?"
    ("thesis_invalidate_if","TEXT", None),  # "Was invalidiert sie?"
    ("checklist_json",      "TEXT", None),  # Pre-Trade-Checkliste als JSON
    ("thesis_verdict",      "TEXT", None),  # bestätigt | widerlegt | irrelevant
    ("lesson",              "TEXT", None),  # Freitext "was habe ich gelernt?"
]

EXIT_REASONS = {"STOP_HIT","TP1","TP2","TIME_STOP","DISCRETION","REGIME_FLIP","EMERGENCY"}
ROUTES       = {"DIRECT","HEBEL_KO","HEBEL_OS","FAKTOR"}
SETUPS       = {"Connors-SMA5","Breakout","MeanRev","Insider","Congress","External","Custom"}

app = Flask(__name__)


# --- CORS · Origin-Whitelist statt "*" (SEC-1) -----------------------------
# DNS-Rebinding-Härtung: nur tatsächliche Lokal-Origins dürfen fetchen.
# "null" = file:// im Browser, "" = kein Origin-Header (curl).
_ALLOWED_ORIGINS_EXACT = {"null", ""}
_ALLOWED_HOST_PATTERNS = ("http://localhost", "http://127.0.0.1",
                          "https://localhost", "https://127.0.0.1")

def _origin_allowed(origin: str) -> bool:
    if origin in _ALLOWED_ORIGINS_EXACT:
        return True
    return any(origin.startswith(p) for p in _ALLOWED_HOST_PATTERNS)


def _host_allowed() -> bool:
    host = (request.headers.get("Host") or "").lower().split(":")[0]
    return (not host) or host in ("127.0.0.1", "localhost")


@app.before_request
def _reject_foreign_requests():
    """Abweisen BEVOR ein Handler laeuft (sonst haette ein fremder POST seinen
    Seiteneffekt schon ausgeloest, bevor after_request den Status auf 403 setzt)."""
    if request.method == "OPTIONS":
        return None
    origin = request.headers.get("Origin", "")
    if not _origin_allowed(origin) or not _host_allowed():
        return (jsonify(ok=False, msg="Origin/Host nicht erlaubt"), 403)
    return None


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    if _origin_allowed(origin):
        # Echo-Back nur erlaubte Origins (statt blankes "*")
        resp.headers["Access-Control-Allow-Origin"] = origin or "null"
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Host-Header-Check: DNS-Rebinding verlangt, dass Host auf 127.0.0.1/localhost
    # zeigen muss: andere Hosts wurden über DNS umgeleitet.
    host = (request.headers.get("Host") or "").lower().split(":")[0]
    if host and host not in ("127.0.0.1", "localhost", ""):
        # Hard-Block: Response überschreiben
        resp.status_code = 403
        resp.set_data(b'{"ok":false,"msg":"Host-Header nicht erlaubt"}')
        resp.mimetype = "application/json"
    return resp


@app.route("/<path:_any>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def _opts(_any=""):
    return ("", 204)


# --- Helpers ---------------------------------------------------------------
def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Error-Sanitizer (SEC-2) ----------------------------------------------
import logging
_logger = logging.getLogger("dashboard_api")
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)

def _safe_err(e: Exception, public_prefix: str = "Interner Fehler") -> str:
    """Loggt volle Exception server-seitig, gibt generische Message an Client.
    Verhindert, dass Pfade/Secrets via repr() geleakt werden."""
    _logger.exception("%s: %s", public_prefix, type(e).__name__)
    return f"{public_prefix} ({type(e).__name__})"


def _now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return dict(default)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _ensure_state_file() -> None:
    """First run: create dashboard/v7/state.json from the shipped template.

    state.json is a local runtime file (kill switch, DD level) and is gitignored,
    so a fresh clone only has state.example.json. Never store account values here.
    """
    if STATE_PATH.exists():
        return
    try:
        template = json.loads(STATE_EXAMPLE_PATH.read_text())
    except Exception:
        template = {"schema_version": 1, "kill_switch": "off", "dd_level": 0}
    template["updated_at_utc"] = _now_utc()
    _write_json(STATE_PATH, template)


def _update_heartbeat(key: str) -> None:
    hb = _read_json(HEARTBEAT_PATH, {"schema_version": 1})
    hb[key] = _now_local()
    hb["generated_at_utc"] = _now_utc()
    _write_json(HEARTBEAT_PATH, hb)


def _run_script(candidate_paths: list[Path]) -> tuple[bool, str]:
    """Führt das erste vorhandene Script aus. Return (ok, msg)."""
    for p in candidate_paths:
        if p.exists():
            try:
                r = subprocess.run(
                    ["bash", str(p)] if p.suffix == ".sh" else ["python3", str(p)],
                    capture_output=True, text=True, timeout=180, cwd=str(ROOT),
                )
                ok = r.returncode == 0
                tail = (r.stdout or r.stderr or "").strip().splitlines()[-3:]
                return ok, f"{p.name} rc={r.returncode} · " + " | ".join(tail)
            except subprocess.TimeoutExpired:
                return False, f"{p.name} Timeout (>180s)"
            except Exception as e:
                return False, f"{p.name} Fehler: {type(e).__name__}"
    return False, "Keines der Kandidaten-Scripts gefunden: " + ", ".join(str(p.name) for p in candidate_paths)


def _ensure_schema(con: sqlite3.Connection) -> list[str]:
    """Sorgt defensiv dafür, dass trades + daily_equity existieren und alle nötigen
    Spalten haben. Neue Spalten werden per ALTER TABLE ADD COLUMN nachgezogen.
    Return Liste der migrierten Spalten (für Logging)."""
    cur = con.cursor()
    # Basistabelle
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            side TEXT,
            qty INTEGER,
            entry_price REAL,
            entry_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_equity (
            date TEXT PRIMARY KEY,
            equity REAL
        )
    """)
    # Bestehende Spalten abfragen
    existing = {row[1] for row in cur.execute("PRAGMA table_info(trades)").fetchall()}
    migrated: list[str] = []
    for name, sql_type, _default in TRADES_SCHEMA_COLUMNS:
        if name not in existing:
            cur.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")
            migrated.append(name)
    con.commit()
    return migrated


def _regenerate_snapshot(con: sqlite3.Connection) -> dict:
    """Schreibt trades_snapshot.json neu. Nimmt bestehende Connection an."""
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Equity-Series (letzte 90 Tage, chronologisch)
    rows = cur.execute(
        "SELECT date, equity FROM daily_equity ORDER BY date DESC LIMIT 90"
    ).fetchall()
    equity_series = [{"date": r["date"], "equity": r["equity"]} for r in reversed(rows)]
    equity_now = equity_series[-1]["equity"] if equity_series else 10000.0

    peak = max((x["equity"] for x in equity_series), default=equity_now)
    current_dd_pct = (equity_now - peak) / peak if peak else 0.0
    day_pnl = (equity_series[-1]["equity"] - equity_series[-2]["equity"]) if len(equity_series) >= 2 else 0.0

    # Offene Positionen
    open_rows = cur.execute("""
        SELECT id, ticker, entry_price, qty, stop, target, score, rationale, entry_date
          FROM trades
         WHERE (exit_date IS NULL OR exit_date = '')
           AND side = 'BUY'
      ORDER BY id DESC
         LIMIT 50
    """).fetchall()
    open_positions = [
        {
            "id": r["id"],
            "ticker": r["ticker"],
            "entry_price": r["entry_price"],
            "qty": r["qty"],
            "stop": r["stop"],
            "target": r["target"],
            "score": r["score"],
            "rationale": r["rationale"],
            "entry_date": r["entry_date"],
            "pnl_pct": 0.0,  # wird in Phase 3 mit Live-Kursen berechnet
        }
        for r in open_rows
    ]

    snap = {
        "schema_version": 2,
        "generated_at_utc": _now_utc(),
        "equity_now": equity_now,
        "day_pnl": day_pnl,
        "current_dd_pct": current_dd_pct,
        "open_positions": open_positions,
        "equity_series": equity_series,
    }
    _write_json(TRADES_SNAPSHOT_PATH, snap)
    return snap


# --- Endpunkte -------------------------------------------------------------
@app.get("/ping")
def ping():
    return jsonify(ok=True, ts=_now_utc(), service="dashboard_api", port=PORT)


@app.get("/stream")
def stream():
    """Server-Sent-Events Live-Push. Broadcasted Events:
        - trade_logged      : Trade wurde ins Journal geschrieben
        - state_change      : kill_switch / dd_level geändert
        - regime_refresh    : Regime-Snapshot neu berechnet (extern getriggert)
        - dd_alert          : DD-Schwelle überschritten (5/8/10 %)
        - heartbeat_tick    : alle 15s, damit Proxies die Connection nicht killen

    Frontend: new EventSource("http://127.0.0.1:5757/stream")
              .addEventListener("trade_logged", ...)"""
    q = EventBus.subscribe()

    def gen():
        # Begrüßungs-Event: aktueller State
        try:
            state = _read_json(STATE_PATH, {"kill_switch":"off","dd_level":0})
            yield f"event: hello\ndata: {json.dumps(state)}\n\n"
        except Exception:
            pass
        last_tick = time.time()
        try:
            while True:
                try:
                    ev = q.get(timeout=3.0)
                    yield f"event: {ev['event']}\ndata: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    pass
                if time.time() - last_tick > 15:
                    last_tick = time.time()
                    payload = {"event":"heartbeat_tick","ts":_now_utc()}
                    yield f"event: heartbeat_tick\ndata: {json.dumps(payload)}\n\n"
        except GeneratorExit:
            EventBus.unsubscribe(q)
            return

    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache",
                             "X-Accel-Buffering":"no",
                             "Connection":"keep-alive"})


@app.post("/run/morning")
def run_morning():
    ok, msg = _run_script([
        ROOT / "tools" / "run_morning.sh",
        ROOT / "tools" / "morning_briefing.py",
        ROOT / "tools" / "daily_scanner.py",
    ])
    if ok:
        _update_heartbeat("last_briefing_local")
    return jsonify(ok=ok, msg=msg), (200 if ok else 500)


@app.post("/run/evening")
def run_evening():
    ok, msg = _run_script([
        ROOT / "tools" / "run_evening.sh",
        ROOT / "tools" / "evening_tracking.py",
        ROOT / "tools" / "evening_exit_tracking.py",
    ])
    if ok:
        _update_heartbeat("last_evening_local")
    return jsonify(ok=ok, msg=msg), (200 if ok else 500)


# V7.5: Idempotency-Cache gegen Duplicate-Submits. Der Client schickt pro
# Klick einen frischen client_key (UUID). Kommt derselbe Key innerhalb 60s
# erneut an, antworten wir mit dem zwischengespeicherten Response statt eine
# zweite DB-Row zu schreiben. Hart bounded auf 128 Keys (LRU), damit der
# Speicher auch bei langlaufenden Sessions nicht wandert.
_IDEMP_WINDOW_SEC = 60
_IDEMP_MAX_KEYS = 128
_idemp_cache: "OrderedDict[str, tuple[float, tuple]]" = OrderedDict()
_idemp_lock = threading.Lock()

def _idemp_get(key: str):
    """Gibt (status, body_json_dict) zurueck wenn der Key im Window ist, sonst None.
    Strippt nebenbei abgelaufene Eintraege."""
    if not key:
        return None
    now = time.time()
    with _idemp_lock:
        expired = [k for k, (ts, _) in _idemp_cache.items() if now - ts > _IDEMP_WINDOW_SEC]
        for k in expired:
            _idemp_cache.pop(k, None)
        if key in _idemp_cache:
            _idemp_cache.move_to_end(key)
            return _idemp_cache[key][1]
    return None

def _idemp_put(key: str, payload: tuple):
    """Speichert (status, body_dict) unter key mit LRU-Eviction."""
    if not key:
        return
    with _idemp_lock:
        _idemp_cache[key] = (time.time(), payload)
        _idemp_cache.move_to_end(key)
        while len(_idemp_cache) > _IDEMP_MAX_KEYS:
            _idemp_cache.popitem(last=False)


@app.post("/trade_log")
def trade_log():
    """BUY: INSERT neue Zeile. SELL: schliesst zuletzt offene Position gleichen Tickers.
    Auto-Sync von trades_snapshot.json nach erfolgreichem Eintrag.

    V7.2-Felder (optional, aber empfohlen):
        BUY:  stop, tp1, tp2, target, score, rationale,
              route {DIRECT,HEBEL_KO,HEBEL_OS,FAKTOR}, hebel_factor,
              setup_fk, regime_at_entry, max_hold_days (default 5)
        SELL: exit_reason {STOP_HIT,TP1,TP2,TIME_STOP,DISCRETION,REGIME_FLIP,EMERGENCY},
              rationale
    Server berechnet automatisch: risk_eur, account_pct (aus letzter Equity).

    V7.5: Optionaler `client_key` als Idempotency-Token. Wiederholung desselben
    Keys innerhalb 60s liefert das Original-Response zurueck, keine Doppel-Row."""
    data = request.get_json(force=True, silent=True) or {}

    # V7.5: Idempotency-Check (vor jeder Validierung, damit Duplicate-Submits
    # nicht mal die Validator-Fehlermeldung zweimal sehen).
    client_key = str(data.get("client_key") or "").strip()
    if client_key:
        prev = _idemp_get(client_key)
        if prev is not None:
            prev_status, prev_body = prev
            body = dict(prev_body)
            body["msg"] = str(body.get("msg", "")) + " \u00b7 [Duplicate-Submit ignoriert]"
            return jsonify(**body), prev_status

    required = ["ticker", "side", "qty", "price", "entry_date"]
    missing = [k for k in required if k not in data or data[k] in (None, "")]
    if missing:
        return jsonify(ok=False, msg=f"Pflichtfelder fehlen: {missing}"), 400

    ticker = str(data["ticker"]).upper().strip()
    side = str(data["side"]).upper().strip()
    if side not in ("BUY", "SELL"):
        return jsonify(ok=False, msg="side muss 'BUY' oder 'SELL' sein"), 400

    try:
        qty = int(data["qty"])
        price = float(data["price"])
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="qty muss int, price muss float sein"), 400
    if qty <= 0 or price <= 0:
        return jsonify(ok=False, msg="qty und price müssen > 0 sein"), 400

    entry_date = str(data["entry_date"]).strip()

    # --- Numerische Optional-Felder defensiv parsen ---
    def _opt_float(key):
        v = data.get(key)
        if v in (None, ""): return None
        try: return float(v)
        except (TypeError, ValueError): return "ERR"
    def _opt_int(key):
        v = data.get(key)
        if v in (None, ""): return None
        try: return int(v)
        except (TypeError, ValueError): return "ERR"

    stop          = _opt_float("stop")
    target        = _opt_float("target")
    tp1           = _opt_float("tp1")
    tp2           = _opt_float("tp2")
    score         = _opt_float("score")
    hebel_factor  = _opt_float("hebel_factor")
    max_hold      = _opt_int("max_hold_days")

    for k, v in [("stop",stop),("target",target),("tp1",tp1),("tp2",tp2),
                 ("score",score),("hebel_factor",hebel_factor),("max_hold_days",max_hold)]:
        if v == "ERR":
            return jsonify(ok=False, msg=f"{k} muss numerisch sein (oder leer lassen)"), 400

    rationale       = (str(data.get("rationale")).strip() if data.get("rationale") else None)
    route           = (str(data.get("route")).strip().upper() if data.get("route") else None)
    setup_fk        = (str(data.get("setup_fk")).strip() if data.get("setup_fk") else None)
    regime_at_entry = (str(data.get("regime_at_entry")).strip() if data.get("regime_at_entry") else None)
    exit_reason     = (str(data.get("exit_reason")).strip().upper() if data.get("exit_reason") else None)

    # V7.4 S2: Thesis-Tracking + Pre-Trade-Checklist (alle optional, BUY-only)
    thesis_why          = (str(data.get("thesis_why")).strip()          if data.get("thesis_why")          else None)
    thesis_confirm_if   = (str(data.get("thesis_confirm_if")).strip()   if data.get("thesis_confirm_if")   else None)
    thesis_invalidate_if= (str(data.get("thesis_invalidate_if")).strip() if data.get("thesis_invalidate_if") else None)
    checklist_in        = data.get("checklist") or data.get("checklist_json")
    checklist_json      = None
    if checklist_in is not None:
        try:
            # Dict/Liste → JSON-String; String bleibt (sollte valides JSON sein)
            if isinstance(checklist_in, (dict, list)):
                checklist_json = json.dumps(checklist_in, ensure_ascii=False)
            else:
                # Validieren, dass es sich um parsebares JSON handelt
                json.loads(str(checklist_in))
                checklist_json = str(checklist_in)
        except Exception:
            return jsonify(ok=False, msg="checklist muss gültiges JSON sein"), 400

    if route and route not in ROUTES:
        return jsonify(ok=False, msg=f"route muss einer von {sorted(ROUTES)} sein"), 400
    if setup_fk and setup_fk not in SETUPS:
        return jsonify(ok=False, msg=f"setup_fk muss einer von {sorted(SETUPS)} sein"), 400
    if exit_reason and exit_reason not in EXIT_REASONS:
        return jsonify(ok=False, msg=f"exit_reason muss einer von {sorted(EXIT_REASONS)} sein"), 400

    # --- BUY: Risiko-Validierung + Enforcement der 2%-Regel ---
    if side == "BUY":
        if stop is not None and stop >= price:
            return jsonify(ok=False,
                           msg=f"Stop ({stop}) muss unter Entry ({price}) liegen für Long."), 400
        if tp1 is not None and tp1 <= price:
            return jsonify(ok=False,
                           msg=f"TP1 ({tp1}) muss über Entry ({price}) liegen für Long."), 400
        if tp2 is not None and tp1 is not None and tp2 <= tp1:
            return jsonify(ok=False, msg=f"TP2 ({tp2}) muss über TP1 ({tp1}) liegen."), 400
        if max_hold is not None and max_hold <= 0:
            return jsonify(ok=False, msg="max_hold_days muss > 0 sein"), 400

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.execute("PRAGMA journal_mode=WAL")  # SEC-6: concurrent reader-safe
        migrated = _ensure_schema(con)
        cur = con.cursor()

        # Letzte Equity für risk-%-Berechnung
        last_eq_row = cur.execute(
            "SELECT equity FROM daily_equity ORDER BY date DESC LIMIT 1"
        ).fetchone()
        last_equity = float(last_eq_row[0]) if last_eq_row and last_eq_row[0] else None

        if side == "BUY":
            risk_eur    = (price - stop) * qty if (stop is not None) else None
            account_pct = (price * qty / last_equity * 100.0) if last_equity else None

            # Soft-Warnung wenn Risiko > 2% (wird im Response zurückgegeben, blockt nicht)
            warn = None
            if risk_eur is not None and last_equity:
                risk_pct_of_equity = risk_eur / last_equity * 100.0
                if risk_pct_of_equity > 2.0:
                    warn = (f"⚠ Risiko {risk_eur:.0f} € = {risk_pct_of_equity:.2f} % "
                            f"des Kontos > 2 % MCR-Regel")

            now_utc = _now_utc()
            cur.execute("""
                INSERT INTO trades
                    (ticker, side, qty, entry_price, entry_date,
                     stop, target, tp1, tp2, score, rationale,
                     route, hebel_factor, setup_fk, regime_at_entry,
                     risk_eur, account_pct, max_hold_days,
                     thesis_why, thesis_confirm_if, thesis_invalidate_if, checklist_json,
                     created_at_utc, updated_at_utc)
                VALUES (?, 'BUY', ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?)
            """, (ticker, qty, price, entry_date,
                  stop, target, tp1, tp2, score, rationale,
                  route, hebel_factor, setup_fk, regime_at_entry,
                  risk_eur, account_pct, (max_hold if max_hold is not None else 5),
                  thesis_why, thesis_confirm_if, thesis_invalidate_if, checklist_json,
                  now_utc, now_utc))
            con.commit()
            rowid = cur.lastrowid
            parts = [f"BUY #{rowid} {qty}× {ticker} @ {price}"]
            if stop is not None:   parts.append(f"Stop {stop}")
            if tp1  is not None:   parts.append(f"TP1 {tp1}")
            if score is not None:  parts.append(f"Score {score}")
            if route:              parts.append(f"Route {route}")
            if setup_fk:           parts.append(f"Setup {setup_fk}")
            if risk_eur is not None: parts.append(f"Risk {risk_eur:.0f}€")
            action_msg = " · ".join(parts)
            if warn:               action_msg += " · " + warn

        else:  # SELL -> schließt offene Position
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            open_row = cur.execute("""
                SELECT id, qty, entry_price FROM trades
                 WHERE ticker = ? AND side = 'BUY'
                   AND (exit_date IS NULL OR exit_date = '')
              ORDER BY id DESC LIMIT 1
            """, (ticker,)).fetchone()

            if open_row is None:
                con.close()
                return jsonify(
                    ok=False,
                    msg=f"Keine offene BUY-Position für {ticker} gefunden. SELL nicht eingetragen."
                ), 409

            if open_row["qty"] != qty:
                note = (f" (offen: {open_row['qty']}, schließend: {qty}, "
                        "Teilverkauf nicht unterstützt, Position komplett geschlossen)")
            else:
                note = ""

            cur.execute("""
                UPDATE trades
                   SET exit_date = ?, exit_price = ?, exit_reason = ?,
                       rationale = COALESCE(?, rationale),
                       updated_at_utc = ?
                 WHERE id = ?
            """, (entry_date, price, exit_reason, rationale, _now_utc(), open_row["id"]))
            con.commit()
            entry = float(open_row["entry_price"])
            pnl_abs = (price - entry) * int(open_row["qty"])
            pnl_pct = (price - entry) / entry * 100.0
            parts = [f"SELL #{open_row['id']} {ticker} @ {price}",
                     f"PnL {pnl_abs:+.2f} ({pnl_pct:+.2f}%)"]
            if exit_reason: parts.append(f"Reason {exit_reason}")
            action_msg = " · ".join(parts) + note

        # Auto-Sync trades_snapshot.json
        snap = _regenerate_snapshot(con)
        con.close()

        msg = action_msg
        if migrated:
            msg += f" · Schema migriert: {', '.join(migrated)}"
        msg += f" · Snapshot aktualisiert ({len(snap['open_positions'])} offene Positionen)"
        # SSE-Push (Phase C)
        try:
            EventBus.publish("trade_logged",
                             {"ticker": ticker, "side": side, "qty": qty,
                              "price": price, "msg": action_msg})
        except Exception:
            pass
        # V7.5: Idempotency-Cache nach erfolgreichem Insert/Update
        response_body = {"ok": True, "msg": msg}
        if client_key:
            _idemp_put(client_key, (200, response_body))
        return jsonify(**response_body)

    except sqlite3.OperationalError as e:
        return jsonify(ok=False, msg=_safe_err(e, "SQLite-Fehler")), 500
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Trade-Log-Fehler")), 500


@app.get("/lookup")
def lookup():
    """Auto-Fill für Trade-Entry-Formular.
    ?ticker=XYZ&date=YYYY-MM-DD -> {score, regime, atr_stop, equity, suggested_qty_1pct}
    Wenn kein Ticker: nur globale Snapshots (regime, equity)."""
    ticker = (request.args.get("ticker") or "").upper().strip()
    date   = (request.args.get("date")   or datetime.now().strftime("%Y-%m-%d")).strip()

    out = {"ok": True, "ticker": ticker or None, "date": date,
           "score": None, "atr": None, "atr_stop": None,
           "regime": None, "size_multiplier": None,
           "last_equity": None, "suggested_qty_1pct": None,
           "open_exists": False}

    # Regime-Snapshot
    reg_path = ROOT / "Meta" / "regime-history" / f"regime_{date}.json"
    if reg_path.exists():
        try:
            r = json.loads(reg_path.read_text())
            out["regime"] = r.get("regime")
            out["size_multiplier"] = r.get("size_multiplier")
        except Exception:
            pass

    # Scan-Daten → Score + ATR
    scan_path = ROOT / "tools" / f"scan_{date}.json"
    if ticker and scan_path.exists():
        try:
            sc = json.loads(scan_path.read_text())
            rows = sc.get("rows") or sc.get("top_setups") or sc.get("results") or []
            for r in rows:
                rt = (r.get("ticker") or "").upper()
                if rt == ticker:
                    out["score"] = r.get("pre_score") or r.get("score")
                    atr = r.get("atr") or r.get("atr_14") or r.get("atr_20")
                    out["atr"] = atr
                    close = r.get("close") or r.get("entry_hint") or r.get("price")
                    if atr and close:
                        # 2.0 × ATR unter Close als Stop-Vorschlag
                        out["atr_stop"] = round(float(close) - 2.0 * float(atr), 2)
                    break
        except Exception:
            pass

    # Letzte Equity + Position-Size-Vorschlag (1 % Risk)
    try:
        con = sqlite3.connect(str(TRADE_DB))
        _ensure_schema(con)
        row = con.execute(
            "SELECT equity FROM daily_equity ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            out["last_equity"] = float(row[0])
        # Offene Position für diesen Ticker?
        if ticker:
            r = con.execute("""
                SELECT 1 FROM trades WHERE ticker=? AND side='BUY'
                  AND (exit_date IS NULL OR exit_date='') LIMIT 1
            """, (ticker,)).fetchone()
            out["open_exists"] = bool(r)
        con.close()
    except Exception:
        pass

    # Size-Vorschlag bei 1 % Risk pro Trade
    if out["last_equity"] and out["atr_stop"] and ticker:
        try:
            # Risk pro Share = (Close − Stop); Close wird im Frontend gesetzt
            # Hier nur Hinweis auf 1-%-Budget
            out["risk_budget_1pct"] = round(out["last_equity"] * 0.01, 2)
        except Exception:
            pass

    return jsonify(out)


@app.get("/open_positions")
def open_positions():
    """Liste offener Positionen für den Sell-Dialog."""
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        rows = con.execute("""
            SELECT id, ticker, qty, entry_price, entry_date,
                   stop, target, tp1, tp2, score, setup_fk, regime_at_entry, route
              FROM trades
             WHERE side='BUY' AND (exit_date IS NULL OR exit_date='')
          ORDER BY id DESC
        """).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500
    return jsonify(ok=True, positions=[dict(r) for r in rows])


# ============================================================================
# V7.6 · Risk-Foundation (2026-04-20)
#   /portfolio/risk: kombiniert Heat, Daily-Loss, Drawdown in einem Call.
# ============================================================================
def _portfolio_capital(default: float = 10000.0) -> float:
    try:
        st = _read_json(STATE_PATH, {})
        v = float(st.get("account_equity") or st.get("equity") or default)
        return v if v > 0 else default
    except Exception:
        return default


@app.get("/portfolio/risk")
def portfolio_risk():
    """V7.6: Kombinierter Risk-Status (Heat · Daily-Loss · Drawdown).
    Query:
      ?capital=EUR  (default aus state.json, sonst 10000)
    """
    try:
        capital = float(request.args.get("capital") or 0.0)
    except (TypeError, ValueError):
        capital = 0.0
    if capital <= 0:
        capital = _portfolio_capital()
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)

        # ---- HEAT: Summe risk_eur offener Positionen ----------------------
        open_rows = con.execute(
            """SELECT id, ticker, qty, entry_price, stop, target, risk_eur,
                      tp1, tp2, setup_fk
                 FROM trades
                WHERE side='BUY' AND (exit_date IS NULL OR exit_date='')
             ORDER BY id DESC"""
        ).fetchall()
        heat_positions = []
        heat_sum = 0.0
        for r in open_rows:
            risk = r["risk_eur"] or 0.0
            if (not risk) and r["stop"] and r["entry_price"] and r["qty"]:
                risk = abs((r["entry_price"] - r["stop"]) * r["qty"])
            heat_sum += risk
            heat_positions.append({
                "id": r["id"], "ticker": r["ticker"], "qty": r["qty"],
                "entry": r["entry_price"], "stop": r["stop"],
                "risk_eur": round(risk, 2),
                "pct": round(risk / capital * 100.0, 2) if capital > 0 else 0.0,
            })
        heat_pct = (heat_sum / capital * 100.0) if capital > 0 else 0.0
        heat_band = "ok" if heat_pct < 3.0 else "warn" if heat_pct < 5.0 else "bad"

        # ---- DAILY-LOSS: heute geschlossene Trades in R und EUR -----------
        today_rows = con.execute(
            """SELECT netto_pnl, brutto_pnl, r_multiple, risk_eur,
                      entry_price, exit_price, stop, qty
                 FROM trades
                WHERE exit_date = ? AND exit_price IS NOT NULL""",
            (today,),
        ).fetchall()
        pnl_today = sum(((r["netto_pnl"] if r["netto_pnl"] is not None
                          else r["brutto_pnl"]) or 0.0) for r in today_rows)
        r_today = 0.0
        for r in today_rows:
            if r["r_multiple"] is not None:
                r_today += r["r_multiple"]
            elif r["stop"] and r["entry_price"] and r["exit_price"] and r["qty"]:
                risk_per = abs(r["entry_price"] - r["stop"])
                if risk_per > 0:
                    r_today += (r["exit_price"] - r["entry_price"]) / risk_per
        dl_band = "ok" if r_today > -1.0 else "warn" if r_today > -2.0 else "bad"
        dl_lock = r_today <= -2.0

        # ---- DRAWDOWN: aktuelle Equity vs. ATH ---------------------------
        eq = _read_equity_series(con, days=365)
        if eq:
            cur_eq = float(eq[-1]["equity"] or 0.0)
            ath_eq = float(max(p["equity"] or 0.0 for p in eq))
            dd_pct = ((cur_eq - ath_eq) / ath_eq * 100.0) if ath_eq > 0 else 0.0
        else:
            cur_eq = capital; ath_eq = capital; dd_pct = 0.0
        dd_band = "ok" if dd_pct > -3.0 else "warn" if dd_pct > -5.0 else "bad"
        recovery_mode = dd_pct <= -5.0
        sizing_factor = 0.5 if recovery_mode else 1.0

        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "DB-Fehler")), 500

    return jsonify(
        ok=True,
        capital_eur=round(capital, 2),
        today=today,
        heat={
            "risk_eur": round(heat_sum, 2),
            "pct": round(heat_pct, 2),
            "band": heat_band,
            "n_positions": len(heat_positions),
            "positions": heat_positions,
            "thresholds": {"warn": 3.0, "bad": 5.0},
        },
        daily_loss={
            "closed_pnl_today_eur": round(pnl_today, 2),
            "r_today": round(r_today, 2),
            "band": dl_band,
            "lock": dl_lock,
            "n_closed": len(today_rows),
            "thresholds": {"warn": -1.0, "bad": -2.0},
        },
        drawdown={
            "current_equity": round(cur_eq, 2),
            "ath_equity": round(ath_eq, 2),
            "dd_pct": round(dd_pct, 2),
            "band": dd_band,
            "recovery_mode": recovery_mode,
            "sizing_factor": sizing_factor,
            "thresholds": {"warn": -3.0, "bad": -5.0},
        },
    )


# ============================================================================
# V7.6 Phase 2 · R-Multiple-Tracker (2026-04-20)
#   /portfolio/r_state: live R-Multiple pro offener Position.
#   Cache: Live-Preise 2 min TTL (yfinance fast_info).
# ============================================================================
_LIVE_PRICE_CACHE: dict = {}   # ticker -> (ts_epoch, price_float)
_LIVE_PRICE_TTL = 120          # 2 Minuten reichen; R-Bar wird alle 60s gepollt


def _live_price(ticker: str) -> float | None:
    """Holt Live-Close mit 2-min-Cache. Fallback None wenn yfinance fehlt."""
    if not ticker:
        return None
    now = time.time()
    rec = _LIVE_PRICE_CACHE.get(ticker)
    if rec and (now - rec[0]) < _LIVE_PRICE_TTL:
        return rec[1]
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return None
    try:
        t = yf.Ticker(ticker)
        # fast_info ist der günstigste Call; bei Fehlern Fallback auf history
        price = None
        try:
            fi = getattr(t, "fast_info", None)
            if fi is not None:
                price = float(fi.get("last_price") or fi.get("lastPrice") or 0.0)
        except Exception:
            price = None
        if not price:
            h = t.history(period="2d", auto_adjust=False)
            if not h.empty:
                price = float(h["Close"].iloc[-1])
        if price and price > 0:
            _LIVE_PRICE_CACHE[ticker] = (now, price)
            return price
    except Exception:
        pass
    return None


@app.get("/portfolio/r_state")
def portfolio_r_state():
    """R-Multiple-Tracker fuer offene Positionen.
    Pro Position: entry, stop, tp1, tp2, live_price, r_current.
    R = (live - entry) / (entry - stop) ; stop/entry pflicht, sonst r=None.
    """
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        rows = con.execute(
            """SELECT id, ticker, qty, entry_price, stop, target, tp1, tp2,
                      setup_fk, regime_at_entry, entry_date
                 FROM trades
                WHERE side='BUY' AND (exit_date IS NULL OR exit_date='')
             ORDER BY id DESC"""
        ).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "DB-Fehler")), 500

    out = []
    for r in rows:
        tkr = r["ticker"]
        entry = r["entry_price"]
        stop = r["stop"]
        tp1 = r["tp1"]
        tp2 = r["tp2"] or r["target"]
        live = _live_price(tkr) if tkr else None

        # R-Berechnung: (live - entry) / (entry - stop)
        r_cur = None
        r_tp1 = None
        r_tp2 = None
        risk_per_unit = None
        if entry and stop and entry != stop:
            risk_per_unit = abs(entry - stop)
            if live is not None:
                r_cur = (live - entry) / risk_per_unit
            if tp1:
                r_tp1 = (tp1 - entry) / risk_per_unit
            if tp2:
                r_tp2 = (tp2 - entry) / risk_per_unit

        out.append({
            "id": r["id"],
            "ticker": tkr,
            "qty": r["qty"],
            "entry_price": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "live_price": round(live, 4) if live else None,
            "r_current": round(r_cur, 2) if r_cur is not None else None,
            "r_tp1": round(r_tp1, 2) if r_tp1 is not None else None,
            "r_tp2": round(r_tp2, 2) if r_tp2 is not None else None,
            "setup": r["setup_fk"],
            "regime_at_entry": r["regime_at_entry"],
            "entry_date": r["entry_date"],
            "price_stale": live is None,
        })

    return jsonify(
        ok=True,
        n=len(out),
        positions=out,
        cache_ttl_s=_LIVE_PRICE_TTL,
        note=("yfinance nicht verfuegbar" if out and all(p["price_stale"] for p in out) else None),
    )



@app.post("/state/set")
def state_set():
    data = request.get_json(force=True, silent=True) or {}
    s = _read_json(STATE_PATH, {"schema_version": 1, "kill_switch": "off", "dd_level": 0})
    changed = []
    if "kill_switch" in data:
        v = str(data["kill_switch"]).lower()
        if v not in ("on", "off"):
            return jsonify(ok=False, msg="kill_switch muss 'on' oder 'off' sein"), 400
        s["kill_switch"] = v
        changed.append(f"kill_switch={v}")
    if "dd_level" in data:
        try:
            lvl = int(data["dd_level"])
        except (TypeError, ValueError):
            return jsonify(ok=False, msg="dd_level muss int sein (0/3/5/8)"), 400
        if lvl not in (0, 3, 5, 8):
            return jsonify(ok=False, msg="dd_level muss 0, 3, 5 oder 8 sein"), 400
        s["dd_level"] = lvl
        changed.append(f"dd_level={lvl}")
    if not changed:
        return jsonify(ok=False, msg="Kein erlaubtes Feld im Body"), 400
    s["updated_at_utc"] = _now_utc()
    _write_json(STATE_PATH, s)
    try:
        EventBus.publish("state_change", {"state": s, "changed": changed})
    except Exception:
        pass
    return jsonify(ok=True, msg="State aktualisiert: " + ", ".join(changed), state=s)


@app.post("/state/sync")
def state_sync():
    """Regeneriert trades_snapshot.json aus trade_journal.sqlite."""
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.execute("PRAGMA journal_mode=WAL")  # SEC-6: concurrent reader-safe
        migrated = _ensure_schema(con)
        snap = _regenerate_snapshot(con)
        con.close()
        msg = f"Snapshot regeneriert · {len(snap['equity_series'])} Equity-Punkte · {len(snap['open_positions'])} offene Positionen"
        if migrated:
            msg += f" · Schema migriert: {', '.join(migrated)}"
        return jsonify(ok=True, msg=msg, snap=snap)
    except Exception as e:
        return jsonify(ok=False, msg=f"Sync-Fehler: {type(e).__name__}"), 500


# --- V7.1 Krass-Edition Endpunkte -----------------------------------------

def _read_equity_series(con: sqlite3.Connection, days: int = 90) -> list[dict]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT date, equity FROM daily_equity ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    return [{"date": r["date"], "equity": r["equity"]} for r in reversed(rows)]


def _fetch_yf_close_series(ticker: str, days: int) -> list[dict]:
    """Nutzt yfinance falls vorhanden, sonst leere Liste. Nicht fatal wenn offline."""
    try:
        import yfinance as yf  # type: ignore
        import pandas as pd    # noqa: F401
    except ImportError:
        return []
    try:
        period = f"{max(days + 5, 60)}d"
        t = yf.Ticker(ticker)
        hist = t.history(period=period, auto_adjust=False)
        if hist.empty:
            return []
        hist = hist.tail(days)
        return [
            {"date": idx.strftime("%Y-%m-%d"), "close": float(row["Close"])}
            for idx, row in hist.iterrows()
        ]
    except Exception:
        return []


@app.get("/benchmark")
def benchmark():
    """Gibt %-Serie seit Start der Equity-Series für das eigene Portfolio + SPY + QQQ zurück
    (JSON-Key "portfolio"). Fallback: wenn yfinance fehlt oder SPY nicht erreichbar,
    sendet trotzdem die Portfolio-Serie und markiert benchmarks leer."""
    try:
        con = sqlite3.connect(str(TRADE_DB))
        _ensure_schema(con)
        eq = _read_equity_series(con, days=90)
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    if not eq:
        return jsonify(ok=True, portfolio=[], spy=[], qqq=[],
                       note="Equity-Series leer. Sync via /state/sync oder Abend-Tracking.")

    start_equity = eq[0]["equity"] or 1.0
    portfolio_pct = [
        {"date": p["date"], "pct": (p["equity"] - start_equity) / start_equity * 100.0}
        for p in eq
    ]

    # Benchmarks ab gleichem Start-Datum
    def _to_pct(series: list[dict]) -> list[dict]:
        if not series:
            return []
        base = series[0]["close"] or 1.0
        return [{"date": x["date"], "pct": (x["close"] - base) / base * 100.0}
                for x in series]

    days = len(eq)
    spy = _to_pct(_fetch_yf_close_series("SPY", days))
    qqq = _to_pct(_fetch_yf_close_series("QQQ", days))

    return jsonify(
        ok=True,
        portfolio=portfolio_pct,
        spy=spy,
        qqq=qqq,
        note=None if (spy and qqq) else "yfinance-Benchmarks nicht verfügbar (offline oder Paket fehlt)",
    )


@app.get("/history")
def history():
    """Letzte N geschlossene Trades mit PnL-Berechnung. Default 50."""
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        rows = con.execute("""
            SELECT id, ticker, side, qty, entry_price, entry_date,
                   exit_price, exit_date, stop, target, score, rationale
              FROM trades
             WHERE exit_date IS NOT NULL AND exit_date != ''
          ORDER BY id DESC
             LIMIT ?
        """, (limit,)).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    trades = []
    for r in rows:
        entry = float(r["entry_price"]) if r["entry_price"] else 0.0
        exit_ = float(r["exit_price"])  if r["exit_price"]  else 0.0
        qty   = int(r["qty"])           if r["qty"]         else 0
        pnl_abs = (exit_ - entry) * qty if (entry and exit_) else 0.0
        pnl_pct = ((exit_ - entry) / entry * 100.0) if entry else 0.0
        # Haltedauer in Tagen (best effort)
        duration = None
        try:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%d"
            d_in  = _dt.strptime(str(r["entry_date"])[:10], fmt)
            d_out = _dt.strptime(str(r["exit_date"])[:10],  fmt)
            duration = (d_out - d_in).days
        except Exception:
            pass
        trades.append({
            "id": r["id"], "ticker": r["ticker"], "qty": qty,
            "entry_price": entry, "entry_date": r["entry_date"],
            "exit_price": exit_,  "exit_date": r["exit_date"],
            "stop": r["stop"], "target": r["target"],
            "score": r["score"], "rationale": r["rationale"],
            "pnl_abs": round(pnl_abs, 2),
            "pnl_pct": round(pnl_pct, 2),
            "duration_days": duration,
        })

    return jsonify(ok=True, trades=trades, count=len(trades))


@app.get("/correlation")
def correlation():
    """30-Tage-Korrelation der Close-Series aller offenen Positionen.
    Liefert symmetrische Matrix + Alarme für |ρ| > 0.7.
    Wenn yfinance fehlt oder zu wenig Positionen -> leere Matrix.

    V7.6 #100: optional ?include_top10=1 → zieht zusätzlich die heutigen
    Briefing/Empfehlung-Top-10 rein, so dass Cross-Korrelationen zwischen
    offenen Positionen und neuen Scanner-Kandidaten sichtbar werden.
    Pro Ticker wird 'role' gesetzt: "open" | "top10" | "both".
    """
    include_top10 = (request.args.get("include_top10") or "").strip() in ("1", "true", "yes")

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        rows = con.execute("""
            SELECT DISTINCT ticker FROM trades
             WHERE side = 'BUY' AND (exit_date IS NULL OR exit_date = '')
        """).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    open_set = set(r["ticker"] for r in rows if r["ticker"])
    top10_set: set[str] = set()
    top10_meta: dict = {"date": None, "file": None, "tickers": []}
    if include_top10:
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            block = _parse_briefing_top10_scored(today_str)
            top10_tickers = [r["ticker"] for r in (block.get("rows") or []) if r.get("ticker")]
            top10_set = set(top10_tickers)
            top10_meta = {"date": block.get("date"), "file": block.get("file"),
                          "tickers": top10_tickers}
        except Exception:
            top10_meta["note"] = "Top-10-Parser fehlgeschlagen"

    union_tickers = sorted(open_set | top10_set)
    if len(union_tickers) < 2:
        return jsonify(ok=True, tickers=union_tickers, matrix=[], alerts=[],
                       roles={}, top10=top10_meta,
                       note="Weniger als 2 Ticker, keine Korrelation nötig.")

    # Series pro Ticker laden
    series_map: dict[str, list[float]] = {}
    for t in union_tickers:
        s = _fetch_yf_close_series(t, days=30)
        if len(s) >= 10:
            series_map[t] = [x["close"] for x in s]

    if len(series_map) < 2:
        return jsonify(ok=True, tickers=union_tickers, matrix=[], alerts=[],
                       roles={}, top10=top10_meta,
                       note="yfinance nicht verfügbar oder zu wenig Daten pro Ticker.")

    # Auf gemeinsame Länge normieren (kürzeste Serie)
    min_len = min(len(v) for v in series_map.values())
    syms = [t for t in union_tickers if t in series_map]
    mat: list[list[float]] = []
    alerts: list[dict] = []
    # Pearson-Korrelation der %-Returns
    def pct_returns(arr: list[float]) -> list[float]:
        arr = arr[-min_len:]
        return [(arr[i] - arr[i-1]) / arr[i-1] if arr[i-1] else 0.0
                for i in range(1, len(arr))]
    returns = {t: pct_returns(series_map[t]) for t in syms}
    n = len(syms)
    for i, a in enumerate(syms):
        row: list[float] = []
        for j, b in enumerate(syms):
            if i == j:
                row.append(1.0); continue
            xa, xb = returns[a], returns[b]
            if len(xa) < 5 or len(xb) < 5:
                row.append(0.0); continue
            ma = sum(xa)/len(xa); mb = sum(xb)/len(xb)
            cov = sum((xa[k]-ma)*(xb[k]-mb) for k in range(len(xa)))
            va  = sum((x-ma)**2 for x in xa) ** 0.5
            vb  = sum((x-mb)**2 for x in xb) ** 0.5
            rho = cov / (va*vb) if (va and vb) else 0.0
            rho = max(-1.0, min(1.0, rho))
            row.append(round(rho, 3))
            if j > i and abs(rho) > 0.7:
                # Alerts nur für Paare, an denen mindestens ein Open-Trade beteiligt ist
                # (ein reines Top10↔Top10-Paar ist Scanner-Rauschen, keine Positionsentscheidung)
                if (a in open_set) or (b in open_set):
                    alerts.append({"a": a, "b": b, "rho": round(rho, 3),
                                   "msg": f"Klumpenrisiko: {a} & {b} korreliert ρ={rho:+.2f}"})
        mat.append(row)

    roles: dict[str, str] = {}
    for t in syms:
        in_open = t in open_set
        in_top = t in top10_set
        roles[t] = "both" if (in_open and in_top) else ("open" if in_open else "top10")

    return jsonify(ok=True, tickers=syms, matrix=mat, alerts=alerts, n=n,
                   roles=roles, top10=top10_meta)


# V7.6 #101 · Expectancy × Setup × Regime Breakdown --------------------------
@app.get("/stats/expectancy")
def stats_expectancy():
    """Berechnet pro (setup_fk × regime_at_entry)-Zelle:
        n: Anzahl geschlossener Trades
        wins/losses/flat
        win_rate: wins / (wins+losses) (flat nicht gezählt)
        avg_win: Mittel PnL-% der Gewinner
        avg_loss: Mittel |PnL-%| der Verlierer (positiver Betrag)
        expectancy_pct: p * avg_win - (1-p) * avg_loss (in %, Kelly-kompatibel)
        expectancy_eur: mittlerer PnL in € (fallback: pnl_abs)
        avg_r: Mittel r_multiple (falls populated, sonst NULL)

    Query-Optionen:
        ?lookback=120, nur letzte N Tage (Default: alle)
        ?source=systematic|discretionary|all: DISC-BOOK-3-Filter (Default: 'systematic,live' wie cmd_cohort)
        ?include_null=1: wenn gesetzt, bleibt "(none)" als Cell drin; sonst rausgefiltert

    Return:
        { ok, cells: [...], setups: [...], regimes: [...], totals: {...}, note }
    """
    from datetime import datetime, timedelta
    lookback_raw = (request.args.get("lookback") or "").strip()
    source_raw = (request.args.get("source") or "systematic,live").strip().lower()
    include_null = (request.args.get("include_null") or "").strip() in ("1","true","yes")

    try:
        lookback = int(lookback_raw) if lookback_raw.isdigit() else None
    except Exception:
        lookback = None

    # Source-Filter aus DISC-BOOK-3-Konvention
    allowed_sources: set[str] = set()
    if source_raw in ("all","*",""):
        allowed_sources = set()   # no filter
    else:
        allowed_sources = {s.strip() for s in source_raw.split(",") if s.strip()}

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        q = """
            SELECT id, ticker, entry_price, exit_price, exit_date, qty,
                   COALESCE(NULLIF(TRIM(setup_fk),''),'(none)')          AS setup_fk,
                   COALESCE(NULLIF(TRIM(regime_at_entry),''),'(none)')    AS regime_at_entry,
                   r_multiple,
                   COALESCE(source,'')                                    AS source
              FROM trades
             WHERE exit_price IS NOT NULL
               AND entry_price IS NOT NULL
               AND exit_date   IS NOT NULL
               AND exit_date   != ''
        """
        rows = con.execute(q).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    # Lookback-Cutoff
    cutoff = None
    if lookback is not None and lookback > 0:
        cutoff = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")

    # Aggregieren
    cells: dict[tuple[str,str], dict] = {}
    setups_seen: set[str] = set()
    regimes_seen: set[str] = set()
    total = {"n": 0, "wins": 0, "losses": 0, "flat": 0,
             "sum_pct": 0.0, "sum_eur": 0.0, "rs": []}

    for r in rows:
        if allowed_sources and (r["source"] or "").lower() not in allowed_sources:
            continue
        if cutoff and (r["exit_date"] or "") < cutoff:
            continue
        setup = r["setup_fk"] or "(none)"
        regime = r["regime_at_entry"] or "(none)"
        if (not include_null) and (setup == "(none)" or regime == "(none)"):
            # Trotzdem in Total zählen, nur nicht als eigene Pivot-Zelle
            pass
        ep = float(r["entry_price"] or 0)
        xp = float(r["exit_price"] or 0)
        q  = float(r["qty"] or 0)
        if ep <= 0:
            continue
        pnl_pct = (xp - ep) / ep * 100.0
        pnl_eur = (xp - ep) * q
        rm = r["r_multiple"]

        # Total
        total["n"] += 1
        total["sum_pct"] += pnl_pct
        total["sum_eur"] += pnl_eur
        if rm is not None:
            try: total["rs"].append(float(rm))
            except Exception: pass
        if pnl_pct > 0:   total["wins"]   += 1
        elif pnl_pct < 0: total["losses"] += 1
        else:             total["flat"]   += 1

        # Cell
        if (not include_null) and (setup == "(none)" or regime == "(none)"):
            continue
        setups_seen.add(setup); regimes_seen.add(regime)
        key = (setup, regime)
        c = cells.setdefault(key, {
            "setup_fk": setup, "regime_at_entry": regime,
            "n": 0, "wins": 0, "losses": 0, "flat": 0,
            "sum_win_pct": 0.0, "sum_loss_pct": 0.0,
            "sum_eur": 0.0, "rs": [],
        })
        c["n"] += 1
        c["sum_eur"] += pnl_eur
        if pnl_pct > 0:
            c["wins"] += 1; c["sum_win_pct"]  += pnl_pct
        elif pnl_pct < 0:
            c["losses"] += 1; c["sum_loss_pct"] += abs(pnl_pct)
        else:
            c["flat"] += 1
        if rm is not None:
            try: c["rs"].append(float(rm))
            except Exception: pass

    # Cell-Metrics finalisieren
    def _finalize(c: dict) -> dict:
        wins, losses = c["wins"], c["losses"]
        decisive = wins + losses
        p  = (wins / decisive) if decisive else 0.0
        aw = (c["sum_win_pct"]  / wins)   if wins else 0.0
        al = (c["sum_loss_pct"] / losses) if losses else 0.0
        E  = (p * aw) - ((1.0 - p) * al)
        avg_r = (sum(c["rs"]) / len(c["rs"])) if c["rs"] else None
        return {
            "setup_fk": c["setup_fk"], "regime_at_entry": c["regime_at_entry"],
            "n": c["n"], "wins": wins, "losses": losses, "flat": c["flat"],
            "win_rate": round(p, 4),
            "avg_win_pct":  round(aw, 3),
            "avg_loss_pct": round(al, 3),
            "expectancy_pct": round(E, 3),
            "expectancy_eur": round(c["sum_eur"] / c["n"], 2) if c["n"] else 0.0,
            "avg_r": round(avg_r, 3) if avg_r is not None else None,
        }

    cells_out = [_finalize(c) for c in cells.values()]
    # Sort: absteigend nach Expectancy, dann nach n
    cells_out.sort(key=lambda r: (-(r["expectancy_pct"] or 0.0), -(r["n"] or 0)))

    # Totals finalisieren
    tw, tl = total["wins"], total["losses"]
    tdec = tw + tl
    totals_out = {
        "n": total["n"], "wins": tw, "losses": tl, "flat": total["flat"],
        "win_rate":      round(tw / tdec, 4) if tdec else 0.0,
        "expectancy_pct": round(total["sum_pct"] / total["n"], 3) if total["n"] else 0.0,
        "expectancy_eur": round(total["sum_eur"] / total["n"], 2) if total["n"] else 0.0,
        "avg_r": round(sum(total["rs"]) / len(total["rs"]), 3) if total["rs"] else None,
    }

    note = None
    if total["n"] == 0:
        note = "Keine geschlossenen Trades im gewählten Fenster."
    elif not cells_out:
        note = ("Keine Trades mit gesetztem Setup/Regime: Tagging nachholen "
                "(Post-Mortem-Modal beim Close setzt setup_fk + regime_at_entry).")

    return jsonify(ok=True,
                   cells=cells_out,
                   setups=sorted(setups_seen),
                   regimes=sorted(regimes_seen),
                   totals=totals_out,
                   filters={"lookback": lookback, "source": source_raw,
                            "include_null": include_null},
                   note=note)


# V7.6 #102 · Live-Mini-Sparklines (5m-Intraday) ----------------------------
_LIVE_SPARKS_CACHE: dict = {}          # ticker -> (ts_epoch, {series, last, change_pct})
_LIVE_SPARKS_TTL = 180                 # 3 Minuten: 5m-Bars kommen ohnehin nur alle 5min
_LIVE_SPARKS_MAX = 12                  # Cap auf 12 Ticker, Schutz gegen yfinance-Ratelimit


def _fetch_intraday_5m(ticker: str) -> list[float]:
    """Holt letzte 5m-Closes via yfinance. Return [] bei Offline/Fehler."""
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return []
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1d", interval="5m", auto_adjust=False)
        if hist.empty:
            return []
        return [float(c) for c in hist["Close"].tolist() if c is not None]
    except Exception:
        return []


@app.get("/sparks/live")
def sparks_live():
    """Liefert 5-Minuten-Intraday-Sparklines für Top-10-Ticker (oder explizite Liste).

    Query:
        ?top10=1: zieht Ticker aus Briefing/Empfehlung-Top-10 (Default-Modus)
        ?tickers=A,B,C: explizite Kommaliste (Cap 12)

    Cache: 180s pro Ticker (5m-Bars updaten ohnehin nur alle 5min).
    """
    import time as _t
    use_top10 = (request.args.get("top10") or "").strip() in ("1","true","yes")
    tickers_raw = (request.args.get("tickers") or "").strip()

    top10_meta: dict = {"date": None, "file": None, "tickers": []}
    tickers: list[str] = []

    if tickers_raw:
        tickers = [s.strip().upper() for s in tickers_raw.split(",") if s.strip()]
    if use_top10 or not tickers:
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            block = _parse_briefing_top10_scored(today_str)
            top10_meta = {
                "date": block.get("date"),
                "file": block.get("file"),
                "tickers": [r["ticker"] for r in (block.get("rows") or []) if r.get("ticker")],
            }
            if not tickers:
                tickers = list(top10_meta["tickers"])
        except Exception:
            pass

    tickers = tickers[:_LIVE_SPARKS_MAX]
    if not tickers:
        return jsonify(ok=True, series={}, last={}, change_pct={}, tickers=[],
                       top10=top10_meta,
                       note="Keine Ticker: weder Top-10 noch ?tickers= gesetzt.")

    now = _t.time()
    series_out: dict[str, list[float]] = {}
    last_out: dict[str, float] = {}
    chg_out: dict[str, float] = {}
    for t in tickers:
        rec = _LIVE_SPARKS_CACHE.get(t)
        payload = None
        if rec and (now - rec[0]) < _LIVE_SPARKS_TTL:
            payload = rec[1]
        else:
            closes = _fetch_intraday_5m(t)
            if closes:
                first = closes[0] or 0.0
                last = closes[-1] or 0.0
                chg = ((last - first) / first * 100.0) if first else 0.0
                payload = {"series": closes, "last": round(last, 4),
                           "change_pct": round(chg, 3)}
                _LIVE_SPARKS_CACHE[t] = (now, payload)
        if payload:
            series_out[t] = payload["series"]
            last_out[t]   = payload["last"]
            chg_out[t]    = payload["change_pct"]

    return jsonify(ok=True,
                   tickers=tickers,
                   series=series_out,
                   last=last_out,
                   change_pct=chg_out,
                   top10=top10_meta,
                   cached_ttl_s=_LIVE_SPARKS_TTL,
                   note=None if series_out else "yfinance nicht verfügbar oder leere Historie.")


@app.get("/sparks")
def sparks():
    """Liefert 30-Tage-Mini-Serien für KPIs. Quelle: trade_journal.sqlite daily_equity
    + hochgerechnete WR/PF-Fenster. Fallback auf synthetische Werte wenn zu wenig Daten."""
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        eq = _read_equity_series(con, days=60)  # mehr für rolling
        # Alle geschlossenen Trades für rollende WR/PF-Berechnung
        trades = con.execute("""
            SELECT exit_date, entry_price, exit_price, qty
              FROM trades
             WHERE exit_date IS NOT NULL AND exit_date != ''
          ORDER BY exit_date ASC
        """).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    # Equity-Spark (letzte 30)
    equity_spark = [p["equity"] for p in eq][-30:]

    # DD-Spark (rollender DD vs. Peak bis zu diesem Tag, 30d)
    dd_spark: list[float] = []
    peak = 0.0
    for p in eq:
        peak = max(peak, p["equity"])
        dd = (p["equity"] - peak) / peak * 100.0 if peak else 0.0
        dd_spark.append(round(dd, 2))
    dd_spark = dd_spark[-30:]

    # Rollierende WR & PF über die letzten N Trades pro Tag (Fenster 30 Trades)
    from collections import defaultdict
    daily_closed: dict[str, list[tuple[float,float,int]]] = defaultdict(list)
    for t in trades:
        d = str(t["exit_date"])[:10]
        daily_closed[d].append((float(t["entry_price"] or 0),
                                float(t["exit_price"] or 0),
                                int(t["qty"] or 0)))

    wr_spark: list[float] = []
    pf_spark: list[float] = []
    window: list[tuple[float,float,int]] = []
    WIN = 30  # rolling window = 30 trades
    # Reihenfolge: iteriere über Exit-Daten chronologisch
    for d in sorted(daily_closed.keys()):
        for t in daily_closed[d]:
            window.append(t)
            if len(window) > WIN:
                window = window[-WIN:]
            wins = sum(1 for (en,ex,_q) in window if ex > en)
            losses = sum(1 for (en,ex,_q) in window if ex < en)
            gross_win  = sum((ex-en)*q for (en,ex,q) in window if ex > en)
            gross_loss = abs(sum((ex-en)*q for (en,ex,q) in window if ex < en))
            wr = wins / len(window) * 100.0 if window else 0.0
            pf = (gross_win / gross_loss) if gross_loss else (gross_win if gross_win else 0.0)
            wr_spark.append(round(wr, 1))
            pf_spark.append(round(pf, 2))

    wr_spark = wr_spark[-30:] if wr_spark else []
    pf_spark = pf_spark[-30:] if pf_spark else []

    return jsonify(ok=True,
                   equity=equity_spark,
                   dd=dd_spark,
                   wr=wr_spark,
                   pf=pf_spark)


@app.get("/regime_history")
def regime_history():
    """Liefert die letzten N Regime-Snapshots aus Meta/regime-history/regime_*.json.
    Wenn keine Files, leere Liste + Note."""
    try:
        n = int(request.args.get("days", "60"))
    except ValueError:
        n = 60
    n = max(5, min(n, 365))

    regime_dir = ROOT / "Meta" / "regime-history"
    if not regime_dir.exists():
        return jsonify(ok=True, regimes=[],
                       note=f"{regime_dir.relative_to(ROOT)} nicht vorhanden")

    files = sorted(regime_dir.glob("regime_*.json"))[-n:]
    out = []
    for f in files:
        try:
            d = json.loads(f.read_text())
            out.append({
                "date": d.get("date") or f.stem.replace("regime_", ""),
                "regime": d.get("regime") or d.get("name") or "?",
                "size_multiplier": d.get("size_multiplier"),
            })
        except Exception:
            continue
    return jsonify(ok=True, regimes=out, count=len(out))


@app.get("/sim_reports")
def sim_reports():
    """Aggregiert die neuesten Sim-Reports (Bootstrap, Monte-Carlo, Factor, Walk-Forward).
    Sucht in sim/_reports/*.json nach dem jüngsten File je Typ."""
    rep_dir = ROOT / "sim" / "_reports"
    if not rep_dir.exists():
        return jsonify(ok=True, reports={}, note="sim/_reports/ nicht vorhanden")

    def _latest(glob_pattern: str) -> dict | None:
        files = sorted(rep_dir.glob(glob_pattern))
        if not files:
            return None
        f = files[-1]
        try:
            return {"file": f.name, "data": json.loads(f.read_text())}
        except Exception:
            return {"file": f.name, "data": None, "error": "parse-error"}

    reports = {
        "bootstrap":     _latest("bootstrap_ci_*.json"),
        "monte_carlo":   _latest("monte_carlo_*.json"),
        "factor":        _latest("factor_*.json"),
        "walkforward":   _latest("walkforward_*.json"),
        "stress":        _latest("stress_*.json"),
        "hmm":           _latest("hmm_*.json"),
    }
    # Null-Einträge rausfiltern
    reports = {k:v for k,v in reports.items() if v is not None}
    return jsonify(ok=True, reports=reports, count=len(reports))


# ---- /baseline_ci -------------------------------------------------------
@app.get("/baseline_ci")
def baseline_ci():
    """DASH-2: Liefert das neueste bootstrap_ci_live_*.json als strukturiertes JSON
    fuer die Baseline-CI-Karte im V7-Dashboard. Praeferiert Live-Variante vor
    parquet-Legacy. Bei keinem Report: ok=True, snap=None mit note."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from baseline_ci_line import load_latest_live_ci, _extract_ci, format_short, format_markdown  # type: ignore
    except ImportError as e:
        return jsonify(ok=False, msg=f"Import-Fehler baseline_ci_line: {e}"), 500

    fp, doc = load_latest_live_ci()
    if not doc:
        return jsonify(ok=True, snap=None, file=None,
                       note="Kein bootstrap_ci_live_*.json vorhanden. "
                            "Run: `python3 sim/bootstrap_live.py`")
    snap = _extract_ci(doc)
    return jsonify(
        ok=True,
        file=fp.name if fp else None,
        snap=snap,
        short=format_short(snap),
        markdown=format_markdown(snap),
    )


# ============================================================================
# V7.3 · Co-Pilot-Edition Endpunkte (2026-04-17)
#   /fit_check, /hebel_options, /position_actions, /briefing/today,
#   /vertrauensanker_status, /post_mortem (POST+GET), /scheduled_tasks (+ force/toggle),
#   /news/position, /replay/snapshots, /replay
# ============================================================================

POST_MORTEM_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS post_mortems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    ticker TEXT,
    created_at_utc TEXT,
    thesis TEXT,
    outcome TEXT,
    edge_or_mistake TEXT,
    tags TEXT,
    rating INTEGER
)
"""


def _ensure_post_mortems(con: sqlite3.Connection) -> None:
    con.execute(POST_MORTEM_TABLE_SQL)
    # V7.4 S2: additive Migration, thesis_verdict + lesson
    cur = con.execute("PRAGMA table_info(post_mortems)")
    cols = {row[1] for row in cur.fetchall()}
    if "thesis_verdict" not in cols:
        try: con.execute("ALTER TABLE post_mortems ADD COLUMN thesis_verdict TEXT")
        except Exception: pass
    if "lesson" not in cols:
        try: con.execute("ALTER TABLE post_mortems ADD COLUMN lesson TEXT")
        except Exception: pass
    con.commit()


# V7.4 S2: erlaubte Werte für thesis_verdict
VALID_VERDICTS = {"CONFIRMED", "INVALIDATED", "NEUTRAL"}


def _load_scan(date: str) -> dict | None:
    p = ROOT / "tools" / f"scan_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_regime(date: str) -> dict | None:
    p = ROOT / "tools" / f"regime_{date}.json"
    if not p.exists():
        p = ROOT / "Meta" / "regime-history" / f"regime_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_sector_rs(date: str) -> dict | None:
    p = ROOT / "tools" / f"sector_rs_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_insider(date: str) -> dict | None:
    p = ROOT / "tools" / f"insider_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_options_flow(date: str) -> dict | None:
    p = ROOT / "tools" / f"options_flow_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---- /fit_check -----------------------------------------------------------
# Regel-Set Setup × Regime (Co-Pilot-Passform)
SETUP_REGIME_FIT = {
    # setup → { regime → fit 0..10 }
    "Connors-SMA5": {"Trending": 9, "MeanRev": 4, "Volatile": 5, "Choppy": 5, "Crash": 2},
    "Breakout":     {"Trending": 8, "MeanRev": 3, "Volatile": 4, "Choppy": 3, "Crash": 2},
    "MeanRev":      {"Trending": 3, "MeanRev": 9, "Volatile": 6, "Choppy": 7, "Crash": 5},
    "Insider":      {"Trending": 7, "MeanRev": 7, "Volatile": 6, "Choppy": 6, "Crash": 5},
    "Congress":     {"Trending": 7, "MeanRev": 7, "Volatile": 6, "Choppy": 6, "Crash": 5},
    "External":     {"Trending": 7, "MeanRev": 7, "Volatile": 6, "Choppy": 6, "Crash": 5},
    "Custom":       {"Trending": 5, "MeanRev": 5, "Volatile": 5, "Choppy": 5, "Crash": 3},
}


@app.get("/fit_check")
def fit_check():
    """Pre-Trade Fit-Check: Regime/Setup-Passung, Red-Flags, Auto-Sizing.
    Query: ticker, setup, route, entry, stop, risk_pct (default 1.0), date
    Return: {fit_score, fit_label, setup_regime_fit, red_flags[], suggested_qty,
             risk_eur, risk_pct_of_equity, last_equity, regime}"""
    tkr    = (request.args.get("ticker") or "").upper().strip()
    setup  = (request.args.get("setup")  or "").strip()
    route  = (request.args.get("route")  or "DIRECT").upper().strip()
    date   = (request.args.get("date")   or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        entry = float(request.args.get("entry") or 0) or None
    except ValueError:
        entry = None
    try:
        stop = float(request.args.get("stop") or 0) or None
    except ValueError:
        stop = None
    try:
        risk_pct = float(request.args.get("risk_pct") or 1.0)
    except ValueError:
        risk_pct = 1.0
    risk_pct = max(0.1, min(risk_pct, 5.0))

    out = {
        "ok": True, "ticker": tkr or None, "setup": setup or None,
        "route": route, "date": date, "entry": entry, "stop": stop, "risk_pct": risk_pct,
        "fit_score": None, "fit_label": "-",
        "setup_regime_fit": None,
        "red_flags": [], "green_flags": [],
        "last_equity": None, "risk_budget_eur": None,
        "risk_eur": None, "risk_pct_of_equity": None,
        "suggested_qty": None,
        "regime": None, "size_multiplier": None, "allowed_tiers": None,
    }

    # Regime
    regime_obj = _load_regime(date)
    regime = regime_obj.get("regime") if regime_obj else None
    out["regime"] = regime
    if regime_obj:
        out["size_multiplier"] = regime_obj.get("size_multiplier")
        out["allowed_tiers"]   = regime_obj.get("allowed_tiers")

    # Setup-Regime Fit
    if setup and regime and setup in SETUP_REGIME_FIT:
        srf = SETUP_REGIME_FIT[setup].get(regime)
        out["setup_regime_fit"] = srf
        if srf is not None:
            if srf >= 8:   out["green_flags"].append(f"Setup «{setup}» passt stark zu Regime «{regime}» ({srf}/10)")
            elif srf <= 4: out["red_flags"].append(f"Setup «{setup}» schwach im Regime «{regime}» ({srf}/10): bitte nochmal prüfen")

    # Scanner-Score
    pre_score = None
    atr = None
    close = None
    insider_hit = False
    congress_hit = False
    options_hit = None
    sector_hit = None
    earnings_blackout = False

    scan = _load_scan(date)
    if scan and tkr:
        rows = scan.get("rows") or scan.get("top_setups") or scan.get("results") or []
        for r in rows:
            if (r.get("ticker") or "").upper() == tkr:
                pre_score = r.get("pre_score") or r.get("score")
                atr = r.get("atr") or r.get("atr_14") or r.get("atr_20")
                close = r.get("close") or r.get("entry_hint") or r.get("price")
                if r.get("earnings_blackout") or r.get("earnings_days", 99) <= 3:
                    earnings_blackout = True
                break

    if earnings_blackout:
        out["red_flags"].append("Earnings-Blackout in den nächsten 3 Tagen: Breakout-Setups defensiv")
    if pre_score is not None:
        if pre_score >= 8.0: out["green_flags"].append(f"Scanner-Score {pre_score:.1f} (stark)")
        elif pre_score < 6.5: out["red_flags"].append(f"Scanner-Score nur {pre_score:.1f}: unter 6.5 diskretionär")

    # Insider / Congress
    ins = _load_insider(date)
    if ins and tkr:
        tmap = ins.get("ticker_map") or {}
        tinfo = tmap.get(tkr) or {}
        if tinfo.get("insider_score", 0) and float(tinfo["insider_score"]) >= 0.5:
            insider_hit = True
            out["green_flags"].append(f"Insider-Score {tinfo['insider_score']:+.2f} (Clustering)")
        if tinfo.get("congress_signals"):
            congress_hit = True
            out["green_flags"].append(f"Congress-Signal aktiv ({len(tinfo['congress_signals'])})")

    # Options-Flow
    opt = _load_options_flow(date)
    if opt and tkr:
        tmap = opt.get("ticker_map") or {}
        tinfo = tmap.get(tkr) or {}
        pcr = opt.get("market_pcr")
        if tinfo.get("call_oi_spike"):
            options_hit = "call_spike"
            out["green_flags"].append("Call-OI-Spike (bullish Flow)")
        if tinfo.get("put_oi_spike"):
            options_hit = "put_spike"
            out["red_flags"].append("Put-OI-Spike (bearish Flow)")
        if pcr is not None:
            try:
                pcrf = float(pcr)
                if pcrf > 1.2:  out["red_flags"].append(f"Markt-PCR {pcrf:.2f} defensiv (Puts dominieren)")
                elif pcrf < 0.7: out["green_flags"].append(f"Markt-PCR {pcrf:.2f} offensiv")
            except Exception:
                pass

    # Sector-RS
    sr = _load_sector_rs(date)
    if sr and tkr:
        tmap = sr.get("ticker_map") or {}
        tinfo = tmap.get(tkr) or {}
        score = tinfo.get("sector_rs_score")
        if score is not None:
            try:
                s = float(score)
                sector_hit = s
                if s >= 1.0:   out["green_flags"].append(f"Sektor-RS stark ({s:+.1f})")
                elif s <= -1.0: out["red_flags"].append(f"Sektor-RS schwach ({s:+.1f})")
            except Exception:
                pass

    # Korrelationscheck: klumpt es mit offenen Positionen?
    try:
        con = sqlite3.connect(str(TRADE_DB)); _ensure_schema(con)
        open_tkrs = [r[0] for r in con.execute(
            "SELECT DISTINCT ticker FROM trades WHERE side='BUY' AND (exit_date IS NULL OR exit_date='')"
        ).fetchall()]
        # Equity + Sizing
        row = con.execute(
            "SELECT equity FROM daily_equity ORDER BY date DESC LIMIT 1"
        ).fetchone()
        last_equity = float(row[0]) if row and row[0] else None
        out["last_equity"] = last_equity
        con.close()
    except Exception:
        open_tkrs = []
        last_equity = None

    if tkr and tkr in open_tkrs:
        out["red_flags"].append(f"{tkr} ist bereits offen: Aufstockung bewusst?")

    # Sizing-Vorschlag
    if last_equity:
        risk_budget_eur = round(last_equity * risk_pct / 100.0, 2)
        out["risk_budget_eur"] = risk_budget_eur
        if entry and stop and stop < entry:
            per_share_risk = entry - stop
            qty = int(risk_budget_eur / per_share_risk) if per_share_risk > 0 else 0
            if qty > 0:
                out["suggested_qty"] = qty
                risk_eur = per_share_risk * qty
                out["risk_eur"] = round(risk_eur, 2)
                out["risk_pct_of_equity"] = round(risk_eur / last_equity * 100.0, 3)
                if risk_eur / last_equity * 100.0 > 2.0:
                    out["red_flags"].append(f"Risiko {risk_eur:.0f} € > 2 % Konto: MCR-Regel verletzt")
    if entry and stop and stop >= entry:
        out["red_flags"].append(f"Stop {stop} ≥ Entry {entry}: bei Long ungültig")

    # Fit-Score 0..10: Regime-Fit 40 % · Scanner-Score 25 % · Smart-Money 20 % · Sektor-RS 15 %
    parts: list[float] = []
    if out["setup_regime_fit"] is not None:
        parts.append((out["setup_regime_fit"] / 10.0) * 4.0)
    if pre_score is not None:
        parts.append(min(pre_score / 10.0, 1.0) * 2.5)
    sm = 0.0
    if insider_hit:  sm += 1.0
    if congress_hit: sm += 1.0
    if options_hit == "call_spike": sm += 0.5
    if options_hit == "put_spike":  sm -= 1.0
    parts.append(max(0.0, min(2.0, sm)))
    if sector_hit is not None:
        srn = max(-2.0, min(2.0, sector_hit)) / 2.0  # -1..+1
        parts.append((srn + 1.0) / 2.0 * 1.5)         # 0..1.5
    if parts:
        raw = sum(parts)
        # Red-Flags ziehen pro Stück 0.8 ab (cap auf 0)
        raw -= 0.8 * len(out["red_flags"])
        fit = max(0.0, min(10.0, round(raw, 1)))
        out["fit_score"] = fit
        if fit >= 8.0:   out["fit_label"] = "Stark"
        elif fit >= 6.5: out["fit_label"] = "OK"
        elif fit >= 5.0: out["fit_label"] = "Wackelig"
        else:            out["fit_label"] = "Schwach"
    return jsonify(out)


# ---- /hebel_options -------------------------------------------------------
@app.get("/hebel_options")
def hebel_options():
    """Vergleicht DIRECT / HEBEL_KO / HEBEL_OS / FAKTOR für gegebenen Trade.
    Query: entry, stop, tp1, tp2, risk_budget_eur
    Return: routes[] mit {route, implied_leverage, ko_level, buffer_pct,
                          capital_needed_eur, r_at_tp1, r_at_tp2, notes}"""
    try:
        entry = float(request.args.get("entry") or 0)
        stop  = float(request.args.get("stop")  or 0)
        budget = float(request.args.get("risk_budget_eur") or 0)
        tp1 = float(request.args.get("tp1") or 0) or None
        tp2 = float(request.args.get("tp2") or 0) or None
    except ValueError:
        return jsonify(ok=False, msg="entry/stop/tp1/tp2/risk_budget_eur müssen numerisch sein"), 400
    if entry <= 0 or stop <= 0 or stop >= entry:
        return jsonify(ok=False, msg="Ungültig: entry>stop>0 nötig"), 400
    if budget <= 0:
        budget = 100.0  # sensible default falls Equity unbekannt

    per_share_risk = entry - stop
    direct_qty = int(budget / per_share_risk) if per_share_risk > 0 else 0
    routes: list[dict] = []

    def _r(tp):
        if not tp: return None
        return round((tp - entry) / per_share_risk, 2)

    # DIRECT
    routes.append({
        "route": "DIRECT",
        "implied_leverage": 1.0,
        "ko_level": None,
        "buffer_pct": None,
        "qty": direct_qty,
        "capital_needed_eur": round(direct_qty * entry, 2),
        "r_at_tp1": _r(tp1),
        "r_at_tp2": _r(tp2),
        "notes": "Direkt-Kauf, kein Hebelprodukt, volle Aktienmenge.",
    })

    # HEBEL_KO: KO-Level = Stop ± 2 % Puffer
    ko_level = round(stop * 0.98, 4)  # 2 % unter Stop
    ko_distance_pct = (entry - ko_level) / entry * 100.0
    ko_leverage = round(100.0 / ko_distance_pct, 1) if ko_distance_pct > 0 else None
    routes.append({
        "route": "HEBEL_KO",
        "implied_leverage": ko_leverage,
        "ko_level": ko_level,
        "buffer_pct": round((stop - ko_level) / stop * 100.0, 2),
        "qty": None,
        "capital_needed_eur": round(budget, 2),
        "r_at_tp1": _r(tp1),
        "r_at_tp2": _r(tp2),
        "notes": f"Knock-Out 2 % unter Stop. Theoretischer Hebel ≈ {ko_leverage}x. Risiko = eingesetztes Kapital bei KO-Treffer.",
    })

    # HEBEL_OS: Optionsschein, Hebel typ. 3-8x, Risk durch Premium begrenzt
    os_leverage = 5.0
    routes.append({
        "route": "HEBEL_OS",
        "implied_leverage": os_leverage,
        "ko_level": None,
        "buffer_pct": None,
        "qty": None,
        "capital_needed_eur": round(budget, 2),
        "r_at_tp1": _r(tp1) * os_leverage if tp1 else None,
        "r_at_tp2": _r(tp2) * os_leverage if tp2 else None,
        "notes": "Optionsschein, Zeitwertverlust beachten. Eingesetztes Kapital = max. Risiko.",
    })

    # FAKTOR: konstanter Hebel, oft 2x/5x/10x
    fk_leverage = min(10.0, round(100.0 / ((entry - stop) / entry * 100.0), 1))
    routes.append({
        "route": "FAKTOR",
        "implied_leverage": fk_leverage,
        "ko_level": None,
        "buffer_pct": None,
        "qty": None,
        "capital_needed_eur": round(budget, 2),
        "r_at_tp1": _r(tp1) * fk_leverage if tp1 else None,
        "r_at_tp2": _r(tp2) * fk_leverage if tp2 else None,
        "notes": f"Faktor-Zertifikat {fk_leverage}x, tägliches Reset. Achtung Pfad-Risiko in volatilen Märkten.",
    })

    return jsonify(ok=True, entry=entry, stop=stop, per_share_risk=round(per_share_risk, 4),
                   risk_budget_eur=budget, routes=routes)


# ---- /position_actions ----------------------------------------------------
@app.get("/position_actions")
def position_actions():
    """Pro offener Position ein Action-Hint (Regeln: Alter vs. max_hold, PnL-Stufen,
    Score-Verschlechterung, Regime-Flip)."""
    today = datetime.now().strftime("%Y-%m-%d")
    regime_obj = _load_regime(today)
    regime_now = regime_obj.get("regime") if regime_obj else None

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        rows = con.execute("""
            SELECT id, ticker, entry_price, qty, stop, target, tp1, tp2, score,
                   setup_fk, regime_at_entry, entry_date, max_hold_days, route
              FROM trades
             WHERE side='BUY' AND (exit_date IS NULL OR exit_date='')
          ORDER BY id DESC
        """).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    scan_today = _load_scan(today) or {}
    scan_rows = scan_today.get("rows") or scan_today.get("top_setups") or scan_today.get("results") or []
    scan_by_ticker = {(r.get("ticker") or "").upper(): r for r in scan_rows}

    def _days_between(a: str, b: str) -> int:
        try:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%d"
            return (_dt.strptime(b[:10], fmt) - _dt.strptime(a[:10], fmt)).days
        except Exception:
            return 0

    def _fetch_last_close(tkr: str) -> float | None:
        s = _fetch_yf_close_series(tkr, days=5)
        return s[-1]["close"] if s else None

    actions = []
    for r in rows:
        tkr = r["ticker"]
        age = _days_between(str(r["entry_date"]), today) if r["entry_date"] else 0
        max_h = r["max_hold_days"] or 5
        last_close = _fetch_last_close(tkr)
        pnl_pct = None
        if last_close and r["entry_price"]:
            pnl_pct = (last_close - float(r["entry_price"])) / float(r["entry_price"]) * 100.0

        hints = []
        severity = "info"   # info | warn | err | ok

        # 1) Time-Stop
        if age >= max_h:
            hints.append(f"Max-Hold ({max_h}d) erreicht: Time-Stop-Entscheidung")
            severity = "err"
        elif age == max_h - 1:
            hints.append(f"Tag {age}/{max_h}: morgen Time-Stop-Decision")
            severity = "warn"

        # 2) PnL-Stufen (Trailing-Hints)
        if pnl_pct is not None:
            if pnl_pct >= 15:
                hints.append(f"+{pnl_pct:.1f} %: Trailing-Stop auf Break-Even oder TP1 erwägen")
                if severity == "info": severity = "ok"
            elif pnl_pct >= 8:
                hints.append(f"+{pnl_pct:.1f} %: Stop auf Break-Even hochziehen?")
                if severity == "info": severity = "ok"
            elif pnl_pct <= -5:
                hints.append(f"{pnl_pct:+.1f} %: Stop-Zone nah, Discipline halten")
                severity = "warn"

        # 3) Regime-Flip
        if regime_now and r["regime_at_entry"] and regime_now != r["regime_at_entry"]:
            hints.append(f"Regime-Flip {r['regime_at_entry']} → {regime_now}: Setup passt evtl. nicht mehr")
            if severity != "err": severity = "warn"

        # 4) Score-Drop (Scanner heute)
        today_score = None
        sc = scan_by_ticker.get(tkr)
        if sc:
            today_score = sc.get("pre_score") or sc.get("score")
        if r["score"] is not None and today_score is not None:
            try:
                if float(today_score) <= float(r["score"]) - 1.5:
                    hints.append(f"Score {float(r['score']):.1f} → {float(today_score):.1f} (-{float(r['score'])-float(today_score):.1f}), Edge degradiert")
                    if severity != "err": severity = "warn"
            except Exception:
                pass

        # 5) Stop-Hit (Kurs unter Stop)
        if last_close and r["stop"]:
            try:
                if last_close <= float(r["stop"]):
                    hints.append(f"Kurs {last_close:.2f} ≤ Stop {float(r['stop']):.2f}, Stop getriggert, schließen!")
                    severity = "err"
            except Exception:
                pass

        if not hints:
            hints.append("Hold, keine Action erforderlich")

        actions.append({
            "trade_id": r["id"], "ticker": tkr, "qty": r["qty"],
            "entry_price": r["entry_price"], "stop": r["stop"],
            "tp1": r["tp1"], "tp2": r["tp2"], "score_at_entry": r["score"],
            "score_today": today_score,
            "regime_at_entry": r["regime_at_entry"], "regime_now": regime_now,
            "age_days": age, "max_hold_days": max_h,
            "last_close": last_close, "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "hints": hints, "severity": severity,
            "setup_fk": r["setup_fk"], "route": r["route"],
        })

    return jsonify(ok=True, actions=actions, count=len(actions), regime_now=regime_now,
                   note=None if regime_obj else "Kein regime_<date>.json für heute")


# ---- /briefing/today ------------------------------------------------------
# Briefing-Dateimuster (relativ zum Repo-Root), konfigurierbar via
# DASHBOARD_BRIEFING_GLOBS (kommasepariert). Briefings sind private Outputs
# (gitignored) und werden nur lokal gelesen.
BRIEFING_GLOBS = [g.strip() for g in os.environ.get(
    "DASHBOARD_BRIEFING_GLOBS",
    "Briefings/*Briefing*.md,*Market-Briefing-*.md,Daily-Briefing-*.md").split(",") if g.strip()]


@app.get("/briefing/today")
def briefing_today():
    """Liest das neueste Briefing-Markdown (Muster: BRIEFING_GLOBS bzw.
    DASHBOARD_BRIEFING_GLOBS) und gibt strukturiert Sections zurück."""
    candidates = []
    for pattern in BRIEFING_GLOBS:
        candidates += sorted(ROOT.glob(pattern))

    if not candidates:
        return jsonify(ok=True, file=None, sections=[], note="Kein Briefing-File gefunden")

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return jsonify(ok=False, msg=f"Read-Fehler: {type(e).__name__}"), 500

    # Split nach Headings (##, ###)
    lines = text.splitlines()
    sections: list[dict] = []
    current = {"heading": "Intro", "level": 1, "body": []}
    for ln in lines:
        if ln.startswith("## ") or ln.startswith("# "):
            if current["body"]:
                sections.append(current)
            lvl = 1 if ln.startswith("# ") else 2
            current = {"heading": ln.lstrip("# ").strip(), "level": lvl, "body": []}
        elif ln.startswith("### "):
            if current["body"]:
                sections.append(current)
            current = {"heading": ln.lstrip("# ").strip(), "level": 3, "body": []}
        else:
            current["body"].append(ln)
    if current["body"]:
        sections.append(current)

    # Preview (erste 8 Zeilen nicht-leer) pro Sektion
    for s in sections:
        body = [l for l in s["body"] if l.strip()]
        s["preview"] = "\n".join(body[:8])
        s["body"] = "\n".join(s["body"])

    return jsonify(ok=True,
                   file=latest.name,
                   path=str(latest.relative_to(ROOT)),
                   mtime=datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                   sections=sections, count=len(sections))


# ---- /vertrauensanker_status ---------------------------------------------
def _trust_anchor_config() -> list[tuple[str, list[str]]]:
    """DASHBOARD_TRUST_ANCHORS = "Name1:kw1|kw2;Name2:kw3" (optional, Default leer).
    Prueft, ob das aktuelle Briefing die eigenen Referenz-Quellen erwaehnt."""
    raw = os.environ.get("DASHBOARD_TRUST_ANCHORS", "")
    out: list[tuple[str, list[str]]] = []
    for part in raw.split(";"):
        if ":" not in part:
            continue
        name, kws = part.split(":", 1)
        kw_list = [k.strip().lower() for k in kws.split("|") if k.strip()]
        if name.strip() and kw_list:
            out.append((name.strip(), kw_list))
    return out


@app.get("/vertrauensanker_status")
def vertrauensanker_status():
    """Abdeckungsstatus selbst konfigurierter Referenz-Quellen ("Vertrauensanker")
    im aktuellen Briefing-File (simple Keyword-Heuristik). Konfiguration via
    DASHBOARD_TRUST_ANCHORS, ohne Konfiguration leere Liste."""
    anchors_cfg = _trust_anchor_config()
    if not anchors_cfg:
        return jsonify(ok=True, anchors=[],
                       note="keine Anker konfiguriert (DASHBOARD_TRUST_ANCHORS)")
    candidates = []
    for pattern in BRIEFING_GLOBS:
        candidates += sorted(ROOT.glob(pattern))
    if not candidates:
        return jsonify(ok=True, anchors=[
            {"name": name, "status": "unknown", "note": "kein Briefing"}
            for name, _ in anchors_cfg])

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        text = latest.read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        text = ""

    def _check(anchor: str, keywords: list[str]) -> dict:
        hits = sum(1 for k in keywords if k in text)
        if hits >= 1:
            return {"name": anchor, "status": "ok", "note": f"{hits} Treffer im Briefing"}
        return {"name": anchor, "status": "missing", "note": "keine Erwähnung, Fetch prüfen"}

    return jsonify(ok=True,
                   file=latest.name,
                   anchors=[_check(name, kws) for name, kws in anchors_cfg])


# ---- /post_mortem ---------------------------------------------------------
@app.post("/post_mortem")
def post_mortem_post():
    """POST {trade_id, thesis, outcome, edge_or_mistake, tags, rating 1..5,
             thesis_verdict (CONFIRMED|INVALIDATED|NEUTRAL), lesson}

    V7.4 S2: thesis_verdict + lesson werden sowohl im post_mortems-Log
    als auch in trades.thesis_verdict / trades.lesson persistiert (Back-Fill)."""
    data = request.get_json(force=True, silent=True) or {}
    trade_id = data.get("trade_id")
    if trade_id is None:
        return jsonify(ok=False, msg="trade_id fehlt"), 400
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="trade_id muss int sein"), 400
    thesis = (data.get("thesis") or "").strip()
    outcome = (data.get("outcome") or "").strip()
    edge_or_mistake = (data.get("edge_or_mistake") or "").strip()
    tags = (data.get("tags") or "").strip()
    try:
        rating = int(data.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    rating = max(0, min(rating, 5))

    # V7.4 S2: thesis_verdict + lesson (optional)
    thesis_verdict = (data.get("thesis_verdict") or "").strip().upper() or None
    lesson         = (data.get("lesson") or "").strip() or None
    if thesis_verdict and thesis_verdict not in VALID_VERDICTS:
        return jsonify(ok=False,
                       msg=f"thesis_verdict muss einer von {sorted(VALID_VERDICTS)} sein"), 400

    # V7.6 P2: setup_fk Back-Fill + outcome_tag wird in tags gemerged
    setup_fk    = (data.get("setup_fk") or "").strip() or None
    outcome_tag = (data.get("outcome_tag") or "").strip() or None
    if outcome_tag:
        # Outcome-Tag in kommagetrennte tags-Liste einfuegen (idempotent)
        tag_set = [t.strip() for t in tags.split(",") if t.strip()]
        if outcome_tag not in tag_set:
            tag_set.insert(0, outcome_tag)
        tags = ", ".join(tag_set)

    try:
        con = sqlite3.connect(str(TRADE_DB))
        _ensure_schema(con)
        _ensure_post_mortems(con)
        row = con.execute("SELECT ticker FROM trades WHERE id=?", (trade_id,)).fetchone()
        ticker = row[0] if row else None
        con.execute("""
            INSERT INTO post_mortems
                (trade_id, ticker, created_at_utc, thesis, outcome, edge_or_mistake,
                 tags, rating, thesis_verdict, lesson)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_id, ticker, _now_utc(), thesis, outcome, edge_or_mistake,
              tags, rating, thesis_verdict, lesson))
        # V7.4 S2 + V7.6 P2: Back-Fill in trades fuer spaeteres Join-Reporting
        if thesis_verdict or lesson or setup_fk:
            con.execute("""
                UPDATE trades
                   SET thesis_verdict = COALESCE(?, thesis_verdict),
                       lesson         = COALESCE(?, lesson),
                       setup_fk       = COALESCE(?, setup_fk),
                       updated_at_utc = ?
                 WHERE id = ?
            """, (thesis_verdict, lesson, setup_fk, _now_utc(), trade_id))
        con.commit()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500
    return jsonify(ok=True, msg=f"Post-Mortem für Trade #{trade_id} gespeichert")


@app.get("/post_mortem")
def post_mortem_get():
    """GET ?trade_id=X → Post-Mortem(s) für einen Trade.
       GET (ohne param) → alle Post-Mortems, neueste zuerst."""
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        _ensure_post_mortems(con)
        tid = request.args.get("trade_id")
        if tid:
            rows = con.execute("SELECT * FROM post_mortems WHERE trade_id=? ORDER BY id DESC",
                               (int(tid),)).fetchall()
        else:
            rows = con.execute("SELECT * FROM post_mortems ORDER BY id DESC LIMIT 100").fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500
    return jsonify(ok=True, entries=[dict(r) for r in rows], count=len(rows))


@app.get("/post_mortem/weekly")
def post_mortem_weekly():
    """Aggregat der letzten 7 Tage: avg Rating, Top-Tags, Discretion-Edge grob."""
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.row_factory = sqlite3.Row
        _ensure_schema(con)
        _ensure_post_mortems(con)
        since = (datetime.now(timezone.utc).date().isoformat())  # today
        rows = con.execute("""
            SELECT rating, tags, edge_or_mistake
              FROM post_mortems
             WHERE created_at_utc >= date('now', '-7 day')
        """).fetchall()
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=f"DB-Fehler: {type(e).__name__}"), 500

    if not rows:
        return jsonify(ok=True, count=0, avg_rating=None, top_tags=[], note="Keine Post-Mortems in den letzten 7 Tagen")

    ratings = [int(r["rating"]) for r in rows if r["rating"]]
    avg = round(sum(ratings)/len(ratings), 2) if ratings else None

    from collections import Counter
    cnt = Counter()
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip().lower()
            if t: cnt[t] += 1
    top_tags = [{"tag": k, "n": v} for k, v in cnt.most_common(8)]

    return jsonify(ok=True, count=len(rows), avg_rating=avg, top_tags=top_tags)


# ---- /scheduled_tasks -----------------------------------------------------
# SKILL.md Heuristik: wir lesen <DASHBOARD_TASKS_DIR>/*/SKILL.md (Default:
# <repo>/scheduled-tasks). Parsen YAML-Frontmatter + Time-Hinweise.
# "Live-Status" = Existenz des heutigen Output-Files je Task.
TASKS_DIR = Path(os.environ.get("DASHBOARD_TASKS_DIR") or (ROOT / "scheduled-tasks"))

# Slug -> erwartete Output-Dateien von heute ({today} wird ersetzt)
TASK_OUTPUT_RULES = {
    "daily-universe-scanner": ["tools/scan_{today}.json"],
    "morning-pipeline":       ["tools/scan_{today}.json", "tools/regime_{today}.json"],
    "evening-exit-tracking":  ["signals/evening_exit_{today}.json"],
}
# Slug -> Script-Kandidaten fuer /scheduled_tasks/force_run (erstes vorhandenes)
TASK_SCRIPTS = {
    "daily-universe-scanner": ["tools/daily_scanner.py"],
    "morning-pipeline":       ["tools/run_morning_pipeline.py"],
    "evening-exit-tracking":  ["tools/evening_exit_tracking.py"],
}

def _parse_skill_md(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"slug": path.parent.name, "error": "read-fail"}
    front: dict[str, str] = {}
    body_lines: list[str] = []
    in_front = False
    finished_front = False
    for i, ln in enumerate(text.splitlines()):
        if i == 0 and ln.strip() == "---":
            in_front = True; continue
        if in_front and ln.strip() == "---":
            in_front = False; finished_front = True; continue
        if in_front:
            if ":" in ln:
                k, v = ln.split(":", 1)
                front[k.strip()] = v.strip()
        else:
            body_lines.append(ln)
    body = "\n".join(body_lines)
    # Time-Hinweis heuristisch extrahieren
    import re
    time_match = re.search(r"(\d{1,2}[:.]\d{2})\s*(?:CET|Uhr|h)?", body)
    time_hint = time_match.group(1).replace(".", ":") if time_match else None
    return {
        "slug": path.parent.name,
        "name": front.get("name") or path.parent.name,
        "description": front.get("description", "").strip('"').strip("'"),
        "time_hint": time_hint,
        "skill_md": str(path.relative_to(ROOT)),
    }


def _task_output_check(slug: str, today: str) -> dict:
    """Schaut ob heute was geschrieben wurde das zur Task passt. Heuristik."""
    files = [f.format(today=today) for f in TASK_OUTPUT_RULES.get(slug, [])]
    for f in files:
        fp = ROOT / f
        if fp.exists():
            return {"last_run": datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "fresh_today": True, "output_file": f}
    return {"last_run": None, "fresh_today": False, "output_file": None}


@app.get("/scheduled_tasks")
def scheduled_tasks_list():
    """Liste aller SKILL.md-basierten Scheduled-Tasks + Live-Status."""
    today = datetime.now().strftime("%Y-%m-%d")
    tasks = []
    if TASKS_DIR.exists():
        for skill_md in TASKS_DIR.glob("*/SKILL.md"):
            t = _parse_skill_md(skill_md)
            t.update(_task_output_check(t["slug"], today))
            tasks.append(t)
    return jsonify(ok=True, tasks=tasks, count=len(tasks), today=today,
                   tasks_dir=TASKS_DIR.name)


@app.post("/scheduled_tasks/force_run")
def scheduled_tasks_force():
    """POST {slug} → führt das zur Task gehörende Hauptscript aus."""
    data = request.get_json(force=True, silent=True) or {}
    slug = (data.get("slug") or "").strip()
    cands = [ROOT / rel for rel in TASK_SCRIPTS.get(slug, [])]
    if not cands:
        return jsonify(ok=False, msg=f"Unbekannter Task-Slug: {slug}"), 400
    ok, msg = _run_script(cands)
    return jsonify(ok=ok, msg=msg), (200 if ok else 500)


# ---- /news/position -------------------------------------------------------
@app.get("/news/position")
def news_position():
    """Google-News-RSS pro Ticker. Free, keine API. Limit 10 Items.
    Query: ticker, lang (de|en, default de)"""
    tkr = (request.args.get("ticker") or "").upper().strip()
    lang = (request.args.get("lang") or "de").lower().strip()
    if not tkr:
        return jsonify(ok=False, msg="ticker fehlt"), 400
    # SEC-8b: Ticker-Whitelist A-Z0-9.- (verhindert URL-Injection in News-Query)
    import re as _re
    if not _re.match(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$", tkr):
        return jsonify(ok=False, msg="Ticker-Format ungültig"), 400
    # SEC-8b: TTL-Cache 5 min
    cache_key = f"news::{lang}::{tkr}"
    cached = _cache_get(cache_key, ttl_s=300.0)
    if cached is not None:
        return jsonify(ok=True, ticker=tkr, items=cached, count=len(cached), cached=True)
    try:
        import urllib.request
        import xml.etree.ElementTree as ET
        q = f"{tkr}+stock"
        hl = "de&gl=DE&ceid=DE:de" if lang == "de" else "en-US&gl=US&ceid=US:en"
        url = f"https://news.google.com/rss/search?q={q}&hl={hl}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml)
        items = []
        for idx, it in enumerate(root.iterfind(".//item")):
            if idx >= 10: break
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link")  or "").strip()
            pub   = (it.findtext("pubDate") or "").strip()
            # <source> kann namespaced sein; probiere unqualifiziert, dann nach Suffix-Match
            src = ""
            src_el = it.find("source")
            if src_el is not None and src_el.text:
                src = src_el.text
            else:
                for child in it:
                    if child.tag.endswith("source") and child.text:
                        src = child.text; break
            # Material/Noise-Tagger (simpel)
            kws_material = ["earnings", "guidance", "downgrade", "upgrade", "lawsuit",
                            "acquisition", "merger", "spin-off", "fda", "recall",
                            "bankruptcy", "dividend", "buyback", "split"]
            material = any(k in title.lower() for k in kws_material)
            items.append({"title": title, "link": link, "pub": pub, "source": src,
                          "material": material})
        _cache_set(cache_key, items)
        return jsonify(ok=True, ticker=tkr, items=items, count=len(items), cached=False)
    except Exception as e:
        return jsonify(ok=False, ticker=tkr, items=[], msg=_safe_err(e, "Fetch-Fehler")), 200


# ---- /replay --------------------------------------------------------------
SNAPSHOTS_DIR = DASH_V7 / "_snapshots"


def _snapshot_rotate(retention_days: int = 30, hard_cap: int = 500) -> int:
    """SEC-8a: Snapshots älter als N Tage oder mehr als hard_cap → löschen.
    Return: Anzahl gelöschter Files."""
    if not SNAPSHOTS_DIR.exists():
        return 0
    removed = 0
    cutoff = time.time() - (retention_days * 86400)
    files = sorted(SNAPSHOTS_DIR.glob("snap_*.json"), key=lambda p: p.stat().st_mtime)
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(); removed += 1
        except Exception:
            pass
    # Hard-Cap: behalte nur jüngste hard_cap
    files = sorted(SNAPSHOTS_DIR.glob("snap_*.json"), key=lambda p: p.stat().st_mtime)
    if len(files) > hard_cap:
        for f in files[:-hard_cap]:
            try: f.unlink(); removed += 1
            except Exception: pass
    return removed


def _snapshot_now() -> dict:
    """Macht einen State-Snapshot des aktuellen Cockpits (state + heartbeat + trades_snapshot)."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    snap = {
        "ts": ts,
        "ts_iso": datetime.now().isoformat(timespec="seconds"),
        "state": _read_json(STATE_PATH, {}),
        "heartbeat": _read_json(HEARTBEAT_PATH, {}),
        "trades_snapshot": _read_json(TRADES_SNAPSHOT_PATH, {}),
    }
    out = SNAPSHOTS_DIR / f"snap_{ts}.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2))
    rotated = _snapshot_rotate()
    return {"ok": True, "file": out.name, "ts": ts, "rotated": rotated}


# --- SEC-8b: In-Memory-TTL-Cache für externe Fetches ------------------------
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}

def _cache_get(key: str, ttl_s: float):
    with _cache_lock:
        item = _cache.get(key)
        if not item: return None
        ts, val = item
        if time.time() - ts > ttl_s:
            _cache.pop(key, None); return None
        return val

def _cache_set(key: str, val):
    with _cache_lock:
        _cache[key] = (time.time(), val)
        # Simple LRU-ish: wenn > 200 Einträge, älteste 50 entfernen
        if len(_cache) > 200:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:50]
            for k, _ in oldest: _cache.pop(k, None)


@app.post("/replay/snapshot_now")
def replay_snapshot_now():
    try:
        res = _snapshot_now()
        return jsonify(res)
    except Exception as e:
        return jsonify(ok=False, msg=f"Snap-Fehler: {type(e).__name__}"), 500


@app.get("/replay/snapshots")
def replay_snapshots():
    """Listet alle vorhandenen Snapshots (Zeitachsen-Grundlage)."""
    if not SNAPSHOTS_DIR.exists():
        return jsonify(ok=True, snapshots=[], note="Noch keine Snapshots vorhanden")
    files = sorted(SNAPSHOTS_DIR.glob("snap_*.json"))
    items = []
    for f in files:
        try:
            st = f.stat()
            items.append({"file": f.name,
                          "ts": f.name.replace("snap_", "").replace(".json", ""),
                          "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                          "size": st.st_size})
        except Exception:
            continue
    return jsonify(ok=True, snapshots=items, count=len(items))


@app.get("/replay")
def replay_get():
    """GET ?file=snap_...json → vollständiger Snapshot zurück.
    SEC-5: Path-Traversal-Härtung via resolve() + is_relative_to()."""
    fname = (request.args.get("file") or "").strip()
    # Rauhe Vorab-Prüfung (schnell-ablehnen offensichtlich bösartig)
    if not fname or any(c in fname for c in ("/", "\\", "..", "\x00", "%")):
        return jsonify(ok=False, msg="ungültiger file-Parameter"), 400
    if not fname.startswith("snap_") or not fname.endswith(".json"):
        return jsonify(ok=False, msg="Nur snap_*.json erlaubt"), 400
    try:
        # Autoritative Prüfung: tatsächlicher resolved Pfad muss unterhalb SNAPSHOTS_DIR liegen
        resolved_dir = SNAPSHOTS_DIR.resolve()
        p = (SNAPSHOTS_DIR / fname).resolve()
        if not str(p).startswith(str(resolved_dir) + os.sep) and p.parent != resolved_dir:
            return jsonify(ok=False, msg="Pfad außerhalb Snapshot-Dir"), 400
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Path-Resolve-Fehler")), 400
    if not p.exists():
        return jsonify(ok=False, msg="Snapshot nicht gefunden"), 404
    try:
        return jsonify(ok=True, snapshot=json.loads(p.read_text()))
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Parse-Fehler")), 500


# ---- V7.5 · /replay/delta --------------------------------------------------
# Δ-Overlay: Gestern vs. Heute vergleichen. Parst die Top-10-Tabelle aus dem
# Trading-Empfehlung-V6.1-YYYY-MM-DD.md und den Briefing-Fallback und liefert
# - ENTRANTS : heute neu im Top-10
# - DROPOUTS : gestern im Top-10, heute nicht mehr
# - MOVERS   : beide Tage im Top-10, mit Rank-/Score-Delta
# - REGIME   : grober Tilt-Vergleich (falls extrahierbar)
def _parse_briefing_top10_scored(date_str: str) -> dict:
    """Extrahiert Top-10 mit Score + Rank aus dem Briefing/der Empfehlung.
    Return: {"date": str, "file": str|None, "rows": [{"rank":int,"ticker":str,"score":float|None,"cq":int|None}]}
    """
    candidates = [
        ROOT / f"Trading-Empfehlung-V6.1-{date_str}.md",
        ROOT / f"Trading-Empfehlung-{date_str}.md",
        ROOT / f"V6.1-Global-Market-Briefing-{date_str}.md",
        ROOT / f"Global-Market-Briefing-{date_str}.md",
    ]
    f = next((p for p in candidates if p.exists()), None)
    if not f:
        return {"date": date_str, "file": None, "rows": []}
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"date": date_str, "file": f.name, "rows": []}

    # Zeilen der Form `| 1 | **HSY** | ... | 14 | **6,20** | 2,6 | ...`
    # Wir splitten auf |, trimmen Zellen, und picken (rank, ticker, score).
    rows: list[dict] = []
    seen_tickers: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(parts) < 8:
            continue
        # Col 0: rank (reine Zahl 1..10)
        if not parts[0].isdigit():
            continue
        rank = int(parts[0])
        if rank < 1 or rank > 10:
            continue
        # Col 1: ticker (evtl. mit ** umgeben)
        t_raw = parts[1].replace("**", "").strip()
        if not t_raw or not t_raw[0].isalpha():
            continue
        ticker = t_raw.split()[0][:12].upper()
        if ticker in seen_tickers:
            continue
        # Score: versuche Col 7 (Pre-Score), ggf. 8, falls Kopf abweicht
        def _parse_de_float(s: str):
            s = s.replace("**", "").replace(" ", "").replace("%", "").replace(".", "").replace(",", ".")
            try: return float(s)
            except Exception: return None
        score = None
        for col_idx in (7, 6, 8):
            if col_idx < len(parts):
                v = _parse_de_float(parts[col_idx])
                if v is not None and 0.0 <= v <= 12.0:
                    score = v; break
        # CQ (col 6 meist reines int)
        cq = None
        for col_idx in (6, 5, 7):
            if col_idx < len(parts):
                s = parts[col_idx].replace("**", "").strip()
                if s.isdigit() and int(s) <= 30:
                    cq = int(s); break
        rows.append({"rank": rank, "ticker": ticker, "score": score, "cq": cq})
        seen_tickers.add(ticker)
        if len(rows) >= 10:
            break
    rows.sort(key=lambda r: r["rank"])
    return {"date": date_str, "file": f.name, "rows": rows}


@app.get("/replay/delta")
def replay_delta():
    """Δ-Overlay Gestern vs. Heute.
    Query: ?today=YYYY-MM-DD&yesterday=YYYY-MM-DD (beide optional).
    Default: today=heute, yesterday=heute-1d (Kalenderrückwärts, auch über WE).
    """
    today_param = (request.args.get("today") or "").strip()
    yesterday_param = (request.args.get("yesterday") or "").strip()

    def _ok_date(s: str) -> bool:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", s or ""):
            return False
        try:
            datetime.strptime(s, "%Y-%m-%d"); return True
        except Exception:
            return False

    today_str = today_param if _ok_date(today_param) else datetime.now().strftime("%Y-%m-%d")
    if _ok_date(yesterday_param):
        yest_str = yesterday_param
    else:
        t = datetime.strptime(today_str, "%Y-%m-%d")
        # Vortag: falls kein File, nimm letzten Werktag (bis 5 Tage zurueck)
        yest_str = (t - timedelta(days=1)).strftime("%Y-%m-%d")
        if not any((ROOT / f"Trading-Empfehlung-V6.1-{yest_str}.md").exists()
                   or (ROOT / f"V6.1-Global-Market-Briefing-{yest_str}.md").exists() for _ in [0]):
            for back in range(2, 6):
                candidate = (t - timedelta(days=back)).strftime("%Y-%m-%d")
                if ((ROOT / f"Trading-Empfehlung-V6.1-{candidate}.md").exists()
                        or (ROOT / f"V6.1-Global-Market-Briefing-{candidate}.md").exists()):
                    yest_str = candidate; break

    try:
        today_block = _parse_briefing_top10_scored(today_str)
        yest_block = _parse_briefing_top10_scored(yest_str)
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Briefing-Parse-Fehler")), 500

    t_map = {r["ticker"]: r for r in today_block["rows"]}
    y_map = {r["ticker"]: r for r in yest_block["rows"]}

    t_set, y_set = set(t_map), set(y_map)
    entrants = sorted(t_set - y_set, key=lambda k: t_map[k]["rank"])
    dropouts = sorted(y_set - t_set, key=lambda k: y_map[k]["rank"])
    movers_syms = t_set & y_set

    entrants_rows = [{"ticker": k, **t_map[k]} for k in entrants]
    dropouts_rows = [{"ticker": k, **y_map[k]} for k in dropouts]
    movers_rows = []
    for k in movers_syms:
        rt, ry = t_map[k], y_map[k]
        ds = (rt["score"] - ry["score"]) if (rt["score"] is not None and ry["score"] is not None) else None
        dr = rt["rank"] - ry["rank"]   # negativ = aufgestiegen
        movers_rows.append({
            "ticker": k,
            "rank_today": rt["rank"], "rank_yest": ry["rank"], "rank_delta": dr,
            "score_today": rt["score"], "score_yest": ry["score"], "score_delta": ds,
        })
    movers_rows.sort(key=lambda r: (abs(r["rank_delta"] or 0), abs(r.get("score_delta") or 0)), reverse=True)

    return jsonify(
        ok=True,
        today={"date": today_block["date"], "file": today_block["file"], "n": len(t_set)},
        yesterday={"date": yest_block["date"], "file": yest_block["file"], "n": len(y_set)},
        entrants=entrants_rows,
        dropouts=dropouts_rows,
        movers=movers_rows,
        note=f"{len(entrants)} neu · {len(dropouts)} raus · {len(movers_rows)} Bewegungen",
    )


# ============================================================================
# V7.4 · Discipline-Layer Endpunkte (2026-04-17)
#   /discipline/state, /discipline/override
# ============================================================================
try:
    from tools.discipline_engine import (
        compute_state as _disc_compute_state,
        ensure_overrides as _disc_ensure_overrides,
        log_override as _disc_log_override,
        VALID_KINDS as _DISC_KINDS,
    )
except ImportError:
    # Fallback wenn via PYTHONPATH anders aufgerufen
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from tools.discipline_engine import (
        compute_state as _disc_compute_state,
        ensure_overrides as _disc_ensure_overrides,
        log_override as _disc_log_override,
        VALID_KINDS as _DISC_KINDS,
    )


@app.get("/discipline/state")
def discipline_state():
    """V7.4 Discipline-Layer State.
    Liefert Revenge-Streak, Opens-Today, Cooldown, FOMO-Whitelist, Flags.
    Query: ?date=YYYY-MM-DD (optional, default heute)."""
    date = (request.args.get("date") or "").strip() or None
    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(con)
        state = _disc_compute_state(con, today=date)
        con.close()
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Discipline-State-Fehler")), 500
    return jsonify(ok=True, **state)


@app.post("/discipline/override")
def discipline_override():
    """V7.4 Override-Log. POST JSON:
        {kind: COOLDOWN_OVERRIDE|FOMO_OVERRIDE|OVERTRADE_OVERRIDE,
         reason: "<freitext>", ticker?: str, meta?: dict}
    Protokolliert, dass der User eine Guardrail überschrieben hat, für Retro-Analyse
    (hoher Override-Count = Disziplin-Baustelle)."""
    data = request.get_json(force=True, silent=True) or {}
    kind   = str(data.get("kind") or "").strip().upper()
    reason = str(data.get("reason") or "").strip()
    ticker = str(data.get("ticker") or "").strip().upper() or None
    meta   = data.get("meta") if isinstance(data.get("meta"), dict) else {}

    if kind not in _DISC_KINDS:
        return jsonify(ok=False, msg=f"kind muss einer von {sorted(_DISC_KINDS)} sein"), 400
    if not reason or len(reason) < 3:
        return jsonify(ok=False, msg="reason Pflicht (min 3 Zeichen)"), 400
    if len(reason) > 1000:
        return jsonify(ok=False, msg="reason zu lang (max 1000 Zeichen)"), 400

    try:
        con = sqlite3.connect(str(TRADE_DB))
        con.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(con)
        _disc_ensure_overrides(con)
        # State-Snapshot für Retro-Analyse
        state_snap = _disc_compute_state(con)
        rowid = _disc_log_override(
            con, kind=kind, reason=reason, ticker=ticker,
            meta=meta, state_snapshot=state_snap,
        )
        con.close()
    except ValueError as e:
        return jsonify(ok=False, msg=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Override-Log-Fehler")), 500

    # SSE-Push (analog zu trade_logged)
    try:
        EventBus.publish("discipline_override",
                         {"kind": kind, "ticker": ticker, "reason": reason[:80]})
    except Exception:
        pass

    return jsonify(ok=True, id=rowid, kind=kind, ticker=ticker,
                   msg=f"Override protokolliert (id={rowid}, kind={kind})")


# --- Kelly-Sizer Widget (STRAT-KELLY-1 Shadow, 2026-04-18) ---------------

@app.get("/kelly_status")
def kelly_status():
    """STRAT-KELLY-1 Shadow-Sizing-Status.

    Liefert pro Setup das empfohlene Half-Kelly-Sizing (shadow only, kein Live-Routing).
    Query:
      ?setups=V6.1,V6   (default: V6.1)
      ?capital=10000    (EUR, default aus state.json equity falls vorhanden, sonst 10000)
      ?vix=14.5         (optional, floated VIX fuer Regime-Detection)
    """
    import importlib.util, sys
    setups = [s.strip() for s in (request.args.get("setups") or "V6.1").split(",") if s.strip()]
    try:
        capital = float(request.args.get("capital") or 0.0)
    except (TypeError, ValueError):
        capital = 0.0
    vix_q = request.args.get("vix")
    try:
        vix = float(vix_q) if vix_q not in (None, "", "null") else None
    except (TypeError, ValueError):
        vix = None

    # Fallback auf state.json-equity wenn kein Capital uebergeben
    if capital <= 0:
        try:
            st = _read_json(STATE_PATH, {})
            capital = float(st.get("account_equity") or st.get("equity") or 10000.0)
        except Exception:
            capital = 10000.0

    # Kelly-Sizer on-the-fly laden (tools/kelly_sizer.py, keine package-Struktur)
    try:
        ks_path = ROOT / "tools" / "kelly_sizer.py"
        spec = importlib.util.spec_from_file_location("kelly_sizer", ks_path)
        ks = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("kelly_sizer", ks)
        spec.loader.exec_module(ks)  # type: ignore
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Kelly-Modul-Ladefehler")), 500

    out = {"capital_eur": capital, "vix": vix, "setups": {}}
    for s in setups:
        try:
            kr = ks.size_for_setup(s, capital, vix, db_path=TRADE_DB)
            out["setups"][s] = {
                "setup": kr.setup,
                "n_trades": kr.n_trades,
                "win_rate": round(kr.win_rate, 4),
                "avg_win_pct": round(kr.avg_win_pct, 4),
                "avg_loss_pct": round(kr.avg_loss_pct, 4),
                "sharpe_252d": round(kr.sharpe_252d, 4),
                "sigma_252d": round(kr.sigma_252d, 4),
                "f_kelly_raw": round(kr.f_kelly_raw, 4),
                "f_applied": round(kr.f_applied, 4),
                "size_eur": kr.size_eur,
                "size_pct": round((kr.size_eur / capital * 100) if capital > 0 else 0, 2),
                "regime": kr.regime,
                "reason": kr.reason,
            }
        except FileNotFoundError as e:
            out["setups"][s] = {"error": str(e)}
        except Exception as e:
            out["setups"][s] = {"error": _safe_err(e, "Kelly-Setup-Fehler")}

    return jsonify(ok=True, **out)


# --- Tax-Loss-Harvester Tile (X-1 Tag 2 · Block I · V8.0 prep · 2026-04-21) ---

@app.get("/tax_loss/latest")
def tax_loss_latest():
    """Liefert den neuesten tax_loss_harvest_YYYY-MM-DD.json Report aus briefings/.

    Produziert von tools/tax_loss_harvester.py (X-1, KW17 Rollout Tag 1).
    Read-only: falls keine Datei existiert, leere Shape mit ok=False + msg.

    Response-Shape:
      {
        "ok": bool,
        "as_of": "YYYY-MM-DD",
        "age_days": int,
        "toepfe": { aktien_realized_eur, derivate_realized_eur, ... },
        "sparer_used_eur": float,
        "sparer_remaining_eur": float,
        "tfs_verlust_nutzbar_eur": float,
        "candidates": [{ticker, topf, unrealized_pnl, ...}, ...],
        "actions": ["...", ...],
        "hook_trigger": bool
      }
    """
    briefings_dir = ROOT / "briefings"
    if not briefings_dir.exists():
        return jsonify(ok=False, msg="briefings/ existiert nicht"), 200

    # Suche neueste Datei tax_loss_harvest_YYYY-MM-DD.json
    try:
        candidates = sorted(
            briefings_dir.glob("tax_loss_harvest_*.json"),
            key=lambda p: p.name,
            reverse=True,
        )
    except Exception as e:
        return jsonify(ok=False, msg=_safe_err(e, "Tax-Loss-Scan-Fehler")), 500

    if not candidates:
        return jsonify(
            ok=False,
            msg="Kein tax_loss_harvest_*.json vorhanden: X-1 noch nicht gelaufen.",
        ), 200

    latest = candidates[0]
    data = _read_json(latest, {})
    if not data:
        return jsonify(ok=False, msg=f"Datei {latest.name} nicht parsbar"), 200

    # Altersberechnung
    as_of = data.get("as_of") or latest.stem.replace("tax_loss_harvest_", "")
    age_days = None
    try:
        as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date()
        age_days = (datetime.now().date() - as_of_date).days
    except (TypeError, ValueError):
        pass

    return jsonify(
        ok=True,
        as_of=as_of,
        file=latest.name,
        age_days=age_days,
        year_start=data.get("year_start"),
        spar_pauschbetrag_eur=data.get("spar_pauschbetrag_eur"),
        tfs_cap_eur=data.get("tfs_cap_eur"),
        loss_threshold_eur=data.get("loss_threshold_eur"),
        toepfe=data.get("toepfe") or {},
        sparer_used_eur=data.get("sparer_used_eur"),
        sparer_remaining_eur=data.get("sparer_remaining_eur"),
        tfs_verlust_nutzbar_eur=data.get("tfs_verlust_nutzbar_eur"),
        candidates=data.get("candidates") or [],
        actions=data.get("actions") or [],
        hook_trigger=bool(data.get("hook_trigger")),
        disclaimer="Not tax advice. Informational only, verify with a tax advisor.",
    )


# --- Boot -----------------------------------------------------------------
if __name__ == "__main__":
    _ensure_state_file()
    print(f"[dashboard_api] bind 127.0.0.1:{PORT}  root={ROOT}")
    print(f"[dashboard_api] state={STATE_PATH.relative_to(ROOT)}  db={TRADE_DB.name}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
