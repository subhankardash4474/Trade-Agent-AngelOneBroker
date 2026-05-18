"""P1 #13, P1 #14 (2026-05-17) -- LIVE-MODE SAFETY: WebSocket client.

Background. Two distinct holes in the WS layer:

  * P1 #13: ``on_close`` just logged and let the thread die while
    ``_running`` stayed True. Ticks stopped silently for the rest of
    the day -- no exits fired, no alarm.

  * P1 #14: ``subscribe()`` was upsert-only. After a position closed,
    ``_subscriptions`` still held its symbol+token; the next reconnect
    would re-subscribe to the closed name. Held-only mode was leaky.

Plus a P2 fix in the same file:
  * ``_token_to_symbol`` used strict string equality, but Angel ticks
    sometimes return tokens as ints while ``_subscriptions`` stored
    strings -- ticks for matching symbols were silently dropped.
"""
from __future__ import annotations

from unittest import mock

import pytest

from core.websocket_client import WebSocketClient


def _make_client():
    return WebSocketClient(broker="angelone", config={}, smart_api=None)


# ── P1 #14 -- subscribe semantics ──────────────────────────────────────────


def test_subscribe_is_additive():
    """Back-compat: subscribe() upserts each symbol without removing
    pre-existing entries."""
    c = _make_client()
    c.subscribe([{"symbol": "HDFCBANK", "token": "1330"}])
    c.subscribe([{"symbol": "INFY", "token": "1594"}])
    assert "HDFCBANK" in c._subscriptions
    assert "INFY" in c._subscriptions


def test_set_subscriptions_replaces_wholesale():
    """P1 #14: set_subscriptions REPLACES the dict so closed positions
    are dropped, not upserted."""
    c = _make_client()
    c.subscribe([
        {"symbol": "HDFCBANK", "token": "1330"},
        {"symbol": "INFY", "token": "1594"},
        {"symbol": "TCS", "token": "11536"},
    ])
    # Now imagine HDFCBANK and TCS closed and only INFY is still held.
    c.set_subscriptions([{"symbol": "INFY", "token": "1594"}])
    assert set(c._subscriptions) == {"INFY"}
    assert "HDFCBANK" not in c._subscriptions
    assert "TCS" not in c._subscriptions


def test_set_subscriptions_clears_when_empty():
    """Empty list (no positions held) must result in an empty subscription
    set so no broker ticks come through."""
    c = _make_client()
    c.subscribe([{"symbol": "HDFCBANK", "token": "1330"}])
    c.set_subscriptions([])
    assert c._subscriptions == {}


def test_unsubscribe_returns_true_when_removed():
    c = _make_client()
    c.subscribe([{"symbol": "HDFCBANK", "token": "1330"}])
    assert c.unsubscribe("HDFCBANK") is True
    assert "HDFCBANK" not in c._subscriptions


def test_unsubscribe_returns_false_when_absent():
    c = _make_client()
    assert c.unsubscribe("NEVER_SUBSCRIBED") is False


def test_subscriptions_store_tokens_as_strings():
    """Tokens may come from JSON as int; the dict must normalize to str
    so _token_to_symbol can do string comparison reliably."""
    c = _make_client()
    c.subscribe([{"symbol": "HDFCBANK", "token": 1330}])  # int
    assert c._subscriptions["HDFCBANK"] == "1330"


# ── P2 token-mismatch -- _token_to_symbol ──────────────────────────────────


def test_token_to_symbol_matches_int_token_against_string_subscription():
    """Angel tick payload has token as int 11536; subscription dict stores
    it as str "11536". Old strict-equality dropped the tick. New code
    casts both to str."""
    c = _make_client()
    c.subscribe([{"symbol": "TCS", "token": "11536"}])
    assert c._token_to_symbol(11536) == "TCS"   # int input
    assert c._token_to_symbol("11536") == "TCS"  # str input


def test_token_to_symbol_returns_none_for_unknown_token():
    c = _make_client()
    c.subscribe([{"symbol": "TCS", "token": "11536"}])
    assert c._token_to_symbol("99999") is None


def test_token_to_symbol_handles_whitespace():
    """Tokens occasionally come with stray whitespace; trim before compare."""
    c = _make_client()
    c.subscribe([{"symbol": "TCS", "token": "11536 "}])
    assert c._token_to_symbol(" 11536") == "TCS"


# ── P1 #13 -- reconnect-loop semantics ─────────────────────────────────────


