"""Tests for the alert retry + disk-spool behaviour added after the
2026-05-07 VPN-induced email outage. Validates:

  1. A transient network error retries up to N times then succeeds.
  2. A terminal failure (auth) does NOT retry and DOES spool to disk.
  3. Repeated network failures spool exactly once (no double-spool).
  4. drain_failed_alerts replays spool files and removes them on success.
  5. drain_failed_alerts keeps spool files on the disk when send still fails.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
import requests

import monitoring.alerts as alerts_mod
from monitoring.alerts import AlertManager


@pytest.fixture
def cfg():
    return {
        "monitoring": {
            "alerts": {
                "enabled": True,
                "email": {
                    "enabled": True,
                    "provider": "resend",
                    "resend_api_key": "re_test_key",
                    "sender": "test@example.com",
                    "recipient": "user@example.com",
                },
            }
        }
    }


@pytest.fixture
def isolated_spool(tmp_path, monkeypatch):
    """Redirect logs/failed_alerts to tmp dir and zero out backoff so tests
    are fast."""
    spool = tmp_path / "failed_alerts"
    monkeypatch.setattr(alerts_mod, "_FAILED_ALERTS_DIR", spool)
    # Zero out backoff delays so retry tests don't sleep
    monkeypatch.setattr(alerts_mod, "_BACKOFF_DELAYS", [0, 0, 0, 0])
    return spool


def _ok_response(status: int = 200):
    r = mock.Mock()
    r.status_code = status
    r.text = "ok"
    return r


def _err_response(status: int, text: str = "boom"):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    return r


def test_resend_succeeds_first_try(cfg, isolated_spool):
    am = AlertManager(cfg)
    with mock.patch("monitoring.alerts.requests.post", return_value=_ok_response(200)) as p:
        ok = am._send_email_resend("subj", "body")
    assert ok is True
    assert p.call_count == 1
    assert not list(isolated_spool.glob("*.json")), "should not spool on success"


def test_resend_retries_on_network_error_then_succeeds(cfg, isolated_spool):
    am = AlertManager(cfg)
    side_effects = [
        requests.exceptions.ConnectionError("dns fail"),
        requests.exceptions.ConnectionError("dns fail"),
        _ok_response(201),
    ]
    with mock.patch("monitoring.alerts.requests.post", side_effect=side_effects) as p:
        ok = am._send_email_resend("subj", "body")
    assert ok is True
    assert p.call_count == 3
    assert not list(isolated_spool.glob("*.json")), "no spool when retry eventually succeeds"


def test_resend_exhausts_retries_and_spools_once(cfg, isolated_spool):
    am = AlertManager(cfg)
    err = requests.exceptions.ConnectionError("dns dead")
    with mock.patch("monitoring.alerts.requests.post", side_effect=err) as p:
        ok = am._send_email_resend("Daily Report", "body")
    assert ok is False
    assert p.call_count == len(alerts_mod._BACKOFF_DELAYS)
    spool_files = list(isolated_spool.glob("*.json"))
    assert len(spool_files) == 1
    payload = json.loads(spool_files[0].read_text(encoding="utf-8"))
    assert payload["subject"] == "Daily Report"
    assert payload["body"] == "body"
    assert "network" in payload["reason"]


def test_resend_auth_error_does_not_retry_but_spools(cfg, isolated_spool):
    am = AlertManager(cfg)
    with mock.patch(
        "monitoring.alerts.requests.post",
        return_value=_err_response(401, '{"message":"invalid key"}'),
    ) as p:
        ok = am._send_email_resend("subj", "body")
    assert ok is False
    assert p.call_count == 1, "401 must not be retried"
    assert len(list(isolated_spool.glob("*.json"))) == 1


def test_resend_does_not_spool_when_spool_disabled(cfg, isolated_spool):
    """Drain path passes spool_on_fail=False so re-replays don't double-spool."""
    am = AlertManager(cfg)
    with mock.patch(
        "monitoring.alerts.requests.post",
        side_effect=requests.exceptions.ConnectionError("still down"),
    ):
        ok = am._send_email_resend("subj", "body", spool_on_fail=False)
    assert ok is False
    assert not list(isolated_spool.glob("*.json"))


def test_drain_replays_and_removes_spool_on_success(cfg, isolated_spool):
    am = AlertManager(cfg)
    # Pre-seed two spool files (simulate prior failed alerts).
    isolated_spool.mkdir(parents=True, exist_ok=True)
    for i, subj in enumerate(["EOD Summary", "Post-Mortem"]):
        (isolated_spool / f"2026-05-07T15{i:02d}_{subj.replace(' ', '_')}_abc.json").write_text(
            json.dumps({
                "provider": "resend",
                "subject": subj,
                "body": f"body {i}",
                "spooled_at": "2026-05-07T1500",
                "reason": "network: ConnectionError: dns",
            }),
            encoding="utf-8",
        )

    with mock.patch("monitoring.alerts.requests.post", return_value=_ok_response(200)):
        result = am.drain_failed_alerts()

    assert result == {"sent": 2, "failed": 0, "skipped": 0}
    assert not list(isolated_spool.glob("*.json")), "drained files should be removed"


def test_drain_keeps_files_when_replay_still_fails(cfg, isolated_spool):
    am = AlertManager(cfg)
    isolated_spool.mkdir(parents=True, exist_ok=True)
    p = isolated_spool / "2026-05-07T1520_EOD_Summary_abc.json"
    p.write_text(
        json.dumps({"provider": "resend", "subject": "EOD", "body": "b", "spooled_at": "x", "reason": "y"}),
        encoding="utf-8",
    )

    with mock.patch(
        "monitoring.alerts.requests.post",
        side_effect=requests.exceptions.ConnectionError("still no net"),
    ):
        result = am.drain_failed_alerts()

    assert result == {"sent": 0, "failed": 1, "skipped": 0}
    assert p.exists(), "failed replays must stay on disk for next attempt"
    # And critically — drain must not have CREATED a *second* spool entry.
    assert len(list(isolated_spool.glob("*.json"))) == 1


def test_drain_handles_missing_spool_dir_gracefully(cfg, isolated_spool):
    am = AlertManager(cfg)
    # spool dir doesn't exist yet
    assert not isolated_spool.exists()
    result = am.drain_failed_alerts()
    assert result == {"sent": 0, "failed": 0, "skipped": 0}
