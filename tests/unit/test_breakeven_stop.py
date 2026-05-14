"""Tests for the breakeven-stop guard added 2026-05-14.

Background: classic trailing-stop only arms at trail_activation_rr (1.0 R).
A position that ran +0.7 R and reversed all the way to the initial SL gave
back 1.7 R round-trip on what was briefly a winner. This test suite covers
the new breakeven feature on `TrailingStop`:

  * once peak_unrealized_r reaches `breakeven_arm_rr` (default 0.5 R), the
    SL is lifted to entry +/- `breakeven_buffer_pct` (covers charges).
  * the guard is monotonic: once armed, never disarms.
  * the breakeven SL never moves the SL backwards (must be max-of-old-and-new
    for longs, min-of-old-and-new for shorts).
  * symmetry: works for both BUY and SELL sides.
"""
from __future__ import annotations

import pytest

from core.risk_manager import RiskManager, TrailingStop


def _long_ts(**overrides) -> TrailingStop:
    defaults = dict(
        entry_price=100.0,
        initial_sl=98.0,                # 1R = Rs 2 per share
        side="BUY",
        breakeven_arm_rr=0.5,
        breakeven_buffer_pct=0.10,
        breakeven_enabled=True,
        peak_arm_rr=1.5,
        peak_giveback_pct=35.0,
        peak_giveback_enabled=True,
    )
    defaults.update(overrides)
    return TrailingStop(**defaults)


def _short_ts(**overrides) -> TrailingStop:
    defaults = dict(
        entry_price=100.0,
        initial_sl=102.0,               # 1R = Rs 2 per share
        side="SELL",
        breakeven_arm_rr=0.5,
        breakeven_buffer_pct=0.10,
        breakeven_enabled=True,
        peak_arm_rr=1.5,
        peak_giveback_pct=35.0,
        peak_giveback_enabled=True,
    )
    defaults.update(overrides)
    return TrailingStop(**defaults)


# ── Arm conditions ────────────────────────────────────────────────────────


def test_breakeven_does_not_arm_below_threshold_long():
    ts = _long_ts()
    ts.update(100.5)                    # +0.25 R
    assert ts.breakeven_armed is False
    assert ts.current_sl == pytest.approx(98.0)


def test_breakeven_arms_exactly_at_threshold_long():
    ts = _long_ts(breakeven_arm_rr=0.5)
    ts.update(101.0)                    # +0.5 R exactly
    assert ts.breakeven_armed is True


def test_breakeven_arms_above_threshold_long():
    ts = _long_ts(breakeven_arm_rr=0.5)
    ts.update(101.5)                    # +0.75 R
    assert ts.breakeven_armed is True


def test_breakeven_does_not_arm_below_threshold_short():
    ts = _short_ts()
    ts.update(99.5)                     # +0.25 R favorable for SHORT
    assert ts.breakeven_armed is False
    assert ts.current_sl == pytest.approx(102.0)


def test_breakeven_arms_at_threshold_short():
    ts = _short_ts(breakeven_arm_rr=0.5)
    ts.update(99.0)                     # +0.5 R favorable for SHORT
    assert ts.breakeven_armed is True


# ── SL movement on arm ────────────────────────────────────────────────────


def test_breakeven_lifts_sl_to_entry_plus_buffer_long():
    """Once armed, BUY SL moves to entry * (1 + buffer/100)."""
    ts = _long_ts(breakeven_buffer_pct=0.10)   # 10 bps buffer
    ts.update(101.0)                            # arm
    expected_sl = 100.0 * (1 + 0.10 / 100)      # 100.10
    assert ts.current_sl == pytest.approx(expected_sl, abs=0.001)


def test_breakeven_lowers_sl_to_entry_minus_buffer_short():
    """Once armed, SHORT SL moves to entry * (1 - buffer/100)."""
    ts = _short_ts(breakeven_buffer_pct=0.10)
    ts.update(99.0)                             # arm
    expected_sl = 100.0 * (1 - 0.10 / 100)      # 99.90
    assert ts.current_sl == pytest.approx(expected_sl, abs=0.001)


def test_breakeven_never_moves_sl_backwards_long():
    """If trail already raised SL above breakeven level, breakeven cannot
    pull it back down."""
    ts = _long_ts()
    ts.update(102.0)                            # +1R, trail activates
    sl_after_trail = ts.current_sl
    assert sl_after_trail > 100.10              # well above breakeven floor
    ts.update(101.5)                            # pullback, breakeven applies
    assert ts.current_sl == pytest.approx(sl_after_trail)


