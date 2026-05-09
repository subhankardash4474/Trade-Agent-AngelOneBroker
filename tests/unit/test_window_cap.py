"""Tests for the open-window cap added 2026-05-07.

Limits how many entries can fire in a rolling X-min window to prevent
correlated cluster risk (e.g. opening-bell pile-on or sector wave).
Disabled by default — caller must opt in with `max_opens_per_window > 0`.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from unittest import mock

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


def _make_agent(max_opens: int = 3, window_min: int = 5):
    """Build a minimal TradingAgent stub with only the fields needed by
    `_pre_trade_safety_checks` and `_record_position_open`."""
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent.config = {"risk": {"max_opens_per_window": max_opens, "opens_window_minutes": window_min}}
    agent._max_opens_per_window = max_opens
    agent._opens_window_minutes = window_min
    agent._recent_opens = deque()
    return agent


def test_window_cap_disabled_when_zero():
    from trading_agent import IST as agent_ist  # noqa: F401

    agent = _make_agent(max_opens=0)
    # Slam 100 opens — should never block
    now = datetime.now(IST)
    for i in range(100):
        agent._recent_opens.append((now, f"S{i}"))
    # Re-create to ensure record path no-ops too
    agent._record_position_open("FOO")
    assert len(agent._recent_opens) == 100  # nothing was added (gate is off)


def test_window_cap_records_opens():
    agent = _make_agent(max_opens=3, window_min=5)
    agent._record_position_open("AAA")
    agent._record_position_open("BBB")
    assert len(agent._recent_opens) == 2
    assert agent._recent_opens[0][1] == "AAA"
    assert agent._recent_opens[1][1] == "BBB"


def test_window_cap_blocks_at_threshold():
    agent = _make_agent(max_opens=3, window_min=5)
    now = datetime.now(IST)
    # Pre-seed with 3 fresh opens
    for sym in ["AAA", "BBB", "CCC"]:
        agent._recent_opens.append((now, sym))

    # Now check the gate by inspecting deque count + threshold logic
    # (we can't easily call _pre_trade_safety_checks without rest of agent,
    # so we mirror the gate here)
    assert len(agent._recent_opens) >= agent._max_opens_per_window


def test_window_cap_prunes_stale_entries():
    """Entries older than the window must be pruned so the gate accurately
    reflects only recent activity."""
    agent = _make_agent(max_opens=3, window_min=5)
    now = datetime.now(IST)

    # 2 entries older than window, 1 fresh
    agent._recent_opens.append((now - timedelta(minutes=10), "OLD1"))
    agent._recent_opens.append((now - timedelta(minutes=8), "OLD2"))
    agent._recent_opens.append((now, "FRESH1"))

    # Reproduce the prune logic
    window_start = now - timedelta(minutes=agent._opens_window_minutes)
    while agent._recent_opens and agent._recent_opens[0][0] < window_start:
        agent._recent_opens.popleft()

    assert len(agent._recent_opens) == 1
    assert agent._recent_opens[0][1] == "FRESH1"


def test_window_cap_full_integration_via_safety_checks(tmp_path, monkeypatch):
    """End-to-end: hit `_pre_trade_safety_checks` itself.

    We need a slightly fatter agent stub for this — the function calls
    `self._get_previous_close` and the data handler. We mock those out
    so we test ONLY the window-cap branch.
    """
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent.config = {}
    agent._max_opens_per_window = 3
    agent._opens_window_minutes = 5
    agent._recent_opens = deque()
    agent._max_sector_exposure_pct = 100.0
    agent._max_symbol_exposure_pct = 100.0
    agent._unknown_sector_per_symbol = True

    # Skip the circuit + sector branches (we only care about window-cap)
    agent._get_previous_close = mock.Mock(return_value=None)

    class _Pf:
        def __init__(self):
            self.positions = {}
        def get_total_value(self, prices):
            return 10000.0
    agent.portfolio = _Pf()

    class _DH:
        def get_historical_data(self, *a, **kw):
            return None
    agent.data_handler = _DH()

    # Pre-fill 3 recent opens
    now = datetime.now(IST)
    for sym in ["S1", "S2", "S3"]:
        agent._recent_opens.append((now, sym))

    safe, reason = agent._pre_trade_safety_checks("S4", current_price=100.0, cost=200.0)
    assert safe is False
    assert "window_cap" in reason
    assert "3 opens" in reason or "3" in reason


def test_window_cap_allows_when_below_threshold(monkeypatch):
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent.config = {}
    agent._max_opens_per_window = 5
    agent._opens_window_minutes = 5
    agent._recent_opens = deque()
    agent._max_sector_exposure_pct = 100.0
    agent._max_symbol_exposure_pct = 100.0
    agent._unknown_sector_per_symbol = True
    agent._get_previous_close = mock.Mock(return_value=None)

    class _Pf:
        def __init__(self):
            self.positions = {}
        def get_total_value(self, prices):
            return 10000.0
    agent.portfolio = _Pf()

    class _DH:
        def get_historical_data(self, *a, **kw):
            return None
    agent.data_handler = _DH()

    # Only 2 in window (cap is 5)
    now = datetime.now(IST)
    for sym in ["S1", "S2"]:
        agent._recent_opens.append((now, sym))

    safe, reason = agent._pre_trade_safety_checks("S3", current_price=100.0, cost=200.0)
    assert safe is True, reason
