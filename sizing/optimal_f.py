"""
R-2: Optimal-f mit Sortino-Decay-Adjustment + Vol-Scaling.

Referenzen:
    * Vince, R. (2007): *The Handbook of Portfolio Mathematics*, Optimal-f
      maximiert das geometrische Wachstum (TWR) eines Trade-Return-Vektors
      ueber ein gemeinsames `f` (Fraktion des Kapitals pro Trade).
    * "Sortino-Decay-Adjustment" (attributed to Escobar, J. (2023); this
      source could NOT be verified, treat it as an in-house heuristic) ersetzt die
      Vollvolatilitaet bei der Risk-Penalty durch die Downside-Deviation
      (Semi-Std ueber Returns unterhalb MAR=0), waehrend die Aufwaerts-
      Volatilitaet vollen Credit bekommt. Penalty-Term `drawdown_penalty`
      daempft f* zusaetzlich in Richtung 0 (Regularisierung bei kurzen
      Sample-Sizes / Regime-Shift).

Wir implementieren drei Bausteine:

1.  `twr(f, returns)`: Terminal Wealth Relative
2.  `compute_optimal_f(returns, ...)`: Grid-Search ueber f ∈ (0, 1)
                                              mit Sortino-Decay-Penalty
3.  `vol_scaling_factor(returns, target)`: Scaler ≤ 1.0, der Sizing an
                                              annualisierte Zielvola koppelt.

Feature-Freeze-konform: reine Math-Schicht, keine DB-/API-Abhaengigkeit,
keine Scoring-Aenderung. Shadow-Mode Integration laeuft ueber
`sizing/optimal_f_shadow.py` und den Kelly-Sizer (`--sizing-mode optimal_f`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence

import numpy as np


# --- Konstanten ---------------------------------------------------------------

OPT_F_GRID_STEPS = 200          # Grid 0.005..1.0 in 0.005er-Schritten
MIN_SAMPLE_OPTF = 20            # Unter N=20 kein Optimal-f
DEFAULT_TARGET_VOL = 0.15       # 15% annualisierte Zielvola (Vol-Scaling)
TRADING_DAYS_YEAR = 252
DEFAULT_PENALTY = 0.5           # Escobar-Penalty-Koeffizient (0..1)
EPS = 1e-12


# --- Dataclass ----------------------------------------------------------------

@dataclass
class OptimalFResult:
    """Ergebnis einer Optimal-f-Kalkulation inkl. Sortino-Decay-Adjustment."""

    n: int
    f_raw: float            # Grid-Maximizer ohne Penalty (klassisches Vince)
    f_sortino: float        # Nach Downside-Deviation-Penalty
    f_applied: float        # f_sortino * (1 - drawdown_penalty * dd_ratio)
    twr_at_f: float         # Terminal Wealth Relative am f_applied
    downside_dev: float     # Semi-Std (Returns < 0)
    upside_dev: float       # Semi-Std (Returns >= 0)
    sortino_ratio: float    # mean / downside_dev
    max_drawdown_pct: float # groesster Kapital-DD im geom. Wachstumspfad
    reason: str             # "ok", "undersampled", "no_edge", "degenerate"

    def to_dict(self) -> dict:
        return asdict(self)


# --- Kern-Mathematik ----------------------------------------------------------

def _as_array(returns: Iterable[float]) -> np.ndarray:
    arr = np.asarray(list(returns), dtype=float)
    # NaN -> 0 vermeiden; zu konservativ, weil wir sonst Samples verlieren.
    # Stattdessen: NaN-Rows droppen.
    return arr[~np.isnan(arr)]


def twr(f: float, returns: np.ndarray) -> float:
    """
    Terminal Wealth Relative nach Vince:
        TWR(f) = Π ( 1 + f * r_i / |worst_loss| )
    wobei `worst_loss` der groesste Verlust in Return-Einheiten ist.
    Bei f=0 oder leeren Returns -> 1.0 (neutral).
    """
    if f <= 0 or returns.size == 0:
        return 1.0
    worst = float(np.min(returns))
    if worst >= 0:
        # keine Verluste -> unbegrenzter TWR; kein sinnvolles Optimal-f
        # (geometrisches Wachstum divergiert). Return +inf als Signal.
        return math.inf
    denom = abs(worst)
    # Holding Period Return pro Trade bei Hebel f*:
    hpr = 1.0 + f * returns / denom
    # Clip gegen <= 0 (sonst log(-x) = NaN; Ruin-Event)
    if np.any(hpr <= 0):
        return 0.0
    return float(np.prod(hpr))


def _downside_upside_dev(returns: np.ndarray,
                         mar: float = 0.0) -> tuple[float, float]:
    """
    Semi-Deviation unter (downside) und ueber (upside) MAR=0.
    Nutzt `N` als Divisor (nicht N-1): standardkonform fuer Sortino.
    """
    if returns.size == 0:
        return 0.0, 0.0
    diffs = returns - mar
    downside = diffs[diffs < 0]
    upside = diffs[diffs >= 0]
    n = returns.size
    dd = math.sqrt(float(np.sum(downside**2)) / n) if downside.size else 0.0
    ud = math.sqrt(float(np.sum(upside**2)) / n) if upside.size else 0.0
    return dd, ud


def _max_drawdown_on_twr_path(f: float, returns: np.ndarray) -> float:
    """
    Simuliert Equity-Pfad unter fester f-Fraktion und liefert max DD in %.
    """
    if f <= 0 or returns.size == 0:
        return 0.0
    worst = float(np.min(returns))
    if worst >= 0:
        return 0.0
    denom = abs(worst)
    hpr = 1.0 + f * returns / denom
    hpr = np.clip(hpr, EPS, None)  # verhindert Ruin-Abbruch
    equity = np.cumprod(hpr)
    running_max = np.maximum.accumulate(equity)
    dd = (equity - running_max) / running_max
    return float(np.min(dd))  # negativer Wert; -0.25 = -25%


def compute_optimal_f(
    returns: Iterable[float],
    drawdown_penalty: float = DEFAULT_PENALTY,
    grid_steps: int = OPT_F_GRID_STEPS,
    min_sample: int = MIN_SAMPLE_OPTF,
) -> OptimalFResult:
    """
    Berechnet Optimal-f nach Vince 2007 mit Sortino-Decay-Adjustment
    (in-house heuristic, unverified attribution "Escobar 2023").

    Parameters
    ----------
    returns : iterable of floats
        Trade-Returns in dezimaler Form (0.02 = +2%).
    drawdown_penalty : float in [0, 1]
        Gewicht des DD-Penaltys. 0 = keine Dampfung (reines Vince),
        1 = maximale Reduktion bei grossem DD. Default 0.5.
    grid_steps : int
        Anzahl f-Kandidaten im Grid 0..1. Default 200 (Schrittweite 0.005).
    min_sample : int
        Unter dieser Trade-Zahl liefern wir `reason="undersampled"` + f=0.

    Returns
    -------
    OptimalFResult
    """
    arr = _as_array(returns)
    n = int(arr.size)
    if n < min_sample:
        return OptimalFResult(
            n=n, f_raw=0.0, f_sortino=0.0, f_applied=0.0, twr_at_f=1.0,
            downside_dev=0.0, upside_dev=0.0, sortino_ratio=0.0,
            max_drawdown_pct=0.0, reason="undersampled",
        )

    mean = float(np.mean(arr))
    worst = float(np.min(arr))
    if worst >= 0:
        # Keine Verluste im Sample -> Optimal-f ist degenerate (∞).
        # Wir cappen konservativ bei f=1.0 und markieren `degenerate`.
        return OptimalFResult(
            n=n, f_raw=1.0, f_sortino=1.0, f_applied=0.0, twr_at_f=math.inf,
            downside_dev=0.0, upside_dev=float(np.std(arr)),
            sortino_ratio=math.inf, max_drawdown_pct=0.0,
            reason="degenerate",
        )
    if mean <= 0:
        # Kein positiver Edge -> f=0 ist optimal.
        return OptimalFResult(
            n=n, f_raw=0.0, f_sortino=0.0, f_applied=0.0, twr_at_f=1.0,
            downside_dev=float(np.std(arr[arr < 0])) if (arr < 0).any() else 0.0,
            upside_dev=float(np.std(arr[arr >= 0])) if (arr >= 0).any() else 0.0,
            sortino_ratio=0.0, max_drawdown_pct=0.0, reason="no_edge",
        )

    # --- Grid-Search nach klassischem Vince-TWR --------------------------
    grid = np.linspace(1.0 / grid_steps, 1.0, grid_steps)
    twrs = np.array([twr(f, arr) for f in grid])
    # inf aussortieren (degenerate Faelle)
    finite = np.isfinite(twrs) & (twrs > 0)
    if not finite.any():
        return OptimalFResult(
            n=n, f_raw=0.0, f_sortino=0.0, f_applied=0.0, twr_at_f=1.0,
            downside_dev=0.0, upside_dev=0.0, sortino_ratio=0.0,
            max_drawdown_pct=0.0, reason="degenerate",
        )
    grid_f = grid[finite]
    grid_twr = twrs[finite]
    idx_raw = int(np.argmax(grid_twr))
    f_raw = float(grid_f[idx_raw])
    twr_raw = float(grid_twr[idx_raw])

    # --- Sortino-Decay-Adjustment (Escobar 2023) -------------------------
    # f_sortino := f_raw * (downside_dev / std). Je kleiner die Downside-
    # Komponente im Verhaeltnis zur Gesamtvola, desto naeher bleibt
    # f_sortino an f_raw. Je asymmetrischer gegen die Downside, desto
    # staerker wird f_sortino reduziert.
    dd_dev, up_dev = _downside_upside_dev(arr, mar=0.0)
    total_std = float(np.std(arr))
    if total_std <= EPS:
        sortino_scale = 1.0
    else:
        sortino_scale = float(np.clip(up_dev / (up_dev + dd_dev + EPS), 0.0, 1.0))
    f_sortino = float(np.clip(f_raw * sortino_scale, 0.0, 1.0))

    # --- DD-Penalty (Escobar-Regularisierung) ----------------------------
    dd_path = _max_drawdown_on_twr_path(f_sortino, arr)  # negativer Wert
    dd_ratio = min(1.0, abs(dd_path))  # in [0, 1] clampen
    penalty = float(np.clip(drawdown_penalty, 0.0, 1.0))
    f_applied = float(np.clip(f_sortino * (1.0 - penalty * dd_ratio), 0.0, 1.0))

    sortino_ratio = (mean / dd_dev) if dd_dev > EPS else math.inf
    twr_at_f = twr(f_applied, arr)

    return OptimalFResult(
        n=n,
        f_raw=f_raw,
        f_sortino=f_sortino,
        f_applied=f_applied,
        twr_at_f=twr_at_f,
        downside_dev=dd_dev,
        upside_dev=up_dev,
        sortino_ratio=sortino_ratio,
        max_drawdown_pct=dd_path,
        reason="ok",
    )


# --- Vol-Scaling (Q2) ---------------------------------------------------------

def realized_vol(returns: Sequence[float],
                 periods_per_year: int = TRADING_DAYS_YEAR,
                 window: int | None = None) -> float:
    """
    Annualisierte realisierte Volatilitaet aus Return-Vektor.
    Bei `window=None` wird das gesamte Sample verwendet.
    """
    arr = _as_array(returns)
    if window is not None and window > 0:
        arr = arr[-window:]
    if arr.size < 2:
        return 0.0
    return float(np.std(arr, ddof=1) * math.sqrt(periods_per_year))


def vol_scaling_factor(
    returns: Sequence[float],
    target_vol: float = DEFAULT_TARGET_VOL,
    periods_per_year: int = TRADING_DAYS_YEAR,
    window: int | None = None,
) -> float:
    """
    Liefert den Vol-Scaler `s = min(1.0, target_vol / current_vol)`.

    Clamp auf [0.0, 1.0]:
        * current_vol <= target_vol  → s = 1.0 (keine Abwertung)
        * current_vol  > target_vol  → s < 1.0 (Sizing wird gekuerzt)

    Niedrige current_vol kann die Position NICHT vergroessern: das ist
    absichtlich konservativ (Vol kann in Stress-Phasen brutal ansteigen,
    ohne dass das Sample es bereits zeigt).
    """
    cur = realized_vol(returns, periods_per_year=periods_per_year, window=window)
    if cur <= EPS:
        return 1.0
    raw = target_vol / cur
    return float(np.clip(raw, 0.0, 1.0))


# --- Self-Test ----------------------------------------------------------------

def _selftest() -> int:
    """Minimale Sanity-Checks (werden auch von tests/test_optimal_f.py genutzt)."""
    rng = np.random.default_rng(42)
    # Positive-Edge Sample: 60% Wins bei +1.5R, 40% Losses bei -1R
    n = 500
    draws = rng.random(n)
    returns = np.where(draws < 0.6, 0.015, -0.01)
    res = compute_optimal_f(returns, drawdown_penalty=0.5)
    assert res.reason == "ok", res
    assert 0.0 < res.f_applied < 1.0
    assert res.f_applied <= res.f_sortino <= res.f_raw

    # Negative-Edge -> f=0
    neg = np.where(draws < 0.4, 0.01, -0.02)
    res_neg = compute_optimal_f(neg)
    assert res_neg.f_applied == 0.0
    assert res_neg.reason == "no_edge"

    # Undersampled
    res_small = compute_optimal_f(returns[:10])
    assert res_small.reason == "undersampled"

    # Vol-Scaling: hohe Vola -> Scaler < 1, niedrige -> == 1.0
    high_vol = returns * 5.0  # blaeht Vola auf
    s_high = vol_scaling_factor(high_vol, target_vol=0.10)
    s_low = vol_scaling_factor(returns * 0.1, target_vol=0.50)
    assert 0.0 < s_high < 1.0, s_high
    assert s_low == 1.0, s_low

    print("sizing/optimal_f selftest: PASS "
          f"(f_raw={res.f_raw:.3f}, f_sortino={res.f_sortino:.3f}, "
          f"f_applied={res.f_applied:.3f}, n={res.n})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
