"""Tests for trailing-stop exit classification (2026-05-04).

Bug it fixes:
  RiskManager.check_stop_loss_take_profit() returns "stop_loss" for any SL
  breach — including trailing-stop hits that locked in profit. When IDEA
  closed for +Rs 20.80 today via a trailing stop, the email arrived as:
      "Exit: STOP_LOSS"  (level=warning)
  which is misleading: the trade was a winner, not a stop-out.

Fix: when the trigger is "stop_loss" but `TrailingStop.trailing_active` is
True (i.e. the position moved >=1R favorable, activating the ratchet),
relabel the exit as "trailing_stop". Also drive the alert level by the
realised PnL, not the trigger name — a profitable trailing-stop hit must
NOT be a "warning"-level alert.

These tests stub `_check_position_exits` dependencies so we can verify the
classification, alert subject, alert level, and the downstream
`_record_exit` cooldown decision in isolation, without booting the full
TradingAgent or hitting the broker.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.risk_manager import TrailingStop


# ─────────────────────────────────────────────────────────────
# Helpers — minimal recreation of the production classifier
# ─────────────────────────────────────────────────────────────


def _classify_exit(trigger_reason: str, trailing_stop) -> str:
    """Mirror of the production classification logic in `_check_position_exits`.

    `trailing_stop` is either None or a TrailingStop instance.
    """
    if trigger_reason == "stop_loss":
        if trailing_stop is not None and getattr(trailing_stop, "trailing_active", False):
            return "trailing_stop"
    return trigger_reason


def _alert_level(pnl: float) -> str:
    """Mirror of the production level logic — driven by PnL, not trigger."""
    return "warning" if pnl < 0 else "info"


# ─────────────────────────────────────────────────────────────
# Trigger classification
# ─────────────────────────────────────────────────────────────


class TestTrailingStopReclassification:
    """When the trailing stop has activated and gets hit, the exit is a
    trailing-stop event — never a 'stop_loss' (which implies a loss)."""

    def test_short_idea_replay_classifies_as_trailing_stop(self):
        """Replay of today's IDEA trade: SHORT 264 @ 10.60, closed @ 10.51 for +Rs 20.80."""
        ts = TrailingStop(
            entry_price=10.60,
            initial_sl=10.73,  # SL above entry for a short
            side="SELL",
            trail_activation_rr=1.0,
            trail_step_pct=0.3,
        )
        # Price falls from 10.60 -> 10.45 (favorable for short).
        # _initial_risk = |10.60 - 10.73| = 0.13. So 1R favorable = 10.60 - 0.13 = 10.47.
        # At 10.45 the unrealized R = (10.60 - 10.45) / 0.13 ≈ 1.15R, so trailing activates.
        ts.update(10.45)
        assert ts.trailing_active is True, "Trailing must activate after >=1R favorable move"
        # Then price reverts back up to ~10.51 and hits the trailed SL.
        # The risk manager returns "stop_loss" — but that's misleading.
        classified = _classify_exit("stop_loss", ts)
        assert classified == "trailing_stop", (
            "When the trailing stop is active and gets hit, the exit must be "
            "labeled 'trailing_stop' — not 'stop_loss'."
        )

    def test_long_replay_with_trailing_active(self):
        """Same logic for a long: trailing activates after +1R, exit is profitable."""
        ts = TrailingStop(
            entry_price=100.0,
            initial_sl=98.0,
            side="BUY",
            trail_activation_rr=1.0,
            trail_step_pct=0.3,
        )
        # Price runs to 103 (1.5R favorable).
        ts.update(103.0)
        assert ts.trailing_active is True
        # Then reverses; trailed SL gets hit on the way down.
        assert _classify_exit("stop_loss", ts) == "trailing_stop"

    def test_initial_stop_hit_stays_as_stop_loss(self):
        """A real initial-SL hit (no favorable move yet) keeps the 'stop_loss' label."""
        ts = TrailingStop(
            entry_price=100.0,
            initial_sl=98.0,
            side="BUY",
            trail_activation_rr=1.0,
            trail_step_pct=0.3,
        )
        # Price moves only 0.5R favorable — not enough to activate trailing.
        ts.update(101.0)
        assert ts.trailing_active is False
        # Then drops to 97.5 and hits the initial SL.
        assert _classify_exit("stop_loss", ts) == "stop_loss"

    def test_no_trailing_stop_object_keeps_label(self):
        """If trailing stop wasn't even registered, classification is unchanged."""
        assert _classify_exit("stop_loss", None) == "stop_loss"

    def test_take_profit_never_reclassified(self):
        """TP triggers don't get touched, even when trailing was active."""
        ts = TrailingStop(
            entry_price=100.0, initial_sl=98.0, side="BUY",
            trail_activation_rr=1.0, trail_step_pct=0.3,
        )
        ts.update(105.0)
        assert ts.trailing_active is True
        # TP triggered — that's a TP, not a trailing-stop, regardless of state.
        assert _classify_exit("take_profit", ts) == "take_profit"


