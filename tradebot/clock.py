"""Clock abstraction — the parity-critical seam.

PARITY MANDATE: nothing outside this module may call ``time.time()`` /
``datetime.now()``. In backtest the clock is bar-close time (SimClock); in
live/paper it is wall-clock (LiveClock). Routing all "what time is it now"
through here is what keeps cooldowns, daily resets, and time-stops identical
across backtest and live, and prevents an entire class of look-ahead bugs.

A test (``tests/test_no_lookahead.py``) greps the package to enforce this.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now_ms(self) -> int: ...


class SimClock:
    """Backtest clock — advanced by the engine to each bar's close time."""

    __slots__ = ("_ms",)

    def __init__(self, start_ms: int = 0) -> None:
        self._ms = int(start_ms)

    def now_ms(self) -> int:
        return self._ms

    def set(self, ms: int) -> None:
        self._ms = int(ms)


class LiveClock:
    """Wall-clock for live/paper trading."""

    __slots__ = ()

    def now_ms(self) -> int:
        return int(time.time() * 1000)


def to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def utc_day_start_ms(ms: int) -> int:
    """Epoch-ms of 00:00:00 UTC for the day containing ``ms``."""
    d = to_utc(ms)
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000)
