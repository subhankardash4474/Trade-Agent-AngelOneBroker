"""Unit tests for packages.core.runtime_state_persistence (P2 restart-cluster).

Three intraday state buckets used to reset on restart:
  * _strategy_state    per-strategy circuit breaker (the worst miss --
                       a suspended strategy un-suspended itself on restart)
  * _recent_opens      sliding window for global open-rate cap
  * _consec_tp_today   per-symbol TP streak for trend-continuation

These tests pin:
  1. Round-trip preserves all three.
  2. ``_strategy_state`` and ``_consec_tp_today`` are date-scoped (reset
     on a day boundary), so a yesterday snapshot must be discarded.
  3. ``_recent_opens`` entries older than the open-rate window are
     discarded (same TTL the runtime enforces).
  4. Missing / malformed snapshots return empty containers and never raise.
"""
from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from core.runtime_state_persistence import (
    SNAPSHOT_FILENAME,
    load_runtime_state,
    save_runtime_state,
)

IST = pytz.timezone("Asia/Kolkata")


def test_roundtrip_preserves_all_three(tmp_path):
    now = datetime.now(IST)
    strategy_state = {
        "mean_reversion": {
            "consec_losses": 3, "daily_pnl": -120.5,
            "suspended": True, "suspended_reason": "consec_losses=3", "trades": 4,
        },
        "supertrend_follow": {
            "consec_losses": 0, "daily_pnl": 75.0,
            "suspended": False, "suspended_reason": "", "trades": 2,
        },
    }
    recent_opens = deque([
        (now - timedelta(minutes=1), "HDFCBANK"),
        (now - timedelta(minutes=2), "INFY"),
    ])
    consec_tp_today = {"TCS": 2, "HDFCBANK": 1}

    save_runtime_state(strategy_state, recent_opens, consec_tp_today, data_dir=tmp_path)
    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == strategy_state
    assert len(o) == 2
    assert t == consec_tp_today


def test_strategy_state_dropped_on_day_boundary(tmp_path):
    """Snapshot from yesterday must NOT re-suspend a strategy today."""
    yesterday = datetime.now(IST) - timedelta(days=1)
    strategy_state = {
        "supertrend_follow": {
            "consec_losses": 3, "daily_pnl": -200.0,
            "suspended": True, "suspended_reason": "consec_losses=3", "trades": 4,
        },
    }
    save_runtime_state(
        strategy_state, deque(), {}, data_dir=tmp_path,
    )
    # Hand-edit the saved_at to yesterday
    path = tmp_path / SNAPSHOT_FILENAME
    payload = json.loads(path.read_text())
    payload["saved_at"] = yesterday.isoformat()
    path.write_text(json.dumps(payload))

    s, _, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {}, "stale strategy_state must be dropped on day boundary"


def test_consec_tp_today_dropped_on_day_boundary(tmp_path):
    """Same rule for the per-symbol TP-streak map."""
    yesterday = datetime.now(IST) - timedelta(days=1)
    save_runtime_state({}, deque(), {"TCS": 3}, data_dir=tmp_path)
    path = tmp_path / SNAPSHOT_FILENAME
    payload = json.loads(path.read_text())
    payload["saved_at"] = yesterday.isoformat()
    path.write_text(json.dumps(payload))

    _, _, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert t == {}


def test_recent_opens_outside_window_dropped(tmp_path):
    """Entries older than the open-rate window TTL must be discarded."""
    now = datetime.now(IST)
    fresh = now - timedelta(minutes=2)
    stale = now - timedelta(minutes=30)
    opens = deque([(stale, "OLD_SYM"), (fresh, "FRESH_SYM")])
    save_runtime_state({}, opens, {}, data_dir=tmp_path)

    _, o, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert len(o) == 1
    assert o[0][1] == "FRESH_SYM"


def test_load_missing_file_returns_empty(tmp_path):
    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {} and o == [] and t == {}


def test_load_malformed_json_returns_empty(tmp_path):
    (tmp_path / SNAPSHOT_FILENAME).write_text("{{not-json", encoding="utf-8")
    s, o, t = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s == {} and o == [] and t == {}


def test_suspended_strategy_survives_same_day_restart(tmp_path):
    """End-to-end: a strategy hits its 3-loss breaker, daemon restarts
    within the same trading day, breaker state survives."""
    now = datetime.now(IST)
    strategy_state = {
        "supertrend_follow": {
            "consec_losses": 3, "daily_pnl": -150.0,
            "suspended": True, "suspended_reason": "consec_losses=3", "trades": 3,
        },
    }
    save_runtime_state(strategy_state, deque(), {}, data_dir=tmp_path)

    # Restart: load
    s, _, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert s["supertrend_follow"]["suspended"] is True
    assert s["supertrend_follow"]["consec_losses"] == 3


def test_malformed_strategy_entries_skipped(tmp_path):
    """A row that isn't a dict is silently dropped instead of crashing
    the rest of the load."""
    save_runtime_state({}, deque(), {}, data_dir=tmp_path)
    path = tmp_path / SNAPSHOT_FILENAME
    payload = json.loads(path.read_text())
    payload["strategy_state"] = {
        "good": {"consec_losses": 1, "daily_pnl": -50.0, "suspended": False,
                 "suspended_reason": "", "trades": 1},
        "bad_not_dict": "this should be silently skipped",
    }
    path.write_text(json.dumps(payload))
    s, _, _ = load_runtime_state(
        open_rate_window=timedelta(minutes=5), data_dir=tmp_path,
    )
    assert "good" in s
    assert "bad_not_dict" not in s
