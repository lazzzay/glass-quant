#!/usr/bin/env python3
"""
sim/risk.py - SIM-5: Value-at-Risk + Conditional VaR (Rolling 252d)

Teil des sim-Stacks (Welle B) im Validation & Simulation Framework.
Ergaenzt sim/bootstrap.py (SIM-1), sim/monte_carlo.py (SIM-2), sim/costs.py (SIM-3).

Methoden:
  - Historischer VaR/CVaR ueber Quantile
  - Parametrischer VaR/CVaR unter Student-t Verteilung (Fat-Tails)
  - Einfacher Monte-Carlo VaR (fuer Referenz)

Datenquellen (Fallback-Kette):
  1. dashboard/v7/trades_snapshot.json (equity_series aus trade_journal)
  2. yfinance SPY-Proxy (Reference-Portfolio) wenn equity_history < MIN_SAMPLES

Output: sim/_reports/risk_YYYY-MM-DD.json

Policy-konform:
  - Nur Free-Stack (numpy + scipy + yfinance optional)
  - Kein Paid-API
  - Liest/Schreibt ausschliesslich lokal
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

try:
    from scipy import stats as scistats  # type: ignore
    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    scistats = None  # type: ignore
    _HAS_SCIPY = False

try:
    import yfinance as yf  # type: ignore
    _HAS_YF = True
except ImportError:  # pragma: no cover
    yf = None  # type: ignore
    _HAS_YF = False


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_SNAPSHOT = ROOT / "dashboard" / "v7" / "trades_snapshot.json"
REPORTS_DIR = HERE / "_reports"
VERSION = "sim-risk/1.0"
WINDOW_DAYS = 252
MIN_SAMPLES = 30          # unter diesem Wert -> Fallback auf Reference-Portfolio
CONFIDENCE_LEVELS = (0.95, 0.99)


# ---------- Datenlader ----------


def load_returns_from_snapshot(path: Path) -> tuple[np.ndarray, list[str]]:
    """Liest equity_series aus trades_snapshot.json und gibt tägliche Returns zurück.

    Returns:
        (returns_array, dates_list_without_first)
    """
    if not path.exists():
        return np.array([]), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return np.array([]), []
    series = data.get("equity_series") or []
    if len(series) < 2:
        return np.array([]), []
    eq = np.array([float(x.get("equity", 0.0)) for x in series], dtype=float)
    dates = [str(x.get("date", "")) for x in series]
    if np.any(eq <= 0):
        return np.array([]), []
    rets = np.diff(eq) / eq[:-1]
    return rets, dates[1:]


def load_returns_yfinance(ticker: str = "SPY", days: int = WINDOW_DAYS) -> tuple[np.ndarray, list[str]]:
    """Fallback: laedt SPY (oder anderer Ticker) als Reference-Portfolio ueber yfinance."""
    if not _HAS_YF:
        return np.array([]), []
    try:
        # period='2y' um Puffer fuer 252 Handelstage zu haben
        hist = yf.Ticker(ticker).history(period="2y", auto_adjust=True)
    except Exception:  # pragma: no cover
        return np.array([]), []
    if hist is None or hist.empty or "Close" not in hist.columns:
        return np.array([]), []
    closes = hist["Close"].dropna().astype(float).to_numpy()
    dates = [d.strftime("%Y-%m-%d") for d in hist.index[-len(closes):]]
    if len(closes) < 2:
        return np.array([]), []
    rets = np.diff(closes) / closes[:-1]
    # Rolling-Fenster schneiden
    if len(rets) > days:
        rets = rets[-days:]
        dates = dates[-len(rets):]
    else:
        dates = dates[1:]
    return rets, dates


# ---------- Kern-Methoden ----------


def historical_var_cvar(returns: np.ndarray, confidence: float) -> tuple[float, float]:
    """Historischer VaR + CVaR bei gegebenem Konfidenzniveau.

    VaR als negatives Quantil (positiver Verlust, z. B. 0.02 = 2% Tages-Verlust-Schwelle).
    CVaR = mittlerer Verlust im Tail jenseits des VaR-Quantils.
    """
    if returns.size == 0:
        return (float("nan"), float("nan"))
    q = 1.0 - confidence
    # numpy-Quantil auf Returns; typischerweise negativ -> *-1 = Verlust-Maß
    var_quantile = float(np.quantile(returns, q))
    var_loss = -var_quantile
    tail = returns[returns <= var_quantile]
    cvar_loss = -float(np.mean(tail)) if tail.size > 0 else var_loss
    return var_loss, cvar_loss


def parametric_student_t_var_cvar(returns: np.ndarray, confidence: float) -> tuple[float, float, float]:
    """Parametrischer VaR/CVaR unter Student-t.

    Returns:
        (var_loss, cvar_loss, degrees_of_freedom)
    """
    if returns.size < 10 or not _HAS_SCIPY:
        return (float("nan"), float("nan"), float("nan"))
    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    # Fit t-distribution
    try:
        df, loc, scale = scistats.t.fit(returns)
    except Exception:  # pragma: no cover
        return (float("nan"), float("nan"), float("nan"))
    q = 1.0 - confidence
    t_quantile = scistats.t.ppf(q, df, loc=loc, scale=scale)
    var_loss = -float(t_quantile)
    # CVaR Analytisch fuer Student-t (Rockafellar-Uryasev / McNeil-Frey)
    # ES_alpha = -[loc + scale * (df + t_q^2)/(df - 1) * pdf_t(t_q) / (1 - alpha)]
    # wobei t_q standardisiertes Quantil (nicht location-scale)
    try:
        t_q_std = scistats.t.ppf(q, df)
        pdf_val = scistats.t.pdf(t_q_std, df)
        es_std = -((df + t_q_std**2) / (df - 1.0)) * pdf_val / q
        cvar_loss = -(loc + scale * es_std)
    except Exception:
        cvar_loss = float("nan")
    return var_loss, cvar_loss, float(df)


def monte_carlo_var(returns: np.ndarray, confidence: float, n_sims: int = 20000, seed: int = 42) -> float:
    """Einfacher parametrischer Monte-Carlo VaR (Normal-Fit)."""
    if returns.size < 10:
        return float("nan")
    rng = np.random.default_rng(seed)
    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    sims = rng.normal(mu, sigma, size=n_sims)
    q = 1.0 - confidence
    return -float(np.quantile(sims, q))


# ---------- Report ----------


def _stats(returns: np.ndarray) -> dict[str, float]:
    if returns.size == 0:
        return {}
    out = {
        "samples": int(returns.size),
        "mean_daily_return_pct": float(np.mean(returns) * 100),
        "stdev_daily_return_pct": float(np.std(returns, ddof=1) * 100),
        "min_daily_return_pct": float(np.min(returns) * 100),
        "max_daily_return_pct": float(np.max(returns) * 100),
    }
    if _HAS_SCIPY and returns.size >= 3:
        out["skew"] = float(scistats.skew(returns))
        out["excess_kurtosis"] = float(scistats.kurtosis(returns))
    return out


def build_report(
    returns: np.ndarray,
    source: str,
    source_detail: str,
    dates: list[str],
    today_return: float | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    rep: dict[str, Any] = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": VERSION,
        "source": source,
        "source_detail": source_detail,
        "window_days_target": WINDOW_DAYS,
        "window_days_actual": int(returns.size),
        "min_samples_threshold": MIN_SAMPLES,
        "stats": _stats(returns),
        "var": {},
        "cvar": {},
        "parametric_student_t": {},
        "monte_carlo": {},
        "breach_check": {},
        "notes": [],
    }
    if returns.size == 0:
        rep["notes"].append("Keine Daten verfuegbar - VaR/CVaR nicht berechenbar.")
        return rep

    for conf in CONFIDENCE_LEVELS:
        hv, hc = historical_var_cvar(returns, conf)
        rep["var"][f"historical_{int(conf*100)}_pct"] = round(hv * 100, 4)
        rep["cvar"][f"historical_{int(conf*100)}_pct"] = round(hc * 100, 4)
        pv, pc, df = parametric_student_t_var_cvar(returns, conf)
        rep["parametric_student_t"][f"var_{int(conf*100)}_pct"] = None if math.isnan(pv) else round(pv * 100, 4)
        rep["parametric_student_t"][f"cvar_{int(conf*100)}_pct"] = None if math.isnan(pc) else round(pc * 100, 4)
        rep["parametric_student_t"][f"df_{int(conf*100)}"] = None if math.isnan(df) else round(df, 3)
        mc = monte_carlo_var(returns, conf)
        rep["monte_carlo"][f"var_{int(conf*100)}_pct"] = None if math.isnan(mc) else round(mc * 100, 4)

    # Breach-Check
    today_ret = today_return if today_return is not None else (float(returns[-1]) if returns.size else None)
    if today_ret is not None:
        thr95 = rep["var"].get("historical_95_pct")
        thr99 = rep["var"].get("historical_99_pct")
        rep["breach_check"] = {
            "today_return_pct": round(today_ret * 100, 4),
            "threshold_var_95_pct": thr95,
            "threshold_var_99_pct": thr99,
            "breach_95": bool(thr95 is not None and (today_ret * 100) <= -thr95),
            "breach_99": bool(thr99 is not None and (today_ret * 100) <= -thr99),
        }

    # Notes / Warnungen
    if returns.size < MIN_SAMPLES:
        rep["notes"].append(
            f"Sample-Size {returns.size} < MIN_SAMPLES {MIN_SAMPLES}: VaR unsicher, Reference-Portfolio empfohlen."
        )
    if returns.size < WINDOW_DAYS:
        rep["notes"].append(
            f"Weniger als Zielfenster {WINDOW_DAYS} Tage - rollendes Fenster wird linear anwachsen."
        )
    if not _HAS_SCIPY:
        rep["notes"].append("scipy nicht installiert - parametrische Student-t-VaR-Berechnung uebersprungen.")
    if _HAS_SCIPY and returns.size >= 3:
        sk = rep["stats"].get("skew", 0.0)
        ek = rep["stats"].get("excess_kurtosis", 0.0)
        if abs(sk) > 1.0 or ek > 3.0:
            rep["notes"].append(
                f"Tail-Asymmetrie erkannt (skew={sk:.2f}, excess_kurt={ek:.2f}) - Student-t-VaR vorziehen."
            )
    if dates:
        rep["date_range"] = {"first": dates[0], "last": dates[-1]}
    return rep


# ---------- CLI ----------


def run(
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    reference_ticker: str = "SPY",
    force_reference: bool = False,
    out_dir: Path = REPORTS_DIR,
) -> dict[str, Any]:
    returns = np.array([])
    dates: list[str] = []
    source = "unknown"
    source_detail = ""
    today_ret: float | None = None

    if not force_reference:
        returns, dates = load_returns_from_snapshot(snapshot_path)
        if returns.size >= MIN_SAMPLES:
            source = "equity_series"
            source_detail = str(snapshot_path.relative_to(ROOT)) if snapshot_path.is_relative_to(ROOT) else str(snapshot_path)
        else:
            returns = np.array([])  # reset; nutze Fallback
            dates = []

    if returns.size == 0:
        yf_returns, yf_dates = load_returns_yfinance(reference_ticker, WINDOW_DAYS)
        if yf_returns.size > 0:
            returns = yf_returns
            dates = yf_dates
            source = "reference_portfolio"
            source_detail = f"yfinance:{reference_ticker} (Fallback: equity_history < {MIN_SAMPLES} Tage)"
        else:
            # Letzter Fallback: die kurzen Equity-Daten trotzdem nutzen
            snap_r, snap_d = load_returns_from_snapshot(snapshot_path)
            returns, dates = snap_r, snap_d
            source = "equity_series_short"
            source_detail = f"{snapshot_path} (yfinance nicht verfuegbar)"

    rep = build_report(returns, source, source_detail, dates, today_ret)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"risk_{rep['date']}.json"
    out_path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    rep["_written_to"] = str(out_path)
    return rep


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SIM-5 VaR/CVaR Report")
    p.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT, help="Pfad zu trades_snapshot.json")
    p.add_argument("--reference-ticker", default="SPY", help="Fallback-Ticker fuer Reference-Portfolio")
    p.add_argument("--force-reference", action="store_true", help="Erzwingt Reference-Portfolio auch bei genug Equity-Daten")
    p.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    p.add_argument("--print", action="store_true", help="Print Report nach stdout")
    args = p.parse_args(argv)

    rep = run(
        snapshot_path=args.snapshot,
        reference_ticker=args.reference_ticker,
        force_reference=args.force_reference,
        out_dir=args.out_dir,
    )
    if args.print:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        # Kompakte Ampel-Ausgabe
        src = rep.get("source", "?")
        hv95 = rep.get("var", {}).get("historical_95_pct")
        hv99 = rep.get("var", {}).get("historical_99_pct")
        cv95 = rep.get("cvar", {}).get("historical_95_pct")
        breach = rep.get("breach_check", {})
        b95 = breach.get("breach_95")
        b99 = breach.get("breach_99")
        n = rep.get("window_days_actual", 0)
        print(f"[SIM-5] source={src} n={n} | VaR95={hv95}% VaR99={hv99}% CVaR95={cv95}% | breach95={b95} breach99={b99}")
        print(f"[SIM-5] report -> {rep.get('_written_to')}")
    # Exit 0 = OK, 1 = Breach-95, 2 = Breach-99
    if rep.get("breach_check", {}).get("breach_99"):
        return 2
    if rep.get("breach_check", {}).get("breach_95"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
