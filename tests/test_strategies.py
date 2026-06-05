"""Strategy correctness — focused on the capital-preservation-critical gates."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tradebot.config import BotConfig
from tradebot.data import ParquetFeed
from tradebot.enums import Regime, Side
from tradebot.strategies.base import StrategyContext
from tradebot.strategies.mean_reversion import MeanReversionStrategy
from tradebot.strategies.regime import RegimeRouter, detect_regime
from tradebot.strategies.trend import TrendStrategy
from tests.conftest import make_ohlcv_df


def _ctx(closes, htf_closes, position=None):
    df = make_ohlcv_df(closes, "4h")
    htf = make_ohlcv_df(htf_closes, "1d") if htf_closes else df.iloc[0:0]
    bar_row = df.iloc[-1]
    from tradebot.data import row_to_bar

    bar = row_to_bar(bar_row, "BTC/USDT", "4h")
    return StrategyContext(bar, df, htf, position, bar.close_time_ms, {})


def test_detect_regime_trending_vs_ranging():
    up = make_ohlcv_df([100 + 5 * i for i in range(150)], "4h")
    chop = make_ohlcv_df([100 + 3 * math.sin(i / 3) for i in range(150)], "4h")
    assert detect_regime(up, 14, 25, 20) == Regime.TRENDING
    assert detect_regime(chop, 14, 25, 20) in (Regime.RANGING, Regime.NEUTRAL)


def test_mean_reversion_refuses_to_catch_falling_knife():
    # strong downtrend: oversold + below band, but trending and below HTF -> NEVER enter
    closes = [300 - 1.2 * i for i in range(260)]
    htf = [300 - i for i in range(260)]
    strat = MeanReversionStrategy("BTC/USDT", "4h")
    sig = strat.on_bar(_ctx(closes, htf))
    assert sig is None  # the blow-up guard


def test_trend_does_not_trade_without_htf_history():
    # a clean cross-up setup but with too little HTF history -> no entry
    closes = [100.0] * 200 + [100 + 3 * i for i in range(60)]
    strat = TrendStrategy("BTC/USDT", "4h")
    sig = strat.on_bar(_ctx(closes, htf_closes=[100, 101, 102]))  # < htf_ema bars
    assert sig is None


DATA = Path("data/BTC_USDT_4h.parquet")


@pytest.mark.skipif(not DATA.exists(), reason="run scripts.fetch_data first")
def test_emitted_entry_signals_are_well_formed():
    """Every BUY the trend strategy emits on real data must have a sane stop."""
    cfg = BotConfig()
    import pandas as pd

    df = pd.read_parquet("data/BTC_USDT_4h.parquet")
    htf = pd.read_parquet("data/BTC_USDT_1d.parquet")
    strat = TrendStrategy("BTC/USDT", "4h")
    feed = ParquetFeed("BTC/USDT", "4h", df, htf, "1d", warmup=strat.warmup_bars)

    buys = 0
    for slc in feed:
        ctx = StrategyContext(slc.bar, slc.window, slc.htf_window, None, slc.bar.close_time_ms, strat.params)
        sig = strat.on_bar(ctx)
        if sig is not None and sig.side == Side.BUY:
            buys += 1
            assert sig.stop is not None and sig.stop < slc.bar.close
            if sig.target is not None:
                assert sig.target > slc.bar.close
    assert buys > 0  # the strategy actually fires entries on real history
