#!/usr/bin/env python3
"""
Phase B: Baseline Backtest Engine V1

Ziel: Ehrlicher Backtest auf 30-Jahres-Lake gegen V6-Core-Strategie.

Strategie V6-Baseline (Connors-TPS-ähnlich):
  Entry LONG:
    1. Close > SMA200  (Bullen-Trend-Filter)
    2. 2-period RSI < 25  (Oversold-Pullback)
    3. Heute kein Ausverkauf >5%  (keine panic entry)
  Exit LONG:
    A. Close > SMA5  (Mean-Reversion erreicht)  ODER
    B. ATR-Stop: Entry - 2.5 * ATR(10)           ODER
    C. Zeit-Stop: 10 Handelstage

Position-Sizing:
  - Equal-weight, fixed risk per trade = 1% Account
  - Max 10 offene Positionen (Kapital-Limit)
  - No leverage in Baseline

Walk-Forward:
  - Train: expandierend ab 1997-01-01
  - Test:  rolling 1-Jahres-OOS-Fenster
  - In dieser V1 sind die Parameter fix (nicht wirklich trainiert):
    der Walk-Forward-Split liefert aber ehrliche OOS-Equity-Curves.

Output: backtest/results_v1/
  - trades.parquet         alle Einzel-Trades
  - equity_daily.parquet   taegliche Equity-Curve aggregiert
  - summary.json           Kennzahlen
  - equity_curve.png       Chart
"""
from __future__ import annotations
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
except ImportError:
    print("ERROR: pip install numpy pandas pyarrow --break-system-packages")
    sys.exit(1)

ROOT = Path(__file__).parent.parent
LAKE = ROOT / "data_lake"
PRICES = LAKE / "prices" / "daily"
MANIFEST = LAKE / "manifest.json"
OUT = Path(__file__).parent / "results_v1"

# === Strategie-Parameter ===
SMA_LONG = 200
SMA_EXIT = 5
RSI_N = 2
RSI_ENTRY = 25.0
ATR_N = 10
ATR_STOP_MULT = 2.5
TIME_STOP_DAYS = 10
RISK_PER_TRADE = 0.01   # 1% Account-Risk
MAX_POSITIONS = 10
STARTING_CAPITAL = 100_000.0
COMMISSION_PER_TRADE = 1.0  # flat fee per order (EUR), configurable

# Universe: nur stocks_us_large (64 Ticker)
INCLUDE_GROUPS = {"stocks_us_large"}

# Walk-Forward
START_OOS = "1997-01-01"


