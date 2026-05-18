"""P0 #1 (2026-05-15) — LIVE-MODE SAFETY: regression tests for the
coordinated-exit helper `TradingAgent._close_position_safely`.

Background
----------
Before this fix, every close path on `TradingAgent` did:

    1. place_order(flatten)        # broker now has BOTH flatten + SL-M live
    2. portfolio.close_position()  # in-memory close
    3. cancel_sl_order_for_symbol  # broker SL-M cancelled

The gap between (1) and (3) could last anywhere from milliseconds (paper
mode) to multiple seconds in real network conditions. During that window
an adverse tick that touched the SL trigger would fire BOTH the SL leg
and the intentional exit. Net effect: we'd close the position once and
then accidentally open the opposite-direction position on the same name.
The orphaned-SL hazard the SL registry was supposed to close was, in
practice, still wide open at every exit.

These tests pin the call ordering so the bug cannot regress silently.
"""
from __future__ import annotations

from typing import Any
import threading
from unittest import mock

import pytest


def _mk_agent_for_safe_exit(*, sl_tracked: bool, cancel_ok: bool, flatten_ok: bool):
    """Construct a stub `TradingAgent` exposing just the surface used by
    `_close_position_safely`. We use `__new__` to skip the real (heavy)
    constructor — full integration of the close paths is covered elsewhere.

    P0 #2 residual (2026-05-18): `_close_position_safely` now wraps its
    entire body in `self._exit_check_lock` (an RLock) and runs an
    idempotency check on `self.portfolio.positions[symbol]` before
    touching the broker. The stub MUST therefore initialise both, or
    every test in this file fails with AttributeError / KeyError that
    looks unrelated.
    """
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._exit_check_lock = threading.RLock()

    # Order of calls is recorded on the parent mock so we can assert
    # cancel-then-place ordering even though they live on different attrs.
    call_log: list[str] = []

    def _cancel(symbol):
        call_log.append("cancel_sl")
        return cancel_ok

    def _get_sl(symbol):
        if sl_tracked:
            return {"order_id": "SL-ORD-1", "trigger": 95.0, "side": "SELL"}
        return None

    def _place_order(**kw):
        call_log.append("place_order")
        if flatten_ok:
            return {"status": "FILLED", "filled_price": kw.get("price")}
        return None

    agent.execution = mock.Mock()
    agent.execution.cancel_sl_order_for_symbol.side_effect = _cancel
    agent.execution.get_sl_order_for_symbol.side_effect = _get_sl
    agent.execution.place_order.side_effect = _place_order

    # Portfolio close: returns a record with .pnl
    record = mock.Mock()
    record.pnl = 25.0
    record.symbol = "TESTSYM"

    agent.portfolio = mock.Mock()
    agent.portfolio.close_position.return_value = record
    # P0 #2 residual (2026-05-18): _close_position_safely now does an
    # idempotency re-check `if symbol not in self.portfolio.positions:
    # bail`. Seed the positions dict so the stub symbol is "open" at the
    # time of the call. Tests that want to assert the "already-closed"
    # branch can override this.
    agent.portfolio.positions = {"TESTSYM": mock.Mock(side="BUY", quantity=10)}

    agent.risk_manager = mock.Mock()
    agent.alert_manager = mock.Mock()

    # Bypass the real instance methods that fire downstream effects.
    agent._record_exit = mock.Mock()
    agent._on_trade_closed = mock.Mock()
    agent._persist_trailing_states = mock.Mock()

    return agent, call_log, record


# ── Call ordering ─────────────────────────────────────────────────────────


def test_cancel_sl_is_called_before_place_order_when_sl_tracked():
    """The P0 #1 invariant: cancel runs first, flatten second."""
    agent, call_log, _ = _mk_agent_for_safe_exit(
        sl_tracked=True, cancel_ok=True, flatten_ok=True,
    )
    order, record = agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    assert order is not None
    assert record is not None
    assert call_log == ["cancel_sl", "place_order"], (
        f"Expected cancel-then-place, got {call_log}. "
        "P0 #1 regression: flatten happening before SL cancel reopens the "
        "double-fire race window."
    )


