"""Bug L YELLOW-detector tests for tools/audit_checkpoint.py.

Background
----------
On 2026-05-28 (Bakri Eid) the trader daemon ran ~7h of cycles against
a closed market because the date was missing from
``packages/core/data_handler.py:NSE_HOLIDAYS``. The defensive stack
absorbed the one signal generated (zero damage), but every audit
checkpoint that day reported ``GREEN``. The detector tested here is
the belt-and-braces tripwire that catches the same pattern next time
the calendar misses a holiday.

The function under test is
``tools.audit_checkpoint._possible_missed_holiday_verdict``. We pass
``holiday_set=set()`` in most tests so the detector's behaviour is
isolated from changes to the live ``NSE_HOLIDAYS`` constant. One test
deliberately passes a populated set to verify the "today is a known
holiday -> don't alert" short-circuit.

Heuristic contract (must NOT change without updating these tests +
the module-level Bug L comment in audit_checkpoint.py):

    YELLOW iff ALL of:
      - now.weekday() < 5 (Mon-Fri)
      - now.strftime("%Y-%m-%d") NOT IN holiday_set
      - now.hour > 12 or (now.hour == 12 and now.minute >= 30)
      - signal_pipeline.cycles_completed >= 5
      - signal_pipeline.total_ensemble_acts == 0
      - day_pnl.closed_trades_today == 0
      - signal_pipeline.avg_directional_votes < 15
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tools.audit_checkpoint import _possible_missed_holiday_verdict


# ────────────────── shared fixtures ──────────────────


@pytest.fixture
def quiet_pipeline() -> dict:
    """A signal_pipeline payload that mirrors the 16:00 checkpoint
    on 2026-05-28 (Bug L day): cycles ran, no acts, low votes."""
    return {
        "cycles_completed": 6,
        "total_ensemble_acts": 0,
        "avg_directional_votes": 6.0,
        "current_regime": "bear_low_vol",
    }


@pytest.fixture
def no_trades_pnl() -> dict:
    return {"closed_trades_today": 0}


@pytest.fixture
def bug_l_now() -> datetime:
    """A weekday afternoon timestamp that is NOT in any holiday set we
    pass. Used as the canonical 'should trigger' moment."""
    # 2026-07-09 is a Thursday, not on the authoritative 2026 NSE
    # calendar. Using it instead of 2026-05-28 (which is now in
    # NSE_HOLIDAYS post-Bug-L fix) keeps the test independent of the
    # data_handler constant.
    return datetime(2026, 7, 9, 14, 0)


# ────────────────── positive case ──────────────────


def test_fires_on_missed_holiday_pattern(
    quiet_pipeline: dict, no_trades_pnl: dict, bug_l_now: datetime
) -> None:
    """The exact pattern from 2026-05-28 must trigger YELLOW."""
    verdict = _possible_missed_holiday_verdict(
        bug_l_now, quiet_pipeline, no_trades_pnl, holiday_set=set()
    )
    assert verdict is not None
    assert verdict.startswith("YELLOW")
    assert "POSSIBLE_MISSED_HOLIDAY" in verdict
    assert "Bug L" in verdict
    # The verdict text must include actionable detail.
    assert "2026-07-09" in verdict
    assert "6 cycles" in verdict
    assert "NSE_HOLIDAYS" in verdict


# ────────────────── short-circuit gates ──────────────────


def test_silent_on_saturday(quiet_pipeline: dict, no_trades_pnl: dict) -> None:
    """Weekend dates already non-trading; no alert needed."""
    saturday = datetime(2026, 7, 11, 14, 0)  # Sat
    assert (
        _possible_missed_holiday_verdict(
            saturday, quiet_pipeline, no_trades_pnl, holiday_set=set()
        )
        is None
    )


def test_silent_on_sunday(quiet_pipeline: dict, no_trades_pnl: dict) -> None:
    sunday = datetime(2026, 7, 12, 14, 0)
    assert (
        _possible_missed_holiday_verdict(
            sunday, quiet_pipeline, no_trades_pnl, holiday_set=set()
        )
        is None
    )


def test_silent_when_today_is_known_holiday(
    quiet_pipeline: dict, no_trades_pnl: dict, bug_l_now: datetime
) -> None:
    """If the calendar already knows today is a holiday, the daemon
    would have idled. No alert needed."""
    holiday_set = {bug_l_now.strftime("%Y-%m-%d")}
    assert (
        _possible_missed_holiday_verdict(
            bug_l_now, quiet_pipeline, no_trades_pnl, holiday_set=holiday_set
        )
        is None
    )


def test_silent_before_12_30_ist(
    quiet_pipeline: dict, no_trades_pnl: dict
) -> None:
    """Don't alert in the morning -- a real quiet morning can still
    produce signals by lunch."""
    morning = datetime(2026, 7, 9, 11, 0)
    assert (
        _possible_missed_holiday_verdict(
            morning, quiet_pipeline, no_trades_pnl, holiday_set=set()
        )
        is None
    )


def test_silent_exactly_at_12_29(
    quiet_pipeline: dict, no_trades_pnl: dict
) -> None:
    """Inclusive lower bound is 12:30 -- 12:29 must not fire."""
    edge = datetime(2026, 7, 9, 12, 29)
    assert (
        _possible_missed_holiday_verdict(
            edge, quiet_pipeline, no_trades_pnl, holiday_set=set()
        )
        is None
    )


def test_fires_exactly_at_12_30(
    quiet_pipeline: dict, no_trades_pnl: dict
) -> None:
    """12:30 sharp is the first eligible minute."""
    edge = datetime(2026, 7, 9, 12, 30)
    verdict = _possible_missed_holiday_verdict(
        edge, quiet_pipeline, no_trades_pnl, holiday_set=set()
    )
    assert verdict is not None
    assert verdict.startswith("YELLOW")


# ────────────────── activity-threshold gates ──────────────────


def test_silent_when_ensemble_acts_present(
    no_trades_pnl: dict, bug_l_now: datetime
) -> None:
    """A single ensemble act is enough to convince us the market is
    open. Don't alert."""
    pipe = {
        "cycles_completed": 6,
        "total_ensemble_acts": 1,
        "avg_directional_votes": 6.0,
    }
    assert (
        _possible_missed_holiday_verdict(
            bug_l_now, pipe, no_trades_pnl, holiday_set=set()
        )
        is None
    )


