"""
Unit tests for the EnsembleBacktester progress-meter and timestamp
helpers added on 2026-05-17.

Why these exist
---------------
The 2026-05-15 smoke-battery run churned for 45+ wall-clock hours with
*no* operator-visible progress signal. Two root causes were found:

  1. backtest_ensemble.run() iterated 209,597 (ts, symbol) events but
     never emitted a "% done / ETA" line, only strategy-signal lines.
  2. portfolio.open_position / close_position stamped entry_time and
     exit_time from `datetime.now(IST)` even when called from the
     backtest, so holding_minutes on every TradeRecord was measured in
     wall-clock seconds elapsed while the backtest was running — not in
     simulated market time.

The fix introduced two small static helpers on EnsembleBacktester that
this file pins down:

  - _ts_to_datetime: normalize pandas Timestamp / numpy datetime64 /
    python datetime to a tz-aware Asia/Kolkata datetime so naive bar
    indices can't slip through into Position.entry_time.
  - _format_duration: render seconds as `s` / `m` / `h` so the
    [BATTERY-PROGRESS] line is human-readable.

We deliberately do NOT spin up an end-to-end backtest here — that would
pull in yfinance, FeatureEngine, every strategy, etc., turning these
into slow integration tests. The integration coverage lives in the
existing tests/integration/ tree. Here we just lock the helpers'
contract.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import pytz

from research.backtest_ensemble import (
    PROGRESS_LOG_INTERVAL_EVENTS,
    EnsembleBacktester,
)

IST = pytz.timezone("Asia/Kolkata")


class TestTsToDatetime:
    def test_tz_aware_pandas_timestamp_passes_through(self):
        ts = pd.Timestamp("2026-03-18 10:15:00", tz="Asia/Kolkata")
        out = EnsembleBacktester._ts_to_datetime(ts)
        assert out.tzinfo is not None
        assert out.year == 2026 and out.hour == 10 and out.minute == 15

    def test_naive_pandas_timestamp_gets_ist_localized(self):
        ts = pd.Timestamp("2026-03-18 10:15:00")  # no tz
        out = EnsembleBacktester._ts_to_datetime(ts)
        # Must be tz-aware after the helper runs, else downstream
        # portfolio.close_position would raise TypeError when computing
        # holding_minutes against a tz-aware entry_time.
        assert out.tzinfo is not None
        assert (out.hour, out.minute) == (10, 15)

    def test_naive_python_datetime_gets_ist_localized(self):
        ts = datetime(2026, 3, 18, 10, 15, 0)
        out = EnsembleBacktester._ts_to_datetime(ts)
        assert out.tzinfo is not None
        assert out.hour == 10 and out.minute == 15

    def test_already_ist_datetime_is_unchanged(self):
        ts = IST.localize(datetime(2026, 3, 18, 10, 15, 0))
        out = EnsembleBacktester._ts_to_datetime(ts)
        assert out == ts

    def test_utc_datetime_is_converted_to_ist(self):
        utc = pytz.UTC.localize(datetime(2026, 3, 18, 4, 45, 0))  # = 10:15 IST
        out = EnsembleBacktester._ts_to_datetime(utc)
        assert out.tzinfo is not None
        # Should now read 10:15 in IST.
        assert (out.hour, out.minute) == (10, 15)


class TestFormatDuration:
    @pytest.mark.parametrize(
        "secs, expected_suffix",
        [
            (0.0, "s"),
            (12.3, "s"),
            (59.9, "s"),
            (60.0, "m"),
            (90.0, "m"),
            (3599.0, "m"),
            (3600.0, "h"),
            (86400.0, "h"),
        ],
    )
    def test_unit_picked_by_magnitude(self, secs, expected_suffix):
        out = EnsembleBacktester._format_duration(secs)
        assert out.strip().endswith(expected_suffix)

    def test_negative_input_clamped_to_zero(self):
        # Defensive: time.time() math can briefly go negative if the
        # system clock steps backwards (NTP slew). Format must not
        # crash or render a "-0.1s ETA".
        out = EnsembleBacktester._format_duration(-5.0)
        assert "-" not in out


class TestProgressInterval:
    def test_interval_constant_is_reasonable(self):
        # If someone accidentally drops this to e.g. 10, multi-million
        # event runs would emit 100k+ INFO lines. If they raise it to
        # 10M, the operator gets back to "no progress signal" territory.
        # 10k events at the observed ~1.25 ev/s = a progress line every
        # ~2 hours, which is the sweet spot the audit picked.
        assert 1_000 <= PROGRESS_LOG_INTERVAL_EVENTS <= 100_000
