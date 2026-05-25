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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 senior-dev backtester scan — three bug fixes
# ─────────────────────────────────────────────────────────────────────


class _FakePos:
    """Lightweight stand-in for Portfolio.Position used by intra-bar tests.

    We can't import the real Position type because it'd pull in
    core.portfolio and its DB dependencies; for testing the pure
    `_detect_intrabar_exit` helper a tiny duck-typed object suffices.
    """
    def __init__(self, side, stop_loss=None, take_profit=None, entry_price=None):
        self.side = side
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.entry_price = entry_price


class TestIntrabarExitDetection:
    """Bug A fix: SL/TP triggers must fire on bar WICKS, not just close.

    Before this fix the backtester checked `close <= sl` only, so any
    bar that touched SL intra-bar and recovered to close above it was
    treated as "still holding" -- in live trading the SL order would
    have filled the moment price touched. Net effect across a 60-day
    battery: backtest systematically OVERSTATED PnL and UNDERSTATED
    drawdown. The pre-fix close-only check is the bug we're locking
    out forever with these tests.
    """

    # ── long-side cases ──
    def test_long_sl_wick_triggers(self):
        # Long position; bar dipped below SL but closed above it.
        # Live = stopped out at SL; old backtest = still holding.
        pos = _FakePos("BUY", stop_loss=95.0, take_profit=110.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=101.0, low=94.5, close=99.5,
        )
        assert trig == "stop_loss"
        assert px == 95.0  # filled exactly at SL

    def test_long_tp_wick_triggers(self):
        # Long; bar spiked above TP then pulled back.
        pos = _FakePos("BUY", stop_loss=95.0, take_profit=110.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=110.5, low=99.5, close=105.0,
        )
        assert trig == "take_profit"
        assert px == 110.0  # filled exactly at TP

    def test_long_gap_down_through_sl_fills_at_open(self):
        # Long; bar gap-opens below SL -- live broker fills at OPEN
        # (worse than the static SL level). Pre-fix code would have
        # used `close` which could be anywhere.
        pos = _FakePos("BUY", stop_loss=95.0, take_profit=110.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=90.0, high=92.0, low=89.0, close=91.0,
        )
        assert trig == "stop_loss"
        assert px == 90.0  # filled at the gap open (worse than SL)

    def test_long_close_only_inside_range_no_trigger(self):
        # SL = 95, TP = 110. Bar moved entirely between them.
        pos = _FakePos("BUY", stop_loss=95.0, take_profit=110.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=104.0, low=98.0, close=101.0,
        )
        assert trig is None
        assert px is None

    # ── short-side cases ──
    def test_short_sl_wick_triggers(self):
        # Short; SL above entry; bar wicked above SL then closed below.
        pos = _FakePos("SELL", stop_loss=105.0, take_profit=90.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=105.5, low=99.0, close=101.0,
        )
        assert trig == "stop_loss"
        assert px == 105.0

    def test_short_tp_wick_triggers(self):
        # Short; bar spiked DOWN through TP and recovered.
        pos = _FakePos("SELL", stop_loss=105.0, take_profit=90.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=101.0, low=89.5, close=95.0,
        )
        assert trig == "take_profit"
        assert px == 90.0

    def test_short_gap_up_through_sl_fills_at_open(self):
        # Short; gap-opens above SL = live fills at OPEN (worse fill).
        pos = _FakePos("SELL", stop_loss=105.0, take_profit=90.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=108.0, high=109.0, low=107.0, close=107.5,
        )
        assert trig == "stop_loss"
        assert px == 108.0

    # ── conservative tie-breaking ──
    def test_long_both_sl_and_tp_hit_chooses_sl_worst_case(self):
        # Wide-range bar that touched BOTH SL and TP -- intra-bar order
        # is unknown, so we conservatively assume SL fired first.
        # Avoids the opposite optimistic bias that would have to assume
        # TP fired first.
        pos = _FakePos("BUY", stop_loss=95.0, take_profit=110.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=112.0, low=93.0, close=105.0,
        )
        assert trig == "stop_loss"
        assert px == 95.0

    def test_short_both_sl_and_tp_hit_chooses_sl_worst_case(self):
        pos = _FakePos("SELL", stop_loss=105.0, take_profit=90.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=107.0, low=88.0, close=95.0,
        )
        assert trig == "stop_loss"
        assert px == 105.0

    def test_missing_sl_or_tp_does_not_crash(self):
        # Defensive: some legacy positions may have stop_loss=None.
        # Detector must not raise; the unset level just can't trigger.
        pos = _FakePos("BUY", stop_loss=None, take_profit=110.0, entry_price=100.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=111.0, low=80.0, close=105.0,
        )
        assert trig == "take_profit"
        assert px == 110.0

    def test_unknown_side_returns_no_trigger(self):
        # Defensive: a corrupt Position object (side=None) must not
        # crash the loop; just skip the exit check for that bar.
        pos = _FakePos(side=None, stop_loss=95.0, take_profit=110.0)
        trig, px = EnsembleBacktester._detect_intrabar_exit(
            pos, open_p=100.0, high=120.0, low=80.0, close=100.0,
        )
        assert trig is None
        assert px is None


class TestSharpeUsesDailyEquities:
    """Bug D fix: Sharpe must annualize from DAILY samples, not events.

    The legacy code computed std() over the per-event equity_curve
    (220k entries for a 60-day battery) and multiplied by sqrt(252).
    Since 252 is the annualization factor for DAILY returns, this was
    apples-to-oranges and produced numbers that had no intuitive meaning.
    """

    def test_sharpe_uses_daily_path_when_provided(self):
        from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

        bt = EnsembleBacktester.__new__(EnsembleBacktester)
        bt.bt = BacktestConfig(initial_capital=100000.0)
        # Steady 1% daily gain across 30 days -> known annualised Sharpe.
        # mean(daily_ret) = 0.01, std(daily_ret) = 0
        # We can't test std=0 (degenerate); pick a noisy gain.
        rng = [100000.0 * (1.01 ** i) * (1 + (0.002 if i % 2 == 0 else -0.001))
               for i in range(30)]
        result = bt._build_result(
            trades=[],
            equity_curve=rng,                          # all daily samples used as event curve too
            gate_stats=type("G", (), {"as_dict": lambda self: {}})(),
            daily_equities=rng,
        )
        # With ~1% positive drift and small noise, Sharpe should be positive
        # and well above 0 but a reasonable number (not the wild ev/s value).
        assert result.sharpe > 0
        assert result.sharpe < 100  # legacy event-level Sharpe routinely went > 200

    def test_sharpe_falls_back_when_daily_missing(self):
        # If daily_equities is not provided (older caller), the legacy
        # event-level computation must still kick in -- never crash.
        from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

        bt = EnsembleBacktester.__new__(EnsembleBacktester)
        bt.bt = BacktestConfig(initial_capital=100000.0)
        curve = [100000.0, 100100.0, 99900.0, 100200.0]
        result = bt._build_result(
            trades=[],
            equity_curve=curve,
            gate_stats=type("G", (), {"as_dict": lambda self: {}})(),
            daily_equities=None,
        )
        # No exception, sharpe computed (value not asserted -- this
        # branch is the documented legacy fallback).
        assert isinstance(result.sharpe, float)
