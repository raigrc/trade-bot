"""Entrypoint. Reads config.yaml's mode and dispatches.

    python -m tradebot.main                 # uses mode from config.yaml
    python -m tradebot.main --mode backtest
    python -m tradebot.main --mode paper --symbol ETH/USDT

LIVE mode (real money) refuses to start unless config has `confirm_live: true`
AND real Binance keys are present. Backtest -> paper gates should pass first.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config, load_secrets
from .enums import Mode


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Run the trading bot.")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=None)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit (smoke test)")
    args = ap.parse_args()

    if args.mode:
        cfg.mode = Mode(args.mode)
    if args.symbol:
        cfg.symbols = [args.symbol]
    if args.strategy:
        cfg.strategy.name = args.strategy

    if cfg.mode == Mode.BACKTEST:
        from .backtest import run_backtest

        res = run_backtest(cfg)
        print(f"\nStrategy={res.strategy}  Symbol={res.symbol}  TF={cfg.timeframe}")
        print(res.metrics.render())
        return 0

    # paper or live
    try:
        cfg.assert_live_allowed()
    except RuntimeError as exc:
        print(f"\n[BLOCKED] {exc}\n")
        return 3

    secrets = load_secrets(cfg.mode)
    paper_sim = cfg.mode == Mode.PAPER and cfg.paper_execution == "sim"
    if not paper_sim:
        key, secret = secrets.keys_for(cfg.mode)
        if not (key and secret):
            which = "BINANCE_API_KEY/SECRET" if cfg.mode == Mode.LIVE else "BINANCE_TESTNET_API_KEY/SECRET"
            print(f"\n[!] Missing {which} in .env.\n")
            return 2

    if paper_sim:
        print(f"\nPaper-sim: live Binance mainnet prices + simulated fills, virtual "
              f"{cfg.initial_equity:.0f} {cfg.quote_currency}. No keys, no risk. Ctrl-C to stop.\n")
    elif cfg.mode == Mode.LIVE:
        print("\n*** LIVE MODE — REAL MONEY. *** Risk per trade "
              f"{cfg.risk.risk_fraction_per_trade:.1%}, drawdown kill-switch "
              f"{cfg.risk.max_drawdown_pct:.0%}. Ctrl-C to stop.\n")

    from .live import LiveRunner

    runner = LiveRunner(cfg, secrets)
    runner.run_once() if args.once else runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
