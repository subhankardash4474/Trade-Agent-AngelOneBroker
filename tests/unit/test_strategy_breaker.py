"""Tests for the per-strategy circuit breaker added 2026-05-07.

Suspends a single strategy for the day after:
  - N consecutive losses, OR
  - daily PnL drops below -X% of initial capital (just for that strategy)

Other strategies keep trading. Day rollover clears the state.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest


def _make_agent(consec_max: int = 3, daily_pct: float = 1.0,
                initial_balance: float = 10000.0):
    """Minimal TradingAgent stub for breaker logic only."""
    from trading_agent import TradingAgent

    a = TradingAgent.__new__(TradingAgent)
    a.config = {"capital": {"initial_balance": initial_balance}}
    a._strategy_max_consec_losses = consec_max
    a._strategy_daily_loss_pct = daily_pct
    a._strategy_state = {}
    return a


def _record(strategy: str, pnl: float):
    return SimpleNamespace(strategy=strategy, pnl=pnl)


def test_strategy_breaker_consec_losses_trips_at_threshold():
    a = _make_agent(consec_max=3)
    for _ in range(2):
        a._update_strategy_breaker_state(_record("mr", -10))
    suspended, _ = a._strategy_is_suspended("mr")
    assert suspended is False

    a._update_strategy_breaker_state(_record("mr", -10))
    suspended, reason = a._strategy_is_suspended("mr")
    assert suspended is True
    assert "consec_losses" in reason


def test_strategy_breaker_consec_resets_on_win():
    a = _make_agent(consec_max=3)
    a._update_strategy_breaker_state(_record("mr", -10))
    a._update_strategy_breaker_state(_record("mr", -10))
    a._update_strategy_breaker_state(_record("mr", +20))   # break the streak
    a._update_strategy_breaker_state(_record("mr", -10))
    suspended, _ = a._strategy_is_suspended("mr")
    assert suspended is False, "win should have reset the consec-loss counter"


def test_strategy_breaker_daily_loss_pct():
    a = _make_agent(consec_max=999, daily_pct=1.0, initial_balance=10000)
    # 1% of 10000 = -100 floor
    a._update_strategy_breaker_state(_record("mr", -50))
    suspended, _ = a._strategy_is_suspended("mr")
    assert suspended is False
    a._update_strategy_breaker_state(_record("mr", -60))   # cumulative -110
    suspended, reason = a._strategy_is_suspended("mr")
    assert suspended is True
    assert "daily_pnl" in reason


def test_strategy_breaker_other_strategies_unaffected():
    a = _make_agent(consec_max=3)
    for _ in range(3):
        a._update_strategy_breaker_state(_record("mr", -10))
    assert a._strategy_is_suspended("mr")[0] is True
    assert a._strategy_is_suspended("xgboost_classifier")[0] is False
    assert a._strategy_is_suspended("supertrend_follow")[0] is False


def test_strategy_breaker_disabled_when_zero():
    a = _make_agent(consec_max=0, daily_pct=0.0)
    for _ in range(20):
        a._update_strategy_breaker_state(_record("mr", -100))
    suspended, _ = a._strategy_is_suspended("mr")
    assert suspended is False


def test_strategy_breaker_does_not_unsuspend_on_subsequent_win():
    """Once suspended, must STAY suspended for the day even if mock
    record_trade is called again with a profitable pnl."""
    a = _make_agent(consec_max=3)
    for _ in range(3):
        a._update_strategy_breaker_state(_record("mr", -10))
    assert a._strategy_is_suspended("mr")[0] is True
    a._update_strategy_breaker_state(_record("mr", +500))
    # Even a big winner shouldn't lift the suspension mid-day
    assert a._strategy_is_suspended("mr")[0] is True


def test_strategy_breaker_handles_missing_strategy_field():
    """Records without `strategy` should not crash the breaker update."""
    a = _make_agent(consec_max=3)
    rec = SimpleNamespace(strategy=None, pnl=-100)
    a._update_strategy_breaker_state(rec)  # must not raise
    assert a._strategy_state == {}


def test_strategy_breaker_records_trade_count():
    a = _make_agent(consec_max=3)
    for pnl in [-10, +20, -10]:
        a._update_strategy_breaker_state(_record("mr", pnl))
    assert a._strategy_state["mr"]["trades"] == 3
    assert a._strategy_state["mr"]["daily_pnl"] == pytest.approx(0)
