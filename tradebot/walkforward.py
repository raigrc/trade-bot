"""Honest evaluation: walk-forward analysis, Monte-Carlo, parameter sensitivity.

A single in-sample backtest is marketing. The stitched out-of-sample
walk-forward curve is reality. Monte-Carlo on trade order/outcomes gives a
distribution of drawdowns (size risk against the 95th percentile, not the lucky
historical path). Parameter sensitivity finds robust plateaus, not lonely
optimisation spikes.
"""

from __future__ import annotations

import argparse
import itertools
import logging
from dataclasses import dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import _load_parquet, run_backtest
from .config import BotConfig, load_config
from .metrics import Metrics, compute_metrics
from .types import Trade

log = logging.getLogger(__name__)
_DAY_MS = 86_400_000

# Small grids on purpose — robustness beats over-optimisation. Round numbers.
DEFAULT_GRIDS: dict[str, dict[str, list]] = {
    "trend": {"ema_fast": [10, 20], "ema_slow": [50, 100], "adx_min": [20, 25]},
    "mean_reversion": {"rsi_oversold": [25, 30], "bb_std": [2.0, 2.5], "adx_max": [20, 25]},
    "breakout": {"entry_length": [20, 40], "exit_length": [10, 20], "stop_atr_mult": [2.0, 3.0]},
    "regime_router": {},
    # Pre-registered TSMom grid — deliberately small (<=6 combos) to avoid the
    # multiple-testing trap. lookback in days (converted to 4h bars in-strategy);
    # one optional regime gate (daily close > daily SMA-200).
    "tsmom": {"lookback_days": [14, 21, 28], "require_htf_uptrend": [False, True]},
}


# --------------------------------------------------------------------------- #
# go/no-go gate (backtest -> paper)
# --------------------------------------------------------------------------- #
def backtest_to_paper_gate(m: Metrics) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if m.expectancy_pct <= 0:
        reasons.append(f"expectancy {m.expectancy_pct:.3%} <= 0 after costs")
    if m.profit_factor < 1.3:
        reasons.append(f"profit factor {m.profit_factor:.2f} < 1.3")
    if m.calmar < 0.5:
        reasons.append(f"Calmar {m.calmar:.2f} < 0.5")
    if m.sortino < 1.0:
        reasons.append(f"Sortino {m.sortino:.2f} < 1.0")
    if m.max_drawdown_pct > 0.25:
        reasons.append(f"max drawdown {m.max_drawdown_pct:.1%} > 25%")
    if m.n_trades < 100:
        reasons.append(f"only {m.n_trades} OOS trades (< 100; statistically thin)")
    return (not reasons), reasons


# --------------------------------------------------------------------------- #
# walk-forward
# --------------------------------------------------------------------------- #
@dataclass
class WindowRecord:
    train_start_ms: int
    test_start_ms: int
    test_end_ms: int
    best_params: dict
    train_return: float
    test_return: float
    test_trades: int


@dataclass
class WalkForwardResult:
    symbol: str
    strategy: str
    metrics: Metrics  # on the stitched OOS curve
    windows: list[WindowRecord]
    oos_trades: list[Trade]
    stitched_curve: list[tuple[int, float]]
    wf_efficiency: float


def _combos(grid: dict[str, list]) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def _windows(start_ms: int, end_ms: int, train_ms: int, test_ms: int) -> list[tuple[int, int, int, int]]:
    out = []
    ts = start_ms
    while True:
        train_start, train_end = ts, ts + train_ms
        test_start, test_end = train_end, min(train_end + test_ms, end_ms)
        if test_start >= end_ms:
            break
        out.append((train_start, train_end, test_start, test_end))
        ts += test_ms
        if test_end >= end_ms:
            break
    return out


def walk_forward(
    config: BotConfig,
    symbol: str,
    strategy_name: str,
    grid: Optional[dict] = None,
    train_days: int = 540,
    test_days: int = 180,
    min_trades: int = 5,
    objective: str = "calmar",
) -> WalkForwardResult:
    grid = DEFAULT_GRIDS.get(strategy_name, {}) if grid is None else grid
    combos = _combos(grid)
    df = _load_parquet(config.data_dir, symbol, config.timeframe)
    start_ms, end_ms = int(df["open_time"].min()), int(df["open_time"].max())
    windows = _windows(start_ms, end_ms, train_days * _DAY_MS, test_days * _DAY_MS)

    initial = config.initial_equity
    running = initial
    stitched: list[tuple[int, float]] = []
    oos_trades: list[Trade] = []
    records: list[WindowRecord] = []
    wfe_ratios: list[float] = []

    for (trs, tre, tes, tee) in windows:
        # optimise on the train window. Default to the first combo (not {}) so a
        # single-combo grid (a fixed variant) is always used even if a train
        # window is below min_trades — otherwise it would silently fall back to
        # the strategy defaults and contaminate an A/B comparison.
        best, best_score, best_train = combos[0], -float("inf"), None
        for combo in combos:
            r = run_backtest(config, symbol, strategy_name, params=combo, start_ms=trs, end_ms=tre)
            score = getattr(r.metrics, objective) if r.metrics.n_trades >= min_trades else -float("inf")
            if score > best_score:
                best, best_score, best_train = combo, score, r
        # trade the chosen params, untouched, on the OOS window
        rt = run_backtest(config, symbol, strategy_name, params=best, start_ms=tes, end_ms=tee)
        scale = running / initial
        stitched.extend((ms, eq * scale) for ms, eq in rt.equity_curve)
        oos_trades.extend(replace(t, pnl=t.pnl * scale) for t in rt.trades)
        running = rt.metrics.final_equity * scale

        train_ret = best_train.metrics.total_return_pct if best_train else 0.0
        records.append(
            WindowRecord(trs, tes, tee, best, train_ret, rt.metrics.total_return_pct, rt.metrics.n_trades)
        )
        if train_ret > 0:
            wfe_ratios.append(rt.metrics.total_return_pct / train_ret)

    metrics = compute_metrics(stitched, oos_trades, config.timeframe, initial, total_bars=len(stitched))
    wfe = float(np.mean(wfe_ratios)) if wfe_ratios else 0.0
    return WalkForwardResult(symbol, strategy_name, metrics, records, oos_trades, stitched, wfe)


