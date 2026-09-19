"""Multi-symbol Portfolio tests — concurrent positions, equity, persistence."""

from __future__ import annotations

import pytest

from tradebot.enums import Side
from tradebot.portfolio import Portfolio
from tradebot.types import Fill, OrderStatus


def _fill(symbol: str, side: Side, qty: float, price: float, fee: float = 0.0, ts_ms: int = 0) -> Fill:
    return Fill(
        client_order_id="",
        symbol=symbol,
        side=side,
        filled_qty=qty,
        avg_price=price,
        fee=fee,
        status=OrderStatus.FILLED,
        ts_ms=ts_ms,
    )


class TestMultiPositionConcurrent:
    """Two concurrent positions (BTC + ETH) coexist correctly."""

    def test_two_concurrent_positions(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        assert len(pf.positions) == 2
        assert "BTC/USDT" in pf.positions
        assert "ETH/USDT" in pf.positions
        # position property returns None when >1 open
        assert pf.position is None

    def test_single_position_property_returns_position(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))

        assert len(pf.positions) == 1
        assert pf.position is not None
        assert pf.position.symbol == "BTC/USDT"
        assert pf.position.qty == pytest.approx(0.1)

    def test_empty_portfolio_returns_none(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        assert pf.position is None
        assert len(pf.positions) == 0


class TestMultiEquity:
    """Equity = cash + sum of position notionals."""

    def test_equity_with_two_positions(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        # cash = 10000 - (0.1*50000+5) - (2*3000+6) = 10000 - 5005 - 6006 = -1011
        assert pf.cash == pytest.approx(10_000.0 - 5005.0 - 6006.0)

        # equity with per-symbol mark prices
        eq = pf.equity(0, mark_prices={"BTC/USDT": 55_000.0, "ETH/USDT": 3_500.0})
        expected = pf.cash + 0.1 * 55_000.0 + 2.0 * 3_500.0
        assert eq == pytest.approx(expected)

    def test_equity_single_price_applies_to_all(self):
        """Without mark_prices, the single price is used for every position."""
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        eq = pf.equity(100.0)
        expected = pf.cash + 0.1 * 100.0 + 2.0 * 100.0
        assert eq == pytest.approx(expected)

    def test_equity_empty(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        assert pf.equity(100.0) == pytest.approx(10_000.0)


class TestMultiApplyFill:
    """apply_fill routes to the correct symbol."""

    def test_buy_routes_to_correct_symbol(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        assert pf.positions["BTC/USDT"].qty == pytest.approx(0.1)
        assert pf.positions["ETH/USDT"].qty == pytest.approx(2.0)

    def test_sell_closes_correct_position(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        trade = pf.apply_fill(_fill("ETH/USDT", Side.SELL, 2.0, 3_500.0, fee=7.0), reason="target")

        assert trade is not None
        assert trade.symbol == "ETH/USDT"
        assert trade.pnl == pytest.approx((3_500.0 - 3_000.0) * 2.0 - 6.0 - 7.0)
        # ETH position removed, BTC still there
        assert "ETH/USDT" not in pf.positions
        assert "BTC/USDT" in pf.positions
        assert len(pf.positions) == 1
        # Now single-position property works again
        assert pf.position is not None
        assert pf.position.symbol == "BTC/USDT"

    def test_sell_nonexistent_symbol_logs_warning(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        trade = pf.apply_fill(_fill("SOL/USDT", Side.SELL, 1.0, 100.0), reason="oops")
        assert trade is None
        assert len(pf.positions) == 0

    def test_partial_close(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        # Sell half the ETH
        trade = pf.apply_fill(_fill("ETH/USDT", Side.SELL, 1.0, 3_200.0, fee=3.2), reason="trim")

        assert trade is not None
        assert pf.positions["ETH/USDT"].qty == pytest.approx(1.0)
        assert "BTC/USDT" in pf.positions
        assert "ETH/USDT" in pf.positions
        assert len(pf.positions) == 2

    def test_unrealized_pnl_sums_across_positions(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        upnl = pf.unrealized_pnl(0, mark_prices={"BTC/USDT": 52_000.0, "ETH/USDT": 2_800.0})
        # BTC: (52000-50000)*0.1 = 200, ETH: (2800-3000)*2 = -400
        assert upnl == pytest.approx(200.0 - 400.0)

    def test_exposure_pct_multiple_positions(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        pct = pf.exposure_pct(0, mark_prices={"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0})
        total_notional = 0.1 * 50_000.0 + 2.0 * 3_000.0
        eq = pf.cash + total_notional
        assert pct == pytest.approx(total_notional / eq)


class TestMultiStatePersistence:
    """state_dict / load_state round-trip with multiple positions."""

    def test_round_trip_multi_position(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))
        # Override accounting totals to known values for the round-trip check
        pf.realized_pnl = 150.0
        pf.fees_paid = 11.0
        expected_cash = pf.cash  # preserve what apply_fill computed

        d = pf.state_dict()
        restored = Portfolio(0.0, "UNUSED")
        restored.load_state(d)

        assert restored.cash == pytest.approx(expected_cash)
        assert restored.realized_pnl == pytest.approx(150.0)
        assert restored.fees_paid == pytest.approx(11.0)
        assert len(restored.positions) == 2
        assert restored.positions["BTC/USDT"].qty == pytest.approx(0.1)
        assert restored.positions["BTC/USDT"].avg_entry == pytest.approx(50_000.0)
        assert restored.positions["ETH/USDT"].qty == pytest.approx(2.0)
        assert restored.positions["ETH/USDT"].avg_entry == pytest.approx(3_000.0)
        # position property returns None for 2+ positions
        assert restored.position is None

    def test_legacy_single_position_loads(self):
        """Old state_dict format (single 'position' key) still loads."""
        pf = Portfolio(10_000.0, "BTC/USDT")
        legacy = {
            "cash": 9_500.0,
            "realized_pnl": 100.0,
            "fees_paid": 5.0,
            "position": {
                "symbol": "BTC/USDT", "side": "buy", "qty": 0.1, "avg_entry": 50_000.0,
                "stop": 48_000.0, "target": 55_000.0, "opened_ms": 1700000000000,
                "highest_since_entry": 51_000.0, "lowest_since_entry": 49_000.0,
                "bars_held": 5, "entry_fee": 5.0, "entry_reason": "test",
            },
        }
        pf.load_state(legacy)

        assert pf.cash == pytest.approx(9_500.0)
        assert len(pf.positions) == 1
        assert pf.position is not None
        assert pf.position.symbol == "BTC/USDT"
        assert pf.position.qty == pytest.approx(0.1)

    def test_load_state_corrupted_position_skipped(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.load_state({"cash": 5_000.0, "positions": [{"invalid": "data"}]})
        assert len(pf.positions) == 0
        assert pf.cash == pytest.approx(5_000.0)

    def test_account_state_returns_all_positions(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        acct = pf.account_state(0, mark_prices={"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0})
        assert len(acct.positions) == 2
        symbols = {p.symbol for p in acct.positions}
        assert symbols == {"BTC/USDT", "ETH/USDT"}

    def test_state_dict_positions_key_is_list(self):
        pf = Portfolio(10_000.0, "BTC/USDT")
        pf.apply_fill(_fill("BTC/USDT", Side.BUY, 0.1, 50_000.0, fee=5.0))
        pf.apply_fill(_fill("ETH/USDT", Side.BUY, 2.0, 3_000.0, fee=6.0))

        d = pf.state_dict()
        assert isinstance(d["positions"], list)
        assert len(d["positions"]) == 2
        symbols = {p["symbol"] for p in d["positions"]}
        assert symbols == {"BTC/USDT", "ETH/USDT"}
