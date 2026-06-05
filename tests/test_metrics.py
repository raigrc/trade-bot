"""Metric correctness on known fixtures."""

from __future__ import annotations

import pytest

from tradebot.enums import Side
from tradebot.metrics import compute_metrics
from tradebot.types import Trade


def _trade(pnl, ret):
    return Trade("BTC/USDT", Side.BUY, 1.0, 100.0, 100 + pnl, 0, 0, pnl, 0.0, ret, 5, "in", "out")


def test_max_drawdown_and_trade_stats():
    tf = "4h"
    step = 14_400_000
    curve = [(0, 100.0), (step, 120.0), (2 * step, 90.0), (3 * step, 108.0)]
    trades = [_trade(10, 0.10), _trade(-5, -0.05), _trade(-5, -0.05), _trade(20, 0.20)]

    m = compute_metrics(curve, trades, tf, initial_equity=100.0, total_bars=4)

    assert m.max_drawdown_pct == pytest.approx(0.25)  # 120 -> 90
    assert m.profit_factor == pytest.approx(3.0)  # 30 / 10
    assert m.win_rate == pytest.approx(0.5)
    assert m.longest_losing_streak == 2
    assert m.expectancy_pct == pytest.approx(0.05)
    assert m.n_trades == 4
    assert m.final_equity == pytest.approx(108.0)


def test_empty_is_safe():
    m = compute_metrics([], [], "4h", initial_equity=1000.0)
    assert m.n_trades == 0
    assert m.max_drawdown_pct == 0.0
    assert m.profit_factor == 0.0
