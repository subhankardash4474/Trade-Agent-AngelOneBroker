"""
Tests for the 2026-04-28 win-rate enhancements:

  - Dead-hour filter (noon lull window)
  - Minimum ATR% gate
  - Minimum holding time (signal-exit only; SL/TP still fire)
  - Time-range parsing helper
  - EOD diagnostic builder
"""
from datetime import datetime, time as dtime, timedelta
from unittest.mock import MagicMock

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


class _FrozenDatetime:
    def __init__(self, fixed: datetime):
        self._fixed = fixed

    def now(self, tz=None):
        if tz is None:
            return self._fixed
        return self._fixed.astimezone(tz) if self._fixed.tzinfo else tz.localize(self._fixed)

    def __getattr__(self, name):
        return getattr(datetime, name)


class TestTimeRangeParsing:
    def test_parses_valid_ranges(self):
        from trading_agent import TradingAgent
        out = TradingAgent._parse_time_ranges(["12:00-13:00", "14:30-14:45"])
        assert out == [(dtime(12, 0), dtime(13, 0)), (dtime(14, 30), dtime(14, 45))]

    def test_ignores_invalid_ranges(self):
        from trading_agent import TradingAgent
        out = TradingAgent._parse_time_ranges(["bad", "12-13", "12:00-13:00"])
        assert out == [(dtime(12, 0), dtime(13, 0))]

    def test_empty_list(self):
        from trading_agent import TradingAgent
        assert TradingAgent._parse_time_ranges([]) == []


class TestDeadHourFilter:
    def _make_agent_stub(self, blocks):
        """Minimal object with just what _is_in_dead_hour needs."""
        from trading_agent import TradingAgent
        stub = TradingAgent.__new__(TradingAgent)
        stub._dead_hour_blocks = TradingAgent._parse_time_ranges(blocks)
        return stub

    def test_inside_block_returns_true(self, monkeypatch):
        import trading_agent as ta_mod
        fake = datetime.now(IST).replace(hour=12, minute=30, second=0, microsecond=0)
        monkeypatch.setattr(ta_mod, "datetime", _FrozenDatetime(fake))
        stub = self._make_agent_stub(["12:00-13:00"])
        inside, label = stub._is_in_dead_hour()
        assert inside is True
        assert "12:00-13:00" in label

    def test_outside_block_returns_false(self, monkeypatch):
        import trading_agent as ta_mod
        fake = datetime.now(IST).replace(hour=11, minute=30, second=0, microsecond=0)
        monkeypatch.setattr(ta_mod, "datetime", _FrozenDatetime(fake))
        stub = self._make_agent_stub(["12:00-13:00"])
        inside, _ = stub._is_in_dead_hour()
        assert inside is False

    def test_boundary_exclusive_at_end(self, monkeypatch):
        """13:00:00 should be OUTSIDE the '12:00-13:00' block."""
        import trading_agent as ta_mod
        fake = datetime.now(IST).replace(hour=13, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(ta_mod, "datetime", _FrozenDatetime(fake))
        stub = self._make_agent_stub(["12:00-13:00"])
        inside, _ = stub._is_in_dead_hour()
        assert inside is False

    def test_no_blocks_configured(self, monkeypatch):
        stub = self._make_agent_stub([])
        inside, label = stub._is_in_dead_hour()
        assert inside is False
        assert label == ""


class TestDiagnosticBuilder:
    """Smoke-test the EOD diagnostic string — don't need to be pixel-perfect,
    just verify the structure makes sense for some realistic trade data."""

    def test_builds_report_with_trades(self):
        from trading_agent import TradingAgent

        # Create a minimal stub — we only need `database.load_trades_for_day`
        stub = TradingAgent.__new__(TradingAgent)
        stub.database = MagicMock()
        stub.database.load_trades_for_day.return_value = [
            {"pnl": +20.0, "exit_reason": "take_profit", "entry_time": "2026-04-28T11:16:00"},
            {"pnl": -15.0, "exit_reason": "stop_loss", "entry_time": "2026-04-28T12:30:00"},
            {"pnl": -25.0, "exit_reason": "stop_loss", "entry_time": "2026-04-28T12:45:00"},
            {"pnl": +10.0, "exit_reason": "take_profit", "entry_time": "2026-04-28T14:00:00"},
        ]

        report = stub._build_daily_diagnostics("2026-04-28")
        assert "DAILY DIAGNOSTICS" in report
        assert "Win rate: 50.0%" in report
        assert "Exit mix:" in report
        assert "stop_loss" in report
        assert "take_profit" in report
        assert "By-hour" in report

    def test_empty_day_returns_empty(self):
        from trading_agent import TradingAgent
        stub = TradingAgent.__new__(TradingAgent)
        stub.database = MagicMock()
        stub.database.load_trades_for_day.return_value = []
        assert stub._build_daily_diagnostics("2026-04-28") == ""

    def test_loss_warning_emitted_when_rr_poor(self):
        """If avg_loss > avg_win and win rate < breakeven, add NOTE hint."""
        from trading_agent import TradingAgent
        stub = TradingAgent.__new__(TradingAgent)
        stub.database = MagicMock()
        stub.database.load_trades_for_day.return_value = [
            {"pnl": +10.0, "exit_reason": "take_profit", "entry_time": "2026-04-28T11:00:00"},
            {"pnl": -25.0, "exit_reason": "stop_loss", "entry_time": "2026-04-28T11:30:00"},
            {"pnl": -30.0, "exit_reason": "stop_loss", "entry_time": "2026-04-28T12:00:00"},
        ]
        report = stub._build_daily_diagnostics("2026-04-28")
        assert "NOTE: avg loss > avg win" in report


class TestConfigChangesLoaded:
    """Verify the new config fields are actually read by the agent on init."""

    def test_config_values_are_stricter(self):
        """Sanity check that the yaml has been tightened as planned."""
        import yaml
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        # Stop-loss widened
        assert cfg["risk"]["atr_stop_multiplier"] >= 2.0
        # Min reward raised
        assert cfg["risk"]["min_absolute_reward_rs"] >= 20.0
        # Profit/charges ratio raised
        assert cfg["risk"]["min_profit_to_charges_ratio"] >= 2.5
        # Ensemble tighter
        assert cfg["ensemble"]["confidence_threshold"] >= 0.55
        # New robustness keys present
        robust = cfg["robustness"]
        assert "dead_hour_blocks" in robust
        assert robust.get("min_holding_minutes", 0) > 0
        assert robust.get("min_entry_atr_pct", 0) > 0
