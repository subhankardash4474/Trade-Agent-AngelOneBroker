"""Regression tests for the 2026-05-28 audit follow-up Phase-2 fixes.

Each test maps 1:1 (or 1:few) to a finding ID in
``docs/audit_2026-05-28_followup.md``. Naming convention:
``test_<finding>_<one_line_intent>``.

Phase-2 scope (this file): money-at-risk truth-telling.

  * ORD-01 / STATE-01: ``_wait_for_terminal`` + live order path waits
    for terminal status and uses broker-reported ``averageprice`` /
    ``filledshares`` instead of signal-time price.
  * ORD-02:           pre-retry ``orderBook`` reconciliation (no
                      idempotency tag = no client-supplied key, so we
                      probe the broker's own state on every retry).
  * ORD-03:           atomic-entry rollback when ``portfolio.open_position``
                      fails after a successful broker leg.
  * STATE-02:         boot reconcile detects broker-only positions
                      absent from DB and CRITICAL-blocks the symbol.
  * OBS-05:           boot reconcile fails CLOSED on positionBook fetch
                      error in live mode (3 retries with backoff, then
                      a global gate that requires an operator ack file).

Tests are pure-Python and avoid any real broker / DB I/O. All
broker calls are MagicMock'd.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────── shared helpers ─────────────


def _make_engine(mode: str = "live", api: object | None = None):
    """Construct a minimal ExecutionEngine with the given mode and api."""
    from core.execution import ExecutionEngine
    cfg = {
        "broker": {"mode": mode},
        "execution": {
            "live_order_fill_timeout_sec": 0.5,
            "live_order_fill_poll_interval_sec": 0.05,
            "idempotency_lookback_sec": 30.0,
            "retry_attempts": 2,
            "retry_delay_seconds": 0.0,
        },
        "market": {"exchange": "NSE"},
    }
    return ExecutionEngine(cfg, smart_api=api, database=None)


# ───────────────────────── ORD-01 / STATE-01 ─────────────────────────


def test_ord01_wait_for_terminal_returns_row_on_complete():
    """When the broker reports ``COMPLETE`` for the polled order_id,
    ``_wait_for_terminal`` must return the matching row."""
    api = MagicMock()
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-1",
            "status": "complete",
            "averageprice": "234.55",
            "filledshares": "10",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    row = eng._wait_for_terminal("OID-1", timeout_sec=0.5)
    assert row is not None
    assert row.get("orderid") == "OID-1"
    assert eng._normalise_status(row) == "complete"


def test_ord01_wait_for_terminal_returns_none_on_ttl():
    """Order present but always-pending must time out and return None."""
    api = MagicMock()
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-2",
            "status": "open",       # never terminal
            "averageprice": "0",
            "filledshares": "0",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    eng.live_order_fill_timeout_sec = 0.2
    eng.live_order_fill_poll_interval_sec = 0.05
    start = time.time()
    row = eng._wait_for_terminal("OID-2")
    elapsed = time.time() - start
    assert row is None
    assert 0.15 <= elapsed <= 1.0, (
        f"TTL must be respected (~0.2s) -- elapsed={elapsed:.3f}s"
    )


def test_ord01_wait_for_terminal_returns_row_on_rejected():
    """REJECTED is terminal: helper returns the row, caller decides."""
    api = MagicMock()
    api.orderBook.return_value = {
        "data": [{"orderid": "OID-3", "status": "rejected"}]
    }
    eng = _make_engine(mode="live", api=api)
    row = eng._wait_for_terminal("OID-3", timeout_sec=0.3)
    assert row is not None
    assert eng._normalise_status(row) == "rejected"


def test_ord01_wait_for_terminal_paper_mode_short_circuits():
    """Paper mode returns None immediately without polling."""
    api = MagicMock()
    eng = _make_engine(mode="paper", api=api)
    assert eng._wait_for_terminal("OID-X", timeout_sec=5.0) is None
    api.orderBook.assert_not_called()


def test_ord01_extract_avg_fill_price_handles_zero_and_string():
    from core.execution import ExecutionEngine
    assert ExecutionEngine._extract_avg_fill_price({}) is None
    assert ExecutionEngine._extract_avg_fill_price({"averageprice": "0"}) is None
    assert ExecutionEngine._extract_avg_fill_price({"averageprice": "234.55"}) == 234.55
    assert ExecutionEngine._extract_avg_fill_price({"averagePrice": 12.5}) == 12.5
    assert ExecutionEngine._extract_avg_fill_price({"averageprice": "junk"}) is None


def test_ord01_live_order_uses_broker_averageprice_not_signal_price():
    """The headline ORD-01 contract: ``filled_price`` in the returned
    dict must be the broker's ``averageprice`` after the order
    reaches terminal FILLED status -- NOT the signal-time price the
    caller passed in."""
    api = MagicMock()
    api.placeOrder.return_value = "OID-FILLED"
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-FILLED",
            "status": "complete",
            "averageprice": "240.10",
            "filledshares": "10",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    eng._place_sl_order = MagicMock(return_value=None)  # skip SL leg
    eng._persist_order = MagicMock()
    out = eng._live_order_with_retry(
        symbol="YESBANK", token="11915", tx_type="BUY",
        quantity=10, price=235.00, order_type="LIMIT",
        stop_loss=None, take_profit=None, tag="t",
    )
    assert out is not None
    assert out["status"] == "FILLED"
    assert out["filled_price"] == 240.10
    assert out["filled_quantity"] == 10
    # Slippage must be computed against the requested price (5.10)
    assert out["slippage"] == pytest.approx(5.10, abs=1e-6)


def test_ord01_live_order_returns_none_on_terminal_rejected():
    """When the broker reports REJECTED (or CANCELLED) the live path
    must return None so the entry caller treats it as a failure --
    not as a fill at the signal-time price."""
    api = MagicMock()
    api.placeOrder.return_value = "OID-REJ"
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-REJ",
            "status": "rejected",
            "averageprice": "0",
            "filledshares": "0",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    eng._place_sl_order = MagicMock(return_value=None)
    eng._persist_order = MagicMock()
    out = eng._live_order_with_retry(
        symbol="YESBANK", token="11915", tx_type="BUY",
        quantity=10, price=235.00, order_type="MARKET",
        stop_loss=None, take_profit=None, tag="t",
    )
    assert out is None, (
        "ORD-01 regression: terminal REJECTED must surface as None to caller"
    )


def test_ord01_live_order_keeps_placed_status_on_ttl_with_no_terminal():
    """If the broker is still PENDING at TTL, the wrapper keeps the
    pre-existing degrade behaviour (status='PLACED', filled_price=None)
    so the caller can decide. Documents the contract; the warning
    inside ``_wait_for_terminal`` is the operator's signal."""
    api = MagicMock()
    api.placeOrder.return_value = "OID-PEND"
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-PEND",
            "status": "open",  # never reaches terminal
            "averageprice": "0",
            "filledshares": "0",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    eng.live_order_fill_timeout_sec = 0.15
    eng.live_order_fill_poll_interval_sec = 0.05
    eng._place_sl_order = MagicMock(return_value=None)
    eng._persist_order = MagicMock()
    out = eng._live_order_with_retry(
        symbol="YESBANK", token="11915", tx_type="BUY",
        quantity=10, price=235.00, order_type="LIMIT",
        stop_loss=None, take_profit=None, tag="t",
    )
    assert out is not None
    assert out["status"] == "PLACED"
    assert out["filled_price"] is None
    assert out["filled_quantity"] == 0


