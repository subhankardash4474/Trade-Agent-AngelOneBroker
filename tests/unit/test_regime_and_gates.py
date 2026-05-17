"""
Tests for the new production hardening: regime classification, expected-profit
gate, per-strategy attribution, and dynamic confidence threshold.
"""

import pandas as pd
import pytest

from strategies.ensemble import EnsembleModel
from core.regime import classify_regime, regime_multiplier
from core.risk_manager import RiskManager
from strategies.base_strategy import Signal, TradeSignal


# ----------------------------------------------------------------
# Regime classification
# ----------------------------------------------------------------

class TestRegimeClassification:
    def test_bull_low_vol(self):
        ctx = {"nifty_trend": 1, "india_vix": 12.0}
        assert classify_regime(ctx) == "bull_low_vol"

    def test_bull_high_vol(self):
        ctx = {"nifty_trend": 1, "india_vix": 20.0}
        assert classify_regime(ctx) == "bull_high_vol"

    def test_bear_high_vol(self):
        ctx = {"nifty_trend": -1, "india_vix": 26.0}
        assert classify_regime(ctx) == "bear_high_vol"

    def test_bear_low_vol(self):
        ctx = {"nifty_trend": -1, "india_vix": 14.0}
        assert classify_regime(ctx) == "bear_low_vol"

    def test_unknown_on_missing(self):
        assert classify_regime(None) == "unknown"
        assert classify_regime({}) == "unknown"
        assert classify_regime({"nifty_trend": 1}) == "unknown"

    # P1 #11 (2026-05-17): when Yahoo Nifty history is short (<200 closes)
    # or absent, trading_agent now defaults nifty_trend to 0 (neutral) rather
    # than 1 (bull). Verify classify_regime routes 0 to "sideways" so the
    # sideways position-size multiplier applies \u2014 bear defenses retained.
    def test_neutral_trend_routes_to_sideways(self):
        assert classify_regime({"nifty_trend": 0, "india_vix": 14.0}) == "sideways"
        assert classify_regime({"nifty_trend": 0, "india_vix": 22.0}) == "sideways"

    def test_mr_multiplier_favors_sideways(self):
        assert regime_multiplier("mean_reversion", "sideways") > 1.0
        assert regime_multiplier("mean_reversion", "bull_low_vol") < 1.0

    def test_supertrend_multiplier_favors_trend(self):
        assert regime_multiplier("supertrend_follow", "bull_low_vol") > 1.0
        assert regime_multiplier("supertrend_follow", "sideways") < 1.0


# ----------------------------------------------------------------
# Expected-profit gate
# ----------------------------------------------------------------

@pytest.fixture
def rm():
    cfg = {
        "risk": {
            "min_profit_to_charges_ratio": 2.0,
            "min_absolute_reward_rs": 15.0,
        }
    }
    return RiskManager(cfg, 10000.0)


class TestExpectedProfitGate:
    def test_rejects_tiny_reward(self, rm):
        # TP of Rs 50.50 on a 10-share trade = Rs 5 reward, below Rs 15 floor
        ok, reason = rm.is_trade_worth_taking(
            entry_price=50.0, take_profit=50.50, stop_loss=49.80,
            quantity=10, side="BUY", product="INTRADAY",
        )
        assert not ok
        assert "reward_too_small" in reason

    def test_rejects_poor_rr(self, rm):
        # 1:1 reward:risk should be rejected (needs 1.2x)
        ok, reason = rm.is_trade_worth_taking(
            entry_price=100.0, take_profit=101.0, stop_loss=99.0,
            quantity=50, side="BUY", product="INTRADAY",
        )
        assert not ok
        assert "poor_rr" in reason

    def test_accepts_good_setup(self, rm):
        # 2:1 reward:risk, Rs 200 reward, charges ~ Rs 2, ratio ~ 100x. Should pass.
        ok, reason = rm.is_trade_worth_taking(
            entry_price=100.0, take_profit=104.0, stop_loss=98.0,
            quantity=50, side="BUY", product="INTRADAY",
        )
        assert ok, f"Expected OK, got {reason}"

    def test_rejects_zero_inputs(self, rm):
        ok, _ = rm.is_trade_worth_taking(0, 10, 5, 10)
        assert not ok
        ok, _ = rm.is_trade_worth_taking(100, 100, 100, 0)
        assert not ok


# ----------------------------------------------------------------
# Ensemble per-strategy attribution
# ----------------------------------------------------------------

def _sig(signal: Signal, strat: str, conf: float = 0.8, price: float = 100.0) -> TradeSignal:
    return TradeSignal(
        signal=signal,
        symbol="TEST",
        price=price,
        timestamp=pd.Timestamp.now(),
        strategy_name=strat,
        confidence=conf,
        stop_loss=price * 0.985 if signal == Signal.BUY else None,
        take_profit=price * 1.03 if signal == Signal.BUY else None,
    )


@pytest.fixture
def ensemble():
    return EnsembleModel({
        "ensemble": {
            "confidence_threshold": 0.5,
            "min_strategies_agree": 2,
            "min_dynamic_threshold": 0.45,
            "max_dynamic_threshold": 0.75,
        }
    })


class TestAttribution:
    def test_contributing_strategies_populated(self, ensemble):
        signals = [
            _sig(Signal.BUY, "supertrend_follow", 0.9),
            _sig(Signal.BUY, "rsi_momentum", 0.8),
            _sig(Signal.HOLD, "mean_reversion", 0.0),
        ]
        out = ensemble.aggregate(signals, "TEST", 100.0)
        assert out is not None
        contrib = out.contributing_strategies
        assert contrib is not None
        assert "supertrend_follow" in contrib
        assert "rsi_momentum" in contrib
        assert "mean_reversion" not in contrib  # HOLDs don't get credit
        total = sum(contrib.values())
        assert abs(total - 1.0) < 0.01, f"Contributions must sum to ~1, got {total}"

    def test_higher_weight_gets_more_share(self, ensemble):
        signals = [
            _sig(Signal.BUY, "supertrend_follow", 0.9),   # base weight 1.5
            _sig(Signal.BUY, "mean_reversion", 0.9),       # base weight 0.8
        ]
        out = ensemble.aggregate(signals, "TEST", 100.0)
        assert out is not None
        assert out.contributing_strategies["supertrend_follow"] > out.contributing_strategies["mean_reversion"]


class TestDynamicThreshold:
    def test_raise_threshold_increases(self, ensemble):
        initial = ensemble.confidence_threshold
        ensemble.set_runtime_threshold(initial + 0.1)
        assert ensemble.confidence_threshold > initial

    def test_threshold_clamped(self, ensemble):
        ensemble.set_runtime_threshold(10.0)
        assert ensemble.confidence_threshold <= 0.75
        ensemble.set_runtime_threshold(-1.0)
        assert ensemble.confidence_threshold >= 0.45


class TestRegimeAwareWeighting:
    def test_regime_multiplier_applied(self, ensemble):
        # Mean reversion has regime multiplier 0.6 in bull_high_vol vs 1.4 in sideways
        signals = [
            _sig(Signal.BUY, "mean_reversion", 0.9),
            _sig(Signal.BUY, "supertrend_follow", 0.9),
        ]
        # In sideways: MR boosted, ST suppressed
        w_side = ensemble._build_contributions(signals, regime="sideways")
        # In bull_low_vol: ST boosted, MR suppressed
        w_bull = ensemble._build_contributions(signals, regime="bull_low_vol")
        assert w_side["mean_reversion"] > w_bull["mean_reversion"]
        assert w_bull["supertrend_follow"] > w_side["supertrend_follow"]
