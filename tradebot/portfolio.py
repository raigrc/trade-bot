"""Portfolio accounting. ``apply_fill`` is the ONLY ledger mutator.

Both backtest and live feed identical ``Fill`` objects through ``apply_fill``,
which is what makes accounting bit-for-bit identical across modes. Cash and
positions are never updated from a *requested* order — only from actual fills.

Supports one (default, capital-preservation) or many concurrent positions
keyed by symbol.  The ``position`` property preserves full backward
compatibility: it returns the single ``Position`` when exactly one is open
and ``None`` otherwise, so every existing ``portfolio.position`` read-site
keeps working unchanged.
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
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.closed_trades: list[Trade] = []

    # -- backward-compatible single-position access ----------------------------
    @property
    def position(self) -> Optional[Position]:
        """Return the sole position when exactly one is open, else ``None``.

        This keeps all existing single-symbol call-sites working unchanged.
        """
        if len(self.positions) == 1:
            return next(iter(self.positions.values()))
        return None

    @position.setter
    def position(self, value: Optional[Position]) -> None:
        if value is None:
            self.positions.clear()
        else:
            self.positions = {value.symbol: value}

    # -- queries ---------------------------------------------------------------
    def equity(
        self,
        mark_price: Optional[float] = None,
        mark_prices: Optional[dict[str, float]] = None,
    ) -> float:
        """Total equity = cash + sum of position notionals.

        *mark_price* is used for the single-position (backward-compatible) path.
        *mark_prices* maps symbol -> price for the multi-symbol path; when
        provided it takes precedence over *mark_price* for each symbol.
        """
        eq = self.cash
        for sym, pos in self.positions.items():
            price: Optional[float] = None
            if mark_prices is not None:
                price = mark_prices.get(sym)
            if price is None:
                price = mark_price
            if price is not None:
                eq += pos.notional(price)
        return eq

    def unrealized_pnl(
        self,
        mark_price: float,
        mark_prices: Optional[dict[str, float]] = None,
    ) -> float:
        """Sum of unrealised PnL across all open positions."""
        total = 0.0
        for sym, pos in self.positions.items():
            price = (mark_prices or {}).get(sym, mark_price) if mark_prices else mark_price
            total += pos.unrealized_pnl(price)
        return total

    def exposure_pct(
        self,
        mark_price: float,
        mark_prices: Optional[dict[str, float]] = None,
    ) -> float:
        """Total notional exposure / equity."""
        eq = self.equity(mark_price, mark_prices)
        if eq <= 0 or not self.positions:
            return 0.0
        total_notional = 0.0
        for sym, pos in self.positions.items():
            price = (mark_prices or {}).get(sym, mark_price) if mark_prices else mark_price
            total_notional += pos.notional(price)
        return total_notional / eq

    def account_state(self, mark_price: float, mark_prices: Optional[dict[str, float]] = None) -> AccountState:
        return AccountState(
            equity=self.equity(mark_price, mark_prices),
            free=self.cash,
            positions=tuple(self.positions.values()),
        )

    # -- persistence (live crash recovery) -------------------------------------
    @staticmethod
    def _serialize_position(p: Position) -> dict:
        return {
            "symbol": p.symbol, "side": p.side.value, "qty": p.qty, "avg_entry": p.avg_entry,
            "stop": p.stop, "target": p.target, "opened_ms": p.opened_ms,
            "highest_since_entry": p.highest_since_entry, "lowest_since_entry": p.lowest_since_entry,
            "bars_held": p.bars_held, "entry_fee": p.entry_fee, "entry_reason": p.entry_reason,
        }

    @staticmethod
    def _deserialize_position(pos: dict) -> Optional[Position]:
        try:
            return Position(
                symbol=str(pos["symbol"]), side=Side(pos["side"]),
                qty=float(pos["qty"]), avg_entry=float(pos["avg_entry"]),
                stop=pos.get("stop") if pos.get("stop") is not None else None,
                target=pos.get("target") if pos.get("target") is not None else None,
                opened_ms=int(pos.get("opened_ms", 0)),
                highest_since_entry=float(pos.get("highest_since_entry", 0.0)),
                lowest_since_entry=float(pos.get("lowest_since_entry", 0.0)),
                bars_held=int(pos.get("bars_held", 0)),
                entry_fee=float(pos.get("entry_fee", 0.0)),
                entry_reason=str(pos.get("entry_reason", "")),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Corrupted position state, skipping: %s", exc)
            return None

    def state_dict(self) -> dict:
        return {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "positions": [self._serialize_position(p) for p in self.positions.values()],
        }

    def load_state(self, d: dict) -> None:
        self.cash = float(d.get("cash", self.cash))
        self.realized_pnl = float(d.get("realized_pnl", 0.0))
        self.fees_paid = float(d.get("fees_paid", 0.0))
        self.positions.clear()

        # New format: list of position dicts under "positions"
        pos_list = d.get("positions")
        if isinstance(pos_list, list):
            for raw in pos_list:
                if not isinstance(raw, dict):
                    continue
                p = self._deserialize_position(raw)
                if p is not None:
                    self.positions[p.symbol] = p
            return

        # Legacy format: single position dict under "position"
        pos = d.get("position")
        if pos and isinstance(pos, dict):
            p = self._deserialize_position(pos)
            if p is not None:
                self.positions[p.symbol] = p

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
        existing = self.positions.get(fill.symbol)
        if existing is None:
            self.positions[fill.symbol] = Position(
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
            total_qty = existing.qty + fill.filled_qty
            existing.avg_entry = (existing.avg_entry * existing.qty + fill.avg_price * fill.filled_qty) / total_qty
            existing.qty = total_qty
            existing.entry_fee += fill.fee

    def _apply_sell(self, fill: Fill, reason: str) -> Optional[Trade]:
        p = self.positions.get(fill.symbol)
        if p is None:
            log.warning("Sell fill with no open position: %s", fill)
            return None
        qty_closed = min(fill.filled_qty, p.qty)
        proceeds = qty_closed * fill.avg_price - fill.fee
        self.cash += proceeds
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
            del self.positions[fill.symbol]
        else:
            p.qty = remaining
            p.entry_fee -= entry_fee_part
        return trade