# ───────────────────────── ORD-02 ─────────────────────────


def test_ord02_idempotent_match_finds_recent_in_flight_order():
    """If the broker already has an open order matching (symbol,
    side, qty, ordertype), reuse its id instead of placing a duplicate."""
    from datetime import datetime, timedelta
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    api = MagicMock()
    recent_ts = (datetime.now(IST) - timedelta(seconds=5)).strftime("%d-%b-%Y %H:%M:%S")
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-EXIST",
            "tradingsymbol": "YESBANK",
            "transactiontype": "BUY",
            "ordertype": "MARKET",
            "quantity": "10",
            "status": "open",
            "orderentrytime": recent_ts,
        }]
    }
    eng = _make_engine(mode="live", api=api)
    found = eng._find_idempotent_match(
        symbol="YESBANK", tx_type="BUY", quantity=10, order_type="MARKET",
    )
    assert found == "OID-EXIST"


def test_ord02_idempotent_match_ignores_cancelled():
    """A cancelled / rejected order is NOT a live duplicate, so it must
    not be reused."""
    api = MagicMock()
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-OLD",
            "tradingsymbol": "YESBANK",
            "transactiontype": "BUY",
            "ordertype": "MARKET",
            "quantity": "10",
            "status": "cancelled",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    assert eng._find_idempotent_match(
        symbol="YESBANK", tx_type="BUY", quantity=10, order_type="MARKET",
    ) is None


