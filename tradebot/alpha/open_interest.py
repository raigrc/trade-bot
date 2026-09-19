"""Fetch open interest from Binance futures and compute the 24h delta.

Open interest (OI) represents the total value of outstanding derivative
contracts.  OI delta indicates whether leverage is increasing or unwinding:

- Rising OI + rising price = trend confirmation (bullish)
- Rising OI + falling price = distribution (bearish)
- Falling OI + falling price = capitulation (potential reversal)
- Falling OI + rising price = short covering (may be weak)

This is PUBLIC information — no API keys required.
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

# Cache duration: OI updates frequently, but daily delta is enough for 1d timeframe
_CACHE_TTL_MS = 60 * 60 * 1000  # 1 hour


class OpenInterestProvider:
    """Fetches open interest and computes the 24h delta for a symbol.

    Results are cached for up to 1 hour.  All failures are non-fatal.
    """

    def __init__(self) -> None:
        self._ex = ccxt.binance({"enableRateLimit": True})
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (delta, timestamp_ms)
        # Store previous OI for delta computation
        self._prev_oi: dict[str, tuple[float, float]] = {}  # symbol -> (oi, timestamp_ms)

    def fetch(self, symbol: str, now_ms: int) -> Optional[AlphaSnapshot]:
        """Return an ``AlphaSnapshot`` with ``open_interest_delta`` populated, or ``None``."""
        cached = self._cache.get(symbol)
        if cached is not None:
            delta, ts = cached
            if now_ms - ts < _CACHE_TTL_MS:
                return AlphaSnapshot(open_interest_delta=delta, timestamp_ms=ts)

        try:
            delta = self._fetch_oi_delta(symbol, now_ms)
        except Exception as exc:
            log.warning("OpenInterest fetch failed for %s: %s", symbol, exc)
            return None

        self._cache[symbol] = (delta, now_ms)
        return AlphaSnapshot(open_interest_delta=delta, timestamp_ms=now_ms)

    @_RETRY
    def _fetch_oi_delta(self, symbol: str, now_ms: int) -> float:
        """Compute OI delta as percentage change over ~24h.

        If we don't have a historical reading, returns 0.0 (no signal).
        """
        current_oi = self._fetch_current_oi(symbol)

        # Check if we have a previous reading from ~24h ago
        prev = self._prev_oi.get(symbol)
        if prev is not None:
            prev_oi, prev_ts = prev
            elapsed = now_ms - prev_ts
            # Only compute delta if we have at least 12h of data (be lenient)
            if elapsed >= 12 * 60 * 60 * 1000 and prev_oi > 0:
                delta = (current_oi - prev_oi) / prev_oi * 100.0
                # Store current as new baseline
                self._prev_oi[symbol] = (current_oi, now_ms)
                return delta

        # No previous reading — store current as baseline for next call
        self._prev_oi[symbol] = (current_oi, now_ms)
        return 0.0

    @_RETRY
    def _fetch_current_oi(self, symbol: str) -> float:
        """Fetch current open interest from Binance futures."""
        oi = self._ex.fetch_open_interest(symbol)
        return float(oi.get("openInterestAmount", 0.0) or 0.0)
