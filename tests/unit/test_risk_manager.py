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

    # P0 #5 (2026-05-15): regression guards. A None / NaN / non-numeric value
    # in market_context used to raise TypeError ("'>' not supported between
    # instances of 'NoneType' and 'int'"), aborting the trading cycle. After
    # 5 such failures the daemon halted. These verify the defensive coercion.
    def test_can_trade_handles_none_vix(self, rm):
        allowed, reason = rm.can_trade(market_context={"india_vix": None, "nifty_trend": 1})
        assert allowed is True, reason

    def test_can_trade_handles_string_vix(self, rm):
        allowed, reason = rm.can_trade(market_context={"india_vix": "n/a", "nifty_trend": 1})
        assert allowed is True, reason

    def test_can_trade_handles_numeric_string_vix(self, rm):
        # Some upstream caches return numbers as strings; we still want them parsed.
        allowed, reason = rm.can_trade(market_context={"india_vix": "30.0", "nifty_trend": 1})
        assert allowed is False
        assert "VIX" in reason

    def test_can_trade_handles_none_nifty_trend(self, rm):
        # None nifty_trend must not raise. Default to neutral (=1, allows entry).
        allowed, reason = rm.can_trade(
            market_context={"india_vix": 15.0, "nifty_trend": None}
        )
        assert allowed is True, reason

    def test_can_trade_handles_nan_vix(self, rm):
        # NaN > x is always False in Python float semantics, so this would not
        # have crashed historically, but the new path uses float() which
        # propagates NaN. Verify we still allow trading (NaN compares False).
        import math
        allowed, reason = rm.can_trade(
            market_context={"india_vix": math.nan, "nifty_trend": 1}
        )
        assert allowed is True, reason


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


class TestAbsoluteDailyLossFloor:
    """E2E Stage 3: --max-loss-rs hard rupee floor on daily realised P&L.

    Independent from the percentage-based limit; whichever is *tighter*
    fires first.
    """

    def test_floor_disabled_by_default(self, risk_config):
        rm = RiskManager(risk_config, initial_balance=100000.0)
        # No floor configured -> attribute is None
        assert rm.absolute_daily_loss_floor_rs is None

    def test_constructor_kwarg_enables_floor(self, risk_config):
        rm = RiskManager(
            risk_config, initial_balance=100000.0,
            absolute_daily_loss_floor_rs=500.0,
        )
        rm.intraday_exit_time = "23:59"
        assert rm.absolute_daily_loss_floor_rs == 500.0

    def test_rupee_floor_trips_before_percentage_at_small_loss(self, risk_config):
        """On a Rs 1L config, the 3% limit is Rs 3,000. With --max-loss-rs 500,
        the rupee floor should trip at Rs 500 -- well before the % limit."""
        rm = RiskManager(
            risk_config, initial_balance=100000.0,
            absolute_daily_loss_floor_rs=500.0,
        )
        rm.intraday_exit_time = "23:59"

        # Rs 250 loss: under both limits, trading still allowed
        rm.record_trade(-250.0)
        allowed, _ = rm.can_trade()
        assert allowed is True

        # Rs 500 cumulative loss: hits the rupee floor exactly
        rm.record_trade(-250.0)
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Absolute daily loss floor breached" in reason

    def test_constructor_kwarg_wins_over_config_value(self, risk_config):
        """CLI --max-loss-rs should override any config-file fallback."""
        risk_config["risk"]["absolute_daily_loss_floor_rs"] = 999.0
        rm = RiskManager(
            risk_config, initial_balance=100000.0,
            absolute_daily_loss_floor_rs=200.0,  # CLI override
        )
        rm.intraday_exit_time = "23:59"
        assert rm.absolute_daily_loss_floor_rs == 200.0

    def test_config_fallback_used_when_no_kwarg(self, risk_config):
        risk_config["risk"]["absolute_daily_loss_floor_rs"] = 750.0
        rm = RiskManager(risk_config, initial_balance=100000.0)
        rm.intraday_exit_time = "23:59"
        assert rm.absolute_daily_loss_floor_rs == 750.0

    def test_floor_breach_activates_circuit_breaker(self, risk_config):
        """A floor breach should not just refuse the next trade -- it
        should activate the breaker state (so subsequent ``can_trade``
        calls also fail without re-evaluating P&L)."""
        rm = RiskManager(
            risk_config, initial_balance=100000.0,
            absolute_daily_loss_floor_rs=300.0,
        )
        rm.intraday_exit_time = "23:59"
        rm.record_trade(-350.0)
        rm.can_trade()  # trips the breaker
        assert rm.state.is_circuit_breaker_active is True
        assert "Absolute daily loss floor" in rm.state.breaker_reason

    def test_floor_not_tripped_below_threshold(self, risk_config):
        rm = RiskManager(
            risk_config, initial_balance=100000.0,
            absolute_daily_loss_floor_rs=500.0,
        )
        rm.intraday_exit_time = "23:59"
        rm.record_trade(-499.99)
        allowed, _ = rm.can_trade(market_context={"india_vix": 15.0, "nifty_trend": 1})
        assert allowed is True


