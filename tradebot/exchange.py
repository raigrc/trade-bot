"""ccxt wrapper around Binance (live + testnet).

Responsibilities:
- Build the right client for the mode (testnet vs live keys; sandbox URL).
- Retry transient network/rate-limit errors with backoff; never retry on
  business errors (InsufficientFunds / InvalidOrder).
- Surface exchange trading rules as ``MarketInfo`` (Binance filters).
- Round amounts/prices to exchange precision via ccxt helpers.
- A startup ``self_test`` that proves auth + connectivity before any loop runs.

This module does NOT decide anything about trading — it is plumbing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import ccxt
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import BotConfig, Secrets
from .enums import Mode
from .types import MarketInfo

log = logging.getLogger(__name__)

# Errors worth retrying (transient). Business errors are NOT here on purpose.
_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.DDoSProtection,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
)

_retry = retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
)


class ExchangeError(Exception):
    pass


class BinanceExchange:
    def __init__(self, config: BotConfig, secrets: Secrets, public_data_only: bool = False) -> None:
        self.config = config
        self.mode = config.mode
        self.public_data_only = public_data_only
        if public_data_only:
            # mainnet PUBLIC data only — no keys, no sandbox (for paper-sim forward testing)
            self._ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        else:
            api_key, api_secret = secrets.keys_for(config.mode)
            self._ex = ccxt.binance(
                {
                    "apiKey": api_key,
                    "secret": api_secret,
                    "enableRateLimit": True,  # without this you get banned fast
                    "options": {"defaultType": "spot", "adjustForTimeDifference": True},
                }
            )
            if config.mode == Mode.PAPER:
                self._ex.set_sandbox_mode(True)
                self._maybe_override_testnet_url()
        self._markets_loaded = False

    # -- setup -----------------------------------------------------------------

    def _maybe_override_testnet_url(self) -> None:
        """Defend against ccxt's sandbox URL drifting out of date (#27266).

        ``set_sandbox_mode`` points ``urls['api']`` at ccxt's hard-coded testnet
        host. If that host is stale, auth fails with "Invalid API-Key". We let
        the user pin the correct host via config.testnet_url.
        """
        url = (self.config.testnet_url or "").rstrip("/")
        if not url:
            return
        try:
            api = self._ex.urls.get("api")
            if isinstance(api, dict):
                # Preserve the versioned paths (e.g., /api/v3) but replace the host
                for key in ("public", "private", "v1"):
                    if key in api:
                        old_path = api[key].split("//", 1)[-1]  # get path after host
                        api[key] = url + "/" + old_path.split("/", 1)[-1]  # replace host only
            log.info("Testnet REST host overridden to %s", url)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not override testnet URL (%s); using ccxt default", exc)

    @_retry
    def load_markets(self, reload: bool = False) -> dict:
        markets = self._ex.load_markets(reload)
        self._markets_loaded = True
        return markets

    def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            self.load_markets()

    # -- read-only -------------------------------------------------------------

    @_retry
    def fetch_time(self) -> int:
        return int(self._ex.fetch_time())

    def check_time_drift(self, max_drift_ms: int = 2000) -> int:
        """Return |server-local| drift in ms; warn if large (Binance rejects
        requests whose timestamp is outside recvWindow)."""
        import time as _t  # local: clock.py owns wall-clock policy, this is a health check

        server = self.fetch_time()
        local = int(_t.time() * 1000)
        drift = abs(server - local)
        if drift > max_drift_ms:
            log.warning("Clock drift %d ms exceeds %d ms — sync your system clock.", drift, max_drift_ms)
        return drift

    @_retry
    def fetch_balance(self) -> dict:
        return self._ex.fetch_balance()

    def free_quote(self, quote: str = "USDT") -> float:
        bal = self.fetch_balance()
        return float(bal.get("free", {}).get(quote, 0.0) or 0.0)

    @_retry
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[int] = None,
        limit: int = 1000,
    ) -> list[list[float]]:
        """Raw OHLCV: [open_time_ms, open, high, low, close, volume].

        NOTE: the last row may be the still-forming candle — callers must drop
        unclosed bars (see data.drop_unclosed). Always pass an explicit limit.
        """
        return self._ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    @_retry
    def fetch_ticker(self, symbol: str) -> dict:
        return self._ex.fetch_ticker(symbol)

    def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        t = self.fetch_ticker(symbol)
        bid = float(t.get("bid") or t.get("close"))
        ask = float(t.get("ask") or t.get("close"))
        return bid, ask

    def market_info(self, symbol: str) -> MarketInfo:
        self._ensure_markets()
        m = self._ex.market(symbol)
        filters = {f.get("filterType"): f for f in m.get("info", {}).get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        price_f = filters.get("PRICE_FILTER", {})
        notional_f = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return MarketInfo(
            symbol=symbol,
            is_spot=bool(m.get("spot", False)),
            base=m.get("base", ""),
            quote=m.get("quote", ""),
            tick_size=float(price_f.get("tickSize", 0) or 0),
            step_size=float(lot.get("stepSize", 0) or 0),
            min_qty=float(lot.get("minQty", 0) or 0),
            max_qty=float(lot.get("maxQty", 0) or 0),
            min_notional=float(notional_f.get("minNotional", notional_f.get("notional", 0)) or 0),
        )

    # -- precision helpers (delegate to ccxt; handles Binance's precision mode) -

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        self._ensure_markets()
        return float(self._ex.amount_to_precision(symbol, amount))

    def price_to_precision(self, symbol: str, price: float) -> float:
        self._ensure_markets()
        return float(self._ex.price_to_precision(symbol, price))

    # -- orders (used by CcxtExecution) ----------------------------------------

    @_retry
    def create_order(
        self,
        symbol: str,
        type_: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[dict] = None,
    ) -> dict:
        return self._ex.create_order(symbol, type_, side, amount, price, params or {})

    @_retry
    def cancel_order(self, order_id: str, symbol: str) -> dict:
        return self._ex.cancel_order(order_id, symbol)

    @_retry
    def fetch_order(self, order_id: str, symbol: str) -> dict:
        return self._ex.fetch_order(order_id, symbol)

    @_retry
    def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        return self._ex.fetch_open_orders(symbol)

    @_retry
    def fetch_my_trades(self, symbol: str, since: Optional[int] = None) -> list[dict]:
        return self._ex.fetch_my_trades(symbol, since=since)

    @property
    def raw(self) -> Any:
        """Escape hatch to the underlying ccxt client (advanced use)."""
        return self._ex

    # -- self-test -------------------------------------------------------------

    def self_test(self, symbol: str = "BTC/USDT", deep: bool = False) -> dict:
        """Prove auth + connectivity before starting any trading loop.

        Light test: load markets, check clock drift, fetch balance + recent
        candles. Deep test additionally places a tiny far-from-market limit
        order and cancels it (testnet only by default).
        """
        result: dict[str, Any] = {"mode": self.mode.value}
        self.load_markets()
        result["markets"] = len(self._ex.markets)
        result["time_drift_ms"] = self.check_time_drift()

        if not self.public_data_only:
            bal = self.fetch_balance()
            result["quote_free"] = float(bal.get("free", {}).get(self.config.quote_currency, 0.0) or 0.0)

        ohlcv = self.fetch_ohlcv(symbol, self.config.timeframe, limit=5)
        result["ohlcv_bars"] = len(ohlcv)
        result["last_close"] = float(ohlcv[-1][4]) if ohlcv else None

        if deep:
            result["deep"] = self._deep_order_test(symbol)
        return result

    def _deep_order_test(self, symbol: str) -> dict:
        """Place a tiny limit buy ~30% below market (won't fill) and cancel it."""
        if self.mode == Mode.LIVE:
            raise ExchangeError("Refusing to run deep order self-test in LIVE mode.")
        mi = self.market_info(symbol)
        bid, ask = self.best_bid_ask(symbol)
        price = self.price_to_precision(symbol, bid * 0.7)
        # smallest compliant size: enough to clear min_notional at this low price
        raw_qty = max(mi.min_qty, (mi.min_notional / price) if mi.min_notional else mi.min_qty)
        qty = self.amount_to_precision(symbol, raw_qty * 1.05)
        order = self.create_order(symbol, "limit", "buy", qty, price)
        oid = order.get("id")
        canceled = self.cancel_order(oid, symbol) if oid else {}
        return {"placed_id": oid, "qty": qty, "price": price, "canceled": bool(canceled)}
