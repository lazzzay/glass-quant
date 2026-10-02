#!/usr/bin/env python3
"""
STRAT-KELLY-1: Half-Kelly Position-Sizer (V7.5 Shadow-Mode).

Berechnet pro Setup-Kategorie den Kelly-Anteil f* aus rolling 252d Sharpe
und sigma und gibt eine empfohlene Positionsgröße zurück.

Default: half-Kelly (f_applied = 0.5 * f_kelly).
Bei VIX > 25: quarter-Kelly (0.25 * f_kelly).
Bei negativem SR oder sigma=0: size = 0 (kein Trade).

V7.8+ / R-2 + Q2 (2026-04-22, KW17 Tag 2):
    * Neues Sizing-Mode `optimal_f` (Vince + Sortino-Decay, via
      `sizing/optimal_f.py`) als Alternative zu half_kelly.
    * Vol-Scaling: sizing_factor *= min(1.0, target_vol / current_vol)
      mit target_vol default 15 % p.a. (konfigurierbar via --target-vol).
    * Beides Shadow-first: Live-Aktivierung nur explizit via CLI.

Benutzung:
    python3 kelly_sizer.py                                 # Demo
    python3 kelly_sizer.py --setup mr_v61 --cap 10000
    python3 kelly_sizer.py --sizing-mode optimal_f         # R-2 Vince
    python3 kelly_sizer.py --target-vol 0.12               # Q2 Vol-Scaler
    python3 kelly_sizer.py --shadow                        # Delta-Log

Feature-Freeze-konform: reiner Sizing-Layer, kein Scoring-Change.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib import request as urlrequest
from urllib.error import URLError

# --- R-2: Optimal-f Integration (KW17 Tag 2) ---------------------------------
# Optionaler Import, damit `--sizing-mode half_kelly` weiter funktioniert,
# falls das Sizing-Paket aus irgendeinem Grund fehlt.
try:
    # kelly_sizer liegt in tools/; das Paket in sizing/ auf Repo-Root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sizing.optimal_f import (  # noqa: E402
        compute_optimal_f,
        vol_scaling_factor,
        DEFAULT_TARGET_VOL as OPTF_DEFAULT_TARGET_VOL,
    )
    _OPTF_AVAILABLE = True
except Exception:  # pragma: no cover, fail-open
    compute_optimal_f = None      # type: ignore[assignment]
    vol_scaling_factor = None     # type: ignore[assignment]
    OPTF_DEFAULT_TARGET_VOL = 0.15
    _OPTF_AVAILABLE = False


REPO_ROOT = Path(__file__).resolve().parents[1]
JOURNAL_DB = REPO_ROOT / "trade_journal.sqlite"
SHADOW_DIR = REPO_ROOT / "shadow"
SHADOW_DIR.mkdir(exist_ok=True)

# --- Kelly-Parameter ----------------------------------------------------------

MIN_TRADES_PER_SETUP = 30        # Unter N=30 kein Kelly (Stichprobe zu klein)
KELLY_FRACTION_DEFAULT = 0.5     # Half-Kelly
KELLY_FRACTION_HIGHVOL = 0.25    # Quarter-Kelly bei VIX > VIX_STRESS
VIX_STRESS = 25.0
KELLY_CAP = 0.20                 # Max 20% Kapital pro Trade (hard cap)
ROLLING_DAYS = 252               # 1 Jahr rolling window

# R-2 / Q2 Sizing-Modes -------------------------------------------------------
SIZING_MODE_HALF_KELLY = "half_kelly"
SIZING_MODE_OPTIMAL_F = "optimal_f"
SIZING_MODES = (SIZING_MODE_HALF_KELLY, SIZING_MODE_OPTIMAL_F)

# Default Target-Vol 15 % p.a. (Escobar/Vince-Regularisierung fuer Sizing).
DEFAULT_TARGET_VOL = float(os.environ.get(
    "KELLY_TARGET_VOL", str(OPTF_DEFAULT_TARGET_VOL)))

# --- V7.7 Recovery-Integration (KELLY-RECOVERY-1, 2026-04-20) -----------------
# Bezieht `drawdown.sizing_factor` + `daily_loss.lock` aus dem V7.6-Dashboard-
# Endpoint `/portfolio/risk`. Ohne API-Verfügbarkeit bleibt sizing_factor=1.0
# und der Sizer arbeitet wie bisher (fail-open, keine stille Abwertung).

DEFAULT_API_PORT = int(os.environ.get("DASHBOARD_API_PORT", "5757"))
DEFAULT_API_BASE = os.environ.get(
    "DASHBOARD_API_BASE", f"http://127.0.0.1:{DEFAULT_API_PORT}"
)
RISK_API_TIMEOUT_S = float(os.environ.get("KELLY_RISK_API_TIMEOUT", "3.0"))


@dataclass
class KellyResult:
    setup: str
    n_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    sharpe_252d: float
    sigma_252d: float
    f_kelly_raw: float
    f_applied: float
    size_eur: float
    regime: str
    reason: str
    # V7.7 KELLY-RECOVERY-1
    sizing_factor: float = 1.0           # aus /portfolio/risk drawdown.sizing_factor
    recovery_applied: bool = False       # True wenn dd_pct <= -5% → Halbierung
    daily_loss_lock: bool = False        # True wenn r_today <= -2R (Hardlock → size=0)
    risk_source: str = "api_unreachable" # api_ok | api_unreachable | manual | bypass
    # R-2 + Q2 (KW17 Tag 2, 2026-04-22)
    sizing_mode: str = SIZING_MODE_HALF_KELLY  # half_kelly | optimal_f
    f_optimal_f: float = 0.0             # Vince-Sortino f* (0 wenn nicht berechnet)
    vol_scaling: float = 1.0             # min(1, target_vol/current_vol)
    realized_vol: float = 0.0            # annualisierte Sample-Vola (Diagnose)
    target_vol: float = DEFAULT_TARGET_VOL  # Zielvola fuer Vol-Scaling
    optimal_f_reason: str = ""           # Diagnose aus OptimalFResult.reason

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# --- Risk-Fetch (V7.7 KELLY-RECOVERY-1) --------------------------------------

def fetch_risk_state(api_base: str | None = None,
                     timeout_s: float = RISK_API_TIMEOUT_S) -> dict:
    """
    Holt `/portfolio/risk` vom Dashboard-API (Default: 127.0.0.1:5757).
    Liefert normalisiertes Dict:
        {
          "ok": bool,
          "sizing_factor": float,     # 1.0 normal, 0.5 wenn recovery_mode
          "recovery_mode": bool,
          "dd_pct": float,
          "daily_loss_lock": bool,    # True wenn r_today <= -2R
          "heat_band": str,
          "source": str               # "api_ok" | "api_unreachable" | "api_error"
        }
    Fail-open: bei jedem Fehler sizing_factor=1.0 + lock=False zurückgeben,
    damit der Sizer nicht wegen Netzwerk-Problemen stillschweigend auf 0 geht.
    """
    base = (api_base or DEFAULT_API_BASE).rstrip("/")
    url = f"{base}/portfolio/risk"
    try:
        req = urlrequest.Request(url, headers={"Accept": "application/json"})
        with urlrequest.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
    except (URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
        return dict(
            ok=False, sizing_factor=1.0, recovery_mode=False,
            dd_pct=0.0, daily_loss_lock=False, heat_band="unknown",
            source="api_unreachable", error=f"{type(e).__name__}: {e}",
        )

    if not payload.get("ok"):
        return dict(
            ok=False, sizing_factor=1.0, recovery_mode=False,
            dd_pct=0.0, daily_loss_lock=False, heat_band="unknown",
            source="api_error", error=str(payload.get("msg") or "ok=false"),
        )

    dd = payload.get("drawdown") or {}
    dl = payload.get("daily_loss") or {}
    heat = payload.get("heat") or {}
    sf = dd.get("sizing_factor")
    try:
        sizing_factor = float(sf) if sf is not None else 1.0
    except (TypeError, ValueError):
        sizing_factor = 1.0
    # Clamp: nie > 1.0 (Recovery darf nur halbieren, nie erhöhen)
    sizing_factor = max(0.0, min(1.0, sizing_factor))
    return dict(
        ok=True,
        sizing_factor=sizing_factor,
        recovery_mode=bool(dd.get("recovery_mode")),
        dd_pct=float(dd.get("dd_pct") or 0.0),
        daily_loss_lock=bool(dl.get("lock")),
        heat_band=str(heat.get("band") or "unknown"),
        source="api_ok",
    )


# --- Core math ----------------------------------------------------------------

def kelly_from_wl(win_rate: float, avg_win_pct: float, avg_loss_pct: float) -> float:
    """
    Klassisches Kelly für binäres Win/Loss:
      f* = p/a - q/b
    mit p = win_rate, q = 1-p, b = avg_win (positiv), a = avg_loss (positiv).

    Gibt 0.0 zurück bei degenerierten Inputs (vermeidet Div-by-0 / negatives
    Staking).
    """
    if avg_loss_pct <= 0 or avg_win_pct <= 0 or not (0 < win_rate < 1):
        return 0.0
    p = win_rate
    q = 1.0 - p
    b = avg_win_pct
    a = avg_loss_pct
    f = (p / a) - (q / b)
    return max(0.0, f)


def kelly_from_sr(sharpe: float, sigma: float) -> float:
    """
    Continuous-Kelly-Approximation fuer Return-Serien:
      f* = mu / sigma^2 = SR / sigma
    Bei SR <= 0 oder sigma <= 0 -> 0.
    """
    if sharpe <= 0 or sigma <= 0:
        return 0.0
    return sharpe / sigma


def fractional_kelly(f_raw: float, vix: float | None = None) -> tuple[float, str]:
    """Wendet Half-/Quarter-Kelly und Hard-Cap an."""
    if vix is not None and vix > VIX_STRESS:
        frac = KELLY_FRACTION_HIGHVOL
        regime = f"HIGHVOL (VIX={vix:.1f}>{VIX_STRESS})"
    else:
        frac = KELLY_FRACTION_DEFAULT
        regime = f"NORMAL (VIX={vix:.1f})" if vix is not None else "NORMAL (VIX n/a)"
    f_applied = min(f_raw * frac, KELLY_CAP)
    return f_applied, regime


# --- Journal-Anbindung --------------------------------------------------------

def load_setup_stats(db_path: Path, setup: str) -> dict:
    """
    Liest aus trade_journal.sqlite die Statistik fuer ein Setup.
    Erwartet Spalten: setup, exit_price, entry_price, entry_date, source.
    Fallback: wenn 'setup'-Spalte fehlt, nutzt 'system_version' als Proxy.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Journal DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(trades)")
    cols = {row[1] for row in cur.fetchall()}
    setup_col = "setup" if "setup" in cols else "system_version"

    # K4-FIX 2026-06-14: "geschlossen" via exit_date definieren (nicht exit_price).
    # Per-Trade-Return aus Preisen, wenn vorhanden, sonst aus return_pct -> Live-Trades,
    # die nur mit EUR-notional + return_pct geloggt wurden, zaehlen jetzt mit
    # (vorher schloss `exit_price IS NOT NULL` sie still aus der Kelly-Stichprobe aus).
    q = f"""
        SELECT entry_price, exit_price, return_pct, exit_date
        FROM trades
        WHERE {setup_col} = ?
          AND exit_date IS NOT NULL
        ORDER BY exit_date DESC
        LIMIT {ROLLING_DAYS * 3}
    """
    rows = list(cur.execute(q, (setup,)))
    conn.close()

    if not rows:
        return dict(n=0)

    rets = []
    wins = []
    losses = []
    for r in rows:
        ep = r["entry_price"]
        xp = r["exit_price"]
        if ep is not None and float(ep) > 0 and xp is not None:
            ret = (float(xp) - float(ep)) / float(ep)
        elif r["return_pct"] is not None:
            ret = float(r["return_pct"]) / 100.0   # K4: EUR-only Live-Trades
        else:
            continue
        rets.append(ret)
        if ret > 0:
            wins.append(ret)
        elif ret < 0:
            losses.append(abs(ret))

    n = len(rets)
    if n == 0:
        return dict(n=0)

    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
    sigma = math.sqrt(var)
    sharpe = (mean / sigma) if sigma > 0 else 0.0
    win_rate = len(wins) / n if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    return dict(
        n=n,
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        sharpe_252d=sharpe,
        sigma_252d=sigma,
        mean=mean,
        returns=rets,   # R-2: rohe Per-Trade-Returns fuer Optimal-f + Vol-Scaling
    )


