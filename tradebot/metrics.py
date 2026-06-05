"""Backtest performance metrics — preservation-first.

Tier 1 (gate on these): max drawdown, Calmar, Sortino, longest losing streak.
Tier 2: expectancy (must be +after costs), profit factor, Sharpe.
Tier 3 (never optimise for): win rate, exposure, trade count (validity only).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .types import Trade, timeframe_ms

_MS_PER_YEAR = 365.25 * 24 * 3600 * 1000


@dataclass
class Metrics:
    initial_equity: float
    final_equity: float
    total_return_pct: float
    cagr: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    calmar: float
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_quote: float
    expectancy_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    longest_losing_streak: int
    exposure_pct: float
    best_trade_pct: float
    worst_trade_pct: float

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        d = self.as_dict()
        lines = ["== Backtest metrics =="]
        order = [
            ("Initial equity", "initial_equity", "{:.2f}"),
            ("Final equity", "final_equity", "{:.2f}"),
            ("Total return", "total_return_pct", "{:.2%}"),
            ("CAGR", "cagr", "{:.2%}"),
            ("Max drawdown", "max_drawdown_pct", "{:.2%}"),
            ("Sharpe", "sharpe", "{:.2f}"),
            ("Sortino", "sortino", "{:.2f}"),
            ("Calmar", "calmar", "{:.2f}"),
            ("Trades", "n_trades", "{:d}"),
            ("Win rate", "win_rate", "{:.1%}"),
            ("Profit factor", "profit_factor", "{:.2f}"),
            ("Expectancy", "expectancy_pct", "{:.3%}"),
            ("Avg win", "avg_win_pct", "{:.2%}"),
            ("Avg loss", "avg_loss_pct", "{:.2%}"),
            ("Longest losing streak", "longest_losing_streak", "{:d}"),
            ("Exposure", "exposure_pct", "{:.1%}"),
            ("Best / worst trade", None, None),
        ]
        for label, key, fmt in order:
            if key is None:
                lines.append(f"  {label:<24}: {self.best_trade_pct:.2%} / {self.worst_trade_pct:.2%}")
            else:
                lines.append(f"  {label:<24}: {fmt.format(d[key])}")
        return "\n".join(lines)


def _longest_losing_streak(trades: list[Trade]) -> int:
    streak = best = 0
    for t in trades:
        if t.pnl < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def compute_metrics(
    equity_curve: list[tuple[int, float]],
    trades: list[Trade],
    timeframe: str,
    initial_equity: float,
    total_bars: int | None = None,
) -> Metrics:
    if not equity_curve:
        equity_curve = [(0, initial_equity)]
    ts = np.array([c[0] for c in equity_curve], dtype="float64")
    eq = pd.Series([c[1] for c in equity_curve], dtype="float64")
    final_equity = float(eq.iloc[-1])
    total_return = final_equity / initial_equity - 1.0

    # annualisation from the bar cadence
    bars_per_year = _MS_PER_YEAR / timeframe_ms(timeframe)
    span_years = max((ts[-1] - ts[0]) / _MS_PER_YEAR, 1e-9) if len(ts) > 1 else 1e-9
    cagr = (final_equity / initial_equity) ** (1 / span_years) - 1 if final_equity > 0 else -1.0

    rets = eq.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * math.sqrt(bars_per_year))
    else:
        sharpe = 0.0
    downside = rets[rets < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = float(rets.mean() / downside.std() * math.sqrt(bars_per_year))
    else:
        sortino = 0.0

    cummax = eq.cummax()
    drawdown = (eq - cummax) / cummax
    max_dd = float(-drawdown.min()) if len(drawdown) else 0.0
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    win_rate = len(wins) / n if n else 0.0
    expectancy_q = (sum(t.pnl for t in trades) / n) if n else 0.0
    expectancy_pct = (sum(t.return_pct for t in trades) / n) if n else 0.0
    avg_win_pct = (sum(t.return_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss_pct = (sum(t.return_pct for t in losses) / len(losses)) if losses else 0.0
    bars_in_pos = sum(t.bars_held for t in trades)
    exposure = (bars_in_pos / total_bars) if total_bars else 0.0
    best = max((t.return_pct for t in trades), default=0.0)
    worst = min((t.return_pct for t in trades), default=0.0)

    return Metrics(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return_pct=total_return,
        cagr=cagr,
        max_drawdown_pct=max_dd,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        n_trades=n,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_quote=expectancy_q,
        expectancy_pct=expectancy_pct,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        longest_losing_streak=_longest_losing_streak(trades),
        exposure_pct=exposure,
        best_trade_pct=best,
        worst_trade_pct=worst,
    )
