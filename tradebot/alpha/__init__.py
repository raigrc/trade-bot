"""Alpha overlay providers for the TSMom strategy.

Each provider fetches a single market micro-structure signal (funding rate,
open interest, sentiment) and returns it as a structured ``AlphaSnapshot``.
Providers are advisory-only — they never block trades; they adjust confidence.

Usage::

    from tradebot.alpha import CompositeAlpha

    alpha = CompositeAlpha()
    snap = alpha.fetch("BTC/USDT", now_ms=int(time.time() * 1000))
    if snap.sentiment_score is not None and snap.sentiment_score > 75:
        ...  # extreme greed — lower confidence
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True, slots=True)
class AlphaSnapshot:
    """Immutable snapshot of alpha signals for a single symbol at a point in time."""

    funding_rate: Optional[float] = None       # perpetual funding rate (8h)
    open_interest_delta: Optional[float] = None  # OI change over 24h (as %)
    sentiment_score: Optional[float] = None      # 0-100 (0=extreme fear, 100=extreme greed)
    news_sentiment: Optional[float] = None       # -1.0 (bearish) to 1.0 (bullish)
    timestamp_ms: int = 0


class AlphaProvider(Protocol):
    """Interface for a single alpha signal source."""

    def fetch(self, symbol: str, now_ms: int) -> Optional[AlphaSnapshot]: ...
