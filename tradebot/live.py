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
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import httpx

from .backtest import synthetic_market_info
from .clock import LiveClock
from .alpha.composite import CompositeAlpha
from .config import BotConfig, Secrets
from .data import CcxtLiveFeed
from .engine import Engine, MultiEngine
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
_MAX_CONSECUTIVE_TRANSIENT = 5  # alert after this many consecutive failures


class LiveRunner:
    def __init__(self, config: BotConfig, secrets: Secrets) -> None:
        config.assert_live_allowed()
        self.config = config
        self.symbol = config.symbols[0]  # primary symbol (used for backward compat)
        self._multi = len(config.symbols) > 1
        self.clock = LiveClock()
        self.shutdown = threading.Event()

        self.notifier = Notifier(secrets)
        # paper-sim: live mainnet public data + simulated fills, NO keys, virtual equity
        self.paper_sim = config.mode == Mode.PAPER and config.paper_execution == "sim"

        # single-symbol uses the symbol name; multi-symbol uses a shared DB
        sym_part = "multi" if self._multi else self.symbol.replace("/", "_")
        db = Path(config.state_dir) / f"{sym_part}_{config.mode.value}.sqlite"
        self.store = StateStore(db)
        self.journal = TradeJournal(db)

        if self._multi:
            self._init_multi(config, secrets)
        else:
            self._init_single(config, secrets)

        self._cur_week_start: int | None = None  # ISO week currently in progress
        self._consecutive_transient: int = 0  # circuit breaker counter
        self._last_heartbeat_ms: int = 0  # epoch-ms of last heartbeat send
        self._tg_update_id: int = 0  # Telegram getUpdates offset (dedup)

    # -- initialization --------------------------------------------------------
    def _init_single(self, config: BotConfig, secrets: Secrets) -> None:
        """Single-symbol initialization (backward-compatible path)."""
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
        self.alpha = CompositeAlpha()
        self.engine = Engine(
            self.symbol, self.clock, self.strategy, self.risk, self.execution, self.portfolio, self.market,
            on_trade=self.journal.record, on_alert=self.notifier.send,
            on_equity=self.journal.record_equity,
            alpha_provider=self.alpha,
        )
        self.feeds: dict[str, CcxtLiveFeed] = {self.symbol: self.feed}
        self.multi_engine: MultiEngine | None = None

    def _init_multi(self, config: BotConfig, secrets: Secrets) -> None:
        """Multi-symbol initialization — one engine/feed per symbol."""
        if self.paper_sim:
            self.exchange = BinanceExchange(config, secrets, public_data_only=True)
        else:
            self.exchange = BinanceExchange(config, secrets)

        # Fetch account balance once for all symbols
        if not self.paper_sim:
            initial_cash = self.exchange.free_quote(config.quote_currency)
        else:
            initial_cash = config.initial_equity

        engines: dict[str, Engine] = {}
        self.feeds = {}
        alpha = CompositeAlpha()

        for symbol in config.symbols:
            strategy = build_strategy(config.strategy.name, symbol, config.timeframe, config.strategy.params)
            risk = RiskManager(config.risk, RiskState())

            if self.paper_sim:
                market = synthetic_market_info(symbol, config.quote_currency)
                execution = SimulatedExecution(config.risk, market)
            else:
                market = self.exchange.market_info(symbol)
                execution = CcxtExecution(self.exchange, market, config.risk)
            portfolio = Portfolio(initial_cash, symbol, config.quote_currency)

            feed = CcxtLiveFeed(
                self.exchange, self.clock, symbol, config.timeframe, config.htf_timeframe
            )
            self.feeds[symbol] = feed

            engines[symbol] = Engine(
                symbol, self.clock, strategy, risk, execution, portfolio, market,
                on_trade=self.journal.record, on_alert=self.notifier.send,
                on_equity=self.journal.record_equity,
                alpha_provider=alpha,
            )

        self.multi_engine = MultiEngine(
            engines,
            on_trade=self.journal.record,
            on_alert=self.notifier.send,
            on_equity=self.journal.record_equity,
        )
        self.engine = None  # type: ignore[assignment]  # unused in multi mode
        self.alpha = alpha

    # -- state ----------------------------------------------------------------
    def _persist(self) -> None:
        now = self.clock.now_ms()
        if self._multi:
            assert self.multi_engine is not None
            self.store.put("multi_engine", self.multi_engine.state_dict(), now)
            for sym, feed in self.feeds.items():
                self.store.put(f"{sym}/meta", {"last_processed_ms": feed.last_processed_ms}, now)
            self.store.put("meta", {"cur_week_start": self._cur_week_start}, now)
        else:
            self.store.put("risk", self.risk.state.state_dict(), now)
            self.store.put("portfolio", self.portfolio.state_dict(), now)
            self.store.put("execution", self.execution.state_dict(), now)
            self.store.put("strategy", self.strategy.state_dict(), now)
            self.store.put("meta", {"last_processed_ms": self.feed.last_processed_ms,
                                    "cur_week_start": self._cur_week_start}, now)

    def _restore_and_reconcile(self) -> None:
        if self._multi:
            self._restore_and_reconcile_multi()
        else:
            self._restore_and_reconcile_single()

    def _restore_and_reconcile_single(self) -> None:
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

    def _restore_and_reconcile_multi(self) -> None:
        """Restore and reconcile all per-symbol engines."""
        assert self.multi_engine is not None

        # 1. load persisted state
        if (d := self.store.get("multi_engine")):
            self.multi_engine.load_state(d)
        for sym, feed in self.feeds.items():
            if (m := self.store.get(f"{sym}/meta")):
                feed.last_processed_ms = int(m.get("last_processed_ms", 0))
        if (m := self.store.get("meta")):
            self._cur_week_start = m.get("cur_week_start")

        # 2. exchange reconciliation (per symbol)
        if not self.paper_sim:
            bal_map: dict[str, float] = {}
            try:
                bal = self.exchange.fetch_balance()
                for sym in self.config.symbols:
                    engine = self.multi_engine.engines[sym]
                    bal_map[sym] = float(bal.get("free", {}).get(engine.market.base, 0.0) or 0.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("Startup reconcile could not query exchange: %s", exc)
                bal_map = {}

            for sym in self.config.symbols:
                engine = self.multi_engine.engines[sym]
                if sym not in bal_map:
                    continue
                base_free = bal_map[sym]
                try:
                    open_orders = self.exchange.fetch_open_orders(sym)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Startup reconcile could not query open orders for %s: %s", sym, exc)
                    open_orders = []

                has_local_pos = engine.portfolio.position is not None
                has_exch_pos = (
                    base_free * self.exchange.best_bid_ask(sym)[0] >= engine.market.min_notional
                )
                if has_exch_pos and not has_local_pos:
                    self.notifier.send(
                        f"RECONCILE [{sym}]: exchange shows ~{base_free} {engine.market.base} "
                        "but local state is flat. Trusting exchange. Verify manually."
                    )
                elif has_local_pos and not has_exch_pos:
                    self.notifier.send(
                        f"RECONCILE [{sym}]: local state has a position but exchange is flat. Clearing it."
                    )
                    engine.portfolio.position = None
                    engine.execution.adopt(0.0, None, None, None)
                # recover the native protective stop order id
                if has_exch_pos:
                    for o in open_orders:
                        if o.get("side") == "sell" and (o.get("stopPrice") or o.get("triggerPrice")):
                            engine.execution.adopt(
                                base_free,
                                float(o.get("stopPrice") or o.get("triggerPrice")),
                                engine.execution.protective_target,
                                o.get("id"),
                            )
                            log.info("Recovered native protective stop order %s for %s", o.get("id"), sym)
                            break

        # 3. kill-switch (any engine triggers global halt)
        if any(e.risk.state.kill_switch_engaged for e in self.multi_engine.engines.values()):
            reasons = [f"{s}: {e.risk.state.kill_switch_reason}"
                       for s, e in self.multi_engine.engines.items()
                       if e.risk.state.kill_switch_engaged]
            self.notifier.send(
                f"Kill-switch is ENGAGED ({'; '.join(reasons)}). "
                "Trading halted until manually cleared. Exiting."
            )
            self.multi_engine.halted = True

    # -- run loop -------------------------------------------------------------
    def _next_close_ms(self) -> int:
        tf = timeframe_ms(self.config.timeframe)
        now = self.clock.now_ms()
        return ((now // tf) + 1) * tf

    def _startup(self) -> None:
        backend = "sim (mainnet data, virtual fills)" if self.paper_sim else self.config.mode.value
        log.info("Self-testing exchange connectivity ...")
        if self._multi:
            for symbol in self.config.symbols:
                st = self.exchange.self_test(symbol)
                log.info("Bot starting [%s] on %s %s strategy=%s self-test=%s bars",
                         backend, symbol, self.config.timeframe, self.config.strategy.name,
                         st.get("ohlcv_bars"))
        else:
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

    # -- heartbeat & /status ---------------------------------------------------
    def _build_status_message(self) -> str:
        """Build the compact heartbeat/status summary (mirrors paper_status.py)."""
        if self._multi:
            return self._build_status_message_multi()

        cfg = self.config
        q = cfg.quote_currency
        risk_data = self.store.get("risk") or {}
        portfolio_data = self.store.get("portfolio") or {}
        meta_data = self.store.get("meta") or {}

        # equity & drawdown
        current_equity = self.journal.latest_equity()
        if current_equity is None:
            current_equity = cfg.initial_equity
        peak_equity = float(risk_data.get("peak_equity", 0.0))
        if peak_equity <= 0:
            peak_equity = current_equity
        drawdown = ((peak_equity - current_equity) / peak_equity) if peak_equity > 0 else 0.0

        # open position
        pos = portfolio_data.get("position")
        cash = float(portfolio_data.get("cash", cfg.initial_equity))

        # trades this week
        now_ms = self.clock.now_ms()
        week_start, _ = iso_week_bounds(now_ms)
        n_week = len(self.journal.trades_between(week_start, week_start + _WEEK_MS))

        # kill-switch
        kill_engaged = bool(risk_data.get("kill_switch_engaged", False))
        kill_reason = risk_data.get("kill_switch_reason", "")
        kill_str = f"ENGAGED — {kill_reason}" if kill_engaged else "clear"

        # last bar
        last_ms = meta_data.get("last_processed_ms")
        last_bar = (
            datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if last_ms else "n/a"
        )

        # position line
        if pos is not None:
            pos_str = (
                f"{pos['side'].upper()} {float(pos['qty']):.6f} {cfg.symbols[0].split('/')[0]}"
                f" @ {float(pos['avg_entry']):.2f}"
            )
        else:
            pos_str = "none"

        header = f"{cfg.symbols[0]} {cfg.strategy.name} {cfg.timeframe}"
        return (
            f"Heartbeat | {header}\n"
            f"Equity: {current_equity:.2f} {q} | Peak: {peak_equity:.2f} | DD: {drawdown:.2%}\n"
            f"Position: {pos_str} | Cash: {cash:.2f}\n"
            f"Kill-switch: {kill_str} | Trades this week: {n_week}\n"
            f"Last bar: {last_bar}"
        )

    def _build_status_message_multi(self) -> str:
        """Build status message for multi-symbol mode."""
        cfg = self.config
        q = cfg.quote_currency
        assert self.multi_engine is not None

        now_ms = self.clock.now_ms()
        week_start, _ = iso_week_bounds(now_ms)
        n_week = len(self.journal.trades_between(week_start, week_start + _WEEK_MS))

        # Portfolio-level equity from the last aggregated entry
        if self.multi_engine.equity_curve:
            current_equity = self.multi_engine.equity_curve[-1][1]
        else:
            current_equity = cfg.initial_equity
        # Peak across all sub-engines
        peak_equity = max(
            (e.risk.state.peak_equity for e in self.multi_engine.engines.values()),
            default=current_equity,
        )
        if peak_equity <= 0:
            peak_equity = current_equity
        drawdown = ((peak_equity - current_equity) / peak_equity) if peak_equity > 0 else 0.0

        # Positions per symbol
        pos_parts = []
        for sym, engine in self.multi_engine.engines.items():
            pos = engine.portfolio.position
            if pos is not None:
                pos_parts.append(f"{sym}: {pos.side.value.upper()} {pos.qty:.6f} @ {pos.avg_entry:.2f}")
            else:
                pos_parts.append(f"{sym}: FLAT")
        pos_str = " | ".join(pos_parts)

        # Kill-switch (any engine)
        kill_engaged = any(e.risk.state.kill_switch_engaged for e in self.multi_engine.engines.values())
        kill_str = "ENGAGED" if kill_engaged else "clear"

        # Last bar (most recent across all feeds)
        last_ms = max(
            (f.last_processed_ms for f in self.feeds.values()),
            default=0,
        )
        last_bar = (
            datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if last_ms else "n/a"
        )

        header = f"multi-symbol ({len(cfg.symbols)}) {cfg.strategy.name} {cfg.timeframe}"
        return (
            f"Heartbeat | {header}\n"
            f"Equity: {current_equity:.2f} {q} | Peak: {peak_equity:.2f} | DD: {drawdown:.2%}\n"
            f"Positions: {pos_str}\n"
            f"Kill-switch: {kill_str} | Trades this week: {n_week}\n"
            f"Last bar: {last_bar}"
        )

    def _maybe_send_heartbeat(self) -> None:
        """Send a heartbeat if ``heartbeat_hours`` have elapsed since the last one."""
        cfg = self.config
        if cfg.heartbeat_hours <= 0:
            return
        now_ms = self.clock.now_ms()
        elapsed_ms = now_ms - self._last_heartbeat_ms
        if elapsed_ms < cfg.heartbeat_hours * 3_600_000 and self._last_heartbeat_ms != 0:
            return
        self._last_heartbeat_ms = now_ms
        try:
            msg = self._build_status_message()
            self.notifier.send(msg)
        except Exception as exc:  # noqa: BLE001 — heartbeat must never crash the bot
            log.warning("Heartbeat send failed: %s", exc)

    def _poll_status_command(self) -> None:
        """Poll Telegram ``getUpdates`` for /status commands (non-blocking).

        If Telegram is not configured this is a no-op.
        """
        if not self.notifier.enabled:
            return
        try:
            resp = httpx.get(
                f"https://api.telegram.org/bot{self.notifier._token}/getUpdates",
                params={
                    "offset": self._tg_update_id,
                    "timeout": 2,  # short long-poll — does not block the tick loop
                    "allowed_updates": '["message"]',
                },
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            for update in data.get("result", []):
                uid = update.get("update_id", 0)
                if uid >= self._tg_update_id:
                    self._tg_update_id = uid + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                if "/status" in text:
                    log.info("Received /status command from chat %s", msg.get("chat", {}).get("id"))
                    status_msg = self._build_status_message()
                    self.notifier.send(status_msg)
        except Exception as exc:  # noqa: BLE001 — polling must never crash the bot
            log.debug("Telegram getUpdates failed (will retry): %s", exc)

    def run_once(self) -> None:
        """Process the latest closed bar, then exit — the scheduled deployment path."""
        try:
            self._startup()
        except _TRANSIENT as exc:
            # Binance briefly unreachable: nothing was loaded/mutated yet, so close
            # WITHOUT persisting (don't clobber prior state) and retry on the next run.
            self._consecutive_transient += 1
            log.warning("Binance unreachable (%d/%d), skipping this tick: %s",
                        self._consecutive_transient, _MAX_CONSECUTIVE_TRANSIENT, exc)
            if self._consecutive_transient >= _MAX_CONSECUTIVE_TRANSIENT:
                self.notifier.send(
                    f"WARNING: {self._consecutive_transient} consecutive exchange failures. "
                    "Check connectivity. Will keep retrying."
                )
            self._close()
            return
        self._consecutive_transient = 0
        self._cleanup()

    def run(self) -> None:
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self.shutdown.set())
                except (ValueError, OSError):
                    pass  # not in main thread / unsupported

        self._startup()
        symbols_str = ", ".join(self.config.symbols) if self._multi else self.symbol
        self.notifier.send(
            f"Bot started ({'paper-sim' if self.paper_sim else self.config.mode.value}) on "
            f"{symbols_str} {self.config.timeframe}, strategy={self.config.strategy.name}."
        )
        self._last_heartbeat_ms = self.clock.now_ms()  # arm heartbeat timer on start
        while not self.shutdown.is_set():
            # --- heartbeat + /status (before sleeping so they're responsive) ---
            self._maybe_send_heartbeat()
            self._poll_status_command()

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
        if self._multi:
            self._tick_multi()
        else:
            self._tick_single()

    def _tick_single(self) -> None:
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

    def _tick_multi(self) -> None:
        """Fetch latest closed bar for each symbol and step the MultiEngine."""
        assert self.multi_engine is not None

        slices: dict[str, object] = {}
        for sym, feed in self.feeds.items():
            slc = feed.latest_closed()
            if slc is not None:
                slices[sym] = slc
        if not slices:
            return

        self.multi_engine.step(slices)  # type: ignore[arg-type]
        for sym, slc in slices.items():
            self.feeds[sym].mark_processed(slc)

        # use the latest bar's close time for the weekly report trigger
        latest_ms = max(slc.bar.close_time_ms for slc in slices.values())
        self._maybe_weekly_report(latest_ms)
        self._persist()

        if self.multi_engine.halted:
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
