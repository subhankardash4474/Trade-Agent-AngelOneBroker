"""
Unit tests for the Ensemble Meta-Model.
Tests weighted voting, confidence thresholds, and signal aggregation.
"""

import pandas as pd
import pytest

from core.ensemble import EnsembleModel
from strategies.base_strategy import Signal, TradeSignal


def _make_signal(signal: Signal, strategy: str, confidence: float = 0.8,
                 price: float = 100.0) -> TradeSignal:
    return TradeSignal(
        signal=signal,
        symbol="TEST",
        price=price,
        timestamp=pd.Timestamp.now(),
        strategy_name=strategy,
        confidence=confidence,
        stop_loss=price * 0.985 if signal == Signal.BUY else None,
        take_profit=price * 1.03 if signal == Signal.BUY else None,
    )


@pytest.fixture
def ensemble():
    return EnsembleModel({"ensemble": {"confidence_threshold": 0.7, "min_strategies_agree": 2}})


class TestEnsembleAggregation:
    def test_buy_consensus(self, ensemble):
        signals = [
            _make_signal(Signal.BUY, "xgboost_classifier", 0.9),
            _make_signal(Signal.BUY, "supertrend_follow", 0.85),
            _make_signal(Signal.BUY, "rsi_momentum", 0.8),
        ]
        result = ensemble.aggregate(signals, "TEST", 100.0)
        assert result is not None
        assert result.signal == Signal.BUY

    def test_sell_consensus(self, ensemble):
        signals = [
            _make_signal(Signal.SELL, "xgboost_classifier", 0.9),
            _make_signal(Signal.SELL, "supertrend_follow", 0.85),
            _make_signal(Signal.SELL, "rsi_momentum", 0.8),
        ]
        result = ensemble.aggregate(signals, "TEST", 100.0)
        assert result is not None
        assert result.signal == Signal.SELL

    def test_no_consensus_below_threshold(self, ensemble):
        signals = [
            _make_signal(Signal.BUY, "rsi_momentum", 0.3),
            _make_signal(Signal.HOLD, "mean_reversion", 0.0),
            _make_signal(Signal.HOLD, "supertrend_follow", 0.0),
        ]
        result = ensemble.aggregate(signals, "TEST", 100.0)
        assert result is None  # below threshold

    def test_conflicting_signals_no_action(self, ensemble):
        signals = [
            _make_signal(Signal.BUY, "rsi_momentum", 0.7),
            _make_signal(Signal.SELL, "supertrend_follow", 0.7),
            _make_signal(Signal.HOLD, "mean_reversion", 0.0),
        ]
        result = ensemble.aggregate(signals, "TEST", 100.0)
        # Neither side reaches threshold when split
        assert result is None

    def test_min_strategies_requirement(self, ensemble):
        # Only 1 strategy agrees — below min_strategies_agree=2
        signals = [
            _make_signal(Signal.BUY, "supertrend_follow", 0.95),
            _make_signal(Signal.HOLD, "rsi_momentum", 0.0),
            _make_signal(Signal.HOLD, "mean_reversion", 0.0),
        ]
        result = ensemble.aggregate(signals, "TEST", 100.0)
        assert result is None

    def test_empty_signals(self, ensemble):
        result = ensemble.aggregate([], "TEST", 100.0)
        assert result is None

    def test_result_has_metadata(self, ensemble):
        signals = [
            _make_signal(Signal.BUY, "xgboost_classifier", 0.85),
            _make_signal(Signal.BUY, "supertrend_follow", 0.75),
        ]
        result = ensemble.aggregate(signals, "TEST", 100.0)
        assert result is not None
        assert "buy_strategies" in result.metadata
        assert "buy_confidence" in result.metadata
