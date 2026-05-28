"""Bug L regression tests for the NSE holiday calendar.

Background
----------
``packages/core/data_handler.py:NSE_HOLIDAYS`` is consulted by
``DataHandler.is_market_open()`` to decide whether to fetch/scan
on a given day. On 2026-05-28 the live trader daemon ran ~7 hours
of trading cycles against a closed market because 2026-05-28 was
missing from the set (NSE was closed for Bakri Eid that day).
Damage was zero -- the defensive stack (opening lockout +
allow_shorts:false + paper mode + xgb-disabled) absorbed
everything -- but a diff against authoritative sources (Samco,
Upstox, Zerodha, ET, Outlook Business) revealed the 2026 list
had **9 spurious entries** and was **missing 7 real holidays**.

These tests pin the corrected list so a future refactor cannot
silently re-introduce the gap.

Source of truth
---------------
All 5 third-party sources (Samco, Upstox, Zerodha, ET, Outlook
Business) agree on the 2026 NSE list:

    01-15 Municipal Corporation Elections in Maharashtra (Thu)
    01-26 Republic Day                                   (Mon)
    03-03 Holi                                           (Tue)
    03-26 Shri Ram Navami                                (Thu)
    03-31 Shri Mahavir Jayanti                           (Tue)
    04-03 Good Friday                                    (Fri)
    04-14 Dr. Baba Saheb Ambedkar Jayanti                (Tue)
    05-01 Maharashtra Day / Buddha Pournima              (Fri)
    05-28 Bakri Id                                       (Thu)
    06-26 Muharram                                       (Fri)
    09-14 Ganesh Chaturthi                               (Mon)
    10-02 Mahatma Gandhi Jayanti                         (Fri)
    10-20 Dussehra                                       (Tue)
    11-10 Diwali-Balipratipada                           (Tue)
    11-24 Prakash Gurpurb Sri Guru Nanak Dev             (Tue)
    12-25 Christmas                                      (Fri)

If a future curator extends to 2027 they should follow the same
contract documented in the data_handler.py header: each entry
carries the festival name + day-of-week as an inline comment.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from packages.core.data_handler import NSE_HOLIDAYS, is_known_holiday_year


# ────────────────────── 2026 contract ──────────────────────


# Authoritative 2026 NSE holiday list -- cross-checked against
# Samco/Upstox/Zerodha/ET/Outlook Business on 2026-05-28.
_EXPECTED_2026 = {
    "2026-01-15",  # Municipal Corporation Elections in Maharashtra
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day / Buddha Pournima
    "2026-05-28",  # Bakri Id (Bug L: added after live miss)
    "2026-06-26",  # Muharram (Bug L: corrected from 2026-07-07)
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali-Balipratipada
    "2026-11-24",  # Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",  # Christmas
}


def test_2026_holiday_set_matches_authoritative_list() -> None:
    """The 2026 subset must exactly match the cross-source list. If a
    curator changes ``NSE_HOLIDAYS`` they MUST update this constant +
    record the rationale in docs/findings_log_*.md."""
    actual_2026 = {h for h in NSE_HOLIDAYS if h.startswith("2026-")}
    missing = _EXPECTED_2026 - actual_2026
    extra = actual_2026 - _EXPECTED_2026
    assert not missing and not extra, (
        "2026 NSE holiday calendar drift detected.\n"
        f"  Missing (should be present): {sorted(missing)}\n"
        f"  Extra (should be removed): {sorted(extra)}\n"
        "Cross-check Samco/Upstox/Zerodha before changing this test."
    )


def test_2026_has_exactly_16_holidays() -> None:
    """NSE publishes 16 trading holidays for 2026. Hard-pinning the
    count is a cheap second guard against silent additions."""
    count = len([h for h in NSE_HOLIDAYS if h.startswith("2026-")])
    assert count == 16, (
        f"Expected exactly 16 NSE holidays for 2026, found {count}. "
        "Verify against the official NSE 2026 holiday circular."
    )


# ─────────────────── Bug L specific tests ───────────────────


def test_bakri_id_2026_in_set() -> None:
    """The specific date that caused the 2026-05-28 live miss. If this
    assertion ever fails, the trader daemon will scan a closed market
    on Bakri Eid every year until the next curator notices."""
    assert "2026-05-28" in NSE_HOLIDAYS, (
        "Bug L regression: 2026-05-28 (Bakri Id, Thursday) missing from "
        "NSE_HOLIDAYS. This was the exact gap that caused the live "
        "daemon to burn ~7h of compute on 2026-05-28 scanning a closed "
        "market. See docs/findings_log_2026-05-27.md section 12."
    )


def test_muharram_2026_date_correct() -> None:
    """The pre-Bug-L set listed 2026-07-07 as Muharram. The correct
    date is 2026-06-26 per all 5 third-party sources. Both must hold:
    the wrong date must be ABSENT, the right date must be PRESENT."""
    assert "2026-06-26" in NSE_HOLIDAYS, (
        "2026-06-26 (Muharram, Friday) missing. This was the next gap "
        "that would have bitten us if Bug L hadn't been caught early."
    )
    assert "2026-07-07" not in NSE_HOLIDAYS, (
        "2026-07-07 is NOT a 2026 NSE holiday. It was an error in the "
        "pre-Bug-L list, corrected to 2026-06-26."
    )


@pytest.mark.parametrize(
    "spurious_date",
    [
        # All entries that were in the pre-Bug-L 2026 set but are NOT on
        # any authoritative 2026 NSE calendar.
        "2026-02-17",
        "2026-03-20",
        "2026-03-30",
        "2026-05-25",
        "2026-07-07",
        "2026-08-15",  # Saturday in 2026, already non-trading
        "2026-08-17",
        "2026-10-09",
        "2026-10-21",
    ],
)
def test_no_spurious_2026_entries(spurious_date: str) -> None:
    """Each of these dates was in the pre-Bug-L set despite NOT being
    on any authoritative 2026 NSE calendar. They must stay out."""
    assert spurious_date not in NSE_HOLIDAYS, (
        f"{spurious_date} was a spurious entry in the pre-Bug-L set "
        "and should NOT be added back. If you believe this is a real "
        "NSE holiday, cite the source in docs/findings_log_*.md before "
        "changing this test."
    )


# ───────────── is_known_holiday_year contract ─────────────


def test_known_holiday_year_coverage_includes_2025_and_2026() -> None:
    """B-6 contract: callers can ask whether a year is curated. Without
    this, 2027 would silently return 'not a holiday' for every date."""
    assert is_known_holiday_year(2025)
    assert is_known_holiday_year(2026)


def test_known_holiday_year_excludes_2027() -> None:
    """2027 has not been curated. is_known_holiday_year MUST report
    that fact so run_daemon can warn loudly."""
    assert not is_known_holiday_year(2027), (
        "is_known_holiday_year(2027) should be False until the 2027 "
        "NSE schedule is added to NSE_HOLIDAYS. If you have added "
        "2027 entries, update this test."
    )


# ───────────── is_market_open integration ─────────────


@pytest.fixture
def fake_market_config() -> dict:
    return {
        "trading_hours": {
            "start": "09:15",
            "end": "15:30",
        }
    }


def _make_fake_datetime(target_dt: datetime) -> type:
    """Build a fake datetime class whose .now() returns ``target_dt`` but
    which delegates every other attribute (.strptime, .strftime, etc.)
    to the real datetime class. Naive subclassing breaks isinstance
    checks; metaclass __getattr__ keeps that intact."""
    real_dt = datetime

    class _Meta(type):
        def __getattr__(cls, name):  # pragma: no cover - simple proxy
            return getattr(real_dt, name)

    class _FakeDt(metaclass=_Meta):
        @staticmethod
        def now(tz=None):
            return target_dt

    return _FakeDt


@pytest.mark.parametrize(
    "iso_date,festival",
    [
        ("2026-05-28", "Bakri Id"),
        ("2026-06-26", "Muharram"),
        ("2026-01-26", "Republic Day"),
        ("2026-12-25", "Christmas"),
    ],
)
def test_is_market_open_returns_false_on_known_2026_holidays(
    monkeypatch: pytest.MonkeyPatch, iso_date: str, festival: str
) -> None:
    """is_market_open() must return False at any time of day on a known
    NSE holiday, even when the wall-clock time would otherwise fall
    within trading hours."""
    from packages.core import data_handler

    target_dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(
        hour=10, minute=30  # mid-session local time
    )
    target_dt = data_handler.IST.localize(target_dt)

    monkeypatch.setattr(data_handler, "datetime", _make_fake_datetime(target_dt))

    dh = data_handler.DataHandler.__new__(data_handler.DataHandler)
    dh._market_config = {"trading_hours": {"start": "09:15", "end": "15:30"}}

    assert dh.is_market_open() is False, (
        f"is_market_open() returned True at 10:30 IST on {iso_date} "
        f"({festival}) -- daemon would treat a closed market as open. "
        "Check NSE_HOLIDAYS membership."
    )


def test_is_market_open_returns_true_on_regular_trading_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: ensure we didn't accidentally inflate the holiday
    set such that a normal trading day is rejected. 2026-05-29
    (Friday after Bakri Eid) is a known regular trading day."""
    from packages.core import data_handler

    target_dt = datetime(2026, 5, 29, 10, 30)  # Friday after Bakri Eid
    target_dt = data_handler.IST.localize(target_dt)

    monkeypatch.setattr(data_handler, "datetime", _make_fake_datetime(target_dt))

    dh = data_handler.DataHandler.__new__(data_handler.DataHandler)
    dh._market_config = {"trading_hours": {"start": "09:15", "end": "15:30"}}

    assert dh.is_market_open() is True, (
        "is_market_open() returned False on a known regular trading "
        "day (Fri 2026-05-29). Check NSE_HOLIDAYS for spurious entries."
    )
