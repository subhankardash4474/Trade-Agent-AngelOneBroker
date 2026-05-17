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
