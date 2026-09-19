"""Strategy correctness — focused on the capital-preservation-critical gates."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from tradebot.data import ParquetFeed
from tradebot.enums import Regime, Side
from tradebot.strategies.base import StrategyContext
from tradebot.strategies.mean_reversion import MeanReversionStrategy
from tradebot.strategies.regime import detect_regime
from tradebot.strategies.trend import TrendStrategy
from tradebot.strategies.tsmom import TSMomStrategy
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


# ---------------------------------------------------------------------------
# TSMom new filter tests: volume confirmation & RSI divergence guard
# ---------------------------------------------------------------------------
def _make_df(closes: list[float], volume: float | list[float] = 100.0) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame. Volume can be a scalar or per-bar list."""
    tf = 14_400_000  # 4h
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + 0.5
        lo = min(o, c) - 0.5
        v = volume[i] if isinstance(volume, list) else volume
        rows.append([1_577_836_800_000 + i * tf, o, hi, lo, c, v])
        prev = c
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["open_time"] + tf
    return df


def _tsmom_ctx(
    window: pd.DataFrame,
    position=None,
    alpha=None,
    params: dict | None = None,
) -> StrategyContext:
    from tradebot.data import row_to_bar
    from tradebot.strategies.tsmom import TSMomStrategy

    bar_row = window.iloc[-1]
    bar = row_to_bar(bar_row, "BTC/USDT", "4h")
    p = params or {**TSMomStrategy.default_params}
    return StrategyContext(bar, window, window.iloc[0:0], position, bar.close_time_ms, p, alpha)


def test_tsmom_volume_low_dampens_confidence():
    """Low volume reduces confidence below 1.0 but still emits a BUY."""
    # 250 bars of steady uptrend — trailing return positive
    closes = [100.0 + 0.5 * i for i in range(250)]
    # Bars 0-229: normal volume (200), bars 230-248: moderate (100), bar 249: very low (10)
    # SMA-20 at bar 249 = (19*100 + 10)/20 = 95.5, 0.8*95.5 = 76.4, 10 < 76.4 → dampened
    vol = [200.0] * 230 + [100.0] * 19 + [10.0]
    df = _make_df(closes, vol)
    strat = TSMomStrategy("BTC/USDT", "4h")
    sig = strat.on_bar(_tsmom_ctx(df))
    assert sig is not None
    assert sig.side == Side.BUY
    assert sig.confidence < 1.0


def test_tsmom_volume_normal_full_confidence():
    """Normal volume keeps confidence at 1.0."""
    closes = [100.0 + 0.5 * i for i in range(250)]
    vol = [100.0] * 250
    df = _make_df(closes, vol)
    strat = TSMomStrategy("BTC/USDT", "4h")
    sig = strat.on_bar(_tsmom_ctx(df))
    assert sig is not None
    assert sig.side == Side.BUY
    assert sig.confidence == 1.0


def test_tsmom_rsi_divergence_skips_entry():
    """Price near high but RSI weakening → entry skipped (bearish divergence)."""
    # 230 bars ramping up (RSI very high), then 20 bars slightly declining
    # so RSI drops while price stays near the 20-bar high
    closes = [100.0 + 1.0 * i for i in range(230)] + [
        330.0 - 0.1 * j for j in range(20)
    ]
    df = _make_df(closes)
    strat = TSMomStrategy("BTC/USDT", "4h")
    sig = strat.on_bar(_tsmom_ctx(df))
    assert sig is None, "RSI divergence guard should block entry"


def test_tsmom_rsi_no_divergence_allows_entry():
    """Price near high and RSI also strong → no divergence, entry proceeds."""
    # 250 bars monotonic uptrend — RSI stays pegged near 100, no divergence
    closes = [100.0 + 1.0 * i for i in range(250)]
    df = _make_df(closes)
    strat = TSMomStrategy("BTC/USDT", "4h")
    sig = strat.on_bar(_tsmom_ctx(df))
    assert sig is not None
    assert sig.side == Side.BUY


@pytest.mark.skipif(not DATA.exists(), reason="run scripts.fetch_data first")
def test_emitted_entry_signals_are_well_formed():
    """Every BUY the trend strategy emits on real data must have a sane stop."""
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
