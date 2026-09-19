"""ASCII equity curve + drawdown chart for the terminal.

Reads the full equity time series from the SQLite journal and renders a
20-line ASCII chart: equity curve on top, drawdown from peak on the bottom.

    python -m scripts.equity_chart
    python -m scripts.equity_chart --mode paper --symbol BTC/USDT
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from tradebot.config import load_config
from tradebot.enums import Mode
from tradebot.journal import TradeJournal

# -- chart dimensions --------------------------------------------------------
EQUITY_LINES = 10
DRAWDOWN_LINES = 10
CHART_WIDTH = 60  # inner columns between y-axis and right edge

# -- ASCII charset -----------------------------------------------------------
CHAR_POINT = "*"
CHAR_EMPTY = " "
CHAR_X_TICK = "+"
CHAR_X_LINE = "-"
CHAR_Y_LINE = "|"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ms(ms: int) -> datetime:
    """Convert epoch ms to a UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _date_label(dt: datetime) -> str:
    """Compact date for x-axis, e.g. 'Jan' or 'Jan 26'."""
    return dt.strftime("%b")


def _format_value(v: float) -> str:
    """Format a number compactly for y-axis labels."""
    if abs(v) >= 10_000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _format_pct(v: float) -> str:
    """Format a percentage for drawdown y-axis labels."""
    return f"{v:.1f}%"


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

def _render_chart(
    values: list[float],
    y_min: float,
    y_max: float,
    n_lines: int,
    timestamps: list[int],
    y_formatter,
    invert: bool = False,
) -> list[str]:
    """Render an ASCII chart from *values* into *n_lines* rows.

    Parameters
    ----------
    values : equity or drawdown series (same length as timestamps)
    y_min, y_max : value range to map into
    n_lines : number of rows to produce
    timestamps : epoch-ms for x-axis labels
    y_formatter : callable(float) -> str for y-axis labels
    invert : if True, higher values render lower (drawdown convention)
    """
    if not values:
        return []

    n_points = len(values)
    # Determine how many columns the data spans (at most CHART_WIDTH)
    n_cols = min(n_points, CHART_WIDTH)

    # Build the canvas: n_lines rows x n_cols columns
    canvas: list[list[str]] = [[CHAR_EMPTY] * n_cols for _ in range(n_lines)]

    # Map each data point to a column index (subsample if needed)
    if n_points <= n_cols:
        col_indices = list(range(n_points))
    else:
        step = (n_points - 1) / (n_cols - 1)
        col_indices = [round(i * step) for i in range(n_cols)]

    # Compute y range with a small padding
    v_range = y_max - y_min
    if v_range <= 0:
        v_range = 1.0  # flat line — put everything in the middle

    # Plot points
    for ci, pi in enumerate(col_indices):
        v = values[pi]
        # Map value to row (0 = top, n_lines-1 = bottom)
        frac = (v - y_min) / v_range
        if invert:
            frac = 1.0 - frac
        row = round(frac * (n_lines - 1))
        row = max(0, min(n_lines - 1, row))
        canvas[row][ci] = CHAR_POINT

    # Render lines with y-axis labels
    lines: list[str] = []
    for r in range(n_lines):
        # Top row = max value, bottom row = min value
        frac = 1.0 - r / (n_lines - 1) if n_lines > 1 else 1.0
        y_val = y_min + frac * v_range
        label = y_formatter(y_val)
        row_str = "".join(canvas[r])
        lines.append(f" {label:>{6}} {CHAR_Y_LINE}{row_str}")

    # X-axis border
    border = f"      {CHAR_X_TICK}" + (CHAR_X_LINE * n_cols)
    lines.append(border)

    # X-axis date labels (start, middle, end)
    label_spots = [0, n_points // 2, n_points - 1]
    tick_cols: list[int] = []
    for lp in label_spots:
        col = lp if n_points <= n_cols else round(lp / (n_points - 1) * (n_cols - 1))
        tick_cols.append(col)

    # Build the label row: mark tick positions for label placement.
    label_buf = [" "] * n_cols

    # Place labels: first and middle labels go after their tick, last goes before.
    for idx, lp in enumerate(label_spots):
        dt = _fmt_ms(timestamps[lp])
        text = _date_label(dt)
        tc = tick_cols[idx]
        if idx == len(label_spots) - 1:
            start = tc - len(text)  # left of tick
        else:
            start = tc + 1  # right of tick
        for ch_i, ch in enumerate(text):
            pos = start + ch_i
            if 0 <= pos < n_cols:
                label_buf[pos] = ch

    # Append year after last label if room
    year_str = str(_fmt_ms(timestamps[-1]).year) if timestamps else ""
    if year_str:
        last_label_end = tick_cols[-1] + 1
        # Find the end of the last placed label text
        for p in range(n_cols - 1, -1, -1):
            if label_buf[p] != " ":
                last_label_end = p + 2
                break
        for ch_i, ch in enumerate(year_str):
            pos = last_label_end + ch_i
            if pos < n_cols:
                label_buf[pos] = ch

    label_line = "".join(label_buf).rstrip()
    lines.append(f"      {label_line}")

    return lines


def _compute_drawdowns(equities: list[float]) -> list[float]:
    """Compute drawdown percentages from a equity series.

    Returns a list of percentages (0.0 to negative) where each element is
    (peak - value) / peak * 100, i.e. 0.0 means at peak, -5.0 means 5% DD.
    """
    peak = equities[0] if equities else 1.0
    drawdowns: list[float] = []
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = ((peak - eq) / peak) * 100.0 if peak > 0 else 0.0
        drawdowns.append(-dd)  # negative = below peak
    return drawdowns


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="ASCII equity curve + drawdown chart.")
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
        print("  The bot hasn't run yet in this mode.\n")
        return 0

    journal = TradeJournal(db)
    try:
        _print_chart(cfg, symbol, journal)
    finally:
        journal.close()
    return 0


