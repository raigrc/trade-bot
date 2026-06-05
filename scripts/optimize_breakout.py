"""Honest A/B test of breakout false-breakout filters.

Each variant is evaluated by WALK-FORWARD (stitched out-of-sample) on BTC and
ETH with FIXED params (no per-window optimisation) so the comparison isolates
the filter's effect and minimises overfitting. A variant only matters if it
improves OOS on BOTH symbols and moves toward the go/no-go gate.

    python -m scripts.optimize_breakout                      # run all variants, BTC + ETH
    python -m scripts.optimize_breakout --variant vol_adx --symbol BTC/USDT --json   # one cell (for agents)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tradebot.config import load_config
from tradebot.walkforward import backtest_to_paper_gate, monte_carlo, walk_forward

# theory-grounded false-breakout filters (each addresses a known failure mode)
VARIANTS: dict[str, dict] = {
    "baseline": {},
    "vol": {"volume_mult": 1.3},                                  # participation confirmation
    "adx": {"adx_min": 25},                                       # only break out of a real trend
    "htf_slope": {"htf_slope": True},                             # daily trend actually rising
    "vol_adx": {"volume_mult": 1.3, "adx_min": 25},
    "vol_slope": {"volume_mult": 1.3, "htf_slope": True},
    "all": {"volume_mult": 1.3, "adx_min": 25, "htf_slope": True},
}
SYMBOLS = ["BTC/USDT", "ETH/USDT"]


def run_one(cfg, symbol: str, params: dict, train_days: int, test_days: int) -> dict:
    grid = {k: [v] for k, v in params.items()} or None  # single fixed combo
    wf = walk_forward(cfg, symbol, "breakout", grid=grid, train_days=train_days, test_days=test_days)
    m = wf.metrics
    gate_pass, reasons = backtest_to_paper_gate(m)
    mc = monte_carlo(wf.oos_trades, cfg.initial_equity)
    return {
        "symbol": symbol, "params": params,
        "ret": round(m.total_return_pct, 4), "mdd": round(m.max_drawdown_pct, 4),
        "calmar": round(m.calmar, 3), "sortino": round(m.sortino, 3),
        "pf": round(m.profit_factor, 3), "trades": m.n_trades,
        "expectancy_pct": round(m.expectancy_pct, 5), "wfe": round(wf.wf_efficiency, 3),
        "mc_loss_prob": mc.get("prob_loss"), "mc_mdd_p95": mc.get("mdd_p95"),
        "gate_pass": gate_pass, "gate_reasons": reasons,
    }


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Walk-forward A/B test of breakout filters.")
    ap.add_argument("--variant", default=None, choices=list(VARIANTS))
    ap.add_argument("--symbol", default=None, choices=SYMBOLS)
    ap.add_argument("--train-days", type=int, default=540)
    ap.add_argument("--test-days", type=int, default=180)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.variant and args.symbol:  # single cell (parallel agent invocation)
        r = run_one(cfg, args.symbol, VARIANTS[args.variant], args.train_days, args.test_days)
        r["variant"] = args.variant
        print(json.dumps(r))
        return 0

    results = []
    hdr = f"{'variant':<10}{'symbol':<10}{'ret':>8}{'MDD':>7}{'Calmar':>8}{'Sortino':>8}{'PF':>6}{'trades':>7}{'loss%':>7}  gate"
    print(hdr)
    print("-" * len(hdr))
    for name, params in VARIANTS.items():
        for sym in SYMBOLS:
            r = run_one(cfg, sym, params, args.train_days, args.test_days)
            r["variant"] = name
            results.append(r)
            lp = r["mc_loss_prob"]
            print(f"{name:<10}{sym:<10}{r['ret']:>7.1%}{r['mdd']:>7.1%}{r['calmar']:>8.2f}"
                  f"{r['sortino']:>8.2f}{r['pf']:>6.2f}{r['trades']:>7}"
                  f"{(lp if lp is not None else 0):>7.0%}  {'PASS' if r['gate_pass'] else 'no-go'}")
    out = Path(cfg.reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "breakout_opt.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n-> {out / 'breakout_opt.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
