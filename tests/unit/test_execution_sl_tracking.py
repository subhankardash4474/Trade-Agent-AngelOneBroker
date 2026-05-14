"""Tests for the broker-side SL tracking + trail-propagation API
added 2026-05-14 (P0 LIVE-MODE SAFETY).

Background: prior to this change, `_place_sl_order` placed a standing
SL-M on AngelOne at entry time but the agent never tracked, modified,
or cancelled it. Every signal-exit / trailing / peak-giveback / square-
off path closed the position via a fresh order and *left the standing
SL-M as an orphan*. If LTP later touched the original trigger, the
broker would open an unintended reverse position.

These tests cover the new ExecutionEngine surface:
  * `_sl_orders_by_symbol` is populated on entry when SL is provided.
  * `cancel_sl_order_for_symbol` cancels and forgets the tracked id.
  * `update_sl_trigger_for_symbol` propagates trail SL changes to the
    broker via `modify_stop_loss` and updates the cache.
  * Idempotency: a no-op trigger update doesn't bother the broker.
  * Failure modes: a broker rejection during cancel re-tracks the id.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.execution import ExecutionEngine


def _live_engine_with_mock_api(api_mock):
    cfg = {
        "broker": {"mode": "live"},
        "execution": {"order_type": "LIMIT", "product_type": "INTRADAY"},
        "market": {"exchange": "NSE"},
    }
    return ExecutionEngine(cfg, smart_api=api_mock)


# ── Tracking on entry ─────────────────────────────────────────────────────


def test_entry_with_sl_tracks_broker_order_id():
    api = MagicMock()
    # First placeOrder = entry order, second = SL-M
    api.placeOrder.side_effect = ["ENTRY-ORD-1", "SL-ORD-1"]
    eng = _live_engine_with_mock_api(api)

    res = eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    assert res is not None
    tracked = eng.get_sl_order_for_symbol("HDFCBANK")
    assert tracked is not None
    assert tracked["order_id"] == "SL-ORD-1"
    assert tracked["trigger"] == pytest.approx(1485.0)
    assert tracked["side"] == "SELL"   # opposite of BUY entry


def test_short_entry_tracks_buy_side_sl():
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-ORD-2", "SL-ORD-2"]
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="SELL",
        quantity=10, price=1500.0, stop_loss=1515.0,
    )
    tracked = eng.get_sl_order_for_symbol("HDFCBANK")
    assert tracked["side"] == "BUY"


def test_entry_without_sl_does_not_track():
    api = MagicMock()
    api.placeOrder.return_value = "ENTRY-ORD-3"
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0,
    )
    assert eng.get_sl_order_for_symbol("HDFCBANK") is None


# ── Cancellation on close ─────────────────────────────────────────────────


def test_cancel_sl_order_for_symbol_calls_broker():
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-1", "SL-1"]
    api.cancelOrder.return_value = "OK"
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    ok = eng.cancel_sl_order_for_symbol("HDFCBANK")
    assert ok is True
    # Verify broker was asked to cancel the tracked id
    api.cancelOrder.assert_called_once_with("SL-1", "NORMAL")
    # Tracking cleared
    assert eng.get_sl_order_for_symbol("HDFCBANK") is None


def test_cancel_unknown_symbol_is_noop():
    api = MagicMock()
    eng = _live_engine_with_mock_api(api)
    assert eng.cancel_sl_order_for_symbol("NEVERTRACKED") is True
    api.cancelOrder.assert_not_called()


def test_failed_cancel_retracks_id_for_retry():
    """If the broker refuses to cancel (transient failure), the agent
    must remember the id so a later retry doesn't abandon a live SL."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-1", "SL-1"]
    api.cancelOrder.return_value = None    # broker refuses
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    ok = eng.cancel_sl_order_for_symbol("HDFCBANK")
    assert ok is False
    # Critical: id must still be tracked so a retry has something to call
    tracked = eng.get_sl_order_for_symbol("HDFCBANK")
    assert tracked is not None
    assert tracked["order_id"] == "SL-1"


# ── Trail propagation ─────────────────────────────────────────────────────


def test_update_sl_trigger_calls_modify_on_broker():
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-1", "SL-1"]
    api.modifyOrder.return_value = "OK"
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    # Trail moves SL up
    ok = eng.update_sl_trigger_for_symbol("HDFCBANK", 1492.0)
    assert ok is True
    # modifyOrder called with the new trigger
    api.modifyOrder.assert_called_once()
    args = api.modifyOrder.call_args[0][0]
    assert args["orderid"] == "SL-1"
    assert args["triggerprice"] == "1492.0"
    # Cache updated
    assert eng.get_sl_order_for_symbol("HDFCBANK")["trigger"] == pytest.approx(1492.0)


def test_update_sl_trigger_idempotent_when_trigger_unchanged():
    """If we'd push the same trigger twice, the second call is a no-op
    (no broker round-trip). Critical for the trail loop which fires
    every tick / poll cycle."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-1", "SL-1"]
    api.modifyOrder.return_value = "OK"
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    eng.update_sl_trigger_for_symbol("HDFCBANK", 1492.0)
    eng.update_sl_trigger_for_symbol("HDFCBANK", 1492.0)   # repeat
    # modifyOrder called exactly once; second call short-circuited.
    assert api.modifyOrder.call_count == 1


def test_update_sl_trigger_unknown_symbol_returns_false():
    api = MagicMock()
    eng = _live_engine_with_mock_api(api)
    assert eng.update_sl_trigger_for_symbol("NOTRACKED", 100.0) is False
    api.modifyOrder.assert_not_called()


def test_list_tracked_sl_orders_returns_copy():
    """Defensive: the introspection accessor must not let callers mutate
    internal state (else heartbeat code could accidentally drop a tracked
    SL by .pop'ing the dict)."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-1", "SL-1"]
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    snapshot = eng.list_tracked_sl_orders()
    snapshot.pop("HDFCBANK", None)
    # Internal state still has it
    assert eng.get_sl_order_for_symbol("HDFCBANK") is not None


# ── Paper mode is always a no-op (no live broker to call) ─────────────────


def test_paper_mode_does_not_call_broker_for_sl():
    """In paper mode the SL is enforced in-process; we never place a
    standing SL-M on the (nonexistent) broker, so tracking stays empty."""
    cfg = {
        "broker": {"mode": "paper"},
        "execution": {"order_type": "LIMIT", "product_type": "INTRADAY"},
        "market": {"exchange": "NSE"},
    }
    eng = ExecutionEngine(cfg, smart_api=None)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    assert eng.get_sl_order_for_symbol("HDFCBANK") is None
    # Cancel is also a no-op
    assert eng.cancel_sl_order_for_symbol("HDFCBANK") is True
