"""RiskManager — the capital-preservation core.

Sits between the strategy signal and execution. ``evaluate`` runs a fail-fast
pipeline (global halts first, then per-trade math) and returns an ``Approval``
(carrying the fully-sized, filter-checked order) or a ``Rejection`` with a code.

Only ENTRY (BUY) signals are evaluated here. Exits (SELL) always bypass risk —
reducing risk is never blocked.

The same RiskManager + RiskConfig instance runs in backtest, paper, and live.
``RiskState`` is in-memory here; persistence.py (M4) serialises it so the
drawdown peak / kill-switch / cooldowns survive restarts.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Optional

from .clock import utc_day_start_ms
from .config import RiskConfig
from .correlation import correlation_penalty
from .enums import OrderType, RejectCode, Side
from .types import (
    AccountState,
    Approval,
    MarketInfo,
    Order,
    Rejection,
    RiskDecision,
    Signal,
    Trade,
)

log = logging.getLogger(__name__)
_MINUTE_MS = 60_000


class RiskState:
    """Mutable risk accounting. Persisted for live (see persistence.py)."""

    def __init__(self) -> None:
        self.peak_equity: float = 0.0
        self.kill_switch_engaged: bool = False
        self.kill_switch_reason: str = ""
        self.day_start_equity: float = 0.0
        self.day_start_ms: int = -1  # sentinel: first equity update always anchors the day
        self.consecutive_losses: int = 0
        self.cooldown_until_ms: int = 0
        self.symbol_reentry_until: dict[str, int] = {}
        self.cooldown_count: int = 0  # how many streak-breakers fired (escalation)

    def state_dict(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "kill_switch_engaged": self.kill_switch_engaged,
            "kill_switch_reason": self.kill_switch_reason,
            "day_start_equity": self.day_start_equity,
            "day_start_ms": self.day_start_ms,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until_ms": self.cooldown_until_ms,
            "symbol_reentry_until": dict(self.symbol_reentry_until),
            "cooldown_count": self.cooldown_count,
        }

    _STATE_KEYS = frozenset({
        "peak_equity", "kill_switch_engaged", "kill_switch_reason",
        "day_start_equity", "day_start_ms", "consecutive_losses",
        "cooldown_until_ms", "symbol_reentry_until", "cooldown_count",
    })

    def load_state(self, d: dict) -> None:
        for k, v in d.items():
            if k in self._STATE_KEYS:
                setattr(self, k, v)


class RiskManager:
    def __init__(self, config: RiskConfig, state: Optional[RiskState] = None) -> None:
        self.cfg = config
        self.state = state or RiskState()

    # -- lifecycle hooks (called by the engine every bar / on close) -----------
    def on_equity_update(self, equity: float, now_ms: int) -> bool:
        """Update peak + daily anchor; engage kill-switch on max drawdown.

        Returns True if the drawdown kill-switch just engaged (engine must
        flatten + halt)."""
        s = self.state
        day0 = utc_day_start_ms(now_ms)
        if s.day_start_ms != day0:
            s.day_start_ms = day0
            s.day_start_equity = equity
        if equity > s.peak_equity:
            s.peak_equity = equity
        if s.peak_equity > 0:
            dd = (s.peak_equity - equity) / s.peak_equity
            if dd >= self.cfg.max_drawdown_pct and not s.kill_switch_engaged:
                s.kill_switch_engaged = True
                s.kill_switch_reason = f"max drawdown {dd:.1%} >= {self.cfg.max_drawdown_pct:.0%}"
                log.error("KILL-SWITCH ENGAGED: %s", s.kill_switch_reason)
                return True
        return False

    def on_trade_closed(self, trade: Trade, now_ms: int) -> None:
        s = self.state
        band = self.cfg.breakeven_band_pct
        if trade.return_pct < -band:
            s.consecutive_losses += 1
            # per-symbol re-entry cooldown after a loss (avoid getting chopped)
            s.symbol_reentry_until[trade.symbol] = now_ms + self.cfg.reentry_cooldown_minutes * _MINUTE_MS
            if s.consecutive_losses >= self.cfg.loss_streak_threshold:
                mins = self.cfg.cooldown_minutes
                if self.cfg.cooldown_escalates:
                    mins = min(self.cfg.cooldown_max_minutes, mins * (2 ** s.cooldown_count))
                s.cooldown_until_ms = now_ms + mins * _MINUTE_MS
                s.cooldown_count += 1
                s.consecutive_losses = 0
                log.warning("Loss-streak cooldown for %d min", mins)
        elif trade.return_pct > band:
            s.consecutive_losses = 0  # a win resets the streak

    def clear_kill_switch(self) -> None:
        """Manual re-arm (operator action). Resets peak to current on next update."""
        self.state.kill_switch_engaged = False
        self.state.kill_switch_reason = ""
        self.state.peak_equity = 0.0

    # -- the gate pipeline -----------------------------------------------------
    def evaluate(
        self,
        signal: Signal,
        account: AccountState,
        market: MarketInfo,
        ref_price: float,
        now_ms: int,
    ) -> RiskDecision:
        cfg = self.cfg
        s = self.state

        # 1. kill switch
        if s.kill_switch_engaged:
            return self._reject(RejectCode.KILL_SWITCH_ENGAGED, s.kill_switch_reason, halt=True)

        # 2. market type / leverage — spot only, no margin/futures
        if cfg.spot_only and not market.is_spot:
            return self._reject(RejectCode.LEVERAGE_FORBIDDEN, f"{market.symbol} is not a spot market")
        if cfg.max_leverage > 1.0:
            return self._reject(RejectCode.LEVERAGE_FORBIDDEN, "leverage > 1 is forbidden")

        # 3. daily loss limit (uses equity incl. unrealized vs the day's start)
        if s.day_start_equity > 0:
            day_loss = (s.day_start_equity - account.equity) / s.day_start_equity
            if day_loss >= cfg.daily_loss_limit_pct:
                return self._reject(
                    RejectCode.DAILY_LOSS_LIMIT,
                    f"daily loss {day_loss:.1%} >= {cfg.daily_loss_limit_pct:.0%}",
                    retryable=True,
                )

        # 4. drawdown backstop (primary check is on_equity_update each bar)
        if s.peak_equity > 0:
            dd = (s.peak_equity - account.equity) / s.peak_equity
            if dd >= cfg.max_drawdown_pct:
                return self._reject(RejectCode.MAX_DRAWDOWN, f"drawdown {dd:.1%}", halt=True)

        # 5. loss-streak cooldown
        if now_ms < s.cooldown_until_ms:
            return self._reject(RejectCode.LOSS_STREAK_COOLDOWN, "in loss-streak cooldown", retryable=True)

        # 6. per-symbol re-entry cooldown
        if now_ms < s.symbol_reentry_until.get(market.symbol, 0):
            return self._reject(RejectCode.SYMBOL_REENTRY_COOLDOWN, "symbol re-entry cooldown", retryable=True)

        # 7. signal validity (entries only)
        if signal.side != Side.BUY:
            return self._reject(RejectCode.INVALID_SIGNAL, "risk.evaluate only sizes BUY entries")
        if signal.confidence <= 0:
            return self._reject(RejectCode.INVALID_SIGNAL, "non-positive confidence")

        # 8. mandatory stop, on the correct side, within distance band
        if cfg.require_stop and signal.stop is None:
            return self._reject(RejectCode.NO_VALID_STOP, "entry has no stop (mandatory)")
        stop = float(signal.stop)
        if stop <= 0 or stop >= ref_price:
            return self._reject(RejectCode.NO_VALID_STOP, f"stop {stop} not below entry {ref_price}")
        stop_dist_pct = (ref_price - stop) / ref_price
        if not (cfg.min_stop_dist_pct <= stop_dist_pct <= cfg.max_stop_dist_pct):
            return self._reject(
                RejectCode.NO_VALID_STOP,
                f"stop distance {stop_dist_pct:.2%} outside [{cfg.min_stop_dist_pct:.2%},"
                f"{cfg.max_stop_dist_pct:.0%}]",
            )

        # 9. exposure caps
        if account.position_for(market.symbol) is not None:
            return self._reject(RejectCode.ALREADY_IN_POSITION, "already in a position for this symbol")
        if len(account.positions) >= cfg.max_concurrent_positions:
            return self._reject(RejectCode.MAX_POSITIONS, f"max {cfg.max_concurrent_positions} positions")

        # 10. position sizing (fixed-fractional, off stop distance, costs folded in)
        size, est_risk = self._size(account.equity, ref_price, stop, market)

        # 10b. correlation-aware sizing penalty
        open_symbols = [p.symbol for p in account.positions]
        corr_penalty = correlation_penalty(open_symbols, market.symbol, cfg.data_dir)
        if corr_penalty < 1.0:
            size *= corr_penalty
            size = self._round_down(size, market.step_size)
            est_risk = size * self._risk_per_unit(ref_price, stop)
            log.info(
                "Correlation penalty %.2f applied to %s (open: %s)",
                corr_penalty, market.symbol, open_symbols,
            )
            # Re-check min notional after correlation adjustment
            notional = size * ref_price
            if size <= 0 or size < market.min_qty or (
                market.min_notional and notional < market.min_notional
            ):
                return self._reject(
                    RejectCode.SIZE_BELOW_MIN,
                    f"correlation-adjusted size {size} / notional {notional:.2f} "
                    f"below minimum (penalty={corr_penalty:.2f})",
                    context={"size": size, "notional": notional, "corr_penalty": corr_penalty},
                )
            # Re-check risk hard cap after correlation adjustment
            if est_risk / account.equity > cfg.risk_hard_cap_per_trade:
                return self._reject(
                    RejectCode.SIZE_EXCEEDS_MAX,
                    f"correlation-adjusted risk {est_risk / account.equity:.2%} "
                    f"exceeds hard cap {cfg.risk_hard_cap_per_trade:.0%}",
                )

        # 11. min/max exchange filters
        notional = size * ref_price
        if size <= 0 or size < market.min_qty or (market.min_notional and notional < market.min_notional):
            if cfg.allow_min_notional_override and market.min_notional:
                size = self._round_down(max(market.min_qty, market.min_notional / ref_price), market.step_size)
                notional = size * ref_price
                est_risk = size * self._risk_per_unit(ref_price, stop)
                if est_risk / account.equity > cfg.risk_hard_cap_per_trade:
                    return self._reject(RejectCode.SIZE_BELOW_MIN,
                                        "min-notional override would exceed risk hard cap")
            else:
                return self._reject(
                    RejectCode.SIZE_BELOW_MIN,
                    f"size {size} / notional {notional:.2f} below exchange minimum "
                    f"(min_qty={market.min_qty}, min_notional={market.min_notional})",
                    context={"size": size, "notional": notional},
                )
        if market.max_qty and size > market.max_qty:
            return self._reject(RejectCode.SIZE_EXCEEDS_MAX, f"size {size} > max_qty {market.max_qty}")

        # 12. net-edge gate (only if a target is defined)
        if signal.target is not None:
            reward_per_unit = signal.target - ref_price
            cost_per_unit = self._cost_per_unit(ref_price, stop)
            stop_dist = ref_price - stop
            if reward_per_unit - cost_per_unit <= cfg.min_rr_after_costs * stop_dist:
                return self._reject(
                    RejectCode.INSUFFICIENT_EDGE,
                    f"reward/risk after costs below {cfg.min_rr_after_costs}",
                )

        # 13. balance
        est_fee = notional * cfg.taker_fee_pct
        if notional + est_fee > account.free:
            return self._reject(
                RejectCode.INSUFFICIENT_BALANCE,
                f"need {notional + est_fee:.2f} but only {account.free:.2f} free",
            )

        order = Order(
            symbol=market.symbol,
            side=Side.BUY,
            qty=size,
            order_type=OrderType.MARKET,
            stop_price=stop,
            client_order_id=self._client_id(market.symbol, now_ms),
        )
        return Approval(
            order=order,
            stop_price=stop,
            target_price=signal.target,
            est_risk_quote=est_risk,
            est_risk_pct=est_risk / account.equity if account.equity else 0.0,
        )

    # -- sizing math -----------------------------------------------------------
    def _risk_per_unit(self, entry: float, stop: float) -> float:
        return (entry - stop) + self._cost_per_unit(entry, stop)

    def _cost_per_unit(self, entry: float, stop: float) -> float:
        cfg = self.cfg
        fee = entry * cfg.taker_fee_pct + stop * cfg.taker_fee_pct
        slip = entry * cfg.slippage_pct + stop * cfg.stop_slippage_pct
        return fee + slip

    def _size(self, equity: float, entry: float, stop: float, market: MarketInfo) -> tuple[float, float]:
        risk_capital = equity * self.cfg.risk_fraction_per_trade
        rpu = self._risk_per_unit(entry, stop)
        if rpu <= 0:
            return 0.0, 0.0
        raw = risk_capital / rpu
        size = self._round_down(raw, market.step_size)
        return size, size * rpu

    @staticmethod
    def _round_down(qty: float, step: float) -> float:
        if step and step > 0:
            return math.floor(qty / step) * step
        return qty

    # -- helpers ---------------------------------------------------------------
    def _client_id(self, symbol: str, now_ms: int) -> str:
        return f"tb-{symbol.replace('/', '')}-{now_ms}-{uuid.uuid4().hex[:6]}"

    @staticmethod
    def _reject(code: RejectCode, message: str, *, retryable: bool = False,
                halt: bool = False, context: Optional[dict] = None) -> Rejection:
        return Rejection(code=code, message=message, retryable=retryable, halt=halt,
                         context=context or {})
