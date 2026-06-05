"""Enumerations shared across the bot."""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    NEUTRAL = "neutral"  # 20<=ADX<=25 dead-band: stay flat


class RejectCode(str, Enum):
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    LEVERAGE_FORBIDDEN = "leverage_forbidden"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_DRAWDOWN = "max_drawdown"
    LOSS_STREAK_COOLDOWN = "loss_streak_cooldown"
    SYMBOL_REENTRY_COOLDOWN = "symbol_reentry_cooldown"
    INVALID_SIGNAL = "invalid_signal"
    NO_VALID_STOP = "no_valid_stop"
    ALREADY_IN_POSITION = "already_in_position"
    MAX_POSITIONS = "max_positions"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    MAX_SYMBOL_EXPOSURE = "max_symbol_exposure"
    MAX_GROUP_EXPOSURE = "max_group_exposure"
    SIZE_BELOW_MIN = "size_below_min"
    SIZE_EXCEEDS_MAX = "size_exceeds_max"
    INSUFFICIENT_EDGE = "insufficient_edge"
    INSUFFICIENT_BALANCE = "insufficient_balance"
