"""Engine integration tests — kill-switch and MultiEngine."""

from __future__ import annotations

import pandas as pd
import pytest

from tradebot.clock import SimClock
from tradebot.config import RiskConfig
from tradebot.data import MarketSlice
from tradebot.engine import Engine, MultiEngine
from tradebot.enums import Side
from tradebot.execution import SimulatedExecution
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskManager, RiskState
from tradebot.strategies.base import Strategy, StrategyContext
from tradebot.types import Bar, Fill, MarketInfo, OrderStatus, Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bar(sym: str, t: int, o: float = 100, h: float = 101, lo: float = 99, c: float = 100) -> Bar:
    return Bar(sym, t, o, h, lo, c, 100.0, "4h")


def _slice(bar: Bar, warmup: int = 0) -> MarketSlice:
    """Build a MarketSlice with a window of `warmup` + 1 rows."""
    rows = []
    tf = 14_400_000  # 4h ms
    for i in range(warmup + 1):
        t = bar.open_time_ms - (warmup - i) * tf
        rows.append([t, 100.0, 101.0, 99.0, 100.0, 100.0])
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = df["open_time"].astype("int64")
    return MarketSlice(bar=bar, window=df, htf_window=df.iloc[0:0])


def _market(sym: str = "BTC/USDT") -> MarketInfo:
    return MarketInfo(
        symbol=sym, is_spot=True, base=sym.split("/")[0], quote="USDT",
        tick_size=0.01, step_size=1e-6, min_qty=1e-5, max_qty=1e9, min_notional=10.0,
    )


class FlatStrategy(Strategy):
    """Strategy that never trades — useful for isolating engine/risk logic."""

    name = "flat"

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        return None


class EnterOnceStrategy(Strategy):
    """Strategy that emits a BUY on the first bar with enough warmup, then stays flat.

    Sets a very wide stop (50%) so the resting stop does not interfere with
    drawdown kill-switch testing.
    """

    name = "enter_once"

    def __init__(self, symbol: str, timeframe: str, params: dict | None = None) -> None:
        super().__init__(symbol, timeframe, params)
        self.warmup_bars = 5
        self._entered = False

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        if ctx.position is not None or self._entered:
            return None
        if len(ctx.window) < self.warmup_bars:
            return None
        self._entered = True
        return Signal(
            side=Side.BUY,
            reason="enter",
            stop=ctx.bar.close * 0.50,
            target=ctx.bar.close * 1.06,
        )

    def state_dict(self) -> dict:
        return {"entered": self._entered}

    def load_state(self, d: dict) -> None:
        self._entered = d.get("entered", False)


def _make_engine(
    symbol: str = "BTC/USDT",
    strategy: Strategy | None = None,
    risk_cfg: RiskConfig | None = None,
    risk_state: RiskState | None = None,
    on_equity=None,
    on_alert=None,
    initial_cash: float = 10_000.0,
) -> Engine:
    cfg = risk_cfg or RiskConfig()
    rm = RiskManager(cfg, risk_state or RiskState())
    ex = SimulatedExecution(cfg, _market(symbol))
    pf = Portfolio(initial_cash, symbol)
    strat = strategy or FlatStrategy(symbol, "4h")
    return Engine(
        symbol, SimClock(), strat, rm, ex, pf, _market(symbol),
        on_equity=on_equity, on_alert=on_alert,
    )


# ---------------------------------------------------------------------------
# BUG-2 regression: oversized sell uses qty_closed for proceeds
# ---------------------------------------------------------------------------
class TestOversizedSellProceeds:
    def test_oversized_sell_credits_only_closed_qty(self):
        from tradebot.portfolio import Portfolio as PF

        pf = PF(10_000.0, "BTC/USDT")
        # Open position: 0.5 BTC @ 100
        buy = Fill("", "BTC/USDT", Side.BUY, 0.5, 100.0, 0.05, OrderStatus.FILLED, 0)
        pf.apply_fill(buy)
        assert pf.cash == pytest.approx(10_000.0 - 0.5 * 100.0 - 0.05)

        # Sell 1.0 BTC (oversized — only 0.5 in position)
        sell = Fill("", "BTC/USDT", Side.SELL, 1.0, 120.0, 0.12, OrderStatus.FILLED, 1000)
        pf.apply_fill(sell, reason="kill-switch")

        # Proceeds should be 0.5 * 120 = 60, not 1.0 * 120 = 120
        expected_cash = (10_000.0 - 50.0 - 0.05) + (0.5 * 120.0 - 0.12)
        assert pf.cash == pytest.approx(expected_cash)
        assert len(pf.positions) == 0


