#!/usr/bin/env python3
"""
Q3: HMM-Regime-Stub (KW17 Rollout, Tag 3).

Light-Version eines 3-State Gaussian HMM Regime-Classifiers fuer das Morning-Briefing.
Nur drei Features (SPY-Return, VIX-Level, Breadth-Spread) und bewusst schlank gehalten.
Die Full-Version (R-1) folgt in KW18 mit weiteren Features + Live-Integration.

Features
--------
* ``spy_log_ret``: Tages-Log-Return SPY (Risiko-Asset-Direction).
* ``vix_level``: VIX-Index Close (Volatilitaets-Level, nicht standardisiert).
* ``breadth_spread``: 5d-Rendite-Spread IWM - SPY (Small-Cap vs Large-Cap als
                       Breadth-/Risk-Appetite-Proxy; positiv = Breadth gut).

Fenster
-------
* Fit auf die letzten ``--lookback-days`` (Default 252) Handelstage.
* Rollierendes Re-Fit 1x pro Woche (Default Montag). An anderen Tagen wird das
  letzte Modell per Viterbi nur "decodiert", keine Neufit. Erzwingbar via
  ``--force-refit``.

Labels
------
Aus Rueckschau auf die gefitteten Gaussians. Der State mit kleinster
``std(spy_log_ret)`` = ``Calm-Bull``, groesste = ``Stress``, Rest = ``Normal``.
Das laesst Labels stabil unabhaengig vom Seed-getriebenen State-Index.

Output
------
``regime/hmm_regime_current.json`` mit ::

    {
      "as_of": "2026-04-23",
      "state_id": 0,
      "label": "Calm-Bull",
      "confidence": 0.78,
      "transition_prob": {"Calm-Bull": 0.82, "Normal": 0.15, "Stress": 0.03},
      "features_last": {...},
      "model": {
        "fitted_at": "2026-04-20",
        "lookback_days": 252,
        "n_train_obs": 251,
        "converged": true,
        "log_likelihood": -1234.56,
        "state_stats": [...]
      },
      "schema_version": "1.0"
    }

Zusaetzlich wird ``regime/hmm_regime_YYYY-MM-DD.json`` als Snapshot geschrieben.

Datenquellen
------------
* Primaer: ``data_lake/prices/daily/{TICKER}.parquet`` (local yfinance downloader).
* Fallback: yfinance live-Download, wenn Parquet fehlt oder zu alt ist.

Free-Stack only: hmmlearn, numpy, pandas, pyarrow; yfinance als optionaler Fallback.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="hmmlearn")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRICES_DIR = PROJECT_ROOT / "data_lake" / "prices" / "daily"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "regime"
DEFAULT_MODEL_CACHE = PROJECT_ROOT / "regime" / "_model_cache.json"

# Ticker-Mapping: Parquet-Dateiname <-> yfinance-Symbol
TICKERS = {
    "spy":    {"parquet": "SPY",     "yf": "SPY"},
    "vix":    {"parquet": "IDX_VIX", "yf": "^VIX"},
    "iwm":    {"parquet": "IWM",     "yf": "IWM"},
}

LABELS_ORDERED = ("Calm-Bull", "Normal", "Stress")
SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Datenladen
# ---------------------------------------------------------------------------
def _load_parquet(ticker_key: str, prices_dir: Path) -> pd.DataFrame | None:
    """Liest ``data_lake/prices/daily/{TICKER}.parquet`` oder ``None`` bei Fehler."""
    symbol = TICKERS[ticker_key]["parquet"]
    path = prices_dir / f"{symbol}.parquet"
    if not path.exists():
        return None
    last_err: Exception | None = None
    # FUSE-Mount gibt gelegentlich "Resource deadlock avoided": ein paar Retries.
    for _ in range(3):
        try:
            df = pd.read_parquet(path)
            break
        except OSError as e:
            last_err = e
            continue
    else:
        print(f"[hmm_stub] WARN parquet {symbol} nicht lesbar ({last_err}), fallback yfinance", file=sys.stderr)
        return None

    if "date" in df.columns:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
    close_col = "adj_close" if "adj_close" in df.columns else "close"
    if close_col not in df.columns:
        return None
    out = df[[close_col]].rename(columns={close_col: "close"})
    return out


def _load_yfinance(ticker_key: str, days: int) -> pd.DataFrame | None:
    """Fallback per yfinance, nur wenn Parquet fehlt/veraltet."""
    try:
        import yfinance as yf
    except ImportError:
        print("[hmm_stub] yfinance nicht installiert, kein Fallback moeglich", file=sys.stderr)
        return None

    symbol = TICKERS[ticker_key]["yf"]
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days + 60)
    try:
        df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
    except Exception as e:
        print(f"[hmm_stub] yfinance-Fehler {symbol}: {e}", file=sys.stderr)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    out = df[[close_col]].rename(columns={close_col: "close"})
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def load_series(ticker_key: str, lookback_days: int, prices_dir: Path, allow_yf: bool = True) -> pd.DataFrame:
    """Laedt eine Ticker-Zeitreihe mit Parquet-Primaer, yfinance-Fallback.

    Validierungsregel fuer Parquet-Freshness: Letztes Datum muss >= (heute - 10 Kalendertage) sein,
    sonst Fallback. Das reduziert Stale-Data-Risiken wenn der Downloader pausiert war.
    """
    df = _load_parquet(ticker_key, prices_dir)
    today = pd.Timestamp(date.today())
    need_fallback = df is None or df.empty or (today - df.index[-1]).days > 10
    if need_fallback and allow_yf:
        yf_df = _load_yfinance(ticker_key, lookback_days)
        if yf_df is not None and not yf_df.empty:
            if df is None or df.empty:
                df = yf_df
            else:
                # Merge + dedupe auf Index
                df = pd.concat([df, yf_df]).sort_index()
                df = df[~df.index.duplicated(keep="last")]
    if df is None or df.empty:
        raise RuntimeError(f"Keine Daten fuer {ticker_key} verfuegbar (parquet+yf leer)")
    return df.tail(lookback_days + 60)  # etwas Puffer fuer Breadth-Rollfenster


def build_feature_matrix(lookback_days: int, prices_dir: Path, allow_yf: bool = True) -> pd.DataFrame:
    """Baut die (T, 3)-Feature-Matrix: spy_log_ret, vix_level, breadth_spread."""
    spy = load_series("spy", lookback_days, prices_dir, allow_yf=allow_yf)["close"]
    vix = load_series("vix", lookback_days, prices_dir, allow_yf=allow_yf)["close"]
    iwm = load_series("iwm", lookback_days, prices_dir, allow_yf=allow_yf)["close"]

    # Align auf gemeinsame Handelstage
    idx = spy.index.intersection(vix.index).intersection(iwm.index).sort_values()
    spy = spy.reindex(idx).astype(float)
    vix = vix.reindex(idx).astype(float)
    iwm = iwm.reindex(idx).astype(float)

    spy_log_ret = np.log(spy / spy.shift(1))
    iwm_log_ret = np.log(iwm / iwm.shift(1))
    # 5d Rendite-Spread (IWM - SPY) = Breadth-Proxy
    breadth_spread = (iwm_log_ret.rolling(5).sum() - spy_log_ret.rolling(5).sum())

    feats = pd.DataFrame({
        "spy_log_ret": spy_log_ret,
        "vix_level": vix,
        "breadth_spread": breadth_spread,
    }).dropna()

    return feats.tail(lookback_days)


# ---------------------------------------------------------------------------
# HMM-Fit + Label-Mapping
# ---------------------------------------------------------------------------
@dataclass
class HMMFitResult:
    hidden_state_seq: np.ndarray             # Viterbi-Pfad auf train
    posterior_last: np.ndarray               # (n_states,) fuer letzten Timestamp
    transmat: np.ndarray                     # (n_states, n_states)
    means: np.ndarray                        # (n_states, n_features) im STANDARDIZED-Raum
    covars: np.ndarray                       # (n_states, n_features, n_features) im STANDARDIZED-Raum
    log_likelihood: float
    converged: bool
    state_to_label: dict[int, str]           # mapping Index -> Label
    label_to_state: dict[str, int]
    n_train_obs: int
    scaler_mean: np.ndarray                  # (n_features,) zum Re-Skalieren
    scaler_std: np.ndarray                   # (n_features,)

    def means_original(self) -> np.ndarray:
        """Rueck-skalierte Means im urspruenglichen Feature-Raum."""
        return self.means * self.scaler_std + self.scaler_mean

    def stds_original(self) -> np.ndarray:
        """Standard-Abweichungen pro Feature pro State im urspruenglichen Raum."""
        diag = np.diagonal(self.covars, axis1=1, axis2=2) if self.covars.ndim == 3 else self.covars
        return np.sqrt(diag) * self.scaler_std


def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = X.mean(axis=0)
    std = X.std(axis=0, ddof=0)
    std = np.where(std < 1e-8, 1.0, std)  # Absicherung gegen degenerierte Features
    return (X - mean) / std, mean, std


def fit_hmm(X: np.ndarray, n_states: int = 3, seed: int = 42, n_iter: int = 500) -> HMMFitResult:
    """Fittet ein Gaussian HMM auf die Feature-Matrix X (mit Z-Score-Standardisierung)."""
    from hmmlearn.hmm import GaussianHMM

    Xs, mean, std = _standardize(X)

    model = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        n_iter=n_iter,
        tol=1e-4,
        random_state=seed,
        init_params="stmc",
        params="stmc",
    )
    model.fit(Xs)
    hidden = model.predict(Xs)
    posterior = model.predict_proba(Xs)
    loglik = float(model.score(Xs))

    # Label-Mapping: lowest std(spy_log_ret) = Calm-Bull, highest = Stress
    # Feature 0 = spy_log_ret (im Standardized-Raum ist die Ordnung erhalten)
    covars = model.covars_
    if covars.ndim == 3:
        std_spy = np.sqrt(np.diagonal(covars, axis1=1, axis2=2)[:, 0])
    else:
        std_spy = np.sqrt(covars[:, 0])

    order = np.argsort(std_spy)  # aufsteigend: niedrigste Vol zuerst
    state_to_label: dict[int, str] = {}
    if n_states == 3:
        state_to_label[int(order[0])] = "Calm-Bull"
        state_to_label[int(order[1])] = "Normal"
        state_to_label[int(order[2])] = "Stress"
    else:
        # Generischer Fallback (wird in der Stub-Version nicht ausgeloest)
        for i, s in enumerate(order):
            state_to_label[int(s)] = f"S{i}"

    label_to_state = {v: k for k, v in state_to_label.items()}

    return HMMFitResult(
        hidden_state_seq=hidden,
        posterior_last=posterior[-1],
        transmat=model.transmat_,
        means=model.means_,
        covars=covars,
        log_likelihood=loglik,
        converged=bool(getattr(model.monitor_, "converged", False)),
        state_to_label=state_to_label,
        label_to_state=label_to_state,
        n_train_obs=int(Xs.shape[0]),
        scaler_mean=mean,
        scaler_std=std,
    )


# ---------------------------------------------------------------------------
# Caching / Weekly-Refit-Logik
# ---------------------------------------------------------------------------
def _should_refit(cache_path: Path, today: date, weekday_trigger: int, force: bool) -> tuple[bool, str]:
    """Regeln:
    * force = True -> refit, reason='force'
    * Kein Cache -> refit, reason='no_cache'
    * heute ist Weekday-Trigger (0=Mo) -> refit, reason='weekly'
    * letzter Fit > 10 Kalendertage zurueck -> refit, reason='stale'
    * sonst -> kein refit, reason='cached'
    """
    if force:
        return True, "force"
    if not cache_path.exists():
        return True, "no_cache"
    try:
        cache = json.loads(cache_path.read_text())
        fitted_at = date.fromisoformat(cache["fitted_at"])
    except Exception:
        return True, "cache_corrupt"
    if today.weekday() == weekday_trigger and (today - fitted_at).days >= 1:
        return True, "weekly"
    if (today - fitted_at).days > 10:
        return True, "stale"
    return False, "cached"


def _save_model_cache(cache_path: Path, fit: HMMFitResult, as_of: date, lookback_days: int) -> None:
    """Serialisiert alles was fuer ein Decode-Only-Run am naechsten Tag reicht."""
    payload = {
        "fitted_at": as_of.isoformat(),
        "lookback_days": lookback_days,
        "n_states": int(fit.means.shape[0]),
        "means": fit.means.tolist(),
        "covars": fit.covars.tolist(),
        "transmat": fit.transmat.tolist(),
        "startprob": [1.0 / fit.means.shape[0]] * fit.means.shape[0],  # nicht kritisch fuer Decode
        "state_to_label": {str(k): v for k, v in fit.state_to_label.items()},
        "scaler_mean": fit.scaler_mean.tolist(),
        "scaler_std": fit.scaler_std.tolist(),
        "log_likelihood": fit.log_likelihood,
        "converged": fit.converged,
        "schema_version": SCHEMA_VERSION,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _load_model_cache(cache_path: Path) -> dict[str, Any] | None:
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return None


def decode_from_cache(cache: dict[str, Any], X: np.ndarray) -> HMMFitResult:
    """Rekonstruiert ein GaussianHMM aus Cache-JSON und dekodiert die Sequenz.

    X wird mit dem gecachten Scaler standardisiert, bevor das Modell es sieht.
    """
    from hmmlearn.hmm import GaussianHMM

    n_states = cache["n_states"]
    means = np.asarray(cache["means"], dtype=float)
    covars = np.asarray(cache["covars"], dtype=float)
    transmat = np.asarray(cache["transmat"], dtype=float)
    startprob = np.asarray(cache.get("startprob") or [1.0 / n_states] * n_states, dtype=float)
    scaler_mean = np.asarray(cache.get("scaler_mean") or [0.0] * X.shape[1], dtype=float)
    scaler_std = np.asarray(cache.get("scaler_std") or [1.0] * X.shape[1], dtype=float)

    Xs = (X - scaler_mean) / scaler_std

    model = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        init_params="",
        params="",
    )
    model.n_features = means.shape[1]
    model.means_ = means
    model.covars_ = covars
    model.transmat_ = transmat
    model.startprob_ = startprob

    hidden = model.predict(Xs)
    posterior = model.predict_proba(Xs)
    loglik = float(model.score(Xs))

    state_to_label = {int(k): v for k, v in cache["state_to_label"].items()}
    label_to_state = {v: k for k, v in state_to_label.items()}

    return HMMFitResult(
        hidden_state_seq=hidden,
        posterior_last=posterior[-1],
        transmat=transmat,
        means=means,
        covars=covars,
        log_likelihood=loglik,
        converged=bool(cache.get("converged", True)),
        state_to_label=state_to_label,
        label_to_state=label_to_state,
        n_train_obs=int(X.shape[0]),
        scaler_mean=scaler_mean,
        scaler_std=scaler_std,
    )


# ---------------------------------------------------------------------------
# Output-Builder
# ---------------------------------------------------------------------------
def build_output(fit: HMMFitResult, feats: pd.DataFrame, as_of: date, refit_reason: str,
                 fitted_at: date, lookback_days: int) -> dict[str, Any]:
    last_state = int(fit.hidden_state_seq[-1])
    label = fit.state_to_label[last_state]
    confidence = float(fit.posterior_last[last_state])

    # Transition-Prob: Wo wandere ich als naechstes hin? Zeile = aktueller State.
    next_row = fit.transmat[last_state]
    trans = {fit.state_to_label[int(i)]: float(next_row[i]) for i in range(fit.transmat.shape[0])}
    # Sortiere stabil in Label-Order fuer Konsumenten
    trans_sorted = {lbl: trans.get(lbl, 0.0) for lbl in LABELS_ORDERED if lbl in trans}

    # State-Stats fuer Debug/Audit: im ORIGINAL-Feature-Raum fuer Lesbarkeit
    means_orig = fit.means_original()
    stds_orig = fit.stds_original()
    state_stats = []
    for s in range(fit.means.shape[0]):
        state_stats.append({
            "state_id": int(s),
            "label": fit.state_to_label[s],
            "mean": {
                "spy_log_ret": float(means_orig[s, 0]),
                "vix_level": float(means_orig[s, 1]),
                "breadth_spread": float(means_orig[s, 2]),
            },
            "std": {
                "spy_log_ret": float(stds_orig[s, 0]),
                "vix_level": float(stds_orig[s, 1]),
                "breadth_spread": float(stds_orig[s, 2]),
            },
        })

    last_row = feats.iloc[-1]
    return {
        "as_of": as_of.isoformat(),
        "state_id": last_state,
        "label": label,
        "confidence": round(confidence, 4),
        "transition_prob": {k: round(v, 4) for k, v in trans_sorted.items()},
        "features_last": {
            "date": feats.index[-1].date().isoformat() if hasattr(feats.index[-1], "date") else str(feats.index[-1])[:10],
            "spy_log_ret": float(last_row["spy_log_ret"]),
            "vix_level": float(last_row["vix_level"]),
            "breadth_spread": float(last_row["breadth_spread"]),
        },
        "model": {
            "fitted_at": fitted_at.isoformat(),
            "refit_reason": refit_reason,
            "lookback_days": lookback_days,
            "n_train_obs": fit.n_train_obs,
            "converged": fit.converged,
            "log_likelihood": round(fit.log_likelihood, 4),
            "state_stats": state_stats,
        },
        "schema_version": SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HMM-Regime-Stub fuer Morning-Briefing (Q3 / Vor-Stufe R-1).")
    p.add_argument("--lookback-days", type=int, default=252, help="Handelstage fuer Fit/Decode (Default 252).")
    p.add_argument("--n-states", type=int, default=3, help="Anzahl HMM-States (Default 3).")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--weekday-refit", type=int, default=0, help="Wochentag fuer Refit (0=Mo, default 0).")
    p.add_argument("--force-refit", action="store_true", help="Erzwingt Refit unabhaengig vom Cache.")
    p.add_argument("--prices-dir", type=Path, default=DEFAULT_PRICES_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--cache-file", type=Path, default=DEFAULT_MODEL_CACHE)
    p.add_argument("--no-yfinance", action="store_true", help="Verbietet yfinance-Fallback (Offline-Mode).")
    p.add_argument("--as-of", type=str, default=None, help="Override-Datum YYYY-MM-DD (Tests/Replay).")
    p.add_argument("--json-only", action="store_true", help="Gibt Output auch auf stdout aus (Briefing-Pipe).")
    args = p.parse_args(argv)

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    # 1) Features laden
    try:
        feats = build_feature_matrix(args.lookback_days, args.prices_dir, allow_yf=not args.no_yfinance)
    except RuntimeError as e:
        print(f"[hmm_stub] ERROR no price data: {e}. The data lake ({args.prices_dir}) is not "
              "included in this repository (see Data-Lake-V1-Spec.md), and the yfinance "
              "fallback is unavailable, failed or disabled (--no-yfinance). Exit code 2.",
              file=sys.stderr)
        return 2
    if feats.shape[0] < 60:
        print(f"[hmm_stub] ERROR zu wenige Observations: {feats.shape[0]} < 60", file=sys.stderr)
        return 2

    X = feats.to_numpy(dtype=float)

    # 2) Refit-Gate
    refit, reason = _should_refit(args.cache_file, as_of, args.weekday_refit, args.force_refit)

    if refit:
        fit = fit_hmm(X, n_states=args.n_states, seed=args.seed)
        _save_model_cache(args.cache_file, fit, as_of, args.lookback_days)
        fitted_at = as_of
    else:
        cache = _load_model_cache(args.cache_file)
        if cache is None:
            fit = fit_hmm(X, n_states=args.n_states, seed=args.seed)
            _save_model_cache(args.cache_file, fit, as_of, args.lookback_days)
            reason, fitted_at = "no_cache", as_of
        else:
            fit = decode_from_cache(cache, X)
            fitted_at = date.fromisoformat(cache["fitted_at"])

    # 3) Output-JSON
    out = build_output(fit, feats, as_of, reason, fitted_at, args.lookback_days)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    current_path = args.output_dir / "hmm_regime_current.json"
    history_path = args.output_dir / f"hmm_regime_{as_of.isoformat()}.json"
    current_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    history_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    # Briefing-Hook: Markdown-Snippet fuer daily-global-market-briefing.
    # Das Briefing liest diese Datei und haengt die Zeile in Abschnitt "V6.1 Regime-Klassifikation" an.
    briefing_dir = PROJECT_ROOT / "Briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)
    hook_line = (
        f"**HMM-Regime (Q3-Stub):** {out['label']} "
        f"({out['confidence']*100:.1f}% Confidence) · "
        f"Next-State-Prob: "
        + " · ".join(f"{k} {v*100:.0f}%" for k, v in out["transition_prob"].items())
        + f" · fitted_at={out['model']['fitted_at']} · n_train={out['model']['n_train_obs']}"
    )
    (briefing_dir / f"hmm_regime_line_{as_of.isoformat()}.md").write_text(hook_line + "\n")
    (briefing_dir / "hmm_regime_line_latest.md").write_text(hook_line + "\n")

    # Kompakt-Summary auf stderr (fuer scheduled-task log)
    summary = f"[hmm_stub] {out['as_of']} label={out['label']} conf={out['confidence']:.2%} reason={reason} vix={out['features_last']['vix_level']:.2f}"
    print(summary, file=sys.stderr)

    if args.json_only:
        print(json.dumps(out, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
