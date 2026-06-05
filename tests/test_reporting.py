"""Weekly reflection computation."""

from __future__ import annotations

import pytest

from tradebot.config import BotConfig
from tradebot.enums import Side
from tradebot.journal import TradeJournal
from tradebot.reporting import build_weekly_report, iso_week_bounds, iso_week_label, weekly_stats
from tradebot.types import Trade

WEEK_START = 1_704_067_200_000  # 2024-01-01 00:00 UTC (a Monday)
DAY = 86_400_000


def _trade(pnl, ret, entry_off, exit_off, reason="stop hit"):
    return Trade("BTC/USDT", Side.BUY, 0.001, 100.0, 100 + pnl, WEEK_START + entry_off,
                 WEEK_START + exit_off, pnl, 0.1, ret, 6, "breakout", reason)


@pytest.fixture
def journal(tmp_path):
    j = TradeJournal(tmp_path / "t.sqlite")
    j.record_equity(WEEK_START, 10_000.0)
    j.record_equity(WEEK_START + 2 * DAY, 9_950.0)
    j.record_equity(WEEK_START + 5 * DAY, 9_850.0)
    j.record(_trade(+200, 0.02, int(0.5 * DAY), 1 * DAY, "target hit"))
    j.record(_trade(-50, -0.005, 2 * DAY + 1, 3 * DAY, "stop hit"))
    j.record(_trade(-300, -0.03, int(3.5 * DAY), 4 * DAY, "stop hit"))  # 300/9950 ≈ 3% > 2% cap
    return j


def test_iso_week_bounds_is_monday():
    start, end = iso_week_bounds(WEEK_START + 3 * DAY)
    assert start == WEEK_START
    assert end == WEEK_START + 7 * DAY
    assert iso_week_label(WEEK_START) == "2024-W01"


def test_weekly_stats(journal):
    s = weekly_stats(journal, BotConfig(), WEEK_START)
    assert s.n_trades == 3
    assert s.wins == 1 and s.losses == 2
    assert s.net_pnl == pytest.approx(-150.0)
    assert s.start_equity == pytest.approx(10_000.0)
    assert s.end_equity == pytest.approx(9_850.0)
    assert s.weekly_return == pytest.approx(-0.015)
    assert s.intra_week_max_dd == pytest.approx(0.015)
    assert s.risk_breaches == 1  # the -300 trade exceeded the 2% hard cap
    assert s.worst_loss_pct_equity == pytest.approx(300 / 9950, abs=1e-4)
    assert s.longest_loss_streak == 2
    assert s.drawdown_from_peak == pytest.approx(0.015)
    assert s.pct_to_kill_switch == pytest.approx(0.10, abs=0.01)
    # edge-quality metrics (R = pnl / (1% * equity-at-entry))
    assert s.expectancy_r == pytest.approx((200 / 100 - 50 / 99.5 - 300 / 99.5) / 3, abs=1e-3)
    assert s.payoff_ratio == pytest.approx(200 / 175, abs=1e-3)  # avg win 200 / avg loss 175
    assert s.fees_pct_gross == pytest.approx(0.3 / 200, abs=1e-4)
    assert s.avg_realised_risk_pct == pytest.approx(((50 + 300) / 9950) / 2, abs=1e-4)


def test_reflection_flags_breach(journal):
    s = weekly_stats(journal, BotConfig(), WEEK_START)
    assert any("hard cap" in r for r in s.reflections)


def test_empty_week_is_graceful(journal):
    s = weekly_stats(journal, BotConfig(), WEEK_START + 4 * 7 * DAY)  # a week with no trades
    assert s.n_trades == 0
    assert any("No trades" in r for r in s.reflections)


def test_build_markdown_has_sections(journal):
    label, md = build_weekly_report(journal, BotConfig(), WEEK_START)
    assert label == "2024-W01"
    assert "# Weekly reflection — 2024-W01" in md
    assert "## Risk & capital preservation" in md
    assert "## All-time live (vs go/no-go gate)" in md
    assert "## Reflection" in md
    assert "Expectancy:" in md
