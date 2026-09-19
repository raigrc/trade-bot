"""Fetch the current perpetual funding rate from Binance via ccxt.

Funding rate is a periodic payment between longs and shorts on perpetual
futures.  High positive funding = crowded longs = contrarian bearish signal;
high negative funding = crowded shorts = contrarian bullish signal.

This is PUBLIC information — no API keys required.  We only read the rate,
never place orders.
"""

from __future__ import annotations

import logging
from typing import Optional

import ccxt
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import AlphaSnapshot

log = logging.getLogger(__name__)

# Transient errors worth retrying
_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.DDoSProtection,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
)

_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(_RETRYABLE),
)

# Cache duration: funding rate settles every 8h; refresh at ~half interval
_CACHE_TTL_MS = 4 * 60 * 60 * 1000  # 4 hours


class FundingRateProvider:
    """Fetches the current perpetual funding rate for a symbol.

    Results are cached for up to 4 hours (funding settles every 8h).
    All failures are non-fatal — returns ``None`` and logs at WARNING.
    """

    def __init__(self) -> None:
        self._ex = ccxt.binance({"enableRateLimit": True})
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (rate, timestamp_ms)

    def fetch(self, symbol: str, now_ms: int) -> Optional[AlphaSnapshot]:
        """Return an ``AlphaSnapshot`` with ``funding_rate`` populated, or ``None``."""
        # Check cache
        cached = self._cache.get(symbol)
        if cached is not None:
            rate, ts = cached
            if now_ms - ts < _CACHE_TTL_MS:
                return AlphaSnapshot(funding_rate=rate, timestamp_ms=ts)

        try:
            rate = self._fetch_rate(symbol)
        except Exception as exc:
            log.warning("FundingRate fetch failed for %s: %s", symbol, exc)
            return None

        self._cache[symbol] = (rate, now_ms)
        return AlphaSnapshot(funding_rate=rate, timestamp_ms=now_ms)

    @_RETRY
    def _fetch_rate(self, symbol: str) -> float:
        """Fetch the raw funding rate from Binance futures.

        Binance funding rate is for perpetual contracts — the rate itself
        is public info that indicates crowd positioning.
        """
        # Use a futures-configured exchange to access funding rate data
        # The funding rate endpoint is public — no auth needed
        funding = self._ex.fetch_funding_rate(symbol)
        rate = float(funding.get("fundingRate", 0.0) or 0.0)
        return rate
