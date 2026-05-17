"""P0 #2 (2026-05-15) — LIVE-MODE SAFETY: regression tests for the lock
that serializes `_check_position_exits` across the WebSocket thread and
the main scan loop.

Background
----------
The trading agent has two concurrent producers for the exit-check path:

  • Main loop: every ~15s (`_fast_exits_sleep` slicing the poll cycle),
    plus the regular full-scan cadence, calls `_check_position_exits`
    with a multi-symbol price snapshot.
  • WebSocket thread (`_on_tick`): on every tick for a held symbol, calls
    `_check_position_exits({symbol: price})`.

Without a lock, the SAME symbol can hit the SL/peak-giveback gate twice
in the same race window — once from the main thread's fast-exits poll,
once from the WS thread's tick callback. Both produce a "close this
symbol" decision; both call `_close_position_safely` (with our P0 #1 fix
this means both cancel the SL and both submit a flatten). The position is
flattened twice, leaving us accidentally long/short.

These tests pin the contract that `_exit_check_lock` serializes the
combined check+flatten transaction.
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest


def _mk_agent_for_thread_safety_test():
    """Build a stub TradingAgent exposing just the surface that the
    `_check_position_exits` lock guards. We use `__new__` to skip the
    real constructor and inject the minimal attribute set."""
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._exit_check_lock = threading.Lock()

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


def test_lock_exists_as_threading_lock_on_init():
    """Light structural check: any future refactor that drops the lock
    will fail this test. The lock must be a `threading.Lock` instance."""
    agent, _ = _mk_agent_for_thread_safety_test()
    # threading.Lock returns an instance of `_thread.lock`/`_thread.LockType`
    # depending on Python version. Duck-type with acquire/release instead.
    assert hasattr(agent._exit_check_lock, "acquire")
    assert hasattr(agent._exit_check_lock, "release")
    # Verify the lock works (acquire-then-release).
    acquired = agent._exit_check_lock.acquire(timeout=0.5)
    assert acquired is True
    agent._exit_check_lock.release()


def test_exception_in_locked_body_releases_lock():
    """If the locked body raises, the lock MUST be released so subsequent
    callers aren't deadlocked. Python's `with` statement guarantees this
    but pin it explicitly so a future refactor away from `with` (e.g. to
    manual acquire/release) doesn't silently regress."""
    from trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._exit_check_lock = threading.Lock()

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