class TestEnforceSlFloor:
    """The HCLTECH bug, 2026-05-13. Strategy-provided SLs must be routed
    through `enforce_sl_floor` so the noise-resistant `min_stop_loss_pct`
    floor isn't bypassed."""

    @pytest.fixture
    def rm_with_floor(self, risk_config):
        risk_config["risk"]["min_stop_loss_pct"] = 1.2
        rm = RiskManager(risk_config, initial_balance=100000.0)
        rm.intraday_exit_time = "23:59"
        return rm

    def test_buy_sl_inside_floor_is_widened(self, rm_with_floor):
        # supertrend_follow on a quiet stock: 0.7% SL distance
        # Entry 1142.35, proposed SL = 1134.35 (0.7% below entry)
        # Floor 1.2% => floor_sl = 1128.64
        floored = rm_with_floor.enforce_sl_floor(
            entry_price=1142.35, proposed_sl=1134.35, side="BUY",
        )
        assert floored == pytest.approx(1128.64, abs=0.05)

    def test_sell_sl_inside_floor_is_widened_hcltech_bug(self, rm_with_floor):
        # The actual 2026-05-13 HCLTECH bug data:
        # Entry 1142.35 (SHORT), proposed SL = 1150.93 (0.75% above)
        # Floor 1.2% => floor_sl = 1156.06
        # The supertrend SL would have been widened to 1156.06, and the
        # actual exit price of 1150.40 would NOT have triggered SL.
        floored = rm_with_floor.enforce_sl_floor(
            entry_price=1142.35, proposed_sl=1150.93, side="SELL",
        )
        assert floored == pytest.approx(1156.06, abs=0.05)
        # Sanity: HCLTECH's actual stop-out price 1150.40 is INSIDE the
        # floored SL (i.e. would NOT have triggered).
        assert 1150.40 < floored

    def test_buy_sl_outside_floor_passes_through(self, rm_with_floor):
        # 2% below entry: already past the 1.2% floor, return as-is.
        floored = rm_with_floor.enforce_sl_floor(
            entry_price=100.0, proposed_sl=98.0, side="BUY",
        )
        assert floored == 98.0

    def test_sell_sl_outside_floor_passes_through(self, rm_with_floor):
        floored = rm_with_floor.enforce_sl_floor(
            entry_price=100.0, proposed_sl=102.0, side="SELL",
        )
        assert floored == 102.0

    def test_floor_disabled_when_zero_config(self, risk_config):
        risk_config["risk"]["min_stop_loss_pct"] = 0.0
        rm = RiskManager(risk_config, initial_balance=100000.0)
        # Even a 0.1 % SL is returned unchanged.
        floored = rm.enforce_sl_floor(100.0, 99.9, "BUY")
        assert floored == 99.9

    def test_get_stop_loss_still_floors(self, rm_with_floor):
        """get_stop_loss must continue to apply the floor itself (no
        regression from extracting the helper)."""
        # ATR 0.5 -> 2.0*ATR = 1.0 absolute -> 1% of 100 -> inside 1.2%.
        sl = rm_with_floor.get_stop_loss(entry_price=100.0, side="BUY", atr=0.5)
        assert sl == pytest.approx(98.8, abs=0.05)


