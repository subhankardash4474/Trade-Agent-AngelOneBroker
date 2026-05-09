"""Tests for the peak-giveback exit added 2026-05-07.

Background: today's MEESHO trade hit MFE +Rs 276 (peak ~3.5R) and exited
on a signal-flip at +Rs 71 — a 74% giveback the existing price-trail
(which only fires on a 0.3% pullback from peak price) never caught.
The new peak-giveback exit triggers when current_R has fallen by
`peak_giveback_pct` of peak_R, INDEPENDENT of price-trail logic.
"""
from __future__ import annotations

import pytest

from core.risk_manager import RiskManager, TrailingStop


# ── TrailingStop unit tests ──


def _new_long_ts(**overrides) -> TrailingStop:
    defaults = dict(
        entry_price=100.0,
        initial_sl=98.0,            # 1R = Rs 2 per share
        side="BUY",
        peak_arm_rr=1.5,
        peak_giveback_pct=35.0,
        peak_giveback_enabled=True,
    )
    defaults.update(overrides)
    return TrailingStop(**defaults)


def _new_short_ts(**overrides) -> TrailingStop:
    defaults = dict(
        entry_price=100.0,
        initial_sl=102.0,           # 1R = Rs 2 per share
        side="SELL",
        peak_arm_rr=1.5,
        peak_giveback_pct=35.0,
        peak_giveback_enabled=True,
    )
    defaults.update(overrides)
    return TrailingStop(**defaults)


def test_peak_giveback_does_not_arm_below_threshold():
    ts = _new_long_ts()
    # Price moves to +1.0R then drops to 0
    ts.update(102.0)
    ts.update(100.0)
    assert ts.peak_giveback_armed is False
    assert ts.should_peak_giveback_exit() is False


def test_peak_giveback_arms_at_arm_rr():
    ts = _new_long_ts(peak_arm_rr=1.5)
    ts.update(103.0)  # +1.5R exactly
    assert ts.peak_giveback_armed is True


def test_peak_giveback_does_not_fire_while_making_new_highs():
    ts = _new_long_ts()
    ts.update(105.0)  # +2.5R
    # Tiny pullback of 1% of peak — way under 35% threshold
    ts.update(104.9)
    assert ts.should_peak_giveback_exit() is False


def test_peak_giveback_fires_at_threshold_long():
    ts = _new_long_ts(peak_arm_rr=1.5, peak_giveback_pct=35.0)
    ts.update(106.0)  # peak +3R
    assert ts.peak_unrealized_r == pytest.approx(3.0)
    # Drop to +1.9R = giveback of 1.1R = 36.7% of peak (just past 35%)
    ts.update(103.8)
    assert ts.last_unrealized_r == pytest.approx(1.9, abs=0.05)
    assert ts.should_peak_giveback_exit() is True


def test_peak_giveback_fires_at_threshold_short():
    ts = _new_short_ts(peak_arm_rr=1.5, peak_giveback_pct=35.0)
    ts.update(94.0)  # short, peak +3R
    assert ts.peak_unrealized_r == pytest.approx(3.0)
    # Drift back to 96.2 -> +1.9R, 36.7% giveback (just past 35%)
    ts.update(96.2)
    assert ts.last_unrealized_r == pytest.approx(1.9, abs=0.05)
    assert ts.should_peak_giveback_exit() is True


def test_peak_giveback_below_threshold_no_fire():
    ts = _new_long_ts(peak_giveback_pct=35.0)
    ts.update(106.0)  # peak +3R
    ts.update(105.0)  # back to +2.5R, only 17% giveback
    assert ts.should_peak_giveback_exit() is False


def test_peak_giveback_disabled_never_fires():
    ts = _new_long_ts(peak_giveback_enabled=False)
    ts.update(106.0)  # peak +3R
    ts.update(101.0)  # back to +0.5R, 83% giveback
    assert ts.should_peak_giveback_exit() is False


def test_peak_giveback_meesho_scenario():
    """Real today's case: SHORT MEESHO entry 203.36, peak low 196.28
    (~+3.47R for 38 shares with SL ~205), drift to 201.32. The existing
    price-trail (0.3% from 196.28 = 196.87) was never breached on bar
    closes. Peak-giveback at 35% of 3.47R = 1.21R giveback should fire
    once current_R drops to 2.26R (~price 198.84).
    """
    entry = 203.36
    sl = 205.36   # 1R = Rs 2 per share, 1R-pct = 0.98%
    ts = TrailingStop(
        entry_price=entry, initial_sl=sl, side="SELL",
        peak_arm_rr=1.5, peak_giveback_pct=35.0,
    )
    # Walk to MFE peak
    ts.update(196.28)
    peak_r = ts.peak_unrealized_r
    assert peak_r >= 3.4

    # Slow drift back. At some point between 198 and 200 we should fire.
    fired_at = None
    for px in [197.5, 198.5, 199.0, 199.5, 200.0]:
        ts.update(px)
        if ts.should_peak_giveback_exit():
            fired_at = px
            break
    assert fired_at is not None, "should fire somewhere on the drift back"
    # Sanity: must fire BEFORE the actual exit price (201.32)
    assert fired_at < 201.32


# ── RiskManager wiring test ──


def test_risk_manager_passes_peak_giveback_config_to_ts():
    cfg = {
        "risk": {
            "peak_giveback_enabled": True,
            "peak_giveback_arm_rr": 2.0,
            "peak_giveback_pct": 50.0,
        }
    }
    rm = RiskManager(cfg, initial_balance=10000)
    ts = rm.create_trailing_stop("FOO", entry_price=100, initial_sl=98, side="BUY")
    assert ts.peak_arm_rr == 2.0
    assert ts.peak_giveback_pct == 50.0
    assert ts.peak_giveback_enabled is True


def test_risk_manager_default_peak_giveback_enabled():
    rm = RiskManager({}, initial_balance=10000)
    ts = rm.create_trailing_stop("FOO", entry_price=100, initial_sl=98, side="BUY")
    # Defaults: enabled with arm=1.5R, giveback=35%
    assert ts.peak_giveback_enabled is True
    assert ts.peak_arm_rr == 1.5
    assert ts.peak_giveback_pct == 35.0


def test_classic_trail_and_peak_giveback_independent():
    """Both protections should track in parallel; whichever fires first
    is the caller's choice (caller checks both)."""
    ts = _new_long_ts()
    ts.update(102.5)  # +1.25R - below trail_activation_rr=1.0? 1.25 > 1.0 yes
    assert ts.trailing_active is True
    # Peak-giveback not armed yet (need 1.5R)
    assert ts.peak_giveback_armed is False

    ts.update(106.0)  # peak +3R, now armed
    assert ts.peak_giveback_armed is True
    # 0.3% trail from 106 = ~105.68. Current SL should be at least that high.
    assert ts.current_sl >= 105.0