def test_ord02_idempotent_match_ignores_stale_outside_lookback():
    """An order placed before the lookback window is treated as
    "not the duplicate of THIS retry attempt"."""
    from datetime import datetime, timedelta
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    api = MagicMock()
    stale_ts = (datetime.now(IST) - timedelta(seconds=300)).strftime("%d-%b-%Y %H:%M:%S")
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-STALE",
            "tradingsymbol": "YESBANK",
            "transactiontype": "BUY",
            "ordertype": "MARKET",
            "quantity": "10",
            "status": "open",
            "orderentrytime": stale_ts,
        }]
    }
    eng = _make_engine(mode="live", api=api)
    eng.idempotency_lookback_sec = 30.0
    assert eng._find_idempotent_match(
        symbol="YESBANK", tx_type="BUY", quantity=10, order_type="MARKET",
    ) is None


def test_ord02_retry_skips_placeOrder_when_idempotent_match_found():
    """The classic ORD-02 hazard: first placeOrder times out (returns
    None / raises). On retry attempt #2 the broker orderBook reports
    an in-flight order with the same intent. The retry MUST reuse
    that id and MUST NOT call placeOrder a second time -- that
    second call would create a duplicate position."""
    from datetime import datetime, timedelta
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    api = MagicMock()
    # Attempt #1: simulate a timeout / silent drop. AngelOne wrapper
    # documents this exact mode: timed-out call may have placed.
    # We simulate by raising on the first call only. The order DID
    # land at the broker.
    placed_ts = (datetime.now(IST) - timedelta(seconds=2)).strftime("%d-%b-%Y %H:%M:%S")
    api.placeOrder.side_effect = [
        TimeoutError("simulated network stall"),
        "OID-DUP-IF-CALLED",  # if our fix is broken we'd see a duplicate
    ]
    # orderBook returns the first attempt's actual broker-side order:
    api.orderBook.return_value = {
        "data": [{
            "orderid": "OID-FIRST",
            "tradingsymbol": "YESBANK",
            "transactiontype": "BUY",
            "ordertype": "MARKET",
            "quantity": "10",
            "status": "complete",
            "averageprice": "240.10",
            "filledshares": "10",
            "orderentrytime": placed_ts,
        }]
    }
    eng = _make_engine(mode="live", api=api)
    eng.retry_attempts = 2
    eng.retry_delay = 0
    eng._place_sl_order = MagicMock(return_value=None)
    eng._persist_order = MagicMock()
    out = eng._live_order_with_retry(
        symbol="YESBANK", token="11915", tx_type="BUY",
        quantity=10, price=235.00, order_type="MARKET",
        stop_loss=None, take_profit=None, tag="t",
    )
    assert out is not None
    assert out["order_id"] == "OID-FIRST", (
        "ORD-02 regression: retry must reuse the idempotent order id, "
        "not the second placeOrder return value"
    )
    # Crucially, placeOrder was called exactly ONCE (the first attempt).
    # The second attempt short-circuits via the idempotent match.
    assert api.placeOrder.call_count == 1, (
        f"ORD-02 regression: expected 1 placeOrder call, got "
        f"{api.placeOrder.call_count} (duplicate)"
    )