def test_silent_when_trades_closed_today(
    quiet_pipeline: dict, bug_l_now: datetime
) -> None:
    """A closed trade -- even a stop-out -- means signals fired and
    orders were placed. Don't alert."""
    pnl = {"closed_trades_today": 1}
    assert (
        _possible_missed_holiday_verdict(
            bug_l_now, quiet_pipeline, pnl, holiday_set=set()
        )
        is None
    )


def test_silent_when_high_avg_votes(
    no_trades_pnl: dict, bug_l_now: datetime
) -> None:
    """Plenty of directional voting means data is flowing and
    strategies are firing. Slow-decision day, not a holiday."""
    pipe = {
        "cycles_completed": 6,
        "total_ensemble_acts": 0,
        "avg_directional_votes": 25.0,
    }
    assert (
        _possible_missed_holiday_verdict(
            bug_l_now, pipe, no_trades_pnl, holiday_set=set()
        )
        is None
    )


def test_silent_when_few_cycles(
    no_trades_pnl: dict, bug_l_now: datetime
) -> None:
    """If the daemon ran <5 cycles in the window, it likely just
    started or was restarted; don't fire on insufficient evidence."""
    pipe = {
        "cycles_completed": 2,
        "total_ensemble_acts": 0,
        "avg_directional_votes": 0.0,
    }
    assert (
        _possible_missed_holiday_verdict(
            bug_l_now, pipe, no_trades_pnl, holiday_set=set()
        )
        is None
    )


# ────────────────── robustness ──────────────────


def test_handles_missing_keys_gracefully(bug_l_now: datetime) -> None:
    """Empty payloads should not raise; should default to 'no alert'
    because the activity thresholds aren't met."""
    assert (
        _possible_missed_holiday_verdict(bug_l_now, {}, {}, holiday_set=set())
        is None
    )


def test_handles_none_values_gracefully(bug_l_now: datetime) -> None:
    """If upstream produces None instead of 0 (e.g. empty CycleDigest
    averages), the detector must still return cleanly."""
    pipe = {
        "cycles_completed": None,
        "total_ensemble_acts": None,
        "avg_directional_votes": None,
    }
    pnl = {"closed_trades_today": None}
    assert (
        _possible_missed_holiday_verdict(
            bug_l_now, pipe, pnl, holiday_set=set()
        )
        is None
    )


def test_uses_live_holiday_set_by_default(
    quiet_pipeline: dict, no_trades_pnl: dict
) -> None:
    """If holiday_set is omitted, the live NSE_HOLIDAYS from
    data_handler must be consulted -- so 2026-05-28 (now in the set
    post-Bug-L) does NOT fire."""
    bakri_eid = datetime(2026, 5, 28, 14, 0)  # Thu, now in NSE_HOLIDAYS
    assert (
        _possible_missed_holiday_verdict(
            bakri_eid, quiet_pipeline, no_trades_pnl  # no holiday_set kwarg
        )
        is None
    )
