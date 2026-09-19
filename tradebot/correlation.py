"""Rolling pairwise correlation between symbols and position-sizing penalty.

Used by ``RiskManager.evaluate`` to scale down position sizes when entering
a symbol that is highly correlated with an existing open position.  All
functions are pure (no side effects) and operate on parquet-cached daily bars
in ``data/``.

Design:
- Rolling Pearson correlation is computed on daily *returns* (pct change),
  aligned on ``open_time`` so that missing data (e.g. SOL starting later) is
  handled gracefully via pandas ``join``.
- The rolling series is cached in module-level memory so repeated calls
  within the same process don't re-read parquet.
- The penalty formula is linear, clamped to [0.3, 1.0], and is designed to
  feed into ``RiskManager`` as a position-size multiplier.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache:  {(symbol_a, symbol_b, window): pd.Series}
# ---------------------------------------------------------------------------
_rolling_cache: dict[tuple[str, str, int], pd.Series] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _cache_path(symbol: str, data_dir: Path) -> Path:
    """Resolve the parquet path for a symbol's daily bars."""
    return data_dir / f"{symbol.replace('/', '_')}_1d.parquet"


def _load_close(symbol: str, data_dir: Path) -> pd.Series:
    """Load close prices as a Series indexed by ``open_time`` (epoch-ms).

    Returns an empty Series if the parquet file is missing or unreadable.
    """
    path = _cache_path(symbol, data_dir)
    try:
        df = pd.read_parquet(path, columns=["open_time", "close"])
    except Exception:
        log.warning("Could not load parquet for %s at %s", symbol, path)
        return pd.Series(dtype="float64")
    return df.set_index("open_time")["close"]


def _daily_returns(close: pd.Series) -> pd.Series:
    """Percentage returns from close prices.  NaN-safe (first row is NaN)."""
    return close.pct_change()


def _pair_key(a: str, b: str, window: int) -> tuple[str, str, int]:
    """Canonical cache key — order-independent so (A,B) == (B,A)."""
    lo, hi = sorted((a, b))
    return (lo, hi, window)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_rolling_correlation(
    symbol_a: str,
    symbol_b: str,
    data_dir: str | Path,
    window: int = 60,
) -> pd.Series:
    """Rolling Pearson correlation between two symbols' daily returns.

    Parameters
    ----------
    symbol_a, symbol_b:
        Binance-style symbols, e.g. ``"BTC/USDT"``.
    data_dir:
        Directory containing ``<SYMBOL>_1d.parquet`` files.
    window:
        Rolling window in trading days (default 60 ≈ ~2 months).

    Returns
    -------
    pd.Series
        Indexed by ``open_time`` (epoch-ms), values in [-1, 1].
        NaN where the window is not yet full or data is missing.
    """
    data_dir = Path(data_dir)
    key = _pair_key(symbol_a, symbol_b, window)
    if key in _rolling_cache:
        return _rolling_cache[key]

    close_a = _load_close(symbol_a, data_dir)
    close_b = _load_close(symbol_b, data_dir)

    if close_a.empty or close_b.empty:
        log.warning(
            "Correlation data unavailable for %s / %s — returning empty series",
            symbol_a,
            symbol_b,
        )
        return pd.Series(dtype="float64")

    ret_a = _daily_returns(close_a)
    ret_b = _daily_returns(close_b)

    # Align on overlapping dates (inner join handles offset starts like SOL)
    aligned = pd.concat([ret_a, ret_b], axis=1, join="inner", keys=["a", "b"])
    aligned = aligned.dropna()

    if len(aligned) < window:
        log.warning(
            "Not enough overlapping data for %s / %s (%d bars < window %d)",
            symbol_a,
            symbol_b,
            len(aligned),
            window,
        )
        _rolling_cache[key] = pd.Series(dtype="float64")
        return _rolling_cache[key]

    corr = aligned["a"].rolling(window).corr(aligned["b"])
    corr.index.name = "open_time"
    _rolling_cache[key] = corr
    return corr


def correlation_penalty(
    open_positions: list[str],
    new_symbol: str,
    data_dir: str | Path,
    threshold: float = 0.7,
) -> float:
    """Position-size penalty factor for entering a new correlated symbol.

    The factor scales position size *down* when the new symbol is highly
    correlated with an already-open position:

    - ``avg_abs_corr < threshold`` → 1.0 (no penalty)
    - ``avg_abs_corr >= threshold`` →
      ``1.0 - (avg_corr - threshold) * 2.0``, clamped to [0.3, 1.0]

    Parameters
    ----------
    open_positions:
        Symbols with currently open positions, e.g. ``["BTC/USDT", "ETH/USDT"]``.
    new_symbol:
        Symbol we want to enter.
    data_dir:
        Directory containing parquet files.
    threshold:
        Correlation level at which the penalty starts (default 0.7).

    Returns
    -------
    float
        Multiplier in [0.3, 1.0].  1.0 means no sizing reduction.
    """
    if not open_positions:
        return 1.0

    data_dir = Path(data_dir)
    abs_corrs: list[float] = []

    for pos_sym in open_positions:
        corr_series = compute_rolling_correlation(pos_sym, new_symbol, data_dir)
        if corr_series.empty:
            continue
        last_val = corr_series.iloc[-1]
        if pd.notna(last_val):
            abs_corrs.append(abs(last_val))

    if not abs_corrs:
        return 1.0  # fail-safe: no usable correlation data

    avg_corr = sum(abs_corrs) / len(abs_corrs)

    if avg_corr < threshold:
        return 1.0

    penalty = 1.0 - (avg_corr - threshold) * 2.0
    return max(0.3, min(1.0, penalty))


def get_current_correlation(
    symbol_a: str,
    symbol_b: str,
    data_dir: str | Path,
) -> float:
    """Most recent rolling correlation between two symbols.

    Useful for display / reporting.  Returns ``NaN`` when data is unavailable.
    """
    corr_series = compute_rolling_correlation(symbol_a, symbol_b, data_dir)
    if corr_series.empty:
        return float("nan")
    last_val = corr_series.iloc[-1]
    return float(last_val) if pd.notna(last_val) else float("nan")
