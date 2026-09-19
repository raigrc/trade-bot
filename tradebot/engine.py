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
from collections.abc import Iterable
from typing import Callable, Optional

import pandas as pd

from . import indicators as ind
from .alpha import AlphaProvider, AlphaSnapshot
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
        alpha_provider: Optional[AlphaProvider] = None,
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
        self._alpha_provider = alpha_provider

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
        alpha = self._fetch_alpha(bar.symbol, now)
        ctx = StrategyContext(
            bar=bar,
            window=slc.window,
            htf_window=slc.htf_window,
            position=self.portfolio.position,
            clock_ms=now,
            params=self.strategy.params,
            alpha=alpha,
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

    def _fetch_alpha(self, symbol: str, now_ms: int) -> Optional[AlphaSnapshot]:
        """Fetch alpha snapshot; provider failures are non-fatal (returns None)."""
        if self._alpha_provider is None:
            return None
        try:
            return self._alpha_provider.fetch(symbol, now_ms)
        except Exception as exc:  # noqa: BLE001
            log.debug("Alpha fetch failed (non-fatal): %s", exc)
            return None


# --------------------------------------------------------------------------- #
# MultiEngine — orchestrates multiple single-symbol engines
# --------------------------------------------------------------------------- #
class MultiEngine:
    """Orchestrates multiple single-symbol engines for a portfolio.

    Each sub-Engine runs independently with its own strategy, risk manager,
    execution layer, and portfolio.  MultiEngine aggregates equity, bars
    processed, halted status, and rejections across all symbols.

    Callbacks:
      * ``on_trade`` — forwarded as-is (``Trade`` already carries ``symbol``).
      * ``on_alert`` — prefixed with ``[SYMBOL]`` so the origin is obvious.
      * ``on_equity`` — reports *portfolio-level* equity (sum of all sub-engine
        equity values), not per-engine.
    """

    def __init__(
        self,
        engines: dict[str, Engine],
        *,
        on_trade: Optional[Callable[[Trade], None]] = None,
        on_alert: Optional[Callable[[str], None]] = None,
        on_equity: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        self.engines = engines  # keyed by symbol
        self._on_trade = on_trade
        self._on_alert = on_alert
        self._on_equity = on_equity

        self.halted = False
        self.equity_curve: list[tuple[int, float]] = []
        self.rejections: dict[str, int] = {}
        self.bars_processed = 0

        # Wire sub-engine callbacks so they route through MultiEngine logic.
        for sym, engine in engines.items():
            # on_trade: Trade already contains .symbol — forward as-is.
            engine._on_trade = on_trade
            # on_alert: prefix with [SYMBOL] so the source is unambiguous.
            engine._on_alert = (
                (lambda msg, s=sym: on_alert(f"[{s}] {msg}")) if on_alert else None
            )
            # on_equity: suppressed on sub-engines; aggregated at this level.
            engine._on_equity = None

    # -- main entry ------------------------------------------------------------
    def run(self, feeds: dict[str, Iterable[MarketSlice]]) -> None:
        """Run all feeds in lockstep, aligned by ``close_time_ms``.

        Feeds that are shorter or longer than others are handled gracefully:
        only symbols with a bar at the current timestamp are stepped.  The
        loop ends when every feed is exhausted.
        """
        iters = {sym: iter(feed) for sym, feed in feeds.items()}
        buffers: dict[str, MarketSlice] = {}

        # Prime: pull the first bar from each feed.
        for sym in list(iters):
            try:
                buffers[sym] = next(iters[sym])
            except StopIteration:
                del iters[sym]

        while buffers:
            # Earliest close time across all buffered bars.
            earliest = min(b.bar.close_time_ms for b in buffers.values())

            # Collect every symbol that has a bar at this timestamp.
            batch: dict[str, MarketSlice] = {}
            for sym in list(buffers):
                if buffers[sym].bar.close_time_ms == earliest:
                    batch[sym] = buffers.pop(sym)

            # Advance the iterator for each consumed symbol.
            for sym in batch:
                if sym in iters:
                    try:
                        buffers[sym] = next(iters[sym])
                    except StopIteration:
                        del iters[sym]

            self.step(batch)

    def step(self, slices: dict[str, MarketSlice]) -> None:
        """Process one bar for each symbol.

        *slices* maps symbol → MarketSlice.  Symbols present in
        ``self.engines`` but absent from *slices* (e.g. data gap) are
        silently skipped for that tick.
        """
        if not slices:
            return

        for symbol, slc in slices.items():
            if symbol in self.engines:
                self.engines[symbol].step(slc)

        # Aggregate portfolio-level equity across all engines that have a bar.
        total_equity = sum(
            e.portfolio.equity(slices[s].bar.close)
            for s, e in self.engines.items()
            if s in slices
        )
        now = max(slc.bar.close_time_ms for slc in slices.values())
        self.equity_curve.append((now, total_equity))
        if self._on_equity:
            self._on_equity(now, total_equity)

        # Aggregate counters.
        self.bars_processed = sum(e.bars_processed for e in self.engines.values())
        self.halted = any(e.halted for e in self.engines.values())

        # Merge rejection codes from all sub-engines.
        self.rejections = {}
        for e in self.engines.values():
            for code, count in e.rejections.items():
                self.rejections[code] = self.rejections.get(code, 0) + count

    # -- state persistence (aggregated from all sub-engines) -------------------
    def state_dict(self) -> dict:
        """Serialize state from every sub-engine, keyed by symbol."""
        return {
            "engines": {
                sym: {
                    "portfolio": e.portfolio.state_dict(),
                    "risk": e.risk.state.state_dict(),
                    "execution": e.execution.state_dict(),
                    "strategy": e.strategy.state_dict(),
                }
                for sym, e in self.engines.items()
            },
            "bars_processed": self.bars_processed,
            "halted": self.halted,
        }

    def load_state(self, d: dict) -> None:
        """Restore state into each sub-engine from a ``state_dict`` snapshot."""
        self.halted = d.get("halted", False)
        self.bars_processed = d.get("bars_processed", 0)

        engines_data = d.get("engines", {})
        for sym, engine_state in engines_data.items():
            if sym not in self.engines:
                log.warning("load_state: unknown symbol %r, skipping", sym)
                continue
            e = self.engines[sym]
            if "portfolio" in engine_state:
                e.portfolio.load_state(engine_state["portfolio"])
            if "risk" in engine_state:
                e.risk.state.load_state(engine_state["risk"])
            if "execution" in engine_state:
                e.execution.load_state(engine_state["execution"])
            if "strategy" in engine_state:
                e.strategy.load_state(engine_state["strategy"])
