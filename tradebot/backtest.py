"""Backtest assembler. Contains NO loop — it wires the simulated feed / clock /
execution into the one shared Engine, runs it, and reports metrics.

    python -m tradebot.backtest
    python -m tradebot.backtest --symbol ETH/USDT --strategy trend --start 2021-01-01
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from .clock import SimClock
from .config import BotConfig, load_config
from .data import ParquetFeed, iso_to_ms
from .engine import Engine, MultiEngine
from .execution import SimulatedExecution
from .metrics import Metrics, compute_metrics
from .portfolio import Portfolio
from .risk import RiskManager, RiskState
from .strategies import build_strategy
from .types import MarketInfo, Trade

log = logging.getLogger(__name__)


def synthetic_market_info(symbol: str, quote: str = "USDT") -> MarketInfo:
    """Binance-like spot filters for the simulated path (real ones used live)."""
    base = symbol.split("/")[0]
    return MarketInfo(
        symbol=symbol,
        is_spot=True,
        base=base,
        quote=quote,
        tick_size=0.01,
        step_size=1e-6,
        min_qty=1e-6,
        max_qty=1e9,
        min_notional=10.0,
    )


def _load_parquet(data_dir: str, symbol: str, timeframe: str) -> pd.DataFrame:
    path = Path(data_dir) / f"{symbol.replace('/', '_')}_{timeframe}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"No cached data at {path}. Run:  python -m scripts.fetch_data "
            f"--symbols {symbol} --timeframes {timeframe}"
        )
    return pd.read_parquet(path)


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    metrics: Metrics
    trades: list[Trade]
    equity_curve: list[tuple[int, float]]
    rejections: dict[str, int]


def run_backtest(
    config: BotConfig,
    symbol: Optional[str] = None,
    strategy_name: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    risk_state: Optional[RiskState] = None,
    params: Optional[dict] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> BacktestResult:
    symbol = symbol or config.symbols[0]
    strategy_name = strategy_name or config.strategy.name
    df = _load_parquet(config.data_dir, symbol, config.timeframe)
    htf_df = _load_parquet(config.data_dir, symbol, config.htf_timeframe)

    merged_params = {**config.strategy.params, **(params or {})}
    strategy = build_strategy(strategy_name, symbol, config.timeframe, merged_params)
    if start_ms is None:
        start_ms = iso_to_ms(start) if start else (iso_to_ms(config.backtest_start) if config.backtest_start else None)
    if end_ms is None:
        end_ms = iso_to_ms(end) if end else (iso_to_ms(config.backtest_end) if config.backtest_end else None)

    feed = ParquetFeed(
        symbol=symbol,
        timeframe=config.timeframe,
        df=df,
        htf_df=htf_df,
        htf_timeframe=config.htf_timeframe,
        warmup=strategy.warmup_bars,
        iter_start_ms=start_ms,
        end_ms=end_ms,
    )
    market = synthetic_market_info(symbol, config.quote_currency)
    risk = RiskManager(config.risk, risk_state)
    execution = SimulatedExecution(config.risk, market)
    portfolio = Portfolio(config.initial_equity, symbol, config.quote_currency)
    engine = Engine(symbol, SimClock(), strategy, risk, execution, portfolio, market)

    engine.run(feed)

    metrics = compute_metrics(
        engine.equity_curve,
        portfolio.closed_trades,
        config.timeframe,
        config.initial_equity,
        total_bars=engine.bars_processed,
    )
    return BacktestResult(
        symbol=symbol,
        strategy=strategy_name,
        metrics=metrics,
        trades=portfolio.closed_trades,
        equity_curve=engine.equity_curve,
        rejections=engine.rejections,
    )


@dataclass
class MultiBacktestResult:
    """Aggregated result from a multi-symbol backtest."""

    symbols: list[str]
    strategy: str
    per_symbol: dict[str, BacktestResult]
    equity_curve: list[tuple[int, float]]  # portfolio-level, aligned by time
    total_bars: int
    combined_rejections: dict[str, int]


def run_multi_backtest(
    config: BotConfig,
    symbols: list[str],
    strategy_name: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    risk_state: Optional[RiskState] = None,
    params: Optional[dict] = None,
) -> MultiBacktestResult:
    """Run a multi-symbol backtest with time-aligned feeds.

    Creates one ``Engine`` per symbol, wraps them in a ``MultiEngine``, and
    runs all feeds in lockstep by ``close_time_ms``.  Each symbol gets its own
    independent risk manager, execution layer, and portfolio — the
    ``MultiEngine`` only orchestrates and aggregates.
    """
    strategy_name = strategy_name or config.strategy.name
    merged_params = {**config.strategy.params, **(params or {})}

    start_ms = (
        iso_to_ms(start)
        if start
        else (iso_to_ms(config.backtest_start) if config.backtest_start else None)
    )
    end_ms = (
        iso_to_ms(end)
        if end
        else (iso_to_ms(config.backtest_end) if config.backtest_end else None)
    )

    engines: dict[str, Engine] = {}
    feeds: dict[str, ParquetFeed] = {}

    for symbol in symbols:
        df = _load_parquet(config.data_dir, symbol, config.timeframe)
        htf_df = _load_parquet(config.data_dir, symbol, config.htf_timeframe)
        strategy = build_strategy(strategy_name, symbol, config.timeframe, merged_params)

        feeds[symbol] = ParquetFeed(
            symbol=symbol,
            timeframe=config.timeframe,
            df=df,
            htf_df=htf_df,
            htf_timeframe=config.htf_timeframe,
            warmup=strategy.warmup_bars,
            iter_start_ms=start_ms,
            end_ms=end_ms,
        )
        market = synthetic_market_info(symbol, config.quote_currency)
        risk = RiskManager(config.risk, RiskState())
        execution = SimulatedExecution(config.risk, market)
        portfolio = Portfolio(config.initial_equity, symbol, config.quote_currency)
        engines[symbol] = Engine(
            symbol, SimClock(), strategy, risk, execution, portfolio, market
        )

    multi = MultiEngine(engines)
    multi.run(feeds)

    per_symbol: dict[str, BacktestResult] = {}
    for symbol in symbols:
        e = engines[symbol]
        metrics = compute_metrics(
            e.equity_curve,
            e.portfolio.closed_trades,
            config.timeframe,
            config.initial_equity,
            total_bars=e.bars_processed,
        )
        per_symbol[symbol] = BacktestResult(
            symbol=symbol,
            strategy=strategy_name,
            metrics=metrics,
            trades=e.portfolio.closed_trades,
            equity_curve=e.equity_curve,
            rejections=e.rejections,
        )

    return MultiBacktestResult(
        symbols=symbols,
        strategy=strategy_name,
        per_symbol=per_symbol,
        equity_curve=multi.equity_curve,
        total_bars=multi.bars_processed,
        combined_rejections=multi.rejections,
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Run a backtest.")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    res = run_backtest(cfg, args.symbol, args.strategy, args.start, args.end)
    print(f"\nStrategy={res.strategy}  Symbol={res.symbol}  TF={cfg.timeframe}")
    print(res.metrics.render())
    if res.rejections:
        print("\n  risk rejections:")
        for code, count in sorted(res.rejections.items(), key=lambda kv: -kv[1]):
            print(f"    {code:<24}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
