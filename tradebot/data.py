"""Market data: historical download + cache, and the DataFeed abstraction.

- ``HistoricalDataFetcher`` downloads real Binance *mainnet* OHLCV (public, no
  API key) and caches it to parquet. Backtests use mainnet history because the
  testnet has no meaningful price history.
- ``ParquetFeed`` replays cached bars as a stream of ``MarketSlice`` for the
  backtest. ``CcxtLiveFeed`` (M4) yields the same ``MarketSlice`` shape from
  live data, so ``Engine.step`` is identical in both modes.

CLOSED-BARS ONLY: ``drop_unclosed`` removes the still-forming candle that
ccxt's ``fetch_ohlcv`` may include as its last row. The HTF window for each
trading bar contains only daily bars that have fully closed as of that bar.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Protocol

import ccxt
import numpy as np
import pandas as pd

from .types import Bar, timeframe_ms

log = logging.getLogger(__name__)

OHLCV_COLS = ["open_time", "open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def iso_to_ms(date_str: str) -> int:
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ohlcv_to_df(rows: list[list[float]], timeframe: str) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=OHLCV_COLS)
    df["open_time"] = df["open_time"].astype("int64")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype("float64")
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    df["close_time"] = df["open_time"] + timeframe_ms(timeframe)
    return df


def drop_unclosed(df: pd.DataFrame, timeframe: str, now_ms: int) -> pd.DataFrame:
    """Drop bars whose close boundary is in the future relative to now_ms."""
    tf = timeframe_ms(timeframe)
    return df[df["open_time"] + tf <= now_ms].reset_index(drop=True)


def validate_gaps(df: pd.DataFrame, timeframe: str) -> list[tuple[int, int]]:
    """Return (prev_open, gap_bars) for each non-contiguous step. Empty == clean."""
    tf = timeframe_ms(timeframe)
    ot = df["open_time"].to_numpy()
    if len(ot) < 2:
        return []
    diffs = np.diff(ot)
    gaps: list[tuple[int, int]] = []
    for i, d in enumerate(diffs):
        if d != tf:
            gaps.append((int(ot[i]), int(round(d / tf))))
    return gaps


def row_to_bar(row: pd.Series, symbol: str, timeframe: str) -> Bar:
    return Bar(
        symbol=symbol,
        open_time_ms=int(row["open_time"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        timeframe=timeframe,
    )


# --------------------------------------------------------------------------- #
# MarketSlice — the unit handed to Engine.step (identical in backtest & live)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MarketSlice:
    bar: Bar  # last CLOSED trading-timeframe bar
    window: pd.DataFrame  # trailing trading-tf OHLCV, ending at `bar`
    htf_window: pd.DataFrame  # trailing HTF OHLCV, only bars closed as of bar.close_time


class DataFeed(Protocol):
    symbol: str
    timeframe: str

    def __iter__(self) -> Iterator[MarketSlice]: ...


# --------------------------------------------------------------------------- #
# historical download + cache
# --------------------------------------------------------------------------- #
class HistoricalDataFetcher:
    """Download real Binance mainnet OHLCV (public) and cache to parquet."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})

    def cache_path(self, symbol: str, timeframe: str) -> Path:
        return self.data_dir / f"{symbol.replace('/', '_')}_{timeframe}.parquet"

    def fetch_range(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: Optional[int] = None
    ) -> pd.DataFrame:
        tf = timeframe_ms(timeframe)
        until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
        cursor = since_ms
        rows: list[list[float]] = []
        limit = 1000
        while cursor < until_ms:
            batch = self._ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit)
            if not batch:
                break
            rows.extend(batch)
            last_open = int(batch[-1][0])
            nxt = last_open + tf
            if nxt <= cursor:
                break
            cursor = nxt
            log.info("  %s %s: %d bars (through %s)", symbol, timeframe, len(rows),
                     datetime.fromtimestamp(last_open / 1000, tz=timezone.utc).date())
            if len(batch) < limit:
                break
        df = ohlcv_to_df(rows, timeframe)
        df = df[df["open_time"] < until_ms].reset_index(drop=True)
        df = drop_unclosed(df, timeframe, until_ms)
        return df

    def load_or_fetch(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        until_ms: Optional[int] = None,
        refresh: bool = False,
    ) -> pd.DataFrame:
        path = self.cache_path(symbol, timeframe)
        if path.exists() and not refresh:
            df = pd.read_parquet(path)
            log.info("Loaded %d cached %s %s bars from %s", len(df), symbol, timeframe, path.name)
            return df
        log.info("Fetching %s %s since %s ...", symbol, timeframe,
                 datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).date())
        df = self.fetch_range(symbol, timeframe, since_ms, until_ms)
        gaps = validate_gaps(df, timeframe)
        if gaps:
            log.warning("%s %s has %d gap(s); largest=%d bars", symbol, timeframe,
                        len(gaps), max(g[1] for g in gaps))
        df.to_parquet(path, index=False)
        log.info("Cached %d %s %s bars -> %s", len(df), symbol, timeframe, path.name)
        return df


