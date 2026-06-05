"""Regime detection + router.

The regime filter is the switch that prevents running mean-reversion in a trend
(how MR bots blow up) or trend-following in dead chop. ADX is the primary
detector with a dead-band and hysteresis — the single most important
anti-whipsaw mechanism:

  ADX > adx_trend  -> TRENDING  (route to trend-following)
  ADX < adx_range  -> RANGING   (route to mean-reversion)
  in between        -> NEUTRAL   (stay flat — cash is a position)

Hysteresis: a new regime must persist `hysteresis_bars` closes before we switch,
so we don't flip strategies (and pay fees) on every ADX wiggle.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .. import indicators as ind
from ..enums import Regime, Side
from ..types import Signal
from .base import Strategy, StrategyContext
from .mean_reversion import MeanReversionStrategy
from .trend import TrendStrategy


def detect_regime(window: pd.DataFrame, adx_period: int, adx_trend: float, adx_range: float) -> Regime:
    if len(window) < adx_period + 3:
        return Regime.NEUTRAL
    adx_val = ind.adx(window["high"], window["low"], window["close"], adx_period).iloc[-1]
    if adx_val is None or pd.isna(adx_val):
        return Regime.NEUTRAL
    if adx_val >= adx_trend:
        return Regime.TRENDING
    if adx_val < adx_range:
        return Regime.RANGING
    return Regime.NEUTRAL


class RegimeRouter(Strategy):
    name = "regime_router"
    default_params = {
        "adx_period": 14,
        "adx_trend": 25,
        "adx_range": 20,
        "hysteresis_bars": 2,
        "warmup_bars": 210,
    }

    def __init__(self, symbol: str, timeframe: str, params: dict | None = None) -> None:
        super().__init__(symbol, timeframe, params)
        sub = self.params
        self.trend = TrendStrategy(symbol, timeframe, sub.get("trend"))
        self.mr = MeanReversionStrategy(symbol, timeframe, sub.get("mean_reversion"))
        self.warmup_bars = max(self.warmup_bars, self.trend.warmup_bars, self.mr.warmup_bars)
        self._regime = Regime.NEUTRAL
        self._cand = Regime.NEUTRAL
        self._cand_count = 0
        self._active: Optional[Strategy] = None
        self._was_in_pos = False

    def _update_regime(self, window: pd.DataFrame) -> None:
        raw = detect_regime(
            window, self.params["adx_period"], self.params["adx_trend"], self.params["adx_range"]
        )
        if raw == self._regime:
            self._cand_count = 0
            return
        if raw == self._cand:
            self._cand_count += 1
        else:
            self._cand, self._cand_count = raw, 1
        if self._cand_count >= self.params["hysteresis_bars"]:
            self._regime = raw
            self._cand_count = 0

    def on_bar(self, ctx: StrategyContext) -> Optional[Signal]:
        self._update_regime(ctx.window)

        in_pos = ctx.position is not None
        if self._was_in_pos and not in_pos:
            self._active = None  # position closed -> free the active manager
        self._was_in_pos = in_pos

        if in_pos:
            manager = self._active or (self.trend if self._regime == Regime.TRENDING else self.mr)
            return manager.on_bar(ctx)

        # flat: enter only via the regime-appropriate strategy
        if self._regime == Regime.TRENDING:
            sig = self.trend.on_bar(ctx)
            chosen = self.trend
        elif self._regime == Regime.RANGING:
            sig = self.mr.on_bar(ctx)
            chosen = self.mr
        else:
            return None  # NEUTRAL dead-band: stay flat
        if sig is not None and sig.side == Side.BUY:
            self._active = chosen
        return sig

    def state_dict(self) -> dict:
        return {"regime": self._regime.value}

    def load_state(self, d: dict) -> None:
        if "regime" in d:
            self._regime = Regime(d["regime"])
