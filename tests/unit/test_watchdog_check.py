"""Tests for tools/watchdog_check.py -- the 5-min daemon liveness probe.

Created 2026-05-25 in response to the 11-hour silent hang on 2026-05-22.
The watchdog reads logs/health.json and alerts when its mtime is older
than STALE_SECONDS during the trading window. State is kept in
logs/watchdog_state.json so we don't re-alert every 5 minutes.

The tests below pin the state-machine semantics:

  * STALE transition fires alert (in window).
  * STALE -> still STALE doesn't fire (until escalation hours pass).
  * STALE -> HEALTHY fires recovery alert.
  * Out-of-window staleness is silenced.
  * Missing health.json is treated as STALE.

Failure of any of these tests would indicate the watchdog has lost
its core deduplication discipline and would either spam or stay silent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytz

import tools.watchdog_check as wd

IST = pytz.timezone("Asia/Kolkata")


def _make_health_file(path: Path, age_seconds: float,
                      now: datetime | None = None,
                      payload: dict | None = None) -> None:
    """Write a fake health.json whose mtime is `age_seconds` before `now`.

    `now` MUST be the same datetime the test will inject into
    watchdog_check.datetime, otherwise file age (real wall clock based)
    will diverge from the script's notion of `now` (mocked) and the
    test will see an unexpected age. When `now` is omitted we fall
    back to the real filesystem mtime -- only valid for tests that
    don't override `datetime.now`."""
    import os
    payload = payload or {"cycle_count": 5, "pid": 7, "ts": "x", "open_position_count": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if now is not None:
        target_mtime = now.timestamp() - age_seconds
    else:
        target_mtime = path.stat().st_mtime - age_seconds
    os.utime(path, (target_mtime, target_mtime))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Redirect HEALTH_FILE / STATE_FILE into a tmp dir."""
    monkeypatch.setattr(wd, "HEALTH_FILE", tmp_path / "logs" / "health.json")
    monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "logs" / "watchdog_state.json")
    return tmp_path


def _trading_window_now() -> datetime:
    """A datetime guaranteed to be inside the trading window."""
    return IST.localize(datetime(2026, 5, 26, 11, 30))  # Tuesday 11:30 IST


def _weekend_now() -> datetime:
    """A datetime outside the trading window (Saturday)."""
    return IST.localize(datetime(2026, 5, 23, 11, 30))


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Trading-window discriminator
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_trading_window_includes_weekday_market_hours():
    assert wd._is_trading_window(IST.localize(datetime(2026, 5, 25, 9, 30)))
    assert wd._is_trading_window(IST.localize(datetime(2026, 5, 25, 12, 0)))
    assert wd._is_trading_window(IST.localize(datetime(2026, 5, 25, 15, 59)))


def test_trading_window_excludes_off_hours_and_weekends():
    assert not wd._is_trading_window(IST.localize(datetime(2026, 5, 25, 8, 30)))
    assert not wd._is_trading_window(IST.localize(datetime(2026, 5, 25, 16, 30)))
    assert not wd._is_trading_window(IST.localize(datetime(2026, 5, 23, 11, 30)))  # Sat
    assert not wd._is_trading_window(IST.localize(datetime(2026, 5, 24, 11, 30)))  # Sun


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# State machine: STALE transition + HEALTHY recovery
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_first_stale_in_window_fires_alert(isolated, monkeypatch):
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=900, now=now)  # 15 min stale

    sent = []

    def fake_send(cfg, subject, body):
        sent.append((subject, body))
        return True

    monkeypatch.setattr(wd, "_send_alert", fake_send)
    monkeypatch.setattr(wd, "_load_config", lambda: {})
    with patch.object(wd, "datetime", _MockDatetime(now)):
        rc = wd.main()
    assert rc == 0
    assert len(sent) == 1
    assert "SILENT" in sent[0][0]
    state = json.loads(wd.STATE_FILE.read_text(encoding="utf-8"))
    assert state["last_status"] == "STALE"
    assert state["last_alert_unix"] > 0


def test_repeated_stale_within_escalation_window_does_not_re_alert(isolated, monkeypatch):
    """If we already alerted 30 min ago, we MUST stay silent until the
    1-hour escalation boundary passes. This is the rate-limit that keeps
    a 4-hour hang from producing 48 emails."""
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=900, now=now)
    wd.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    wd.STATE_FILE.write_text(json.dumps({
        "last_status": "STALE",
        "last_alert_unix": int(now.timestamp()) - 1800,  # 30 min ago
        "first_stale_unix": int(now.timestamp()) - 1800,
    }), encoding="utf-8")

    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()

    assert sent == [], "must not re-alert before escalation window"


def test_stale_past_escalation_window_re_alerts(isolated, monkeypatch):
    """After 1 full hour of continuous staleness with no fresh alert, we
    re-alert. This catches the case where the operator missed the first
    email."""
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=4000, now=now)  # ~67 min stale
    wd.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    wd.STATE_FILE.write_text(json.dumps({
        "last_status": "STALE",
        "last_alert_unix": int(now.timestamp()) - 3700,  # 62 min ago
        "first_stale_unix": int(now.timestamp()) - 4000,
    }), encoding="utf-8")

    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    monkeypatch.setattr(wd, "_load_config", lambda: {})
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()

    assert len(sent) == 1
    assert "SILENT" in sent[0]


def test_recovery_from_stale_fires_recovered_alert(isolated, monkeypatch):
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=10, now=now)  # fresh
    wd.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    wd.STATE_FILE.write_text(json.dumps({
        "last_status": "STALE",
        "last_alert_unix": int(now.timestamp()) - 600,
        "first_stale_unix": int(now.timestamp()) - 1200,
    }), encoding="utf-8")

    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    monkeypatch.setattr(wd, "_load_config", lambda: {})
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()

    assert len(sent) == 1
    assert "recovered" in sent[0].lower()


def test_first_run_clean_state_does_not_alert(isolated, monkeypatch):
    """On the very first invocation (no state file), a HEALTHY daemon
    must not produce a 'recovered' alert -- there was nothing to recover
    FROM."""
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=10, now=now)
    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()
    assert sent == []


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Off-hours silencing -- the 'no email at 03:00 IST Sunday' guarantee
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_stale_outside_trading_window_does_not_alert(isolated, monkeypatch):
    now = _weekend_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=900, now=now)
    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()
    assert sent == []
    state = json.loads(wd.STATE_FILE.read_text(encoding="utf-8"))
    # State still tracks staleness even though we don't alert -- so when
    # Monday morning rolls around we recognise the prior-stale condition.
    assert state["last_status"] == "STALE"


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Missing health.json -- worst case (daemon never started)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_missing_health_file_treated_as_stale(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    monkeypatch.setattr(wd, "_load_config", lambda: {})
    with patch.object(wd, "datetime", _MockDatetime(_trading_window_now())):
        wd.main()
    assert len(sent) == 1
    assert "SILENT" in sent[0]


def test_stale_threshold_above_heartbeat_cadence(isolated, monkeypatch):
    """REGRESSION GUARD for the 2026-05-25 11:40 IST false positive.

    The daemon's _write_health_json runs inside the [HEARTBEAT] block,
    which fires every 5 cycles (~5m20s observed). If STALE_SECONDS is
    set tighter than that cadence, every healthy daemon will trip the
    watchdog at the tail end of each heartbeat interval.

    This test pins STALE_SECONDS at >= 2x the observed worst-case
    heartbeat cadence (~330s). 600s gives a 2x safety margin."""
    assert wd.STALE_SECONDS >= 600, (
        f"STALE_SECONDS={wd.STALE_SECONDS} is too tight relative to "
        "the daemon's 5-cycle heartbeat cadence (~5m20s). Bump to >=600."
    )

    # And a positive case: a 5-min-old health.json must NOT trip the
    # watchdog under normal heartbeat cadence.
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=320, now=now)  # 5m20s
    sent = []
    monkeypatch.setattr(wd, "_send_alert", lambda c, s, b: sent.append(s) or True)
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()
    assert sent == [], "5m20s-old health.json must not be flagged stale"


def test_alert_send_records_state_when_alertmanager_returns_none(isolated, monkeypatch):
    """REGRESSION GUARD for the 2026-05-25 11:40 IST 'alert FAILED' bug.

    AlertManager.send_alert is fire-and-forget and returns None. The
    pre-fix code treated None as failure -> last_alert_unix was never
    persisted -> no recovery alert ever fired -> escalation logic
    broken (never triggers because last_alert_unix stayed 0).

    This test injects a fake AlertManager whose send_alert returns
    None (matching reality) and verifies state is correctly persisted."""
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=900, now=now)

    class FakeAlertManager:
        def __init__(self, cfg):
            pass
        def send_alert(self, title, message, level="info"):
            return None  # exactly what real AlertManager returns

    fake_module = type("M", (), {"AlertManager": FakeAlertManager})
    monkeypatch.setitem(__import__("sys").modules, "packages.monitoring.alerts", fake_module)
    monkeypatch.setattr(wd, "_load_config", lambda: {})
    with patch.object(wd, "datetime", _MockDatetime(now)):
        wd.main()
    state = json.loads(wd.STATE_FILE.read_text(encoding="utf-8"))
    assert state["last_alert_unix"] > 0, (
        "send_alert returning None must NOT cause the watchdog to mark "
        "the alert as failed -- state must record the dispatch attempt"
    )
    assert state["last_status"] == "STALE"


def test_alert_failure_does_not_crash(isolated, monkeypatch):
    """The watchdog must NEVER crash -- if AlertManager throws, the
    cron exit code stays 0 so cron doesn't spam root@ with stderr."""
    now = _trading_window_now()
    _make_health_file(wd.HEALTH_FILE, age_seconds=900, now=now)
    monkeypatch.setattr(
        wd, "_send_alert",
        lambda c, s, b: (_ for _ in ()).throw(RuntimeError("dispatch broken")),
    )
    monkeypatch.setattr(wd, "_load_config", lambda: {})
    with patch.object(wd, "datetime", _MockDatetime(now)):
        # Either return 0, OR raise -- if it raises that's the bug.
        try:
            rc = wd.main()
        except RuntimeError:
            pytest.fail("watchdog must not propagate alert exceptions")
    assert rc == 0


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Helpers: a minimal mock for datetime that lets us control "now"
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class _MockDatetime:
    """Drop-in for the `datetime` symbol inside watchdog_check; only
    overrides .now(tz). Other class attributes (timedelta, etc.) are
    served from the real module so e.g. fromtimestamp() still works."""

    def __init__(self, fixed_now: datetime):
        self._fixed = fixed_now

    def now(self, tz=None):
        if tz is None:
            return self._fixed.replace(tzinfo=None)
        return self._fixed.astimezone(tz)

    def __getattr__(self, name):
        return getattr(datetime, name)
