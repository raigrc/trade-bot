"""Fetch news-based sentiment from the cryptocurrency.cv API.

Returns a score from -1.0 (bearish) to 1.0 (bullish) based on recent
cryptocurrency news headlines.  The API aggregates multiple news sources
and applies NLP sentiment analysis.

This is PUBLIC information — no API key required.  The endpoint returns
a JSON object with a ``sentiment`` field containing the normalized score.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from . import AlphaSnapshot

log = logging.getLogger(__name__)

_API_URL = "https://cryptocurrency.cv/api/ai/sentiment"
_CACHE_TTL_MS = 30 * 60 * 1000  # 30 minutes


class NewsSentimentProvider:
    """Fetches news-based sentiment score from cryptocurrency.cv.

    Returns a score from -1.0 (bearish) to 1.0 (bullish).
    Results are cached for up to 30 minutes.  All failures are non-fatal.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (score, timestamp_ms)
        self._client = httpx.Client(timeout=10.0)

    def fetch(self, symbol: str, now_ms: int) -> Optional[AlphaSnapshot]:
        """Return an ``AlphaSnapshot`` with ``news_sentiment`` populated, or ``None``."""
        # Extract the base asset (e.g. "BTC" from "BTC/USDT")
        asset = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()

        cached = self._cache.get(asset)
        if cached is not None:
            score, ts = cached
            if now_ms - ts < _CACHE_TTL_MS:
                return AlphaSnapshot(news_sentiment=score, timestamp_ms=ts)

        try:
            score = self._fetch_score(asset)
        except Exception as exc:
            log.warning("NewsSentiment fetch failed for %s: %s", asset, exc)
            return None

        self._cache[asset] = (score, now_ms)
        return AlphaSnapshot(news_sentiment=score, timestamp_ms=now_ms)

    def _fetch_score(self, asset: str) -> float:
        """Fetch the current news sentiment score (-1.0 to 1.0) for an asset."""
        resp = self._client.get(_API_URL, params={"asset": asset})
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("sentiment", 0.0))
