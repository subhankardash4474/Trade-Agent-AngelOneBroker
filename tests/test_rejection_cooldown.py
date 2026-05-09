"""Tests for the rejection cooldown (2026-05-04 part 4).

Bug it fixes:
  Persistent rejection gates (notional_floor, safety_gate, sector_concentration,
  ATR gate, expected_profit, etc.) re-reject the same (symbol, direction)
  every cycle because the strategy keeps firing the same signal. On 2026-05-04:
    - BANDHANBNK SELL rejected 3x (09:19, 10:34, 10:47, 10:48)
    - MEESHO SELL rejected 3x
    - TATACHEM SELL rejected 3x
    - VTL SELL rejected 2x
  Each rejection logs a CYCLE-DIGEST line, an audit-CSV row, and a console
  log. Pure noise + wasted compute on a 17-symbol watchlist.

Fix: a (symbol, direction) -> datetime cooldown map. When `_audit_reject` is
called with a persistent reason (i.e. NOT already_open / blacklist / cooldown
/ shorts_disabled), seed the map. `_open_new_position` then short-circuits
on the next signal in the same direction within the cooldown window.

State-dependent reasons (already_open, blacklist, etc.) are excluded because
they clear when state changes — caching a stale cooldown for those would
incorrectly block the agent from re-entering after a position closes or a
blacklist resets.

These tests exercise the helpers directly without booting the full agent.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────
# Helpers — replicate the production logic in isolation
# ─────────────────────────────────────────────────────────────


class _FakeAgent:
    """Minimal stand-in for TradingAgent that exercises the cooldown logic."""

    SKIP_REASONS = ("already_open", "blacklist", "cooldown", "shorts_disabled")

    def __init__(self, cooldown_seconds: int = 300):
        self._rejection_cooldown_seconds = cooldown_seconds
        self._rejection_cooldown_map: dict = {}
        self._rejection_cooldown_skip_reasons = self.SKIP_REASONS

    def _reason_skips_cooldown(self, reason: str) -> bool:
        prefix = reason.split(":", 1)[0]
        return prefix in self._rejection_cooldown_skip_reasons

    def _is_rejection_cooldown_active(self, symbol: str, direction: str) -> bool:
        if self._rejection_cooldown_seconds <= 0:
            return False
        last = self._rejection_cooldown_map.get((symbol, direction))
        if last is None:
            return False
        elapsed = (datetime.now(IST) - last).total_seconds()
        return elapsed < self._rejection_cooldown_seconds

    def seed_rejection(self, symbol: str, direction: str, reason: str, when: datetime = None):
        """Mirror of the seed step inside `_audit_reject`."""
        if self._rejection_cooldown_seconds <= 0:
            return
        if self._reason_skips_cooldown(reason):
            return
        self._rejection_cooldown_map[(symbol, direction)] = when or datetime.now(IST)


# ─────────────────────────────────────────────────────────────
# Reasons that DO seed the cooldown
# ─────────────────────────────────────────────────────────────


class TestPersistentReasonsSeedCooldown:
    """Persistent gates should seed the cooldown — re-evaluation is wasted."""

    @pytest.mark.parametrize("reason", [
        "notional_floor:2268<2659:cap_constrained",
        "safety_gate:sector_concentration",
        "atr_gate:0.30<0.50@bear_high_vol",
        "expected_profit:rr_too_low_0.85<1.20",
        "late_cutoff:14:30",
        "sizing:zero_qty",
        "dead_hour:noon_lull",
        "pattern:reversal_pending",
        "risk_gate:daily_loss_limit",
        "short_regime:bull_low_vol",
    ])
    def test_persistent_reason_seeds_cooldown(self, reason):
        agent = _FakeAgent(cooldown_seconds=300)
        assert agent._is_rejection_cooldown_active("RAILTEL", "SELL") is False
        agent.seed_rejection("RAILTEL", "SELL", reason)
        assert agent._is_rejection_cooldown_active("RAILTEL", "SELL") is True


# ─────────────────────────────────────────────────────────────
# Reasons that SHOULD NOT seed the cooldown (state-dependent)
# ─────────────────────────────────────────────────────────────


class TestStateDependentReasonsExcluded:
    """These reasons clear naturally when state changes — caching them would
    block re-entry after the underlying state resolves."""

    @pytest.mark.parametrize("reason", [
        "already_open:duplicate",
        "already_open:duplicate_short",
        "blacklist:loss_cap",
        "cooldown:5m",  # exit cooldown
        "shorts_disabled",
    ])
    def test_state_dependent_reason_does_not_seed(self, reason):
        agent = _FakeAgent(cooldown_seconds=300)
        agent.seed_rejection("RAILTEL", "SELL", reason)
        assert agent._is_rejection_cooldown_active("RAILTEL", "SELL") is False, (
            f"State-dependent reason {reason!r} must not seed the cooldown."
        )


# ─────────────────────────────────────────────────────────────
# Direction + symbol scoping
# ─────────────────────────────────────────────────────────────


class TestCooldownScoping:
    def test_cooldown_is_per_symbol(self):
        """Rejecting BANDHANBNK SELL doesn't block MEESHO SELL."""
        agent = _FakeAgent()
        agent.seed_rejection("BANDHANBNK", "SELL", "notional_floor:1000<2659")
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "SELL") is True
        assert agent._is_rejection_cooldown_active("MEESHO", "SELL") is False

    def test_cooldown_is_per_direction(self):
        """Rejecting BANDHANBNK SELL doesn't block BANDHANBNK BUY."""
        agent = _FakeAgent()
        agent.seed_rejection("BANDHANBNK", "SELL", "notional_floor:1000<2659")
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "SELL") is True
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "BUY") is False