def test_cancel_sl_is_called_before_place_order_even_when_no_sl_tracked():
    """Paper mode / pre-SL-tracking entry — cancel is still called first
    (cheap no-op) so the ordering invariant is uniform."""
    agent, call_log, _ = _mk_agent_for_safe_exit(
        sl_tracked=False, cancel_ok=True, flatten_ok=True,
    )
    agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    assert call_log == ["cancel_sl", "place_order"]


# ── Downstream state updates ──────────────────────────────────────────────


def test_portfolio_closed_and_trade_recorded_on_happy_path():
    agent, _, record = _mk_agent_for_safe_exit(
        sl_tracked=True, cancel_ok=True, flatten_ok=True,
    )
    order, ret_record = agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    assert order is not None
    assert ret_record is record
    agent.portfolio.close_position.assert_called_once()
    agent.risk_manager.record_trade.assert_called_once_with(25.0)
    agent.risk_manager.remove_trailing_stop.assert_called_once_with("TESTSYM")
    agent._record_exit.assert_called_once()
    agent._on_trade_closed.assert_called_once_with(record)


# ── Failure-path invariants ───────────────────────────────────────────────


def test_flatten_failure_after_successful_cancel_fires_critical_alert():
    """The new edge case the helper has to handle: we cancelled the broker
    SL, then the flatten fails. The position is now naked. Helper must:
      • NOT update portfolio or risk state (no record_trade).
      • Fire a CRITICAL alert so ops can intervene.
      • Return (None, None).
    """
    agent, call_log, _ = _mk_agent_for_safe_exit(
        sl_tracked=True, cancel_ok=True, flatten_ok=False,
    )
    order, record = agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    assert order is None
    assert record is None
    assert call_log == ["cancel_sl", "place_order"]
    agent.portfolio.close_position.assert_not_called()
    agent.risk_manager.record_trade.assert_not_called()
    # Critical-level alert with "Naked position" in the title
    agent.alert_manager.send_alert.assert_called_once()
    title_arg = agent.alert_manager.send_alert.call_args.args[0]
    assert "Naked" in title_arg or "CRITICAL" in title_arg, title_arg
    kwargs = agent.alert_manager.send_alert.call_args.kwargs
    assert kwargs.get("level") == "critical"


def test_cancel_failure_does_not_block_flatten():
    """If broker cancel itself fails (e.g. order in terminal state already
    or transient API hiccup), we still place the flatten — a stuck broker
    SL is the lesser evil compared to a stuck open position."""
    agent, call_log, _ = _mk_agent_for_safe_exit(
        sl_tracked=True, cancel_ok=False, flatten_ok=True,
    )
    order, record = agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    assert order is not None
    assert record is not None
    assert call_log == ["cancel_sl", "place_order"]


def test_cancel_failure_does_not_alert_critical_naked():
    """When cancel fails, broker SL is still in place — the position is
    NOT naked. The naked-position alert is reserved for the (cancel_ok &
    flatten_fail) case. Otherwise ops alerts get spammed."""
    agent, _, _ = _mk_agent_for_safe_exit(
        sl_tracked=True, cancel_ok=False, flatten_ok=True,
    )
    agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    # No alert should fire on success-with-cancel-failure
    agent.alert_manager.send_alert.assert_not_called()


def test_no_alert_when_no_sl_was_tracked_and_flatten_fails():
    """No tracked SL means no orphan to worry about. Flatten failure is a
    normal exit-attempt failure and should NOT fire the naked-position
    alert (that alert is specifically for the cancel_ok+flatten_fail
    edge case)."""
    agent, _, _ = _mk_agent_for_safe_exit(
        sl_tracked=False, cancel_ok=True, flatten_ok=False,
    )
    order, record = agent._close_position_safely(
        symbol="TESTSYM", token="999", exit_side="SELL",
        quantity=10, price=100.0, tag="signal", exit_reason="signal",
    )
    assert order is None
    assert record is None
    agent.alert_manager.send_alert.assert_not_called()
