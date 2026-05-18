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


# ── P0 #3 (2026-05-15) — Entry-success / SL-fail rollback ─────────────────
#
# The compound entry+SL flow used to return the entry result dict even when
# `_place_sl_order` returned None. Caller (trading_agent) would then record
# the position as "open with protection" while broker reality was a NAKED
# position with no stop. A single margin glitch / API throttle = unhedged
# exposure with uncapped downside. The fix below pins the rollback path.


def test_sl_failure_after_entry_returns_none_and_does_not_track():
    """Bug repro: when placeOrder returns the entry id but the SECOND
    placeOrder (the SL leg) returns falsy, the engine must NOT pretend
    the compound succeeded."""
    api = MagicMock()
    # First call = entry succeeds, second call = SL placement fails (None)
    api.placeOrder.side_effect = ["ENTRY-ORD-1", None]
    eng = _live_engine_with_mock_api(api)

    res = eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    assert res is None, (
        "P0 #3 regression: engine claimed entry succeeded even though the "
        "SL leg failed. Caller will record a 'protected' position while the "
        "broker has a naked one."
    )
    # And no SL is tracked (would be a stale registration)
    assert eng.get_sl_order_for_symbol("HDFCBANK") is None


def test_sl_failure_triggers_counter_flatten_market_order():
    """The naked position MUST be unwound via a counter market order on
    the opposite side. Manual intervention is too slow for a live tick."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-ORD-1", None, "FLATTEN-ORD-1"]
    eng = _live_engine_with_mock_api(api)

    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )

    # 3 placeOrder calls total: entry, SL, counter-flatten
    assert api.placeOrder.call_count == 3
    # The third call is the counter-flatten, with opposite side, MARKET order
    third_call_params = api.placeOrder.call_args_list[2].args[0]
    assert third_call_params["transactiontype"] == "SELL"   # opposite of BUY entry
    assert third_call_params["ordertype"] == "MARKET"
    assert third_call_params["tradingsymbol"] == "HDFCBANK"
    assert third_call_params["quantity"] == "10"


def test_short_entry_sl_failure_flattens_with_buy():
    """Mirror image: SHORT entry with failed SL must counter-flatten BUY."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-ORD-S", None, "FLATTEN-ORD-S"]
    eng = _live_engine_with_mock_api(api)

    res = eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="SELL",
        quantity=10, price=1500.0, stop_loss=1515.0,
    )
    assert res is None
    counter = api.placeOrder.call_args_list[2].args[0]
    assert counter["transactiontype"] == "BUY"
    assert counter["ordertype"] == "MARKET"


def test_sl_failure_cleans_pending_order_tracking():
    """The failed compound's tracking artifacts must be cleaned so the
    caller's view of `list_tracked_sl_orders` / pending-orders is correct
    after the rollback."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-ORD-1", None, "FLATTEN-ORD-1"]
    eng = _live_engine_with_mock_api(api)

    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )

    # Failed entry should not appear as a pending order
    assert "ENTRY-ORD-1" not in eng._pending_orders
    # And no SL tracked
    assert eng.list_tracked_sl_orders() == {}


def test_counter_flatten_failure_still_returns_none():
    """Worst case: SL placement fails AND counter-flatten also fails (or
    throws). The engine must STILL return None — never claim success —
    and the CRITICAL logs are the only signal ops has."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-ORD-1", None, Exception("network down")]
    eng = _live_engine_with_mock_api(api)

    res = eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    assert res is None
    # All three calls were attempted
    assert api.placeOrder.call_count == 3


def test_entry_without_sl_request_unaffected_by_rollback():
    """If the caller never asked for an SL (stop_loss=None), the rollback
    path is bypassed entirely and the entry returns successfully."""
    api = MagicMock()
    api.placeOrder.return_value = "ENTRY-ORD-1"
    eng = _live_engine_with_mock_api(api)

    res = eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0,   # no stop_loss
    )
    assert res is not None
    assert res["order_id"] == "ENTRY-ORD-1"
    # Single placeOrder call (entry only — no SL, no flatten)
    assert api.placeOrder.call_count == 1


# ── P0 #4 (2026-05-15) — Restart-time SL reconciliation ────────────────────
#
# After daemon restart, `_sl_orders_by_symbol` is empty even though broker
# still has the original SL-M live. Without `reconcile_sl_orders_from_broker`,
# `update_sl_trigger_for_symbol` silently no-ops, and `cancel_sl_order_for_symbol`
# silently no-ops, leaving the orphaned SL behind. These tests pin the new
# reconciliation contract.


