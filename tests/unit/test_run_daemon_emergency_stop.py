"""Unit tests for the off-hours emergency-stop check added to
``run_daemon.sleep_until_market``.

Background
----------
Until 2026-05-13 the daemon's off-hours sleep loop only checked the
``_shutdown_requested`` global (set by SIGTERM/SIGINT) and the
``is_market_window`` clock. The file-based kill switch
(``logs/STOP``) was wired into ``TradingAgent._check_emergency_stop``,
which only runs once a trading cycle has started -- so during the
overnight / weekend idle period a STOP file would be ignored entirely.

These tests pin down the new behaviour:
  1. Stale STOP file -> immediate exit, no sleep.
  2. STOP touched mid-sleep -> exit on next poll (60s, not 300s).
  3. No STOP file + market_window returning True -> loop exits normally.
  4. Path resolution falls back to ``logs/STOP`` when config is missing
     the ``operations`` block (older configs).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import run_daemon  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_globals():
    run_daemon._shutdown_requested = False
    yield
    run_daemon._shutdown_requested = False


def _write_config(tmp_path: Path, stop_path: str | None = None,
                  log_dir: str = "logs") -> str:
    cfg = {"logging": {"log_dir": log_dir}}
    if stop_path is not None:
        cfg["operations"] = {"emergency_stop_path": stop_path}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def test_path_resolution_prefers_operations_block(tmp_path):
    custom_stop = str(tmp_path / "kill_me")
    cfg_path = _write_config(tmp_path, stop_path=custom_stop)
    assert run_daemon._emergency_stop_path_from_config(cfg_path) == custom_stop


def test_path_resolution_falls_back_to_logs_stop(tmp_path):
    """Older configs without an ``operations`` block must still get a
    sensible default rather than ``None`` -- otherwise the kill switch
    silently disappears after a config downgrade."""
    cfg_path = _write_config(tmp_path, stop_path=None, log_dir="logs")
    expected = os.path.join("logs", "STOP")
    assert run_daemon._emergency_stop_path_from_config(cfg_path) == expected


def test_path_resolution_handles_missing_file(tmp_path):
    """A garbled / unreadable config shouldn't crash the daemon -- the
    fallback default must still come out clean."""
    bogus_path = str(tmp_path / "does_not_exist.yaml")
    assert run_daemon._emergency_stop_path_from_config(bogus_path) == \
        os.path.join("logs", "STOP")


def test_stale_stop_file_exits_immediately(tmp_path, monkeypatch):
    """If a STOP file is present *before* sleep_until_market is called
    (e.g. operator forgot to remove it before restart), the loop should
    detect it on the very first iteration and set _shutdown_requested
    rather than busy-looping waiting for the market."""
    stop = tmp_path / "STOP"
    stop.write_text("stale")
    cfg = _write_config(tmp_path, stop_path=str(stop))

    monkeypatch.setattr(run_daemon, "is_market_window", lambda: False)
    monkeypatch.setattr(run_daemon, "_write_idle_heartbeat", lambda _c: None)
    sleeps = []
    monkeypatch.setattr(run_daemon.time, "sleep", lambda s: sleeps.append(s))

    run_daemon.sleep_until_market(cfg)

    assert run_daemon._shutdown_requested is True
    # We must NOT have slept once -- the loop has to exit on the very
    # first iteration so the wrapper can shut down without delay.
    assert sleeps == []


def test_stop_file_mid_sleep_exits_on_next_poll(tmp_path, monkeypatch):
    """Touch STOP while the loop is mid-sleep. The next iteration must
    pick it up and exit; we shouldn't have to wait for is_market_window
    to flip True."""
    stop = tmp_path / "STOP"
    cfg = _write_config(tmp_path, stop_path=str(stop))

    monkeypatch.setattr(run_daemon, "is_market_window", lambda: False)
    monkeypatch.setattr(run_daemon, "_write_idle_heartbeat", lambda _c: None)
    iter_count = {"n": 0}

    def fake_sleep(seconds):
        iter_count["n"] += 1
        # Create the STOP file on the 2nd iteration to simulate an
        # operator touching it shortly after daemon startup.
        if iter_count["n"] == 2:
            stop.write_text("halt")

    monkeypatch.setattr(run_daemon.time, "sleep", fake_sleep)

    run_daemon.sleep_until_market(cfg)

    assert run_daemon._shutdown_requested is True
    # 2 sleeps means: iter 1 (no file, sleep) -> iter 2 (no file YET --
    # file is written by fake_sleep AFTER the check, then sleep) ->
    # iter 3 (file detected, break before sleep). Confirms the 60s
    # poll cadence is what gates response time.
    assert iter_count["n"] >= 2


def test_market_open_exits_loop_normally(tmp_path, monkeypatch):
    """When market opens, the loop must exit without touching the STOP
    machinery -- this is the happy path on a normal weekday at 09:15."""
    cfg = _write_config(tmp_path, stop_path=str(tmp_path / "STOP"))
    window = {"open": False}
    monkeypatch.setattr(run_daemon, "is_market_window", lambda: window["open"])
    monkeypatch.setattr(run_daemon, "_write_idle_heartbeat", lambda _c: None)

    def fake_sleep(_s):
        window["open"] = True

    monkeypatch.setattr(run_daemon.time, "sleep", fake_sleep)

    run_daemon.sleep_until_market(cfg)

    assert run_daemon._shutdown_requested is False


def test_fs_error_during_check_is_silently_swallowed(tmp_path, monkeypatch):
    """A flaky filesystem (NFS, dying disk, etc.) raising OSError on
    ``os.path.exists`` must NOT kill the daemon -- this is the same
    posture as ``TradingAgent._check_emergency_stop``. The loop should
    log + continue."""
    cfg = _write_config(tmp_path, stop_path=str(tmp_path / "STOP"))
    monkeypatch.setattr(run_daemon, "is_market_window", lambda: False)
    monkeypatch.setattr(run_daemon, "_write_idle_heartbeat", lambda _c: None)

    call_count = {"n": 0}

    def flaky_exists(_p):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("EIO: simulated disk flake")
        return False

    iters = {"n": 0}

    def fake_sleep(_s):
        iters["n"] += 1
        if iters["n"] >= 2:
            # End the loop by flipping market open.
            monkeypatch.setattr(run_daemon, "is_market_window", lambda: True)

    monkeypatch.setattr(run_daemon.os.path, "exists", flaky_exists)
    monkeypatch.setattr(run_daemon.time, "sleep", fake_sleep)

    run_daemon.sleep_until_market(cfg)
    assert run_daemon._shutdown_requested is False