# ───────────────────────── ORD-03 ─────────────────────────


def test_ord03_rollback_calls_cancel_sl_then_counter_flatten_in_live():
    """The live rollback must:
       1. Cancel the SL leg first.
       2. Place a counter-flatten MARKET order on the OPPOSITE side.
       3. Pop the entry from pending_orders.
    """
    api = MagicMock()
    api.placeOrder.return_value = "OID-FLAT"
    eng = _make_engine(mode="live", api=api)
    eng.cancel_sl_order_for_symbol = MagicMock(return_value=True)
    eng._pending_orders["OID-ENTRY"] = {"order_id": "OID-ENTRY"}
    ok = eng.rollback_entry_on_portfolio_failure(
        symbol="YESBANK", token="11915",
        entry_order_id="OID-ENTRY", entry_tx="BUY", quantity=10,
    )
    assert ok is True
    eng.cancel_sl_order_for_symbol.assert_called_once_with("YESBANK")
    api.placeOrder.assert_called_once()
    flatten_params = api.placeOrder.call_args[0][0]
    assert flatten_params["transactiontype"] == "SELL"  # opposite of BUY
    assert flatten_params["ordertype"] == "MARKET"
    assert flatten_params["quantity"] == "10"
    assert "OID-ENTRY" not in eng._pending_orders


def test_ord03_rollback_returns_false_when_counter_flatten_fails():
    """If the counter-flatten broker call returns empty / raises,
    rollback returns False so the caller blocks the symbol."""
    api = MagicMock()
    api.placeOrder.return_value = None  # empty return = broker refused
    eng = _make_engine(mode="live", api=api)
    eng.cancel_sl_order_for_symbol = MagicMock(return_value=True)
    ok = eng.rollback_entry_on_portfolio_failure(
        symbol="YESBANK", token="11915",
        entry_order_id="OID-ENTRY", entry_tx="BUY", quantity=10,
    )
    assert ok is False


def test_ord03_rollback_returns_false_when_sl_cancel_fails():
    """An SL cancel failure must surface as rollback_ok=False; the
    counter-flatten still runs (naked exposure is the bigger risk)
    but the caller gets the truth."""
    api = MagicMock()
    api.placeOrder.return_value = "OID-FLAT"
    eng = _make_engine(mode="live", api=api)
    eng.cancel_sl_order_for_symbol = MagicMock(return_value=False)
    ok = eng.rollback_entry_on_portfolio_failure(
        symbol="YESBANK", token="11915",
        entry_order_id="OID-ENTRY", entry_tx="BUY", quantity=10,
    )
    assert ok is False
    api.placeOrder.assert_called_once()  # counter-flatten still ran


def test_ord03_rollback_paper_mode_is_noop_with_pending_cleanup():
    """Paper mode: no broker call, but the caller-side cleanup still
    happens so the in-memory state is consistent."""
    eng = _make_engine(mode="paper", api=None)
    eng._pending_orders["OID-PAPER"] = {"order_id": "OID-PAPER"}
    ok = eng.rollback_entry_on_portfolio_failure(
        symbol="YESBANK", token="11915",
        entry_order_id="OID-PAPER", entry_tx="BUY", quantity=10,
    )
    assert ok is True
    assert "OID-PAPER" not in eng._pending_orders


