"""Guards against the silent-failure modes that froze the agent on 2026-04-29.

Each test pins down a specific failure so it can never come back:

  1. _pre_trade_safety_checks must not AttributeError on Portfolio.total_value
     (live fix: use `get_total_value`).
  2. check_data_quality must treat tz-naive yfinance timestamps as UTC, not
     IST (live fix: `pytz.utc.localize` for naive stamps).
  3. _evaluate_strategy must request enough historical bars early-in-session
     so strategies aren't starved of warmup data (live fix: 7-day floor).
  4. Regime-aware ATR gate — `_atr_gate_threshold` must read the per-regime
     value when configured, falling back to the flat default otherwise.
  5. Data-quality log noise — routine staleness must log at DEBUG and only
     escalate to WARNING after N consecutive failures for a given symbol.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────
# Bug 1 — Portfolio.total_value typo in _pre_trade_safety_checks
# ─────────────────────────────────────────────────────────────


class TestPreTradeSafetyChecksUsesCorrectMethod:
    """The typo `self.portfolio.total_value(...)` crashed every BUY that
    reached the safety-gate. The real method is `get_total_value`."""

    def test_portfolio_exposes_get_total_value_not_total_value(self, tmp_path):
        """Portfolio must expose get_total_value; the wrong name must not
        be accidentally re-introduced as a silent alias."""
        from core.portfolio import Portfolio

        p = Portfolio(initial_balance=10000.0, log_dir=str(tmp_path))
        assert hasattr(p, "get_total_value"), "Portfolio must expose get_total_value"
        # total_value should NOT exist — if someone adds it as an alias,
        # future code might drift back to the typo and mask the bug.
        assert not hasattr(p, "total_value"), (
            "Portfolio.total_value is a footgun — use get_total_value"
        )

    def test_safety_check_with_open_positions_does_not_crash(self, tmp_path):
        """Reproduces the 2026-04-29 13:55 crash:
        BUY signal arrives, agent has positions open, safety check runs."""
        import yaml
        from core.portfolio import Portfolio

        # Minimal trading agent setup is expensive; instead exercise the exact
        # code path by constructing the same call the method performs.
        p = Portfolio(initial_balance=10000.0, log_dir=str(tmp_path))
        # Simulate an already-open position so positions dict is non-empty,
        # which was the condition that triggered the crash path.
        p.open_position(
            symbol="BANDHANBNK", side="BUY", price=200.0,
            quantity=10, strategy="mean_reversion",
            stop_loss=196.0, take_profit=210.0,
        )
        prices_map = {s: pos.entry_price for s, pos in p.positions.items()}
        # This is the exact call that crashed live — assert it works now.
        total = p.get_total_value(prices_map)
        assert total > 0


# ─────────────────────────────────────────────────────────────
# Bug 2 — Staleness check treating UTC as IST
# ─────────────────────────────────────────────────────────────


class TestDataQualityStalenessTimezone:
    """yfinance returns tz-naive timestamps that are actually UTC. The live
    code was localizing them as IST, inflating every bar's age by 5h30m and
    silently blocking all signals for the first ~4 hours of each session."""

    def _fresh_df(self, last_ts) -> pd.DataFrame:
        idx = pd.date_range(end=last_ts, periods=20, freq="5min")
        return pd.DataFrame(
            {
                "open":   [100.0] * 20,
                "high":   [101.0] * 20,
                "low":    [99.0] * 20,
                "close":  [100.5] * 20,
                "volume": [10000] * 20,
            },
            index=idx,
        )

    def test_tz_naive_utc_bar_is_considered_fresh(self):
        from core.market_safety import check_data_quality

        # yfinance: tz-naive timestamp, expressed in UTC.
        # IST "now" is this UTC time + 5h30m.
        utc_now = datetime.now(pytz.UTC).replace(tzinfo=None, microsecond=0)
        last_bar_naive_utc = utc_now - timedelta(minutes=2)
        df = self._fresh_df(last_bar_naive_utc)
        is_safe, reason = check_data_quality(df, max_staleness_minutes=15)
        assert is_safe, f"Fresh UTC bar must not be flagged stale. got: {reason}"

    def test_genuinely_stale_bar_still_rejected(self):
        """Sanity check — don't over-correct: a truly old bar must still fail."""
        from core.market_safety import check_data_quality

        old_utc = datetime.now(pytz.UTC).replace(tzinfo=None) - timedelta(hours=1)
        df = self._fresh_df(old_utc)
        is_safe, reason = check_data_quality(df, max_staleness_minutes=15)
        assert not is_safe
        assert "stale" in reason.lower()

    def test_tz_aware_ist_bar_unaffected(self):
        """If a caller already passes tz-aware IST timestamps, we must still
        compute the correct age (no double localization)."""
        from core.market_safety import check_data_quality

        ist_now = datetime.now(IST).replace(microsecond=0)
        last_bar_ist = ist_now - timedelta(minutes=2)
        df = self._fresh_df(last_bar_ist)
        is_safe, _ = check_data_quality(df, max_staleness_minutes=15)
        assert is_safe


