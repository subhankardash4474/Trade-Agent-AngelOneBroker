"""Unit tests for the post-market-close transition in
``run_daemon.main`` -- the fix that prevents the 2026-05-13 EOD
restart loop.

Background
----------
The agent has exactly one *voluntary* clean-exit path: after market
close (15:30 IST), ``TradingAgent._trading_cycle`` sets
``self._running = False`` and ``agent.run()`` returns. Until today the
wrapper handled this by ``break``-ing out of its main loop, which let
the Python process exit, which let Docker's ``restart: unless-stopped``
policy re-create the container, which ran the agent again, which
re-fired the EOD trio (Summary + Post-Mortem + Profit Diagnostic) and
exited again -- approximately once every 3 minutes between 15:30 and
16:00 IST.

The patched wrapper detects "agent self-exited at >= 15:30 IST on a
weekday" and transitions directly to ``sleep_until_market`` instead of
breaking. These tests pin that behaviour.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import run_daemon  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _reset_globals():
    run_daemon._shutdown_requested = False
    yield
    run_daemon._shutdown_requested = False


class _FakeArgs:
    """Minimal stand-in for argparse.Namespace covering only the fields
    ``main`` actually reads after the parsing happens."""

    def __init__(self, market_hours_only=True):
        self.config = "config.yaml"
        self.paper = True
        self.dashboard = False
        self.interval = 60
        self.market_hours_only = market_hours_only
        self.reset_balance = False
        self.max_loss_rs = None
        self.single_shot = False
        # Added 2026-05-14 for Stage 3 prep -- run_daemon.main() now reads
        # these fields. Default to paper-equivalent (no overlay, not live).
        self.live = False
        self.config_overlay = None


def _run_main_one_pass(args, run_once_side_effect, is_market_window_seq,
                       sleep_until_market_side_effect, now_seq,
                       max_iters: int = 5):
    """Drive ``run_daemon.main`` through a bounded number of iterations
    by stubbing out everything that would talk to the world, then
    verifying which branches were exercised.

    Returns a dict of call counters so individual tests can assert.
    """
    calls = {
        "run_once": 0,
        "sleep_until_market": 0,
        "is_market_window": 0,
    }
    now_iter = iter(now_seq)

    def fake_run_once(*a, **kw):
        calls["run_once"] += 1
        if run_once_side_effect:
            run_once_side_effect(calls["run_once"])

    def fake_is_market_window():
        calls["is_market_window"] += 1
        try:
            return next(is_market_window_seq)
        except StopIteration:
            # Forcibly end the loop by signalling shutdown -- prevents
            # infinite spin if a test under-specifies the sequence.
            run_daemon._shutdown_requested = True
            return False

    def fake_sleep_until_market(_cfg):
        calls["sleep_until_market"] += 1
        if sleep_until_market_side_effect:
            sleep_until_market_side_effect(calls["sleep_until_market"])

    def fake_datetime_now(tz=None):  # noqa: ARG001 -- match signature
        try:
            return next(now_iter)
        except StopIteration:
            return datetime(2026, 5, 13, 16, 5, 0, tzinfo=IST)

    with patch.object(run_daemon, "run_once", side_effect=fake_run_once), \
         patch.object(run_daemon, "is_market_window", side_effect=fake_is_market_window), \
         patch.object(run_daemon, "sleep_until_market", side_effect=fake_sleep_until_market), \
         patch.object(run_daemon, "datetime") as mock_dt, \
         patch.object(run_daemon, "argparse") as mock_argparse, \
         patch.object(run_daemon.time, "sleep", lambda _s: None):
        mock_dt.now.side_effect = fake_datetime_now
        mock_argparse.ArgumentParser.return_value.parse_args.return_value = args

        # Safety net: after max_iters, force shutdown so a regression that
        # spins forever doesn't hang the test runner.
        original_signal_handler = run_daemon._signal_handler

        def safety_net(sig, frame):
            original_signal_handler(sig, frame)

        # We can't easily preempt main(), so we rely on the
        # is_market_window sequence ending to terminate.
        run_daemon.main()

    return calls


def test_clean_exit_post_close_transitions_to_sleep_not_break():
    """The bug: agent.run() returns at 15:30:30 IST. Old code did
    ``break`` and let Docker handle the next-day restart, which meant
    Docker actually restarted the container 10x in the next 30 min.
    New code must detect "post-close clean exit" and call
    ``sleep_until_market`` directly."""
    args = _FakeArgs(market_hours_only=True)

    # Sequence:
    #  iter 1: is_market_window=True (we're in window) -> run_once ->
    #          agent self-exits at 15:31 IST -> wrapper should call
    #          sleep_until_market -> sleep_until_market signals shutdown
    #          via the global flag.
    #  iter 2: never reached because shutdown was set.
    def stop_after_sleep(_n):
        run_daemon._shutdown_requested = True

    calls = _run_main_one_pass(
        args=args,
        run_once_side_effect=None,
        is_market_window_seq=iter([True]),
        sleep_until_market_side_effect=stop_after_sleep,
        now_seq=[datetime(2026, 5, 13, 15, 31, 0, tzinfo=IST)],
    )

    assert calls["run_once"] == 1, (
        "Agent should have been launched exactly once -- the loop must "
        "NOT relaunch after a post-close clean exit."
    )
    assert calls["sleep_until_market"] == 1, (
        "Post-close clean exit must transition directly to "
        "sleep_until_market instead of breaking out of the wrapper."
    )


def test_clean_exit_before_close_still_breaks():
    """Defensive: if for some reason agent.run() returns BEFORE 15:30
    IST (manual stop via stop_daemon.py, e.g.), the wrapper should
    still break out so the operator's intent (stop the daemon) is
    honoured. Don't accidentally turn manual stops into "sleep till
    tomorrow"."""
    args = _FakeArgs(market_hours_only=True)
    # Agent exits at 11:00 IST -- well before any close window.
    calls = _run_main_one_pass(
        args=args,
        run_once_side_effect=None,
        is_market_window_seq=iter([True]),
        sleep_until_market_side_effect=None,
        now_seq=[datetime(2026, 5, 13, 11, 0, 0, tzinfo=IST)],
    )

    assert calls["run_once"] == 1
    assert calls["sleep_until_market"] == 0, (
        "Pre-close clean exit must NOT call sleep_until_market -- it "
        "should break and let the operator decide what's next."
    )


