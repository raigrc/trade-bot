"""Persistence round-trip tests — StateStore and TradeJournal survive restarts."""

from __future__ import annotations

from tradebot.enums import Side
from tradebot.execution import SimulatedExecution
from tradebot.persistence import StateStore
from tradebot.config import RiskConfig
from tradebot.journal import TradeJournal
from tradebot.risk import RiskState
from tradebot.portfolio import Portfolio
from tradebot.types import Order, Trade


class TestStateStore:
    def test_put_and_get_round_trip(self, tmp_path):
        store = StateStore(tmp_path / "test.sqlite")
        store.put("risk", {"peak_equity": 10000.0, "kill_switch_engaged": True}, 1000)
        result = store.get("risk")
        assert result is not None
        assert result["peak_equity"] == 10000.0
        assert result["kill_switch_engaged"] is True
        store.close()

    def test_get_missing_key_returns_none(self, tmp_path):
        store = StateStore(tmp_path / "test.sqlite")
        assert store.get("nonexistent") is None
        store.close()

    def test_put_overwrites_existing(self, tmp_path):
        store = StateStore(tmp_path / "test.sqlite")
        store.put("meta", {"value": 1}, 1000)
        store.put("meta", {"value": 2}, 2000)
        result = store.get("meta")
        assert result["value"] == 2
        store.close()

    def test_json_round_trip_complex_types(self, tmp_path):
        store = StateStore(tmp_path / "test.sqlite")
        data = {
            "nested": {"a": [1, 2, 3], "b": {"c": True}},
            "string": "hello",
            "number": 42.5,
        }
        store.put("complex", data, 1000)
        result = store.get("complex")
        assert result == data
        store.close()


class TestRiskState:
    def test_state_dict_and_load_round_trip(self):
        state = RiskState()
        state.peak_equity = 15000.0
        state.kill_switch_engaged = True
        state.kill_switch_reason = "max drawdown 16.0% >= 15%"
        state.day_start_equity = 14500.0
        state.day_start_ms = 1700000000000
        state.consecutive_losses = 3
        state.cooldown_until_ms = 1700001000000
        state.symbol_reentry_until = {"BTC/USDT": 1700002000000}
        state.cooldown_count = 2

        d = state.state_dict()
        restored = RiskState()
        restored.load_state(d)

        assert restored.peak_equity == 15000.0
        assert restored.kill_switch_engaged is True
        assert restored.kill_switch_reason == "max drawdown 16.0% >= 15%"
        assert restored.day_start_equity == 14500.0
        assert restored.day_start_ms == 1700000000000
        assert restored.consecutive_losses == 3
        assert restored.cooldown_until_ms == 1700001000000
        assert restored.symbol_reentry_until == {"BTC/USDT": 1700002000000}
        assert restored.cooldown_count == 2

    def test_load_state_ignores_unknown_keys(self):
        state = RiskState()
        state.peak_equity = 5000.0
        state.load_state({"peak_equity": 8000.0, "evil_key": "injected"})
        assert state.peak_equity == 8000.0
        assert not hasattr(state, "evil_key")


class TestPortfolio:
    def test_state_dict_and_load_round_trip(self):
        pf = Portfolio(10000.0, "BTC/USDT")
        pf.cash = 9500.0
        pf.realized_pnl = 200.0
        pf.fees_paid = 15.0

        d = pf.state_dict()
        restored = Portfolio(10000.0, "BTC/USDT")
        restored.load_state(d)

        assert restored.cash == 9500.0
        assert restored.realized_pnl == 200.0
        assert restored.fees_paid == 15.0
        assert restored.position is None

    def test_state_dict_with_position_round_trip(self):
        from tradebot.types import Position

        pf = Portfolio(10000.0, "BTC/USDT")
        pf.position = Position(
            symbol="BTC/USDT", side=Side.BUY, qty=0.1, avg_entry=50000.0,
            stop=48000.0, target=55000.0, opened_ms=1700000000000,
            highest_since_entry=51000.0, lowest_since_entry=49000.0,
            bars_held=5, entry_fee=5.0, entry_reason="test",
        )

        d = pf.state_dict()
        restored = Portfolio(10000.0, "BTC/USDT")
        restored.load_state(d)

        assert restored.position is not None
        assert restored.position.symbol == "BTC/USDT"
        assert restored.position.qty == 0.1
        assert restored.position.avg_entry == 50000.0
        assert restored.position.stop == 48000.0
        assert restored.position.target == 55000.0

    def test_load_state_handles_corrupted_position(self):
        pf = Portfolio(10000.0, "BTC/USDT")
        pf.load_state({"cash": 9500.0, "position": {"invalid": "data"}})
        assert pf.position is None
        assert pf.cash == 9500.0