# ─────────────────────────────────────────────────────────────
# Time expiry
# ─────────────────────────────────────────────────────────────


class TestCooldownExpiry:
    def test_cooldown_expires_after_window(self):
        """A 5-min cooldown should clear after 5+ min."""
        agent = _FakeAgent(cooldown_seconds=300)
        # Seed a rejection that "happened" 6 minutes ago.
        old_time = datetime.now(IST) - timedelta(minutes=6)
        agent.seed_rejection("BANDHANBNK", "SELL", "notional_floor:1000<2659", when=old_time)
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "SELL") is False

    def test_cooldown_active_within_window(self):
        agent = _FakeAgent(cooldown_seconds=300)
        recent = datetime.now(IST) - timedelta(seconds=120)  # 2 min ago
        agent.seed_rejection("BANDHANBNK", "SELL", "notional_floor:1000<2659", when=recent)
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "SELL") is True


# ─────────────────────────────────────────────────────────────
# Disable knob
# ─────────────────────────────────────────────────────────────


class TestDisableKnob:
    def test_zero_seconds_disables_cooldown(self):
        agent = _FakeAgent(cooldown_seconds=0)
        agent.seed_rejection("BANDHANBNK", "SELL", "notional_floor:1000<2659")
        # No cooldown is even active when disabled.
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "SELL") is False

    def test_negative_seconds_disables_cooldown(self):
        agent = _FakeAgent(cooldown_seconds=-1)
        agent._rejection_cooldown_map[("BANDHANBNK", "SELL")] = datetime.now(IST)
        assert agent._is_rejection_cooldown_active("BANDHANBNK", "SELL") is False


# ─────────────────────────────────────────────────────────────
# Config wiring
# ─────────────────────────────────────────────────────────────


class TestConfigWiring:
    @pytest.fixture(scope="class")
    def cfg(self):
        path = Path(__file__).parent.parent / "config.yaml"
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_rejection_cooldown_minutes_present(self, cfg):
        risk = cfg.get("risk", {})
        assert "rejection_cooldown_minutes" in risk, (
            "config.yaml risk.rejection_cooldown_minutes must be defined."
        )

    def test_rejection_cooldown_minutes_sane(self, cfg):
        risk = cfg.get("risk", {})
        value = risk.get("rejection_cooldown_minutes", 0)
        assert 0 <= value <= 60, f"cooldown must be in [0, 60] min, got {value}"


# ─────────────────────────────────────────────────────────────
# Production source guard
# ─────────────────────────────────────────────────────────────


class TestProductionSourceShape:
    def test_trading_agent_wires_in_cooldown(self):
        src = (Path(__file__).parent.parent / "trading_agent.py").read_text(encoding="utf-8")
        assert "_rejection_cooldown_map" in src
        assert "_is_rejection_cooldown_active" in src
        assert "_reason_skips_cooldown" in src
        # Must short-circuit at top of _open_new_position.
        assert "[REJECT-COOLDOWN]" in src
        # Daily reset must clear the map.
        assert "self._rejection_cooldown_map.clear()" in src