# ---------------------------------------------------------------------------
# Engine.kill-switch integration test
# ---------------------------------------------------------------------------
class TestEngineKillSwitch:
    def test_on_equity_update_flatten_and_halt(self):
        """Drawdown kill-switch: equity drops > max_drawdown -> flatten + halt."""
        alerts: list[str] = []
        equity_log: list[tuple[int, float]] = []

        cfg = RiskConfig(max_drawdown_pct=0.15, trail_mode="off")
        engine = _make_engine(
            risk_cfg=cfg,
            on_alert=alerts.append,
            on_equity=lambda ts, eq: equity_log.append((ts, eq)),
        )

        # Manually establish a position: buy 50 BTC @ 100 (cost 5000, cash=5000)
        buy_fill = Fill("", "BTC/USDT", Side.BUY, 50.0, 100.0, 0.0, OrderStatus.FILLED, 0)
        engine.portfolio.apply_fill(buy_fill, reason="setup")
        assert engine.portfolio.position is not None

        # Prime the risk state: first bar sets peak equity at ~10000
        b0 = _bar("BTC/USDT", 0, o=100, h=101, lo=99, c=100)
        engine.step(_slice(b0, warmup=10))
        # equity = cash(5000) + qty(50) * close(100) = 10000
        assert engine.risk.state.peak_equity == pytest.approx(10_000.0)
        assert engine.halted is False

        # Crash: close at 75 → equity = 5000 + 50*75 = 8750 → dd = 12.5%
        b1 = _bar("BTC/USDT", 14_400_000, o=100, h=101, lo=99, c=75)
        engine.step(_slice(b1, warmup=10))
        # Still above 15% threshold — no kill yet
        assert engine.halted is False

        # Deeper crash: close at 50 → equity = 5000 + 50*50 = 7500 → dd = 25%
        b2 = _bar("BTC/USDT", 28_800_000, o=75, h=76, lo=49, c=50)
        engine.step(_slice(b2, warmup=10))
        assert engine.halted is True
        assert engine.risk.state.kill_switch_engaged is True
        kill_alerts = [a for a in alerts if "KILL-SWITCH" in a]
        assert len(kill_alerts) >= 1

    def test_halted_engine_skips_strategy(self):
        """Once halted, step returns immediately — no new trades."""
        cfg = RiskConfig(max_drawdown_pct=0.15)
        rm = RiskManager(cfg, RiskState())
        rm.state.kill_switch_engaged = True
        rm.state.kill_switch_reason = "test"

        engine = _make_engine(risk_cfg=cfg, risk_state=rm.state)
        engine.halted = True

        bar1 = _bar("BTC/USDT", 1000)
        engine.step(_slice(bar1, warmup=10))
        # No new bars should be processed after halt
        assert engine.bars_processed == 1  # step still runs once but returns early


