"""tools/send_heartbeat.py -- the daily heartbeat email pulse.

Why this test exists
--------------------
The heartbeat email is the operator's silent-failure detector
(`docs/freeze_contingencies.md` §C1.b). If it stops working without
the operator noticing, the freeze loses its main "is the daemon alive?"
signal -- which is precisely the failure mode the verdict flagged as
HIGH-probability.

Two contracts pinned here:

1. The body composer is **resilient to missing inputs.** Daemon down,
   no EOD yet, no audit checkpoints, no health file -- the script
   produces a usable body, not a crash.

2. The script's exit code is **predictable** so cron / systemd can
   pipe to a "didn't send heartbeat" alarm:
     0  -- sent (or dry-run produced output)
     1  -- alerter not configured
     2  -- send failed at the transport layer
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tools.send_heartbeat as hb  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


# ────────────────────────────────────────────────────────────────────
# Body composer -- resilience to missing inputs
# ────────────────────────────────────────────────────────────────────


def test_compose_body_handles_fully_unavailable_inputs():
    """All collectors return 'not ok'. The body must still be a coherent
    markdown string with the headline daemon status flagged UNREACHABLE.
    """
    now = datetime(2026, 5, 19, 9, 10, tzinfo=IST)
    body = hb.compose_body(
        now_ist=now,
        health={"ok": False, "reason": "logs/health.json missing"},
        last_eod={"ok": False, "reason": "no eod_*.md files yet"},
        audit={"ok": False, "reason": "no audit_checkpoint_*.md files yet"},
        spool={"ok": True, "depth": 0},
        disk={"ok": False, "reason": "PermissionError"},
    )
    assert "Trading Agent Heartbeat" in body
    assert "UNREACHABLE" in body
    assert "§C1.a" in body  # operator action callout
    assert "unavailable" in body


def test_compose_body_flags_stale_health_file():
    """A health.json older than 5 minutes during market hours is itself
    a failure signal -- the body must say STALE, not UP."""
    now = datetime(2026, 5, 19, 9, 10, tzinfo=IST)
    body = hb.compose_body(
        now_ist=now,
        health={"ok": True, "_file_age_seconds": 600, "uptime_seconds": 3600, "open_positions": 0},
        last_eod={"ok": False, "reason": "n/a"},
        audit={"ok": False, "reason": "n/a"},
        spool={"ok": True, "depth": 0},
        disk={"ok": True, "pct": 30.0, "free_gb": 10.0, "total_gb": 50.0},
    )
    assert "STALE" in body


def test_compose_body_marks_fresh_daemon_as_up():
    now = datetime(2026, 5, 19, 9, 10, tzinfo=IST)
    body = hb.compose_body(
        now_ist=now,
        health={"ok": True, "_file_age_seconds": 30, "uptime_seconds": 7200, "open_positions": 2},
        last_eod={"ok": False, "reason": "n/a"},
        audit={"ok": False, "reason": "n/a"},
        spool={"ok": True, "depth": 0},
        disk={"ok": True, "pct": 30.0, "free_gb": 10.0, "total_gb": 50.0},
    )
    assert "Daemon:** UP" in body
    assert "Open positions:** 2" in body


def test_compose_body_flags_non_zero_spool_depth():
    """A non-zero failed-alert spool depth is the canonical 'alert
    pipeline broken silently' indicator -- it must produce a WARN
    string in the body, not be silently rendered as 'depth: 3'."""
    now = datetime(2026, 5, 19, 9, 10, tzinfo=IST)
    body = hb.compose_body(
        now_ist=now,
        health={"ok": True, "_file_age_seconds": 30, "uptime_seconds": 60, "open_positions": 0},
        last_eod={"ok": False, "reason": "n/a"},
        audit={"ok": False, "reason": "n/a"},
        spool={"ok": True, "depth": 3},
        disk={"ok": True, "pct": 30.0, "free_gb": 10.0, "total_gb": 50.0},
    )
    assert "spool depth:** 3" in body
    assert "WARN" in body


def test_compose_body_flags_disk_above_75pct():
    """Disk > 75 % gets a WARN flag so the daily heartbeat surfaces it
    long before the disk actually fills (§C1.c)."""
    now = datetime(2026, 5, 19, 9, 10, tzinfo=IST)
    body = hb.compose_body(
        now_ist=now,
        health={"ok": True, "_file_age_seconds": 30, "uptime_seconds": 60, "open_positions": 0},
        last_eod={"ok": False, "reason": "n/a"},
        audit={"ok": False, "reason": "n/a"},
        spool={"ok": True, "depth": 0},
        disk={"ok": True, "pct": 82.5, "free_gb": 5.0, "total_gb": 50.0},
    )
    assert "82.5" in body
    assert "> 75" in body


def test_compose_body_includes_followup_callout():
    """The body must end with the §C1.b follow-up instruction so the
    operator knows what to do if the email DIDN'T arrive."""
    now = datetime(2026, 5, 19, 9, 10, tzinfo=IST)
    body = hb.compose_body(
        now_ist=now,
        health={"ok": True, "_file_age_seconds": 30, "uptime_seconds": 60, "open_positions": 0},
        last_eod={"ok": False, "reason": "n/a"},
        audit={"ok": False, "reason": "n/a"},
        spool={"ok": True, "depth": 0},
        disk={"ok": True, "pct": 30.0, "free_gb": 10.0, "total_gb": 50.0},
    )
    assert "§C1.b" in body


def test_format_uptime_handles_minutes_hours_days():
    """The displayed uptime must scale: minutes for new daemons, hours
    for mid-day, days for stable runs."""
    assert hb._format_uptime(0) == "unknown"
    assert hb._format_uptime(60) == "0h 1m"
    assert hb._format_uptime(7200) == "2h 0m"
    assert hb._format_uptime(90000) == "1d 1h 0m"
    assert hb._format_uptime(86400 * 3) == "3d 0h 0m"


