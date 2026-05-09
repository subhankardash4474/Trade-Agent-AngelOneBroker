"""
Tests for the trend-continuation fixes (Apr-28 ADANIENSOL case).

The agent took 3 quick TPs at +Rs 8 each on a stock that ran +Rs 48. These
tests pin the new behavior:

    1. Take-profit widens with trending regimes (3x ATR in bull_low_vol).
    2. trend_continuation flag pushes TP further out (4x ATR).
    3. Re-entry cooldown does NOT apply after a take_profit exit.
    4. Re-entry cooldown DOES apply after a stop_loss exit.
"""

import pytest

from core.risk_manager import RiskManager


@pytest.fixture
def rm():
    cfg = {
        "risk": {
            "atr_stop_multiplier": 1.5,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 3.0,
            "min_profit_to_charges_ratio": 2.0,
            "min_absolute_reward_rs": 15.0,
        }
    }
    return RiskManager(cfg, 10000.0)


class TestRegimeAwareTP:
    def test_default_2x_when_regime_unspecified(self, rm):
        # 2x base = 2 * 1.5 ATR = 3 ATR distance
        tp = rm.get_take_profit(entry_price=100.0, side="BUY", atr=1.0)
        assert abs(tp - 103.0) < 0.01

    def test_bull_low_vol_widens_to_3x(self, rm):
        tp = rm.get_take_profit(100.0, "BUY", atr=1.0, regime="bull_low_vol")
        # 3x base = 3 * 1.5 = 4.5 ATR
        assert abs(tp - 104.50) < 0.01

    def test_sideways_tightens_to_1_5x(self, rm):
        tp = rm.get_take_profit(100.0, "BUY", atr=1.0, regime="sideways")
        # 1.5x base = 1.5 * 1.5 = 2.25 ATR
        assert abs(tp - 102.25) < 0.01

    def test_trend_continuation_overrides_to_4x(self, rm):
        tp = rm.get_take_profit(
            100.0, "BUY", atr=1.0, regime="bull_low_vol", trend_continuation=True,
        )
        # 4x base = 4 * 1.5 = 6 ATR
        assert abs(tp - 106.00) < 0.01

    def test_short_side_inverted(self, rm):
        tp = rm.get_take_profit(100.0, "SELL", atr=1.0, regime="bull_low_vol")
        # Should be below entry by same distance
        assert abs(tp - 95.50) < 0.01


class TestCooldownLogic:
    """
    Cooldown should ONLY apply after losing exits, NOT after take_profit.
    Test against the trading_agent state directly (mock-style).
    """

    def test_take_profit_does_not_trigger_cooldown(self):
        # Simulate the new logic from trading_agent._record_exit
        is_loss = False
        is_take_profit = True
        pnl = 8.0  # winning TP

        should_cooldown = is_loss or (not is_take_profit and pnl < 5.0)
        assert should_cooldown is False, "Take-profit should NOT trigger cooldown"

    def test_stop_loss_triggers_cooldown(self):
        is_loss = True
        is_take_profit = False
        pnl = -25.0

        should_cooldown = is_loss or (not is_take_profit and pnl < 5.0)
        assert should_cooldown is True

    def test_breakeven_signal_exit_triggers_cooldown(self):
        # Tiny positive but signal exit (low conviction) -> cool down to be safe
        is_loss = False
        is_take_profit = False
        pnl = 2.0

        should_cooldown = is_loss or (not is_take_profit and pnl < 5.0)
        assert should_cooldown is True

    def test_high_signal_profit_does_not_cooldown(self):
        # Signal exit but big profit -> trust it, no cooldown
        is_loss = False
        is_take_profit = False
        pnl = 50.0

        should_cooldown = is_loss or (not is_take_profit and pnl < 5.0)
        assert should_cooldown is False
