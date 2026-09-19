"""Strategy base class and the context handed to it each bar.

A Strategy is PURE with respect to time and execution: it only sees a rolling
window of CLOSED bars (ending at ``ctx.bar``), a read-only position, and a
clock value that equals the bar close in BOTH backtest and live. It returns
INTENT (a Signal) — it never sizes orders, touches the exchange, or reads the
wall-clock. Indicators are computed here on ``ctx.window`` (never precomputed
over full history) so backtest and live results are identical.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import pandas as pd

from ..alpha import AlphaSnapshot
from ..types import Bar, Position, Signal


@dataclass(frozen=True, slots=True)
class StrategyContext:
    bar: Bar  # last CLOSED trading-tf bar
    window: pd.DataFrame  # trailing trading-tf OHLCV, ending at `bar`
    htf_window: pd.DataFrame  # trailing HTF OHLCV, only bars closed as of `bar`
    position: Optional[Position]  # current position (read-only view)
    clock_ms: int  # == bar.close_time_ms in backtest AND live
    params: Mapping[str, Any]
    alpha: Optional[AlphaSnapshot] = None  # advisory micro-structure signals


class Strategy(ABC):
    name: str = "base"
    default_params: dict[str, Any] = {}

    def __init__(self, symbol: str, timeframe: str, params: dict | None = None) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.params: dict[str, Any] = {**self.default_params, **(params or {})}
        self.warmup_bars = int(self.params.get("warmup_bars", 210))

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> Optional[Signal]:
        """Return a Signal (BUY=enter, SELL=exit) or None to hold."""

    # optional internal state persistence (for crash recovery in live)
    def state_dict(self) -> dict:
        return {}

    def load_state(self, d: dict) -> None:  # noqa: B027
        pass