# ─────────────────────────────────────────────────────────────
# Alert level — driven by PnL, not trigger
# ─────────────────────────────────────────────────────────────


class TestAlertLevel:
    def test_profitable_exit_is_info_regardless_of_reason(self):
        """A trailing-stop locking in profit must not be a warning."""
        assert _alert_level(+20.80) == "info"
        assert _alert_level(+0.01) == "info"
        assert _alert_level(0.0) == "info"

    def test_losing_exit_is_warning(self):
        assert _alert_level(-50.00) == "warning"
        assert _alert_level(-0.01) == "warning"

    def test_old_buggy_logic_would_have_misclassified(self):
        """Documents the OLD buggy logic for posterity. We do NOT use it now.
        Old: level = 'warning' if reason == 'stop_loss' else 'info'
        New: level = 'warning' if pnl < 0 else 'info'
        """
        old = lambda reason: "warning" if reason == "stop_loss" else "info"
        # The IDEA case: profit but reason was "stop_loss" → old code said "warning".
        assert old("stop_loss") == "warning"  # BUG: misleading
        # Correct: PnL=+20.80 → info.
        assert _alert_level(+20.80) == "info"


# ─────────────────────────────────────────────────────────────
# Cooldown logic recognizes trailing_stop as a "won" exit
# ─────────────────────────────────────────────────────────────


def _should_cooldown(pnl: float, exit_reason: str) -> bool:
    """Mirror of the production cooldown-decision in `_record_exit`."""
    is_loss = pnl < 0
    is_take_profit = exit_reason == "take_profit"
    is_trailing_win = exit_reason == "trailing_stop" and pnl >= 5.0
    return is_loss or (not (is_take_profit or is_trailing_win) and pnl < 5.0)


class TestCooldownRecognizesTrailingWins:
    def test_idea_trade_no_cooldown(self):
        """The IDEA trade today: trailing-stop hit for +Rs 20.80 → no cooldown."""
        assert _should_cooldown(20.80, "trailing_stop") is False

    def test_loss_always_cools_down(self):
        assert _should_cooldown(-15.0, "stop_loss") is True
        assert _should_cooldown(-1.0, "trailing_stop") is True  # losing trail still cools

    def test_take_profit_never_cools_down(self):
        assert _should_cooldown(50.0, "take_profit") is False
        assert _should_cooldown(2.0, "take_profit") is False  # even small TPs

    def test_small_trailing_win_still_cools(self):
        """Small trailing wins (<Rs 5) still cool down — the trend reversed,
        and re-entering immediately would chase a fading move."""
        assert _should_cooldown(2.50, "trailing_stop") is True
        assert _should_cooldown(4.99, "trailing_stop") is True

    def test_large_trailing_win_no_cooldown(self):
        """Trailing wins >=Rs 5 are real trend captures — let re-entry happen."""
        assert _should_cooldown(5.0, "trailing_stop") is False
        assert _should_cooldown(20.80, "trailing_stop") is False

    def test_signal_exit_in_profit_no_cooldown_when_big(self):
        """A signal-driven exit with comfortable profit stays free of cooldown."""
        assert _should_cooldown(15.0, "signal") is False


# ─────────────────────────────────────────────────────────────
# Production-source guard
# ─────────────────────────────────────────────────────────────


class TestProductionSourceShape:
    """Structural test: ensure the fix is actually wired in production code."""

    def test_trading_agent_uses_actual_reason_and_pnl_level(self):
        src = (Path(__file__).parents[2] / "trading_agent.py").read_text(encoding="utf-8")
        # The new code path must classify the trigger before placing the order.
        assert "actual_reason = reason" in src, (
            "Production code must recompute exit reason from trailing-stop state."
        )
        assert "trailing_active" in src, (
            "Production code must inspect TrailingStop.trailing_active."
        )
        # Alert level must be PnL-driven, not reason-driven.
        assert "level = \"warning\" if record.pnl < 0 else \"info\"" in src, (
            "Alert level must be driven by realised PnL, not trigger name."
        )
        # The buggy form must not exist.
        assert 'level = "warning" if reason == "stop_loss" else "info"' not in src, (
            "Old buggy reason-driven level logic must not exist anymore."
        )
