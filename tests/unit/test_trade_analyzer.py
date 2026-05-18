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

    def test_scratch_trade_not_counted_as_loss_per_strategy(self, analyzer):
        """A pnl==0 (flat) close must not inflate the loss count."""
        rec = _make_trade_record("rsi_momentum", pnl=0.0)
        stats = analyzer.record_trade(rec)
        assert stats["total_trades"] == 1
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["scratches"] == 1

    def test_scratch_trade_not_counted_as_loss_per_regime(self, analyzer):
        """
        2026-05-18 regression guard: the P2 logic-edges patch added scratch
        handling to the per-strategy block but MISSED the per-regime block,
        so pnl==0 in a given regime would be counted as a loss in the
        regime scorecard while not in the per-strategy scorecard. Reproduce
        the bug, then assert it's fixed.

        2026-05-18 (Audit Issue #4) revision: the win_rate denominator was
        also fixed in the same audit pass to use DECISIVE trades
        (wins + losses), not total_trades. So 1 win + 1 scratch is now
        a clean 100% win-rate on decisive trades, not 50% diluted.
        """
        rec_win = _make_trade_record("rsi_momentum", pnl=40.0)
        rec_win.regime = "bear_high_vol"
        rec_flat = _make_trade_record("rsi_momentum", pnl=0.0)
        rec_flat.regime = "bear_high_vol"

        analyzer.record_trade(rec_win)
        analyzer.record_trade(rec_flat)

        r_stats = analyzer._regime_stats[("rsi_momentum", "bear_high_vol")]
        assert r_stats["total_trades"] == 2
        assert r_stats["wins"] == 1
        assert r_stats["losses"] == 0
        assert r_stats["scratches"] == 1
        # Decisive-trade win_rate: 1 win / (1 win + 0 losses) = 1.0.
        # Scratches do NOT dilute the WR seen by the regime weighter.
        assert r_stats["win_rate"] == pytest.approx(1.0)

    def test_scratch_trade_per_regime_does_not_distort_loss_branch(self, analyzer):
        """Negative pnl must still be counted as a loss in the regime branch."""
        rec_loss = _make_trade_record("rsi_momentum", pnl=-30.0)
        rec_loss.regime = "bull_low_vol"
        rec_flat = _make_trade_record("rsi_momentum", pnl=0.0)
        rec_flat.regime = "bull_low_vol"

        analyzer.record_trade(rec_loss)
        analyzer.record_trade(rec_flat)

        r_stats = analyzer._regime_stats[("rsi_momentum", "bull_low_vol")]
        assert r_stats["total_trades"] == 2
        assert r_stats["wins"] == 0
        assert r_stats["losses"] == 1
        assert r_stats["scratches"] == 1
        # Decisive-trade win_rate: 0 wins / (0 wins + 1 loss) = 0.0.
        # The scratch isn't in the denominator so the WR isn't artificially
        # nudged toward 50/50 just because of a flat exit.
        assert r_stats["win_rate"] == pytest.approx(0.0)

    def test_win_rate_uses_decisive_denominator_per_strategy(self, analyzer):
        """2026-05-18 (Audit Issue #4): the per-strategy win_rate must use
        the DECISIVE-trade denominator (wins + losses), not total_trades.
        Without this fix, a 50% true-edge strategy with a meaningful share
        of flat exits showed a depressed WR to the ensemble weight tuner,
        causing it to demote a strategy that was actually pulling its
        weight."""
        # Pattern: 5 wins, 3 losses, 4 scratches.
        for pnl in [50.0, 60.0, 70.0, 40.0, 30.0]:
            analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=pnl))
        for pnl in [-20.0, -15.0, -25.0]:
            analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=pnl))
        for _ in range(4):
            analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=0.0))

        stats = analyzer.get_scorecard()["rsi_momentum"]
        assert stats["total_trades"] == 12
        assert stats["wins"] == 5
        assert stats["losses"] == 3
        assert stats["scratches"] == 4
        # OLD (broken) formula: 5/12 = 0.4167.
        # NEW (fixed)  formula: 5/(5+3) = 0.625 -- the true decisive WR.
        assert stats["win_rate"] == pytest.approx(0.625, abs=1e-6), (
            "Audit Issue #4 regression: win_rate must use decisive denominator "
            "(wins + losses); scratches must NOT dilute the WR seen by the "
            "ensemble weight tuner."
        )

    def test_win_rate_decisive_denominator_handles_all_scratches(self, analyzer):
        """If every trade is a scratch, the decisive denominator is 0 and the
        formula must fall back to 0.0 (NOT divide by zero) -- the strategy
        has produced no decisive evidence either way."""
        for _ in range(3):
            analyzer.record_trade(_make_trade_record("rsi_momentum", pnl=0.0))
        stats = analyzer.get_scorecard()["rsi_momentum"]
        assert stats["total_trades"] == 3
        assert stats["wins"] == 0
        assert stats["losses"] == 0
        assert stats["scratches"] == 3
        assert stats["win_rate"] == pytest.approx(0.0)

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


