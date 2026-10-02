#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adjudication_gate.py: ADJUDICATION-DATA-QUALITY-GATE-1  (V7.36, BUGFIX)

Prevents adjudication on degraded scans (low fill rate or CRITICAL data health).

This tool is the single mechanical import point that a recommendation or
adjudication workflow should run BEFORE any adjudication step (see the
project documentation). It does not add a new live rule, it enforces the
existing doctrine mechanically:

    "data_health CRITICAL => NO-TRADE"  und  "Fill-Floor 0.80"
    (identisch zu data_health SCAN_FILL_LT_80/H und daily_scanner V7.32
     valid_fill_guard: EIN Floor, drei Enforcement-Punkte).

Gate-Kette (fail-safe, timing-unabhaengig von data_health: Fill kommt DIREKT
aus dem Scan-JSON, Fix-Richtung (b) des Tickets):

    1. SCAN_PRESENT     tools/scan_<date>.json existiert + parsebar
                        (fehlt/kaputt => NO_TRADE, rc=2; scan_abort_<date>.json
                         wird als Kontext mitgeliefert)
    2. SCAN_DATE_MATCH  scan_date im JSON == angefragtes Datum (Stale-Guard, rc=5)
    3. SCAN_FILL        valid_results/universe_size >= --fill-floor (0.80)
                        (verletzt => NO_TRADE, rc=3)
    4. DATA_HEALTH      tools/data_health_<date>.json:
                          CRITICAL            => NO_TRADE, rc=4
                          nicht-OK (WARN/...) => TRADE_ALLOWED_WITH_WARNINGS
                          ABSENT (Timing)     => Warnung, KEIN Block
                        (Gate 3 ist der timing-unabhaengige Primaerschutz)

Verdikt: TRADE_ALLOWED / TRADE_ALLOWED_WITH_WARNINGS (rc=0) oder NO_TRADE_* (rc>0).
Zusaetzlich liefert das Tool fertige deutsche Markdown-Zeilen fuer die
Briefing-Quellen-Quittung (`briefing_source_rows`): Fill-VERDIKT statt
Datei-Existenz (Fix-Richtung (c)).

rc-Vertrag (kollisionsfrei dokumentiert):
    0 = TRADE_ALLOWED / TRADE_ALLOWED_WITH_WARNINGS
    1 = TOOL_ERROR (unerwartete Exception)
    2 = NO_TRADE_SCAN_MISSING   (kein/kaputtes Scan-Artefakt)
    3 = NO_TRADE_SCAN_FILL      (Fill < Floor)
    4 = NO_TRADE_DATA_HEALTH    (overall_status CRITICAL)
    5 = NO_TRADE_STALE_SCAN     (scan_date-Mismatch)

WICHTIG:
  * READ-ONLY auf alle Inputs. Kein Netz-Fetch. Keine Schwellen-/Sizing-Aenderung.
  * Bei rc=2/3 gilt weiter die Self-Heal-Regel (Pipeline nativ nachstarten,
    pollen, DANN erneut gaten): dieses Tool ersetzt den Self-Heal nicht,
    es verhindert nur die Adjudikation auf kaputten Daten.

Artefakte (--write):
  signals/adjudication_gate_<date>.json

CLI:
  python3 tools/adjudication_gate.py --selftest
  python3 tools/adjudication_gate.py [--date 2026-07-02] [--fill-floor 0.80]
                                     [--write] [--json]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import tempfile

VERSION = "V7.36"
TOOL = "adjudication_gate"
DEFAULT_FILL_FLOOR = 0.80  # == data_health SCAN_FILL_LT_80 == daily_scanner V7.32 valid_fill_guard

# Verdikte / rc-Vertrag
RC_OK = 0
RC_TOOL_ERROR = 1
RC_SCAN_MISSING = 2
RC_SCAN_FILL = 3
RC_DATA_HEALTH = 4
RC_STALE_SCAN = 5

VERDICT_ALLOWED = "TRADE_ALLOWED"
VERDICT_ALLOWED_WARN = "TRADE_ALLOWED_WITH_WARNINGS"
VERDICT_NO_TRADE = "NO_TRADE"


