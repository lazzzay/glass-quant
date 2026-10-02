#!/usr/bin/env python3
"""
V6.1 Walk-Forward Analyse
Zerlegt den Backtest in Train / Validation / Test und prüft, ob die
V6.1-Regeln (Tier-X-Blacklist, Max-Hold 3d, ATR-Multi je Asset) auch
out-of-sample standhalten.

Split:
    Train:      2023 H2  (Regel-Fit)
    Validation: 2024     (Regel-Tuning)
    Test:       2025-26  (echtes Out-of-Sample)

Metriken pro Segment:
    Trades, Win-Rate, Profit Factor, Netto P&L, Avg Win/Loss,
    Best/Worst Exit-Reason, Best/Worst Ticker
"""
import csv, sys, os
from collections import defaultdict

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "v6_backtest_results_v61.csv")

def f(v):
    try: return float(v)
    except: return 0.0

def stats(rows, label):
    if not rows:
        print(f"{label}: (keine Trades)")
        return
    n = len(rows)
    wins = sum(1 for r in rows if r['is_win']=='True')
    netto_v6 = sum(f(r['netto_pnl_1eur']) for r in rows)
    netto_v61 = sum(f(r['v61_netto_pnl']) for r in rows if r['v61_skip']=='False')
    kept = sum(1 for r in rows if r['v61_skip']=='False')
    wins_v61 = sum(1 for r in rows if r['v61_skip']=='False' and r['is_win']=='True')

    win_sum = sum(f(r['v61_netto_pnl']) for r in rows if r['v61_skip']=='False' and f(r['v61_netto_pnl'])>0)
    loss_sum = sum(-f(r['v61_netto_pnl']) for r in rows if r['v61_skip']=='False' and f(r['v61_netto_pnl'])<0)
    pf = win_sum/loss_sum if loss_sum > 0 else float('inf')

    print(f"\n=== {label} ===")
    print(f"V6 @1€:  {n:4d} trades, WR {wins/n*100:5.1f}%, Netto {netto_v6:+8.2f}€")
    print(f"V6.1:    {kept:4d} trades, WR {wins_v61/kept*100 if kept else 0:5.1f}%, Netto {netto_v61:+8.2f}€, PF {pf:.2f}")
    print(f"Blacklisted (Tier X): {n-kept}")

    # Top/Bottom Ticker in diesem Segment (V6.1)
    by_t = defaultdict(lambda: [0, 0.0])
    for r in rows:
        if r['v61_skip']=='False':
            by_t[r['ticker']][0] += 1
            by_t[r['ticker']][1] += f(r['v61_netto_pnl'])
    ranked = sorted(by_t.items(), key=lambda x:-x[1][1])
    if ranked:
        t,v = ranked[0]
        print(f"Best Ticker:  {t} ({v[0]}tr, {v[1]:+.2f}€)")
        t,v = ranked[-1]
        print(f"Worst Ticker: {t} ({v[0]}tr, {v[1]:+.2f}€)")


def main():
    rows = list(csv.DictReader(open(CSV_PATH)))

    # Split by year
    train = [r for r in rows if r['year'] == '2023']
    val = [r for r in rows if r['year'] == '2024']
    test = [r for r in rows if r['year'] in ('2025', '2026')]
    test_2025 = [r for r in rows if r['year'] == '2025']
    test_2026 = [r for r in rows if r['year'] == '2026']

    print("V6.1 WALK-FORWARD ANALYSE")
    print("=" * 60)
    stats(train, "TRAIN (2023 H2)")
    stats(val, "VALIDATION (2024)")
    stats(test, "TEST (2025 + 2026 Q1): OUT-OF-SAMPLE")
    stats(test_2025, "  Sub-Test 2025")
    stats(test_2026, "  Sub-Test 2026 YTD")

    # Overfitting check
    print("\n" + "=" * 60)
    print("OVERFITTING-CHECK")
    print("=" * 60)
    def wr(rs):
        k = [r for r in rs if r['v61_skip']=='False']
        if not k: return 0
        return sum(1 for r in k if r['is_win']=='True') / len(k) * 100
    def netto(rs):
        return sum(f(r['v61_netto_pnl']) for r in rs if r['v61_skip']=='False')

    train_wr = wr(train); val_wr = wr(val); test_wr = wr(test)
    print(f"Train WR: {train_wr:5.1f}%  Val WR: {val_wr:5.1f}%  Test WR: {test_wr:5.1f}%")
    print(f"Train Netto: {netto(train):+.0f}€  Val: {netto(val):+.0f}€  Test: {netto(test):+.0f}€")

    drop_val_test = val_wr - test_wr
    if drop_val_test > 8:
        print(f"\n⚠️  WARNUNG: WR fällt von Val→Test um {drop_val_test:.1f}pp, möglicher Overfit.")
    elif drop_val_test > 4:
        print(f"\n→ Leichte Degradation ({drop_val_test:.1f}pp). Akzeptabel, aber beobachten.")
    else:
        print(f"\n✅ Kein signifikanter Overfit ({drop_val_test:.1f}pp Drop).")

    # Regime-Drift warning
    wr_2026 = wr(test_2026)
    if wr_2026 < 50 and len(test_2026) > 20:
        print(f"⚠️  2026 YTD WR = {wr_2026:.1f}%: Regime-Drift wahrscheinlich, Sprint 2 Regime-Gate dringend nötig.")

if __name__ == "__main__":
    main()