# --------------------------------------------------------------------------- #
# backtest feed
# --------------------------------------------------------------------------- #
class ParquetFeed:
    """Replay a cached trading-tf DataFrame as a stream of MarketSlice.

    For each trading bar (after warmup) it provides a trailing trading-tf window
    plus the trailing HTF window of bars that have fully closed by that bar's
    close time (no forming-HTF-candle look-ahead).
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame] = None,
        htf_timeframe: Optional[str] = None,
        window: int = 300,
        htf_window: int = 300,
        warmup: int = 200,
        iter_start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.htf_timeframe = htf_timeframe
        self.window = window
        self.htf_window = htf_window
        self.warmup = warmup

        df = df.sort_values("open_time").reset_index(drop=True)
        if end_ms is not None:
            df = df[df["open_time"] <= end_ms].reset_index(drop=True)
        self.df = df

        # iteration begins at iter_start_ms (warmup drawn from BEFORE it, so a
        # walk-forward test window doesn't sacrifice its first `warmup` bars).
        if iter_start_ms is not None and len(df):
            idx = int(np.searchsorted(df["open_time"].to_numpy(), iter_start_ms, side="left"))
        else:
            idx = 0
        self._i_start = max(self.warmup, idx)

        if htf_df is not None and not htf_df.empty:
            self.htf_df = htf_df.sort_values("open_time").reset_index(drop=True)
            self._htf_close = self.htf_df["close_time"].to_numpy()
        else:
            self.htf_df = pd.DataFrame(columns=df.columns)
            self._htf_close = np.array([], dtype="int64")

    def __len__(self) -> int:
        return max(0, len(self.df) - self._i_start)

    def __iter__(self) -> Iterator[MarketSlice]:
        df = self.df
        n = len(df)
        for i in range(self._i_start, n):
            bar = row_to_bar(df.iloc[i], self.symbol, self.timeframe)
            lo = max(0, i - self.window + 1)
            window = df.iloc[lo : i + 1]
            htf_window = self._htf_slice(bar.close_time_ms)
            yield MarketSlice(bar=bar, window=window, htf_window=htf_window)

    def _htf_slice(self, as_of_close_ms: int) -> pd.DataFrame:
        if len(self._htf_close) == 0:
            return self.htf_df
        # number of HTF bars fully closed at or before this trading bar's close
        k = int(np.searchsorted(self._htf_close, as_of_close_ms, side="right"))
        lo = max(0, k - self.htf_window)
        return self.htf_df.iloc[lo:k]


# --------------------------------------------------------------------------- #
# live feed (paper/live) — yields the SAME MarketSlice shape as ParquetFeed
# --------------------------------------------------------------------------- #
class CcxtLiveFeed:
    """Fetch the latest CLOSED trading bar + windows from the exchange.

    Driven by the live scheduler (live.py), which calls ``latest_closed`` after
    each candle close. Returns a MarketSlice only when a NEW closed bar exists.
    """

    def __init__(
        self,
        exchange,
        clock,
        symbol: str,
        timeframe: str,
        htf_timeframe: str,
        window: int = 300,
        htf_window: int = 300,
    ) -> None:
        self.ex = exchange
        self.clock = clock
        self.symbol = symbol
        self.timeframe = timeframe
        self.htf_timeframe = htf_timeframe
        self.window = window
        self.htf_window = htf_window
        self.last_processed_ms = 0

    def latest_closed(self) -> Optional[MarketSlice]:
        now = self.clock.now_ms()
        rows = self.ex.fetch_ohlcv(self.symbol, self.timeframe, limit=self.window + 5)
        df = drop_unclosed(ohlcv_to_df(rows, self.timeframe), self.timeframe, now)
        if df.empty:
            return None
        last = df.iloc[-1]
        if int(last["close_time"]) <= self.last_processed_ms:
            return None  # no new closed bar yet
        htf_rows = self.ex.fetch_ohlcv(self.symbol, self.htf_timeframe, limit=self.htf_window + 5)
        htf_df = drop_unclosed(ohlcv_to_df(htf_rows, self.htf_timeframe), self.htf_timeframe, now)
        bar = row_to_bar(last, self.symbol, self.timeframe)
        return MarketSlice(bar=bar, window=df.tail(self.window), htf_window=htf_df.tail(self.htf_window))

    def mark_processed(self, slc: MarketSlice) -> None:
        self.last_processed_ms = slc.bar.close_time_ms
