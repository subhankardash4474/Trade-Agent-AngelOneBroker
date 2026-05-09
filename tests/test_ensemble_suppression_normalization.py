"""Tests for the ensemble normalization fix (2026-05-04 part 2).

Bug it fixes:
  The 2026-04-30 direction-aware regime multiplier (e.g. 0.1x for
  mean_reversion BUY in bear_high_vol) was a NO-OP for solo-strategy
  decisions because the ensemble normalized confidence by the same
  multiplier-laden weight that appeared in the score. Numerator and
  denominator both carried the multiplier, so it cancelled.

  Today's evidence (signal_audit_2026-05-04.csv):
    09:19:12  ZENTEC mean_reversion BUY  conf=0.918  bear_high_vol
    09:20:37  ZENTEC mean_reversion BUY  conf=0.886  bear_high_vol
  Both should have been crushed to ~0.09 effective. Both reached the
  sizing layer (only blocked there by cash).

Fix: normalize by the *unsuppressed* weight (base × learned, no regime
multiplier). The multiplier now actually reduces final confidence.

These tests confirm the fix without booting the full agent — they
exercise the EnsembleModel directly with realistic config.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ensemble import EnsembleModel
from strategies.base_strategy import Signal, TradeSignal


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def ensemble_realistic():
    """An ensemble with the production threshold + min_strategies_agree=1
    (matching today's running config), so we exercise the exact scenario
    that broke today.
    """
    cfg = {
        "ensemble": {
            "confidence_threshold": 0.55,
            "min_strategies_agree": 1,
            "min_dynamic_threshold": 0.45,
            "max_dynamic_threshold": 0.75,
        }
    }
    return EnsembleModel(cfg)


def _mk_signal(strategy: str, sig: Signal, conf: float, symbol: str = "TEST", price: float = 100.0):
    return TradeSignal(
        signal=sig,
        symbol=symbol,
        price=price,
        timestamp=datetime.now(timezone.utc),
        strategy_name=strategy,
        confidence=conf,
    )


# ─────────────────────────────────────────────────────────────
# The headline bug — ZENTEC replay
# ─────────────────────────────────────────────────────────────


class TestZentecReplay:
    """Replay the actual ZENTEC trades from signal_audit_2026-05-04.csv."""

    def test_zentec_918_buy_in_bear_blocked(self, ensemble_realistic):
        """09:19:12 — mean_reversion BUY conf=0.918, bear_high_vol.
        With multiplier 0.1, ensemble confidence should drop to ~0.092 — well
        below the 0.55 threshold. Result: ensemble must HOLD (return None).
        """
        signal = _mk_signal("mean_reversion", Signal.BUY, 0.918, symbol="ZENTEC", price=1496.20)
        result = ensemble_realistic.aggregate(
            [signal], symbol="ZENTEC", current_price=1496.20, regime="bear_high_vol"
        )
        assert result is None, (
            "mean_reversion BUY at 0.918 in bear_high_vol must NOT pass — "
            f"got {result.signal if result else None}@{result.confidence if result else None:.3f}"
        )

    def test_zentec_886_buy_in_bear_blocked(self, ensemble_realistic):
        """09:20:37 — mean_reversion BUY conf=0.886, bear_high_vol."""
        signal = _mk_signal("mean_reversion", Signal.BUY, 0.886, symbol="ZENTEC", price=1472.00)
        result = ensemble_realistic.aggregate(
            [signal], symbol="ZENTEC", current_price=1472.00, regime="bear_high_vol"
        )
        assert result is None

    def test_zentec_replay_yields_low_confidence(self, ensemble_realistic):
        """Sanity: the calculated buy_confidence should be < 0.20, not the
        raw 0.886. We test by mocking 1 below-threshold case to read the log."""
        # We don't have a public way to peek at buy_confidence when result is
        # None, but we can verify by lowering the threshold and confirming the
        # signal would emerge at < 0.20.
        cfg = {"ensemble": {"confidence_threshold": 0.05, "min_strategies_agree": 1}}
        ens = EnsembleModel(cfg)
        signal = _mk_signal("mean_reversion", Signal.BUY, 0.886, symbol="ZENTEC")
        result = ens.aggregate([signal], "ZENTEC", 1472.00, regime="bear_high_vol")
        assert result is not None  # threshold lowered enough to emerge
        assert result.confidence < 0.20, (
            f"Solo mean_reversion BUY in bear_high_vol must yield "
            f"confidence < 0.20 (was {result.confidence:.3f}); "
            f"if this fails the multiplier is being cancelled in normalization."
        )


# ─────────────────────────────────────────────────────────────
# Symmetric direction validation
# ─────────────────────────────────────────────────────────────


class TestDirectionAsymmetry:
    """Same strategy, same regime, same confidence — different direction.
    BUY should be suppressed; SELL should pass through normally."""

    def test_mean_reversion_sell_passes_in_bear(self, ensemble_realistic):
        """SELL the rally in bear is the right trade. Multiplier=0.7 means
        confidence still ~70% of raw. Should pass threshold easily."""
        signal = _mk_signal("mean_reversion", Signal.SELL, 0.886, symbol="ZENTEC")
        result = ensemble_realistic.aggregate(
            [signal], "ZENTEC", 1472.00, regime="bear_high_vol"
        )
        assert result is not None, "mean_reversion SELL in bear must still trade"
        assert result.signal == Signal.SELL
        # 0.886 * 0.7 = 0.620
        assert 0.55 <= result.confidence <= 0.7

    def test_mean_reversion_buy_passes_in_sideways(self, ensemble_realistic):
        """In sideways, multiplier=1.4 (favored). BUY should pass at boosted conf."""
        signal = _mk_signal("mean_reversion", Signal.BUY, 0.886)
        result = ensemble_realistic.aggregate([signal], "TEST", 100.0, regime="sideways")
        assert result is not None
        assert result.signal == Signal.BUY
        # 0.886 * 1.4 = 1.240 (clamped is fine; threshold is 0.55)
        assert result.confidence >= 0.55


# ─────────────────────────────────────────────────────────────
# Backward compatibility — multi-strategy / non-suppressed paths
# ─────────────────────────────────────────────────────────────


class TestBackwardCompat:
    """Confirm the fix doesn't break normal cases."""

    def test_multi_strategy_neutral_regime_emits_at_threshold(self, ensemble_realistic):
        """RSI BUY @ 0.7 + MA-X BUY @ 0.7 in `unknown` regime (multipliers all 1.0)."""
        signals = [
            _mk_signal("rsi_momentum", Signal.BUY, 0.7),
            _mk_signal("moving_average_crossover", Signal.BUY, 0.7),
        ]
        result = ensemble_realistic.aggregate(signals, "TEST", 100.0, regime="unknown")
        assert result is not None
        assert result.signal == Signal.BUY
        # buy_score = 0.7*1.0 + 0.7*1.0 = 1.4
        # norm_weight = 1.0 + 1.0 = 2.0  (base weights, no multiplier)
        # buy_confidence = 0.7
        assert 0.65 <= result.confidence <= 0.75

    def test_solo_strong_signal_in_neutral_regime_passes(self, ensemble_realistic):
        """When multiplier=1.0 (neutral), behavior matches the OLD code."""
        signal = _mk_signal("rsi_momentum", Signal.SELL, 0.85)
        result = ensemble_realistic.aggregate([signal], "TEST", 100.0, regime="unknown")
        assert result is not None
        assert result.signal == Signal.SELL
        # confidence = 0.85 * 1.0 / 1.0 = 0.85
        assert abs(result.confidence - 0.85) < 0.01

    def test_no_signals_returns_none(self, ensemble_realistic):
        assert ensemble_realistic.aggregate([], "TEST", 100.0) is None

    def test_all_hold_returns_none(self, ensemble_realistic):
        signals = [
            _mk_signal("rsi_momentum", Signal.HOLD, 0.5),
            _mk_signal("moving_average_crossover", Signal.HOLD, 0.5),
        ]
        result = ensemble_realistic.aggregate(signals, "TEST", 100.0, regime="unknown")
        assert result is None


# ─────────────────────────────────────────────────────────────
# Mathematical contract — normalization denominator
# ─────────────────────────────────────────────────────────────


class TestNormalizationContract:
    """The denominator must be the unsuppressed weight, not the effective."""

    def test_unsuppressed_weight_excludes_regime_multiplier(self, ensemble_realistic):
        """For mean_reversion in bear_high_vol BUY, base=0.8, multiplier=0.1.
        Effective = 0.08; unsuppressed should be 0.8.
        """
        eff_buy = ensemble_realistic.effective_weight(
            "mean_reversion", regime="bear_high_vol", direction="BUY"
        )
        unsup = ensemble_realistic._unsuppressed_weight(
            "mean_reversion", regime="bear_high_vol"
        )
        assert eff_buy < 0.15, f"effective BUY weight should be ~0.08, got {eff_buy}"
        assert unsup >= 0.7, f"unsuppressed weight should be ~0.8, got {unsup}"
        assert unsup > eff_buy * 5, "unsuppressed must be much larger than suppressed BUY"

    def test_unsuppressed_weight_independent_of_direction(self, ensemble_realistic):
        """Unsuppressed weight only reflects base+learned; direction is irrelevant."""
        # The function doesn't even take a direction parameter.
        unsup = ensemble_realistic._unsuppressed_weight(
            "mean_reversion", regime="bear_high_vol"
        )
        assert unsup > 0


# ─────────────────────────────────────────────────────────────
# Production source guard
# ─────────────────────────────────────────────────────────────


class TestProductionSourceShape:
    def test_aggregate_uses_unsuppressed_weight_for_normalization(self):
        src = (Path(__file__).parent.parent / "core" / "ensemble.py").read_text(encoding="utf-8")
        # The new code must call _unsuppressed_weight inside aggregate.
        assert "unsuppressed_per_signal" in src, (
            "aggregate() must snapshot unsuppressed weights for normalization."
        )
        assert "norm_weight = sum(unsuppressed_per_signal.get" in src, (
            "aggregate() must normalize by unsuppressed weight, not effective."
        )
        # The old buggy form must be gone.
        assert "active_weight = sum(effective_per_signal" not in src, (
            "Old buggy normalization (by effective_weight) must be removed."
        )