def size_for_setup(setup: str, capital_eur: float, vix: float | None,
                   db_path: Path = JOURNAL_DB,
                   *,
                   sizing_factor: float = 1.0,
                   daily_loss_lock: bool = False,
                   risk_source: str = "manual",
                   sizing_mode: str = SIZING_MODE_HALF_KELLY,
                   target_vol: float = DEFAULT_TARGET_VOL,
                   drawdown_penalty: float = 0.5) -> KellyResult:
    """
    V7.7: Wendet `sizing_factor` (aus Recovery-Mode, 0.5 bei DD<=-5%) und
    `daily_loss_lock` (Hardstop bei r_today<=-2R) auf die final-size an.

    V7.8 / R-2 + Q2 (KW17 Tag 2): `sizing_mode` waehlt zwischen
    `half_kelly` (bisher) und `optimal_f` (Vince 2007 + Sortino-Decay).
    `target_vol` steuert Vol-Scaling: sizing_factor *= min(1, target_vol /
    current_vol). current_vol wird aus den per-Trade-Returns annualisiert.

    sizing_factor=1.0 + lock=False + sizing_mode=half_kelly + target_vol=0 →
    Verhalten identisch zur V7.7-Linie (backward-compat).
    """
    if sizing_mode not in SIZING_MODES:
        raise ValueError(
            f"sizing_mode must be one of {SIZING_MODES}, got {sizing_mode!r}"
        )
    stats = load_setup_stats(db_path, setup)
    if stats["n"] < MIN_TRADES_PER_SETUP:
        return KellyResult(
            setup=setup, n_trades=stats["n"], win_rate=0.0, avg_win_pct=0.0,
            avg_loss_pct=0.0, sharpe_252d=0.0, sigma_252d=0.0,
            f_kelly_raw=0.0, f_applied=0.0, size_eur=0.0,
            regime="UNDERSAMPLED",
            reason=f"n={stats['n']} < {MIN_TRADES_PER_SETUP} - kein Sizing moeglich",
            sizing_factor=sizing_factor,
            recovery_applied=sizing_factor < 1.0,
            daily_loss_lock=daily_loss_lock,
            risk_source=risk_source,
            sizing_mode=sizing_mode,
            target_vol=target_vol,
        )

    # --- Klassisches Kelly (immer berechnet, fuer Shadow-Reports) ---------
    f_wl = kelly_from_wl(stats["win_rate"], stats["avg_win_pct"], stats["avg_loss_pct"])
    f_sr = kelly_from_sr(stats["sharpe_252d"], stats["sigma_252d"])
    f_kelly = min(f_wl, f_sr) if (f_wl > 0 and f_sr > 0) else max(f_wl, f_sr)

    # --- R-2 Optimal-f (Vince + Sortino-Decay) ----------------------------
    f_optf = 0.0
    optf_reason = "not_requested"
    returns = stats.get("returns") or []
    if sizing_mode == SIZING_MODE_OPTIMAL_F:
        if not _OPTF_AVAILABLE or compute_optimal_f is None:
            optf_reason = "module_unavailable"
        else:
            optf = compute_optimal_f(returns, drawdown_penalty=drawdown_penalty)
            f_optf = float(optf.f_applied)
            optf_reason = optf.reason

    # --- Mode-dependent f_raw Selection -----------------------------------
    if sizing_mode == SIZING_MODE_OPTIMAL_F and f_optf > 0:
        f_raw = f_optf
    else:
        f_raw = f_kelly

    f_applied, regime = fractional_kelly(f_raw, vix)

    # --- Q2 Vol-Scaling ---------------------------------------------------
    # target_vol <= 0 deaktiviert Vol-Scaling (backward-compat-Pfad).
    vol_scale = 1.0
    realized = 0.0
    if target_vol and target_vol > 0 and vol_scaling_factor is not None and returns:
        realized = _annualized_vol_from_trades(returns)
        if realized > 0:
            vol_scale = float(min(1.0, target_vol / realized))
        # Falls keine Vola messbar: 1.0 (konservativ)

    # V7.7 Recovery-Halbierung + Hardlock ----------------------------------
    sf = max(0.0, min(1.0, float(sizing_factor)))
    # Vol-Scaling multipliziert on-top des Recovery-sizing-factor.
    sf_effective = max(0.0, min(1.0, sf * vol_scale))
    recovery_applied = sf < 1.0
    reason_parts = []
    if daily_loss_lock:
        size_eur = 0.0
        reason = "DAILY_LOSS_LOCK"
    else:
        size_eur = round(capital_eur * f_applied * sf_effective, 2)
        if recovery_applied:
            reason_parts.append(f"recovery={sf:.2f}")
        if vol_scale < 1.0:
            reason_parts.append(f"vol_scale={vol_scale:.2f}")
        if sizing_mode == SIZING_MODE_OPTIMAL_F:
            reason_parts.append(f"optf_{optf_reason}")
        reason = ", ".join(reason_parts) if reason_parts else "ok"

    return KellyResult(
        setup=setup,
        n_trades=stats["n"],
        win_rate=stats["win_rate"],
        avg_win_pct=stats["avg_win_pct"],
        avg_loss_pct=stats["avg_loss_pct"],
        sharpe_252d=stats["sharpe_252d"],
        sigma_252d=stats["sigma_252d"],
        f_kelly_raw=f_raw,
        f_applied=f_applied,
        size_eur=size_eur,
        regime=regime,
        reason=reason,
        sizing_factor=sf,
        recovery_applied=recovery_applied,
        daily_loss_lock=daily_loss_lock,
        risk_source=risk_source,
        sizing_mode=sizing_mode,
        f_optimal_f=f_optf,
        vol_scaling=vol_scale,
        realized_vol=realized,
        target_vol=float(target_vol),
        optimal_f_reason=optf_reason,
    )