def test_reconnect_loop_does_not_break_after_one_success():
    """The OLD code had ``break`` after a successful _run_*; so when the
    SECOND session also closed, the daemon was tickless. New code stays
    in the loop while _running is True."""
    c = _make_client()
    c._running = True

    # Simulate _run_angelone returning normally THREE times before _running
    # flips to False (mimics 3 reconnect attempts, then stop()).
    call_count = {"n": 0}

    def fake_run():
        call_count["n"] += 1
        if call_count["n"] >= 3:
            c._running = False

    c._run_angelone = fake_run
    # Patch sleep to avoid the 2-60s backoff during the test
    with mock.patch("core.websocket_client.time.sleep", lambda *_: None):
        c._reconnect_loop()

    assert call_count["n"] == 3, (
        "P1 #13 regression: reconnect loop exited after the first "
        "successful _run, so subsequent session ends would leave the "
        "daemon tickless."
    )


def test_reconnect_loop_resets_backoff_on_clean_return():
    """A clean session-end (no exception) should reset the backoff so a
    healthy daily cycle doesn't slowly stretch the reconnect delay to 60s."""
    c = _make_client()
    c._running = True

    delays_seen = []

    def fake_sleep(d):
        delays_seen.append(d)
        # Stop after the third sleep so the test doesn't loop forever
        if len(delays_seen) >= 3:
            c._running = False

    def fake_run():
        return  # clean exit (no raise)

    c._run_angelone = fake_run
    with mock.patch("core.websocket_client.time.sleep", side_effect=fake_sleep):
        c._reconnect_loop()
    # All sleeps should be the initial backoff (2s) -- backoff resets.
    assert all(d == 2 for d in delays_seen), delays_seen


def test_reconnect_loop_grows_backoff_on_exception():
    c = _make_client()
    c._running = True

    delays_seen = []

    def fake_sleep(d):
        delays_seen.append(d)
        if len(delays_seen) >= 4:
            c._running = False

    def fake_run():
        raise RuntimeError("simulated connection refusal")

    c._run_angelone = fake_run
    with mock.patch("core.websocket_client.time.sleep", side_effect=fake_sleep):
        c._reconnect_loop()
    # Backoff doubles: 2, 4, 8, 16 -- cap at 60.
    assert delays_seen == [2, 4, 8, 16]


def test_reconnect_loop_caps_at_60s():
    c = _make_client()
    c._running = True
    delays_seen = []

    def fake_sleep(d):
        delays_seen.append(d)
        if len(delays_seen) >= 10:
            c._running = False

    def fake_run():
        raise RuntimeError("down")

    c._run_angelone = fake_run
    with mock.patch("core.websocket_client.time.sleep", side_effect=fake_sleep):
        c._reconnect_loop()
    assert max(delays_seen) <= 60


def test_reconnect_loop_simulation_broker_returns_immediately():
    """Simulation mode has no broker; reconnect must not loop infinitely."""
    c = WebSocketClient(broker="simulation", config={}, smart_api=None)
    c._running = True
    with mock.patch("core.websocket_client.time.sleep", lambda *_: None):
        c._reconnect_loop()
    # Did not crash; just returned.


# ── Regression #7 (2026-05-18) -- Kite tick exchange_timestamp ────────────


def _make_kite_client():
    c = WebSocketClient(broker="kite", config={}, smart_api=None)
    c.subscribe([{"symbol": "TCS", "token": "11536"}])
    return c


def test_kite_tick_carries_exchange_timestamp_when_datetime():
    """KiteConnect hands exchange_timestamp as a datetime. The aggregator
    must receive it so candle bucketing uses event time, not wall-clock.
    Closes the same bucket-skew bug the Angel patch fixed."""
    from datetime import datetime as _dt

    import pytz as _pytz

    received = []
    c = _make_kite_client()
    c.on_tick = lambda t: received.append(t)
    ist = _pytz.timezone("Asia/Kolkata")
    event_time = ist.localize(_dt(2026, 5, 18, 10, 30, 15))

    c._handle_kite_tick({
        "instrument_token": "11536",
        "last_price": 3500.0,
        "volume_traded": 1000,
        "exchange_timestamp": event_time,
    })
    assert len(received) == 1
    assert "timestamp" in received[0]
    assert received[0]["timestamp"] == event_time


