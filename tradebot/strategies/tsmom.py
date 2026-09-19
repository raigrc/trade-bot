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
        "warmup_bars": 220,
        "volume_sma_period": 20,
        "volume_dry_threshold": 0.8,
        "volume_confidence_mult": 0.6,
        "rsi_period": 14,
        "rsi_lookback": 20,
        "rsi_high_pct": 0.9,
        "price_near_high_pct": 0.01,
    }

    def _lookback_bars(self) -> int:
        """N days expressed in trading-timeframe bars (e.g. 28d -> 168 bars @ 4h)."""
        tf_ms = timeframe_ms(self.timeframe)
        bars_per_day = max(1, _DAY_MS // tf_ms)
        return int(self.params["lookback_days"]) * bars_per_day

    def _volume_confirmation(self, ctx: StrategyContext) -> float:
        """Return confidence multiplier based on volume vs its SMA.

        Low volume (below 0.8 × 20-period SMA) dampens confidence by the
        configured multiplier. Returns a value in (0, 1].
        """
        p = ctx.params if ctx.params else self.params
        vol = ctx.window["volume"]
        sma_len = p["volume_sma_period"]
        if len(vol) < sma_len + 1:
            return 1.0
        vol_sma = ind.sma(vol, sma_len).iloc[-1]
        cur_vol = vol.iloc[-1]
        if _nan(vol_sma, cur_vol) or vol_sma <= 0:
            return 1.0
        if cur_vol < p["volume_dry_threshold"] * vol_sma:
            return p["volume_confidence_mult"]
        return 1.0

    def _rsi_divergence_guard(self, ctx: StrategyContext) -> bool:
        """Return True if bearish RSI divergence detected (should skip entry).

        Checks: price within 1% of 20-bar high AND RSI below 90% of its 20-bar
        high. Classic distribution pattern — price makes new highs on weakening
        momentum.
        """
        p = ctx.params if ctx.params else self.params
        w = ctx.window
        rsi_len = p["rsi_period"]
        lookback = p["rsi_lookback"]
        required = max(rsi_len + 2, lookback + 1)
        if len(w) < required:
            return False
        rsi_vals = ind.rsi(w["close"], rsi_len)
        cur_rsi = rsi_vals.iloc[-1]
        if _nan(cur_rsi):
            return False
        high_col = w["high"]
        price = high_col.iloc[-1]
        recent_high = high_col.iloc[-lookback:].max()
        rsi_high = rsi_vals.iloc[-lookback:].max()
        if _nan(recent_high, rsi_high) or recent_high <= 0 or rsi_high <= 0:
            return False
        price_near = price >= recent_high * (1 - p["price_near_high_pct"])
        rsi_weak = cur_rsi < p["rsi_high_pct"] * rsi_high
        return price_near and rsi_weak

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
        confidence = 1.0
        skip = False

        # ---- alpha filters (funding, sentiment, OI, news) -------------------
        if ctx.alpha is not None:
            a = ctx.alpha
            # crowded longs: funding rate > 0.1%
            if a.funding_rate is not None and a.funding_rate > 0.001:
                skip = True
            # extreme greed: sentiment > 75
            if a.sentiment_score is not None and a.sentiment_score > 75:
                skip = True
            # rapid OI expansion: >5% — potential distribution
            if a.open_interest_delta is not None and abs(a.open_interest_delta) > 5.0:
                confidence *= 0.5
            # bearish news sentiment: score < -0.5
            if a.news_sentiment is not None and a.news_sentiment < -0.5:
                skip = True

            if skip:
                return None

        # ---- volume confirmation (dampens confidence, never skips) ----------
        confidence *= self._volume_confirmation(ctx)

        # ---- RSI divergence guard (skips entry) -----------------------------
        if self._rsi_divergence_guard(ctx):
            return None

        if confidence < 1.0:
            return Signal(
                side=Side.BUY,
                reason=f"tsmom {p['lookback_days']}d ret={trailing_ret:+.1%} (alpha flag)",
                stop=stop,
                confidence=confidence,
            )

        return Signal(
            side=Side.BUY,
            reason=f"tsmom {p['lookback_days']}d ret={trailing_ret:+.1%}",
            stop=stop,
        )