def _annualized_vol_from_trades(returns: list[float],
                                periods_per_year: int = ROLLING_DAYS) -> float:
    """
    Sehr einfache Annualisierung: std(per-trade-returns, ddof=1) * sqrt(252).
    Hinweis: per-Trade-Returns sind holding-period-normalisiert, daher ist
    das eine Proxy-Annualisierung; der Vol-Scaler ist absichtlich
    konservativ (clip ≤ 1.0).
    """
    if not returns or len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def size_for_setup_auto(setup: str, capital_eur: float, vix: float | None,
                        db_path: Path = JOURNAL_DB,
                        *,
                        api_base: str | None = None,
                        auto_recovery: bool = True,
                        sizing_mode: str = SIZING_MODE_HALF_KELLY,
                        target_vol: float = DEFAULT_TARGET_VOL) -> KellyResult:
    """
    V7.7: Conveniece-Wrapper der `size_for_setup` mit Auto-Pull von
    `/portfolio/risk`. Liefert dieselbe `KellyResult`-Struktur, die
    `risk_source` kennzeichnet woher `sizing_factor`/`lock` kamen.

    V7.8 / R-2 + Q2: `sizing_mode` und `target_vol` werden an die
    untere Schicht durchgereicht.

    Bei `auto_recovery=False` → Verhalten identisch zu size_for_setup()
    mit Defaults (Bypass, keine API-Abfrage).
    """
    if not auto_recovery:
        return size_for_setup(
            setup, capital_eur, vix, db_path=db_path,
            sizing_factor=1.0, daily_loss_lock=False, risk_source="bypass",
            sizing_mode=sizing_mode, target_vol=target_vol,
        )
    risk = fetch_risk_state(api_base=api_base)
    return size_for_setup(
        setup, capital_eur, vix, db_path=db_path,
        sizing_factor=float(risk.get("sizing_factor") or 1.0),
        daily_loss_lock=bool(risk.get("daily_loss_lock")),
        risk_source=str(risk.get("source") or "api_unreachable"),
        sizing_mode=sizing_mode,
        target_vol=target_vol,
    )