class TestRegimeSizeMultiplier:
    """Regime-aware position sizing (2026-05-13). Multipliers scale BOTH
    risk budget and max-position-value cap."""

    @pytest.fixture
    def rm_with_regime(self, risk_config):
        risk_config["risk"]["regime_size_multipliers"] = {
            "bull_low_vol":  1.20,
            "bear_high_vol": 0.70,
            "sideways":      0.85,
        }
        rm = RiskManager(risk_config, initial_balance=100000.0)
        rm.intraday_exit_time = "23:59"
        return rm

    def test_lookup_known_regime(self, rm_with_regime):
        assert rm_with_regime.regime_size_multiplier("bull_low_vol")  == 1.20
        assert rm_with_regime.regime_size_multiplier("bear_high_vol") == 0.70
        assert rm_with_regime.regime_size_multiplier("sideways")      == 0.85

    def test_unknown_regime_returns_one(self, rm_with_regime):
        # Defaults coverage for any regime the config didn't override.
        # The class's default `bear_low_vol` is 0.85; pass an entirely
        # unmapped regime to verify the fallback.
        assert rm_with_regime.regime_size_multiplier("martian_regime") == 1.0

    def test_none_regime_returns_one(self, rm_with_regime):
        assert rm_with_regime.regime_size_multiplier(None) == 1.0

    def test_bear_high_vol_shrinks_quantity(self, rm_with_regime):
        # Fixture: balance 100k, max_position_size_pct=20, max_risk=1%.
        # Stock Rs 100, SL Rs 2 distance.
        #   max_position_value (no regime) = 100k * 20% = Rs 20k -> 200 shares
        #   risk_budget (no regime)        = 100k * 1%  = Rs 1k  -> 500 shares
        #   binding = min(500, 200) = 200
        base_qty = rm_with_regime.calculate_position_size(
            price=100.0, stop_loss_price=98.0, side="BUY", regime=None,
        )
        assert base_qty == 200

        # bear_high_vol multiplier 0.70:
        #   max_position_value = 100k * 20% * 0.70 = Rs 14k -> 140 shares
        #   risk_budget        = 100k * 1%  * 0.70 = Rs 700 -> 350 shares
        #   binding = min(350, 140) = 140
        bear_qty = rm_with_regime.calculate_position_size(
            price=100.0, stop_loss_price=98.0, side="BUY", regime="bear_high_vol",
        )
        assert bear_qty < base_qty
        assert bear_qty == 140

    def test_bull_low_vol_expands_quantity(self, rm_with_regime):
        # bull_low_vol multiplier 1.20:
        #   max_position_value = 100k * 20% * 1.20 = Rs 24k -> 240 shares
        #   risk_budget        = 100k * 1%  * 1.20 = Rs 1.2k -> 600 shares
        #   binding = min(600, 240) = 240
        bull_qty = rm_with_regime.calculate_position_size(
            price=100.0, stop_loss_price=98.0, side="BUY", regime="bull_low_vol",
        )
        assert bull_qty == 240

    def test_regime_none_matches_no_arg_behaviour(self, rm_with_regime):
        # Backwards-compat: omitting the arg or passing None must be identical.
        q1 = rm_with_regime.calculate_position_size(
            price=100.0, stop_loss_price=98.0, side="BUY",
        )
        q2 = rm_with_regime.calculate_position_size(
            price=100.0, stop_loss_price=98.0, side="BUY", regime=None,
        )
        assert q1 == q2


class TestDynamicThresholdSmoothing:
    """The threshold-tuner now uses a continuous WR->threshold mapping
    (was a per-cycle ratchet that saturated to bound). We verify the
    pure math here; the cycle-loop integration lives in trading_agent."""

    def _target(self, base: float, wr: float, span: float) -> float:
        return base + (0.5 - wr) * span

    def test_wr_at_50pct_returns_base(self):
        assert self._target(0.55, 0.5, 0.30) == pytest.approx(0.55)

    def test_wr_extreme_low_pushes_to_max_band(self):
        # WR=0% should push threshold to base + span/2 = 0.70
        assert self._target(0.55, 0.0, 0.30) == pytest.approx(0.70)

    def test_wr_extreme_high_pushes_to_min_band(self):
        # WR=100% should pull threshold to base - span/2 = 0.40
        assert self._target(0.55, 1.0, 0.30) == pytest.approx(0.40)

    def test_monotonic_in_wr(self):
        prev = float("inf")
        for wr in (0.0, 0.2, 0.5, 0.7, 1.0):
            cur = self._target(0.55, wr, 0.30)
            assert cur < prev
            prev = cur

    def test_idempotent_under_repeated_call(self):
        """Hitting the formula 100x with the same WR yields the same
        threshold -- the bug it replaces ratcheted unboundedly."""
        targets = [self._target(0.55, 0.35, 0.30) for _ in range(100)]
        assert len(set(targets)) == 1
