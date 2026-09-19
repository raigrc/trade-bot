from __future__ import annotations

import pandas as pd
import pytest

from tradebot.config import RiskConfig
from tradebot.types import AccountState, MarketInfo, timeframe_ms


@pytest.fixture
def market() -> MarketInfo:
    return MarketInfo(
        symbol="BTC/USDT",
        is_spot=True,
        base="BTC",
        quote="USDT",
        tick_size=0.01,
        step_size=1e-6,
        min_qty=1e-5,
        max_qty=1e9,
        min_notional=10.0,
    )


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig()


def make_ohlcv_df(
    closes: list[float],
    timeframe: str = "4h",
    start_ms: int = 1_577_836_800_000,  # 2020-01-01
    spread: float = 0.5,
) -> pd.DataFrame:
    """Build a deterministic OHLCV DataFrame from a list of close prices."""
    tf = timeframe_ms(timeframe)
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + spread
        lo = min(o, c) - spread
        rows.append([start_ms + i * tf, o, hi, lo, c, 100.0])
        prev = c
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["open_time"] + tf
    return df


@pytest.fixture
def account_flat() -> AccountState:
    return AccountState(equity=10_000.0, free=10_000.0, positions=())
