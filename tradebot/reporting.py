"""Weekly reflection — an automatic, capital-preservation-focused review.

Reads the trade logbook + equity series from the journal and produces a
deterministic, rule-based markdown report. No LLM dependency — every line is
derived from recorded numbers so the review is reproducible and trustworthy.

Surfaces what actually decides whether a capital-preservation bot survives:
expectancy in R-multiples, payoff ratio, fee drag, exposure, drawdown vs the
kill-switch, and the all-time live risk-adjusted metrics vs the go/no-go gate.

``weekly_stats`` computes the numbers (unit-tested); ``build_weekly_report``
renders them to markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean

from .config import BotConfig
from .journal import TradeJournal
from .types import Trade, timeframe_ms

_WEEK_MS = 7 * 24 * 3600 * 1000


def iso_week_bounds(ms: int) -> tuple[int, int]:
    """[Monday 00:00 UTC, next Monday) for the ISO week containing ms."""
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(monday.timestamp() * 1000)
    return start, start + _WEEK_MS


def iso_week_label(ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def _fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _max_drawdown(series: list[float]) -> float:
    peak, mdd = (series[0] if series else 0.0), 0.0
    for e in series:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def _longest_loss_streak(trades: list[Trade]) -> int:
    s = best = 0
    for t in trades:
        if t.pnl < 0:
            s += 1
            best = max(best, s)
        else:
            s = 0
    return best


@dataclass
class WeeklyStats:
    label: str
    week_start_ms: int
    week_end_ms: int
    # activity
    n_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    # money
    net_pnl: float
    fees: float
    weekly_return: float
    start_equity: float
    end_equity: float
    intra_week_max_dd: float
    # edge quality
    expectancy_r: float          # avg R per trade (pnl / intended 1% risk)
    payoff_ratio: float          # avg win / avg loss (pnl)
    fees_pct_gross: float        # fees / gross winning pnl
    fees_pct_equity: float       # fees / week-open equity
    exposure_pct: float          # fraction of the week with an open position
    best_trade_share: float      # single best trade's share of gross profit
    # risk discipline
    worst_loss_pct_equity: float
    avg_realised_risk_pct: float  # avg loss as % equity (target ~1%)
    risk_breaches: int            # trades that lost more than the 2% hard cap
    longest_loss_streak: int
    # capital preservation / all-time
    current_equity: float
    peak_equity: float
    drawdown_from_peak: float
    pct_to_kill_switch: float
    all_time_trades: int
    at_sharpe: float
    at_sortino: float
    at_calmar: float
    at_max_dd: float
    at_expectancy_pct: float
    gate_pass: bool
    gate_reasons: list[str] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    reflections: list[str] = field(default_factory=list)


def weekly_stats(journal: TradeJournal, config: BotConfig, week_start_ms: int) -> WeeklyStats:
    from .metrics import compute_metrics  # lazy: avoid import cycle / heavy load at module import
    from .walkforward import backtest_to_paper_gate

    start, end = week_start_ms, week_start_ms + _WEEK_MS
    risk = config.risk

    trades = journal.trades_between(start, end)
    eq_rows = journal.equity_between(start, end)
    eq_vals = [e for _, e in eq_rows]
    baseline = journal.equity_at_or_before(start)
    # week-open equity: last snapshot strictly before/at the boundary; else the
    # starting equity (NOT the first in-week snapshot, which is already mid-week).
    start_eq = baseline if baseline is not None else config.initial_equity
    end_eq = eq_vals[-1] if eq_vals else start_eq

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    net = sum(t.pnl for t in trades)
    fees = sum(t.fees for t in trades)
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    avg_win = fmean([t.pnl for t in wins]) if wins else 0.0
    avg_loss = fmean([abs(t.pnl) for t in losses]) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)

    # expectancy in R-multiples + realised risk (vs equity at the time of each trade)
    rs: list[float] = []
    realised_risk: list[float] = []
    worst_loss_pct = 0.0
    breaches = 0
    for t in trades:
        eq_at = journal.equity_at_or_before(t.entry_ms) or start_eq
        intended = risk.risk_fraction_per_trade * eq_at
        if intended > 0:
            rs.append(t.pnl / intended)
        if t.pnl < 0 and eq_at > 0:
            loss_pct = -t.pnl / eq_at
            realised_risk.append(loss_pct)
            worst_loss_pct = max(worst_loss_pct, loss_pct)
            if loss_pct > risk.risk_hard_cap_per_trade:
                breaches += 1

    tf_ms = timeframe_ms(config.timeframe)
    exposure = min(1.0, sum(t.bars_held for t in trades) * tf_ms / _WEEK_MS)
    best_share = (max((t.pnl for t in wins), default=0.0) / gross_win) if gross_win > 0 else 0.0

    # all-time, from the full equity series + all trades — reuse the backtest metrics
    all_eq = journal.equity_between(0, end)
    all_trades = journal.all()
    peak_eq = max((e for _, e in all_eq), default=start_eq)
    cur_eq = journal.latest_equity() or end_eq
    cur_dd = ((peak_eq - cur_eq) / peak_eq) if peak_eq > 0 else 0.0
    if len(all_eq) >= 2:
        m = compute_metrics(all_eq, all_trades, config.timeframe, config.initial_equity, total_bars=len(all_eq))
        gate_pass, gate_reasons = backtest_to_paper_gate(m)
        at = (m.sharpe, m.sortino, m.calmar, m.max_drawdown_pct, m.expectancy_pct)
    else:
        gate_pass, gate_reasons = False, ["insufficient live history"]
        at = (0.0, 0.0, 0.0, 0.0, 0.0)

    s = WeeklyStats(
        label=iso_week_label(week_start_ms), week_start_ms=start, week_end_ms=end,
        n_trades=len(trades), wins=len(wins), losses=len(losses),
        win_rate=(len(wins) / len(trades)) if trades else 0.0, profit_factor=pf,
        net_pnl=net, fees=fees,
        weekly_return=(end_eq / start_eq - 1.0) if start_eq > 0 else 0.0,
        start_equity=start_eq, end_equity=end_eq,
        intra_week_max_dd=_max_drawdown([start_eq, *eq_vals]),  # peak seeded at week-open equity
        expectancy_r=(fmean(rs) if rs else 0.0), payoff_ratio=payoff,
        fees_pct_gross=(fees / gross_win) if gross_win > 0 else 0.0,
        fees_pct_equity=(fees / start_eq) if start_eq > 0 else 0.0,
        exposure_pct=exposure, best_trade_share=best_share,
        worst_loss_pct_equity=worst_loss_pct,
        avg_realised_risk_pct=(fmean(realised_risk) if realised_risk else 0.0),
        risk_breaches=breaches, longest_loss_streak=_longest_loss_streak(trades),
        current_equity=cur_eq, peak_equity=peak_eq, drawdown_from_peak=cur_dd,
        pct_to_kill_switch=(cur_dd / risk.max_drawdown_pct) if risk.max_drawdown_pct > 0 else 0.0,
        all_time_trades=len(all_trades),
        at_sharpe=at[0], at_sortino=at[1], at_calmar=at[2], at_max_dd=at[3], at_expectancy_pct=at[4],
        gate_pass=gate_pass, gate_reasons=gate_reasons, trades=trades,
    )
    s.reflections = _reflect(s, config)
    return s


def _reflect(s: WeeklyStats, config: BotConfig) -> list[str]:
    risk = config.risk
    out: list[str] = []
    if s.n_trades == 0:
        out.append("No trades closed this week — the strategy stayed flat (often correct; cash is a position).")
    else:
        verb = "gained" if s.net_pnl >= 0 else "lost"
        out.append(f"{s.n_trades} trade(s); {verb} {abs(s.net_pnl):.2f} {config.quote_currency} "
                   f"({s.weekly_return:+.2%} on equity), win rate {s.win_rate:.0%}.")
        # expectancy in R is the real proof of edge — PF/win-rate can mislead
        if s.expectancy_r > 0:
            out.append(f"Expectancy {s.expectancy_r:+.2f}R per trade (payoff {s.payoff_ratio:.2f}:1) — "
                       "positive, edge present this week.")
        else:
            out.append(f"Expectancy {s.expectancy_r:+.2f}R per trade — NEGATIVE; the edge did not show this week "
                       "(normal for trend/breakout in chop, but watch the running average).")
        if s.fees_pct_gross > 0.25:
            out.append(f"⚠️ Fees ate {s.fees_pct_gross:.0%} of gross profit — cost drag is high relative to edge "
                       "(the small-account tax).")
        if s.risk_breaches:
            out.append(f"⚠️ {s.risk_breaches} trade(s) lost more than the {risk.risk_hard_cap_per_trade:.0%} hard "
                       "cap (slippage/gap through stop) — investigate fills.")
        else:
            out.append(f"Risk discipline held: largest loss {s.worst_loss_pct_equity:.2%} of equity, avg loss "
                       f"{s.avg_realised_risk_pct:.2%} vs the {risk.risk_fraction_per_trade:.0%} target.")
        if s.longest_loss_streak >= risk.loss_streak_threshold:
            out.append(f"Loss streak of {s.longest_loss_streak} hit the cooldown threshold "
                       f"({risk.loss_streak_threshold}); the bot pauses entries — don't override it.")
    if s.drawdown_from_peak > 0:
        out.append(f"Drawdown from peak: {s.drawdown_from_peak:.2%} — {s.pct_to_kill_switch:.0%} of the way to the "
                   f"{risk.max_drawdown_pct:.0%} kill-switch.")
    else:
        out.append("At a fresh equity high; no open drawdown.")
    # the decisive production question
    if s.all_time_trades >= 5:
        verdict = "PASSES" if s.gate_pass else "does NOT pass"
        tail = "" if s.gate_pass else f" ({'; '.join(s.gate_reasons[:2])})"
        out.append(f"All-time live: Sortino {s.at_sortino:.2f}, Calmar {s.at_calmar:.2f} — {verdict} the go/no-go "
                   f"gate{tail}.")
    return out


def build_weekly_report(journal: TradeJournal, config: BotConfig, week_start_ms: int) -> tuple[str, str]:
    """Return (iso_week_label, markdown)."""
    s = weekly_stats(journal, config, week_start_ms)
    q = config.quote_currency

    def _r(x: float) -> str:
        return "inf" if x == float("inf") else f"{x:.2f}"

    md = [
        f"# Weekly reflection — {s.label}",
        f"_{_fmt_date(s.week_start_ms)} → {_fmt_date(s.week_end_ms - 86_400_000)} UTC · "
        f"{config.symbols[0]} · {config.timeframe} · {config.strategy.name} · mode={config.mode.value}_",
        "",
        "## This week",
        f"- Trades: **{s.n_trades}**  ·  Win rate: **{s.win_rate:.0%}**  ·  Profit factor: **{_r(s.profit_factor)}**  "
        f"·  Exposure: {s.exposure_pct:.0%}",
        f"- Net P&L: **{s.net_pnl:+.2f} {q}** ({s.weekly_return:+.2%})  ·  Equity: {s.start_equity:.2f} → "
        f"**{s.end_equity:.2f}**  ·  Intra-week max DD: {s.intra_week_max_dd:.2%}",
        f"- **Expectancy: {s.expectancy_r:+.2f}R/trade**  ·  Payoff: {_r(s.payoff_ratio)}:1  ·  "
        f"Fees: {s.fees:.2f} ({s.fees_pct_gross:.0%} of gross profit)",
    ]
    if s.trades:
        best = max(s.trades, key=lambda t: t.return_pct)
        worst = min(s.trades, key=lambda t: t.return_pct)
        md.append(f"- Best: {best.return_pct:+.2%} ({best.exit_reason})  ·  Worst: {worst.return_pct:+.2%} "
                  f"({worst.exit_reason})  ·  Top trade = {s.best_trade_share:.0%} of gross profit")
    md += [
        "",
        "## Risk & capital preservation",
        f"- Largest single-trade loss: {s.worst_loss_pct_equity:.2%} of equity (hard cap "
        f"{config.risk.risk_hard_cap_per_trade:.0%}; breaches: {s.risk_breaches})",
        f"- Avg realised risk/loss: {s.avg_realised_risk_pct:.2%} (target {config.risk.risk_fraction_per_trade:.0%})  "
        f"·  Longest loss streak: {s.longest_loss_streak}",
        f"- Current equity: **{s.current_equity:.2f}**  ·  Peak: {s.peak_equity:.2f}  ·  Drawdown: "
        f"**{s.drawdown_from_peak:.2%}** ({s.pct_to_kill_switch:.0%} of the {config.risk.max_drawdown_pct:.0%} kill-switch)",
        "",
        "## All-time live (vs go/no-go gate)",
        f"- Sharpe {s.at_sharpe:.2f}  ·  Sortino {s.at_sortino:.2f}  ·  Calmar {s.at_calmar:.2f}  ·  "
        f"Max DD {s.at_max_dd:.2%}  ·  Expectancy {s.at_expectancy_pct:.3%}  ·  Trades {s.all_time_trades}",
        f"- Gate: **{'PASS' if s.gate_pass else 'NO-GO'}**"
        + ("" if s.gate_pass else "  — " + "; ".join(s.gate_reasons)),
        "",
        "## Reflection",
        *[f"- {line}" for line in s.reflections],
    ]
    if s.trades:
        md += ["", "## Trades this week",
               "| exit (UTC) | entry | exit | P&L | ret% | held | exit reason |",
               "|---|--:|--:|--:|--:|--:|---|"]
        for t in s.trades:
            md.append(f"| {datetime.fromtimestamp(t.exit_ms/1000, tz=timezone.utc):%Y-%m-%d %H:%M} | "
                      f"{t.entry_price:.2f} | {t.exit_price:.2f} | {t.pnl:+.2f} | {t.return_pct:+.2%} | "
                      f"{t.bars_held} | {t.exit_reason} |")
    md.append("")
    return s.label, "\n".join(md)


def write_report(text: str, label: str, reports_dir: str | Path) -> Path:
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"weekly_{label}.md"
    path.write_text(text, encoding="utf-8")
    return path
