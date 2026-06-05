"""M0 verification: connect to Binance and run the exchange self-test.

Places NO orders by default. Pass --deep to additionally place + cancel a tiny
far-from-market test order (testnet only).

    python -m scripts.check_connectivity
    python -m scripts.check_connectivity --deep
    python -m scripts.check_connectivity --mode paper --symbol ETH/USDT
"""

from __future__ import annotations

import argparse
import logging
import sys

from tradebot.config import load_config, load_secrets
from tradebot.enums import Mode
from tradebot.exchange import BinanceExchange


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Check Binance connectivity (no orders by default).")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=None)
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--deep", action="store_true", help="place & cancel a tiny test order (testnet)")
    args = ap.parse_args()

    config = load_config()
    if args.mode:
        config.mode = Mode(args.mode)
    # connectivity check should default to paper (testnet) unless explicitly live
    if config.mode == Mode.BACKTEST:
        config.mode = Mode.PAPER

    secrets = load_secrets()
    key, _ = secrets.keys_for(config.mode)
    if not key:
        which = "BINANCE_API_KEY" if config.mode == Mode.LIVE else "BINANCE_TESTNET_API_KEY"
        print(f"\n[!] No {which} in .env — add testnet keys from https://testnet.binance.vision\n")
        return 2

    print(f"Connecting to Binance ({config.mode.value}) ...")
    ex = BinanceExchange(config, secrets)
    try:
        result = ex.self_test(symbol=args.symbol, deep=args.deep)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] self-test error: {type(exc).__name__}: {exc}")
        print(
            "If this is an 'Invalid API-Key' on testnet, ccxt's sandbox host may be "
            "stale (#27266) — try setting testnet_url: https://demo-api.binance.com "
            "in config.yaml, or upgrade ccxt.\n"
        )
        return 1

    print("\n[OK] self-test passed:")
    for k, v in result.items():
        print(f"  {k:>14}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
