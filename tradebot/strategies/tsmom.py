"""Time-series momentum (TSMom) — the asset's own trailing return predicts its next.

The best-evidenced systematic effect in crypto (Liu & Tsyvinski 2021, RFS;
Moskowitz-Ooi-Pedersen 2012). Rule: go LONG when the trailing N-day close-to-close
return is positive; go FLAT (hold cash) when it turns negative. Long-only spot — we
stay in cash rather than short.

Entry (long): trailing N-day return > 0. Exit: trailing N-day return flips <= 0 (go
to cash); the engine also enforces the mandatory ATR stop and trails it. Optional
regime gate (``require_htf_uptrend``): only enter when the daily close is above its
200-day SMA — reusing the same HTF series the other strategies read.

Failure mode: in choppy/sideways regimes the sign of the trailing return whipsaws,
churning small losses against costs. The HTF gate is the only (pre-registered)
defense; we deliberately do NOT add more knobs (multiple-testing trap — see
docs/breakout_findings.md).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .. import indicators as ind
from ..enums import Side
from ..types import Signal, timeframe_ms
from .base import Strategy, StrategyContext

_DAY_MS = 86_400_000


def _nan(*vals: float) -> bool:
    return any(v is None or pd.isna(v) for v in vals)


class TSMomStrategy(Strategy):
    name = "tsmom"
    default_params = {
        "lookback_days": 28,
        "require_htf_uptrend": False,  # gate: daily close > daily SMA-200
        "htf_sma": 200,
        "atr_period": 14,
        "stop_atr_mult": 1.5,
        "warmup_bars": 210,
    }

    def _lookback_bars(self) -> int:
        """N days expressed in trading-timeframe bars (e.g. 28d -> 168 bars @ 4h)."""
        tf_ms = timeframe_ms(self.timeframe)
        bars_per_day = max(1, _DAY_MS // tf_ms)
        return int(self.params["lookback_days"]) * bars_per_day

    def on_bar(self, ctx: StrategyContext) -> Optional[Signal]:
        p = self.params
        w = ctx.window
        close = w["close"]
        lookback = self._lookback_bars()
        # need lookback+1 closes for close[t]/close[t-lookback], plus ATR headroom
        if len(close) < max(lookback + 1, p["atr_period"] + 3):
            return None

        ref = close.iloc[-1 - lookback]
        if _nan(ref) or ref <= 0:
            return None
        trailing_ret = close.iloc[-1] / ref - 1.0
        price = ctx.bar.close

        # ---- exit: trailing return flipped non-positive (go to cash) --------
        if ctx.position is not None:
            if trailing_ret <= 0:
                return Signal(side=Side.SELL, reason=f"tsmom {p['lookback_days']}d ret <= 0")
            return None

        # ---- entry: positive trailing momentum ------------------------------
        if trailing_ret <= 0:
            return None

        atr_val = ind.atr(w["high"], w["low"], close, p["atr_period"]).iloc[-1]
        if _nan(atr_val) or atr_val <= 0:
            return None

        # optional regime gate: daily close above its 200-day SMA
        if p.get("require_htf_uptrend"):
            htf = ctx.htf_window
            if len(htf) < p["htf_sma"]:
                return None
            htf_sma_val = ind.sma(htf["close"], p["htf_sma"]).iloc[-1]
            htf_close = htf["close"].iloc[-1]
            if _nan(htf_sma_val, htf_close) or htf_close <= htf_sma_val:
                return None

        stop = price - p["stop_atr_mult"] * atr_val
        return Signal(
            side=Side.BUY,
            reason=f"tsmom {p['lookback_days']}d ret={trailing_ret:+.1%}",
            stop=stop,
        )