class TestSimulatedExecution:
    def test_state_dict_and_load_round_trip(self):
        cfg = RiskConfig()
        ex = SimulatedExecution(cfg, market=None)
        order = Order("BTC/USDT", Side.BUY, qty=1.0, client_order_id="test-123")
        ex.submit_entry(order, stop=98.0, target=106.0, bar=None, reason="enter")

        d = ex.state_dict()
        restored = SimulatedExecution(cfg, market=None)
        restored.load_state(d)

        assert restored.has_pending()
        assert restored._pending_entry is not None
        assert restored._pending_entry[0].symbol == "BTC/USDT"
        assert restored._pending_entry[0].qty == 1.0
        assert restored._pending_entry[1] == 98.0
        assert restored._pending_entry[2] == 106.0
        assert restored._pending_entry[3] == "enter"

    def test_load_state_with_corrupted_entry(self):
        cfg = RiskConfig()
        ex = SimulatedExecution(cfg, market=None)
        ex.load_state({"pending_entry": {"broken": True}})
        assert ex._pending_entry is None

    def test_load_state_adopt_values(self):
        cfg = RiskConfig()
        ex = SimulatedExecution(cfg, market=None)
        ex.load_state({"open_qty": 0.5, "stop": 99.0, "target": 105.0})
        assert ex._open_qty == 0.5
        assert ex._stop == 99.0
        assert ex._target == 105.0


class TestTradeJournal:
    def test_record_and_retrieve_trade(self, tmp_path):
        j = TradeJournal(tmp_path / "test.sqlite")
        t = Trade(
            symbol="BTC/USDT", side=Side.BUY, qty=0.1, entry_price=50000.0,
            exit_price=51000.0, entry_ms=1700000000000, exit_ms=1700001000000,
            pnl=95.0, fees=5.0, return_pct=0.019, bars_held=5,
            entry_reason="test", exit_reason="target hit",
        )
        j.record(t)

        trades = j.all()
        assert len(trades) == 1
        assert trades[0].symbol == "BTC/USDT"
        assert trades[0].pnl == 95.0
        j.close()

    def test_record_equity_and_retrieve(self, tmp_path):
        j = TradeJournal(tmp_path / "test.sqlite")
        j.record_equity(1700000000000, 10000.0)
        j.record_equity(1700001000000, 10100.0)

        eq = j.latest_equity()
        assert eq == 10100.0

        eq_at = j.equity_at_or_before(1700000500000)
        assert eq_at == 10000.0
        j.close()

    def test_trades_between_filters_correctly(self, tmp_path):
        j = TradeJournal(tmp_path / "test.sqlite")
        t1 = Trade("BTC/USDT", Side.BUY, 0.1, 50000, 51000, 1000, 2000, 95, 5, 0.019, 5, "x", "y")
        t2 = Trade("BTC/USDT", Side.BUY, 0.1, 50000, 51000, 3000, 4000, 95, 5, 0.019, 5, "x", "y")
        j.record(t1)
        j.record(t2)

        # trades_between filters by exit_ms in [start, end)
        all_trades = j.trades_between(0, 5000)
        assert len(all_trades) == 2
        recent = j.trades_between(2500, 5000)
        assert len(recent) == 1
        assert recent[0].entry_ms == 3000
        j.close()