class _StubPos:
    def __init__(self, symbol, side, quantity, entry_price, stop_loss=None):
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.entry_price = entry_price
        self.stop_loss = stop_loss


def test_reconcile_finds_matching_sl_and_registers():
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [
            {
                "orderid": "SL-RECON-1",
                "tradingsymbol": "HDFCBANK",
                "transactiontype": "SELL",
                "ordertype": "SL-M",
                "status": "trigger pending",
                "triggerprice": "1485.0",
                "quantity": "10",
                "symboltoken": "123",
            },
        ],
    }
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)

    assert report == {"HDFCBANK": "reconciled"}
    tracked = eng.get_sl_order_for_symbol("HDFCBANK")
    assert tracked is not None
    assert tracked["order_id"] == "SL-RECON-1"
    assert tracked["trigger"] == pytest.approx(1485.0)
    assert tracked["side"] == "SELL"
    assert tracked["quantity"] == 10


def test_reconcile_flags_unprotected_when_no_matching_sl():
    """Position restored from DB but broker has NO live SL for it (e.g.
    the SL filled while the daemon was down). Must be flagged so ops
    sees it, but the engine still boots."""
    api = MagicMock()
    api.orderBook.return_value = {"status": True, "data": []}
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "unprotected"}
    assert eng.get_sl_order_for_symbol("HDFCBANK") is None


def test_reconcile_skips_wrong_side_sl():
    """A broker SL-M with the SAME side as the position (e.g. SELL SL for
    a SHORT position) is NOT the protective leg; ignore it."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-WRONGSIDE",
            "tradingsymbol": "HDFCBANK",
            "transactiontype": "BUY",   # same as BUY position — wrong side
            "ordertype": "SL-M",
            "status": "trigger pending",
            "triggerprice": "1485.0",
            "quantity": "10",
            "symboltoken": "123",
        }],
    }
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "unprotected"}


def test_reconcile_skips_terminal_status_sl():
    """A completed / cancelled / rejected SL is not 'live'. Must skip."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-DONE",
            "tradingsymbol": "HDFCBANK",
            "transactiontype": "SELL",
            "ordertype": "SL-M",
            "status": "complete",   # already filled
            "triggerprice": "1485.0",
            "quantity": "10",
            "symboltoken": "123",
        }],
    }
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "unprotected"}


def test_reconcile_skips_non_sl_orders():
    """Plain LIMIT / MARKET orders in the orderbook must be ignored even if
    they're on the right symbol with the right side."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "LIMIT-1",
            "tradingsymbol": "HDFCBANK",
            "transactiontype": "SELL",
            "ordertype": "LIMIT",   # not an SL-M
            "status": "open",
            "triggerprice": "0",
            "quantity": "10",
            "symboltoken": "123",
        }],
    }
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "unprotected"}


def test_reconcile_handles_multiple_positions():
    """One reconciled, one orphan: must report both, must register the
    reconciled one, must NOT touch the unprotected one."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-HDFC",
            "tradingsymbol": "HDFCBANK",
            "transactiontype": "SELL",
            "ordertype": "SL-M",
            "status": "trigger pending",
            "triggerprice": "1485.0",
            "quantity": "10",
            "symboltoken": "123",
        }],
    }
    eng = _live_engine_with_mock_api(api)
    positions = {
        "HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0),
        "INFY":     _StubPos("INFY", "BUY", 5, 1400.0),  # no broker SL
    }
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "reconciled", "INFY": "unprotected"}
    assert eng.get_sl_order_for_symbol("HDFCBANK") is not None
    assert eng.get_sl_order_for_symbol("INFY") is None