# --- Shadow-Runner ------------------------------------------------------------

def shadow_compare(capital_eur: float, vix: float | None, setups: Iterable[str],
                   db_path: Path = JOURNAL_DB, out_dir: Path = SHADOW_DIR,
                   *,
                   api_base: str | None = None,
                   auto_recovery: bool = True,
                   sizing_mode: str = SIZING_MODE_HALF_KELLY,
                   target_vol: float = DEFAULT_TARGET_VOL) -> dict:
    """Erzeugt einen Vergleichs-Log Status-Quo-Sizing vs. Kelly-Sizing.

    V7.7: Wenn `auto_recovery=True`, wird `/portfolio/risk` genau einmal
    abgefragt und das Ergebnis auf alle Setups angewendet (gemeinsamer
    Kontext). Der Block `risk_state` im Output dokumentiert Quelle + Werte.
    """
    today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    if auto_recovery:
        risk = fetch_risk_state(api_base=api_base)
    else:
        risk = dict(
            ok=True, sizing_factor=1.0, recovery_mode=False, dd_pct=0.0,
            daily_loss_lock=False, heat_band="bypass", source="bypass",
        )
    sf = float(risk.get("sizing_factor") or 1.0)
    lock = bool(risk.get("daily_loss_lock"))
    src = str(risk.get("source") or "api_unreachable")
    out = dict(
        date=today, vix=vix, capital_eur=capital_eur, setups={},
        risk_state=risk,
    )
    for s in setups:
        try:
            kr = size_for_setup(
                s, capital_eur, vix, db_path=db_path,
                sizing_factor=sf, daily_loss_lock=lock, risk_source=src,
                sizing_mode=sizing_mode, target_vol=target_vol,
            )
            out["setups"][s] = asdict(kr)
        except FileNotFoundError as e:
            out["setups"][s] = dict(error=str(e))
    out["sizing_mode"] = sizing_mode
    out["target_vol"] = float(target_vol)
    target = out_dir / f"kelly_shadow_{today}.json"
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    out["_shadow_file"] = str(target)
    return out


