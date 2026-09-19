"""RiskManager gate + sizing tests — the capital-preservation guarantees."""

from __future__ import annotations

import pytest

from tradebot.enums import RejectCode, Side
from tradebot.risk import RiskManager
from tradebot.types import AccountState, Approval, Position, Rejection, Signal


def _buy(stop=98.0, target=None, conf=1.0):
    return Signal(side=Side.BUY, reason="test", stop=stop, target=target, confidence=conf)


def test_sizing_risks_one_percent(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    dec = rm.evaluate(_buy(stop=98.0), account_flat, market, ref_price=100.0, now_ms=0)
    assert isinstance(dec, Approval)
    # est risk should be ~1% of equity (costs folded in keep it at/under 1%)
    assert dec.est_risk_pct == pytest.approx(0.01, abs=0.0015)
    assert dec.order.qty > 0
    assert dec.order.side == Side.BUY
    assert dec.stop_price == 98.0


def test_wider_stop_smaller_size(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    tight = rm.evaluate(_buy(stop=99.0), account_flat, market, 100.0, 0)
    wide = rm.evaluate(_buy(stop=90.0), account_flat, market, 100.0, 0)
    assert isinstance(tight, Approval) and isinstance(wide, Approval)
    assert wide.order.qty < tight.order.qty  # wider stop => smaller position


def test_mandatory_stop_rejected(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    dec = rm.evaluate(_buy(stop=None), account_flat, market, 100.0, 0)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.NO_VALID_STOP


def test_stop_wrong_side_rejected(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    dec = rm.evaluate(_buy(stop=105.0), account_flat, market, 100.0, 0)  # stop above entry
    assert isinstance(dec, Rejection) and dec.code == RejectCode.NO_VALID_STOP


def test_stop_too_far_rejected(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    dec = rm.evaluate(_buy(stop=50.0), account_flat, market, 100.0, 0)  # 50% stop > max band
    assert isinstance(dec, Rejection) and dec.code == RejectCode.NO_VALID_STOP


def test_below_min_notional_rejected(risk_cfg, market):
    rm = RiskManager(risk_cfg)
    # tiny equity -> 1% risk produces sub-$10 notional -> reject, do NOT upsize
    acct = AccountState(equity=20.0, free=20.0, positions=())
    dec = rm.evaluate(_buy(stop=98.0), acct, market, 100.0, 0)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.SIZE_BELOW_MIN


def test_non_spot_market_rejected(risk_cfg, account_flat):
    from tradebot.types import MarketInfo

    rm = RiskManager(risk_cfg)
    fut = MarketInfo("BTC/USDT", False, "BTC", "USDT", 0.01, 1e-6, 1e-5, 1e9, 10.0)
    dec = rm.evaluate(_buy(), account_flat, fut, 100.0, 0)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.LEVERAGE_FORBIDDEN


def test_already_in_position_rejected(risk_cfg, market):
    rm = RiskManager(risk_cfg)
    pos = Position(symbol="BTC/USDT", side=Side.BUY, qty=1.0, avg_entry=100.0)
    acct = AccountState(equity=10_000.0, free=5_000.0, positions=(pos,))
    dec = rm.evaluate(_buy(), acct, market, 100.0, 0)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.ALREADY_IN_POSITION


def test_daily_loss_limit_blocks(risk_cfg, market):
    rm = RiskManager(risk_cfg)
    rm.on_equity_update(10_000.0, now_ms=0)  # anchor day-start equity
    acct = AccountState(equity=9_600.0, free=9_600.0, positions=())  # -4% > 3% limit
    dec = rm.evaluate(_buy(), acct, market, 100.0, now_ms=1000)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.DAILY_LOSS_LIMIT


def test_drawdown_kill_switch(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    rm.on_equity_update(10_000.0, 0)  # peak
    engaged = rm.on_equity_update(8_000.0, 1000)  # -20% > 15%
    assert engaged is True and rm.state.kill_switch_engaged
    dec = rm.evaluate(_buy(), AccountState(8000, 8000, ()), market, 100.0, 2000)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.KILL_SWITCH_ENGAGED
    assert dec.halt is True


def test_loss_streak_cooldown(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    from tradebot.types import Trade

    for _ in range(risk_cfg.loss_streak_threshold):
        t = Trade("BTC/USDT", Side.BUY, 1, 100, 98, 0, 0, -2.0, 0.2, -0.02, 5, "x", "stop")
        rm.on_trade_closed(t, now_ms=0)
    dec = rm.evaluate(_buy(), account_flat, market, 100.0, now_ms=1000)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.LOSS_STREAK_COOLDOWN


def test_insufficient_edge_rejected(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    # target barely above entry, below the min RR after costs -> reject
    dec = rm.evaluate(_buy(stop=98.0, target=100.5), account_flat, market, 100.0, 0)
    assert isinstance(dec, Rejection) and dec.code == RejectCode.INSUFFICIENT_EDGE


def test_happy_path_with_good_target(risk_cfg, market, account_flat):
    rm = RiskManager(risk_cfg)
    dec = rm.evaluate(_buy(stop=98.0, target=106.0), account_flat, market, 100.0, 0)
    assert isinstance(dec, Approval)
    assert dec.target_price == 106.0
