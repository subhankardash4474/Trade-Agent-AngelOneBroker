"""
Unit tests for the enhanced RiskManager module.
Tests ATR stops, trailing SL, VIX filter, consecutive losses,
drawdown tiers, weekly limits, and time-based exits.
"""

import pytest

from core.risk_manager import RiskManager, TrailingStop


@pytest.fixture
def risk_config():
    return {
        "risk": {
            "max_position_size_pct": 20.0,
            "max_risk_per_trade_pct": 1.0,
            "atr_stop_multiplier": 1.5,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 3.0,
            "trailing_activation_rr": 1.0,
            "trailing_step_pct": 0.3,
            "daily_loss_limit_pct": 3.0,
            "max_trades_per_day": 5,
            "max_open_positions": 2,
            "max_consecutive_losses": 2,
            "drawdown_reduce_pct": 15.0,
            "drawdown_halt_pct": 30.0,
            "max_drawdown_pct": 10.0,
            "weekly_loss_limit_pct": 5.0,
            "max_vix": 25.0,
            "require_nifty_above_200ema": True,
            "intraday_exit_time": "15:15",
        }
    }


@pytest.fixture
def rm(risk_config):
    manager = RiskManager(risk_config, initial_balance=10000.0)
    # Override intraday exit time to avoid test failures outside market hours
    manager.intraday_exit_time = "23:59"
    return manager


class TestPositionSizing:
    def test_basic_sizing(self, rm):
        qty = rm.calculate_position_size(price=100.0, stop_loss_price=98.5)
        assert qty > 0
        assert qty * 100 <= 10000 * 0.2

    def test_atr_based_sizing(self, rm):
        qty = rm.calculate_position_size(price=100.0, atr=2.0)
        assert qty > 0

    def test_zero_price(self, rm):
        assert rm.calculate_position_size(price=0) == 0

    def test_drawdown_reduces_size(self, rm):
        normal_qty = rm.calculate_position_size(price=100.0, stop_loss_price=98.5)
        # Simulate 15% drawdown
        rm.state.current_balance = 8500.0
        reduced_qty = rm.calculate_position_size(price=100.0, stop_loss_price=98.5)
        assert reduced_qty <= normal_qty


class TestCanTrade:
    def test_initial_can_trade(self, rm):
        allowed, reason = rm.can_trade()
        assert allowed is True

    def test_blocked_by_daily_loss(self, rm):
        rm.record_trade(-301.0)
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Daily loss" in reason

    def test_blocked_by_consecutive_losses(self, rm):
        rm.record_trade(-10.0)
        rm.record_trade(-10.0)
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Consecutive" in reason

    def test_blocked_by_max_positions(self, rm):
        rm.update_open_positions(2)
        allowed, reason = rm.can_trade()
        assert allowed is False

    def test_blocked_by_max_trades(self, rm):
        for _ in range(5):
            rm.record_trade(1.0)
        allowed, reason = rm.can_trade()
        assert allowed is False

    def test_blocked_by_high_vix(self, rm):
        allowed, reason = rm.can_trade(market_context={"india_vix": 30.0})
        assert allowed is False
        assert "VIX" in reason

    def test_blocked_by_nifty_below_200ema(self, rm):
        allowed, reason = rm.can_trade(market_context={"nifty_trend": -1, "india_vix": 15.0})
        assert allowed is False
        assert "Nifty" in reason

    def test_allowed_with_good_market(self, rm):
        allowed, reason = rm.can_trade(market_context={"india_vix": 15.0, "nifty_trend": 1})
        assert allowed is True


class TestDrawdownTiers:
    def test_normal_tier(self, rm):
        summary = rm.get_risk_summary()
        assert summary["drawdown_tier"] == "NORMAL"

    def test_reduced_tier(self, rm):
        rm.state.current_balance = 8400.0  # 16% DD
        summary = rm.get_risk_summary()
        assert summary["drawdown_tier"] == "REDUCED"

    def test_halt_tier(self, rm):
        rm.state.current_balance = 6900.0  # 31% DD
        rm.record_trade(-1.0)  # triggers check
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "CRITICAL" in reason


class TestATRStops:
    def test_atr_stop_loss_buy(self, rm):
        sl = rm.get_atr_stop_loss(100.0, atr=2.0, side="BUY")
        assert sl == 97.0  # 100 - 1.5*2

    def test_atr_stop_loss_sell(self, rm):
        sl = rm.get_atr_stop_loss(100.0, atr=2.0, side="SELL")
        assert sl == 103.0


class TestTrailingStop:
    def test_trailing_activates_at_rr(self):
        ts = TrailingStop(entry_price=100.0, initial_sl=97.0, side="BUY",
                          trail_activation_rr=1.0, trail_step_pct=0.3)
        assert not ts.trailing_active
        # Price rises to 1:1 R:R (risk was 3, so target = 103)
        ts.update(103.0)
        assert ts.trailing_active

    def test_trailing_ratchets_up(self):
        ts = TrailingStop(entry_price=100.0, initial_sl=97.0, side="BUY",
                          trail_activation_rr=1.0, trail_step_pct=0.5)
        ts.update(104.0)
        sl1 = ts.current_sl
        ts.update(106.0)
        sl2 = ts.current_sl
        assert sl2 > sl1

    def test_trailing_never_moves_down(self):
        ts = TrailingStop(entry_price=100.0, initial_sl=97.0, side="BUY",
                          trail_activation_rr=1.0, trail_step_pct=0.5)
        ts.update(105.0)
        sl_high = ts.current_sl
        ts.update(103.0)
        assert ts.current_sl == sl_high


class TestRiskSummary:
    def test_summary_has_new_fields(self, rm):
        summary = rm.get_risk_summary()
        assert "weekly_pnl" in summary
        assert "consecutive_losses" in summary
        assert "drawdown_tier" in summary
        assert "trailing_stops_active" in summary

    def test_weekly_pnl_tracked(self, rm):
        rm.record_trade(50.0)
        rm.record_trade(-20.0)
        summary = rm.get_risk_summary()
        assert summary["weekly_pnl"] == 30.0