# ────────────────────────────────────────────────────────────────────
# Collectors -- file-system contract
# ────────────────────────────────────────────────────────────────────


def test_collect_health_returns_not_ok_when_missing(tmp_path: Path):
    """If logs/health.json is missing, the collector returns {ok: False, reason}
    rather than crashing -- the heartbeat must still send."""
    with patch.object(hb, "HEALTH_FILE", tmp_path / "missing.json"):
        out = hb.collect_health()
    assert out["ok"] is False
    assert "missing" in out["reason"].lower() or "down" in out["reason"].lower()


def test_collect_health_reads_well_formed_file(tmp_path: Path):
    health = tmp_path / "health.json"
    health.write_text(json.dumps({
        "timestamp": "2026-05-19T09:00:00+05:30",
        "uptime_seconds": 1800,
        "open_positions": 3,
        "last_cycle": "2026-05-19T09:00:00",
    }))
    with patch.object(hb, "HEALTH_FILE", health):
        out = hb.collect_health()
    assert out["ok"] is True
    assert out["uptime_seconds"] == 1800
    assert out["open_positions"] == 3
    assert "_file_age_seconds" in out


def test_collect_last_eod_finds_most_recent(tmp_path: Path):
    diag = tmp_path / "diag"
    diag.mkdir()
    (diag / "eod_2026-05-12.md").write_text("# Profit Diagnostic\nTotal PnL: Rs +100  | Trades: 5\n")
    (diag / "eod_2026-05-18.md").write_text("# Profit Diagnostic\n- **Total PnL (net of charges):** Rs -1,132\n- **Trades:** 22\n")
    with patch.object(hb, "DIAG_DIR", diag):
        out = hb.collect_last_eod()
    assert out["ok"] is True
    assert out["file"] == "eod_2026-05-18.md"


def test_collect_last_eod_returns_not_ok_when_empty(tmp_path: Path):
    diag = tmp_path / "diag"
    diag.mkdir()
    with patch.object(hb, "DIAG_DIR", diag):
        out = hb.collect_last_eod()
    assert out["ok"] is False


def test_collect_last_audit_extracts_verdict(tmp_path: Path):
    diag = tmp_path / "diag"
    diag.mkdir()
    (diag / "audit_checkpoint_20260518_143000.md").write_text(
        "# Audit Checkpoint\n\n**Audit verdict: GREEN -- nothing blocking trade**\n"
    )
    with patch.object(hb, "DIAG_DIR", diag):
        out = hb.collect_last_audit()
    assert out["ok"] is True
    assert out["verdict"] == "GREEN"
    assert out["timestamp"] == "20260518_143000"


def test_collect_spool_depth_counts_json_files(tmp_path: Path):
    spool = tmp_path / "failed_alerts"
    spool.mkdir()
    for i in range(4):
        (spool / f"alert_{i}.json").write_text("{}")
    (spool / "not_a_payload.txt").write_text("ignore")
    with patch.object(hb, "FAILED_ALERTS_DIR", spool):
        out = hb.collect_spool_depth()
    assert out["ok"] is True
    assert out["depth"] == 4  # only .json counted


def test_collect_spool_depth_empty_when_dir_missing(tmp_path: Path):
    with patch.object(hb, "FAILED_ALERTS_DIR", tmp_path / "does_not_exist"):
        out = hb.collect_spool_depth()
    assert out == {"ok": True, "depth": 0}


# ────────────────────────────────────────────────────────────────────
# Exit codes -- cron/systemd contract
# ────────────────────────────────────────────────────────────────────


def test_send_heartbeat_dry_run_returns_zero(capsys):
    """--dry-run prints to stdout and returns 0 so cron treats it as success."""
    rc = hb.send_heartbeat("test body", dry_run=True)
    assert rc == 0
    captured = capsys.readouterr()
    assert "test body" in captured.out


def test_send_heartbeat_returns_one_when_alerts_disabled():
    """If email isn't configured, exit 1 so cron's mail wrapper captures
    the body and the operator sees something is misconfigured."""
    fake_mgr = MagicMock()
    fake_mgr._email_enabled = False

    with patch.object(hb, "_load_config", return_value={}), \
         patch("packages.monitoring.alerts.AlertManager", return_value=fake_mgr):
        rc = hb.send_heartbeat("test body", dry_run=False)
    assert rc == 1


def test_send_heartbeat_returns_two_when_send_raises():
    """Transport-layer failure must NOT be silent. Exit 2 = composed but
    failed to send. The body is also printed to stdout so cron mail
    captures it as a fallback."""
    fake_mgr = MagicMock()
    fake_mgr._email_enabled = True
    fake_mgr.send_alert.side_effect = RuntimeError("SMTP auth failed")

    with patch.object(hb, "_load_config", return_value={}), \
         patch("packages.monitoring.alerts.AlertManager", return_value=fake_mgr):
        rc = hb.send_heartbeat("test body", dry_run=False)
    assert rc == 2


def test_send_heartbeat_returns_zero_on_success():
    fake_mgr = MagicMock()
    fake_mgr._email_enabled = True
    fake_mgr.send_alert.return_value = None  # AlertManager doesn't return; absence of raise = success.

    with patch.object(hb, "_load_config", return_value={}), \
         patch("packages.monitoring.alerts.AlertManager", return_value=fake_mgr):
        rc = hb.send_heartbeat("test body", dry_run=False)
    assert rc == 0
    fake_mgr.send_alert.assert_called_once()
