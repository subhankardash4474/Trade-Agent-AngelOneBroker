"""
Unit tests for the Portfolio module.
Tests position management, P&L calculation, and performance metrics.
"""

import os
import tempfile

import pytest

from core.portfolio import Portfolio


@pytest.fixture
def portfolio(tmp_path):
    return Portfolio(initial_balance=10000.0, commission_pct=0.03, log_dir=str(tmp_path))


class TestPositionManagement:
    def test_open_position(self, portfolio):
        result = portfolio.open_position("RELIANCE", "BUY", 2500.0, 2, strategy="ma_cross")
        assert result is True
        assert "RELIANCE" in portfolio.positions
        assert portfolio.positions["RELIANCE"].quantity == 2

    def test_duplicate_position_rejected(self, portfolio):
        portfolio.open_position("RELIANCE", "BUY", 2500.0, 2)
        result = portfolio.open_position("RELIANCE", "BUY", 2510.0, 1)
        assert result is False

    def test_insufficient_cash(self, portfolio):
        result = portfolio.open_position("RELIANCE", "BUY", 6000.0, 2)
        assert result is False  # 12000 > 10000

    def test_close_position(self, portfolio):
        portfolio.open_position("TCS", "BUY", 3500.0, 2, strategy="rsi")
        record = portfolio.close_position("TCS", 3600.0, exit_reason="signal")
        assert record is not None
        assert record.pnl > 0
        assert "TCS" not in portfolio.positions

    def test_close_nonexistent_position(self, portfolio):
        record = portfolio.close_position("FAKE", 100.0)
        assert record is None


class TestPnLCalculation:
    def test_profitable_trade(self, portfolio):
        portfolio.open_position("INFY", "BUY", 1500.0, 5)
        record = portfolio.close_position("INFY", 1550.0)
        # PnL = (1550 - 1500) * 5 - commissions
        assert record.pnl > 0

    def test_losing_trade(self, portfolio):
        portfolio.open_position("SBIN", "BUY", 600.0, 10)
        record = portfolio.close_position("SBIN", 580.0)
        assert record.pnl < 0

    def test_unrealized_pnl(self, portfolio):
        portfolio.open_position("HDFCBANK", "BUY", 1600.0, 3)
        pnl = portfolio.get_unrealized_pnl({"HDFCBANK": 1650.0})
        assert pnl == (1650.0 - 1600.0) * 3

    def test_total_value(self, portfolio):
        portfolio.open_position("TCS", "BUY", 3500.0, 1)
        value = portfolio.get_total_value({"TCS": 3600.0})
        # cash (after buying) + current position value
        expected_cash = 10000.0 - 3500.0 - (3500.0 * 0.0003)
        assert abs(value - (expected_cash + 3600.0)) < 1


class TestPerformanceMetrics:
    def test_empty_metrics(self, portfolio):
        metrics = portfolio.get_performance_metrics()
        assert metrics["total_trades"] == 0
        assert metrics["win_rate"] == 0.0

    def test_metrics_after_trades(self, portfolio):
        portfolio.open_position("A", "BUY", 100.0, 10)
        portfolio.close_position("A", 110.0)
        portfolio.open_position("B", "BUY", 200.0, 5)
        portfolio.close_position("B", 190.0)

        metrics = portfolio.get_performance_metrics()
        assert metrics["total_trades"] == 2
        assert metrics["winning_trades"] == 1
        assert metrics["losing_trades"] == 1
        assert metrics["win_rate"] == 50.0

    def test_trade_logged_to_csv(self, portfolio):
        portfolio.open_position("TCS", "BUY", 3500.0, 1)
        portfolio.close_position("TCS", 3550.0)
        assert os.path.exists(portfolio._trade_log_path)
        with open(portfolio._trade_log_path) as f:
            lines = f.readlines()
        assert len(lines) == 2  # header + 1 trade


class TestSummary:
    def test_summary_keys(self, portfolio):
        summary = portfolio.get_summary()
        assert "cash" in summary
        assert "total_value" in summary
        assert "metrics" in summary
        assert "realized_pnl" in summary


class TestSimulatedTimestamps:
    """Regression tests for the 2026-05-17 backtest-correctness fix.

    Before the fix, open_position/close_position unconditionally used
    `datetime.now(IST)` for entry_time and exit_time. The backtester
    therefore recorded wall-clock entry/exit times and computed
    holding_minutes against real elapsed seconds — not against the
    simulated bar timestamps it was iterating over. These tests pin the
    contract: explicit `entry_time` / `exit_time` win over wall-clock,
    naive datetimes get IST-localized, and the live path (no argument
    passed) still falls back to wall-clock.
    """

    def test_explicit_entry_time_overrides_wallclock(self, portfolio):
        import pytz
        from datetime import datetime as dt

        ist = pytz.timezone("Asia/Kolkata")
        bar_ts = ist.localize(dt(2026, 3, 18, 10, 15))
        portfolio.open_position(
            "RELIANCE", "BUY", 2500.0, 1, entry_time=bar_ts,
        )
        # Position's entry_time must be the bar timestamp, not the
        # current wall-clock value (the test obviously isn't running
        # at March 18, 2026, 10:15 IST).
        assert portfolio.positions["RELIANCE"].entry_time == bar_ts

    def test_explicit_exit_time_produces_simulated_holding_minutes(self, portfolio):
        import pytz
        from datetime import datetime as dt

        ist = pytz.timezone("Asia/Kolkata")
        entry_ts = ist.localize(dt(2026, 3, 18, 10, 00))
        exit_ts = ist.localize(dt(2026, 3, 18, 10, 45))  # +45 simulated minutes

        portfolio.open_position(
            "TCS", "BUY", 3500.0, 1, entry_time=entry_ts,
        )
        record = portfolio.close_position("TCS", 3520.0, exit_time=exit_ts)

        assert record is not None
        # 45 simulated minutes — NOT the real wall-clock fraction-of-a-second
        # this test takes to run.
        assert 44.5 <= record.holding_minutes <= 45.5

    def test_naive_entry_time_is_ist_localized(self, portfolio):
        from datetime import datetime as dt

        naive_bar = dt(2026, 3, 18, 10, 15)  # no tzinfo
        portfolio.open_position(
            "INFY", "BUY", 1500.0, 1, entry_time=naive_bar,
        )
        et = portfolio.positions["INFY"].entry_time
        # Without IST localization we'd see a naive datetime; assert tz-aware.
        assert et.tzinfo is not None
        # And same wall-time digits.
        assert (et.year, et.month, et.day, et.hour, et.minute) == (
            2026, 3, 18, 10, 15,
        )

    def test_no_entry_time_falls_back_to_wallclock(self, portfolio):
        """Live path must remain unchanged: when callers don't pass
        entry_time we still use datetime.now(IST).
        """
        from datetime import datetime as dt
        import pytz

        ist = pytz.timezone("Asia/Kolkata")
        before = dt.now(ist)
        portfolio.open_position("SBIN", "BUY", 600.0, 5)
        after = dt.now(ist)

        et = portfolio.positions["SBIN"].entry_time
        assert before <= et <= after