def test_weekend_clean_exit_breaks_not_sleeps():
    """The post-close check is gated on weekday < 5 because Saturday/
    Sunday calls into the close window aren't real. Belt-and-braces:
    we shouldn't even be running at 15:31 IST on a weekend (the agent
    wouldn't launch), but if we somehow are, treat it as a manual stop
    not a market close."""
    args = _FakeArgs(market_hours_only=True)
    # 2026-05-16 is a Saturday.
    calls = _run_main_one_pass(
        args=args,
        run_once_side_effect=None,
        is_market_window_seq=iter([True]),
        sleep_until_market_side_effect=None,
        now_seq=[datetime(2026, 5, 16, 15, 31, 0, tzinfo=IST)],
    )

    assert calls["run_once"] == 1
    assert calls["sleep_until_market"] == 0, (
        "Weekend post-close exit must not be auto-routed to sleep -- "
        "the weekday < 5 gate exists for exactly this reason."
    )


def test_bug_n_is_market_window_closes_at_1530_ist():
    """Bug N (2026-05-29): pin the upper bound of ``is_market_window``.

    Pre-fix, the function used ``hour < 16`` (window stayed open until
    15:59:59 IST). The agent self-exits at 15:30 IST, so between 15:30
    and 16:00 the outer wrapper saw ``is_market_window()=True``, skipped
    ``sleep_until_market``, and re-launched the agent on a 30-minute
    spurious-restart loop -- 22 restarts/day on the trader VM, masked
    by alert-dedup. The fix tightens the upper bound to ``< 15:30 IST``
    so the next supervisor iteration immediately falls into the sleep
    branch.

    Test cases hand-picked across the 15:00-16:00 IST hour so a
    regression that flips the comparison back to hour-resolution would
    fail loudly.
    """
    from unittest.mock import patch as _patch

    cases = [
        # (hour, minute, expected, label)
        (15, 29, True,  "15:29 IST -- still inside 15:30 cutoff"),
        (15, 29, True,  "15:29 IST repeated -- determinism check"),
        (15, 30, False, "15:30 IST EXACTLY -- agent self-exits here"),
        (15, 31, False, "15:31 IST -- previously caused Bug N flap loop"),
        (15, 45, False, "15:45 IST -- middle of the old-bug flap window"),
        (15, 59, False, "15:59 IST -- pre-fix would still return True here"),
        (16, 0,  False, "16:00 IST -- post-cutoff under any interpretation"),
        (8,  0,  True,  "08:00 IST -- pre-market boot window opens"),
        (7,  59, False, "07:59 IST -- before pre-market window opens"),
    ]

    for hour, minute, expected, label in cases:
        # Weekday Tuesday (2026-05-12) so the weekend gate doesn't kick in.
        fake_now = datetime(2026, 5, 12, hour, minute, 0, tzinfo=IST)
        with _patch.object(run_daemon, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            assert run_daemon.is_market_window() is expected, (
                f"is_market_window() failed at {label}: "
                f"expected {expected} for {hour:02d}:{minute:02d} IST"
            )


def test_bug_n_post_close_loop_does_not_relaunch_agent():
    """Bug N regression: even if a future change re-widens
    ``is_market_window()`` to keep returning True past 15:30 IST,
    the supervisor's ``past_close`` branch must NOT relaunch the agent.

    Pre-fix:
      iter1: is_market_window=True -> run_once (#1) -> agent self-exits
             at 15:31 -> past_close=True -> continue
      iter2: is_market_window=True (still!) -> run_once (#2) -> exit again
      iter3: is_market_window=True -> run_once (#3) -> exit again
      ... 22 times until is_market_window finally flips False at 16:00.

    Post-fix: ``run_once`` is called exactly once, then the explicit
    ``sleep_until_market`` (defence-in-depth) keeps us out of the
    relaunch path even if ``is_market_window`` is wrong.
    """
    args = _FakeArgs(market_hours_only=True)

    # Simulate the pre-fix is_market_window: returns True 10 times in a
    # row, mimicking the 30-min flap window. If our supervisor logic
    # is correct, the test will end inside the first iteration -- we
    # should never consume more than 2-3 of those Trues.
    is_window_seq = iter([True] * 10)

    def stop_after_sleep(_n):
        run_daemon._shutdown_requested = True

    calls = _run_main_one_pass(
        args=args,
        run_once_side_effect=None,
        is_market_window_seq=is_window_seq,
        sleep_until_market_side_effect=stop_after_sleep,
        # Every datetime.now(IST) inside the loop returns 15:31 IST.
        now_seq=[datetime(2026, 5, 12, 15, 31, i, tzinfo=IST) for i in range(20)],
    )

    assert calls["run_once"] == 1, (
        f"Bug N regression: agent was launched {calls['run_once']} times "
        f"in the post-close branch instead of exactly 1. The supervisor "
        f"is back in the 30-min flap loop."
    )
    assert calls["sleep_until_market"] == 1, (
        f"Bug N regression: expected exactly 1 explicit "
        f"sleep_until_market call (defence-in-depth in the past_close "
        f"branch), got {calls['sleep_until_market']}."
    )


def test_non_market_hours_mode_does_not_trigger_post_close_branch():
    """When ``--market-hours-only`` is False (24/7 mode, used during
    paper-trading replays and integration tests), the post-close
    transition must NOT fire -- the wrapper should keep relaunching
    the agent indefinitely until manual stop, because the operator
    explicitly opted out of the market-hours gate."""
    args = _FakeArgs(market_hours_only=False)

    # With market_hours_only=False, sleep_until_market should never be
    # called. The agent exits cleanly -> break, then the outer while
    # loop exits because _shutdown_requested gets flipped (we'll do
    # that from run_once_side_effect).
    def end_after_one_run(_n):
        run_daemon._shutdown_requested = True

    calls = _run_main_one_pass(
        args=args,
        run_once_side_effect=end_after_one_run,
        is_market_window_seq=iter([True, True, True]),
        sleep_until_market_side_effect=None,
        now_seq=[datetime(2026, 5, 13, 15, 31, 0, tzinfo=IST)],
    )

    assert calls["run_once"] >= 1
    assert calls["sleep_until_market"] == 0, (
        "market_hours_only=False must skip the post-close transition; "
        "the gate exists to honour the operator's intent."
    )