def test_reconcile_short_position_finds_buy_side_sl():
    """SHORT positions are protected by BUY-side SL-M. Mirror of the long case."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-SHORT",
            "tradingsymbol": "HDFCBANK",
            "transactiontype": "BUY",   # buy-side SL for a short
            "ordertype": "SL-M",
            "status": "trigger pending",
            "triggerprice": "1515.0",
            "quantity": "10",
            "symboltoken": "123",
        }],
    }
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "SELL", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "reconciled"}
    assert eng.get_sl_order_for_symbol("HDFCBANK")["side"] == "BUY"


def test_reconcile_handles_broker_api_failure():
    """If orderBook() throws, we must NOT crash the boot; flag every
    position as unprotected and let the daemon proceed."""
    api = MagicMock()
    api.orderBook.side_effect = Exception("connection refused")
    eng = _live_engine_with_mock_api(api)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "unprotected"}


def test_reconcile_in_paper_mode_is_noop():
    """Paper mode has no broker to query; reconciliation must short-circuit
    and report skipped_paper for each position. Tracking stays empty."""
    cfg = {
        "broker": {"mode": "paper"},
        "execution": {"order_type": "LIMIT", "product_type": "INTRADAY"},
        "market": {"exchange": "NSE"},
    }
    eng = ExecutionEngine(cfg, smart_api=None)
    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)
    assert report == {"HDFCBANK": "skipped_paper"}
    assert eng.get_sl_order_for_symbol("HDFCBANK") is None


def test_reconcile_empty_positions_still_sweeps_orphans():
    """P0 #3 residual (2026-05-18): even with NO restored positions, we MUST
    query the broker order book to detect any live SL-M left behind (e.g.
    a previous run crashed AFTER the SL placed but BEFORE the open was
    persisted). The pre-residual contract returned early here, which left
    every orphan SL-M live on the broker indefinitely.
    """
    api = MagicMock()
    api.orderBook.return_value = {"status": True, "data": []}
    eng = _live_engine_with_mock_api(api)
    report = eng.reconcile_sl_orders_from_broker({})
    assert report == {}
    api.orderBook.assert_called_once()


# ── P0 #3 residual (2026-05-18): orphan + duplicate SL sweep ─────────────────
#
# The previous reconciliation only looked at restored positions. Any live
# SL-M on the broker that did NOT correspond to a restored position was
# silently ignored — it stayed live, ready to fire later and open an
# unintended reverse trade against an empty book. Multiple SL-Ms for the
# same symbol (duplicates from a previous bug, or two entries that both
# placed SL legs) had the same problem: only the first was used, the rest
# stayed live.
#
# These tests pin the new contract: orphans + duplicates get cancelled,
# CRITICAL-logged, and surfaced in the report so ops can audit.


def test_reconcile_cancels_orphan_sl_for_unknown_symbol():
    """Broker has a live SL-M for RELIANCE but the agent's DB has NO
    RELIANCE position. The orphan must be cancelled, not silently
    ignored — otherwise the next price touch opens an unintended
    reverse position."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-ORPHAN-1",
            "tradingsymbol": "RELIANCE",
            "transactiontype": "SELL",
            "ordertype": "SL-M",
            "status": "trigger pending",
            "triggerprice": "2800.0",
            "quantity": "5",
            "symboltoken": "2885",
        }],
    }
    api.cancelOrder.return_value = "CXL-OK"
    eng = _live_engine_with_mock_api(api)

    report = eng.reconcile_sl_orders_from_broker(restored_positions={})

    assert report.get("RELIANCE") == "orphan_cancelled", (
        f"P0 #3 residual regression: orphan SL-M was not cancelled. "
        f"Report={report}. cancel_order calls={api.cancelOrder.call_args_list}"
    )
    api.cancelOrder.assert_called_once_with("SL-ORPHAN-1", "NORMAL")
    # And the orphan should NOT be registered as a tracked SL.
    assert eng.get_sl_order_for_symbol("RELIANCE") is None


def test_reconcile_cancels_duplicate_sl_keeps_primary():
    """Broker shows TWO live SL-Ms for the same symbol — same side
    (the protective side for a BUY position). The first is matched
    and registered; the second is a duplicate and must be cancelled.
    """
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [
            {
                "orderid": "SL-PRIMARY",
                "tradingsymbol": "HDFCBANK",
                "transactiontype": "SELL",
                "ordertype": "SL-M",
                "status": "trigger pending",
                "triggerprice": "1485.0",
                "quantity": "10",
                "symboltoken": "123",
            },
            {
                "orderid": "SL-DUPLICATE",
                "tradingsymbol": "HDFCBANK",
                "transactiontype": "SELL",
                "ordertype": "SL-M",
                "status": "trigger pending",
                "triggerprice": "1480.0",
                "quantity": "10",
                "symboltoken": "123",
            },
        ],
    }
    api.cancelOrder.return_value = "CXL-OK"
    eng = _live_engine_with_mock_api(api)

    positions = {"HDFCBANK": _StubPos("HDFCBANK", "BUY", 10, 1500.0)}
    report = eng.reconcile_sl_orders_from_broker(positions)

    assert report.get("HDFCBANK") == "reconciled", (
        f"Primary SL should still register. Report={report}"
    )
    tracked = eng.get_sl_order_for_symbol("HDFCBANK")
    assert tracked is not None and tracked["order_id"] == "SL-PRIMARY", (
        "First matching SL-M must remain the registered primary."
    )
    # Duplicate must be cancelled. Only ONE cancel call (the duplicate).
    api.cancelOrder.assert_called_once_with("SL-DUPLICATE", "NORMAL")


