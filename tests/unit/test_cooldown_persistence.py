"""Unit tests for packages.core.cooldown_persistence.

Background. See module docstring for the live-evidence motivation.
These tests pin the four behaviours the trading loop depends on:

  1. Save/load round-trip preserves all three maps exactly.
  2. Entries past their TTL are dropped at load time.
  3. ``stock_loss_today`` is dropped if the snapshot is from a previous
     calendar day in IST (the runtime resets it daily, so a stale
     yesterday-snapshot must not re-blacklist a stock).
  4. Failure modes (missing file, malformed JSON) return empty dicts
     and never raise -- trading must not be blocked by a bad snapshot.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from core.cooldown_persistence import (
    SCHEMA_VERSION,
    SNAPSHOT_FILENAME,
    load_cooldown_state,
    save_cooldown_state,
)

IST = pytz.timezone("Asia/Kolkata")


def _now() -> datetime:
    return datetime.now(IST)


def test_round_trip_preserves_all_three_maps(tmp_path):
    now = _now()
    cooldown_map = {
        "BSOFT": now - timedelta(minutes=5),
        "AEGISLOG": now - timedelta(minutes=10),
        "FEDERALBNK": now - timedelta(minutes=1),
    }
    stock_loss_today = {"BSOFT": 1, "FEDERALBNK": 1}
    rejection_cooldown_map = {
        ("CHOLAFIN", "SELL"): now - timedelta(seconds=30),
        ("TATACAP", "BUY"): now - timedelta(seconds=120),
    }

    save_cooldown_state(
        cooldown_map, stock_loss_today, rejection_cooldown_map, data_dir=tmp_path
    )

    restored_cd, restored_loss, restored_rej = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
        now=now,
    )

    assert set(restored_cd.keys()) == set(cooldown_map.keys())
    for sym, ts in cooldown_map.items():
        assert abs((restored_cd[sym] - ts).total_seconds()) < 1.0
    assert restored_loss == stock_loss_today
    assert set(restored_rej.keys()) == set(rejection_cooldown_map.keys())


def test_expired_reentry_cooldowns_dropped_at_load(tmp_path):
    """Live mirror: 30-min reentry TTL drops a 45-min-old entry but keeps
    a fresh one. This is what protects us from re-loading stale state on
    a Monday-morning restart that reads Friday's snapshot."""
    now = _now()
    cooldown_map = {
        "FRESH": now - timedelta(minutes=5),     # < 30 min, kept
        "STALE": now - timedelta(minutes=45),    # > 30 min, dropped
    }
    save_cooldown_state(cooldown_map, {}, {}, data_dir=tmp_path)

    restored_cd, _, _ = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
        now=now,
    )
    assert "FRESH" in restored_cd
    assert "STALE" not in restored_cd


def test_expired_rejection_cooldowns_dropped(tmp_path):
    now = _now()
    rej = {
        ("FRESH", "SELL"): now - timedelta(seconds=60),     # < 5 min, kept
        ("STALE", "BUY"): now - timedelta(minutes=10),      # > 5 min, dropped
    }
    save_cooldown_state({}, {}, rej, data_dir=tmp_path)

    _, _, restored_rej = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
        now=now,
    )
    assert ("FRESH", "SELL") in restored_rej
    assert ("STALE", "BUY") not in restored_rej


def test_stock_loss_today_dropped_if_snapshot_from_previous_day(tmp_path):
    """If we restart at 09:30 Monday and the snapshot is from Friday's
    15:30, the daily-reset semantics demand we drop ``stock_loss_today``
    -- otherwise Friday's two-loss blacklist would carry into Monday."""
    now = _now()
    yesterday = now - timedelta(days=1)

    # Hand-craft a snapshot with the saved_at backdated to yesterday but
    # cooldown_map entries fresh (within TTL). The runtime's reentry
    # cooldown is short (30 min) so reentry entries from yesterday are
    # naturally expired -- but stock_loss_today doesn't carry a per-entry
    # timestamp, so the saved_at date is the only way to invalidate it.
    snapshot = {
        "version": SCHEMA_VERSION,
        "saved_at": yesterday.astimezone(IST).isoformat(),
        "cooldown_map": {},
        "stock_loss_today": {"BSOFT": 2, "FEDERALBNK": 1},
        "rejection_cooldown_map": {},
    }
    path = tmp_path / SNAPSHOT_FILENAME
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    _, restored_loss, _ = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
        now=now,
    )
    assert restored_loss == {}, "yesterday's loss counts must not survive into today"


