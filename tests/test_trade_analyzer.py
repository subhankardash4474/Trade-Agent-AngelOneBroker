"""
Unit tests for the TradeAnalyzer self-learning module.
Tests all three layers: Strategy Scorecard, Adaptive Weight Tuner, Pattern Memory.
"""

import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import pytz

from core.database import Database
from core.trade_analyzer import TradeAnalyzer

IST = pytz.timezone("Asia/Kolkata")


def _make_trade_record(strategy: str, pnl: float, pnl_pct: float = None,
                       symbol: str = "TESTSTOCK", hour: int = 10):
    """Create a mock trade record resembling portfolio.TradeRecord."""
    if pnl_pct is None:
        pnl_pct = pnl / 100.0
    rec = MagicMock()
    rec.strategy = strategy
    rec.symbol = symbol
    rec.pnl = pnl
    rec.pnl_pct = pnl_pct
    rec.entry_price = 100.0
    rec.exit_price = 100.0 + pnl
    rec.quantity = 1
    rec.entry_time = datetime(2026, 3, 30, hour, 15, 0, tzinfo=IST)
    rec.exit_time = datetime(2026, 3, 30, hour + 1, 0, 0, tzinfo=IST)
    rec.exit_reason = "signal"
    rec.rsi = None
    rec.atr_pct = None
    rec.volume_ratio = None
    rec.market_trend = None
    return rec


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    os.unlink(path)


@pytest.fixture
def config():
    return {
        "learning": {
            "enabled": True,
            "min_trades_to_learn": 5,
            "weight_update_interval": 5,
            "decay_factor": 0.95,
            "pattern_lookback": 50,
            "max_weight": 5.0,
            "min_weight": 0.1,
            "pattern_similarity_threshold": 0.75,
        },
        "ensemble": {
            "weights": {
                "rsi_momentum": 1.0,
                "mean_reversion": 0.8,
                "supertrend_follow": 1.5,
            }
        }
    }


@pytest.fixture
def analyzer(config, db):
    return TradeAnalyzer(config, db)


class TestStrategyScorecard:
    def test_record_single_win(self, analyzer):
        rec = _make_trade_record("rsi_momentum", pnl=50.0)
        stats = analyzer.record_trade(rec)
        assert stats["total_trades"] == 1
        assert stats["wins"] == 1
        assert stats["losses"] == 0
        assert stats["total_pnl"] == 50.0
        assert stats["win_rate"] == 1.0

    def test_record_single_loss(self, analyzer):
        rec = _make_trade_record("rsi_momentum", pnl=-30.0)
        stats = analyzer.record_trade(rec)
        assert stats["total_trades"] == 1
        assert stats["wins"] == 0
        assert stats["losses"] == 1
        assert stats["total_pnl"] == -30.0

    def test_multiple_trades_update_stats(self, analyzer):
        for pnl in [50, -20, 30, -10, 40]:
            stats = analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=pnl))

        assert stats["total_trades"] == 5
        assert stats["wins"] == 3
        assert stats["losses"] == 2
        assert stats["total_pnl"] == 90.0
        assert stats["win_rate"] == pytest.approx(0.6, abs=0.01)

    def test_profit_factor(self, analyzer):
        for pnl in [100, -50, 80, -30]:
            stats = analyzer.record_trade(_make_trade_record("supertrend_follow", pnl=pnl))

        assert stats["profit_factor"] == pytest.approx(180.0 / 80.0, abs=0.01)

    def test_scorecard_across_strategies(self, analyzer):
        analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=50.0))
        analyzer.record_trade(_make_trade_record("mean_reversion", pnl=-20.0))
        scorecard = analyzer.get_scorecard()
        assert "rsi_momentum" in scorecard
        assert "mean_reversion" in scorecard
        assert scorecard["rsi_momentum"]["total_pnl"] == 50.0
        assert scorecard["mean_reversion"]["total_pnl"] == -20.0


