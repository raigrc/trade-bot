"""The ONE shared event loop. Backtest and live both drive ``Engine.step``.

Only the injected feed, clock, and execution differ between modes — the
strategy / risk / portfolio objects and this loop are identical. That identity
is the parity guarantee.

Per-bar order of operations (``step``):
  1. advance the clock to the bar's close
  2. process executions for this bar (pending fills + resting stop/target)
  3. mark-to-market; update peak equity; check the drawdown kill-switch
  4. trail the protective stop on any open position
  5. ask the strategy for a signal (only on closed bars, after warmup)
  6. route it: exits flatten next-bar-open; entries go through RiskManager
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import pandas as pd

from . import indicators as ind
from .clock import Clock, SimClock
from .data import MarketSlice
from .enums import Side
from .execution import ExecutionEngine, FillEvent
from .portfolio import Portfolio
from .risk import RiskManager
from .strategies.base import Strategy, StrategyContext
from .types import Approval, Bar, MarketInfo, Trade

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        symbol: str,
        clock: Clock,
        strategy: Strategy,
        risk: RiskManager,
        execution: ExecutionEngine,
        portfolio: Portfolio,
        market: MarketInfo,
        *,
        on_trade: Optional[Callable[[Trade], None]] = None,
        on_alert: Optional[Callable[[str], None]] = None,
        on_equity: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        self.symbol = symbol
        self.clock = clock
        self.strategy = strategy
        self.risk = risk
        self.execution = execution
        self.portfolio = portfolio
        self.market = market
        self._on_trade = on_trade
        self._on_alert = on_alert
        self._on_equity = on_equity

        self.halted = False
        self.equity_curve: list[tuple[int, float]] = []
        self.rejections: dict[str, int] = {}
        self.bars_processed = 0

    # -- main entry ------------------------------------------------------------
    def run(self, feed) -> None:
        for slc in feed:
            self.step(slc)

    def step(self, slc: MarketSlice) -> None:
        bar = slc.bar
        if isinstance(self.clock, SimClock):
            self.clock.set(bar.close_time_ms)
        now = self.clock.now_ms()
        self.bars_processed += 1

        # 2. executions for this bar (entry/exit fills + protective triggers)
        for ev in self.execution.on_new_bar(bar):
            self._apply_event(ev, bar, now)

        if self.portfolio.position is not None:
            self.portfolio.position.bars_held += 1

        # 3. mark-to-market + drawdown kill-switch
        equity = self.portfolio.equity(bar.close)
        self.equity_curve.append((bar.close_time_ms, equity))
        if self._on_equity:
            self._on_equity(bar.close_time_ms, equity)
        if self.risk.on_equity_update(equity, now) and not self.halted:
            self._alert(f"KILL-SWITCH: {self.risk.state.kill_switch_reason} — flattening")
            if self.portfolio.position is not None and not self.execution.has_pending():
                self.execution.submit_exit(self.portfolio.position, bar, "kill-switch flatten")
            self.halted = True
        if self.halted:
            return

        # 4. trail protective stop on open position
        self._update_trailing(slc)
        if self.portfolio.position is not None:
            self.portfolio.position.stop = self.execution.protective_stop
            self.portfolio.position.target = self.execution.protective_target

        # 5. strategy (closed bar, after warmup)
        if len(slc.window) < self.strategy.warmup_bars:
            return
        ctx = StrategyContext(
            bar=bar,
            window=slc.window,
            htf_window=slc.htf_window,
            position=self.portfolio.position,
            clock_ms=now,
            params=self.strategy.params,
        )
        signal = self.strategy.on_bar(ctx)
        if signal is None:
            return

        # 6. route. Apply any fills returned synchronously (live fills entries
        # immediately; backtest returns [] and fills next bar via on_new_bar).
        if signal.side == Side.SELL:
            if self.portfolio.position is not None and not self.execution.has_pending():
                for ev in self.execution.submit_exit(self.portfolio.position, bar, signal.reason):
                    self._apply_event(ev, bar, now)
        else:  # BUY entry — must clear the RiskManager
            if self.portfolio.position is None and not self.execution.has_pending():
                account = self.portfolio.account_state(bar.close)
                decision = self.risk.evaluate(signal, account, self.market, bar.close, now)
                if isinstance(decision, Approval):
                    for ev in self.execution.submit_entry(
                        decision.order, decision.stop_price, decision.target_price, bar, signal.reason
                    ):
                        self._apply_event(ev, bar, now)
                else:
                    self.rejections[decision.code.value] = self.rejections.get(decision.code.value, 0) + 1
                    if decision.halt:
                        self.halted = True
                        self._alert(f"HALT on risk reject: {decision.message}")

    # -- helpers ---------------------------------------------------------------
    def _apply_event(self, ev: FillEvent, bar: Bar, now: int) -> None:
        trade = self.portfolio.apply_fill(ev.fill, ev.reason)
        if ev.is_entry and self.portfolio.position is not None:
            self.portfolio.position.stop = self.execution.protective_stop
            self.portfolio.position.target = self.execution.protective_target
            self._alert(
                f"ENTRY {bar.symbol} {ev.fill.filled_qty:.6f} @ {ev.fill.avg_price:.2f} "
                f"stop={self.portfolio.position.stop}"
            )
        if trade is not None:
            self.risk.on_trade_closed(trade, now)
            if self._on_trade:
                self._on_trade(trade)
            self._alert(
                f"EXIT  {trade.symbol} pnl={trade.pnl:+.2f} ({trade.return_pct:+.2%}) "
                f"[{trade.exit_reason}] held={trade.bars_held}"
            )

    def _update_trailing(self, slc: MarketSlice) -> None:
        p = self.portfolio.position
        if p is None:
            return
        cfg = self.risk.cfg
        if cfg.trail_mode == "off":
            return
        p.highest_since_entry = max(p.highest_since_entry, slc.bar.high)
        # only start trailing once price has moved enough in our favour
        if p.highest_since_entry < p.avg_entry * (1 + cfg.trail_activation_pct):
            return
        if cfg.trail_mode == "atr":
            w = slc.window
            atr_series = ind.atr(w["high"], w["low"], w["close"], cfg.atr_period)
            atr_val = atr_series.iloc[-1] if len(atr_series) else None
            if atr_val is None or pd.isna(atr_val):
                return
            new_stop = p.highest_since_entry - cfg.trail_atr_mult * float(atr_val)
        else:  # fixed
            new_stop = p.highest_since_entry * (1 - cfg.trail_pct)
        self.execution.update_protective_stop(new_stop)

    def _alert(self, msg: str) -> None:
        log.info(msg)
        if self._on_alert:
            self._on_alert(msg)