# --------------------------------------------------------------------------
# Pfad-Resolver (injizierbares base_dir fuer Tests/Selftest)
# --------------------------------------------------------------------------
def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p_scan(base: str, date: str) -> str:
    return os.path.join(base, "tools", "scan_%s.json" % date)


def _p_scan_abort(base: str, date: str) -> str:
    return os.path.join(base, "tools", "scan_abort_%s.json" % date)


def _p_data_health(base: str, date: str) -> str:
    return os.path.join(base, "tools", "data_health_%s.json" % date)


def _p_out(base: str, date: str) -> str:
    return os.path.join(base, "signals", "adjudication_gate_%s.json" % date)


def _load_json(path: str):
    """(ok, payload_or_error): fail-safe Reader, wirft nie."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return True, json.load(fh)
    except FileNotFoundError:
        return False, "ABSENT"
    except Exception as exc:  # kaputtes JSON / EDEADLK / etc.
        return False, "PARSE_ERROR: %s" % exc.__class__.__name__


def _fmt_pct_de(x: float) -> str:
    return ("%.1f" % (100.0 * x)).replace(".", ",")


# --------------------------------------------------------------------------
# Kern-Evaluierung
# --------------------------------------------------------------------------
def evaluate(date: str, base_dir: str = None, fill_floor: float = DEFAULT_FILL_FLOOR) -> dict:
    """Wendet die Gate-Kette an. Read-only. Gibt kanonisches Verdikt-Dict zurueck."""
    base = base_dir or project_root()
    checks = []
    warnings = []
    rows = []  # briefing_source_rows (Markdown)
    verdict = VERDICT_ALLOWED
    no_trade_reason = None
    rc = RC_OK

    def _fail(reason: str, _rc: int):
        nonlocal verdict, no_trade_reason, rc
        if verdict != VERDICT_NO_TRADE:  # erste Verletzung bestimmt rc/reason
            verdict = VERDICT_NO_TRADE
            no_trade_reason = reason
            rc = _rc

    # ---- Gate 1: SCAN_PRESENT ------------------------------------------------
    scan_path = _p_scan(base, date)
    ok, scan = _load_json(scan_path)
    abort_ok, abort = _load_json(_p_scan_abort(base, date))
    abort_ctx = abort if abort_ok else None
    if not ok:
        detail = {"path": scan_path, "error": scan,
                  "scan_abort_present": bool(abort_ok),
                  "scan_abort_reason": (abort_ctx or {}).get("reason") if isinstance(abort_ctx, dict) else None}
        checks.append({"id": "SCAN_PRESENT", "status": "FAIL", "detail": detail})
        _fail("SCAN_MISSING", RC_SCAN_MISSING)
        rows.append("| Universe-Scanner `scan_%s.json` | Artefakt fehlt/unlesbar (%s)%s | ❌ NO-TRADE |"
                    % (date, scan,
                       " · scan_abort: %s" % detail["scan_abort_reason"] if detail["scan_abort_reason"] else ""))
        scan = None
    else:
        checks.append({"id": "SCAN_PRESENT", "status": "PASS", "detail": {"path": scan_path}})

    # ---- Gate 2: SCAN_DATE_MATCH ----------------------------------------------
    if isinstance(scan, dict):
        scan_date = str(scan.get("scan_date") or "")
        if scan_date and scan_date != date:
            checks.append({"id": "SCAN_DATE_MATCH", "status": "FAIL",
                           "detail": {"scan_date": scan_date, "requested": date}})
            _fail("STALE_SCAN", RC_STALE_SCAN)
            rows.append("| Universe-Scanner `scan_%s.json` | scan_date=%s ≠ %s (stale) | ❌ NO-TRADE |"
                        % (date, scan_date, date))
        else:
            checks.append({"id": "SCAN_DATE_MATCH", "status": "PASS" if scan_date else "WARN",
                           "detail": {"scan_date": scan_date or "ABSENT_FIELD"}})
            if not scan_date:
                warnings.append("scan_date-Feld fehlt im Scan-JSON (Match-Guard uebersprungen)")
    else:
        checks.append({"id": "SCAN_DATE_MATCH", "status": "SKIP", "detail": {"reason": "no scan"}})

    # ---- Gate 3: SCAN_FILL (timing-unabhaengiger Primaerschutz) ----------------
    fill_rate = None
    if isinstance(scan, dict):
        try:
            uni = int(scan.get("universe_size") or 0)
            valid = int(scan.get("valid_results") or 0)
        except (TypeError, ValueError):
            uni, valid = 0, 0
        if uni > 0:
            fill_rate = valid / float(uni)
            passed = fill_rate >= fill_floor
            checks.append({"id": "SCAN_FILL", "status": "PASS" if passed else "FAIL",
                           "detail": {"valid_results": valid, "universe_size": uni,
                                      "fill_rate": round(fill_rate, 4), "floor": fill_floor}})
            pct = _fmt_pct_de(fill_rate)
            floor_pct = _fmt_pct_de(fill_floor)
            if passed:
                rows.append("| Universe-Scanner `scan_%s.json` | Fill **%s %%** (%d/%d) ≥ Floor %s %% | ✅ |"
                            % (date, pct, valid, uni, floor_pct))
            else:
                _fail("SCAN_FILL_BELOW_FLOOR", RC_SCAN_FILL)
                rows.append("| Universe-Scanner `scan_%s.json` | Fill **%s %%** (%d/%d) < Floor %s %% | ❌ SUB-FLOOR → NO-TRADE |"
                            % (date, pct, valid, uni, floor_pct))
        else:
            checks.append({"id": "SCAN_FILL", "status": "FAIL",
                           "detail": {"reason": "universe_size fehlt/0: Fill nicht bestimmbar (fail-safe)"}})
            _fail("SCAN_FILL_BELOW_FLOOR", RC_SCAN_FILL)
            rows.append("| Universe-Scanner `scan_%s.json` | universe_size fehlt/0: Fill nicht pruefbar | ❌ NO-TRADE (fail-safe) |" % date)
    else:
        checks.append({"id": "SCAN_FILL", "status": "SKIP", "detail": {"reason": "no scan"}})

    # ---- Gate 4: DATA_HEALTH ----------------------------------------------------
    dh_path = _p_data_health(base, date)
    dh_ok, dh = _load_json(dh_path)
    if dh_ok and isinstance(dh, dict):
        status = str(dh.get("overall_status") or "UNKNOWN").upper()
        h_findings = [f for f in (dh.get("findings") or [])
                      if isinstance(f, dict) and str(f.get("severity", "")).upper() == "H"]
        h_codes = [str(f.get("code") or f.get("finding") or "?") for f in h_findings]
        if status == "CRITICAL":
            checks.append({"id": "DATA_HEALTH", "status": "FAIL",
                           "detail": {"overall_status": status, "h_findings": h_codes}})
            _fail("DATA_HEALTH_CRITICAL", RC_DATA_HEALTH)
            rows.append("| `data_health_%s.json` | overall **CRITICAL** (%d H: %s) | ❌ NO-TRADE |"
                        % (date, len(h_codes), ", ".join(h_codes) or "-"))
        elif status == "OK":
            checks.append({"id": "DATA_HEALTH", "status": "PASS", "detail": {"overall_status": status}})
            rows.append("| `data_health_%s.json` | overall OK | ✅ |" % date)
        else:
            checks.append({"id": "DATA_HEALTH", "status": "WARN",
                           "detail": {"overall_status": status, "h_findings": h_codes}})
            warnings.append("data_health overall_status=%s (nicht OK, nicht CRITICAL)" % status)
            rows.append("| `data_health_%s.json` | overall %s | ⚠️ |" % (date, status))
    else:
        checks.append({"id": "DATA_HEALTH", "status": "WARN",
                       "detail": {"path": dh_path, "error": dh}})
        warnings.append("data_health_%s.json nicht lesbar (%s): Fill-Gate ist Primaerschutz" % (date, dh))
        rows.append("| `data_health_%s.json` | nicht vorhanden/lesbar (Timing): Fill-Gate greift | ⚠️ |" % date)

    if verdict == VERDICT_ALLOWED and warnings:
        verdict = VERDICT_ALLOWED_WARN

    return {
        "version": VERSION,
        "tool": TOOL,
        "date": date,
        "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fill_floor": fill_floor,
        "fill_rate": round(fill_rate, 4) if fill_rate is not None else None,
        "checks": checks,
        "warnings": warnings,
        "verdict": verdict,
        "no_trade_reason": no_trade_reason,
        "rc": rc,
        "briefing_source_rows": rows,
        "note": "READ-ONLY Gate; erzwingt bestehende Doktrin (CRITICAL/Sub-Floor => NO-TRADE); keine neue Live-Regel/Schwelle/Sizing.",
    }


def write_artifact(result: dict, base_dir: str = None) -> str:
    base = base_dir or project_root()
    out = _p_out(base, result["date"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return out


def render_stdout(result: dict) -> str:
    lines = []
    lines.append("ADJUDICATION-GATE %s  ·  %s  ·  Floor %s %%"
                 % (result["version"], result["date"], _fmt_pct_de(result["fill_floor"])))
    for c in result["checks"]:
        lines.append("  [%-4s] %-16s %s" % (c["status"], c["id"],
                                            json.dumps(c["detail"], ensure_ascii=False)[:100]))
    for w in result["warnings"]:
        lines.append("  WARN: %s" % w)
    lines.append("VERDIKT: %s%s  (rc=%d)"
                 % (result["verdict"],
                    " %s" % result["no_trade_reason"] if result["no_trade_reason"] else "",
                    result["rc"]))
    lines.append("Quellen-Quittungs-Zeilen (fuer Briefing §Quellen, 1:1 uebernehmen):")
    for r in result["briefing_source_rows"]:
        lines.append("  %s" % r)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Selftest (synthetische Fixtures, 9 Gruppen)
# --------------------------------------------------------------------------
def _mk_base(tmp: str) -> str:
    os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "signals"), exist_ok=True)
    return tmp


def _w(base: str, rel: str, payload) -> None:
    path = os.path.join(base, rel)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)


def _scan(date: str, uni: int = 639, valid: int = 620) -> dict:
    return {"scan_date": date, "universe_size": uni, "valid_results": valid, "top_setups": []}


def _dh(date: str, status: str = "OK", h_codes=None) -> dict:
    return {"date": date, "overall_status": status,
            "findings": [{"severity": "H", "source": "scan", "code": c} for c in (h_codes or [])]}


def selftest() -> int:
    d = "2026-07-02"
    failures = []

    def check(name, cond):
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        base = _mk_base(tmp)

        print("Gruppe 1: healthy pass")
        _w(base, "tools/scan_%s.json" % d, _scan(d, 639, 615))
        _w(base, "tools/data_health_%s.json" % d, _dh(d, "OK"))
        r = evaluate(d, base)
        check("verdict TRADE_ALLOWED", r["verdict"] == VERDICT_ALLOWED)
        check("rc==0", r["rc"] == RC_OK)
        check("fill_rate ~0.962", abs((r["fill_rate"] or 0) - 615 / 639.0) < 1e-3)  # Feld ist auf 4 Dez. gerundet

        print("Gruppe 2: fill sub-floor (07-02-Muster 66%)")
        _w(base, "tools/scan_%s.json" % d, _scan(d, 639, 422))
        r = evaluate(d, base)
        check("verdict NO_TRADE", r["verdict"] == VERDICT_NO_TRADE)
        check("reason SCAN_FILL_BELOW_FLOOR", r["no_trade_reason"] == "SCAN_FILL_BELOW_FLOOR")
        check("rc==3", r["rc"] == RC_SCAN_FILL)
        check("md-row ❌ SUB-FLOOR", any("SUB-FLOOR" in x for x in r["briefing_source_rows"]))

        print("Gruppe 3: scan missing")
        os.remove(os.path.join(base, "tools", "scan_%s.json" % d))
        r = evaluate(d, base)
        check("rc==2", r["rc"] == RC_SCAN_MISSING)
        check("reason SCAN_MISSING", r["no_trade_reason"] == "SCAN_MISSING")

        print("Gruppe 4: scan missing + abort-marker Kontext")
        _w(base, "tools/scan_abort_%s.json" % d, {"reason": "VALID_FILL_FLOOR"})
        r = evaluate(d, base)
        sp = [c for c in r["checks"] if c["id"] == "SCAN_PRESENT"][0]
        check("abort reason propagiert", sp["detail"].get("scan_abort_reason") == "VALID_FILL_FLOOR")
        os.remove(os.path.join(base, "tools", "scan_abort_%s.json" % d))

        print("Gruppe 5: data_health CRITICAL trotz Fill ok")
        _w(base, "tools/scan_%s.json" % d, _scan(d, 639, 615))
        _w(base, "tools/data_health_%s.json" % d, _dh(d, "CRITICAL", ["EARNINGS_JSON_MISSING"]))
        r = evaluate(d, base)
        check("rc==4", r["rc"] == RC_DATA_HEALTH)
        check("reason DATA_HEALTH_CRITICAL", r["no_trade_reason"] == "DATA_HEALTH_CRITICAL")
        check("H-Code in md-row", any("EARNINGS_JSON_MISSING" in x for x in r["briefing_source_rows"]))

        print("Gruppe 6: data_health absent -> allowed mit Warnung (Fill-Gate Primaerschutz)")
        os.remove(os.path.join(base, "tools", "data_health_%s.json" % d))
        r = evaluate(d, base)
        check("verdict ALLOWED_WITH_WARNINGS", r["verdict"] == VERDICT_ALLOWED_WARN)
        check("rc==0", r["rc"] == RC_OK)
        check("warnung vorhanden", len(r["warnings"]) >= 1)

        print("Gruppe 7: stale scan_date mismatch")
        _w(base, "tools/scan_%s.json" % d, _scan("2026-07-01", 639, 615))
        r = evaluate(d, base)
        check("rc==5", r["rc"] == RC_STALE_SCAN)
        check("reason STALE_SCAN", r["no_trade_reason"] == "STALE_SCAN")

        print("Gruppe 8: kaputtes Scan-JSON -> fail-safe NO_TRADE (rc=2), kein Crash")
        _w(base, "tools/scan_%s.json" % d, "{not valid json")
        r = evaluate(d, base)
        check("rc==2 bei PARSE_ERROR", r["rc"] == RC_SCAN_MISSING)
        check("PARSE_ERROR im detail", "PARSE_ERROR" in str(r["checks"][0]["detail"].get("error")))

        print("Gruppe 9: --write Artefakt-Roundtrip + Prioritaet Fill vor data_health")
        _w(base, "tools/scan_%s.json" % d, _scan(d, 639, 235))  # 36,8% (07-01-Muster)
        _w(base, "tools/data_health_%s.json" % d, _dh(d, "CRITICAL", ["SCAN_FILL_LT_80"]))
        r = evaluate(d, base)
        check("erste Verletzung Fill (rc=3) vor data_health", r["rc"] == RC_SCAN_FILL)
        out = write_artifact(r, base)
        ok, back = _load_json(out)
        check("Artefakt geschrieben+lesbar", ok and back.get("verdict") == VERDICT_NO_TRADE)
        check("rc-Feld persistiert", back.get("rc") == RC_SCAN_FILL)

    print("SELFTEST %s: %d Failure(s)" % ("GRUEN" if not failures else "ROT", len(failures)))
    return 0 if not failures else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="V7.36 Pre-Adjudikations-Data-Quality-Gate (read-only)")
    ap.add_argument("--date", default=_dt.date.today().isoformat())
    ap.add_argument("--fill-floor", type=float, default=DEFAULT_FILL_FLOOR)
    ap.add_argument("--write", action="store_true", help="signals/adjudication_gate_<date>.json schreiben")
    ap.add_argument("--json", action="store_true", help="rohes JSON auf stdout")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        _dt.datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("Ungueltiges --date (YYYY-MM-DD): %s" % args.date, file=sys.stderr)
        return RC_TOOL_ERROR

    try:
        result = evaluate(args.date, fill_floor=args.fill_floor)
        if args.write:
            out = write_artifact(result)
            result["artifact"] = out
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_stdout(result))
        return int(result["rc"])
    except Exception as exc:  # noqa: BLE001, Tool-Error hart signalisieren, nie stumm 0
        print("TOOL_ERROR: %s: %s" % (exc.__class__.__name__, exc), file=sys.stderr)
        return RC_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
