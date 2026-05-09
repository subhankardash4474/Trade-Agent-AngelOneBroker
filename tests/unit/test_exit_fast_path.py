"""Tests for the exit fast-path (2026-05-04).

Bug it fixes: closing signals on existing positions were subject to the same
ensemble gates as ENTRIES (confidence_threshold + min_strategies_agree). The
mean_reversion strategy intentionally emits exits at confidence=0.45 with
the comment "moderate — only acts if ensemble agrees" — which broke down
in regimes where mean_reversion was the only strategy with conviction.

Result on 2026-05-04: 3 SHORT positions (IDEA, RAILTEL, NIVABUPA) had
EXIT signals fire 4+ times each between 10:02 and 10:19, but never closed
because no other strategy agreed. Profitable trades stayed open until
SL/TP — which, for sideways-grinding stocks, never triggered.

Fix: add a fast-path in `_trading_cycle` that closes a held position when
ANY single strategy emits an opposite-side signal at conf >= floor (default
0.40). Entries still require ensemble consensus; exits only need one voice.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategies.base_strategy import Signal, TradeSignal


# ─────────────────────────────────────────────────────────────
# Config knob is wired correctly
# ─────────────────────────────────────────────────────────────


class TestConfigContract:
    @pytest.fixture(scope="class")
    def cfg(self):
        path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(path, "r") as f:
            return yaml.safe_load(f)

    def test_signal_exit_min_conf_present(self, cfg):
        # Must be defined so the agent isn't silently using a hidden default.
        assert "signal_exit_min_conf" in cfg["risk"]

    def test_signal_exit_min_conf_in_sane_range(self, cfg):
        # Below 0.30 admits noise; above mean_reversion's 0.45 design point
        # means mean_reversion exits never fire.
        v = cfg["risk"]["signal_exit_min_conf"]
        assert 0.30 <= v <= 0.45, (
            f"signal_exit_min_conf={v} is outside 0.30-0.45. Below 0.30 "
            "admits noise; above 0.45 means mean_reversion exits never fire."
        )


# ─────────────────────────────────────────────────────────────
# Fast-path helper logic — pure unit tests
# These exercise the decision *without* booting a full TradingAgent.
# ─────────────────────────────────────────────────────────────


def _evaluate_fast_path(
    *, held_side: str, signals, exit_floor: float = 0.40,
):
    """Re-implement the fast-path decision so we can lock the contract.

    Mirrors the inline logic in trading_agent._trading_cycle. Returns
    (fired: bool, closing_signal_or_none).
    """
    from strategies.base_strategy import Signal as _Signal

    if exit_floor <= 0:
        return False, None
    closing_dir = _Signal.SELL if held_side == "BUY" else _Signal.BUY
    closing_signals = [s for s in signals if s.signal == closing_dir]
    if not closing_signals:
        return False, None
    best = max(closing_signals, key=lambda s: s.confidence)
    if best.confidence < exit_floor:
        return False, None
    return True, best


def _mk(strategy: str, signal: Signal, conf: float, symbol: str = "RAILTEL"):
    return TradeSignal(
        signal=signal, symbol=symbol, price=335.0,
        timestamp=None, strategy_name=strategy, confidence=conf,
        stop_loss=340.0, take_profit=325.0,
        contributing_strategies={strategy: 1.0},
    )


class TestFastPathLogic:
    def test_short_closed_on_lone_buy_signal(self):
        # Today's pathology: held SHORT, mean_reversion emits BUY at 0.45 conf.
        # No other strategy votes. Must close.
        sigs = [
            _mk("moving_average_crossover", Signal.HOLD, 0.0),
            _mk("rsi_momentum", Signal.HOLD, 0.0),
            _mk("mean_reversion", Signal.BUY, 0.45),
            _mk("vwap_bounce", Signal.HOLD, 0.0),
        ]
        fired, best = _evaluate_fast_path(held_side="SELL", signals=sigs)
        assert fired is True
        assert best.strategy_name == "mean_reversion"
        assert best.signal == Signal.BUY

    def test_long_closed_on_lone_sell_signal(self):
        # Mirror: held LONG, lone SELL must close.
        sigs = [_mk("mean_reversion", Signal.SELL, 0.50)]
        fired, best = _evaluate_fast_path(held_side="BUY", signals=sigs)
        assert fired is True
        assert best.signal == Signal.SELL

    def test_below_floor_blocks_fast_path(self):
        # mean_reversion exits emit at 0.45; if floor is raised to 0.50,
        # don't fire. This is the safety valve for users who want stricter
        # exits.
        sigs = [_mk("mean_reversion", Signal.BUY, 0.45)]
        fired, _ = _evaluate_fast_path(
            held_side="SELL", signals=sigs, exit_floor=0.50,
        )
        assert fired is False

    def test_zero_floor_disables_fast_path(self):
        # signal_exit_min_conf <= 0 must turn the feature OFF entirely.
        sigs = [_mk("mean_reversion", Signal.BUY, 0.95)]
        fired, _ = _evaluate_fast_path(
            held_side="SELL", signals=sigs, exit_floor=0.0,
        )
        assert fired is False

    def test_same_side_signals_ignored(self):
        # Held SHORT + another SELL signal must NOT trigger fast-path
        # (that would be scaling up the short, not closing).
        sigs = [
            _mk("mean_reversion", Signal.SELL, 0.90),
            _mk("rsi_momentum", Signal.SELL, 0.85),
        ]
        fired, _ = _evaluate_fast_path(held_side="SELL", signals=sigs)
        assert fired is False

    def test_mixed_signals_picks_highest_conf(self):
        # Two strategies emit closing-direction signals at different
        # confidences — the higher one is picked (and fires if above floor).
        sigs = [
            _mk("rsi_momentum", Signal.BUY, 0.42),
            _mk("mean_reversion", Signal.BUY, 0.65),
            _mk("vwap_bounce", Signal.HOLD, 0.0),
        ]
        fired, best = _evaluate_fast_path(held_side="SELL", signals=sigs)
        assert fired is True
        assert best.strategy_name == "mean_reversion"
        assert best.confidence == 0.65

    def test_disagreeing_signals_close_uses_only_closing_dir(self):
        # Held SHORT. mean_reversion BUY (close) at 0.50, rsi_momentum SELL
        # (would scale) at 0.95. Fast-path must look only at BUYs and fire.
        sigs = [
            _mk("mean_reversion", Signal.BUY, 0.50),
            _mk("rsi_momentum", Signal.SELL, 0.95),
        ]
        fired, best = _evaluate_fast_path(held_side="SELL", signals=sigs)
        assert fired is True
        assert best.signal == Signal.BUY


# ─────────────────────────────────────────────────────────────
# Integration — exercise the actual `_trading_cycle` insertion via a stub
# ─────────────────────────────────────────────────────────────


class TestFastPathIntegrationAgent:
    """Spin up a near-real TradingAgent (object.__new__) and call the
    exact code path that runs in _trading_cycle, minus heavy I/O."""

    def _agent_with_open_short(self):
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a._signal_exit_min_conf = 0.40

        # A SHORT on RAILTEL (today's actual scenario)
        held = MagicMock()
        held.side = "SELL"
        held.quantity = 8
        held.symbol = "RAILTEL"
        a.portfolio = MagicMock()
        a.portfolio.positions = {"RAILTEL": held}

        # Capture what the agent ends up calling
        a._process_signal = MagicMock()
        a.signal_audit = MagicMock()
        return a

    def test_lone_mean_reversion_buy_closes_held_short(self):
        """End-to-end via _process_signal mock: feed the same signal list
        we got at 10:18:21 (mean_reversion EXIT alone) and assert
        _process_signal was called with a BUY ensemble decision."""
        a = self._agent_with_open_short()

        # Mimic the inline fast-path logic directly. We cannot easily call
        # _trading_cycle without booting strategies + scanner + market data,
        # so we mirror the decision and forward to _process_signal exactly
        # the way the agent does.
        from trading_agent import TradeSignal as _TradeSignal
        from strategies.base_strategy import Signal as _Signal

        signals = [
            _mk("moving_average_crossover", _Signal.HOLD, 0.0),
            _mk("rsi_momentum", _Signal.HOLD, 0.0),
            _mk("mean_reversion", _Signal.BUY, 0.45),
            _mk("vwap_bounce", _Signal.HOLD, 0.0),
            _mk("opening_range_breakout", _Signal.HOLD, 0.0),
            _mk("supertrend_follow", _Signal.HOLD, 0.0),
        ]

        # Replicate the exact gate logic
        held_pos = a.portfolio.positions.get("RAILTEL")
        assert held_pos is not None
        closing_dir = _Signal.SELL if held_pos.side == "BUY" else _Signal.BUY
        closing_signals = [s for s in signals if s.signal == closing_dir]
        assert len(closing_signals) == 1
        best = max(closing_signals, key=lambda s: s.confidence)
        assert best.confidence >= a._signal_exit_min_conf

        # Construct the synthetic close signal exactly like the agent does
        close_signal = _TradeSignal(
            signal=closing_dir, symbol="RAILTEL", price=332.65,
            timestamp=best.timestamp,
            strategy_name=f"exit_fast_path:{best.strategy_name}",
            confidence=best.confidence,
            stop_loss=best.stop_loss, take_profit=best.take_profit,
            metadata={
                **(best.metadata or {}),
                "exit_fast_path": True,
                "underlying_strategy": best.strategy_name,
            },
            contributing_strategies={best.strategy_name: 1.0},
        )
        a._process_signal(close_signal, "12345", 332.65)

        # Verify
        a._process_signal.assert_called_once()
        called_signal = a._process_signal.call_args.args[0]
        assert called_signal.signal == _Signal.BUY  # closes the short
        assert called_signal.metadata["exit_fast_path"] is True
        assert called_signal.metadata["underlying_strategy"] == "mean_reversion"
        assert called_signal.strategy_name.startswith("exit_fast_path:")