def test_ord03_trading_agent_wraps_open_position_in_try_except():
    """Source-level: the entry path in trading_agent.py must wrap
    portfolio.open_position in a try/except and call
    execution.rollback_entry_on_portfolio_failure on failure."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    needle = "self.portfolio.open_position("
    pos = src.find(needle)
    assert pos != -1
    # The rollback path (inside the ``if not opened:`` branch) sits
    # ~1.5-2.5kb past the open_position call. Use a 4000-char window
    # to cover the whole entry-path block.
    block = src[max(0, pos - 800):pos + 4000]
    assert "ORD-03" in block, (
        "ORD-03 regression: entry path missing the audit-tagged comment"
    )
    assert "rollback_entry_on_portfolio_failure" in block, (
        "ORD-03 regression: rollback helper not invoked from entry path"
    )
    assert "_symbols_blocked_by_rollback" in src, (
        "ORD-03 regression: rollback-block set is not maintained"
    )


# ───────────────────────── STATE-02 ─────────────────────────


def test_state02_reconcile_detects_broker_only_position():
    """When the broker holds a non-zero netqty for a symbol the DB
    has no record of, ``reconcile_positions_with_broker`` must return
    ``status=='broker_only'`` for it -- the daemon was previously
    silent on this case."""
    api = MagicMock()
    api.position.return_value = {
        "data": [{
            "tradingsymbol": "TCS",
            "netqty": "50",
        }]
    }
    eng = _make_engine(mode="live", api=api)
    report = eng.reconcile_positions_with_broker({})  # empty DB
    assert "TCS" in report
    entry = report["TCS"]
    assert entry["status"] == "broker_only"
    assert entry["broker_netqty"] == 50
    assert entry["broker_side"] == "BUY"
    assert entry["broker_quantity"] == 50


def test_state02_reconcile_ignores_zero_netqty_broker_rows():
    """A broker row with netqty=0 is NOT a broker-only position -- it's
    just historical residue from an EOD-flattened position. Don't
    spam CRITICALs on those."""
    api = MagicMock()
    api.position.return_value = {
        "data": [
            {"tradingsymbol": "TCS", "netqty": "0"},
            {"tradingsymbol": "INFY", "netqty": "0"},
        ]
    }
    eng = _make_engine(mode="live", api=api)
    report = eng.reconcile_positions_with_broker({})
    assert "TCS" not in report
    assert "INFY" not in report


def test_state02_trading_agent_blocks_symbol_on_broker_only():
    """Source-level: the boot-reconcile loop in trading_agent.py
    must handle ``status == 'broker_only'`` by adding the symbol to
    ``_stock_loss_today`` and queueing a CRITICAL alert."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert 'status == "broker_only"' in src, (
        "STATE-02 regression: broker_only handler missing from boot reconcile"
    )
    # Find the broker_only branch and confirm it pushes a stock-loss
    # block + queues an alert.
    pos = src.find('status == "broker_only"')
    assert pos != -1
    branch = src[pos:pos + 1800]
    assert "_stock_loss_today" in branch, (
        "STATE-02 regression: broker_only branch must block via stock_loss_today"
    )
    assert "_pending_boot_alerts" in branch, (
        "STATE-02 regression: broker_only branch must queue a CRITICAL alert"
    )


# ───────────────────────── OBS-05 ─────────────────────────


def test_obs05_reconcile_retries_three_times_before_failing_closed():
    """positionBook fetch is retried up to 3 times. Test that all 3
    are attempted and that the failure flag is set after the third.
    We patch ``time.sleep`` to keep the test fast."""
    api = MagicMock()
    api.position.side_effect = RuntimeError("flaky network")
    eng = _make_engine(mode="live", api=api)

    import core.execution as ex_mod
    real_sleep = ex_mod.time.sleep
    ex_mod.time.sleep = lambda *_a, **_kw: None
    try:
        report = eng.reconcile_positions_with_broker(
            {"YESBANK": {"side": "BUY", "quantity": 10}}
        )
    finally:
        ex_mod.time.sleep = real_sleep

    # Should have called position() three times.
    assert api.position.call_count == 3, (
        f"OBS-05 regression: expected 3 retries, got {api.position.call_count}"
    )
    # Failure flag must be set so the caller fails CLOSED.
    assert eng.boot_reconcile_failed_live is True
    assert eng.boot_reconcile_failure_reason is not None
    # Per-symbol report must mark as skipped (api_error).
    assert report["YESBANK"]["status"] == "skipped"
    assert report["YESBANK"]["reason"] == "api_error"


