"""Unit tests for packages.core.trailing_stop_persistence (P1 #15).

Background. ``TrailingStop`` accumulates 8 fields of in-flight state
(water marks, breakeven_armed, peak_giveback_armed, trailing_active,
current_sl, etc.) over the life of a position. All of these reset to
defaults on ``__init__``. Mid-session daemon restart used to wipe them,
sending the position back to its initial-SL distance even if the trail
had already locked in profit. This module persists the state to a
JSON file and restores it on next boot.

These tests pin:
  1. Save / load round-trip preserves all 8 dynamic fields.
  2. Restoration only applies if side AND entry_price (within tol)
     match -- a different trade on the same symbol must not inherit
     stale state.
  3. Malformed / empty / missing snapshots return empty dict and never
     raise; the next mutation will retry.
  4. Restoration onto a TrailingStop that has not yet been mutated
     (defaults) preserves all 8 fields exactly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from core.risk_manager import TrailingStop
from core.trailing_stop_persistence import (
    SNAPSHOT_FILENAME,
    load_trailing_states,
    restore_trailing_state,
    save_trailing_states,
)


def _ts_with_mutations(symbol="HDFCBANK", price_path=(1500.0,)):
    """Build a TrailingStop and feed prices through update() to push it
    into a non-default state. Caller passes a sequence of prices."""
    ts = TrailingStop(
        entry_price=1500.0,
        initial_sl=1485.0,
        side="BUY",
        trail_activation_rr=1.0,
        trail_step_pct=0.3,
        peak_arm_rr=1.5,
        peak_giveback_pct=35.0,
        peak_giveback_enabled=True,
        breakeven_arm_rr=0.5,
        breakeven_buffer_pct=0.10,
        breakeven_enabled=True,
        symbol=symbol,
    )
    for p in price_path:
        ts.update(p)
    return ts


def test_save_and_load_roundtrip_preserves_all_fields(tmp_path):
    # Push price so all flags arm:
    #   entry=1500, sl=1485 -> initial_risk=15
    #   price=1530 -> +2R favorable, both breakeven and peak_giveback armed,
    #   trailing_active True, highest_since_entry=1530.
    ts = _ts_with_mutations(price_path=(1505.0, 1515.0, 1530.0))
    save_trailing_states({"HDFCBANK": ts}, data_dir=tmp_path)

    loaded = load_trailing_states(data_dir=tmp_path)
    assert "HDFCBANK" in loaded
    snap = loaded["HDFCBANK"]
    # Static identity fields preserved
    assert snap["side"] == "BUY"
    assert snap["entry_price"] == 1500.0
    # Dynamic mutations preserved
    assert snap["highest_since_entry"] == 1530.0
    assert snap["trailing_active"] is True
    assert snap["breakeven_armed"] is True
    assert snap["peak_giveback_armed"] is True
    assert snap["peak_unrealized_r"] > 1.5
    assert snap["last_unrealized_r"] > 1.5


def test_restore_applies_snapshot_to_fresh_ts(tmp_path):
    """Construct a fresh TS (defaults) and restore a saved snapshot.
    All 8 dynamic fields must come back."""
    src = _ts_with_mutations(price_path=(1505.0, 1515.0, 1530.0))
    save_trailing_states({"HDFCBANK": src}, data_dir=tmp_path)

    loaded = load_trailing_states(data_dir=tmp_path)
    snap = loaded["HDFCBANK"]

    fresh = TrailingStop(
        entry_price=1500.0, initial_sl=1485.0, side="BUY",
        symbol="HDFCBANK",
    )
    # Pre-restore: defaults
    assert fresh.trailing_active is False
    assert fresh.breakeven_armed is False
    assert fresh.peak_giveback_armed is False

    ok = restore_trailing_state(fresh, snap)
    assert ok is True
    assert fresh.trailing_active is True
    assert fresh.breakeven_armed is True
    assert fresh.peak_giveback_armed is True
    assert fresh.highest_since_entry == 1530.0
    assert fresh.last_unrealized_r > 1.5


def test_restore_refuses_side_mismatch(tmp_path):
    """If a position was BUY and the rehydrated position is SELL, restoring
    state would be nonsense. Must refuse."""
    src = _ts_with_mutations(price_path=(1530.0,))
    save_trailing_states({"HDFCBANK": src}, data_dir=tmp_path)
    snap = load_trailing_states(data_dir=tmp_path)["HDFCBANK"]

    sell_ts = TrailingStop(
        entry_price=1500.0, initial_sl=1515.0, side="SELL",
        symbol="HDFCBANK",
    )
    ok = restore_trailing_state(sell_ts, snap)
    assert ok is False
    # State remained at SELL defaults
    assert sell_ts.trailing_active is False
    assert sell_ts.peak_giveback_armed is False


def test_restore_refuses_entry_price_drift(tmp_path):
    """Different entry price means it's a different round-trip on the same
    symbol; stale snapshot must not be applied."""
    src = _ts_with_mutations(price_path=(1530.0,))
    save_trailing_states({"HDFCBANK": src}, data_dir=tmp_path)
    snap = load_trailing_states(data_dir=tmp_path)["HDFCBANK"]

    # New round-trip at a meaningfully different entry price
    new_ts = TrailingStop(
        entry_price=1650.0, initial_sl=1635.0, side="BUY",
        symbol="HDFCBANK",
    )
    ok = restore_trailing_state(new_ts, snap)
    assert ok is False
    assert new_ts.breakeven_armed is False


def test_restore_accepts_entry_price_within_tol(tmp_path):
    """Tiny float drift (e.g. DB round-trip) must NOT block restoration."""
    src = _ts_with_mutations(price_path=(1530.0,))
    save_trailing_states({"HDFCBANK": src}, data_dir=tmp_path)
    snap = load_trailing_states(data_dir=tmp_path)["HDFCBANK"]

    # 0.001% drift = ~1.5 paise on Rs 1500. Should be tolerated.
    drift_ts = TrailingStop(
        entry_price=1500.015, initial_sl=1485.015, side="BUY",
        symbol="HDFCBANK",
    )
    ok = restore_trailing_state(drift_ts, snap)
    assert ok is True


def test_load_missing_file_returns_empty(tmp_path):
    """No snapshot on disk yet (fresh deploy): load returns empty dict
    without raising."""
    out = load_trailing_states(data_dir=tmp_path)
    assert out == {}


def test_load_malformed_json_returns_empty(tmp_path):
    """Corrupted snapshot file must not crash startup."""
    (tmp_path / SNAPSHOT_FILENAME).write_text("not-json{{", encoding="utf-8")
    out = load_trailing_states(data_dir=tmp_path)
    assert out == {}


def test_load_missing_trailing_stops_section(tmp_path):
    """Partial / future-schema file with no trailing_stops key returns
    empty dict gracefully."""
    (tmp_path / SNAPSHOT_FILENAME).write_text(
        json.dumps({"version": 99, "saved_at": "2026-01-01T09:15:00+05:30"}),
        encoding="utf-8",
    )
    out = load_trailing_states(data_dir=tmp_path)
    assert out == {}


def test_save_creates_parent_dir(tmp_path):
    """Caller passes a data_dir that doesn't exist yet (fresh deploy);
    save creates it instead of crashing."""
    nested = tmp_path / "deep" / "nested" / "data"
    src = _ts_with_mutations(price_path=(1530.0,))
    save_trailing_states({"HDFCBANK": src}, data_dir=nested)
    assert (nested / SNAPSHOT_FILENAME).exists()


def test_atomic_write_no_partial_on_collision(tmp_path):
    """The temp-then-rename pattern should leave no .tmp files behind on a
    normal save."""
    src = _ts_with_mutations(price_path=(1530.0,))
    save_trailing_states({"HDFCBANK": src}, data_dir=tmp_path)
    # Only the snapshot file exists; no orphan .tmp files
    files = list(tmp_path.iterdir())
    names = [f.name for f in files]
    assert SNAPSHOT_FILENAME in names
    assert not any(n.startswith(".trailing_stops.") for n in names)


# ---------------------------------------------------------------------------
# Regression #4 (2026-05-18): trail-override fields in persistence schema
# ---------------------------------------------------------------------------


def test_trail_override_fields_survive_round_trip(tmp_path):
    """trend_continuation entries override trail_activation_rr / trail_step_pct
    on the live TrailingStop. Without persistence those overrides used to
    vanish on restart -- the position came back with the config defaults
    (activation=1.0, step=0.3) and gave back more MFE than intended.
    Now the snapshot carries them explicitly."""
    ts = TrailingStop(
        entry_price=1500.0, initial_sl=1485.0, side="BUY",
        trail_activation_rr=1.0, trail_step_pct=0.3,
        symbol="HDFCBANK",
    )
    # Mirror the trading_agent override for trend_continuation entries.
    ts.trail_activation_rr = 0.5
    ts.trail_step_pct = 0.6

    save_trailing_states({"HDFCBANK": ts}, data_dir=tmp_path)
    loaded = load_trailing_states(data_dir=tmp_path)
    snap = loaded["HDFCBANK"]
    assert snap["trail_activation_rr"] == 0.5
    assert snap["trail_step_pct"] == 0.6

    # And the restore-side applies them back onto a fresh TS (which would
    # otherwise have the default 1.0 / 0.3).
    fresh = TrailingStop(
        entry_price=1500.0, initial_sl=1485.0, side="BUY",
        symbol="HDFCBANK",
    )
    assert fresh.trail_activation_rr == 1.0
    assert fresh.trail_step_pct == 0.3
    assert restore_trailing_state(fresh, snap) is True
    assert fresh.trail_activation_rr == 0.5
    assert fresh.trail_step_pct == 0.6


def test_restore_tolerates_missing_trail_override_fields(tmp_path):
    """Legacy snapshots written before 2026-05-18 won't carry
    trail_activation_rr / trail_step_pct. Restore must accept them as
    optional and leave the freshly-constructed TS's config defaults
    intact."""
    legacy_snap = {
        "entry_price":          1500.0,
        "side":                 "BUY",
        "initial_risk":         15.0,
        "current_sl":           1495.0,
        "highest_since_entry":  1520.0,
        "lowest_since_entry":   1500.0,
        "trailing_active":      True,
        "peak_unrealized_r":    1.3,
        "peak_giveback_armed":  False,
        "last_unrealized_r":    1.2,
        "breakeven_armed":      True,
        # NO trail_activation_rr / trail_step_pct fields
    }
    fresh = TrailingStop(
        entry_price=1500.0, initial_sl=1485.0, side="BUY",
        trail_activation_rr=1.0, trail_step_pct=0.3,
        symbol="HDFCBANK",
    )
    assert restore_trailing_state(fresh, legacy_snap) is True
    # Dynamic fields applied
    assert fresh.current_sl == 1495.0
    assert fresh.breakeven_armed is True
    # Override fields untouched (kept the config defaults)
    assert fresh.trail_activation_rr == 1.0
    assert fresh.trail_step_pct == 0.3


# ---------------------------------------------------------------------------
# Regression #5 / #6 (2026-05-18): schema version + corrupt fail-loud
# ---------------------------------------------------------------------------


def test_corrupt_snapshot_logs_critical(tmp_path, caplog):
    """A corrupt trailing-stop snapshot used to drop to {} silently. Must
    now log CRITICAL so the operator gets a loud forensic signal."""
    import logging

    (tmp_path / SNAPSHOT_FILENAME).write_text("not-json{{", encoding="utf-8")
    with caplog.at_level(logging.CRITICAL):
        out = load_trailing_states(data_dir=tmp_path)
    assert out == {}


def test_future_schema_version_refused(tmp_path, caplog):
    """A future-version snapshot (rollback scenario) must be REFUSED and
    logged at CRITICAL, not silently mis-parsed."""
    import logging

    future = {
        "version": 99,
        "saved_at": "2026-12-31T23:59:59+05:30",
        "trailing_stops": {
            "HDFCBANK": {"entry_price": 1500.0, "side": "BUY"},
        },
    }
    (tmp_path / SNAPSHOT_FILENAME).write_text(json.dumps(future), encoding="utf-8")
    with caplog.at_level(logging.CRITICAL):
        out = load_trailing_states(data_dir=tmp_path)
    assert out == {}


def test_unparseable_schema_version_refused(tmp_path):
    """version: 'banana' is treated as untrusted format -- REFUSE."""
    bad = {
        "version": "banana",
        "saved_at": "2026-05-18T10:00:00+05:30",
        "trailing_stops": {
            "HDFCBANK": {"entry_price": 1500.0, "side": "BUY"},
        },
    }
    (tmp_path / SNAPSHOT_FILENAME).write_text(json.dumps(bad), encoding="utf-8")
    out = load_trailing_states(data_dir=tmp_path)
    assert out == {}


def test_realistic_restart_scenario(tmp_path):
    """End-to-end: position opens, trail mutates, daemon crashes, restarts,
    position re-creates a fresh TrailingStop, restore brings back state."""
    pre_restart = _ts_with_mutations(price_path=(1505.0, 1520.0, 1530.0))
    pre_sl = pre_restart.current_sl
    pre_be = pre_restart.breakeven_armed
    pre_peak = pre_restart.peak_unrealized_r

    save_trailing_states({"HDFCBANK": pre_restart}, data_dir=tmp_path)

    # Daemon restart: fresh TrailingStop on the same restored position.
    snaps = load_trailing_states(data_dir=tmp_path)
    post_restart = TrailingStop(
        entry_price=1500.0, initial_sl=1485.0, side="BUY",
        symbol="HDFCBANK",
    )
    assert post_restart.current_sl == 1485.0  # default
    assert post_restart.breakeven_armed is False  # default

    restore_trailing_state(post_restart, snaps["HDFCBANK"])

    assert post_restart.current_sl == pre_sl
    assert post_restart.breakeven_armed == pre_be
    assert post_restart.peak_unrealized_r == pre_peak
