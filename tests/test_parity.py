"""Determinism / parity: identical inputs -> identical outputs.

Determinism is the prerequisite for backtest<->live parity. (Full live parity
is exercised in M4 against the testnet.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tradebot.backtest import run_backtest, synthetic_market_info
from tradebot.clock import SimClock
from tradebot.config import BotConfig
from tradebot.data import ParquetFeed
from tradebot.engine import Engine
from tradebot.execution import SimulatedExecution
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskManager
from tradebot.strategies.trend import TrendStrategy
from tests.conftest import make_ohlcv_df


def _run(df, htf, initial=10_000.0):
    cfg = BotConfig()
    strat = TrendStrategy("BTC/USDT", "4h")
    feed = ParquetFeed("BTC/USDT", "4h", df, htf, "1d", warmup=strat.warmup_bars)
    market = synthetic_market_info("BTC/USDT")
    eng = Engine(
        "BTC/USDT",
        SimClock(),
        strat,
        RiskManager(cfg.risk),
        SimulatedExecution(cfg.risk, market),
        Portfolio(initial, "BTC/USDT"),
        market,
    )
    eng.run(feed)
    return eng


def test_engine_is_deterministic():
    closes = [100 + 0.3 * i + (5 if (i // 20) % 2 else -5) for i in range(500)]
    df = make_ohlcv_df(closes, "4h")
    htf = make_ohlcv_df([100 + i for i in range(300)], "1d")

    a = _run(df, htf)
    b = _run(df, htf)

    assert a.equity_curve == b.equity_curve
    assert [t.pnl for t in a.portfolio.closed_trades] == [t.pnl for t in b.portfolio.closed_trades]
    assert a.rejections == b.rejections


DATA = Path("data/BTC_USDT_4h.parquet")


@pytest.mark.skipif(not DATA.exists(), reason="run scripts.fetch_data first")
def test_real_backtest_deterministic():
    cfg = BotConfig()
    r1 = run_backtest(cfg, "BTC/USDT", "trend")
    r2 = run_backtest(cfg, "BTC/USDT", "trend")
    assert r1.metrics.final_equity == r2.metrics.final_equity
    assert r1.metrics.n_trades == r2.metrics.n_trades
    assert [t.pnl for t in r1.trades] == [t.pnl for t in r2.trades]
