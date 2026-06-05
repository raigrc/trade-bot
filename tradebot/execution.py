"""Execution engines.

The engine drives bars; execution returns ``FillEvent``s which the engine
applies to the portfolio. Two implementations share one interface so
``Engine.step`` is identical across modes:

- ``SimulatedExecution`` (backtest): a signal on bar N's close fills at bar
  N+1's OPEN (structurally enforced — you cannot fill at the signal bar's
  close). Commission + slippage are always applied. Resting protective stop /
  target are evaluated against each bar's low/high; when both could trigger in
  one bar, the STOP is assumed to hit first (pessimistic, capital-preservation).
- ``CcxtExecution`` (paper/live, M4): submits real orders + native OCO; fills
  come back via submit (entry) and on_new_bar (protective polling).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from .config import RiskConfig
from .enums import OrderStatus, OrderType, Side
from .types import Bar, Fill, MarketInfo, Order, Position

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FillEvent:
    fill: Fill
    reason: str
    is_entry: bool


class ExecutionEngine(Protocol):
    def submit_entry(self, order: Order, stop: float, target: Optional[float], bar: Bar, reason: str) -> list[FillEvent]: ...
    def submit_exit(self, position: Position, bar: Bar, reason: str) -> list[FillEvent]: ...
    def on_new_bar(self, bar: Bar) -> list[FillEvent]: ...
    def update_protective_stop(self, new_stop: float) -> None: ...
    def has_pending(self) -> bool: ...


class SimulatedExecution:
    """Backtest fills with the N->N+1 rule, commission, slippage, resting stops."""

    def __init__(self, risk: RiskConfig, market: Optional[MarketInfo] = None) -> None:
        self.risk = risk
        self.market = market
        self._pending_entry: Optional[tuple[Order, float, Optional[float], str]] = None
        self._pending_exit: Optional[str] = None
        self._open_qty: float = 0.0
        self._stop: Optional[float] = None
        self._target: Optional[float] = None

    # -- engine-facing API -----------------------------------------------------
    def submit_entry(self, order: Order, stop: float, target: Optional[float], bar: Bar, reason: str) -> list[FillEvent]:
        # deferred: fills at the NEXT bar's open
        self._pending_entry = (order, stop, target, reason)
        return []

    def submit_exit(self, position: Position, bar: Bar, reason: str) -> list[FillEvent]:
        self._pending_exit = reason
        return []

    def update_protective_stop(self, new_stop: float) -> None:
        # trailing stop only ratchets in the favourable direction (long)
        if self._stop is None or new_stop > self._stop:
            self._stop = new_stop

    def has_pending(self) -> bool:
        return self._pending_entry is not None or self._pending_exit is not None

    @property
    def protective_stop(self) -> Optional[float]:
        return self._stop

    @property
    def protective_target(self) -> Optional[float]:
        return self._target

    def state_dict(self) -> dict:
        # CRITICAL: persist the pending entry/exit too. In the scheduled --once
        # deployment each bar runs in a separate process, so the next-bar-open
        # fill only works if the pending order survives between processes.
        pe = None
        if self._pending_entry is not None:
            o, stop, target, reason = self._pending_entry
            pe = {
                "order": {"symbol": o.symbol, "side": o.side.value, "qty": o.qty,
                          "order_type": o.order_type.value, "limit_price": o.limit_price,
                          "stop_price": o.stop_price, "reduce_only": o.reduce_only,
                          "client_order_id": o.client_order_id},
                "stop": stop, "target": target, "reason": reason,
            }
        return {"open_qty": self._open_qty, "stop": self._stop, "target": self._target,
                "stop_order_id": None, "pending_entry": pe, "pending_exit": self._pending_exit}

    def load_state(self, d: dict) -> None:
        self._open_qty = d.get("open_qty", 0.0)
        self._stop = d.get("stop")
        self._target = d.get("target")
        self._pending_exit = d.get("pending_exit")
        pe = d.get("pending_entry")
        if pe:
            o = pe["order"]
            order = Order(symbol=o["symbol"], side=Side(o["side"]), qty=o["qty"],
                          order_type=OrderType(o["order_type"]), limit_price=o.get("limit_price"),
                          stop_price=o.get("stop_price"), reduce_only=o.get("reduce_only", False),
                          client_order_id=o.get("client_order_id", ""))
            self._pending_entry = (order, pe["stop"], pe["target"], pe["reason"])
        else:
            self._pending_entry = None

    def adopt(self, open_qty: float, stop: Optional[float], target: Optional[float], stop_order_id=None) -> None:
        self._open_qty = open_qty
        self._stop = stop
        self._target = target

    def on_new_bar(self, bar: Bar) -> list[FillEvent]:
        events: list[FillEvent] = []

        # 1. pending market exit (discretionary / signal) at this bar's OPEN
        if self._pending_exit is not None and self._open_qty > 0:
            events.append(self._exit_fill(bar, bar.open, self.risk.slippage_pct, self._pending_exit))
            self._pending_exit = None
            self._flatten()

        # 2. pending market entry at this bar's OPEN
        elif self._pending_entry is not None and self._open_qty == 0:
            order, stop, target, reason = self._pending_entry
            price = self._round_price(bar.open * (1 + self.risk.slippage_pct))
            fee = order.qty * price * self.risk.taker_fee_pct
            fill = Fill(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=Side.BUY,
                filled_qty=order.qty,
                avg_price=price,
                fee=fee,
                status=OrderStatus.FILLED,
                ts_ms=bar.open_time_ms,
            )
            events.append(FillEvent(fill, reason, is_entry=True))
            self._pending_entry = None
            self._open_qty = order.qty
            self._stop, self._target = stop, target

        # 3. resting protective stop / target against THIS bar's range (intrabar)
        if self._open_qty > 0 and self._stop is not None:
            hit_stop = bar.low <= self._stop
            hit_target = self._target is not None and bar.high >= self._target
            if hit_stop:  # pessimistic: stop assumed to trigger before target
                exit_price = self._round_price(self._stop * (1 - self.risk.stop_slippage_pct))
                events.append(self._exit_fill(bar, exit_price, 0.0, "stop hit", at_price=True))
                self._flatten()
            elif hit_target:
                events.append(self._exit_fill(bar, self._target, 0.0, "target hit", at_price=True, maker=True))
                self._flatten()

        return events

    # -- helpers ---------------------------------------------------------------
    def _exit_fill(self, bar: Bar, ref_price: float, slippage: float, reason: str,
                   at_price: bool = False, maker: bool = False) -> FillEvent:
        price = ref_price if at_price else self._round_price(ref_price * (1 - slippage))
        fee_pct = self.risk.maker_fee_pct if maker else self.risk.taker_fee_pct
        fee = self._open_qty * price * fee_pct
        fill = Fill(
            client_order_id="",
            symbol=bar.symbol,
            side=Side.SELL,
            filled_qty=self._open_qty,
            avg_price=price,
            fee=fee,
            status=OrderStatus.FILLED,
            ts_ms=bar.open_time_ms,
        )
        return FillEvent(fill, reason, is_entry=False)

    def _flatten(self) -> None:
        self._open_qty = 0.0
        self._stop = None
        self._target = None

    def _round_price(self, price: float) -> float:
        if self.market and self.market.tick_size:
            ts = self.market.tick_size
            return round(round(price / ts) * ts, 12)
        return price


class CcxtExecution:
    """Live/paper execution via ccxt with a NATIVE stop (survives a bot crash)
    plus a bot-monitored backstop for the trailing stop and take-profit.

    Hybrid (per the risk design): the native STOP_LOSS_LIMIT is the durable last
    line of defense; the bot-monitored checks in ``on_new_bar`` cover the target
    and act if the native order is somehow missing. Entries fill synchronously,
    so submit_entry returns the fill immediately.

    NOTE: exchange order params (stop/OCO) MUST be validated on the testnet
    before live use — this is exactly what the paper stage is for. Native-stop
    placement failures are logged loudly and fall back to bot-monitored.
    """

    def __init__(self, exchange, market: MarketInfo, risk: RiskConfig) -> None:
        self.ex = exchange
        self.market = market
        self.risk = risk
        self._open_qty = 0.0
        self._stop: Optional[float] = None
        self._target: Optional[float] = None
        self._stop_order_id: Optional[str] = None

    def has_pending(self) -> bool:
        return False  # ccxt fills are synchronous

    @property
    def protective_stop(self) -> Optional[float]:
        return self._stop

    @property
    def protective_target(self) -> Optional[float]:
        return self._target

    # -- entry / exit ----------------------------------------------------------
    def submit_entry(self, order: Order, stop: float, target: Optional[float], bar: Bar, reason: str) -> list[FillEvent]:
        amount = self.ex.amount_to_precision(order.symbol, order.qty)
        resp = self.ex.create_order(
            order.symbol, "market", "buy", amount, None, {"newClientOrderId": order.client_order_id}
        )
        fill = self._fill_from(resp, order.symbol, Side.BUY, bar)
        self._open_qty = fill.filled_qty
        self._stop, self._target = stop, target
        self._place_native_stop(order.symbol, stop)
        return [FillEvent(fill, reason, is_entry=True)]

    def submit_exit(self, position: Position, bar: Bar, reason: str) -> list[FillEvent]:
        return self._market_exit(bar, reason)

    def update_protective_stop(self, new_stop: float) -> None:
        if self._stop is not None and new_stop <= self._stop:
            return
        self._stop = new_stop
        if self._open_qty <= 0:
            return
        self._cancel_native()
        self._place_native_stop(self.market.symbol, new_stop)

    def on_new_bar(self, bar: Bar) -> list[FillEvent]:
        if self._open_qty <= 0:
            return []
        # 1. native stop filled?
        if self._stop_order_id:
            try:
                o = self.ex.fetch_order(self._stop_order_id, bar.symbol)
                if o.get("status") in ("closed", "filled") and float(o.get("filled") or 0) > 0:
                    fill = self._fill_from(o, bar.symbol, Side.SELL, bar)
                    self._flatten()
                    return [FillEvent(fill, "stop hit (native)", is_entry=False)]
            except Exception as exc:  # noqa: BLE001
                log.warning("fetch_order(stop) failed: %s", exc)
        # 2. bot-monitored stop backstop
        if self._stop is not None and bar.low <= self._stop:
            return self._market_exit(bar, "stop hit (monitored)")
        # 3. take-profit (bot-monitored)
        if self._target is not None and bar.high >= self._target:
            return self._market_exit(bar, "target hit")
        return []

    # -- reconciliation (called by live.py on startup) -------------------------
    def adopt(self, qty: float, stop: Optional[float], target: Optional[float], stop_order_id: Optional[str]) -> None:
        self._open_qty = qty
        self._stop = stop
        self._target = target
        self._stop_order_id = stop_order_id

    def state_dict(self) -> dict:
        return {
            "open_qty": self._open_qty,
            "stop": self._stop,
            "target": self._target,
            "stop_order_id": self._stop_order_id,
        }

    def load_state(self, d: dict) -> None:
        self.adopt(d.get("open_qty", 0.0), d.get("stop"), d.get("target"), d.get("stop_order_id"))

    # -- helpers ---------------------------------------------------------------
    def _place_native_stop(self, symbol: str, stop: float) -> None:
        if self._open_qty <= 0:
            return
        try:
            limit = self.ex.price_to_precision(symbol, stop * (1 - self.risk.stop_limit_offset_pct))
            trigger = self.ex.price_to_precision(symbol, stop)
            qty = self.ex.amount_to_precision(symbol, self._open_qty)
            resp = self.ex.create_order(symbol, "STOP_LOSS_LIMIT", "sell", qty, limit, {"stopPrice": trigger})
            self._stop_order_id = resp.get("id")
            log.info("Native stop placed @ %s (limit %s) id=%s", trigger, limit, self._stop_order_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "NATIVE STOP PLACEMENT FAILED (%s). Relying on bot-monitored stop — position is "
                "NAKED if the bot process dies before the next bar. Validate order params on testnet.",
                exc,
            )
            self._stop_order_id = None

    def _cancel_native(self) -> None:
        if not self._stop_order_id:
            return
        try:
            self.ex.cancel_order(self._stop_order_id, self.market.symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel native stop failed: %s", exc)
        self._stop_order_id = None

    def _market_exit(self, bar: Bar, reason: str) -> list[FillEvent]:
        self._cancel_native()
        qty = self.ex.amount_to_precision(bar.symbol, self._open_qty)
        resp = self.ex.create_order(bar.symbol, "market", "sell", qty, None, {})
        fill = self._fill_from(resp, bar.symbol, Side.SELL, bar)
        self._flatten()
        return [FillEvent(fill, reason, is_entry=False)]

    def _flatten(self) -> None:
        self._open_qty = 0.0
        self._stop = None
        self._target = None
        self._stop_order_id = None

    def _fill_from(self, resp: dict, symbol: str, side: Side, bar: Bar) -> Fill:
        filled = float(resp.get("filled") or resp.get("amount") or 0.0)
        avg = float(resp.get("average") or resp.get("price") or bar.close)
        fee_info = resp.get("fee") or {}
        if fee_info and fee_info.get("cost") is not None:
            fee = float(fee_info["cost"])
        else:
            fee = filled * avg * self.risk.taker_fee_pct
        return Fill(
            client_order_id=resp.get("clientOrderId", "") or "",
            symbol=symbol,
            side=side,
            filled_qty=filled,
            avg_price=avg,
            fee=fee,
            status=OrderStatus.FILLED,
            ts_ms=int(resp.get("timestamp") or bar.close_time_ms),
            raw=resp,
        )
