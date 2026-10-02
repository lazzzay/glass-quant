#!/usr/bin/env python3
"""
layer_ablation.py: H2-Baustein der Rendite-Strategie (2026-06-01).

Beantwortet die H2-Kernfrage: *Welcher der Boost-Layer traegt wirklich Edge?*
Vergleicht Konfigurationen (Basis, Basis+Regime, Basis+je-ein-Layer, Voll-Stack)
auf realisierte Forward-Returns und gibt je Layer ein Verdikt KEEP/KILL/NEUTRAL.
Plus Threshold-Sweep zur Neueichung der Buy-/High-Conviction-Schwellen.

WICHTIG: stdlib only (kein numpy/pandas) -> laeuft ueberall, import-safe.
Rechnet NICHT selbst Forward-Returns aus Kursen: es nimmt ein fertiges Panel.
Wie das Panel auf dem Mac erzeugt wird: siehe H2-Layer-Ablation-Runbook.md.

Panel-Format (JSON: Liste von Zeilen ODER {"rows": [...]}):
    {
      "date": "2026-03-01", "ticker": "AAA",
      "base_score": 6.80,                 # 0.667*triple_rsi + 0.333*connors_scaled
      "adjusts": {                        # additive Layer-Adjusts (0/null erlaubt)
          "sector_rs": 0.30, "insider": 0.10, "options_flow": 0.0,
          "market_pcr": 0.0, "news": 0.0, "mtf": 0.0
      },
      "forward_return": 0.021,            # realisierter Return ueber Hold-Horizont (Dezimal)
      "regime_ok": true                   # optional: Regime-Gate bestanden?
    }

CLI:
    python3 tools/layer_ablation.py --panel panel.json [--top-pct 20] [--out ...]
    python3 tools/layer_ablation.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

KEEP_MIN_DELTA = 0.0005   # +5 bps Top-K-Expectancy-Lift -> KEEP-Kandidat
KILL_MAX_DELTA = 0.0      # <= 0 -> KILL-Kandidat (alles dazwischen = NEUTRAL)


# ---------------------------------------------------------------------------
# Statistik (stdlib)
# ---------------------------------------------------------------------------
def pearson(xs: list[float], ys: list[float]) -> float | None:
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 10:
        return None
    xs2 = [p[0] for p in pts]
    ys2 = [p[1] for p in pts]
    mx, my = st.mean(xs2), st.mean(ys2)
    num = sum((x - mx) * (y - my) for x, y in pts)
    den = math.sqrt(sum((x - mx) ** 2 for x in xs2) * sum((y - my) ** 2 for y in ys2))
    return (num / den) if den else None


def _topk_metrics(rows: list[dict], score_fn, top_pct: float) -> dict:
    """Ranke Zeilen nach score_fn, nimm Top-K%, miss deren Forward-Return."""
    scored = [(score_fn(r), r["forward_return"]) for r in rows
              if score_fn(r) is not None and r.get("forward_return") is not None]
    if not scored:
        return {"n": 0}
    scored.sort(key=lambda t: t[0], reverse=True)
    k = max(1, int(round(len(scored) * top_pct / 100.0)))
    top = scored[:k]
    rets = [t[1] for t in top]
    allscore = [t[0] for t in scored]
    allret = [t[1] for t in scored]
    avg = st.mean(rets)
    sd = st.pstdev(rets) if len(rets) > 1 else 0.0
    return {
        "n": len(scored),
        "topk_n": k,
        "corr": pearson(allscore, allret),
        "topk_avg_ret": avg,
        "topk_wr": sum(1 for x in rets if x > 0) / len(rets),
        "topk_sharpe": (avg / sd) if sd else None,   # per-Trade, NICHT annualisiert
    }


# ---------------------------------------------------------------------------
# Ablation
# ---------------------------------------------------------------------------
def _adj(r: dict, layer: str) -> float:
    v = (r.get("adjusts") or {}).get(layer)
    return float(v) if v is not None else 0.0


def run_ablation(panel: list[dict], top_pct: float = 20.0) -> dict:
    layers = sorted({k for r in panel for k in (r.get("adjusts") or {}).keys()})
    has_regime = any("regime_ok" in r for r in panel)

    def base(r):
        b = r.get("base_score")
        return float(b) if b is not None else None

    configs: dict[str, dict] = {}
    configs["BASE"] = _topk_metrics(panel, base, top_pct)
    if has_regime:
        regime_rows = [r for r in panel if r.get("regime_ok")]
        configs["BASE+REGIME"] = _topk_metrics(regime_rows, base, top_pct)
    for L in layers:
        configs[f"BASE+{L}"] = _topk_metrics(
            panel, lambda r, L=L: (base(r) + _adj(r, L)) if base(r) is not None else None, top_pct
        )
    configs["FULL"] = _topk_metrics(
        panel, lambda r: (base(r) + sum(_adj(r, L) for L in layers)) if base(r) is not None else None, top_pct
    )

    # Verdikt je Layer: Top-K-Expectancy-Delta gegen BASE
    base_ret = configs["BASE"].get("topk_avg_ret")
    verdicts = {}
    for L in layers:
        cfg = configs[f"BASE+{L}"]
        d = (cfg.get("topk_avg_ret") - base_ret) if (cfg.get("topk_avg_ret") is not None and base_ret is not None) else None
        if d is None:
            verdict = "INSUFFICIENT"
        elif d > KEEP_MIN_DELTA:
            verdict = "KEEP"
        elif d <= KILL_MAX_DELTA:
            verdict = "KILL"
        else:
            verdict = "NEUTRAL"
        verdicts[L] = {"delta_topk_ret": d, "verdict": verdict}

    return {"configs": configs, "layers": layers, "verdicts": verdicts,
            "has_regime": has_regime, "top_pct": top_pct, "n_rows": len(panel)}


def threshold_sweep(panel: list[dict], use: str = "FULL",
                    lo: float = 6.0, hi: float = 9.0, step: float = 0.25) -> list[dict]:
    layers = sorted({k for r in panel for k in (r.get("adjusts") or {}).keys()})

    def score(r):
        b = r.get("base_score")
        if b is None:
            return None
        b = float(b)
        return b + (sum(_adj(r, L) for L in layers) if use == "FULL" else 0.0)

    rows = [(score(r), r["forward_return"]) for r in panel
            if score(r) is not None and r.get("forward_return") is not None]
    out = []
    t = lo
    while t <= hi + 1e-9:
        sel = [ret for s, ret in rows if s >= t]
        if sel:
            out.append({"threshold": round(t, 2), "n": len(sel),
                        "avg_ret": round(st.mean(sel), 4),
                        "wr": round(sum(1 for x in sel if x > 0) / len(sel), 3)})
        else:
            out.append({"threshold": round(t, 2), "n": 0, "avg_ret": None, "wr": None})
        t += step
    return out


# ---------------------------------------------------------------------------
# I/O + Report
# ---------------------------------------------------------------------------
def load_panel(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        rows = []
        for r in csv.DictReader(open(path, encoding="utf-8")):
            adj = {k.split("adj_", 1)[1]: float(v) for k, v in r.items()
                   if k.startswith("adj_") and v not in (None, "")}
            rows.append({"date": r.get("date"), "ticker": r.get("ticker"),
                         "base_score": float(r["base_score"]) if r.get("base_score") else None,
                         "adjusts": adj,
                         "forward_return": float(r["forward_return"]) if r.get("forward_return") else None,
                         "regime_ok": (str(r.get("regime_ok", "")).lower() in ("1", "true", "yes"))
                         if "regime_ok" in r else None})
        return rows
    data = json.load(open(path, encoding="utf-8"))
    return data["rows"] if isinstance(data, dict) else data


def render(report: dict, sweep: list[dict]) -> str:
    L = []
    L.append(f"# Layer-Ablation: {report['n_rows']} Zeilen, Top-{report['top_pct']:.0f}%\n")
    L.append("## Konfigurationen (Top-K nach Config-Score)\n")
    L.append("| Config | n | corr | Top-K avg ret | Top-K WR | Top-K Sharpe/Trade |")
    L.append("|---|--:|--:|--:|--:|--:|")
    for name, m in report["configs"].items():
        if not m.get("n"):
            L.append(f"| {name} | 0 | - | - | - | - |"); continue
        c = m.get("corr"); s = m.get("topk_sharpe")
        L.append(f"| {name} | {m['n']} | {('%.3f'%c) if c is not None else '-'} | "
                 f"{m['topk_avg_ret']*100:+.2f}% | {m['topk_wr']*100:.0f}% | "
                 f"{('%.2f'%s) if s is not None else '-'} |")
    L.append("\n## Layer-Verdikt (Δ Top-K-Expectancy vs BASE)\n")
    L.append("| Layer | Δ avg ret | Verdikt |")
    L.append("|---|--:|---|")
    for lay, v in report["verdicts"].items():
        d = v["delta_topk_ret"]
        L.append(f"| {lay} | {(('%+.3f%%'% (d*100)) if d is not None else '-')} | {v['verdict']} |")
    keep = [l for l, v in report["verdicts"].items() if v["verdict"] == "KEEP"]
    kill = [l for l, v in report["verdicts"].items() if v["verdict"] == "KILL"]
    L.append(f"\n**KEEP-Kandidaten:** {', '.join(keep) or '-'}")
    L.append(f"**KILL-Kandidaten:** {', '.join(kill) or '-'}")
    L.append("\n## Threshold-Sweep (FULL-Score): fuer Neueichung Buy/High-Conviction\n")
    L.append("| Threshold | n Trades | avg ret | WR |")
    L.append("|--:|--:|--:|--:|")
    for s in sweep:
        ar = ("%+.2f%%" % (s["avg_ret"]*100)) if s["avg_ret"] is not None else "-"
        wr = ("%.0f%%" % (s["wr"]*100)) if s["wr"] is not None else "-"
        L.append(f"| {s['threshold']:.2f} | {s['n']} | {ar} | {wr} |")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Selftest (Synthetik: Signal in EINEM Layer, Rauschen in den anderen)
# ---------------------------------------------------------------------------
def make_synthetic(n: int = 600, seed: int = 7) -> list[dict]:
    import random
    rnd = random.Random(seed)
    rows = []
    for _ in range(n):
        base = rnd.uniform(5.0, 7.3)
        sig = rnd.choice([0.0, 0.30])     # 'sector_rs' = echtes Signal
        noise1 = rnd.choice([-0.20, 0.0, 0.20])
        noise2 = rnd.choice([-0.12, 0.0, 0.12])
        # Forward-Return: getrieben von base (schwach) + sig (stark), Rest reines Rauschen
        fr = 0.002 * (base - 6.0) + 0.060 * sig + rnd.gauss(0, 0.02)
        rows.append({
            "date": "2026-01-01", "ticker": "SYN",
            "base_score": round(base, 2),
            "adjusts": {"sector_rs": sig, "insider": noise1, "news": noise2},
            "forward_return": round(fr, 4),
            "regime_ok": rnd.random() > 0.3,
        })
    return rows


def selftest() -> int:
    panel = make_synthetic()
    rep = run_ablation(panel, top_pct=20.0)
    v = rep["verdicts"]
    ok = True
    # sector_rs muss als KEEP erkannt werden, news/insider nicht als KEEP
    if v["sector_rs"]["verdict"] != "KEEP":
        print(f"FAIL: sector_rs sollte KEEP sein, ist {v['sector_rs']['verdict']}"); ok = False
    for noise in ("insider", "news"):
        if v[noise]["verdict"] == "KEEP":
            print(f"FAIL: {noise} (Rauschen) faelschlich KEEP"); ok = False
    # FULL sollte BASE in der Top-K-Expectancy schlagen (weil Signal drin ist)
    if rep["configs"]["FULL"]["topk_avg_ret"] <= rep["configs"]["BASE"]["topk_avg_ret"]:
        print("WARN: FULL schlaegt BASE nicht, Synthetik-Signal zu schwach?")
    print("Verdikte:", {k: w["verdict"] for k, w in v.items()})
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="H2 Layer-Ablation Harness")
    ap.add_argument("--panel", help="Pfad zum Panel (JSON oder CSV)")
    ap.add_argument("--top-pct", type=float, default=20.0)
    ap.add_argument("--out", default=None, help="JSON-Output-Pfad")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.panel:
        ap.error("--panel erforderlich (oder --selftest)")
    try:
        panel = load_panel(Path(args.panel))
    except FileNotFoundError:
        print(f"[layer_ablation] panel file not found: {args.panel}. Build it first with "
              "python3 tools/build_ablation_panel.py --start YYYY-MM-DD --end YYYY-MM-DD "
              f"--out {args.panel} (it writes no file when the scan archive has no rows). "
              "Exit code 2.", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as e:
        print(f"[layer_ablation] panel file is not readable: {args.panel}: {e}. Exit code 2.",
              file=sys.stderr)
        return 2
    if not panel:
        print(f"[layer_ablation] panel {args.panel} has no rows. Exit code 2.", file=sys.stderr)
        return 2
    rep = run_ablation(panel, top_pct=args.top_pct)
    sweep = threshold_sweep(panel)
    print(render(rep, sweep))
    out = Path(args.out) if args.out else Path(f"tools/layer_ablation_{datetime.now(timezone.utc).replace(tzinfo=None):%Y-%m-%d}.json")
    out.write_text(json.dumps({"report": rep, "sweep": sweep}, indent=2, default=str), encoding="utf-8")
    print(f"\n[+] JSON: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