def test_reconcile_orphan_cancel_failure_is_flagged():
    """Broker rejects the orphan cancel. Status must escalate to
    ``orphan_cancel_failed`` so ops can intervene manually instead of
    assuming we cleaned up."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-ORPHAN-2",
            "tradingsymbol": "ITC",
            "transactiontype": "SELL",
            "ordertype": "SL-M",
            "status": "trigger pending",
            "triggerprice": "400.0",
            "quantity": "100",
            "symboltoken": "5",
        }],
    }
    # cancel_order returns False on broker rejection (e.g. throttled,
    # already cancelled by ops, market-hours mismatch).
    api.cancelOrder.return_value = None
    eng = _live_engine_with_mock_api(api)

    report = eng.reconcile_sl_orders_from_broker(restored_positions={})

    assert report.get("ITC") == "orphan_cancel_failed", (
        f"Cancel rejection must surface as orphan_cancel_failed, not "
        f"orphan_cancelled. Report={report}"
    )


def test_reconcile_multiple_orphans_all_cancelled_independently():
    """Two unrelated orphans on the broker. Both should get cancel calls."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [
            {
                "orderid": "SL-ORPH-A",
                "tradingsymbol": "TCS",
                "transactiontype": "SELL",
                "ordertype": "SL-M",
                "status": "trigger pending",
                "triggerprice": "3700.0",
                "quantity": "5",
                "symboltoken": "11536",
            },
            {
                "orderid": "SL-ORPH-B",
                "tradingsymbol": "INFY",
                "transactiontype": "BUY",   # was a SHORT
                "ordertype": "SL-M",
                "status": "trigger pending",
                "triggerprice": "1620.0",
                "quantity": "8",
                "symboltoken": "1594",
            },
        ],
    }
    api.cancelOrder.return_value = "CXL-OK"
    eng = _live_engine_with_mock_api(api)

    report = eng.reconcile_sl_orders_from_broker(restored_positions={})

    assert report.get("TCS") == "orphan_cancelled"
    assert report.get("INFY") == "orphan_cancelled"
    cancelled_ids = {call.args[0] for call in api.cancelOrder.call_args_list}
    assert cancelled_ids == {"SL-ORPH-A", "SL-ORPH-B"}


def test_reconcile_does_not_cancel_terminal_status_sl():
    """A broker SL-M in a terminal state (complete/rejected/cancelled) is
    NOT live, so it must NOT trigger an orphan cancel call. Idempotency
    against re-running reconcile after a previous run already cancelled
    the orphan."""
    api = MagicMock()
    api.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": "SL-OLD-CANCELLED",
            "tradingsymbol": "WIPRO",
            "transactiontype": "SELL",
            "ordertype": "SL-M",
            "status": "cancelled",
            "triggerprice": "200.0",
            "quantity": "10",
            "symboltoken": "1",
        }],
    }
    eng = _live_engine_with_mock_api(api)

    report = eng.reconcile_sl_orders_from_broker(restored_positions={})

    assert report == {}
    api.cancelOrder.assert_not_called()


# ── P1 #12 (2026-05-17) -- modify_stop_loss false-positive on status:false ──
#
# Angel SmartAPI returns HTTP 200 with `{"status": false, "message": "..."}`
# on validation failures. The OLD code logged "SL modified" and returned True
# whenever no exception was raised, even though the broker order was unchanged.
# Trail SL only existed in RAM in that case.


def test_modify_sl_returns_false_when_broker_status_is_false():
    api = MagicMock()
    api.modifyOrder.return_value = {
        "status": False,
        "message": "invalid trigger price (below LTP)",
    }
    eng = _live_engine_with_mock_api(api)
    ok = eng.modify_stop_loss("SL-ORD-X", 1490.0)
    assert ok is False, (
        "P1 #12 regression: modify_stop_loss returned True on broker "
        "status=false response. Trail SL only exists in RAM."
    )


def test_modify_sl_returns_true_on_legacy_string_response():
    """Some SDK versions return the new order id as a bare string. That
    should still be treated as success (back-compat)."""
    api = MagicMock()
    api.modifyOrder.return_value = "OK"
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is True


def test_modify_sl_returns_true_on_status_true_dict():
    api = MagicMock()
    api.modifyOrder.return_value = {"status": True, "data": {"orderid": "SL-ORD-X"}}
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is True


def test_modify_sl_returns_false_on_empty_response():
    """Broker returned None / empty dict. Could mean throttle, partial
    response. Treat as failure so the caller can retry / alert."""
    api = MagicMock()
    api.modifyOrder.return_value = None
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is False


