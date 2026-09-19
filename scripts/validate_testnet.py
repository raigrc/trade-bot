"""Testnet validation: prove Binance testnet auth, connectivity, and order plumbing.

Runs five sequential checks, each printed with PASS/FAIL:

  1. Auth + connectivity  — self_test() (markets, clock, balance, OHLCV)
  2. Clock drift          — warn if |server-local| > 2000 ms
  3. Balance check        — verify virtual USDT balance > 0
  4. Deep order test      — place tiny limit buy ~30% below market, cancel it
  5. Market info          — fetch BTC/USDT filters (lot/tick/min-notional)

Usage:

    python -m scripts.validate_testnet
    python -m scripts.validate_testnet --symbol ETH/USDT
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from tradebot.config import load_config, load_secrets
from tradebot.enums import Mode
from tradebot.exchange import BinanceExchange, ExchangeError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CRESET = "\033[0m"
_CGREEN = "\033[92m"
_CRED = "\033[91m"
_CYELLOW = "\033[93m"

_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    """Wrap *text* in ANSI color codes when stdout is a TTY."""
    return f"{code}{text}{_CRESET}" if _COLOR else text


def _pass(msg: str) -> None:
    print(f"  {_c('PASS', _CGREEN)}  {msg}")


def _fail(msg: str) -> None:
    print(f"  {_c('FAIL', _CRED)}  {msg}")


def _warn(msg: str) -> None:
    print(f"  {_c('WARN', _CYELLOW)}  {msg}")


# ---------------------------------------------------------------------------
# Validation checks
# ---------------------------------------------------------------------------

_CLOCK_DRIFT_WARN_MS = 2000


def _check_auth_and_connectivity(
    ex: BinanceExchange, symbol: str, quote: str
) -> tuple[bool, dict[str, Any]]:
    """Load markets, check clock, fetch balance, fetch OHLCV bars."""
    try:
        result = ex.self_test(symbol=symbol, deep=False)
        return True, result
    except Exception as exc:  # noqa: BLE001
        return False, {"error": exc}


def _check_clock_drift(drift_ms: int) -> tuple[bool, str]:
    """Return (pass, detail). Warn but still pass if drift > threshold."""
    if drift_ms > _CLOCK_DRIFT_WARN_MS:
        return True, f"{drift_ms} ms — exceeds {_CLOCK_DRIFT_WARN_MS} ms threshold; sync your system clock"
    return True, f"{drift_ms} ms"


def _check_balance(free_quote: float, quote: str) -> tuple[bool, str]:
    """Verify the testnet account has virtual quote currency."""
    if free_quote <= 0:
        return False, f"{free_quote:.2f} {quote} — testnet balance is zero; reset at https://testnet.binance.vision"
    return True, f"{free_quote:.2f} {quote}"


def _check_deep_order(ex: BinanceExchange, symbol: str) -> tuple[bool, str]:
    """Place a tiny far-from-market limit buy and cancel it (testnet only)."""
    try:
        result = ex._deep_order_test(symbol)
        oid = result.get("placed_id")
        canceled = result.get("canceled")
        price = result.get("price")
        qty = result.get("qty")
        if not oid:
            return False, "order placement returned no id"
        if not canceled:
            return False, f"cancel failed for order {oid}"
        return True, f"placed {qty} @ {price} (id={oid}), canceled"
    except ExchangeError as exc:
        return False, f"exchange error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _check_market_info(ex: BinanceExchange, symbol: str) -> tuple[bool, str]:
    """Fetch and display exchange trading rules for *symbol*."""
    try:
        mi = ex.market_info(symbol)
        parts = [
            f"lot_size(step={mi.step_size}, min={mi.min_qty}, max={mi.max_qty})",
            f"tick_size={mi.tick_size}",
            f"min_notional={mi.min_notional}",
        ]
        return True, f"{symbol} — {', '.join(parts)}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import argparse

    ap = argparse.ArgumentParser(description="Validate Binance testnet connectivity and order plumbing.")
    ap.add_argument("--symbol", default="BTC/USDT", help="Trading pair to test (default: BTC/USDT)")
    args = ap.parse_args()

    symbol: str = args.symbol
    quote: str = "USDT"

    print(f"\n{'='*60}")
    print(f"  Testnet Validation — {symbol}")
    print(f"{'='*60}\n")

    # -- Load config + secrets ------------------------------------------------

    config = load_config()
    config.mode = Mode.PAPER  # force testnet
    secrets = load_secrets(config.mode)

    key, _ = secrets.keys_for(config.mode)
    if not key:
        _fail("No BINANCE_TESTNET_API_KEY in .env")
        print("\n  Generate testnet keys at https://testnet.binance.vision\n")
        return 1

    _pass("Config loaded (mode=paper, testnet keys present)")

    # -- Build exchange -------------------------------------------------------

    ex = BinanceExchange(config, secrets, public_data_only=False)

    # -- Run checks -----------------------------------------------------------

    passed = 0
    failed = 0
    checks: list[str] = []

    # 1. Auth + connectivity
    print("1. Auth + connectivity")
    ok, info = _check_auth_and_connectivity(ex, symbol, quote)
    if ok:
        _pass(f"markets={info.get('markets')}, ohlcv_bars={info.get('ohlcv_bars')}, "
              f"last_close={info.get('last_close')}")
        passed += 1
        checks.append("auth_connectivity:PASS")
    else:
        err = info.get("error")
        _fail(f"{type(err).__name__}: {err}")
        failed += 1
        checks.append("auth_connectivity:FAIL")
        # If basic connectivity fails, remaining checks are pointless.
        print(f"\n{'='*60}")
        print(f"  Result: {_c(f'{passed} passed, {failed} failed', _CRED)}")
        print(f"{'='*60}\n")
        return 1

    # 2. Clock drift
    print("2. Clock drift")
    drift_ms = info.get("time_drift_ms", 0)
    ok, detail = _check_clock_drift(drift_ms)
    if drift_ms > _CLOCK_DRIFT_WARN_MS:
        _warn(detail)
        # Still counts as pass — just a warning.
        passed += 1
        checks.append("clock_drift:WARN")
    else:
        _pass(detail)
        passed += 1
        checks.append("clock_drift:PASS")

    # 3. Balance check
    print("3. Balance check")
    free_quote = info.get("quote_free", 0.0)
    ok, detail = _check_balance(free_quote, quote)
    if ok:
        _pass(detail)
        passed += 1
        checks.append("balance:PASS")
    else:
        _fail(detail)
        failed += 1
        checks.append("balance:FAIL")

    # 4. Deep order test
    print("4. Deep order test")
    ok, detail = _check_deep_order(ex, symbol)
    if ok:
        _pass(detail)
        passed += 1
        checks.append("deep_order:PASS")
    else:
        _fail(detail)
        failed += 1
        checks.append("deep_order:FAIL")

    # 5. Market info
    print("5. Market info")
    ok, detail = _check_market_info(ex, symbol)
    if ok:
        _pass(detail)
        passed += 1
        checks.append("market_info:PASS")
    else:
        _fail(detail)
        failed += 1
        checks.append("market_info:FAIL")

    # -- Summary --------------------------------------------------------------

    print(f"\n{'='*60}")
    color = _CGREEN if failed == 0 else _CRED
    print(f"  Result: {_c(f'{passed} passed, {failed} failed', color)}")
    print(f"  Checks: {', '.join(checks)}")
    print(f"{'='*60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