# --------------------------------------------------------------------------- #
# Monte-Carlo (bootstrap trade outcomes)
# --------------------------------------------------------------------------- #
def monte_carlo(trades: list[Trade], initial_equity: float, n_sims: int = 2000, seed: int = 7) -> dict:
    pnls = np.array([t.pnl for t in trades], dtype="float64")
    if len(pnls) < 2:
        return {"n_trades": len(pnls)}
    rng = np.random.default_rng(seed)
    mdds, finals = np.empty(n_sims), np.empty(n_sims)
    n = len(pnls)
    for i in range(n_sims):
        sample = rng.choice(pnls, size=n, replace=True)  # bootstrap
        eq = initial_equity + np.cumsum(sample)
        peak = np.maximum.accumulate(eq)
        mdds[i] = float(np.max((peak - eq) / peak))
        finals[i] = float(eq[-1])
    return {
        "n_trades": n,
        "mdd_median": float(np.median(mdds)),
        "mdd_p95": float(np.percentile(mdds, 95)),
        "final_median": float(np.median(finals)),
        "final_p05": float(np.percentile(finals, 5)),
        "prob_loss": float(np.mean(finals < initial_equity)),
    }


# --------------------------------------------------------------------------- #
# parameter sensitivity
# --------------------------------------------------------------------------- #
def parameter_sensitivity(
    config: BotConfig, symbol: str, strategy_name: str, param: str, values: list
) -> list[dict]:
    out = []
    for v in values:
        r = run_backtest(config, symbol, strategy_name, params={param: v})
        m = r.metrics
        out.append(
            {"value": v, "calmar": m.calmar, "profit_factor": m.profit_factor,
             "max_dd": m.max_drawdown_pct, "trades": m.n_trades, "total_return": m.total_return_pct}
        )
    return out


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Walk-forward + Monte-Carlo evaluation.")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--train-days", type=int, default=540)
    ap.add_argument("--test-days", type=int, default=180)
    args = ap.parse_args()

    symbol = args.symbol or cfg.symbols[0]
    strat = args.strategy or cfg.strategy.name

    print(f"\nWalk-forward: {strat} on {symbol} ({cfg.timeframe})  "
          f"train={args.train_days}d test={args.test_days}d")
    wf = walk_forward(cfg, symbol, strat, train_days=args.train_days, test_days=args.test_days)

    print(f"\n  {'OOS window':<24}{'best params':<38}{'train%':>8}{'test%':>8}{'trades':>7}")
    for w in wf.windows:
        a = pd.to_datetime(w.test_start_ms, unit="ms").date()
        b = pd.to_datetime(w.test_end_ms, unit="ms").date()
        params = ",".join(f"{k}={v}" for k, v in w.best_params.items()) or "(defaults)"
        print(f"  {str(a)+'->'+str(b):<24}{params:<38}{w.train_return:>7.1%}{w.test_return:>8.1%}{w.test_trades:>7}")

    print("\n== Stitched OUT-OF-SAMPLE metrics (the honest estimate) ==")
    print(wf.metrics.render())
    print(f"  Walk-forward efficiency : {wf.wf_efficiency:.2f}")

    mc = monte_carlo(wf.oos_trades, cfg.initial_equity)
    if mc.get("n_trades", 0) >= 2:
        print("\n== Monte-Carlo (bootstrap, OOS trades) ==")
        print(f"  Max drawdown  median / p95 : {mc['mdd_median']:.1%} / {mc['mdd_p95']:.1%}")
        print(f"  Final equity  median / p05 : {mc['final_median']:.0f} / {mc['final_p05']:.0f}")
        print(f"  Probability of net loss    : {mc['prob_loss']:.1%}")

    passed, reasons = backtest_to_paper_gate(wf.metrics)
    print("\n== GO / NO-GO (backtest -> paper) ==")
    if passed:
        print("  PASS — eligible to advance to paper trading on the testnet.")
    else:
        print("  NO-GO — do NOT advance. Failing gates:")
        for r in reasons:
            print(f"    - {r}")
        print("  (Capital-preserving decision: this configuration is not tradeable as-is.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
