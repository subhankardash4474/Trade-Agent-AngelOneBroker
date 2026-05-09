"""Comprehensive verification of the daily-loss + drawdown kill switches.

The old test_risk_manager.py had a single-line check that recording -301 on
a 3%-of-Rs-10000 limit blocks the next can_trade(). This file fleshes
that out: cumulative losses, latching, day rollover, boundary conditions,
and the drawdown halt tier.

These guards are LOAD-BEARING — they're the only thing standing between
a bad strategy day and a catastrophic loss. They MUST trigger.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.risk_manager import RiskManager


def _rm(initial: float = 10000.0, daily_pct: float = 3.0, **risk_kwargs) -> RiskManager:
    cfg = {
        "risk": {
            "daily_loss_limit_pct": daily_pct,
            "weekly_loss_limit_pct": 5.0,
            "drawdown_halt_pct": 30.0,
            "max_drawdown_pct": 100.0,            # disabled for these tests
            "max_consecutive_losses": 999,        # disabled
            "max_trades_per_day": 999,
            "max_open_positions": 999,
            "intraday_exit_time": "23:59",
            "max_vix": 999,
            "require_nifty_above_200ema": False,
            **risk_kwargs,
        }
    }
    return RiskManager(cfg, initial_balance=initial)


# ── Daily loss kill-switch ──


def test_single_trade_below_limit_does_not_trip():
    rm = _rm()
    rm.record_trade(-299.0)  # 2.99% of 10k — under the 3% limit
    allowed, _ = rm.can_trade()
    assert allowed is True


def test_single_trade_at_exact_limit_trips():
    """The implementation uses `<= -daily_limit`, so exactly at the limit
    is treated as a breach. Document and lock that behaviour."""
    rm = _rm()
    rm.record_trade(-300.0)  # exactly 3%
    allowed, reason = rm.can_trade()
    assert allowed is False
    assert "Daily loss limit" in reason or "Circuit breaker" in reason


def test_single_trade_above_limit_trips():
    rm = _rm()
    rm.record_trade(-301.0)
    allowed, reason = rm.can_trade()
    assert allowed is False


def test_cumulative_losses_trip_limit():
    rm = _rm()
    # 3 trades of -100 each -> -300 cumulative. Should hit limit.
    rm.record_trade(-100.0)
    assert rm.can_trade()[0] is True
    rm.record_trade(-100.0)
    assert rm.can_trade()[0] is True
    rm.record_trade(-101.0)  # now cumulatively -301
    allowed, reason = rm.can_trade()
    assert allowed is False
    assert "Daily loss" in reason or "Circuit breaker" in reason


def test_breaker_latches_even_after_winning_trade():
    """Once the daily breaker fires it must NOT auto-reset on a subsequent
    winning trade. Real-world rationale: if you've blown the daily budget
    you stop for the day, no exception."""
    rm = _rm()
    rm.record_trade(-301.0)
    assert rm.can_trade()[0] is False
    rm.record_trade(+500.0)            # daily PnL now +199 (well above limit)
    allowed, reason = rm.can_trade()
    assert allowed is False, "breaker should latch through the day"
    assert "Circuit breaker" in reason


def test_breaker_resets_on_new_day():
    """At day rollover the daily counter resets and the breaker can clear
    if all the breach conditions are no longer met."""
    rm = _rm()
    rm.record_trade(-301.0)
    assert rm.can_trade()[0] is False

    # Force the internal daily-tracking date back by one day
    rm.state.daily_date = date.today() - timedelta(days=1)

    # Trigger the rollover
    allowed, _ = rm.can_trade()
    # After reset, daily_pnl is 0, breaker cleared, fresh slate
    assert rm.state.daily_pnl == 0.0
    assert rm.state.daily_date == date.today()
    assert rm.state.is_circuit_breaker_active is False
    assert allowed is True


def test_custom_daily_limit_2pct():
    """If user lowers the limit to 2%, smaller losses should trip."""
    rm = _rm(daily_pct=2.0)
    rm.record_trade(-201.0)
    assert rm.can_trade()[0] is False


def test_initial_state_allows_trading():
    rm = _rm()
    allowed, reason = rm.can_trade()
    assert allowed is True
    assert reason == "OK"


# ── Drawdown halt kill-switch (independent of daily loss) ──


def test_drawdown_halt_trips_on_peak_decline():
    rm = _rm(drawdown_halt_pct=10.0)
    # Bump peak to 20k, then crash equity to 17k -> 15% drawdown
    rm.state.peak_balance = 20000.0
    rm.state.current_balance = 17000.0
    allowed, reason = rm.can_trade()
    assert allowed is False
    assert "drawdown" in reason.lower()


def test_drawdown_halt_does_not_trip_below_threshold():
    rm = _rm(drawdown_halt_pct=10.0)
    rm.state.peak_balance = 20000.0
    rm.state.current_balance = 19000.0  # 5% drawdown < 10% halt
    allowed, _ = rm.can_trade()
    assert allowed is True


# ── Combined: kill-switch enforces ALL gates, multiple may trip simultaneously ──


def test_kill_switch_message_is_actionable():
    """The reason must be human-readable so we can debug an outage fast."""
    rm = _rm()
    rm.record_trade(-301.0)
    _, reason = rm.can_trade()
    assert reason  # non-empty
    assert any(kw in reason for kw in ("Daily loss", "Circuit breaker", "drawdown"))
