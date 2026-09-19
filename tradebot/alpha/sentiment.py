"""Fetch the Bitcoin Fear & Greed Index from alternative.me.

Score 0-100:
    0-25  = extreme fear (contrarian bullish for momentum)
    25-45 = fear
    45-55 = neutral
    55-75 = greed
    75-100 = extreme greed (contrarian bearish for momentum)

The index updates daily.  We cache for 1 hour to avoid hammering the API.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from . import AlphaSnapshot

log = logging.getLogger(__name__)

_API_URL = "https://api.alternative.me/fng/"
_CACHE_TTL_MS = 60 * 60 * 1000  # 1 hour (index updates daily, so 1h is plenty)


class SentimentProvider:
    """Fetches the Bitcoin Fear & Greed Index.

    Results are cached for up to 1 hour.  All failures are non-fatal.
    """

    def __init__(self) -> None:
        self._cache: Optional[tuple[float, float]] = None  # (score, timestamp_ms)
        self._client = httpx.Client(timeout=10.0)

    def fetch(self, symbol: str, now_ms: int) -> Optional[AlphaSnapshot]:
        """Return an ``AlphaSnapshot`` with ``sentiment_score`` populated, or ``None``."""
        # Fear & Greed is BTC-only; ignore other symbols
        if "BTC" not in symbol.upper():
            return None

        if self._cache is not None:
            score, ts = self._cache
            if now_ms - ts < _CACHE_TTL_MS:
                return AlphaSnapshot(sentiment_score=score, timestamp_ms=ts)

        try:
            score = self._fetch_score()
        except Exception as exc:
            log.warning("Sentiment fetch failed: %s", exc)
            return None

        self._cache = (score, now_ms)
        return AlphaSnapshot(sentiment_score=score, timestamp_ms=now_ms)

    def _fetch_score(self) -> float:
        """Fetch the current Fear & Greed score (0-100) from alternative.me."""
        resp = self._client.get(_API_URL)
        resp.raise_for_status()
        data = resp.json()
        # Response shape: {"data": [{"value": "25", "value_classification": "Extreme Fear", ...}]}
        entry = data["data"][0]
        return float(entry["value"])
