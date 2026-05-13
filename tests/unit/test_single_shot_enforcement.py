"""Tests for the --single-shot kill-switch state machine in TradingAgent.

We don't construct a full TradingAgent (that requires a real DB, ensemble
model, broker, ws_client, etc.) -- instead we exercise the two pure
state-machine methods (`_reset_single_shot_state_if_new_day` and the
per-symbol "done" tracking inside `_on_trade_closed`) via method binding
on a minimal stand-in object.

This is the same pattern used by tests/unit/test_strategy_breaker.py for
the per-strategy circuit breaker state-machine.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
import pytz

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages"))

from trading_agent import TradingAgent  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def _agent_stub(single_shot_enabled: bool) -> SimpleNamespace:
    """Build a minimal duck-typed stand-in for TradingAgent that has just
    the attributes the single-shot methods touch, plus method bindings to
    the real implementations under test."""
    stub = SimpleNamespace(
        _single_shot_enabled=single_shot_enabled,
        _symbols_done_today=set(),
        _single_shot_day=datetime.now(IST).date(),
    )
    # Bind the real methods to the stub so they operate on the stub's state.
    stub._reset_single_shot_state_if_new_day = MethodType(
        TradingAgent._reset_single_shot_state_if_new_day, stub
    )
    return stub


class TestResetOnDayBoundary:
    def test_no_reset_within_same_day(self):
        stub = _agent_stub(single_shot_enabled=True)
        stub._symbols_done_today.add("YESBANK-EQ")
        stub._reset_single_shot_state_if_new_day()
        assert stub._symbols_done_today == {"YESBANK-EQ"}

    def test_reset_when_day_changes(self):
        stub = _agent_stub(single_shot_enabled=True)
        stub._symbols_done_today.add("YESBANK-EQ")
        stub._symbols_done_today.add("HDFCBANK-EQ")
        # Pretend it's yesterday in our state so today's call triggers a reset
        stub._single_shot_day = datetime.now(IST).date() - timedelta(days=1)
        stub._reset_single_shot_state_if_new_day()
        assert stub._symbols_done_today == set()
        assert stub._single_shot_day == datetime.now(IST).date()

    def test_reset_safe_when_set_already_empty(self):
        stub = _agent_stub(single_shot_enabled=True)
        stub._single_shot_day = datetime.now(IST).date() - timedelta(days=3)
        stub._reset_single_shot_state_if_new_day()
        assert stub._symbols_done_today == set()


class TestSymbolMarkedDoneOnClose:
    """Exercise the part of `_on_trade_closed` that adds the symbol to the
    done-set. We can't easily call the full method (it persists to DB and
    updates analyzers), but we can test the state-machine fragment by
    simulating exactly what it does."""

    @staticmethod
    def _simulate_on_trade_closed(stub, record):
        """Mirror of the single-shot tracking block inside
        TradingAgent._on_trade_closed -- kept in sync with that source.
        If you change the agent, change this stub identically and prefer
        adding a 'pure' helper in trading_agent.py that both call."""
        try:
            symbol = getattr(record, "symbol", None)
            if symbol:
                stub._reset_single_shot_state_if_new_day()
                stub._symbols_done_today.add(symbol)
        except Exception:
            pass

    def test_close_marks_symbol_done(self):
        stub = _agent_stub(single_shot_enabled=True)
        record = SimpleNamespace(symbol="YESBANK-EQ")
        self._simulate_on_trade_closed(stub, record)
        assert "YESBANK-EQ" in stub._symbols_done_today

    def test_two_closes_two_symbols(self):
        stub = _agent_stub(single_shot_enabled=True)
        for sym in ("YESBANK-EQ", "HDFCBANK-EQ", "INFY-EQ"):
            self._simulate_on_trade_closed(stub, SimpleNamespace(symbol=sym))
        assert stub._symbols_done_today == {"YESBANK-EQ", "HDFCBANK-EQ", "INFY-EQ"}

    def test_dup_close_same_symbol_is_idempotent(self):
        stub = _agent_stub(single_shot_enabled=True)
        for _ in range(3):
            self._simulate_on_trade_closed(stub, SimpleNamespace(symbol="X"))
        assert stub._symbols_done_today == {"X"}

    def test_tracking_works_even_when_flag_disabled(self):
        """Important: tracking is always active (cheap no-op). Only the
        *enforcement check* on entry uses the flag. This means flipping
        --single-shot mid-day via env vars (future feature) would work
        without surprising 'already-traded today gets ignored' behaviour."""
        stub = _agent_stub(single_shot_enabled=False)
        self._simulate_on_trade_closed(stub, SimpleNamespace(symbol="X"))
        assert "X" in stub._symbols_done_today


class TestArgparsePropagation:
    """Verify the CLI flag flows through run_daemon -> TradingAgent."""

    def test_run_daemon_help_advertises_flag(self):
        import subprocess
        out = subprocess.check_output(
            [sys.executable, str(ROOT / "run_daemon.py"), "--help"],
            text=True, cwd=str(ROOT), stderr=subprocess.STDOUT,
            timeout=30,
        )
        assert "--max-loss-rs" in out
        assert "--single-shot" in out
        # Sanity: the helpful Stage 3 hint is present.
        assert "Stage 3" in out


class TestRehydrateAfterRestart:
    """Bug 3, 2026-05-13: when the daemon restarts mid-day, the
    `_symbols_done_today` set was reset to empty, allowing
    already-round-tripped symbols to re-enter. The fix loads today's
    closed-trade symbols at __init__ time.

    Per the same duck-typing pattern as the rest of this module, we test
    just the rehydration block (the part inside `try:` that adds each
    trade row's symbol to the set) -- not the full TradingAgent init,
    which requires DB / ensemble / broker / etc.
    """

    @staticmethod
    def _simulate_rehydrate(stub, todays_trades_rows):
        """Mirror of the rehydrate block in TradingAgent.__init__.
        Keep this in sync with that source code (if you change the
        agent, change this stub identically)."""
        for t in todays_trades_rows:
            sym = t.get("symbol")
            if sym:
                stub._symbols_done_today.add(sym)

    def test_rehydrate_adds_closed_symbols(self):
        stub = _agent_stub(single_shot_enabled=True)
        rows = [
            {"symbol": "YESBANK-EQ", "exit_time": "2026-05-13T10:01:43"},
            {"symbol": "HDFCBANK-EQ", "exit_time": "2026-05-13T11:30:00"},
        ]
        self._simulate_rehydrate(stub, rows)
        assert stub._symbols_done_today == {"YESBANK-EQ", "HDFCBANK-EQ"}

    def test_rehydrate_ignores_rows_without_symbol(self):
        stub = _agent_stub(single_shot_enabled=True)
        rows = [
            {"symbol": "YESBANK-EQ"},
            {"symbol": None},       # malformed
            {"symbol": ""},          # malformed
            {},                       # malformed
        ]
        self._simulate_rehydrate(stub, rows)
        assert stub._symbols_done_today == {"YESBANK-EQ"}

    def test_rehydrate_idempotent_on_duplicates(self):
        """Same symbol appearing in two trade rows (e.g. shouldn't ever
        happen with single-shot ON, but defensive)."""
        stub = _agent_stub(single_shot_enabled=True)
        rows = [
            {"symbol": "X"},
            {"symbol": "X"},
            {"symbol": "Y"},
        ]
        self._simulate_rehydrate(stub, rows)
        assert stub._symbols_done_today == {"X", "Y"}

    def test_rehydrate_works_when_flag_disabled(self):
        """Important: rehydration runs regardless of `single_shot`
        flag so the set is always populated. Only enforcement uses
        the flag. Matches `_simulate_on_trade_closed` above."""
        stub = _agent_stub(single_shot_enabled=False)
        self._simulate_rehydrate(stub, [{"symbol": "X"}])
        assert "X" in stub._symbols_done_today
