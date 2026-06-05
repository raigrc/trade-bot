"""Trend-following: EMA crossover + higher-timeframe filter + ATR stop.

The core strategy. Crypto's fat right tails reward trend capture: cut losers
fast, let winners run. Expect a LOW win rate (35-45%) with large winners — that
is correct and must not be "fixed" by tightening exits (which kills the edge).

Entry (long-only): fast EMA crosses above slow EMA AND price is above the HTF
EMA-200 AND ADX shows a real trend. Exit: EMA recross down (the engine also
trails an ATR stop). Failure mode: whipsaw in chop — the HTF + ADX gates exist
to suppress it.
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


class TrendStrategy(Strategy):
    name = "trend"
    default_params = {
        "ema_fast": 20,
        "ema_slow": 50,
        "htf_ema": 200,
        "adx_period": 14,
        "adx_min": 25,
        "atr_period": 14,
        "stop_atr_mult": 1.5,
        "tp_atr_mult": 3.0,
        "tp_mode": "atr",  # atr | none
        "warmup_bars": 210,
    }

    def on_bar(self, ctx: StrategyContext) -> Optional[Signal]:
        p = self.params
        w = ctx.window
        close = w["close"]
        if len(close) < max(p["ema_slow"], p["adx_period"], p["atr_period"]) + 3:
            return None

        ef = ind.ema(close, p["ema_fast"])
        es = ind.ema(close, p["ema_slow"])
        ef0, ef1 = ef.iloc[-2], ef.iloc[-1]
        es0, es1 = es.iloc[-2], es.iloc[-1]
        if _nan(ef0, ef1, es0, es1):
            return None

        price = ctx.bar.close

        # ---- exit: EMA recross down -----------------------------------------
        if ctx.position is not None:
            if ef0 >= es0 and ef1 < es1:
                return Signal(side=Side.SELL, reason="ema recross down")
            return None

        # ---- entry: cross up + trend filters --------------------------------
        crossed_up = ef0 <= es0 and ef1 > es1
        if not crossed_up:
            return None

        adx_val = ind.adx(w["high"], w["low"], close, p["adx_period"]).iloc[-1]
        atr_val = ind.atr(w["high"], w["low"], close, p["atr_period"]).iloc[-1]
        if _nan(adx_val, atr_val) or atr_val <= 0:
            return None
        if adx_val < p["adx_min"]:
            return None

        # HTF trend filter — require enough closed daily history; else no trade
        htf = ctx.htf_window
        if len(htf) < p["htf_ema"]:
            return None
        htf_ema_val = ind.ema(htf["close"], p["htf_ema"]).iloc[-1]
        if _nan(htf_ema_val) or price <= htf_ema_val:
            return None

        stop = price - p["stop_atr_mult"] * atr_val
        target = price + p["tp_atr_mult"] * atr_val if p["tp_mode"] == "atr" else None
        return Signal(
            side=Side.BUY,
            reason=f"ema{p['ema_fast']}x{p['ema_slow']} up, adx={adx_val:.0f}",
            stop=stop,
            target=target,
        )
