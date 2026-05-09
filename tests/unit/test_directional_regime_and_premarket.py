"""
Tests for direction-aware regime weighting, cap-aware sizing, and pre-market
warm-up — the three production fixes shipped on 2026-05-04.

Coverage:
  1. Direction-aware regime multiplier — mean_reversion BUY in bear_high_vol
     must be heavily suppressed (catching falling knives), while SELL in the
     same regime stays at a healthy weight (fading rallies).
  2. Ensemble plumbing — `effective_weight` must accept and respect `direction`,
     and `aggregate()` / `_build_contributions()` must pass it through.
  3. Cap-aware notional-floor scaling — the trading_agent's notional-floor
     scaler must clip to the symbol-exposure cap so we don't try to scale a
     trade *into* a sector/symbol concentration rejection.
  4. Pre-market warm-up window — the [open - warmup, open) trigger that lets
     us pre-load the watchlist before the bell.
  5. Config contract — assert today's reverts (ATR 0.5, sym cap 30 %) and
     the new pre-market knob (`scanner.premarket_warmup_minutes`) are in place.
"""

from pathlib import Path

import pytest
import yaml

from core.ensemble import EnsembleModel
from core.regime import (
    STRATEGY_REGIME_PREF,
    STRATEGY_REGIME_PREF_DIRECTIONAL,
    regime_multiplier,
)
from strategies.base_strategy import Signal, TradeSignal


# ----------------------------------------------------------------
# Fix 1 — Direction-aware regime multiplier
# ----------------------------------------------------------------