def test_obs05_reconcile_succeeds_on_second_attempt_no_flag_set():
    """A transient failure that recovers on attempt #2 must NOT set
    the fail-closed flag. Only persistent failures should."""
    api = MagicMock()
    api.position.side_effect = [
        RuntimeError("transient"),
        {"data": []},  # success on retry
    ]
    eng = _make_engine(mode="live", api=api)
    import core.execution as ex_mod
    real_sleep = ex_mod.time.sleep
    ex_mod.time.sleep = lambda *_a, **_kw: None
    try:
        eng.reconcile_positions_with_broker({})
    finally:
        ex_mod.time.sleep = real_sleep
    assert eng.boot_reconcile_failed_live is False, (
        "OBS-05 regression: transient recovery must NOT trip the gate"
    )


def test_obs05_paper_mode_never_fails_closed():
    """Paper mode has no broker truth to compare against -- the
    fail-closed gate must never be tripped, regardless of api state."""
    eng = _make_engine(mode="paper", api=None)
    eng.reconcile_positions_with_broker({"X": {"side": "BUY", "quantity": 1}})
    assert eng.boot_reconcile_failed_live is False


# ───────────── _boot_reconcile_gate_open behaviour (TradingAgent) ─────────────


def test_obs05_gate_open_when_flag_set_and_no_ack_file(tmp_path):
    """The TradingAgent gate returns True iff the flag is set AND the
    ack file does not exist. Reconstruct the helper bound to a stub
    object so we don't need to instantiate the whole TradingAgent."""
    import trading_agent as ta_mod

    class StubAgent:
        def __init__(self, ack_path):
            self._boot_reconcile_failed_live = True
            self._boot_reconcile_failure_reason = "test"
            self._boot_reconcile_ack_path = ack_path

    StubAgent._boot_reconcile_gate_open = ta_mod.TradingAgent._boot_reconcile_gate_open

    ack = tmp_path / "boot_reconcile.ack"
    agent = StubAgent(ack)
    assert agent._boot_reconcile_gate_open() is True


def test_obs05_gate_clears_when_ack_file_present_and_consumes_it(tmp_path):
    """Touching the ack file must clear the gate ONCE; the file is
    consumed (deleted) so a later re-arm needs a fresh ack."""
    import trading_agent as ta_mod

    class StubAgent:
        def __init__(self, ack_path):
            self._boot_reconcile_failed_live = True
            self._boot_reconcile_failure_reason = "test"
            self._boot_reconcile_ack_path = ack_path

    StubAgent._boot_reconcile_gate_open = ta_mod.TradingAgent._boot_reconcile_gate_open

    ack = tmp_path / "boot_reconcile.ack"
    ack.write_text("ack")
    agent = StubAgent(ack)
    assert agent._boot_reconcile_gate_open() is False
    # Ack file consumed.
    assert not ack.exists(), "ack file must be unlinked after consumption"
    # Flag cleared.
    assert agent._boot_reconcile_failed_live is False


def test_obs05_gate_returns_false_when_flag_never_set(tmp_path):
    """If the gate flag was never tripped, the helper returns False
    even if the ack file is absent -- it should not be doing any
    filesystem work in the happy path."""
    import trading_agent as ta_mod

    class StubAgent:
        def __init__(self, ack_path):
            self._boot_reconcile_failed_live = False
            self._boot_reconcile_failure_reason = None
            self._boot_reconcile_ack_path = ack_path

    StubAgent._boot_reconcile_gate_open = ta_mod.TradingAgent._boot_reconcile_gate_open

    agent = StubAgent(tmp_path / "boot_reconcile.ack")
    assert agent._boot_reconcile_gate_open() is False


def test_obs05_open_new_position_refuses_when_gate_open():
    """Source-level: ``_open_new_position`` must check the gate at
    the very top and call ``_audit_reject`` with reason
    ``boot_reconcile_gate`` when the gate is open."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    pos = src.find("def _open_new_position(")
    assert pos != -1
    block = src[pos:pos + 4000]
    assert "_boot_reconcile_gate_open" in block, (
        "OBS-05 regression: _open_new_position missing gate check"
    )
    assert "boot_reconcile_gate" in block, (
        "OBS-05 regression: gate-rejection audit reason missing"
    )
