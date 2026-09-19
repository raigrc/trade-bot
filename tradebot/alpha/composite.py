"""Composite alpha provider — merges all signal sources into one snapshot.

This is the main entry point for strategies.  It calls all providers in
parallel-ish fashion (sequential but fast) and merges their results.
A failure in any single provider is non-fatal — its field is ``None``.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import AlphaSnapshot
from .funding_rate import FundingRateProvider
from .news_sentiment import NewsSentimentProvider
from .open_interest import OpenInterestProvider
from .sentiment import SentimentProvider

log = logging.getLogger(__name__)


class CompositeAlpha:
    """Merges funding rate, open interest, and sentiment into a single snapshot.

    Usage::

        alpha = CompositeAlpha()
        snap = alpha.fetch("BTC/USDT", now_ms=int(time.time() * 1000))
        # snap may have partial data if one provider failed
    """

    def __init__(
        self,
        funding: Optional[FundingRateProvider] = None,
        open_interest: Optional[OpenInterestProvider] = None,
        sentiment: Optional[SentimentProvider] = None,
        news_sentiment: Optional[NewsSentimentProvider] = None,
    ) -> None:
        self._funding = funding or FundingRateProvider()
        self._open_interest = open_interest or OpenInterestProvider()
        self._sentiment = sentiment or SentimentProvider()
        self._news_sentiment = news_sentiment or NewsSentimentProvider()

    def fetch(self, symbol: str, now_ms: int) -> AlphaSnapshot:
        """Fetch all alpha signals and merge into a single snapshot.

        Each provider is called independently.  If one fails, its field
        is ``None`` — the others still return valid data.
        """
        funding_snap = self._safe_fetch(self._funding, symbol, now_ms, "funding_rate")
        oi_snap = self._safe_fetch(self._open_interest, symbol, now_ms, "open_interest")
        sent_snap = self._safe_fetch(self._sentiment, symbol, now_ms, "sentiment")
        news_snap = self._safe_fetch(self._news_sentiment, symbol, now_ms, "news_sentiment")

        # Merge: take the first available timestamp
        ts = now_ms
        if funding_snap and funding_snap.timestamp_ms:
            ts = funding_snap.timestamp_ms
        elif oi_snap and oi_snap.timestamp_ms:
            ts = oi_snap.timestamp_ms
        elif sent_snap and sent_snap.timestamp_ms:
            ts = sent_snap.timestamp_ms
        elif news_snap and news_snap.timestamp_ms:
            ts = news_snap.timestamp_ms

        return AlphaSnapshot(
            funding_rate=funding_snap.funding_rate if funding_snap else None,
            open_interest_delta=oi_snap.open_interest_delta if oi_snap else None,
            sentiment_score=sent_snap.sentiment_score if sent_snap else None,
            news_sentiment=news_snap.news_sentiment if news_snap else None,
            timestamp_ms=ts,
        )

    @staticmethod
    def _safe_fetch(
        provider: object,
        symbol: str,
        now_ms: int,
        name: str,
    ) -> Optional[AlphaSnapshot]:
        """Fetch from a provider, catching any unexpected errors."""
        try:
            return provider.fetch(symbol, now_ms)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("Alpha provider %s failed unexpectedly: %s", name, exc)
            return None