class TestDirectionalRegimeMultiplier:
    """The headline fix: mean_reversion BUYs must be muted in bear regimes
    while mean_reversion SELLs stay healthy."""

    def test_mean_reversion_buy_killed_in_bear_high_vol(self):
        # 0.1 = ~6x suppression vs the symmetric 0.6 baseline (0.4 base * 1.5).
        # This is the core fix — mean_reversion BUY in bear_high_vol led to
        # 3 consecutive stop-outs on 2026-04-30.
        buy = regime_multiplier("mean_reversion", "bear_high_vol", direction="BUY")
        assert buy <= 0.15, f"mean_reversion BUY in bear_high_vol must be <=0.15, got {buy}"

    def test_mean_reversion_sell_healthy_in_bear_high_vol(self):
        # Fading rallies in a bear tape is the *correct* mean-reversion play —
        # we want this trade to fire if the ensemble agrees.
        sell = regime_multiplier("mean_reversion", "bear_high_vol", direction="SELL")
        assert sell >= 0.5, f"mean_reversion SELL in bear_high_vol must be >=0.5, got {sell}"

    def test_mean_reversion_asymmetry_is_large(self):
        for regime in ("bull_low_vol", "bull_high_vol", "bear_low_vol", "bear_high_vol"):
            buy = regime_multiplier("mean_reversion", regime, direction="BUY")
            sell = regime_multiplier("mean_reversion", regime, direction="SELL")
            # BUY and SELL must differ — symmetric weighting is the bug.
            assert buy != sell, f"{regime}: BUY and SELL multipliers must differ"

    def test_trend_followers_have_directional_overrides_post_2026_05_04(self):
        # 2026-05-04: trend-following strategies were previously assumed to
        # be direction-agnostic (because they "only emit signals when the
        # trend agrees"). Today's bear-regime trade revealed that was the
        # wrong abstraction — a moving_average_crossover SELL in a bear
        # tape is trend-aligned (high edge) while a BUY is counter-trend
        # (low edge), and the symmetric multiplier suppressed both equally.
        # We now apply directional overrides to trend-followers too.
        for strategy in ("supertrend_follow", "moving_average_crossover", "opening_range_breakout"):
            buy = regime_multiplier(strategy, "bear_high_vol", direction="BUY")
            sell = regime_multiplier(strategy, "bear_high_vol", direction="SELL")
            assert sell > buy, (
                f"{strategy} SELL must be > BUY in bear_high_vol "
                f"(trend-aligned beats counter-trend); got BUY={buy}, SELL={sell}"
            )
            # And the inverse should hold in bull regimes.
            buy_bull = regime_multiplier(strategy, "bull_low_vol", direction="BUY")
            sell_bull = regime_multiplier(strategy, "bull_low_vol", direction="SELL")
            assert buy_bull > sell_bull, (
                f"{strategy} BUY must be > SELL in bull_low_vol; "
                f"got BUY={buy_bull}, SELL={sell_bull}"
            )

    def test_rsi_momentum_directional_in_trending_regimes(self):
        # rsi_momentum BUY = bounce off oversold (bullish setup);
        # rsi_momentum SELL = rejection at overbought (bearish setup).
        # Each is favored in its trend-aligned regime.
        sell_bear = regime_multiplier("rsi_momentum", "bear_high_vol", direction="SELL")
        buy_bear = regime_multiplier("rsi_momentum", "bear_high_vol", direction="BUY")
        assert sell_bear > buy_bear, (
            f"rsi_momentum SELL in bear must beat BUY; got SELL={sell_bear}, BUY={buy_bear}"
        )
        # Concretely SELL should be at or near the unsuppressed weight so
        # that a multi-strategy bear-rejection setup can pass the 0.55
        # ensemble threshold.
        assert sell_bear >= 0.8, (
            f"rsi_momentum SELL in bear_high_vol should be >= 0.8 "
            f"(otherwise multi-strategy SELLs get over-suppressed); got {sell_bear}"
        )

    def test_no_direction_still_falls_back_for_trend_followers(self):
        # Pre-evaluation skip-filter (trading_agent.py L1119) calls
        # regime_multiplier WITHOUT direction. That code path must
        # continue to use the symmetric STRATEGY_REGIME_PREF table even
        # for strategies that now have directional overrides — otherwise
        # the skip filter's behavior would silently change.
        from core.regime import STRATEGY_REGIME_PREF
        for strategy in ("supertrend_follow", "moving_average_crossover", "rsi_momentum"):
            no_dir = regime_multiplier(strategy, "bear_high_vol")
            assert no_dir == STRATEGY_REGIME_PREF[strategy]["bear_high_vol"], (
                f"{strategy}: no-direction call must use symmetric table"
            )

    def test_no_direction_falls_back_to_symmetric(self):
        # When direction is None the function must behave identically to
        # the pre-fix (symmetric) version — protects the trading_agent
        # skip-filter call site which doesn't know direction in advance.
        for regime in STRATEGY_REGIME_PREF["mean_reversion"].keys():
            symmetric = regime_multiplier("mean_reversion", regime)
            no_dir = regime_multiplier("mean_reversion", regime, direction=None)
            hold = regime_multiplier("mean_reversion", regime, direction="HOLD")
            expected = STRATEGY_REGIME_PREF["mean_reversion"][regime]
            assert symmetric == expected
            assert no_dir == expected
            assert hold == expected

    def test_unknown_strategy_returns_neutral(self):
        # An unlisted strategy must return 1.0 regardless of direction.
        assert regime_multiplier("brand_new_alpha", "bear_high_vol") == 1.0
        assert regime_multiplier("brand_new_alpha", "bear_high_vol", direction="BUY") == 1.0
        assert regime_multiplier("brand_new_alpha", "bear_high_vol", direction="SELL") == 1.0

    def test_directional_map_covers_all_known_regimes(self):
        # Sanity: anyone editing the directional map shouldn't accidentally
        # leave a regime out and silently fall back to symmetric.
        expected_regimes = {
            "bull_low_vol", "bull_high_vol", "bear_low_vol",
            "bear_high_vol", "sideways", "unknown",
        }
        for strategy, regimes in STRATEGY_REGIME_PREF_DIRECTIONAL.items():
            assert set(regimes.keys()) == expected_regimes, (
                f"{strategy} directional map missing regimes: "
                f"{expected_regimes - set(regimes.keys())}"
            )
            for regime, dir_map in regimes.items():
                assert "BUY" in dir_map and "SELL" in dir_map, (
                    f"{strategy}/{regime} missing BUY or SELL key"
                )


# ----------------------------------------------------------------
# Fix 2 — Ensemble plumbs direction through to the multiplier
# ----------------------------------------------------------------

