"""Live / paper runner. Contains the scheduler — but NO trading logic of its
own; it wires the live feed / clock / ccxt execution into the same Engine the
backtest uses, then drives it bar-by-bar on candle closes.

Safety properties:
- acts only on CLOSED candles (sleep to close + confirm buffer + dedupe)
- persists state every bar (SQLite) so a crash/restart never loses the position
  or the drawdown peak / kill-switch
- reconciles against the exchange on startup (exchange is the source of truth)
- graceful shutdown on SIGINT/SIGTERM
- refuses to start LIVE mode without confirm_live + real keys
"""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path

import ccxt

from .backtest import synthetic_market_info
from .clock import LiveClock
from .config import BotConfig, Secrets
from .data import CcxtLiveFeed
from .engine import Engine
from .enums import Mode
from .exchange import BinanceExchange
from .execution import CcxtExecution, SimulatedExecution
from .journal import TradeJournal
from .notify import Notifier
from .persistence import StateStore
from .portfolio import Portfolio
from .reporting import _WEEK_MS, build_weekly_report, iso_week_bounds, write_report
from .risk import RiskManager, RiskState
from .strategies import build_strategy
from .types import timeframe_ms

log = logging.getLogger(__name__)
CONFIRM_BUFFER_MS = 4000  # wake this long AFTER the theoretical close to let it settle
# transient connectivity errors: skip this tick and retry next run (no state change)
_TRANSIENT = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable, ccxt.DDoSProtection)


