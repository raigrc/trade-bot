"""Portfolio accounting. ``apply_fill`` is the ONLY ledger mutator.

Both backtest and live feed identical ``Fill`` objects through ``apply_fill``,
which is what makes accounting bit-for-bit identical across modes. Cash and
positions are never updated from a *requested* order — only from actual fills.

Single-symbol, long-only spot (capital-preservation default of 1 concurrent
position). Quote currency is USDT.
"""

from __future__ import annotations

import logging
from typing import Optional

from .enums import Side
from .types import AccountState, Fill, Position, Trade

log = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, initial_cash: float, symbol: str, quote: str = "USDT") -> None:
        self.symbol = symbol
        self.quote = quote
        self.cash = float(initial_cash)
        self.position: Optional[Position] = None
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.closed_trades: list[Trade] = []

    # -- queries ---------------------------------------------------------------
    def equity(self, mark_price: Optional[float] = None) -> float:
        eq = self.cash
        if self.position is not None and mark_price is not None:
            eq += self.position.notional(mark_price)
        return eq

    def unrealized_pnl(self, mark_price: float) -> float:
        return self.position.unrealized_pnl(mark_price) if self.position else 0.0

    def exposure_pct(self, mark_price: float) -> float:
        eq = self.equity(mark_price)
        if eq <= 0 or self.position is None:
            return 0.0
        return self.position.notional(mark_price) / eq

    def account_state(self, mark_price: float) -> AccountState:
        positions = (self.position,) if self.position is not None else ()
        return AccountState(equity=self.equity(mark_price), free=self.cash, positions=positions)

    # -- persistence (live crash recovery) -------------------------------------
    def state_dict(self) -> dict:
        pos = None
        if self.position is not None:
            p = self.position
            pos = {
                "symbol": p.symbol, "side": p.side.value, "qty": p.qty, "avg_entry": p.avg_entry,
                "stop": p.stop, "target": p.target, "opened_ms": p.opened_ms,
                "highest_since_entry": p.highest_since_entry, "lowest_since_entry": p.lowest_since_entry,
                "bars_held": p.bars_held, "entry_fee": p.entry_fee, "entry_reason": p.entry_reason,
            }
        return {"cash": self.cash, "realized_pnl": self.realized_pnl, "fees_paid": self.fees_paid, "position": pos}

    def load_state(self, d: dict) -> None:
        self.cash = d.get("cash", self.cash)
        self.realized_pnl = d.get("realized_pnl", 0.0)
        self.fees_paid = d.get("fees_paid", 0.0)
        pos = d.get("position")
        if pos:
            self.position = Position(
                symbol=pos["symbol"], side=Side(pos["side"]), qty=pos["qty"], avg_entry=pos["avg_entry"],
                stop=pos.get("stop"), target=pos.get("target"), opened_ms=pos.get("opened_ms", 0),
                highest_since_entry=pos.get("highest_since_entry", 0.0),
                lowest_since_entry=pos.get("lowest_since_entry", 0.0),
                bars_held=pos.get("bars_held", 0), entry_fee=pos.get("entry_fee", 0.0),
                entry_reason=pos.get("entry_reason", ""),
            )
        else:
            self.position = None

    # -- the sole mutator ------------------------------------------------------
    def apply_fill(self, fill: Fill, reason: str = "") -> Optional[Trade]:
        """Mutate cash/position from an actual fill. Returns a Trade if a
        position was fully closed, else None."""
        self.fees_paid += fill.fee
        if fill.side == Side.BUY:
            self._apply_buy(fill, reason)
            return None
        return self._apply_sell(fill, reason)

    def _apply_buy(self, fill: Fill, reason: str) -> None:
        cost = fill.filled_qty * fill.avg_price + fill.fee
        self.cash -= cost
        if self.position is None:
            self.position = Position(
                symbol=fill.symbol,
                side=Side.BUY,
                qty=fill.filled_qty,
                avg_entry=fill.avg_price,
                opened_ms=fill.ts_ms,
                highest_since_entry=fill.avg_price,
                lowest_since_entry=fill.avg_price,
                entry_fee=fill.fee,
                entry_reason=reason,
            )
        else:  # average in (not used by capital-preservation default, but correct)
            p = self.position
            total_qty = p.qty + fill.filled_qty
            p.avg_entry = (p.avg_entry * p.qty + fill.avg_price * fill.filled_qty) / total_qty
            p.qty = total_qty
            p.entry_fee += fill.fee

    def _apply_sell(self, fill: Fill, reason: str) -> Optional[Trade]:
        proceeds = fill.filled_qty * fill.avg_price - fill.fee
        self.cash += proceeds
        if self.position is None:
            log.warning("Sell fill with no open position: %s", fill)
            return None
        p = self.position
        qty_closed = min(fill.filled_qty, p.qty)
        frac = qty_closed / p.qty if p.qty else 1.0
        entry_cost = p.avg_entry * qty_closed
        entry_fee_part = p.entry_fee * frac
        gross = (fill.avg_price - p.avg_entry) * qty_closed
        net_pnl = gross - entry_fee_part - fill.fee
        self.realized_pnl += net_pnl

        trade = Trade(
            symbol=p.symbol,
            side=Side.BUY,
            qty=qty_closed,
            entry_price=p.avg_entry,
            exit_price=fill.avg_price,
            entry_ms=p.opened_ms,
            exit_ms=fill.ts_ms,
            pnl=net_pnl,
            fees=entry_fee_part + fill.fee,
            return_pct=(net_pnl / entry_cost) if entry_cost else 0.0,
            bars_held=p.bars_held,
            entry_reason=p.entry_reason,
            exit_reason=reason,
        )
        self.closed_trades.append(trade)

        remaining = p.qty - qty_closed
        if remaining <= 1e-12:
            self.position = None
        else:
            p.qty = remaining
            p.entry_fee -= entry_fee_part
        return trade
