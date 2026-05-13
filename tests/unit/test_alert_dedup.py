"""Unit tests for the persistent alert dedup in ``packages/monitoring/alerts.py``.

Why this matters
----------------
On 2026-05-13 the operator received 33 duplicate emails (11 EOD Summary
+ 11 Trade Post-Mortem + 11 Profit Diagnostic) because the daemon was
being restarted between 15:20 and 16:00 IST. Each restart re-fired the
full EOD trio. The in-memory ``_eod_summary_sent`` flag resets on every
restart, and ``AlertManager`` had no memory of previously-sent alerts,
so identical content went out on every cycle.

These tests pin down the persistent (disk-backed) dedup so a future
crash/restart loop can't reproduce that incident.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packages.monitoring.alerts import AlertManager  # noqa: E402


def _cfg(tmp_path, ttl_minutes=60, email_enabled=True):
    """Build a minimal alerts config rooted at a tmp_path so each test
    gets its own dedup state file.

    Default ``email_enabled=True`` so the tests reach ``_send_email``
    (which is patched). The one test that needs the top-level alerts
    flag flipped off (``test_disabled_alert_manager_skips_dedup_state``)
    overrides the resulting config directly.
    """
    return {
        "monitoring": {
            "alerts": {
                "enabled": True,
                "email": {
                    "enabled": email_enabled,
                    "provider": "resend",
                    "resend_api_key": "test_key",
                    "sender": "test@example.com",
                    "recipient": "ops@example.com",
                },
                "dedup": {
                    "ttl_minutes": ttl_minutes,
                    "state_path": str(tmp_path / "dedup.json"),
                },
            }
        }
    }


def test_first_alert_passes_and_state_is_recorded(tmp_path):
    """Send once, verify the email path is hit and the state file lists
    the fingerprint with a recent timestamp."""
    am = AlertManager(_cfg(tmp_path))
    with patch.object(am, "_send_email") as mock_send:
        am.send_alert("EOD Summary", "Day PnL: Rs -146.87\nTrades: 4")
    mock_send.assert_called_once()

    state_path = Path(_cfg(tmp_path)["monitoring"]["alerts"]["dedup"]["state_path"])
    state = json.loads(state_path.read_text())
    assert len(state) == 1
    fp, ts = next(iter(state.items()))
    assert isinstance(fp, str) and len(fp) == 16
    assert abs(int(time.time()) - int(ts)) < 5  # recorded ~now


def test_duplicate_within_ttl_is_suppressed(tmp_path):
    """Same (title, message) sent twice within TTL -> only first send."""
    am = AlertManager(_cfg(tmp_path, ttl_minutes=60))
    with patch.object(am, "_send_email") as mock_send:
        am.send_alert("EOD Summary", "Day PnL: Rs -146.87")
        am.send_alert("EOD Summary", "Day PnL: Rs -146.87")
        am.send_alert("EOD Summary", "Day PnL: Rs -146.87")
    assert mock_send.call_count == 1


def test_different_body_is_not_duplicate(tmp_path):
    """Same title with different body must NOT be suppressed -- the
    fingerprint covers both fields, otherwise we'd silently swallow a
    second risk alert that happens to share a subject with the first."""
    am = AlertManager(_cfg(tmp_path))
    with patch.object(am, "_send_email") as mock_send:
        am.send_alert("Trade: SELL HCLTECH", "SELL 16 x HCLTECH @ 1142.35")
        am.send_alert("Trade: SELL HCLTECH", "SELL 16 x HCLTECH @ 1150.40")
    assert mock_send.call_count == 2


def test_dedup_survives_new_manager_instance(tmp_path):
    """Critical: this is the actual 2026-05-13 bug. AlertManager #1 sends
    EOD, daemon crashes, AlertManager #2 is constructed and tries to
    send the same EOD again. With persistent state on disk, the second
    send must be suppressed."""
    cfg = _cfg(tmp_path)

    am1 = AlertManager(cfg)
    with patch.object(am1, "_send_email") as mock1:
        am1.send_alert("EOD Summary", "Day PnL: Rs -146.87")
    mock1.assert_called_once()

    am2 = AlertManager(cfg)  # fresh instance, like after a daemon restart
    with patch.object(am2, "_send_email") as mock2:
        am2.send_alert("EOD Summary", "Day PnL: Rs -146.87")
    mock2.assert_not_called()


def test_expired_dedup_entry_allows_resend(tmp_path):
    """After TTL expires, the same alert is allowed through again. We
    don't want stale state to silently swallow alerts forever (e.g. an
    alert that fires once a day for the same condition)."""
    cfg = _cfg(tmp_path, ttl_minutes=60)
    am = AlertManager(cfg)
    state_path = Path(cfg["monitoring"]["alerts"]["dedup"]["state_path"])

    # First send.
    with patch.object(am, "_send_email") as mock_send:
        am.send_alert("Daily Heartbeat", "uptime ok")
    mock_send.assert_called_once()

    # Rewrite state with a timestamp from 2 hours ago.
    state = json.loads(state_path.read_text())
    fp = next(iter(state))
    state[fp] = int(time.time()) - 2 * 3600
    state_path.write_text(json.dumps(state))

    # Second send: should pass through because the recorded ts is past TTL.
    am2 = AlertManager(cfg)
    with patch.object(am2, "_send_email") as mock_send:
        am2.send_alert("Daily Heartbeat", "uptime ok")
    mock_send.assert_called_once()


def test_dedup_disabled_when_ttl_zero(tmp_path):
    """An operator who explicitly disables dedup (ttl_minutes=0) must
    see every send go through. Useful when debugging a flapping
    service and you WANT the spam."""
    am = AlertManager(_cfg(tmp_path, ttl_minutes=0))
    with patch.object(am, "_send_email") as mock_send:
        am.send_alert("Heartbeat", "ok")
        am.send_alert("Heartbeat", "ok")
        am.send_alert("Heartbeat", "ok")
    assert mock_send.call_count == 3


def test_eleven_identical_eod_emails_only_send_once(tmp_path):
    """Direct replay of the 2026-05-13 incident: simulate 11 daemon
    restarts each trying to send the same EOD summary. Only the first
    should reach the email path."""
    cfg = _cfg(tmp_path)
    body = (
        "EOD Report 2026-05-13\nDay PnL: Rs -146.87\nTrades: 4\n"
        "Win Rate: 50%\nCash: Rs 99,853.13\n"
    )
    send_count = 0
    for restart in range(11):
        am = AlertManager(cfg)
        with patch.object(am, "_send_email") as mock_send:
            am.send_alert("EOD Summary", body)
            if mock_send.called:
                send_count += 1
    assert send_count == 1


def test_corrupt_state_file_does_not_block_alerts(tmp_path):
    """Defensive: a truncated/garbled state file (filesystem flake,
    container kill mid-write) must not silently swallow all future
    alerts. Treat it as empty state and send anyway."""
    cfg = _cfg(tmp_path)
    state_path = Path(cfg["monitoring"]["alerts"]["dedup"]["state_path"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not_valid_json:")
    am = AlertManager(cfg)
    with patch.object(am, "_send_email") as mock_send:
        am.send_alert("Risk Alert", "Stop loss breach on RELIANCE")
    mock_send.assert_called_once()


def test_disabled_alert_manager_skips_dedup_state(tmp_path):
    """If alerts.enabled is False, no state should be written -- we
    should noop early and not pollute the dedup file."""
    cfg = _cfg(tmp_path)
    cfg["monitoring"]["alerts"]["enabled"] = False
    am = AlertManager(cfg)
    am.send_alert("X", "Y")
    state_path = Path(cfg["monitoring"]["alerts"]["dedup"]["state_path"])
    assert not state_path.exists()
