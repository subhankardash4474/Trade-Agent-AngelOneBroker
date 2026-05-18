"""Tests for the 2026-05-07 trading-agent fixes:

1. Opening-bar lockout: blocks new opens during the first N minutes of
   the session but lets exits flow through.
2. Carryover profit-locking: at 15:10 IST, closes profitable carryover
   positions to avoid CROMPTON-style overnight gap risk.
3. Carryover-lock once-per-day flag resets on date change.

These tests freeze time and mock the data handler so they don't hit the
network. They construct a TradingAgent indirectly through helper builders
that bypass the full `__init__` (which needs a live broker session).
"""

from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timedelta, date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytz

IST = pytz.timezone("Asia/Kolkata")


def _make_minimal_agent():
    """Build a TradingAgent shell with just the attrs the tested methods read.

    We deliberately don't run TradingAgent.__init__ here — that would
    require a live broker session, config wiring, etc. The methods under
    test (`_is_in_opening_lockout`, `_maybe_carryover_profit_lock`) only
    touch a small subset of self-state that we set up by hand.
    """
    from trading_agent import TradingAgent
    a = TradingAgent.__new__(TradingAgent)
    a._market_open_time = dtime(9, 15)
    a._opening_lockout_minutes = 15
    a._carryover_lock_time = dtime(15, 10)
    a._carryover_lock_min_profit = 0.0
    a._carryover_lock_done = False
    a._carryover_lock_done_date = None
    return a


# ---------- opening lockout ----------

class TestOpeningLockout:
    def test_during_lockout_window(self):
        a = _make_minimal_agent()
        t = IST.localize(datetime(2026, 5, 8, 9, 20, 0))
        assert a._is_in_opening_lockout(t) is True

    def test_just_after_market_open(self):
        a = _make_minimal_agent()
        t = IST.localize(datetime(2026, 5, 8, 9, 15, 0))
        # 9:15:00 sharp is INSIDE the [9:15, 9:30) window
        assert a._is_in_opening_lockout(t) is True

    def test_at_window_end_is_open(self):
        a = _make_minimal_agent()
        t = IST.localize(datetime(2026, 5, 8, 9, 30, 0))
        # 9:30:00 is the boundary, treated as closed (>=)
        assert a._is_in_opening_lockout(t) is False

    def test_before_market_open_not_locked(self):
        a = _make_minimal_agent()
        t = IST.localize(datetime(2026, 5, 8, 9, 10, 0))
        assert a._is_in_opening_lockout(t) is False

    def test_disabled_with_zero_minutes(self):
        a = _make_minimal_agent()
        a._opening_lockout_minutes = 0
        t = IST.localize(datetime(2026, 5, 8, 9, 20, 0))
        assert a._is_in_opening_lockout(t) is False


# ---------- carryover profit-lock ----------

class _FakePosition:
    def __init__(self, side: str, entry_price: float, qty: int,
                 entry_dt: datetime):
        self.side = side
        self.entry_price = entry_price
        self.quantity = qty
        self.entry_time = entry_dt

    def unrealized_pnl(self, price: float) -> float:
        if self.side == "BUY":
            return (price - self.entry_price) * self.quantity
        return (self.entry_price - price) * self.quantity


class _FakePortfolio:
    def __init__(self, positions: dict):
        self.positions = positions

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    def close_position(self, symbol, exit_price, exit_reason):
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        pnl = pos.unrealized_pnl(exit_price)
        return SimpleNamespace(
            symbol=symbol, side=pos.side, quantity=pos.quantity,
            entry_price=pos.entry_price, exit_price=exit_price,
            entry_time=pos.entry_time, exit_time=datetime.now(IST),
            pnl=pnl, pnl_pct=pnl / (pos.entry_price * pos.quantity) * 100,
            strategy="test", exit_reason=exit_reason, commission=0.0,
        )


def _wire_agent_for_carryover(closes_collected: list,
                               positions: dict | None = None,
                               *, before_lock_time: bool = False,
                               carryover_done: bool = False):
    import threading

    a = _make_minimal_agent()
    a._carryover_lock_done = carryover_done
    a.portfolio = _FakePortfolio(positions or {})
    a.execution = MagicMock()
    a.execution.place_order = MagicMock(
        return_value={"status": "FILLED", "filled_price": None}
    )
    # P0 #2 residual (2026-05-18): _close_position_safely now wraps in
    # self._exit_check_lock and re-checks portfolio.positions for
    # idempotency. Initialise both so the carryover-lock path doesn't
    # blow up with AttributeError before reaching the assertion.
    a._exit_check_lock = threading.RLock()
    a._persist_trailing_states = MagicMock()
    a.risk_manager = MagicMock()
    a.alert_manager = MagicMock()
    a._record_exit = MagicMock()
    a._on_trade_closed = lambda rec: closes_collected.append(rec)
    a._get_token = MagicMock(return_value="TOKEN")
    return a