def test_breakeven_never_moves_sl_backwards_short():
    ts = _short_ts()
    ts.update(98.0)                             # +1R, trail activates
    sl_after_trail = ts.current_sl
    assert sl_after_trail < 99.90
    ts.update(98.5)
    assert ts.current_sl == pytest.approx(sl_after_trail)


# ── Monotonicity ──────────────────────────────────────────────────────────


def test_breakeven_armed_stays_armed_after_pullback_long():
    """Once armed, even a pullback below the arm threshold keeps it armed."""
    ts = _long_ts()
    ts.update(101.0)                            # arm at +0.5R
    assert ts.breakeven_armed is True
    ts.update(100.5)                            # back to +0.25R
    assert ts.breakeven_armed is True
    ts.update(100.0)                            # back to flat
    assert ts.breakeven_armed is True


def test_breakeven_protects_dead_zone_scenario_long():
    """The exact scenario the feature was designed for: +0.8R then reverse
    all the way down. Without breakeven, SL would still be at 98.0 (initial),
    losing the full Rs 2/share. With breakeven armed at 0.5R, SL is at
    100.10 -- worst case is a 10 bp gain, not a 1R loss."""
    ts = _long_ts()
    ts.update(101.6)                            # +0.8R MFE
    assert ts.breakeven_armed is True
    # Reverse to original SL level
    ts.update(98.0)
    # Effective SL is now 100.10 (breakeven), not 98.0
    assert ts.current_sl == pytest.approx(100.10, abs=0.001)


def test_breakeven_protects_dead_zone_scenario_short():
    ts = _short_ts()
    ts.update(98.4)                             # +0.8R MFE for SHORT
    assert ts.breakeven_armed is True
    ts.update(102.0)                            # reverse to original SL
    assert ts.current_sl == pytest.approx(99.90, abs=0.001)


# ── Disable switch ────────────────────────────────────────────────────────


def test_breakeven_disabled_means_legacy_behaviour_long():
    """With breakeven_enabled=False the SL never moves until trail activates."""
    ts = _long_ts(breakeven_enabled=False)
    ts.update(101.5)                            # +0.75R
    assert ts.breakeven_armed is False
    assert ts.current_sl == pytest.approx(98.0)


def test_breakeven_disabled_short():
    ts = _short_ts(breakeven_enabled=False)
    ts.update(98.5)                             # +0.75R favorable for SHORT
    assert ts.breakeven_armed is False
    assert ts.current_sl == pytest.approx(102.0)


# ── Integration with peak-giveback (both fire independently) ──────────────


def test_breakeven_and_peak_giveback_coexist():
    """A position that runs to +2R then gives back 35% should still trigger
    peak-giveback while the SL is also at breakeven (not interfere)."""
    ts = _long_ts()
    ts.update(104.0)                            # peak +2R
    assert ts.breakeven_armed is True
    assert ts.peak_giveback_armed is True       # 2.0 >= 1.5 arm threshold
    # 35% giveback of 2R = 0.7R, so target current_R = 1.3 -> price = 102.6
    ts.update(102.6)
    assert ts.should_peak_giveback_exit() is True
    # And SL is still at the trail level (which is above breakeven), proving
    # they don't fight each other.
    assert ts.current_sl >= 100.10


# ── RiskManager integration ───────────────────────────────────────────────


def test_risk_manager_propagates_breakeven_kwargs_to_trailing_stop():
    """The RiskManager should pass its config-derived breakeven settings
    through `create_trailing_stop`, not silently default."""
    cfg = {
        "risk": {
            "breakeven_enabled": True,
            "breakeven_arm_rr": 0.5,
            "breakeven_buffer_pct": 0.10,
        },
    }
    rm = RiskManager(cfg, initial_balance=100_000)
    ts = rm.create_trailing_stop("TEST", entry_price=100.0, initial_sl=98.0, side="BUY")
    assert ts.breakeven_enabled is True
    assert ts.breakeven_arm_rr == pytest.approx(0.5)
    assert ts.breakeven_buffer_pct == pytest.approx(0.10)


def test_risk_manager_breakeven_can_be_disabled_from_config():
    cfg = {"risk": {"breakeven_enabled": False}}
    rm = RiskManager(cfg, initial_balance=100_000)
    ts = rm.create_trailing_stop("TEST", entry_price=100.0, initial_sl=98.0, side="BUY")
    assert ts.breakeven_enabled is False