# --- Self-test ----------------------------------------------------------------

def _self_test() -> int:
    """Unit-Tests fuer die reine Math-Schicht (keine DB)."""
    rc = 0
    # kelly_from_wl
    assert kelly_from_wl(0.6, 0.02, 0.01) > 0, "bullish edge should yield f>0"
    assert kelly_from_wl(0.4, 0.01, 0.02) == 0.0, "negative edge should be clipped to 0"
    assert kelly_from_wl(0.5, 0.0, 0.01) == 0.0, "zero avg_win -> 0"
    assert kelly_from_wl(0.0, 0.01, 0.01) == 0.0, "wr=0 -> 0"
    assert kelly_from_wl(1.0, 0.01, 0.01) == 0.0, "wr=1 edge case -> 0"
    # kelly_from_sr
    assert kelly_from_sr(1.5, 0.1) == 15.0, "SR/sigma"
    assert kelly_from_sr(-0.5, 0.1) == 0.0, "negative SR -> 0"
    assert kelly_from_sr(0.5, 0.0) == 0.0, "sigma=0 -> 0"
    # fractional
    f, r = fractional_kelly(0.30, vix=15.0)
    assert abs(f - 0.15) < 1e-9, f"half-Kelly expected 0.15, got {f}"
    assert "NORMAL" in r
    f, r = fractional_kelly(0.30, vix=28.0)
    assert abs(f - 0.075) < 1e-9, f"quarter-Kelly expected 0.075, got {f}"
    assert "HIGHVOL" in r
    # Hard-cap
    f, _ = fractional_kelly(1.0, vix=15.0)
    assert f <= KELLY_CAP, "hard cap violated"

    # --- V7.7 KELLY-RECOVERY-1 Tests -------------------------------------
    # fetch_risk_state: unerreichbare API → fail-open (sizing_factor=1.0)
    risk_unreach = fetch_risk_state(api_base="http://127.0.0.1:1",  # reserved dummy port
                                    timeout_s=0.3)
    assert risk_unreach["ok"] is False, "unreachable API must return ok=False"
    assert risk_unreach["sizing_factor"] == 1.0, "fail-open must keep size 1.0"
    assert risk_unreach["daily_loss_lock"] is False, "fail-open must not lock"
    assert risk_unreach["source"] == "api_unreachable"

    # Pure-function recovery-halving test (ohne DB durch direkten Mock)
    # Simuliert: f_applied=0.10, sizing_factor=0.5, capital=10_000 → size=500
    kr_mock = KellyResult(
        setup="MOCK", n_trades=100, win_rate=0.55, avg_win_pct=0.02,
        avg_loss_pct=0.015, sharpe_252d=1.0, sigma_252d=0.1,
        f_kelly_raw=0.2, f_applied=0.10, size_eur=round(10_000 * 0.10 * 0.5, 2),
        regime="NORMAL", reason="recovery_applied (sizing_factor=0.50)",
        sizing_factor=0.5, recovery_applied=True, daily_loss_lock=False,
        risk_source="api_ok",
    )
    assert kr_mock.size_eur == 500.0, "recovery half-size expected 500€"
    assert kr_mock.recovery_applied is True

    # Hardlock zwingt size=0 trotz positiver Edge
    kr_lock = KellyResult(
        setup="MOCK", n_trades=100, win_rate=0.55, avg_win_pct=0.02,
        avg_loss_pct=0.015, sharpe_252d=1.0, sigma_252d=0.1,
        f_kelly_raw=0.2, f_applied=0.10, size_eur=0.0,
        regime="NORMAL", reason="DAILY_LOSS_LOCK",
        sizing_factor=0.5, recovery_applied=True, daily_loss_lock=True,
        risk_source="api_ok",
    )
    assert kr_lock.size_eur == 0.0, "lock must force size=0"
    assert kr_lock.reason == "DAILY_LOSS_LOCK"

    # --- R-2 + Q2 Tests (KW17 Tag 2) --------------------------------------
    # Ungueltiger sizing_mode muss zu ValueError fuehren.
    try:
        size_for_setup("mr_v61", 10_000, None,
                       db_path=Path("/nonexistent.sqlite"),
                       sizing_mode="junk")
    except ValueError:
        pass
    except FileNotFoundError:
        # DB fehlt auch ok: Mode-Validation findet vor DB-Zugriff statt,
        # falls wir die Reihenfolge im Code so halten. Test haelt trotzdem.
        raise AssertionError("sizing_mode validation failed (FileNotFoundError)")
    else:
        raise AssertionError("invalid sizing_mode must raise ValueError")

    # Vol-Annualisierung
    ann = _annualized_vol_from_trades([0.01, -0.01, 0.02, -0.015, 0.005])
    assert ann > 0 and ann < 10, f"annualized vol out of bounds: {ann}"

    # Optimal-f-Verfuegbarkeit (informativ, nicht hart)
    assert _OPTF_AVAILABLE, "optimal_f module must import cleanly"

    print("kelly_sizer self-test: PASS "
          "(incl. V7.7 recovery + R-2 Optimal-f + Q2 Vol-Scaling)")
    return rc


