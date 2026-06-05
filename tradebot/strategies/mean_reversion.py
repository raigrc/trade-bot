"""Mean-reversion: RSI + Bollinger, gated to RANGING markets inside an uptrend.

This is the strategy most likely to blow up, so it gets the strictest gate. In
a strong downtrend price rides the lower band down while RSI stays "oversold" —
a naive bot averages into a collapse. Defenses: only trade when ADX is LOW (not
trending), only buy dips ABOVE the HTF EMA-200 (broader uptrend), and ALWAYS a
hard ATR stop. NEVER average down / martingale.

Entry: close <= lower Bollinger band AND RSI oversold AND ADX < threshold AND
price > HTF EMA-200. Exit: revert to the mid band, or RSI back above 50, or a
time-stop (mean-reversion that doesn't revert quickly = regime change).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .. import indicators as ind
from ..enums import Side
from ..types import Signal
from .base import Strategy, StrategyContext


def _nan(*vals: float) -> bool:
    return any(v is None or pd.isna(v) for v in vals)


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    default_params = {
        "bb_length": 20,
        "bb_std": 2.0,
        "rsi_length": 14,
        "rsi_oversold": 30,
        "rsi_exit": 50,
        "adx_period": 14,
        "adx_max": 20,  # only in NON-trending markets
        "htf_ema": 200,
        "atr_period": 14,
        "stop_atr_mult": 1.5,
        "time_stop_bars": 24,
        "warmup_bars": 210,
    }

    def on_bar(self, ctx: StrategyContext) -> Optional[Signal]:
        p = self.params
        w = ctx.window
        close = w["close"]
        if len(close) < max(p["bb_length"], p["rsi_length"], p["adx_period"]) + 3:
            return None

        rsi_val = ind.rsi(close, p["rsi_length"]).iloc[-1]
        bands = ind.bbands(close, p["bb_length"], p["bb_std"])
        lower, mid = bands.lower.iloc[-1], bands.mid.iloc[-1]
        price = ctx.bar.close
        if _nan(rsi_val, lower, mid):
            return None

        # ---- exit: revert to mid band / RSI recovered / time stop -----------
        if ctx.position is not None:
            if price >= mid or rsi_val >= p["rsi_exit"]:
                return Signal(side=Side.SELL, reason="reverted to mean")
            if ctx.position.bars_held >= p["time_stop_bars"]:
                return Signal(side=Side.SELL, reason="time stop (no reversion)")
            return None

        # ---- entry: oversold dip in a non-trending uptrend ------------------
        if not (price <= lower and rsi_val <= p["rsi_oversold"]):
            return None
        adx_val = ind.adx(w["high"], w["low"], close, p["adx_period"]).iloc[-1]
        atr_val = ind.atr(w["high"], w["low"], close, p["atr_period"]).iloc[-1]
        if _nan(adx_val, atr_val) or atr_val <= 0:
            return None
        if adx_val >= p["adx_max"]:  # trending — do NOT fade
            return None

        htf = ctx.htf_window
        if len(htf) < p["htf_ema"]:
            return None
        htf_ema_val = ind.ema(htf["close"], p["htf_ema"]).iloc[-1]
        if _nan(htf_ema_val) or price <= htf_ema_val:  # only dips inside an uptrend
            return None

        stop = price - p["stop_atr_mult"] * atr_val
        return Signal(
            side=Side.BUY,
            reason=f"bb-lower + rsi={rsi_val:.0f}, adx={adx_val:.0f}",
            stop=stop,
            target=mid,  # target the mean
        )
