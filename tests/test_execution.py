"""SimulatedExecution: next-bar-open fills, costs, pessimistic stops."""

from __future__ import annotations

import pytest

from tradebot.config import RiskConfig
from tradebot.enums import Side
from tradebot.execution import SimulatedExecution
from tradebot.types import Bar, Order


def bar(o, h, low, c, t=0, sym="BTC/USDT"):
    return Bar(sym, t, o, h, low, c, 100.0, "4h")


def _entered_exec(cfg=None):
    """Return an execution with a 1-unit position open (entered cleanly)."""
    ex = SimulatedExecution(cfg or RiskConfig(), market=None)
    order = Order("BTC/USDT", Side.BUY, qty=1.0)
    assert ex.submit_entry(order, stop=98.0, target=106.0, bar=bar(100, 100, 100, 100), reason="enter") == []
    # benign next bar (range does not touch stop/target) fills the entry at OPEN
    evs = ex.on_new_bar(bar(100, 101, 99.5, 100.5, t=1))
    assert len(evs) == 1 and evs[0].is_entry
    return ex, evs[0]


def test_no_fill_on_submit_bar():
    ex = SimulatedExecution(RiskConfig(), market=None)
    assert ex.submit_entry(Order("BTC/USDT", Side.BUY, 1.0), 98.0, None, bar(100, 100, 100, 100), "x") == []
    assert ex.has_pending()


def test_entry_fills_next_bar_open():
    cfg = RiskConfig()
    _, entry = _entered_exec(cfg)
    assert entry.fill.avg_price == pytest.approx(100 * (1 + cfg.slippage_pct))
    assert entry.fill.fee == pytest.approx(1.0 * entry.fill.avg_price * cfg.taker_fee_pct)
    assert entry.fill.side == Side.BUY


def test_stop_hit_exit():
    cfg = RiskConfig()
    ex, _ = _entered_exec(cfg)
    evs = ex.on_new_bar(bar(99, 99.5, 97.0, 98.0, t=2))  # low 97 <= stop 98
    assert len(evs) == 1 and not evs[0].is_entry
    assert evs[0].reason == "stop hit"
    assert evs[0].fill.avg_price == pytest.approx(98.0 * (1 - cfg.stop_slippage_pct))


def test_target_hit_exit():
    cfg = RiskConfig()
    ex, _ = _entered_exec(cfg)
    evs = ex.on_new_bar(bar(101, 107.0, 100.0, 106.5, t=2))  # high 107 >= target 106, low > stop
    assert len(evs) == 1 and not evs[0].is_entry
    assert evs[0].reason == "target hit"
    assert evs[0].fill.avg_price == pytest.approx(106.0)


def test_pessimistic_stop_before_target():
    ex, _ = _entered_exec()
    evs = ex.on_new_bar(bar(101, 107.0, 97.0, 100.0, t=2))  # BOTH stop & target in range
    assert len(evs) == 1 and evs[0].reason == "stop hit"  # stop assumed first


def test_trailing_only_ratchets_up():
    ex, _ = _entered_exec()
    ex.update_protective_stop(99.0)
    assert ex.protective_stop == pytest.approx(99.0)
    ex.update_protective_stop(98.5)  # lower -> ignored
    assert ex.protective_stop == pytest.approx(99.0)
    ex.update_protective_stop(100.0)
    assert ex.protective_stop == pytest.approx(100.0)


def test_pending_entry_survives_serialization():
    """The scheduled --once model runs each bar in a separate process; a queued
    entry must survive state_dict -> load_state or paper-sim would never trade."""
    cfg = RiskConfig()
    ex = SimulatedExecution(cfg, market=None)
    ex.submit_entry(Order("BTC/USDT", Side.BUY, 1.0, client_order_id="x"), stop=98.0, target=106.0,
                    bar=bar(100, 100, 100, 100), reason="enter")
    import json

    state = json.loads(json.dumps(ex.state_dict()))  # round-trip like SQLite JSON storage
    assert state["pending_entry"] is not None

    ex2 = SimulatedExecution(cfg, market=None)  # fresh process
    ex2.load_state(state)
    evs = ex2.on_new_bar(bar(100, 101, 99.5, 100.5, t=1))  # next bar fills it
    assert len(evs) == 1 and evs[0].is_entry
    assert evs[0].reason == "enter"
    assert evs[0].fill.avg_price == pytest.approx(100 * (1 + cfg.slippage_pct))


def test_flat_after_exit():
    ex, _ = _entered_exec()
    ex.on_new_bar(bar(99, 99.5, 97.0, 98.0, t=2))  # stop hit -> flat
    assert ex.protective_stop is None
    # a subsequent bar produces no further fills
    assert ex.on_new_bar(bar(98, 99, 97, 98, t=3)) == []
