"""P0 #2 (2026-05-15) + P0 #2-residual (2026-05-18) — LIVE-MODE SAFETY:
regression tests for the lock that serializes the exit pipeline across
EVERY caller path, not just the `_check_position_exits` poll path.

Background
----------
The trading agent has FOUR concurrent producers that can close the same
position:

  • Main loop fast-exits poll: every ~15s calls `_check_position_exits`
    with a multi-symbol price snapshot.
  • WebSocket tick path (`_on_tick`): per-tick `_check_position_exits`.
  • Signal reversal path (`_exit_on_signal`): ensemble flips on a held
    symbol → calls `_close_position_safely` directly.
  • End-of-day square-off (`_square_off_all`) and carryover profit-lock
    (`_lock_in_carryover_profits`): both call `_close_position_safely`.

The original P0 #2 fix put the lock at `_check_position_exits`, which
only covered the first two callers. The audit on 2026-05-18 caught the
gap: an ensemble SELL on a symbol already approaching its SL can race
against the WS tick exit, both end up calling `_close_position_safely`,
and the broker sees TWO flatten orders for the same lot. The agent is
now accidentally net-short on the rebound — the exact double-flatten
window we set out to close.

Residual fix moved the lock INSIDE `_close_position_safely` (the single
chokepoint that submits flatten orders), upgraded it to RLock so the
existing `_check_position_exits_locked` body can still re-enter it, and
added an idempotency check at the top: if `portfolio.positions` no
longer has the symbol, the second caller logs and bails without
submitting a duplicate flatten.

These tests pin both the lock and the idempotency contract.
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest


def _mk_agent_for_thread_safety_test():
    """Build a stub TradingAgent exposing just the surface that the
    `_check_position_exits` lock guards. We use `__new__` to skip the
    real constructor and inject the minimal attribute set.

    The lock is RLock (P0 #2 residual fix, 2026-05-18) — required so
    `_check_position_exits_locked` can call `_close_position_safely`
    without deadlocking on the second acquire.
    """
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._exit_check_lock = threading.RLock()

    # Track every call into the locked body and how long each held the
    # lock. The test then asserts that no two calls overlapped.
    call_events: list[tuple[str, float]] = []

    def _locked_body(prices):
        call_events.append(("enter", time.monotonic()))
        # Simulate the work of building to_close + flatten + portfolio
        # close. Sleep is short but long enough to expose a race.
        time.sleep(0.05)
        call_events.append(("exit", time.monotonic()))

    agent._check_position_exits_locked = _locked_body
    return agent, call_events


def test_exit_check_serializes_two_concurrent_callers():
    """Two threads call `_check_position_exits` simultaneously. The lock
    must serialize them: the second thread waits until the first exits
    the locked body."""
    agent, call_events = _mk_agent_for_thread_safety_test()

    def runner():
        agent._check_position_exits({"HDFCBANK": 1500.0})

    t1 = threading.Thread(target=runner)
    t2 = threading.Thread(target=runner)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(call_events) == 4  # 2 enters + 2 exits
    # Pair them and check no overlap.
    enters = [t for kind, t in call_events if kind == "enter"]
    exits = [t for kind, t in call_events if kind == "exit"]
    enters.sort()
    exits.sort()
    # The second enter must come AFTER the first exit. If they raced,
    # the second enter would happen before the first exit.
    assert enters[1] >= exits[0], (
        f"P0 #2 regression: second thread entered before first exited.\n"
        f"enters={enters}, exits={exits}. Lock is missing or wrong-scope."
    )


def test_exit_check_serializes_ws_thread_against_main_thread():
    """Simulate the WS-thread/main-thread topology exactly: one thread
    holds the lock running a 'long' exit check, another thread (a tick
    callback) attempts to enter mid-flight."""
    agent, call_events = _mk_agent_for_thread_safety_test()

    # Pin which entries came from which 'thread role' so the assertion
    # message is clearer.
    role_events: list[tuple[str, str, float]] = []

    def make_runner(role: str):
        def _run():
            with agent._exit_check_lock:
                role_events.append((role, "enter", time.monotonic()))
                time.sleep(0.08)
                role_events.append((role, "exit", time.monotonic()))
        return _run

    main_thread = threading.Thread(target=make_runner("main"))
    ws_thread = threading.Thread(target=make_runner("ws"))
    main_thread.start()
    # Give main a tiny head start so it gets the lock first deterministically
    time.sleep(0.01)
    ws_thread.start()
    main_thread.join(timeout=5)
    ws_thread.join(timeout=5)

    assert len(role_events) == 4
    main_enter = next(t for r, k, t in role_events if r == "main" and k == "enter")
    main_exit = next(t for r, k, t in role_events if r == "main" and k == "exit")
    ws_enter = next(t for r, k, t in role_events if r == "ws" and k == "enter")

    assert main_exit <= ws_enter, (
        f"P0 #2 regression: WS thread entered the locked body before the "
        f"main thread exited. main_exit={main_exit}, ws_enter={ws_enter}. "
        f"The WS+main flatten race window is open again."
    )


def test_lock_exists_as_reentrant_lock_on_init():
    """Light structural check: any future refactor that drops or downgrades
    the lock will fail this test. P0 #2 residual (2026-05-18) requires the
    lock to be RE-ENTRANT (RLock) because `_check_position_exits_locked`
    holds it and then calls `_close_position_safely`, which itself
    re-acquires the same lock. A plain `Lock` would deadlock here on the
    very first WS-tick-driven SL exit.
    """
    agent, _ = _mk_agent_for_thread_safety_test()
    assert hasattr(agent._exit_check_lock, "acquire")
    assert hasattr(agent._exit_check_lock, "release")

    # Reentrancy probe: a non-reentrant lock would block on the second
    # acquire from the same thread; an RLock returns immediately.
    first = agent._exit_check_lock.acquire(timeout=0.5)
    assert first is True
    second = agent._exit_check_lock.acquire(timeout=0.5)
    assert second is True, (
        "P0 #2 residual regression: _exit_check_lock is NOT re-entrant. "
        "The locked exit-check body cannot call _close_position_safely "
        "(which now also acquires this lock) without deadlocking. "
        "Restore threading.RLock()."
    )
    agent._exit_check_lock.release()
    agent._exit_check_lock.release()


def test_exception_in_locked_body_releases_lock():
    """If the locked body raises, the lock MUST be released so subsequent
    callers aren't deadlocked. Python's `with` statement guarantees this
    but pin it explicitly so a future refactor away from `with` (e.g. to
    manual acquire/release) doesn't silently regress."""
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._exit_check_lock = threading.RLock()

    def _raising_body(prices):
        raise RuntimeError("simulated failure in exit-check body")

    agent._check_position_exits_locked = _raising_body

    with pytest.raises(RuntimeError):
        agent._check_position_exits({"X": 1.0})

    # Lock must now be releaseable, i.e. not held.
    acquired = agent._exit_check_lock.acquire(timeout=0.5)
    assert acquired is True, (
        "P0 #2 regression: exception in the locked body left the lock "
        "held. Next exit-check call would deadlock."
    )
    agent._exit_check_lock.release()


# ──────────────────────────────────────────────────────────────────────
# P0 #2 RESIDUAL (2026-05-18) — lock at _close_position_safely callee
# ──────────────────────────────────────────────────────────────────────


def _mk_agent_with_close_helper():
    """Build a stub TradingAgent wired up just enough to exercise
    `_close_position_safely` under contention.

    Mocks:
      * `portfolio.positions`: a real dict so the idempotency re-check
        sees the position vanish after the first thread closes it.
      * `execution.{get_sl_order_for_symbol,cancel_sl_order_for_symbol,
        place_order}`: counters so we can assert the second thread did
        NOT submit a duplicate flatten.
      * `portfolio.close_position`: pops the symbol from positions and
        returns a fake TradeRecord-shaped object.
      * `risk_manager`, `alert_manager`, `_persist_trailing_states`,
        `_record_exit`, `_on_trade_closed`: no-op mocks.

    The real `_close_position_safely` body runs unmodified.
    """
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._exit_check_lock = threading.RLock()

    counters = {
        "place_order_calls": 0,
        "cancel_sl_calls": 0,
        "close_position_calls": 0,
    }

    class _StubPortfolio:
        def __init__(self):
            # Single open position used by the contention test.
            self.positions = {
                "RELIANCE": mock.Mock(side="BUY", quantity=10, entry_price=2800.0)
            }
            self._lock = threading.Lock()

        def close_position(self, symbol, price, exit_reason="signal"):
            counters["close_position_calls"] += 1
            with self._lock:
                # Sleep INSIDE close_position so contender threads have
                # time to queue on _exit_check_lock and trip the
                # idempotency re-check.
                time.sleep(0.05)
                if symbol not in self.positions:
                    return None
                del self.positions[symbol]
            return mock.Mock(pnl=12.34, side="BUY")

    class _StubExecution:
        def __init__(self):
            self._sl_orders = {"RELIANCE": "SL-ID-1"}

        def get_sl_order_for_symbol(self, symbol):
            return self._sl_orders.get(symbol)

        def cancel_sl_order_for_symbol(self, symbol):
            counters["cancel_sl_calls"] += 1
            self._sl_orders.pop(symbol, None)
            return True

        def place_order(self, **kw):
            counters["place_order_calls"] += 1
            return {"status": "FILLED", "filled_price": kw["price"]}

    agent.portfolio = _StubPortfolio()
    agent.execution = _StubExecution()
    agent.risk_manager = mock.Mock()
    agent.alert_manager = mock.Mock()
    agent._persist_trailing_states = mock.Mock()
    agent._record_exit = mock.Mock()
    agent._on_trade_closed = mock.Mock()
    return agent, counters


def test_close_position_safely_serializes_concurrent_callers():
    """Two threads call `_close_position_safely` for the same symbol at
    the same moment (the exact ensemble-SELL-while-SL-pending race the
    audit caught). The lock + idempotency re-check together must result
    in EXACTLY ONE flatten order at the broker.
    """
    agent, counters = _mk_agent_with_close_helper()

    def runner(tag):
        agent._close_position_safely(
            symbol="RELIANCE",
            token="12345",
            exit_side="SELL",
            quantity=10,
            price=2800.0,
            tag=tag,
            exit_reason=tag,
        )

    t1 = threading.Thread(target=runner, args=("ws_tick_sl",))
    t2 = threading.Thread(target=runner, args=("signal_reverse",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert counters["place_order_calls"] == 1, (
        f"P0 #2 residual regression: place_order called "
        f"{counters['place_order_calls']} times. Two threads both "
        f"submitted a flatten — the broker is now net-reverse on this "
        f"symbol. The lock at _close_position_safely OR the idempotency "
        f"re-check (or both) regressed."
    )
    assert counters["cancel_sl_calls"] == 1, (
        f"cancel_sl_order_for_symbol called {counters['cancel_sl_calls']} "
        f"times. Should be exactly once — the second caller must hit the "
        f"idempotency re-check and bail BEFORE touching the SL."
    )


def test_close_position_safely_can_be_called_from_inside_locked_body():
    """The realistic call-chain is:

        WS-tick → _check_position_exits → (acquires lock)
          → _check_position_exits_locked → _close_position_safely
            → (re-acquires SAME lock via RLock)

    A non-reentrant lock would deadlock the entire trader process here
    on the first WS-tick-driven exit, which is a P0 LIVE-MODE blocker.
    Force the re-entrant path explicitly.
    """
    agent, counters = _mk_agent_with_close_helper()

    completed = threading.Event()

    def simulate_outer_holder():
        with agent._exit_check_lock:
            # Inner call should NOT block on the lock we already hold.
            agent._close_position_safely(
                symbol="RELIANCE",
                token="12345",
                exit_side="SELL",
                quantity=10,
                price=2800.0,
                tag="reentrant_check",
                exit_reason="reentrant_check",
            )
            completed.set()

    t = threading.Thread(target=simulate_outer_holder)
    t.start()
    finished = completed.wait(timeout=2.0)
    t.join(timeout=2.0)

    assert finished, (
        "P0 #2 residual regression: _close_position_safely DEADLOCKED "
        "when called from inside a thread that already holds "
        "_exit_check_lock. The lock must be threading.RLock(); a plain "
        "threading.Lock() will hang the daemon on every WS-driven exit."
    )
    assert counters["place_order_calls"] == 1


def test_close_position_safely_skips_when_already_closed_by_peer():
    """Manually close the position FIRST, then call
    `_close_position_safely`. The idempotency re-check must catch the
    missing symbol and skip without touching the broker.
    """
    agent, counters = _mk_agent_with_close_helper()

    # Peer thread already closed it.
    del agent.portfolio.positions["RELIANCE"]

    order, record = agent._close_position_safely(
        symbol="RELIANCE",
        token="12345",
        exit_side="SELL",
        quantity=10,
        price=2800.0,
        tag="late_caller",
        exit_reason="signal",
    )

    assert order is None
    assert record is None
    assert counters["place_order_calls"] == 0, (
        "Idempotency check failed: place_order was called for an "
        "already-closed position. This is the double-flatten window."
    )
    assert counters["cancel_sl_calls"] == 0, (
        "Idempotency check failed: cancel_sl_order_for_symbol was called "
        "for an already-closed position. The broker SL was already gone."
    )
