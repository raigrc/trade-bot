"""Donchian breakout — the classic, near-parameterless trend-entry mechanism.

Entry (long): close breaks above the N-bar high AND price is above the HTF
EMA-200. Exit: close breaks below the shorter M-bar low (plus the engine's ATR
trailing stop). Failure mode: false breakouts in ranging markets — a rising-ATR
(volatility-expansion) gate reduces them. Treat this as an alternative
trend-regime entry trigger; don't double up with the EMA trend strategy on the
same symbol.
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


class BreakoutStrategy(Strategy):
    name = "breakout"
    default_params = {
        "entry_length": 20,
        "exit_length": 10,
        "htf_ema": 200,
        "atr_period": 14,
        "stop_atr_mult": 2.0,
        "tp_atr_mult": 0.0,  # 0 => no fixed target, ride the trailing stop
        "require_vol_expansion": True,
        "warmup_bars": 210,
        # --- optional false-breakout filters (default OFF = classic Donchian) ---
        "volume_mult": 0.0,   # >0: require breakout volume > mult * SMA(volume, vol_period)
        "vol_period": 20,
        "adx_min": 0.0,       # >0: require ADX(adx_period) >= adx_min (real trend, not a range poke)
        "adx_period": 14,
        "htf_slope": False,   # require the daily EMA to be RISING, not just price above a flat EMA
        "htf_slope_lookback": 10,
    }

    def on_bar(self, ctx: StrategyContext) -> Optional[Signal]:
        p = self.params
        w = ctx.window
        if len(w) < max(p["entry_length"], p["exit_length"], p["atr_period"]) + 3:
            return None
        high, low, close = w["high"], w["low"], w["close"]
        price = ctx.bar.close

        # ---- exit: break below the M-bar low --------------------------------
        if ctx.position is not None:
            # prior exit_length lows (exclude the current bar to avoid self-trigger)
            exit_low = low.iloc[-(p["exit_length"] + 1) : -1].min()
            if not _nan(exit_low) and price < exit_low:
                return Signal(side=Side.SELL, reason=f"break {p['exit_length']}-bar low")
            return None

        # ---- entry: break above the N-bar high ------------------------------
        prior_high = high.iloc[-(p["entry_length"] + 1) : -1].max()
        if _nan(prior_high) or price <= prior_high:
            return None

        atr_series = ind.atr(high, low, close, p["atr_period"])
        atr_val = atr_series.iloc[-1]
        if _nan(atr_val) or atr_val <= 0:
            return None
        if p["require_vol_expansion"]:
            atr_prev = atr_series.iloc[-2]
            if _nan(atr_prev) or atr_val <= atr_prev:  # require expanding volatility
                return None

        htf = ctx.htf_window
        if len(htf) < p["htf_ema"]:
            return None
        htf_ema = ind.ema(htf["close"], p["htf_ema"])
        htf_ema_val = htf_ema.iloc[-1]
        if _nan(htf_ema_val) or price <= htf_ema_val:
            return None

        # ---- optional false-breakout filters --------------------------------
        if p.get("htf_slope"):  # daily trend must be rising, not flat
            lb = p["htf_slope_lookback"]
            if len(htf_ema) <= lb or _nan(htf_ema.iloc[-1 - lb]) or htf_ema.iloc[-1] <= htf_ema.iloc[-1 - lb]:
                return None
        if p.get("volume_mult", 0.0) > 0:  # breakout needs participation
            vol_sma = ind.sma(w["volume"], p["vol_period"]).iloc[-1]
            if _nan(vol_sma) or vol_sma <= 0 or ctx.bar.volume <= p["volume_mult"] * vol_sma:
                return None
        if p.get("adx_min", 0.0) > 0:  # only break out of a real trend, not a range
            adx_val = ind.adx(high, low, close, p["adx_period"]).iloc[-1]
            if _nan(adx_val) or adx_val < p["adx_min"]:
                return None

        stop = price - p["stop_atr_mult"] * atr_val
        target = price + p["tp_atr_mult"] * atr_val if p["tp_atr_mult"] > 0 else None
        return Signal(
            side=Side.BUY,
            reason=f"donchian {p['entry_length']} breakout",
            stop=stop,
            target=target,
        )
