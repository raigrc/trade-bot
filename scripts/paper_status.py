"""One-page status summary for paper / live mode.

Reads the SQLite state + journal and prints current equity, drawdown,
open position, trade stats, kill-switch status, and all-time risk metrics.

    python -m scripts.paper_status
    python -m scripts.paper_status --mode paper --symbol BTC/USDT
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from tradebot.config import load_config
from tradebot.enums import Mode
from tradebot.journal import TradeJournal
from tradebot.metrics import compute_metrics
from tradebot.persistence import StateStore
from tradebot.reporting import iso_week_bounds


def _fmt_ms(ms: int) -> str:
    """Format epoch ms as a UTC datetime string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _status_line(label: str, value: str, width: int = 22) -> str:
    return f"  {label:<{width}}: {value}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Show paper/live bot status.")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=None)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg.mode = Mode(args.mode)
    symbol = args.symbol or cfg.symbols[0]

    db = Path(cfg.state_dir) / f"{symbol.replace('/', '_')}_{cfg.mode.value}.sqlite"
    if not db.exists():
        print(f"\n  No state file found at {db}")
        print("  The bot hasn't run yet in this mode. Start it with:\n")
        print(f"    python -m tradebot.main --mode {cfg.mode.value} --symbol {symbol}\n")
        return 0

    # --- open both views on the same DB ------------------------------------
    store = StateStore(db)
    journal = TradeJournal(db)
    try:
        _print_status(cfg, symbol, store, journal)
    finally:
        store.close()
        journal.close()
    return 0


def _print_status(cfg, symbol, store: StateStore, journal: TradeJournal) -> None:
    risk_data = store.get("risk") or {}
    portfolio_data = store.get("portfolio") or {}
    meta_data = store.get("meta") or {}

    # --- equity & drawdown --------------------------------------------------
    current_equity = journal.latest_equity()
    if current_equity is None:
        current_equity = cfg.initial_equity

    peak_equity = float(risk_data.get("peak_equity", 0.0))
    if peak_equity <= 0:
        peak_equity = current_equity

    drawdown = ((peak_equity - current_equity) / peak_equity) if peak_equity > 0 else 0.0
    kill_pct = cfg.risk.max_drawdown_pct
    dd_pct_of_kill = (drawdown / kill_pct) if kill_pct > 0 else 0.0

    # --- open position ------------------------------------------------------
    pos = portfolio_data.get("position")
    cash = float(portfolio_data.get("cash", cfg.initial_equity))

    # --- trades -------------------------------------------------------------
    all_trades = journal.all()
    n_trades = len(all_trades)

    week_start_ms, week_end_ms = iso_week_bounds(
        datetime.now(tz=timezone.utc).timestamp() * 1000
    )
    week_trades = journal.trades_between(week_start_ms, week_end_ms)
    n_week = len(week_trades)

    wins = [t for t in all_trades if t.pnl > 0]
    win_rate = len(wins) / n_trades if n_trades else 0.0

    # --- kill-switch & risk state -------------------------------------------
    kill_engaged = bool(risk_data.get("kill_switch_engaged", False))
    kill_reason = risk_data.get("kill_switch_reason", "")
    consecutive_losses = int(risk_data.get("consecutive_losses", 0))

    # --- last bar processed -------------------------------------------------
    last_ms = meta_data.get("last_processed_ms")
    last_bar_str = _fmt_ms(last_ms) if last_ms else "n/a"

    # --- strategy label -----------------------------------------------------
    strategy_label = f"{cfg.strategy.name} ({cfg.timeframe})"

    # --- all-time metrics ---------------------------------------------------
    equity_curve = journal.equity_between(0, datetime.now(tz=timezone.utc).timestamp() * 1000)
    sharpe = sortino = calmar = max_dd = 0.0
    if len(equity_curve) >= 2:
        m = compute_metrics(
            equity_curve, all_trades, cfg.timeframe,
            cfg.initial_equity, total_bars=len(equity_curve),
        )
        sharpe, sortino, calmar = m.sharpe, m.sortino, m.calmar
        max_dd = abs(m.max_drawdown_pct)  # clamp -0.0 from compute_metrics

    # --- mode label ---------------------------------------------------------
    mode_label = cfg.mode.value
    if cfg.mode == Mode.PAPER:
        mode_label += f" ({cfg.paper_execution})"

    # --- print --------------------------------------------------------------
    q = cfg.quote_currency
    print()
    print("=== TradeBot Paper Status ===")
    print(_status_line("Mode", mode_label))
    print(_status_line("Symbol", symbol))
    print(_status_line("Strategy", strategy_label))
    print()
    print(_status_line("Current equity", f"{current_equity:.2f} {q}"))
    print(_status_line("Peak equity", f"{peak_equity:.2f} {q}"))
    print(_status_line("Drawdown", f"{drawdown:.2%} ({dd_pct_of_kill:.0%} of {kill_pct:.0%} kill-switch)"))
    print()

    # open position
    if pos is not None:
        pos_str = (
            f"{pos.side.value.upper()} {pos.qty:.6f} {symbol.split('/')[0]}"
            f" @ {pos.avg_entry:.2f}  stop={pos.stop or 'n/a'}"
        )
    else:
        pos_str = "none"
    print(_status_line("Open position", pos_str))
    print(_status_line("Cash", f"{cash:.2f} {q}"))
    print()
    print(_status_line("Total trades", f"{n_trades}"))
    print(_status_line("Trades this week", f"{n_week}"))
    print(_status_line("Win rate", f"{win_rate:.1%}" if n_trades else "N/A"))
    print()

    # kill-switch
    kill_str = f"ENGAGED — {kill_reason}" if kill_engaged else "clear"
    print(_status_line("Kill-switch", kill_str))
    print(_status_line("Consecutive losses", f"{consecutive_losses}"))
    print(_status_line("Last bar processed", last_bar_str))
    print()
    print(_status_line("All-time Sharpe", f"{sharpe:.2f}"))
    print(_status_line("All-time Sortino", f"{sortino:.2f}"))
    print(_status_line("All-time Calmar", f"{calmar:.2f}"))
    print(_status_line("Max drawdown", f"{max_dd:.2%}"))
    print()


if __name__ == "__main__":
    sys.exit(main())
