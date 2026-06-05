"""Core immutable data types passed between the engine's components.

Design rules encoded here:
- ``Bar`` is the last CLOSED candle; the strategy never sees a forming candle.
- ``Signal`` expresses INTENT only and carries NO quantity — sizing is the
  RiskManager's job (sizing off stop distance is the heart of capital
  preservation).
- ``Fill`` is what execution actually returns (may be partial). The portfolio
  is mutated only from Fills, never from requested Orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .enums import OrderStatus, OrderType, RejectCode, Side

# Timeframe -> milliseconds
TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


def timeframe_ms(tf: str) -> int:
    try:
        return TF_MS[tf]
    except KeyError as exc:  # pragma: no cover - config guard
        raise ValueError(f"Unsupported timeframe: {tf!r}") from exc


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    open_time_ms: int  # epoch ms of the candle OPEN
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str

    @property
    def close_time_ms(self) -> int:
        return self.open_time_ms + timeframe_ms(self.timeframe)

    def is_closed(self, now_ms: int) -> bool:
        """A bar is closed once wall/sim time has passed its close boundary."""
        return self.close_time_ms <= now_ms


@dataclass(frozen=True, slots=True)
class Signal:
    """Strategy output. INTENT only — no quantity (RiskManager sizes it)."""

    side: Side
    reason: str
    stop: Optional[float] = None
    target: Optional[float] = None
    confidence: float = 1.0
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None


@dataclass(frozen=True, slots=True)
class Order:
    symbol: str
    side: Side
    qty: float  # base-asset units, already exchange-precision-rounded
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    client_order_id: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    symbol: str
    side: Side
    filled_qty: float
    avg_price: float
    fee: float  # in quote currency
    status: OrderStatus
    ts_ms: int
    fee_currency: str = "USDT"
    raw: Optional[dict] = None


@dataclass(slots=True)
class Position:
    """An open spot position (long-only). Mutated only via Portfolio.apply_fill."""

    symbol: str
    side: Side  # BUY = long
    qty: float
    avg_entry: float
    stop: Optional[float] = None
    target: Optional[float] = None
    opened_ms: int = 0
    highest_since_entry: float = 0.0  # for chandelier trailing stop
    lowest_since_entry: float = 0.0
    bars_held: int = 0
    entry_fee: float = 0.0
    entry_reason: str = ""

    def notional(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_entry) * self.qty  # long-only spot


@dataclass(frozen=True, slots=True)
class Trade:
    """A completed round-trip, recorded on position close (for metrics/journal)."""

    symbol: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    entry_ms: int
    exit_ms: int
    pnl: float  # net of entry + exit fees, in quote currency
    fees: float
    return_pct: float  # pnl / entry notional
    bars_held: int
    entry_reason: str
    exit_reason: str


@dataclass(frozen=True, slots=True)
class MarketInfo:
    """Exchange trading rules for a symbol (Binance filters)."""

    symbol: str
    is_spot: bool
    base: str
    quote: str
    tick_size: float
    step_size: float
    min_qty: float
    max_qty: float
    min_notional: float


@dataclass(frozen=True, slots=True)
class AccountState:
    equity: float  # total equity in quote currency (USDT)
    free: float  # free quote balance available to deploy
    positions: tuple[Position, ...] = ()

    def position_for(self, symbol: str) -> Optional[Position]:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


@dataclass(frozen=True, slots=True)
class Approval:
    """RiskManager said yes — carries the fully-resolved, sized order plan."""

    order: Order
    stop_price: float
    target_price: Optional[float]
    est_risk_quote: float
    est_risk_pct: float
    approved: bool = True


@dataclass(frozen=True, slots=True)
class Rejection:
    code: RejectCode
    message: str
    context: dict = field(default_factory=dict)
    retryable: bool = False
    halt: bool = False
    approved: bool = False


RiskDecision = Approval | Rejection
