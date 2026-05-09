"""Tests for the lightweight health.json + tools/health_check.py probe."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEALTH_CHECK = ROOT / "tools" / "health_check.py"


def _write_health(path: Path, *, age_seconds: int = 0, pnl: float = 0.0, **extra) -> None:
    payload = {
        "ts_unix": int(time.time()) - age_seconds,
        "ts": "2026-05-07T16:00:00",
        "pid": 12345,
        "mode": "paper",
        "cycle_count": 100,
        "running": True,
        "open_positions": [],
        "open_position_count": 0,
        "cash": 25000.0,
        "daily_pnl": pnl,
        "daily_trades": 0,
        "consecutive_losses": 0,
        "drawdown_pct": 0.0,
        "drawdown_tier": "NORMAL",
        "cooldowns": [],
        "blacklisted": [],
    }
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_check(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HEALTH_CHECK), "--path", str(path), *args],
        capture_output=True, text=True,
    )


def test_health_check_ok_on_fresh_heartbeat(tmp_path):
    p = tmp_path / "health.json"
    _write_health(p, age_seconds=10)
    r = _run_check(p)
    assert r.returncode == 0
    assert "[OK]" in r.stdout


def test_health_check_fail_when_missing(tmp_path):
    p = tmp_path / "health.json"  # never created
    r = _run_check(p)
    assert r.returncode == 1


def test_health_check_stale_when_old(tmp_path):
    p = tmp_path / "health.json"
    _write_health(p, age_seconds=900)  # 15 min old, default max is 600
    r = _run_check(p)
    assert r.returncode == 2
    assert "STALE" in r.stdout


def test_health_check_pnl_floor_breach(tmp_path):
    p = tmp_path / "health.json"
    _write_health(p, age_seconds=10, pnl=-3000.0)
    r = _run_check(p, "--pnl-floor", "-2000")
    assert r.returncode == 3
    assert "PNL-FLOOR" in r.stdout


def test_health_check_pnl_floor_not_breached(tmp_path):
    p = tmp_path / "health.json"
    _write_health(p, age_seconds=10, pnl=-1500.0)
    r = _run_check(p, "--pnl-floor", "-2000")
    assert r.returncode == 0


def test_health_check_quiet_mode(tmp_path):
    p = tmp_path / "health.json"
    _write_health(p, age_seconds=900)
    r = _run_check(p, "--quiet")
    assert r.returncode == 2
    assert r.stdout.strip() == ""


def test_health_check_corrupt_file(tmp_path):
    p = tmp_path / "health.json"
    p.write_text("not valid json {{{", encoding="utf-8")
    r = _run_check(p)
    assert r.returncode == 1
    assert "corrupt" in r.stdout.lower()


def test_health_json_atomic_write(tmp_path, monkeypatch):
    """Verify TradingAgent._write_health_json writes atomically (no half files)."""
    # Build a minimal mock agent — we only need .config, .portfolio, .cash,
    # .positions, ._cycle_count, ._running, ._cooldown_map, ._stock_loss_today,
    # ._max_losses_per_stock.
    from datetime import datetime
    import pytz
    from trading_agent import TradingAgent

    IST = pytz.timezone("Asia/Kolkata")
    agent = TradingAgent.__new__(TradingAgent)
    agent.config = {"logging": {"log_dir": str(tmp_path)}, "broker": {"mode": "paper"}}

    class _P:
        cash = 12345.67
        positions = {}
    agent.portfolio = _P()
    agent._cycle_count = 7
    agent._running = True
    agent._cooldown_map = {}
    agent._stock_loss_today = {}
    agent._max_losses_per_stock = 2

    risk = {"daily_pnl": 100.0, "daily_trades": 3, "consecutive_losses": 0, "drawdown_pct": 0.5}
    agent._write_health_json(datetime.now(IST), risk, ["FOO", "BAR"])

    health = tmp_path / "health.json"
    assert health.exists()
    assert not (tmp_path / "health.json.tmp").exists(), "tmp file should be cleaned up"
    data = json.loads(health.read_text(encoding="utf-8"))
    assert data["pid"] == int(data["pid"])  # int round-trip
    assert data["open_position_count"] == 2
    assert data["open_positions"] == ["FOO", "BAR"]
    assert data["cash"] == 12345.67
    assert data["daily_pnl"] == 100.0
    assert data["mode"] == "paper"