def test_modify_sl_propagates_false_through_update_trigger():
    """Integration: update_sl_trigger_for_symbol calls modify_stop_loss.
    A status=false response must make update_sl_trigger return False so
    the trail loop knows the propagation failed."""
    api = MagicMock()
    api.placeOrder.side_effect = ["ENTRY-1", "SL-1"]
    api.modifyOrder.return_value = {"status": False, "message": "invalid"}
    eng = _live_engine_with_mock_api(api)
    eng.place_order(
        symbol="HDFCBANK", token="123", transaction_type="BUY",
        quantity=10, price=1500.0, stop_loss=1485.0,
    )
    # First trigger update would normally succeed; verify failure path
    ok = eng.update_sl_trigger_for_symbol("HDFCBANK", 1490.0)
    assert ok is False
    # The cached trigger must NOT have been updated (still original 1485)
    assert eng.get_sl_order_for_symbol("HDFCBANK")["trigger"] == pytest.approx(1485.0)


# ---------------------------------------------------------------------------
# P1 (2026-05-18): bool('false') trap + orderBook propagation confirm
# ---------------------------------------------------------------------------


def test_modify_sl_rejects_string_false_status():
    """The pre-fix bool() check celebrated ``status: "false"`` as success
    because ``bool("false") == True`` in Python. Pin the fix."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": "false", "message": "RMS reject"}
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is False


@pytest.mark.parametrize("status_value", [
    "FALSE", "False", "0", "no", "fail", "failed", "rejected", "error",
    "  false  ",  # whitespace
])
def test_modify_sl_rejects_known_stringy_false_values(status_value):
    """All known false-y string aliases the broker might emit are rejected."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": status_value, "message": "x"}
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is False


@pytest.mark.parametrize("status_value", [
    True, "true", "True", "TRUE", "1", "ok", "OK", "yes", "success",
])
def test_modify_sl_accepts_known_truthy_status_values(status_value):
    api = MagicMock()
    api.modifyOrder.return_value = {"status": status_value, "data": {"orderid": "x"}}
    api.orderBook.return_value = {
        "data": [{"orderid": "SL-ORD-X", "triggerprice": "1490.00"}],
    }
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is True


def test_modify_sl_rejects_unknown_stringy_status():
    """Conservative default: a status string we don't recognise (broker
    contract change, garbage) is treated as failure so it surfaces as a
    no-op rather than a silent success."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": "maybe", "message": "?"}
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is False


def test_modify_sl_invokes_orderbook_verify_on_success():
    """After a status=true modify, the engine must fetch orderBook to
    verify the new trigger actually propagated."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": True}
    api.orderBook.return_value = {
        "data": [{"orderid": "SL-ORD-X", "triggerprice": "1490.00"}],
    }
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is True
    api.orderBook.assert_called_once()


def test_modify_sl_verify_warns_on_trigger_mismatch():
    """If orderBook reports a different trigger than the one we just
    modified to, log a WARNING (RMS soft-reject signal). The modify
    response is still treated as success."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": True}
    # Broker accepted the modify but kept the OLD trigger value -- RMS
    # silent reject pattern observed in production.
    api.orderBook.return_value = {
        "data": [{"orderid": "SL-ORD-X", "triggerprice": "1485.00"}],
    }
    eng = _live_engine_with_mock_api(api)

    from loguru import logger as loguru_logger

    captured = []
    handler_id = loguru_logger.add(
        lambda msg: captured.append(str(msg)), level="WARNING",
    )
    try:
        ok = eng.modify_stop_loss("SL-ORD-X", 1490.0)
    finally:
        loguru_logger.remove(handler_id)
    assert ok is True  # response said success
    assert any("SL-MODIFY-VERIFY" in m for m in captured)


def test_modify_sl_verify_tolerates_orderbook_failure():
    """A throwing orderBook call after a successful modify must not
    flip the modify result to False -- it's a best-effort forensic check."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": True}
    api.orderBook.side_effect = RuntimeError("API outage")
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is True


def test_modify_sl_verify_tolerates_missing_order_in_book():
    """If the orderBook doesn't list our order_id (stale snapshot, page
    cutoff), don't warn -- just no-op. Pinning this so we don't accidentally
    fire spurious WARNINGs that desensitise the operator."""
    api = MagicMock()
    api.modifyOrder.return_value = {"status": True}
    api.orderBook.return_value = {"data": [{"orderid": "OTHER-ORD"}]}
    eng = _live_engine_with_mock_api(api)
    assert eng.modify_stop_loss("SL-ORD-X", 1490.0) is True