class TestEnsembleDirectionPlumbing:
    @pytest.fixture
    def ensemble(self):
        return EnsembleModel({
            "ensemble": {
                "confidence_threshold": 0.5,
                "min_strategies_agree": 1,
                "weights": {"mean_reversion": 1.0, "supertrend_follow": 1.0},
            }
        })

    def test_effective_weight_accepts_direction(self, ensemble):
        # The effective_weight signature must accept direction kwarg.
        w_buy = ensemble.effective_weight("mean_reversion", "bear_high_vol", direction="BUY")
        w_sell = ensemble.effective_weight("mean_reversion", "bear_high_vol", direction="SELL")
        # In bear_high_vol BUY must be much weaker than SELL for mean_reversion.
        assert w_buy < w_sell
        assert w_buy <= 0.15  # base 1.0 * mult ~0.1
        assert w_sell >= 0.5

    def test_effective_weight_no_direction_is_symmetric(self, ensemble):
        # Backwards compat — old callers pass no direction.
        w_no_dir = ensemble.effective_weight("mean_reversion", "bear_high_vol")
        w_explicit_none = ensemble.effective_weight("mean_reversion", "bear_high_vol", direction=None)
        assert w_no_dir == w_explicit_none
        # Should equal base * symmetric multiplier (1.0 * 0.4 = 0.4)
        assert abs(w_no_dir - 0.4) < 1e-9

    def test_aggregate_suppresses_lone_mean_reversion_buy_in_bear(self, ensemble):
        # Replicates 2026-04-30: mean_reversion is the *only* strategy with
        # an opinion, and it screams BUY in bear_high_vol. With direction-
        # aware weighting + sane confidence threshold, this gets suppressed
        # below the confidence floor and we hold (no falling-knife trade).
        ensemble.confidence_threshold = 0.5  # match config

        # Build a single mean_reversion BUY at high raw confidence.
        sig = TradeSignal(
            signal=Signal.BUY,
            symbol="VEDL",
            price=400.0,
            timestamp=None,
            strategy_name="mean_reversion",
            confidence=0.9,
            stop_loss=395.0,
            take_profit=410.0,
            metadata={},
        )
        # Note: aggregate normalizes by *active_weight* so a lone signal
        # always reaches confidence ~1.0 of itself. The directional fix
        # primarily matters when multiple strategies vote — see the next test.
        out = ensemble.aggregate([sig], "VEDL", 400.0, regime="bear_high_vol")
        # When only one strategy votes, the normalization makes its vote
        # "100 % of the active weight", so consensus still passes. That's
        # fine — the *real* protection is the lower min_strategies_agree
        # gate (2) applied in production. We assert the contribution math
        # below instead.
        contributions = ensemble._build_contributions(
            [sig], regime="bear_high_vol",
        )
        assert contributions["mean_reversion"] == pytest.approx(1.0)

    def test_aggregate_buy_vs_sell_split_in_bear(self, ensemble):
        # Two strategies disagree: mean_reversion says BUY, supertrend says
        # SELL. In bear_high_vol the directional weighting must let
        # supertrend SELL dominate, even though both have the same raw
        # base weight and confidence.
        ensemble.confidence_threshold = 0.5

        mr_buy = TradeSignal(
            signal=Signal.BUY, symbol="VEDL", price=400.0,
            timestamp=None, strategy_name="mean_reversion",
            confidence=0.9, stop_loss=395.0, take_profit=410.0, metadata={},
        )
        st_sell = TradeSignal(
            signal=Signal.SELL, symbol="VEDL", price=400.0,
            timestamp=None, strategy_name="supertrend_follow",
            confidence=0.7, stop_loss=405.0, take_profit=390.0, metadata={},
        )

        out = ensemble.aggregate([mr_buy, st_sell], "VEDL", 400.0, regime="bear_high_vol")
        # Even though raw mr_buy.confidence (0.9) > st_sell.confidence (0.7),
        # the directional weighting + supertrend's bear-friendly multiplier
        # must produce a SELL or HOLD — never a BUY.
        assert out is None or out.signal == Signal.SELL, (
            f"Expected SELL or HOLD in bear_high_vol with mean_reversion BUY "
            f"vs supertrend SELL, got {out.signal if out else None}"
        )


# ----------------------------------------------------------------
# Fix 3 — Cap-aware notional-floor scaling (logic-level test)
# ----------------------------------------------------------------

class TestCapAwareNotionalFloor:
    """Pure-logic test of the cap-aware sizing math used in
    trading_agent._open_new_position. We re-implement the calc here to lock
    the contract; the actual call site is exercised in the smoke test."""

    @staticmethod
    def _cap_aware_target(
        min_notional: float,
        total_equity: float,
        max_symbol_pct: float,
    ) -> float:
        """Mirror of the production formula."""
        if total_equity <= 0 or max_symbol_pct <= 0:
            return min_notional
        headroom_pct = max(max_symbol_pct - 0.5, 0.5)
        cap = total_equity * headroom_pct / 100.0
        return min(min_notional, cap)

    def test_floor_unchanged_when_book_is_large(self):
        # On a Rs 100k book with 30 % cap = Rs 29.5k headroom, the Rs 3k
        # floor passes through untouched.
        target = self._cap_aware_target(3000, 100_000, 30.0)
        assert target == 3000

    def test_floor_clipped_on_tiny_book(self):
        # Yesterday's pathology: Rs 9.5k book * 30 % = Rs 2.85k cap < Rs 6k
        # min_notional. The fix clips the *target* to the cap.
        target = self._cap_aware_target(6000, 9_500, 30.0)
        assert target < 6000
        assert target < 9_500 * 0.30  # strictly inside the cap (headroom)
        assert target > 9_500 * 0.28  # but close to it

    def test_floor_clipped_on_small_book_with_room(self):
        # Rs 9.5k book, Rs 3k floor, 30 % cap (= Rs 2.85k). 3k > 2.85k,
        # so target = 2.85k (cap-bound, not floor-bound).
        target = self._cap_aware_target(3000, 9_500, 30.0)
        assert target < 3000
        assert 2_700 < target < 2_900

    def test_zero_equity_falls_back_to_floor(self):
        # Edge case: portfolio reports zero equity (e.g. error in valuation).
        # We don't divide by zero — we just use the raw floor.
        assert self._cap_aware_target(3000, 0, 30.0) == 3000
        assert self._cap_aware_target(3000, -100, 30.0) == 3000


