"""Download & cache historical OHLCV for backtesting (Binance mainnet, public).

    python -m scripts.fetch_data
    python -m scripts.fetch_data --symbols BTC/USDT ETH/USDT --start 2020-01-01
    python -m scripts.fetch_data --timeframes 4h 1d --refresh
"""

from __future__ import annotations

import argparse
import logging
import sys

from tradebot.config import load_config
from tradebot.data import HistoricalDataFetcher, iso_to_ms, validate_gaps


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Fetch & cache historical OHLCV.")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--timeframes", nargs="*", default=None)
    ap.add_argument("--start", default=None, help="ISO date, e.g. 2020-01-01")
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    args = ap.parse_args()

    symbols = args.symbols or cfg.symbols
    timeframes = args.timeframes or sorted({cfg.timeframe, cfg.htf_timeframe})
    start = args.start or cfg.backtest_start or "2020-01-01"
    since = iso_to_ms(start)

    fetcher = HistoricalDataFetcher(cfg.data_dir)
    print(f"Fetching {symbols} @ {timeframes} since {start}\n")
    for symbol in symbols:
        for tf in timeframes:
            df = fetcher.load_or_fetch(symbol, tf, since, refresh=args.refresh)
            gaps = validate_gaps(df, tf)
            span = ""
            if len(df):
                from datetime import datetime, timezone

                a = datetime.fromtimestamp(df["open_time"].iloc[0] / 1000, tz=timezone.utc).date()
                b = datetime.fromtimestamp(df["open_time"].iloc[-1] / 1000, tz=timezone.utc).date()
                span = f"{a} -> {b}"
            print(f"  {symbol:>10} {tf:>3}: {len(df):>6} bars  {span}  gaps={len(gaps)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