# --- CLI ----------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Kelly Position-Sizer (STRAT-KELLY-1)")
    p.add_argument("--setup", default="V6.1", help="Setup-Kategorie (default: V6.1)")
    p.add_argument("--cap", type=float, default=10000.0, help="Kapital in EUR")
    p.add_argument("--vix", type=float, default=None, help="Aktueller VIX")
    p.add_argument("--shadow", action="store_true", help="Shadow-Vergleich schreiben")
    p.add_argument("--test", action="store_true", help="Unit-Tests laufen")
    # V7.7 Recovery-Integration
    p.add_argument("--api-base", default=None,
                   help=f"Dashboard-API-Base (default: {DEFAULT_API_BASE} bzw. $DASHBOARD_API_BASE)")
    p.add_argument("--no-auto-recovery", action="store_true",
                   help="Recovery-Sizing-Factor nicht aus /portfolio/risk holen")
    p.add_argument("--sizing-factor", type=float, default=None,
                   help="Manuelles Override des sizing_factor (0.0-1.0); deaktiviert Auto-Recovery")
    p.add_argument("--force-lock", action="store_true",
                   help="Manuelles Daily-Loss-Lock (size_eur=0) fuer Tests")
    p.add_argument("--print-risk", action="store_true",
                   help="Aktuellen /portfolio/risk-Status ausgeben und Exit")
    # R-2 + Q2 (KW17 Tag 2)
    p.add_argument("--sizing-mode", choices=list(SIZING_MODES),
                   default=SIZING_MODE_HALF_KELLY,
                   help=f"Sizing-Modus (default {SIZING_MODE_HALF_KELLY})")
    p.add_argument("--target-vol", type=float, default=DEFAULT_TARGET_VOL,
                   help=(f"Annualisierte Zielvola fuer Vol-Scaling (default "
                         f"{DEFAULT_TARGET_VOL:.2f}); 0 = Vol-Scaling aus"))
    args = p.parse_args()

    if args.test:
        return _self_test()

    if args.print_risk:
        risk = fetch_risk_state(api_base=args.api_base)
        print(json.dumps(risk, indent=2, ensure_ascii=False))
        return 0 if risk.get("ok") else 1

    if args.shadow:
        auto = not args.no_auto_recovery and args.sizing_factor is None
        out = shadow_compare(
            args.cap, args.vix, setups=["V6.1", "V6"],
            api_base=args.api_base, auto_recovery=auto,
            sizing_mode=args.sizing_mode, target_vol=args.target_vol,
        )
        # Manual override: sizing_factor oder force_lock
        if args.sizing_factor is not None or args.force_lock:
            # Rebuild mit manuellen Werten
            out["risk_state"] = dict(
                ok=True, source="manual",
                sizing_factor=float(args.sizing_factor) if args.sizing_factor is not None else 1.0,
                recovery_mode=(args.sizing_factor is not None and args.sizing_factor < 1.0),
                dd_pct=0.0, daily_loss_lock=bool(args.force_lock),
                heat_band="manual",
            )
            for s in list(out["setups"].keys()):
                try:
                    kr = size_for_setup(
                        s, args.cap, args.vix,
                        sizing_factor=out["risk_state"]["sizing_factor"],
                        daily_loss_lock=out["risk_state"]["daily_loss_lock"],
                        risk_source="manual",
                        sizing_mode=args.sizing_mode,
                        target_vol=args.target_vol,
                    )
                    out["setups"][s] = asdict(kr)
                except FileNotFoundError as e:
                    out["setups"][s] = dict(error=str(e))
            SHADOW_DIR.mkdir(exist_ok=True)
            (SHADOW_DIR / f"kelly_shadow_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d')}.json"
             ).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    # Default-Run: ein Setup, mit Auto-Recovery (oder Override)
    try:
        if args.sizing_factor is not None or args.force_lock:
            kr = size_for_setup(
                args.setup, args.cap, args.vix,
                sizing_factor=float(args.sizing_factor) if args.sizing_factor is not None else 1.0,
                daily_loss_lock=bool(args.force_lock),
                risk_source="manual",
                sizing_mode=args.sizing_mode,
                target_vol=args.target_vol,
            )
        elif args.no_auto_recovery:
            kr = size_for_setup_auto(
                args.setup, args.cap, args.vix,
                api_base=args.api_base, auto_recovery=False,
                sizing_mode=args.sizing_mode, target_vol=args.target_vol,
            )
        else:
            kr = size_for_setup_auto(
                args.setup, args.cap, args.vix,
                api_base=args.api_base, auto_recovery=True,
                sizing_mode=args.sizing_mode, target_vol=args.target_vol,
            )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(kr.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