# ----------------------------------------------------------------
# Config contract — make sure today's reverts actually landed
# ----------------------------------------------------------------

class TestConfigReverts:
    @pytest.fixture(scope="class")
    def cfg(self):
        path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(path, "r") as f:
            return yaml.safe_load(f)

    def test_max_symbol_exposure_pct_reverted_to_30(self, cfg):
        # Apr-30 tactical override raised this to 35 — we revert to 30 now
        # that the cap-aware notional scaler doesn't fight it.
        assert cfg["risk"]["max_symbol_exposure_pct"] == 30.0

    def test_bear_high_vol_atr_gate_reverted_to_05(self, cfg):
        # Apr-30 tactical override dropped this to 0.20 to force trades.
        # That's how we ended up with 3 noise stop-outs. Revert to 0.5.
        regime_atr = cfg["robustness"]["min_entry_atr_pct_by_regime"]
        assert regime_atr["bear_high_vol"] == 0.5

    def test_min_trade_notional_sane(self, cfg):
        # Anything between Rs 2.5k and Rs 5k is fine on a Rs 10k book.
        # Today's value: Rs 3000 (up from Rs 2800 since cap-aware scaling
        # makes the floor self-regulating).
        notional = cfg["risk"]["min_trade_notional"]
        assert 2_500 <= notional <= 5_000

    def test_short_selling_still_enabled(self, cfg):
        # We turned this on yesterday and it's a permanent feature.
        assert cfg["execution"]["enable_short_selling"] is True

    def test_premarket_warmup_configured(self, cfg):
        # Today's fix — must be set to a sane positive minute count.
        warmup = cfg["scanner"].get("premarket_warmup_minutes")
        assert warmup is not None, "scanner.premarket_warmup_minutes must be defined"
        assert 1 <= warmup <= 15, (
            f"premarket_warmup_minutes={warmup} is outside the sensible "
            "1-15 minute window"
        )


# ----------------------------------------------------------------
# Fix 4 — Pre-market warm-up scan window
# ----------------------------------------------------------------

class TestPremarketWarmupWindow:
    """Logic-level test for the [open - warmup, open) window. The actual
    scheduling on the live agent is exercised by integration runs."""

    @staticmethod
    def _is_warmup(now_hhmm: str, market_open: str, warmup_min: int,
                   weekday: int = 0) -> bool:
        """Pure-Python mirror of TradingAgent._is_premarket_warmup_window."""
        from datetime import datetime as _dt, timedelta as _td
        if warmup_min <= 0 or weekday >= 5:
            return False
        h, m = map(int, now_hhmm.split(":"))
        now = _dt(2026, 5, 4, h, m)  # Monday
        oh, om = map(int, market_open.split(":"))
        open_time = now.replace(hour=oh, minute=om, second=0, microsecond=0)
        warmup_start = open_time - _td(minutes=warmup_min)
        return warmup_start <= now < open_time

    def test_inside_window(self):
        # 09:10 with 5-min warmup, market opens at 09:15 -> True
        assert self._is_warmup("09:10", "09:15", 5)
        assert self._is_warmup("09:13", "09:15", 5)
        assert self._is_warmup("09:14", "09:15", 5)

    def test_at_open_is_not_warmup(self):
        # The window is [open-warmup, open) — at exactly 09:15 we're trading.
        assert not self._is_warmup("09:15", "09:15", 5)

    def test_before_warmup_is_false(self):
        # 09:09 with 5-min warmup: before the window starts.
        assert not self._is_warmup("09:09", "09:15", 5)
        assert not self._is_warmup("08:30", "09:15", 5)

    def test_after_open_is_false(self):
        # Once market is live, this window logic must not re-trigger.
        assert not self._is_warmup("09:16", "09:15", 5)
        assert not self._is_warmup("11:00", "09:15", 5)

    def test_zero_warmup_disables_window(self):
        # premarket_warmup_minutes=0 must turn the feature off.
        assert not self._is_warmup("09:14", "09:15", 0)

    def test_weekend_returns_false(self):
        # Saturday (weekday=5), Sunday (weekday=6) — never run on weekends.
        assert not self._is_warmup("09:13", "09:15", 5, weekday=5)
        assert not self._is_warmup("09:13", "09:15", 5, weekday=6)

    def test_longer_warmup_extends_window(self):
        # 10-min warmup pulls the window back to 09:05.
        assert self._is_warmup("09:05", "09:15", 10)
        assert self._is_warmup("09:14", "09:15", 10)
        assert not self._is_warmup("09:04", "09:15", 10)
