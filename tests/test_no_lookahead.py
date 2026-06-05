"""No-look-ahead guarantees.

1. Trading-logic modules must not read the wall-clock (only clock.py may).
2. The feed's windows never contain a bar from the future.
3. Mutating FUTURE bars must not change a PAST signal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

import tradebot
from tradebot.data import ParquetFeed
from tradebot.strategies.trend import TrendStrategy
from tradebot.strategies.base import StrategyContext
from tests.conftest import make_ohlcv_df

PKG = Path(tradebot.__file__).parent
# trading-logic modules — these must never touch wall-clock. Plumbing
# (clock.py, exchange.py, data.py) legitimately reads time and is exempt.
SCAN = [
    PKG / "engine.py",
    PKG / "risk.py",
    PKG / "portfolio.py",
    PKG / "execution.py",
    PKG / "indicators.py",
    PKG / "metrics.py",
    *(PKG / "strategies").glob("*.py"),
]
_FORBIDDEN = re.compile(r"time\.time\s*\(|datetime\.now\s*\(|datetime\.utcnow\s*\(")


def test_no_wallclock_in_trading_logic():
    offenders = []
    for path in SCAN:
        text = path.read_text(encoding="utf-8")
        if _FORBIDDEN.search(text):
            offenders.append(path.name)
    assert not offenders, f"wall-clock used in trading logic: {offenders} (route via clock.py)"


def _trend_feed(n_trading=400, n_daily=260):
    closes = [100 + 0.2 * i for i in range(n_trading)]  # gentle uptrend
    df = make_ohlcv_df(closes, timeframe="4h")
    dailies = [100 + i for i in range(n_daily)]
    htf = make_ohlcv_df(dailies, timeframe="1d")
    return df, htf


def test_feed_windows_never_contain_future_bars():
    df, htf = _trend_feed()
    feed = ParquetFeed("BTC/USDT", "4h", df, htf, "1d", warmup=210)
    checked = 0
    for slc in feed:
        ct = slc.bar.close_time_ms
        assert slc.window["close_time"].max() <= ct
        if len(slc.htf_window):
            assert slc.htf_window["close_time"].max() <= ct
        checked += 1
    assert checked > 0


def test_future_mutation_does_not_change_past_signals():
    df, htf = _trend_feed()
    cutoff_idx = 320
    cutoff_ot = int(df["open_time"].iloc[cutoff_idx])

    strat = TrendStrategy("BTC/USDT", "4h")

    def signals_up_to(frame):
        feed = ParquetFeed("BTC/USDT", "4h", frame, htf, "1d", warmup=210)
        out = {}
        for slc in feed:
            if slc.bar.open_time_ms > cutoff_ot:
                continue
            ctx = StrategyContext(slc.bar, slc.window, slc.htf_window, None, slc.bar.close_time_ms, strat.params)
            sig = strat.on_bar(ctx)
            out[slc.bar.open_time_ms] = None if sig is None else (sig.side.value, round(sig.stop or 0, 4))
        return out

    base = signals_up_to(df)

    mutated = df.copy()
    fut = mutated["open_time"] > cutoff_ot
    for col in ("open", "high", "low", "close"):
        mutated.loc[fut, col] = 999_999.0  # wreck the future
    after = signals_up_to(mutated)

    assert base == after and len(base) > 0