class TestAdaptiveWeightTuner:
    def test_weights_not_updated_below_min_trades(self, analyzer):
        for i in range(3):
            analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=10.0))
        weights = analyzer.get_learned_weights()
        assert len(weights) == 0

    def test_weights_updated_after_threshold(self, analyzer):
        """After min_trades (5) and update_interval (5) trades, weights should be recalculated."""
        for i in range(5):
            analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=20.0, hour=10 + i % 3))
        weights = analyzer.get_learned_weights()
        assert "rsi_momentum" in weights
        assert weights["rsi_momentum"] > 1.0

    def test_losing_strategy_gets_lower_weight(self, analyzer):
        for i in range(5):
            analyzer.record_trade(_make_trade_record("mean_reversion", pnl=-15.0, hour=10 + i % 3))
        weights = analyzer.get_learned_weights()
        assert "mean_reversion" in weights
        assert weights["mean_reversion"] < 0.8

    def test_weights_clamped(self, analyzer):
        for i in range(5):
            analyzer.record_trade(_make_trade_record("supertrend_follow", pnl=500.0, hour=10 + i % 3))
        weights = analyzer.get_learned_weights()
        assert weights.get("supertrend_follow", 0) <= 5.0

    def test_weights_persist_across_restart(self, config, db):
        a1 = TradeAnalyzer(config, db)
        for i in range(5):
            a1.record_trade(_make_trade_record("rsi_momentum", pnl=25.0, hour=10 + i % 3))
        w1 = a1.get_learned_weights()

        a2 = TradeAnalyzer(config, db)
        w2 = a2.get_learned_weights()
        assert w1.get("rsi_momentum") == w2.get("rsi_momentum")


class TestPatternMemory:
    def test_patterns_stored(self, analyzer, db):
        analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=50.0))
        patterns = db.load_trade_patterns()
        assert len(patterns) == 1
        assert patterns[0]["outcome"] == "GOOD"

    def test_bad_pattern_stored(self, analyzer, db):
        analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=-30.0))
        patterns = db.load_trade_patterns()
        assert len(patterns) == 1
        assert patterns[0]["outcome"] == "BAD"

    def test_evaluate_setup_insufficient_data(self, analyzer):
        adj, reason = analyzer.evaluate_setup("rsi_momentum", hour_of_day=10)
        assert adj == 0.0
        assert "insufficient" in reason

    def test_evaluate_setup_good_pattern(self, analyzer):
        for i in range(10):
            rec = _make_trade_record("rsi_momentum", pnl=30.0, hour=10)
            analyzer.record_trade(rec)

        adj, reason = analyzer.evaluate_setup("rsi_momentum", hour_of_day=10, day_of_week=0)
        assert adj >= 0, f"Expected non-negative adjustment, got {adj}: {reason}"

    def test_evaluate_setup_bad_pattern(self, analyzer):
        for i in range(10):
            rec = _make_trade_record("rsi_momentum", pnl=-20.0, hour=14)
            analyzer.record_trade(rec)

        adj, reason = analyzer.evaluate_setup("rsi_momentum", hour_of_day=14, day_of_week=0)
        assert adj <= 0, f"Expected non-positive adjustment, got {adj}: {reason}"

    def test_similarity_computation(self):
        pattern = {
            "hour_of_day": 10,
            "day_of_week": 1,
            "rsi": 35.0,
            "atr_pct": 2.5,
            "volume_ratio": 1.8,
            "market_trend": 1,
        }
        sim = TradeAnalyzer._compute_similarity(
            pattern, hour_of_day=10, day_of_week=1, rsi=35.0,
            atr_pct=2.5, volume_ratio=1.8, market_trend=1,
        )
        assert sim == pytest.approx(1.0, abs=0.01)

    def test_similarity_partial_match(self):
        pattern = {
            "hour_of_day": 10,
            "day_of_week": 1,
            "rsi": 45.0,
            "atr_pct": None,
            "volume_ratio": None,
            "market_trend": None,
        }
        sim = TradeAnalyzer._compute_similarity(
            pattern, hour_of_day=11, day_of_week=2, rsi=40.0,
        )
        assert 0 < sim < 1.0


class TestDisabledLearning:
    def test_disabled_returns_empty(self, db):
        config = {"learning": {"enabled": False}, "ensemble": {"weights": {}}}
        analyzer = TradeAnalyzer(config, db)
        rec = _make_trade_record("rsi_momentum", pnl=50.0)
        stats = analyzer.record_trade(rec)
        assert stats == {}

    def test_disabled_evaluate_returns_zero(self, db):
        config = {"learning": {"enabled": False}, "ensemble": {"weights": {}}}
        analyzer = TradeAnalyzer(config, db)
        adj, reason = analyzer.evaluate_setup("rsi_momentum")
        assert adj == 0.0
        assert reason == "learning_disabled"