class LiveRunner:
    def __init__(self, config: BotConfig, secrets: Secrets) -> None:
        config.assert_live_allowed()
        self.config = config
        self.symbol = config.symbols[0]
        self.clock = LiveClock()
        self.shutdown = threading.Event()

        self.notifier = Notifier(secrets)
        # paper-sim: live mainnet public data + simulated fills, NO keys, virtual equity
        self.paper_sim = config.mode == Mode.PAPER and config.paper_execution == "sim"

        db = Path(config.state_dir) / f"{self.symbol.replace('/', '_')}_{config.mode.value}.sqlite"
        self.store = StateStore(db)
        self.journal = TradeJournal(db)

        self.strategy = build_strategy(config.strategy.name, self.symbol, config.timeframe, config.strategy.params)
        self.risk = RiskManager(config.risk, RiskState())

        if self.paper_sim:
            self.exchange = BinanceExchange(config, secrets, public_data_only=True)
            self.market = synthetic_market_info(self.symbol, config.quote_currency)
            self.execution = SimulatedExecution(config.risk, self.market)
            self.portfolio = Portfolio(config.initial_equity, self.symbol, config.quote_currency)
        else:  # testnet (paper) or live — real ccxt orders, real keys
            self.exchange = BinanceExchange(config, secrets)
            self.market = self.exchange.market_info(self.symbol)
            self.execution = CcxtExecution(self.exchange, self.market, config.risk)
            self.portfolio = Portfolio(
                self.exchange.free_quote(config.quote_currency), self.symbol, config.quote_currency
            )

        self.feed = CcxtLiveFeed(
            self.exchange, self.clock, self.symbol, config.timeframe, config.htf_timeframe
        )
        self.engine = Engine(
            self.symbol, self.clock, self.strategy, self.risk, self.execution, self.portfolio, self.market,
            on_trade=self.journal.record, on_alert=self.notifier.send,
            on_equity=self.journal.record_equity,
        )
        self._cur_week_start: int | None = None  # ISO week currently in progress

    # -- state ----------------------------------------------------------------
    def _persist(self) -> None:
        now = self.clock.now_ms()
        self.store.put("risk", self.risk.state.state_dict(), now)
        self.store.put("portfolio", self.portfolio.state_dict(), now)
        self.store.put("execution", self.execution.state_dict(), now)
        self.store.put("strategy", self.strategy.state_dict(), now)
        self.store.put("meta", {"last_processed_ms": self.feed.last_processed_ms,
                                "cur_week_start": self._cur_week_start}, now)

    def _restore_and_reconcile(self) -> None:
        # 1. load persisted state
        if (r := self.store.get("risk")):
            self.risk.state.load_state(r)
        if (p := self.store.get("portfolio")):
            self.portfolio.load_state(p)
        if (s := self.store.get("strategy")):
            self.strategy.load_state(s)
        if (m := self.store.get("meta")):
            self.feed.last_processed_ms = int(m.get("last_processed_ms", 0))
            self._cur_week_start = m.get("cur_week_start")
        if (e := self.store.get("execution")):
            self.execution.load_state(e)  # restores pending entry/exit too (survives --once processes)

        # 2. exchange is the source of truth (real accounts only; paper-sim has none)
        if not self.paper_sim:
            try:
                bal = self.exchange.fetch_balance()
                base_free = float(bal.get("free", {}).get(self.market.base, 0.0) or 0.0)
                open_orders = self.exchange.fetch_open_orders(self.symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("Startup reconcile could not query exchange: %s", exc)
                base_free, open_orders = None, []

            if base_free is not None:
                has_local_pos = self.portfolio.position is not None
                has_exch_pos = base_free * self.exchange.best_bid_ask(self.symbol)[0] >= self.market.min_notional
                if has_exch_pos and not has_local_pos:
                    self.notifier.send(
                        f"RECONCILE: exchange shows ~{base_free} {self.market.base} but local state is flat. "
                        "Trusting exchange. Verify manually."
                    )
                elif has_local_pos and not has_exch_pos:
                    self.notifier.send("RECONCILE: local state has a position but exchange is flat. Clearing it.")
                    self.portfolio.position = None
                    self.execution.adopt(0.0, None, None, None)
                # recover the native protective stop order id from resting open orders
                if has_exch_pos:
                    for o in open_orders:
                        if o.get("side") == "sell" and (o.get("stopPrice") or o.get("triggerPrice")):
                            self.execution.adopt(
                                base_free,
                                float(o.get("stopPrice") or o.get("triggerPrice")),
                                self.execution.protective_target,
                                o.get("id"),
                            )
                            log.info("Recovered native protective stop order %s", o.get("id"))
                            break

        if self.risk.state.kill_switch_engaged:
            self.notifier.send(f"Kill-switch is ENGAGED ({self.risk.state.kill_switch_reason}). "
                               "Trading halted until manually cleared. Exiting.")
            self.engine.halted = True

    # -- run loop -------------------------------------------------------------
    def _next_close_ms(self) -> int:
        tf = timeframe_ms(self.config.timeframe)
        now = self.clock.now_ms()
        return ((now // tf) + 1) * tf

    def _startup(self) -> None:
        backend = "sim (mainnet data, virtual fills)" if self.paper_sim else self.config.mode.value
        log.info("Self-testing exchange connectivity ...")
        st = self.exchange.self_test(self.symbol)
        # Banner is INFO-only: routine start/stop must NOT spam Telegram on every
        # scheduled --once run. Only trades / halts / kill-switch (engine on_alert) notify.
        log.info("Bot starting [%s] on %s %s strategy=%s equity~%.2f %s self-test=%s bars",
                 backend, self.symbol, self.config.timeframe, self.config.strategy.name,
                 self.portfolio.cash, self.config.quote_currency, st.get("ohlcv_bars"))
        self._restore_and_reconcile()
        try:  # process the latest closed bar immediately (guarded like the main loop)
            self._tick()
        except Exception as exc:  # noqa: BLE001
            log.exception("startup tick error")
            self.notifier.send(f"ERROR in tick: {type(exc).__name__}: {exc}")

    def _close(self) -> None:
        self.store.close()
        self.journal.close()

    def _cleanup(self) -> None:
        self._persist()
        self._close()

    def run_once(self) -> None:
        """Process the latest closed bar, then exit — the scheduled deployment path."""
        try:
            self._startup()
        except _TRANSIENT as exc:
            # Binance briefly unreachable: nothing was loaded/mutated yet, so close
            # WITHOUT persisting (don't clobber prior state) and retry on the next run.
            log.warning("Binance unreachable, skipping this tick: %s", exc)
            self._close()
            return
        self._cleanup()

    def run(self) -> None:
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self.shutdown.set())
                except (ValueError, OSError):
                    pass  # not in main thread / unsupported

        self._startup()
        self.notifier.send(
            f"Bot started ({'paper-sim' if self.paper_sim else self.config.mode.value}) on "
            f"{self.symbol} {self.config.timeframe}, strategy={self.config.strategy.name}."
        )
        while not self.shutdown.is_set():
            target = self._next_close_ms() + CONFIRM_BUFFER_MS
            wait_s = max(0.0, (target - self.clock.now_ms()) / 1000.0)
            if self.shutdown.wait(timeout=wait_s):
                break
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — never die silently mid-loop
                log.exception("tick error")
                self.notifier.send(f"ERROR in tick: {type(exc).__name__}: {exc}")

        self._cleanup()
        self.notifier.send("Bot stopped gracefully; state persisted.")
        log.info("Shutdown complete.")

    def _tick(self) -> None:
        slc = self.feed.latest_closed()
        if slc is None:
            return
        self.engine.step(slc)
        self.feed.mark_processed(slc)
        # use the bar's close time (the data's time base), not wall-clock, for the trigger
        self._maybe_weekly_report(slc.bar.close_time_ms)
        self._persist()
        if self.engine.halted:
            self.notifier.send("Engine halted (kill-switch). Stopping loop.")
            self.shutdown.set()

    def _maybe_weekly_report(self, now_ms: int) -> None:
        """Emit the reflection for EVERY completed ISO week, exactly once.

        A catch-up loop ensures that if the bot was offline across one or more
        week boundaries, each elapsed week still gets its report (rather than
        silently skipping to the current week)."""
        wk_start, _ = iso_week_bounds(now_ms)
        if self._cur_week_start is None:
            self._cur_week_start = wk_start
            return
        while self._cur_week_start < wk_start:
            try:
                label, md = build_weekly_report(self.journal, self.config, self._cur_week_start)
                path = write_report(md, label, self.config.reports_dir)
                self.notifier.send(f"Weekly reflection {label} written to {path}")
                log.info("Weekly report %s -> %s", label, path)
            except Exception as exc:  # noqa: BLE001 — a report failure must not stop trading
                log.exception("weekly report failed: %s", exc)
            self._cur_week_start += _WEEK_MS
