"""Tests for the event blackout calendar added 2026-05-14.

Background: an algo entering a stock the afternoon before results is taking
a coin-flip with a thesis-window of hours. The EventCalendar bouncer skips
entries on stocks within N days of a known catalyst.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from core.event_calendar import EventCalendar


@pytest.fixture
def empty_calendar(tmp_path):
    f = tmp_path / "events.csv"
    f.write_text("symbol,event_date,event_type,notes\n", encoding="utf-8")
    cal = EventCalendar(str(f))
    cal.force_reload()
    return cal


@pytest.fixture
def populated_calendar(tmp_path):
    f = tmp_path / "events.csv"
    today = date(2026, 5, 14)
    rows = [
        f"HDFCBANK,{today + timedelta(days=1)},results,Q4_FY26",
        f"TCS,{today + timedelta(days=3)},results,",
        f"RELIANCE,{today - timedelta(days=2)},dividend,ex-date_passed",
        f"INFY,{today + timedelta(days=10)},results,",
    ]
    f.write_text(
        "symbol,event_date,event_type,notes\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    cal = EventCalendar(str(f), blackout_days_before=1, blackout_days_after=0)
    cal.force_reload()
    return cal, today


# ── Loading ───────────────────────────────────────────────────────────────


def test_missing_file_means_empty_no_crash(tmp_path):
    cal = EventCalendar(str(tmp_path / "does_not_exist.csv"))
    n = cal.force_reload()
    assert n == 0
    assert cal.is_blackout("HDFCBANK") == (False, None)


def test_empty_file_loads_zero_events(empty_calendar):
    assert empty_calendar.summary()["total_events"] == 0
    assert empty_calendar.is_blackout("HDFCBANK") == (False, None)


def test_malformed_date_is_skipped(tmp_path):
    f = tmp_path / "bad.csv"
    f.write_text(
        "symbol,event_date,event_type,notes\n"
        "HDFCBANK,not-a-date,results,\n"
        "TCS,2026-05-22,results,\n",
        encoding="utf-8",
    )
    cal = EventCalendar(str(f))
    n = cal.force_reload()
    assert n == 1   # only TCS row valid


def test_event_types_filter(tmp_path):
    """Only honour configured event types."""
    f = tmp_path / "ev.csv"
    f.write_text(
        "symbol,event_date,event_type,notes\n"
        "HDFCBANK,2026-05-15,results,\n"
        "RELIANCE,2026-05-15,dividend,\n",
        encoding="utf-8",
    )
    cal = EventCalendar(str(f), event_types=["results"])
    cal.force_reload()
    assert cal.summary()["total_events"] == 1


# ── is_blackout semantics ─────────────────────────────────────────────────


def test_blackout_day_before_event(populated_calendar):
    cal, today = populated_calendar
    blackout, ev = cal.is_blackout("HDFCBANK", today=today)
    assert blackout is True
    assert ev is not None
    assert ev.event_type == "results"


def test_no_blackout_three_days_out(populated_calendar):
    cal, today = populated_calendar
    blackout, _ = cal.is_blackout("TCS", today=today)
    assert blackout is False  # event is +3 days, window is only +1


def test_no_blackout_event_in_past(populated_calendar):
    cal, today = populated_calendar
    blackout, _ = cal.is_blackout("RELIANCE", today=today)
    assert blackout is False  # event was 2 days ago, post-window=0


def test_unknown_symbol_no_blackout(populated_calendar):
    cal, today = populated_calendar
    blackout, _ = cal.is_blackout("NOTASYMBOL", today=today)
    assert blackout is False


def test_case_insensitive_symbol_lookup(populated_calendar):
    cal, today = populated_calendar
    blackout, _ = cal.is_blackout("hdfcbank", today=today)
    assert blackout is True


def test_event_day_itself_is_blocked(populated_calendar):
    cal, today = populated_calendar
    # On the event day (delta=0), 0 <= 0 <= days_before(1) -> blackout
    event_day = today + timedelta(days=1)
    blackout, _ = cal.is_blackout("HDFCBANK", today=event_day - timedelta(days=0))
    assert blackout is True
    blackout_on_day, _ = cal.is_blackout("HDFCBANK", today=event_day)
    assert blackout_on_day is True


def test_post_event_blackout_window(tmp_path):
    """blackout_days_after lets us also block the recovery day."""
    today = date(2026, 5, 14)
    f = tmp_path / "ev.csv"
    f.write_text(
        f"symbol,event_date,event_type,notes\nHDFCBANK,{today - timedelta(days=1)},results,\n",
        encoding="utf-8",
    )
    cal = EventCalendar(str(f), blackout_days_before=0, blackout_days_after=2)
    cal.force_reload()
    # Event was yesterday; 2-day post window blocks today and tomorrow
    blackout, ev = cal.is_blackout("HDFCBANK", today=today)
    assert blackout is True
    assert ev.event_type == "results"


# ── Reload semantics ──────────────────────────────────────────────────────


def test_maybe_reload_picks_up_file_changes(tmp_path):
    f = tmp_path / "ev.csv"
    f.write_text(
        "symbol,event_date,event_type,notes\nHDFCBANK,2026-05-15,results,\n",
        encoding="utf-8",
    )
    cal = EventCalendar(str(f))
    cal.maybe_reload()
    assert cal.summary()["total_events"] == 1

    # Append a new event with a future date and a newer mtime
    import time
    time.sleep(0.05)
    f.write_text(
        "symbol,event_date,event_type,notes\n"
        "HDFCBANK,2026-05-15,results,\n"
        "TCS,2026-05-22,results,\n",
        encoding="utf-8",
    )
    # Make sure mtime moves forward on coarse-resolution filesystems
    new_time = time.time() + 1
    os.utime(f, (new_time, new_time))

    cal.maybe_reload()
    assert cal.summary()["total_events"] == 2