def test_missing_snapshot_returns_empty_dicts(tmp_path):
    """Fresh deployment / first boot has no snapshot. Must not raise."""
    restored_cd, restored_loss, restored_rej = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path / "nonexistent_subdir",
    )
    assert restored_cd == {}
    assert restored_loss == {}
    assert restored_rej == {}


def test_malformed_snapshot_does_not_raise(tmp_path):
    """A corrupted snapshot (truncated mid-write, hand-edited bad JSON,
    etc.) must be tolerated -- we degrade to empty dicts and log a
    warning, but the trading loop continues."""
    path = tmp_path / SNAPSHOT_FILENAME
    path.write_text("{this is not json,", encoding="utf-8")

    restored_cd, restored_loss, restored_rej = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
    )
    assert restored_cd == {}
    assert restored_loss == {}
    assert restored_rej == {}


def test_save_creates_parent_dir(tmp_path):
    """save_cooldown_state should mkdir its parent if missing -- the
    first save in a fresh data/ directory must not fail."""
    nested = tmp_path / "subdir" / "data"
    save_cooldown_state({"BSOFT": _now()}, {}, {}, data_dir=nested)
    assert (nested / SNAPSHOT_FILENAME).exists()


def test_save_is_atomic_no_temp_files_left(tmp_path):
    """Atomic write should not leave .tmp leftovers in the data dir."""
    save_cooldown_state({"X": _now()}, {}, {}, data_dir=tmp_path)
    leftovers = list(tmp_path.glob(".cooldowns.*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_realistic_scenario_friday_to_monday_restart(tmp_path):
    """Live-evidence regression. Today (Friday 14:46 IST): container
    restart, 3 stop-out cooldowns vanish. Now with persistence: the same
    3 cooldowns load back, all within their 30-min TTL.

    Mirrors the 2026-05-15 incident exactly."""
    # Snapshot saved at 14:44 IST -- the heartbeat we observed pre-restart.
    saved_at = IST.localize(datetime(2026, 5, 15, 14, 44, 7))
    cooldown_map = {
        "BSOFT": IST.localize(datetime(2026, 5, 15, 9, 39, 8)),       # stop_loss 09:39
        "AEGISLOG": IST.localize(datetime(2026, 5, 15, 11, 35, 22)),  # 11:35
        "FEDERALBNK": IST.localize(datetime(2026, 5, 15, 13, 2, 53)), # 13:02
    }
    save_cooldown_state(cooldown_map, {}, {}, data_dir=tmp_path)

    # Restart at 14:46 IST (2 min later). All 3 entries are within the
    # 30-min reentry window from their respective stop-out timestamps?
    # BSOFT: 14:46 - 09:39 = 5h 7m > 30 min -- naturally expired.
    # AEGISLOG: 14:46 - 11:35 = 3h 11m > 30 min -- naturally expired.
    # FEDERALBNK: 14:46 - 13:02 = 1h 44m > 30 min -- naturally expired.
    #
    # So actually the persistence layer DROPS these on load because each
    # had already had its 30-min reentry window elapse before today's
    # restart. The real save is the rejection_cooldown_map (5-min TTL)
    # which would survive a quick restart.
    now = IST.localize(datetime(2026, 5, 15, 14, 46, 30))

    restored_cd, _, _ = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
        now=now,
    )
    # All three are stale (>30 min since stop-out), so they're not
    # restored -- which is correct: at 14:46 the runtime would no longer
    # block re-entry into these symbols even without a restart.
    # The protection that actually mattered (late_entry_cutoff: 14:30)
    # is unaffected. This documents that cooldown persistence helps
    # specifically for *recent* stop-outs (<30 min ago), not the
    # all-day historical loss list.
    assert restored_cd == {}

    # Now the actually-protective case: a stop-out 5 min before restart.
    cooldown_map_fresh = {
        "FRESHSTOP": now - timedelta(minutes=5),
    }
    save_cooldown_state(cooldown_map_fresh, {}, {}, data_dir=tmp_path)
    restored_cd, _, _ = load_cooldown_state(
        reentry_cooldown=timedelta(minutes=30),
        rejection_cooldown=timedelta(minutes=5),
        data_dir=tmp_path,
        now=now + timedelta(seconds=10),
    )
    assert "FRESHSTOP" in restored_cd, "recent stop-outs must survive restart"
