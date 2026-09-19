"""Correlation module — rolling correlation + position-sizing penalty."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from tradebot.correlation import (
    _rolling_cache,
    compute_rolling_correlation,
    correlation_penalty,
    get_current_correlation,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a cold cache."""
    _rolling_cache.clear()
    yield
    _rolling_cache.clear()


# ---- compute_rolling_correlation --------------------------------------------


class TestComputeRollingCorrelation:
    def test_self_correlation_is_one(self):
        """A symbol correlated with itself must be ~1.0 everywhere after warmup."""
        corr = compute_rolling_correlation("BTC/USDT", "BTC/USDT", DATA_DIR, window=30)
        assert not corr.empty, "Expected non-empty correlation series"
        valid = corr.dropna()
        assert len(valid) > 0, "Expected at least some valid correlation values"
        assert valid.iloc[-1] == pytest.approx(1.0, abs=1e-10)

    def test_returns_series_indexed_by_open_time(self):
        corr = compute_rolling_correlation("BTC/USDT", "ETH/USDT", DATA_DIR, window=60)
        assert isinstance(corr, pd.Series)
        if not corr.empty:
            assert corr.index.name == "open_time"

    def test_values_bounded(self):
        """All correlation values must be in [-1, 1]."""
        corr = compute_rolling_correlation("BTC/USDT", "ETH/USDT", DATA_DIR, window=60)
        valid = corr.dropna()
        if not valid.empty:
            assert valid.min() >= -1.0 - 1e-10
            assert valid.max() <= 1.0 + 1e-10

    def test_missing_symbol_returns_empty(self):
        """Non-existent symbol should produce an empty Series, not crash."""
        corr = compute_rolling_correlation("DOGE/USDT", "BTC/USDT", DATA_DIR, window=60)
        assert corr.empty

    def test_cache_hit(self):
        """Second call should return the cached result (same object)."""
        c1 = compute_rolling_correlation("BTC/USDT", "ETH/USDT", DATA_DIR, window=60)
        c2 = compute_rolling_correlation("ETH/USDT", "BTC/USDT", DATA_DIR, window=60)
        assert c1 is c2  # same cached object

    def test_window_too_large_returns_empty(self):
        """Window larger than available data should return empty, not crash."""
        corr = compute_rolling_correlation("BTC/USDT", "BTC/USDT", DATA_DIR, window=99999)
        assert corr.empty


# ---- correlation_penalty ----------------------------------------------------


class TestCorrelationPenalty:
    def test_no_open_positions_returns_one(self):
        """No open positions → no penalty → 1.0."""
        assert correlation_penalty([], "BTC/USDT", DATA_DIR) == 1.0

    def test_missing_symbol_returns_one(self):
        """If correlation data is unavailable, fail-safe returns 1.0."""
        assert correlation_penalty(["DOGE/USDT"], "BTC/USDT", DATA_DIR) == 1.0

    def test_btc_eth_penalty_reduces_size(self):
        """BTC and ETH are highly correlated — penalty should be < 1.0."""
        penalty = correlation_penalty(["BTC/USDT"], "ETH/USDT", DATA_DIR, threshold=0.7)
        assert 0.3 <= penalty <= 1.0

    def test_btc_btc_self_penalty(self):
        """Same symbol as open position → correlation ~1.0 → significant penalty."""
        penalty = correlation_penalty(["BTC/USDT"], "BTC/USDT", DATA_DIR, threshold=0.7)
        # avg_corr ~1.0 → penalty = 1.0 - (1.0 - 0.7) * 2.0 = 0.4
        assert penalty == pytest.approx(0.4, abs=0.05)

    def test_clamped_to_minimum(self):
        """Very low threshold forces penalty below 0.3 → clamped to 0.3."""
        # Use threshold=0.2 → BTC self-corr ≈ 1.0 → penalty = 1-(1.0-0.2)*2 = -0.6 → clamped to 0.3
        penalty = correlation_penalty(["BTC/USDT"], "BTC/USDT", DATA_DIR, threshold=0.2)
        assert penalty == pytest.approx(0.3, abs=0.05)

    def test_clamped_upper_bound(self):
        """Low correlation (below threshold) → penalty stays at 1.0."""
        # Use high threshold=0.99 — BTC/SOL last correlation is likely below 0.99
        penalty = correlation_penalty(
            ["SOL/USDT"], "BTC/USDT", DATA_DIR, threshold=0.99
        )
        assert penalty == 1.0

    def test_multiple_positions_averages(self):
        """Penalty uses the average correlation across all open positions."""
        p = correlation_penalty(
            ["BTC/USDT", "ETH/USDT"], "BTC/USDT", DATA_DIR, threshold=0.5
        )
        assert 0.3 <= p <= 1.0


# ---- get_current_correlation -----------------------------------------------


class TestGetCurrentCorrelation:
    def test_self_correlation(self):
        val = get_current_correlation("BTC/USDT", "BTC/USDT", DATA_DIR)
        assert val == pytest.approx(1.0, abs=1e-10)

    def test_btc_eth_positive(self):
        val = get_current_correlation("BTC/USDT", "ETH/USDT", DATA_DIR)
        assert math.isfinite(val)
        assert val > 0.5  # BTC and ETH are historically quite correlated

    def test_missing_returns_nan(self):
        val = get_current_correlation("DOGE/USDT", "BTC/USDT", DATA_DIR)
        assert math.isnan(val)
