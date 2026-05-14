"""Tests for the 2026-05-14 strategy-concurrency cap.

Live evidence (2026-05-14 morning):
    09:24 SELL TARIL          supertrend_follow
    09:27 SELL AEGISLOG       rsi_momentum
    09:44 SELL CENTRALBK      supertrend_follow
    09:44 SELL TATACAP        supertrend_follow
    09:53 SELL OBEROIRLTY     supertrend_follow
    09:57 SELL PCBL           supertrend_follow
    10:00 SELL JSWENERGY      supertrend_follow
    10:01 SELL FEDERALBNK     supertrend_follow
    11:01 SELL ABLBL          supertrend_follow
    11:07 SELL CHOLAFIN       supertrend_follow

8 of 10 SELL signals in 100 min were `supertrend_follow`. By 11:07
the agent held 4 simultaneous supertrend_follow shorts in financials.
4 stopped out for -Rs 730 in the 40 minutes that followed.

The fix: cap concurrent positions per strategy. With cap=3 the late
CHOLAFIN entry would have been blocked at the safety gate, saving Rs 196
on its own.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from unittest import mock

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


def _mk_position(symbol: str, strategy: str, qty: int = 10, price: float = 100.0):
    """Build a minimal Position-like object that exposes only what
    `_pre_trade_safety_checks` reads."""
    pos = mock.Mock()
    pos.symbol = symbol
    pos.strategy = strategy
    pos.entry_price = price
    pos.quantity = qty
    pos.side = "SELL"
    return pos


def _mk_agent(positions: dict, cap: int = 3):
    """Stub TradingAgent with just the fields touched by the safety gate."""
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent.config = {}
    agent._max_opens_per_window = 0
    agent._opens_window_minutes = 5
    agent._recent_opens = deque()
    agent._max_sector_exposure_pct = 100.0
    agent._max_symbol_exposure_pct = 100.0
    agent._unknown_sector_per_symbol = True
    agent._max_positions_per_strategy = cap
    agent._get_previous_close = mock.Mock(return_value=None)

    class _Pf:
        def __init__(self, positions):
            self.positions = positions

        def get_total_value(self, prices):
            return 100000.0

    agent.portfolio = _Pf(positions)

    class _DH:
        def get_historical_data(self, *a, **kw):
            return None

    agent.data_handler = _DH()
    return agent


def test_cap_disabled_when_zero():
    """Cap=0 == legacy behaviour: never blocks regardless of pile-up."""
    positions = {
        "S1": _mk_position("S1", "supertrend_follow"),
        "S2": _mk_position("S2", "supertrend_follow"),
        "S3": _mk_position("S3", "supertrend_follow"),
        "S4": _mk_position("S4", "supertrend_follow"),
        "S5": _mk_position("S5", "supertrend_follow"),
    }
    agent = _mk_agent(positions, cap=0)
    safe, reason = agent._pre_trade_safety_checks(
        "S6", current_price=100.0, cost=200.0, strategy="supertrend_follow",
    )
    assert safe is True, reason


def test_cap_blocks_at_threshold():
    """Cap=3 with 3 same-strategy positions already open: 4th is rejected."""
    positions = {
        "S1": _mk_position("S1", "supertrend_follow"),
        "S2": _mk_position("S2", "supertrend_follow"),
        "S3": _mk_position("S3", "supertrend_follow"),
    }
    agent = _mk_agent(positions, cap=3)
    safe, reason = agent._pre_trade_safety_checks(
        "S4", current_price=100.0, cost=200.0, strategy="supertrend_follow",
    )
    assert safe is False
    assert "strategy_concurrency" in reason
    assert "supertrend_follow" in reason


def test_cap_allows_below_threshold():
    """Cap=3 with 2 same-strategy positions: 3rd is allowed."""
    positions = {
        "S1": _mk_position("S1", "supertrend_follow"),
        "S2": _mk_position("S2", "supertrend_follow"),
    }
    agent = _mk_agent(positions, cap=3)
    safe, reason = agent._pre_trade_safety_checks(
        "S3", current_price=100.0, cost=200.0, strategy="supertrend_follow",
    )
    assert safe is True, reason


def test_cap_is_per_strategy_not_global():
    """A different strategy is allowed past the cap of the first."""
    positions = {
        "S1": _mk_position("S1", "supertrend_follow"),
        "S2": _mk_position("S2", "supertrend_follow"),
        "S3": _mk_position("S3", "supertrend_follow"),
    }
    agent = _mk_agent(positions, cap=3)
    # Same-strategy: blocked
    safe, _ = agent._pre_trade_safety_checks(
        "S4", current_price=100.0, cost=200.0, strategy="supertrend_follow",
    )
    assert safe is False
    # Different strategy: allowed (no positions of THAT strategy yet)
    safe, reason = agent._pre_trade_safety_checks(
        "S4", current_price=100.0, cost=200.0, strategy="rsi_momentum",
    )
    assert safe is True, reason


def test_cap_skipped_when_strategy_unknown():
    """Caller didn't supply strategy name (legacy code path): never block."""
    positions = {
        "S1": _mk_position("S1", "supertrend_follow"),
        "S2": _mk_position("S2", "supertrend_follow"),
        "S3": _mk_position("S3", "supertrend_follow"),
    }
    agent = _mk_agent(positions, cap=3)
    # No strategy passed → cap can't apply, safety check passes
    safe, reason = agent._pre_trade_safety_checks(
        "S4", current_price=100.0, cost=200.0,
    )
    assert safe is True, reason


def test_cap_replays_2026_05_14_pile_on():
    """Replay: with the live morning pile-on, would the cap have blocked
    CHOLAFIN at 11:07? Yes: by then 4 supertrend_follow shorts were open
    (CENTRALBK, TATACAP, OBEROIRLTY, JSWENERGY -- PCBL closed at 11:06,
    FEDERALBNK was the 4th still-open). Cap=3 would have rejected
    CHOLAFIN, saving the -Rs 196 stop-out at 11:51.
    """
    open_at_1107 = {
        "CENTRALBK": _mk_position("CENTRALBK", "supertrend_follow", qty=374, price=34.37),
        "TATACAP":   _mk_position("TATACAP",   "supertrend_follow", qty=41,  price=306.75),
        "OBEROIRLTY":_mk_position("OBEROIRLTY","supertrend_follow", qty=7,   price=1611.64),
        "JSWENERGY": _mk_position("JSWENERGY", "supertrend_follow", qty=25,  price=506.34),
        "FEDERALBNK":_mk_position("FEDERALBNK","supertrend_follow", qty=46,  price=279.16),
    }
    agent = _mk_agent(open_at_1107, cap=3)
    safe, reason = agent._pre_trade_safety_checks(
        "CHOLAFIN", current_price=1554.85, cost=8 * 1554.85,
        strategy="supertrend_follow",
    )
    assert safe is False
    assert "strategy_concurrency" in reason
    # Saved Rs 196 (CHOLAFIN stop-out at 1577.72, qty=8)
