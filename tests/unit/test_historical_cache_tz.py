"""Audit Issue #3 (2026-05-18) -- historical_cache.py TZ discipline parity.

Background
==========
``HistoricalCache.get_historical_data`` decides whether to refetch a cached
parquet based on a "is the end_date today or later?" check. The original
guard used ``datetime.now().date()`` (naive, host-TZ) for ``today`` and
``end_date.date()`` (whatever the caller passed, possibly naive) for the
end date. On a non-IST host (UTC CI box, an OCI shape before its TZ is
explicitly set, a developer laptop abroad) the day boundary would flip
inconsistently between the two sides of the comparison.

The 2026-05-18 Regression #8 patch fixed the ``today`` side
(``datetime.now(IST).date()``) but only conditionally fixed the
``end_date`` side: a tz-aware end_date was correctly converted via
``end_dt.astimezone(IST).date()``, but a NAIVE end_date kept using
``end_dt.date()`` -- still host-TZ-dependent. The Audit Issue #3 follow-up
localizes the naive branch too (``IST.localize(end_dt).date()``), so the
end_date is interpreted as IST regardless of host TZ.

These tests pin the new contract: a naive ``end_date`` that is "today" in
IST must always compare equal to ``today_IST``, regardless of what TZ the
host happens to be in. We can't easily simulate a foreign-TZ host in a
unit test (would require sub-processing with a patched TZ env var), but
we CAN exercise the code path with a naive end_date around the IST day
boundary and verify the freshness gate behaves correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
import pytz

# These tests round-trip OHLCV frames through parquet to exercise the
# real on-disk freshness path of HistoricalCache. pandas delegates
# parquet I/O to pyarrow / fastparquet; CI minimal images often skip
# both. The historical_cache production code uses parquet directly,
# so the tests can't pivot to pickle without papering over the actual
# code path. Cleanest fix: skip the entire module if no parquet engine
# is available -- the runtime container always ships pyarrow.
_PARQUET_ENGINE_AVAILABLE = False
for _engine in ("pyarrow", "fastparquet"):
    try:
        __import__(_engine)
        _PARQUET_ENGINE_AVAILABLE = True
        break
    except ImportError:
        continue
if not _PARQUET_ENGINE_AVAILABLE:
    pytest.skip(
        "neither pyarrow nor fastparquet available; "
        "historical_cache tests exercise the real parquet round-trip",
        allow_module_level=True,
    )

from core.historical_cache import HistoricalCache, IST  # noqa: E402


def _df_with_one_row() -> pd.DataFrame:
    """Minimal OHLCV DataFrame for parquet round-trips."""
    return pd.DataFrame({
        "open":   [100.0],
        "high":   [101.0],
        "low":    [ 99.0],
        "close":  [100.5],
        "volume": [1234],
    })


def _make_cache(tmp_path: Path, fresh_df: pd.DataFrame):
    """HistoricalCache wrapping a mock data source that always returns
    ``fresh_df`` on a miss. Returned tuple is (cache, source_mock)."""
    src = MagicMock()
    src.get_historical_data.return_value = fresh_df
    return HistoricalCache(src, cache_dir=tmp_path / "cache"), src


def _prime_cache_file(cache: HistoricalCache, symbol: str, interval: str,
                      start: datetime, end: datetime, df: pd.DataFrame,
                      age_seconds: float):
    """Write a parquet at the canonical cache path and back-date its mtime."""
    path = cache._key_path(symbol, interval, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    import os
    mtime = (datetime.now() - timedelta(seconds=age_seconds)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def test_naive_end_date_today_ist_triggers_freshness_check(tmp_path: Path):
    """A naive end_date that IS today in IST must be treated as
    'today-or-later' so the TTL check actually engages. Before the fix,
    a naive end_date on a non-IST host could be off by a day, which
    bypassed the TTL gate entirely and served arbitrarily stale bars.

    Setup: prime a cache file old enough to fail the 1h TTL (2h old),
    request with a naive end_date that's today in IST. New contract:
    the cache is considered stale and the underlying source is hit."""
    today_ist = datetime.now(IST).date()
    # Naive ``end_date`` that represents 02:00 IST today -- the case
    # most likely to flip on a UTC host (02:00 IST is 20:30 IST-prev-day UTC).
    naive_end = datetime(today_ist.year, today_ist.month, today_ist.day, 2, 0)
    naive_start = naive_end - timedelta(days=5)

    fresh_df = _df_with_one_row()
    cache, src = _make_cache(tmp_path, fresh_df)
    _prime_cache_file(
        cache, "RELIANCE", "5min", naive_start, naive_end,
        _df_with_one_row(), age_seconds=2 * 3600,  # 2h old, TTL is 1h
    )

    _ = cache.get_historical_data("RELIANCE", "5min", naive_start, naive_end)

    src.get_historical_data.assert_called_once_with(
        symbol="RELIANCE", interval="5min",
        start_date=naive_start, end_date=naive_end,
    )


def test_naive_end_date_yesterday_serves_from_cache_regardless_of_age(tmp_path: Path):
    """If end_date is YESTERDAY in IST, the TTL gate must NOT engage --
    historical bars for closed days never change. This test pins the
    other side of the boundary: a yesterday naive end_date returns
    from cache even when the parquet is older than the TTL.
    """
    yesterday_ist = (datetime.now(IST) - timedelta(days=1)).date()
    naive_end = datetime(
        yesterday_ist.year, yesterday_ist.month, yesterday_ist.day, 15, 30
    )
    naive_start = naive_end - timedelta(days=5)

    fresh_df = _df_with_one_row()
    cache, src = _make_cache(tmp_path, fresh_df)
    _prime_cache_file(
        cache, "TCS", "5min", naive_start, naive_end,
        _df_with_one_row(), age_seconds=24 * 3600,  # 1 day old, way over TTL
    )

    df = cache.get_historical_data("TCS", "5min", naive_start, naive_end)

    src.get_historical_data.assert_not_called()
    assert len(df) == 1


def test_tz_aware_end_date_today_ist_still_works(tmp_path: Path):
    """Regression guard for the existing tz-aware branch: a tz-aware
    end_date that's today IST must also engage the TTL gate. This was
    correct before the Audit Issue #3 patch but the test pins it so a
    future refactor of the localize logic can't break it silently."""
    today_ist = datetime.now(IST).date()
    utc = pytz.utc
    # 21:00 UTC == 02:30 IST next day; if we want "today IST" we build
    # IST-aware directly and convert.
    aware_end = IST.localize(
        datetime(today_ist.year, today_ist.month, today_ist.day, 11, 0)
    ).astimezone(utc)
    aware_start = aware_end - timedelta(days=5)

    fresh_df = _df_with_one_row()
    cache, src = _make_cache(tmp_path, fresh_df)
    _prime_cache_file(
        cache, "INFY", "5min", aware_start, aware_end,
        _df_with_one_row(), age_seconds=2 * 3600,
    )

    _ = cache.get_historical_data("INFY", "5min", aware_start, aware_end)
    src.get_historical_data.assert_called_once()


def test_freshness_branch_does_not_crash_on_dst_naive(tmp_path: Path):
    """``pytz.IST.localize`` raises ``AmbiguousTimeError`` or
    ``NonExistentTimeError`` for DST-folded zones, but Asia/Kolkata has
    no DST -- so a naive datetime in any month must localize cleanly.
    This test exists to lock that contract in case the project ever
    swaps timezones."""
    naive_end = datetime(2026, 3, 13, 2, 30)  # would be DST-fold in US/Eastern
    naive_start = naive_end - timedelta(days=5)
    fresh_df = _df_with_one_row()
    cache, _src = _make_cache(tmp_path, fresh_df)
    _prime_cache_file(
        cache, "ANY", "5min", naive_start, naive_end,
        _df_with_one_row(), age_seconds=60,
    )
    # Must not raise.
    cache.get_historical_data("ANY", "5min", naive_start, naive_end)