def test_kite_tick_localizes_naive_datetime():
    """A naive datetime from Kite must be treated as IST so downstream
    comparisons don't mix naive and aware values."""
    from datetime import datetime as _dt

    received = []
    c = _make_kite_client()
    c.on_tick = lambda t: received.append(t)

    naive = _dt(2026, 5, 18, 10, 30, 15)
    c._handle_kite_tick({
        "instrument_token": "11536",
        "last_price": 3500.0,
        "exchange_timestamp": naive,
    })
    ts = received[0]["timestamp"]
    assert ts.tzinfo is not None
    assert ts.hour == 10 and ts.minute == 30


def test_kite_tick_falls_back_to_last_trade_time():
    """If exchange_timestamp is absent but last_trade_time is present,
    use that instead -- Kite's docs list both as a usable event time."""
    from datetime import datetime as _dt

    import pytz as _pytz

    received = []
    c = _make_kite_client()
    c.on_tick = lambda t: received.append(t)
    ist = _pytz.timezone("Asia/Kolkata")
    event_time = ist.localize(_dt(2026, 5, 18, 11, 45, 0))

    c._handle_kite_tick({
        "instrument_token": "11536",
        "last_price": 3500.0,
        "last_trade_time": event_time,
    })
    assert received[0]["timestamp"] == event_time


def test_kite_tick_missing_timestamp_does_not_break():
    """Defensive: an old / sparse tick without any event-time field must
    still propagate (the aggregator will fall back to wall-clock as
    before, but the tick itself is not dropped)."""
    received = []
    c = _make_kite_client()
    c.on_tick = lambda t: received.append(t)

    c._handle_kite_tick({
        "instrument_token": "11536",
        "last_price": 3500.0,
    })
    assert len(received) == 1
    assert "timestamp" not in received[0]


# ── P1 (2026-05-18) -- threaded reconnect dispatch + on_error wiring ───


def test_schedule_reconnect_spawns_a_thread_once():
    """The first call spawns a daemon thread running _reconnect_loop;
    subsequent calls while that thread is alive must be no-ops."""
    c = _make_client()
    c._running = True

    # Replace _reconnect_loop with a slow-running stub so we can race
    # multiple _schedule_reconnect calls against it.
    started_count = {"n": 0}
    release = mock.MagicMock()

    def slow_loop():
        started_count["n"] += 1
        # Hold the thread alive for the duration of the test.
        while c._running:
            release()
            import time as _t
            _t.sleep(0.01)

    c._reconnect_loop = slow_loop

    c._schedule_reconnect()
    c._schedule_reconnect()
    c._schedule_reconnect()

    # Give the worker a beat to actually start running.
    import time as _t
    for _ in range(50):
        if started_count["n"] >= 1:
            break
        _t.sleep(0.01)

    try:
        assert started_count["n"] == 1, (
            "P1 (2026-05-18) regression: _schedule_reconnect must be "
            "idempotent while a reconnect thread is alive. Saw "
            f"{started_count['n']} starts."
        )
    finally:
        c._running = False
        c._reconnect_thread.join(timeout=1.0)


def test_schedule_reconnect_noops_when_not_running():
    """stop() flips _running to False; subsequent schedule_reconnect calls
    must be silent no-ops so we don't fight with shutdown."""
    c = _make_client()
    c._running = False

    started = {"n": 0}
    c._reconnect_loop = lambda: started.__setitem__("n", started["n"] + 1)
    c._schedule_reconnect()

    import time as _t
    _t.sleep(0.05)
    assert started["n"] == 0


def test_schedule_reconnect_respawns_after_previous_thread_exits():
    """After a previous reconnect-loop thread cleanly exits (e.g. the
    daemon transitioned through a stop()/start() cycle), a fresh
    _schedule_reconnect call must be able to spawn again."""
    c = _make_client()
    c._running = True

    runs = []

    def quick_loop():
        runs.append(1)
        return  # exits immediately

    c._reconnect_loop = quick_loop
    c._schedule_reconnect()
    # Wait for first thread to finish.
    if c._reconnect_thread is not None:
        c._reconnect_thread.join(timeout=1.0)
    c._schedule_reconnect()
    if c._reconnect_thread is not None:
        c._reconnect_thread.join(timeout=1.0)

    assert len(runs) == 2


# ── on_error wiring (per-broker handler is created inside _run_*; we
#    can't easily intercept it without launching the broker SDK. Instead
#    we test the public surface: _schedule_reconnect is the new single
#    entry point. The hand-off from on_error is exercised in an
#    integration-style test in tests/integration when the SDK is
#    stubbable.) ────────────────────────────────────────────────────────