class TestAccumulatorRehydrate:
    """The 2026-05-13 bug: ``_gross_wins`` / ``_gross_losses`` / ``_pnl_list``
    are stored in-memory only. After daemon restart, the first trade
    writes PF=0 (loss-only) or PF=inf (win-only) -- corrupting auto-suppress.

    Fix: ``_rehydrate_internal_accumulators`` replays the trades table at
    init time and rebuilds these from scratch, then re-saves corrected
    PF/Sharpe to the DB.
    """

    @staticmethod
    def _seed_trade(db, strategy, pnl, idx=0, regime="bull_low_vol"):
        db.store_trade({
            "symbol": f"SYM{idx}",
            "side": "BUY",
            "entry_price": 100.0,
            "exit_price": 100.0 + pnl,
            "quantity": 1,
            "entry_time": f"2026-05-01T09:{30 + idx:02d}:00+05:30",
            "exit_time":  f"2026-05-01T10:{30 + idx:02d}:00+05:30",
            "pnl": pnl,
            "pnl_pct": pnl / 100.0,
            "strategy": strategy,
            "exit_reason": "test",
            "commission": 0.0,
            "slippage": 0.0,
            "market_context": "",
        })

    def test_rebuilds_pf_after_restart_for_profitable_strategy(self, config, db):
        # Simulate a strategy with 3 wins (+100, +50, +30) and 2 losses (-40, -20)
        # Gross wins = 180, gross losses = 60 -> PF = 3.0
        a1 = TradeAnalyzer(config, db)
        for i, pnl in enumerate([100.0, -40.0, 50.0, -20.0, 30.0]):
            a1.record_trade(_make_trade_record("supertrend_follow", pnl=pnl, hour=10 + i))
            self._seed_trade(db, "supertrend_follow", pnl, idx=i)
        pf_pre_restart = a1.get_scorecard()["supertrend_follow"]["profit_factor"]
        assert pf_pre_restart == pytest.approx(3.0, abs=0.01)

        # Simulate a daemon restart by building a new analyzer over the same
        # DB. WITHOUT the fix, the next record_trade would compute PF
        # incorrectly because _gross_wins/_gross_losses were lost.
        a2 = TradeAnalyzer(config, db)
        pf_after_rehydrate = a2.get_scorecard()["supertrend_follow"]["profit_factor"]
        assert pf_after_rehydrate == pytest.approx(3.0, abs=0.01), (
            f"PF should rehydrate from trades history, got {pf_after_rehydrate}"
        )

    def test_loss_only_strategy_does_not_corrupt_to_zero(self, config, db):
        """Reproduces the 2026-05-13 HCLTECH-after-restart pathology:
        if accumulators reset and the next trade is a loss, the naive
        ``_update_profit_factor`` writes PF=0 -- which trips auto-suppress
        (PF < 0.7). The rehydrate must restore prior gross_wins so the
        loss doesn't single-handedly zero out the strategy."""
        a1 = TradeAnalyzer(config, db)
        # 3 wins then 2 losses, profitable overall
        for i, pnl in enumerate([200.0, 150.0, 100.0]):
            a1.record_trade(_make_trade_record("supertrend_follow", pnl=pnl, hour=10 + i))
            self._seed_trade(db, "supertrend_follow", pnl, idx=i)
        pf_initial = a1.get_scorecard()["supertrend_follow"]["profit_factor"]
        # 3 wins, no losses -> PF = inf, but auto-suppress check uses
        # `pf < 0.7` so inf is safe.
        assert pf_initial == float("inf")

        # Restart
        a2 = TradeAnalyzer(config, db)
        # Now record one more loss. WITHOUT the fix the gross_wins
        # accumulator starts at 0 (lost during restart) and adding a
        # -50 PNL yields gross_losses=50, gross_wins=0 -> PF=0.
        # WITH the fix, gross_wins was rehydrated to 450 (200+150+100),
        # so PF stays high.
        a2.record_trade(_make_trade_record("supertrend_follow", pnl=-50.0, hour=14))
        pf_after_loss = a2.get_scorecard()["supertrend_follow"]["profit_factor"]
        # 450 / 50 = 9.0
        assert pf_after_loss == pytest.approx(9.0, abs=0.01), (
            f"After restart+loss, PF should be 9.0 (450 wins / 50 losses); "
            f"got {pf_after_loss}. If 0.0, the gross_wins accumulator was "
            f"NOT rehydrated -- the original bug."
        )

    def test_rehydrate_is_idempotent(self, config, db):
        a1 = TradeAnalyzer(config, db)
        for i, pnl in enumerate([50.0, -20.0, 30.0]):
            a1.record_trade(_make_trade_record("rsi_momentum", pnl=pnl, hour=10 + i))
            self._seed_trade(db, "rsi_momentum", pnl, idx=i)

        # Multiple restarts must converge on the same PF value.
        pfs = []
        for _ in range(3):
            a = TradeAnalyzer(config, db)
            pfs.append(a.get_scorecard()["rsi_momentum"]["profit_factor"])
        assert len(set([round(x, 3) for x in pfs])) == 1, (
            f"Rehydrate is not idempotent: {pfs}"
        )

    def test_phantom_row_is_removed(self, config, db):
        # Plant a phantom: a strategy_scores row with total_trades > 0 but
        # NO actual trades in the trades table for that strategy. Mimics
        # the 2026-05-06 'ensemble' leftover.
        db.save_strategy_score("ghost_strategy", {
            "total_trades": 17, "wins": 6, "losses": 11,
            "total_pnl": -143.46, "avg_pnl": -8.44,
            "win_rate": 0.353, "profit_factor": 0.0, "sharpe": -21.7,
            "learned_weight": 0.0,
        })
        # Real trades for a real strategy
        self._seed_trade(db, "rsi_momentum", 50.0, idx=0)

        a = TradeAnalyzer(config, db)
        sc = a.get_scorecard()
        assert "ghost_strategy" not in sc, (
            "Phantom strategy row should have been removed by rehydrate"
        )
        # The valid strategy is preserved
        assert "rsi_momentum" in sc

        # The phantom is also removed from the DB so subsequent restarts
        # don't have to clean up again.
        assert "ghost_strategy" not in db.load_strategy_scores()

    def test_empty_trades_table_is_noop(self, config, db):
        """If trades table is empty (fresh DB), rehydrate must not crash
        or remove anything; existing strategy_scores rows stay as-is."""
        db.save_strategy_score("rsi_momentum", {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0,
            "profit_factor": 0.0, "sharpe": 0.0, "learned_weight": 1.0,
        })
        a = TradeAnalyzer(config, db)
        # Did not raise. Did not delete (because total_trades == 0).
        assert "rsi_momentum" in db.load_strategy_scores()

    def test_phantom_row_with_zero_trades_is_preserved(self, config, db):
        """A row with total_trades=0 looks identical to a 'just-initialised'
        strategy that hasn't traded yet. We must NOT delete those --
        only rows with positive total_trades but no matching trade
        history are phantoms."""
        db.save_strategy_score("freshly_added_strategy", {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0,
            "profit_factor": 0.0, "sharpe": 0.0, "learned_weight": 1.0,
        })
        self._seed_trade(db, "rsi_momentum", 50.0, idx=0)
        a = TradeAnalyzer(config, db)
        assert "freshly_added_strategy" in a.get_scorecard()


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