# ---------------------------------------------------------------------------
# MultiEngine tests
# ---------------------------------------------------------------------------
class TestMultiEngineStep:
    def test_step_aggregates_equity(self):
        """MultiEngine.step processes all symbols and aggregates equity."""
        e1 = _make_engine("BTC/USDT", initial_cash=5_000.0)
        e2 = _make_engine("ETH/USDT", initial_cash=3_000.0)
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2})

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        eth_bar = _bar("ETH/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10), "ETH/USDT": _slice(eth_bar, warmup=10)})

        assert len(multi.equity_curve) == 1
        ts, eq = multi.equity_curve[0]
        # close_time_ms = open_time_ms + 14_400_000 (4h tf)
        assert ts == 1000 + 14_400_000
        # Both portfolios start at their initial cash with no positions
        assert eq == pytest.approx(8_000.0)
        assert multi.bars_processed == 2

    def test_step_skips_missing_symbols(self):
        """Symbols not in slices are silently skipped."""
        e1 = _make_engine("BTC/USDT", initial_cash=5_000.0)
        e2 = _make_engine("ETH/USDT", initial_cash=3_000.0)
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2})

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10)})

        assert len(multi.equity_curve) == 1
        # Only BTC equity counted (ETH not in slices)
        assert multi.equity_curve[0][1] == pytest.approx(5_000.0)

    def test_step_empty_slices_noop(self):
        multi = MultiEngine({})
        multi.step({})
        assert len(multi.equity_curve) == 0

    def test_halted_when_any_engine_halted(self):
        e1 = _make_engine("BTC/USDT")
        e2 = _make_engine("ETH/USDT")
        e2.halted = True
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2})

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        eth_bar = _bar("ETH/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10), "ETH/USDT": _slice(eth_bar, warmup=10)})

        assert multi.halted is True

    def test_rejections_aggregated(self):
        e1 = _make_engine("BTC/USDT")
        e2 = _make_engine("ETH/USDT")
        e1.rejections = {"stop": 3, "size": 1}
        e2.rejections = {"stop": 2}
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2})

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        eth_bar = _bar("ETH/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10), "ETH/USDT": _slice(eth_bar, warmup=10)})

        assert multi.rejections["stop"] == 5
        assert multi.rejections["size"] == 1

    def test_on_equity_callback_receives_portfolio_equity(self):
        equity_log: list[tuple[int, float]] = []
        e1 = _make_engine("BTC/USDT", initial_cash=5_000.0)
        e2 = _make_engine("ETH/USDT", initial_cash=3_000.0)
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2}, on_equity=lambda ts, eq: equity_log.append((ts, eq)))

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        eth_bar = _bar("ETH/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10), "ETH/USDT": _slice(eth_bar, warmup=10)})

        assert len(equity_log) == 1
        assert equity_log[0][1] == pytest.approx(8_000.0)


class TestMultiEngineStateRoundTrip:
    def test_state_dict_and_load_state(self):
        """Round-trip: state_dict -> load_state restores all sub-engine state."""
        e1 = _make_engine("BTC/USDT", initial_cash=5_000.0)
        e2 = _make_engine("ETH/USDT", initial_cash=3_000.0)
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2})

        # Process a bar so some state is created
        btc_bar = _bar("BTC/USDT", 1000, c=100)
        eth_bar = _bar("ETH/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10), "ETH/USDT": _slice(eth_bar, warmup=10)})

        # Serialize
        state = multi.state_dict()
        assert "engines" in state
        assert "BTC/USDT" in state["engines"]
        assert "ETH/USDT" in state["engines"]
        assert state["bars_processed"] == 2
        assert state["halted"] is False

        # Create fresh engines and restore
        e1_new = _make_engine("BTC/USDT", initial_cash=5_000.0)
        e2_new = _make_engine("ETH/USDT", initial_cash=3_000.0)
        multi_new = MultiEngine({"BTC/USDT": e1_new, "ETH/USDT": e2_new})
        multi_new.load_state(state)

        assert multi_new.bars_processed == 2
        assert multi_new.halted is False
        assert e1_new.portfolio.cash == pytest.approx(e1.portfolio.cash)
        assert e2_new.portfolio.cash == pytest.approx(e2.portfolio.cash)

    def test_state_dict_with_halted_engine(self):
        """Halted flag is preserved across round-trip."""
        e1 = _make_engine("BTC/USDT")
        e1.halted = True
        e2 = _make_engine("ETH/USDT")
        multi = MultiEngine({"BTC/USDT": e1, "ETH/USDT": e2})

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        eth_bar = _bar("ETH/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10), "ETH/USDT": _slice(eth_bar, warmup=10)})

        state = multi.state_dict()
        assert state["halted"] is True

        e1_new = _make_engine("BTC/USDT")
        e2_new = _make_engine("ETH/USDT")
        multi_new = MultiEngine({"BTC/USDT": e1_new, "ETH/USDT": e2_new})
        multi_new.load_state(state)
        assert multi_new.halted is True

    def test_load_state_skips_unknown_symbols(self):
        """Symbols in state but not in engines are logged and skipped."""
        e1 = _make_engine("BTC/USDT")
        multi = MultiEngine({"BTC/USDT": e1})
        state = {
            "engines": {
                "BTC/USDT": {"portfolio": {"cash": 4_000.0}},
                "DOGE/USDT": {"portfolio": {"cash": 1_000.0}},
            },
            "bars_processed": 5,
            "halted": False,
        }
        multi.load_state(state)
        assert e1.portfolio.cash == pytest.approx(4_000.0)
        assert multi.bars_processed == 5

    def test_state_dict_preserves_risk_state(self):
        """Risk state (kill-switch, peak equity) round-trips correctly."""
        e1 = _make_engine("BTC/USDT")
        e1.risk.state.peak_equity = 12_000.0
        e1.risk.state.kill_switch_engaged = True
        e1.risk.state.kill_switch_reason = "test"
        multi = MultiEngine({"BTC/USDT": e1})

        btc_bar = _bar("BTC/USDT", 1000, c=100)
        multi.step({"BTC/USDT": _slice(btc_bar, warmup=10)})

        state = multi.state_dict()
        assert state["engines"]["BTC/USDT"]["risk"]["peak_equity"] == 12_000.0
        assert state["engines"]["BTC/USDT"]["risk"]["kill_switch_engaged"] is True

        e1_new = _make_engine("BTC/USDT")
        multi_new = MultiEngine({"BTC/USDT": e1_new})
        multi_new.load_state(state)
        assert e1_new.risk.state.peak_equity == 12_000.0
        assert e1_new.risk.state.kill_switch_engaged is True