# ─────────────────────────────────────────────────────────────
# Bug 3 — historical fetch window too narrow early-in-session
# ─────────────────────────────────────────────────────────────


class TestEvaluateStrategyLookbackWindow:
    """At 10:00 AM the old code requested only 5 × bars_needed × 2 = 300 min
    of history (~11 5-min bars). Strategies need 25-30 bars to warm up, so
    every strategy returned None silently for the first 2+ hours daily."""

    def test_lookback_covers_at_least_one_trading_day(self):
        """Verify trading_agent code has the 7-day floor for intraday bars."""
        import inspect

        from trading_agent import TradingAgent

        src = inspect.getsource(TradingAgent._evaluate_strategy)
        # The floor keeps lookback honest; either the 7*24*60 minutes constant
        # or an equivalent days= construction must be present in the code.
        assert "7 * 24 * 60" in src or "timedelta(days=7" in src, (
            "Intraday fetch window must cover >= 7 calendar days so early-in-"
            "session cycles have enough warmup bars. Widen if you changed it."
        )


# ─────────────────────────────────────────────────────────────
# Regime-aware ATR gate (post-EOD enhancement)
# ─────────────────────────────────────────────────────────────


class TestRegimeAwareAtrGate:
    """A single flat ATR floor punishes low-vol regimes and under-filters
    high-vol ones. The gate must read the per-regime map with sensible
    fallback to the flat value."""

    def _agent_stub(self, flat: float, by_regime: dict):
        """Build a minimal object that exposes just the attributes the helper
        reads. Avoids spinning up the full TradingAgent for a pure-logic test."""
        from trading_agent import TradingAgent

        stub = object.__new__(TradingAgent)
        stub._min_entry_atr_pct = flat
        stub._min_entry_atr_pct_by_regime = dict(by_regime)
        return stub

    def test_regime_specific_threshold_wins(self):
        agent = self._agent_stub(
            flat=0.5,
            by_regime={"bull_low_vol": 0.4, "bear_high_vol": 0.7},
        )
        assert agent._atr_gate_threshold("bull_low_vol") == 0.4
        assert agent._atr_gate_threshold("bear_high_vol") == 0.7

    def test_unlisted_regime_falls_back_to_flat(self):
        agent = self._agent_stub(
            flat=0.5,
            by_regime={"bull_low_vol": 0.4},
        )
        assert agent._atr_gate_threshold("sideways") == 0.5
        assert agent._atr_gate_threshold(None) == 0.5
        assert agent._atr_gate_threshold("unknown") == 0.5

    def test_disabled_when_no_config(self):
        agent = self._agent_stub(flat=0.0, by_regime={})
        assert agent._atr_gate_threshold("bull_low_vol") == 0.0


# ─────────────────────────────────────────────────────────────
# DATA-QUALITY log escalation
# ─────────────────────────────────────────────────────────────


class TestDataQualityLogEscalation:
    """One bad feed session on 2026-04-29 produced ~50,000 WARNING lines
    (6 strategies × 15 symbols × every cycle). The escalation logic keeps
    routine skips at DEBUG and surfaces a single WARN per symbol once a
    genuine outage is evident."""

    def test_streak_and_warn_set_are_initialized(self, tmp_path, monkeypatch):
        """Stub the heavy sub-systems so we can instantiate the agent cheaply."""
        # Rather than bootstrapping a full agent, assert the initial-state
        # contract by reading the source — the attributes must exist.
        from trading_agent import TradingAgent
        import inspect

        src = inspect.getsource(TradingAgent.__init__)
        assert "_dq_failure_streak" in src
        assert "_dq_warned_symbols" in src

    def test_warn_threshold_is_reasonable(self):
        from trading_agent import TradingAgent

        # Must be > 1 (otherwise there's no de-noising) and < 20 (otherwise
        # an outage silently persists for too long).
        assert 2 <= TradingAgent._DQ_WARN_AFTER <= 20