def _print_chart(cfg, symbol: str, journal: TradeJournal) -> None:
    """Read equity data and render both charts."""
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    series = journal.equity_between(0, now_ms)

    if not series:
        print("\n  No equity data yet — the bot hasn't traded.\n")
        return

    timestamps = [row[0] for row in series]
    equities = [row[1] for row in series]

    # -- equity chart --------------------------------------------------------
    eq_min = min(equities)
    eq_max = max(equities)
    # Add a small padding so the curve isn't flush with top/bottom
    padding = max((eq_max - eq_min) * 0.05, 1.0)
    eq_min_padded = eq_min - padding
    eq_max_padded = eq_max + padding

    mode_label = cfg.mode.value
    print()
    print(f"=== Equity Curve ({symbol} {mode_label}) ===")
    print()

    equity_chart = _render_chart(
        equities,
        y_min=eq_min_padded,
        y_max=eq_max_padded,
        n_lines=EQUITY_LINES,
        timestamps=timestamps,
        y_formatter=_format_value,
    )
    for line in equity_chart:
        print(line)

    # -- drawdown chart ------------------------------------------------------
    drawdowns = _compute_drawdowns(equities)
    dd_min = min(drawdowns)  # most negative = deepest drawdown
    dd_max = 0.0  # always 0% at peak
    # Pad so the 0% line isn't the top row
    dd_padding = max(abs(dd_min) * 0.1, 0.1)
    dd_min_padded = dd_min - dd_padding

    print()
    print(f"=== Drawdown from Peak ({symbol} {mode_label}) ===")
    print()

    dd_chart = _render_chart(
        drawdowns,
        y_min=dd_min_padded,
        y_max=dd_max,
        n_lines=DRAWDOWN_LINES,
        timestamps=timestamps,
        y_formatter=_format_pct,
        invert=True,  # drawdown grows downward
    )
    for line in dd_chart:
        print(line)

    print()


if __name__ == "__main__":
    sys.exit(main())