class TestCarryoverProfitLock:
    def test_carryover_in_profit_is_closed(self):
        yesterday = IST.localize(datetime(2026, 5, 7, 14, 30))
        positions = {
            "CROMPTON": _FakePosition("SELL", 285.83, 28, yesterday),
        }
        closes: list = []
        a = _wire_agent_for_carryover(closes, positions)

        # Stub now() to be 15:15 today (past the 15:10 lock time).
        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 15, 15))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            current_prices = {"CROMPTON": 280.00}  # +Rs 28 favourable
            a._maybe_carryover_profit_lock(current_prices)

        assert "CROMPTON" not in a.portfolio.positions
        assert len(closes) == 1
        assert closes[0].pnl > 0
        assert closes[0].exit_reason == "carryover_profit_lock"

    def test_intraday_position_is_skipped(self):
        today_ist = datetime(2026, 5, 8, 10, 30)
        today = IST.localize(today_ist)
        positions = {
            "FRESHTRADE": _FakePosition("SELL", 100.00, 10, today),
        }
        closes: list = []
        a = _wire_agent_for_carryover(closes, positions)

        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 15, 15))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            current_prices = {"FRESHTRADE": 95.00}  # in profit, but intraday
            a._maybe_carryover_profit_lock(current_prices)

        assert "FRESHTRADE" in a.portfolio.positions  # not closed
        assert len(closes) == 0

    def test_carryover_at_loss_is_skipped(self):
        yesterday = IST.localize(datetime(2026, 5, 7, 14, 30))
        positions = {
            "LOSER": _FakePosition("SELL", 100.00, 10, yesterday),
        }
        closes: list = []
        a = _wire_agent_for_carryover(closes, positions)

        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 15, 15))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            current_prices = {"LOSER": 105.00}  # SHORT at loss
            a._maybe_carryover_profit_lock(current_prices)

        assert "LOSER" in a.portfolio.positions  # held - let SL/TP handle

    def test_runs_only_once_per_day(self):
        yesterday = IST.localize(datetime(2026, 5, 7, 14, 30))
        positions = {
            "CARRY": _FakePosition("SELL", 100.00, 10, yesterday),
        }
        closes: list = []
        a = _wire_agent_for_carryover(closes, positions)

        # First call: closes the carryover.
        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 15, 15))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            a._maybe_carryover_profit_lock({"CARRY": 95.0})

        assert a._carryover_lock_done is True
        assert len(closes) == 1

        # Second call same day: no-op even with new positions.
        a.portfolio.positions["CARRY2"] = _FakePosition(
            "SELL", 100.00, 10, yesterday)
        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 15, 20))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            a._maybe_carryover_profit_lock({"CARRY2": 95.0})

        assert "CARRY2" in a.portfolio.positions  # second-call no-op
        assert len(closes) == 1  # still only one close

    def test_resets_across_day_boundary(self):
        a = _make_minimal_agent()
        a._carryover_lock_done = True
        a._carryover_lock_done_date = date(2026, 5, 7)
        a.portfolio = _FakePortfolio({})

        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 15, 15))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            a._maybe_carryover_profit_lock({})

        # New day -> flag reset to False (then re-set True if no positions
        # to close, see implementation: short-circuits to True immediately)
        assert a._carryover_lock_done_date == date(2026, 5, 8)

    def test_skipped_before_lock_time(self):
        yesterday = IST.localize(datetime(2026, 5, 7, 14, 30))
        positions = {
            "EARLY": _FakePosition("SELL", 100.00, 10, yesterday),
        }
        closes: list = []
        a = _wire_agent_for_carryover(closes, positions)

        # 14:30 < 15:10 -> nothing should happen.
        with patch("trading_agent.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=IST.localize(datetime(2026, 5, 8, 14, 30))
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            a._maybe_carryover_profit_lock({"EARLY": 95.0})

        assert "EARLY" in a.portfolio.positions
        assert a._carryover_lock_done is False  # not yet