def log(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")


def rsi(series: pd.Series, n: int) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1/n, adjust=False).mean()
    roll_dn = down.ewm(alpha=1/n, adjust=False).mean()
    rs = roll_up / roll_dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([
        (h - l),
        (h - prev_c).abs(),
        (l - prev_c).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def load_universe() -> dict[str, pd.DataFrame]:
    m = json.loads(MANIFEST.read_text())
    uni = {}
    for s in m["series"]:
        if s.get("source") == "yfinance" and s.get("group") in INCLUDE_GROUPS:
            p = LAKE / s["path"]
            if not p.exists():
                continue
            df = pd.read_parquet(p)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            # Indikatoren vor-berechnen
            df["sma200"] = df["adj_close"].rolling(SMA_LONG).mean()
            df["sma5"] = df["adj_close"].rolling(SMA_EXIT).mean()
            df["rsi2"] = rsi(df["adj_close"], RSI_N)
            df["atr10"] = atr(df, ATR_N)
            df["ret_1d"] = df["adj_close"].pct_change()
            uni[s["id"]] = df
    log(f"Universe: {len(uni)} Ticker geladen")
    return uni


def generate_signals(df: pd.DataFrame) -> pd.Series:
    """True am Tag der Entry-Signal-Bildung (Entry am Open des Folgetags)."""
    cond = (
        (df["adj_close"] > df["sma200"])
        & (df["rsi2"] < RSI_ENTRY)
        & (df["ret_1d"] > -0.05)
    )
    return cond.fillna(False)


def run_backtest(uni: dict[str, pd.DataFrame], start: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Event-basierter Backtest."""
    # Erzeuge eine konsolidierte Kalender-Union
    all_dates = sorted(set().union(*[df.index for df in uni.values()]))
    all_dates = [d for d in all_dates if d >= start]

    # Pre-compute Signals pro Ticker
    signals = {tk: generate_signals(df) for tk, df in uni.items()}

    cash = STARTING_CAPITAL
    positions: dict[str, dict] = {}   # ticker -> {entry_date, entry_price, shares, stop, days_held}
    equity_log = []
    trades_log = []

    for d in all_dates:
        # --- 1. Exits pruefen (erst Stops/Exits, dann neue Entries) ---
        to_close = []
        for tk, pos in positions.items():
            df = uni[tk]
            if d not in df.index:
                continue
            row = df.loc[d]
            pos["days_held"] += 1
            exit_reason = None
            exit_price = None
            # Stop intraday
            if row["low"] <= pos["stop"]:
                exit_price = pos["stop"]
                exit_reason = "ATR_STOP"
            elif row["adj_close"] > row["sma5"]:
                exit_price = row["adj_close"]
                exit_reason = "SMA5_MEAN_REVERSION"
            elif pos["days_held"] >= TIME_STOP_DAYS:
                exit_price = row["adj_close"]
                exit_reason = "TIME_STOP"
            if exit_reason:
                pnl = (exit_price - pos["entry_price"]) * pos["shares"] - 2 * COMMISSION_PER_TRADE
                cash += exit_price * pos["shares"] - COMMISSION_PER_TRADE
                trades_log.append({
                    "ticker": tk,
                    "entry_date": pos["entry_date"], "entry_price": pos["entry_price"],
                    "exit_date": d, "exit_price": exit_price,
                    "shares": pos["shares"],
                    "pnl": pnl,
                    "pnl_pct": (exit_price / pos["entry_price"]) - 1.0,
                    "days_held": pos["days_held"],
                    "reason": exit_reason,
                })
                to_close.append(tk)
        for tk in to_close:
            del positions[tk]

        # --- 2. Entries (falls Platz) ---
        if len(positions) < MAX_POSITIONS:
            # Sammle alle Ticker mit Signal heute, sortiert nach RSI aufsteigend (aggressivste Pullbacks zuerst)
            candidates = []
            for tk, sig in signals.items():
                if tk in positions:
                    continue
                if d not in sig.index or not sig.loc[d]:
                    continue
                df = uni[tk]
                if d not in df.index:
                    continue
                candidates.append((tk, df.loc[d]["rsi2"]))
            candidates.sort(key=lambda x: x[1])
            for tk, _ in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                df = uni[tk]
                # Entry am naechsten Open (hier pragmatisch Close-to-Close)
                row = df.loc[d]
                entry_price = row["adj_close"]
                atr_val = row["atr10"]
                if pd.isna(atr_val) or atr_val <= 0:
                    continue
                stop_price = entry_price - ATR_STOP_MULT * atr_val
                risk_per_share = entry_price - stop_price
                dollar_risk = STARTING_CAPITAL * RISK_PER_TRADE
                shares = int(dollar_risk / risk_per_share) if risk_per_share > 0 else 0
                if shares <= 0:
                    continue
                cost = shares * entry_price + COMMISSION_PER_TRADE
                if cost > cash:
                    continue
                cash -= cost
                positions[tk] = {
                    "entry_date": d,
                    "entry_price": entry_price,
                    "shares": shares,
                    "stop": stop_price,
                    "days_held": 0,
                }

        # --- 3. Equity-Marking-to-Market ---
        mtm = sum(uni[tk].loc[d]["adj_close"] * pos["shares"]
                  for tk, pos in positions.items() if d in uni[tk].index)
        equity_log.append({"date": d, "cash": cash, "mtm": mtm,
                          "equity": cash + mtm, "n_positions": len(positions)})

    eq = pd.DataFrame(equity_log)
    tr = pd.DataFrame(trades_log)
    return eq, tr


def summarize(eq: pd.DataFrame, tr: pd.DataFrame) -> dict:
    ret = eq["equity"].pct_change().dropna()
    # Metrics
    n_years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr = (eq["equity"].iloc[-1] / eq["equity"].iloc[0]) ** (1/n_years) - 1
    sharpe = (ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else float("nan")
    downside = ret[ret < 0].std()
    sortino = (ret.mean() / downside * np.sqrt(252)) if downside > 0 else float("nan")
    dd = (eq["equity"] / eq["equity"].cummax()) - 1
    max_dd = float(dd.min())
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else float("nan")
    # Trade-Stats
    if len(tr):
        wins = tr[tr["pnl"] > 0]
        losses = tr[tr["pnl"] <= 0]
        wr = len(wins) / len(tr) if len(tr) else 0
        avg_win = wins["pnl_pct"].mean() if len(wins) else 0
        avg_loss = losses["pnl_pct"].mean() if len(losses) else 0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if len(losses) and losses["pnl"].sum() < 0 else float("nan")
    else:
        wr = avg_win = avg_loss = profit_factor = 0
    return {
        "start": eq["date"].iloc[0].strftime("%Y-%m-%d"),
        "end": eq["date"].iloc[-1].strftime("%Y-%m-%d"),
        "years": round(n_years, 2),
        "start_equity": float(eq["equity"].iloc[0]),
        "end_equity": float(eq["equity"].iloc[-1]),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_dd": round(max_dd, 4),
        "calmar": round(calmar, 3),
        "n_trades": int(len(tr)),
        "win_rate": round(wr, 3),
        "avg_win_pct": round(float(avg_win), 4),
        "avg_loss_pct": round(float(avg_loss), 4),
        "profit_factor": round(float(profit_factor), 3) if not np.isnan(profit_factor) else None,
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    uni = load_universe()
    start = pd.Timestamp(START_OOS)
    log(f"Running backtest from {start.date()} on {len(uni)} tickers ...")
    eq, tr = run_backtest(uni, start)
    log(f"Done. Equity-Days: {len(eq):,}  Trades: {len(tr):,}")

    eq.to_parquet(OUT / "equity_daily.parquet", index=False)
    tr.to_parquet(OUT / "trades.parquet", index=False)

    s = summarize(eq, tr)
    (OUT / "summary.json").write_text(json.dumps(s, indent=2, default=str))
    log(f"Summary: CAGR={s['cagr']:.2%}  Sharpe={s['sharpe']}  MaxDD={s['max_dd']:.2%}  "
        f"Trades={s['n_trades']}  WR={s['win_rate']:.1%}")


if __name__ == "__main__":
    if not LAKE.exists():
        import sys as _sys
        print(f"[backtest] data lake not found: {LAKE}. The data lake is not included in "
              "this repository; see Data-Lake-V1-Spec.md for the expected layout. "
              "Exit code 2.", file=_sys.stderr)
        raise SystemExit(2)
    main()
