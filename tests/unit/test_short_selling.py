"""Short-selling support tests.

Covers:
  1. Portfolio correctly models SHORT entry/exit (cash, commissions, P&L sign).
  2. Round-trip cash conservation: final cash = initial + net_pnl.
  3. Unrealized P&L flips sign correctly.
  4. `get_total_value` treats short collateral + unrealized PnL as equity.
  5. Trading-agent routing: SELL w/o position opens SHORT only when
     enabled AND regime allows.
  6. Trading-agent routing: BUY while short triggers a cover via
     `_exit_on_signal`, not a duplicate-block.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.portfolio import Portfolio


# ─────────────────────────────────────────────────────────────
# Portfolio — SHORT position mechanics
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def portfolio(tmp_path):
    # Seeded large enough that a 10-share short at ₹2500 (₹25k notional)
    # fits in the "notional as collateral" accounting model.
    return Portfolio(
        initial_balance=100_000.0, commission_pct=0.03,
        log_dir=str(tmp_path), product_type="INTRADAY",
    )


class TestShortOpenClose:
    def test_open_short_position_succeeds(self, portfolio):
        ok = portfolio.open_position("RELIANCE", "SELL", 2500.0, 2, strategy="rsi")
        assert ok is True
        pos = portfolio.positions["RELIANCE"]
        assert pos.side == "SELL"
        assert pos.quantity == 2

    def test_short_cash_locked_like_long(self, portfolio):
        """Paper-mode accounting locks the notional as collateral so shorts
        can't over-leverage. Cash should drop by ~(price*qty + commission)."""
        initial_cash = portfolio.cash
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 2, strategy="rsi")
        # Cash deducted by ~5000 + small intraday commission
        assert portfolio.cash < initial_cash
        assert portfolio.cash > initial_cash - 5100  # generous upper on commissions

    def test_unrealized_pnl_short_profits_when_price_falls(self, portfolio):
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        pos = portfolio.positions["RELIANCE"]
        assert pos.unrealized_pnl(2450.0) == pytest.approx(500.0)
        assert pos.unrealized_pnl(2550.0) == pytest.approx(-500.0)

    def test_close_short_profitable_cover(self, portfolio):
        """Short 10 @ 2500, cover @ 2450 → +₹500 gross, minus round-trip charges."""
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        record = portfolio.close_position("RELIANCE", 2450.0, exit_reason="signal")
        assert record is not None
        # Gross pnl = 500; realistic charges < 30 → net well above 400
        assert record.pnl > 400
        assert record.pnl < 500
        assert record.side == "SELL"
        assert record.exit_price == 2450.0
        assert "RELIANCE" not in portfolio.positions

    def test_close_short_losing_cover(self, portfolio):
        """Short 10 @ 2500, cover @ 2550 → -₹500 gross, even worse after charges."""
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        record = portfolio.close_position("RELIANCE", 2550.0, exit_reason="stop_loss")
        assert record is not None
        assert record.pnl < -500  # loss + charges

    def test_round_trip_cash_equals_initial_plus_pnl(self, portfolio):
        """Cash-conservation invariant: after a full round-trip, the
        change in cash must equal the recorded realized PnL (within a
        rupee of rounding)."""
        start = portfolio.cash
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        record = portfolio.close_position("RELIANCE", 2450.0)
        end = portfolio.cash
        assert end == pytest.approx(start + record.pnl, abs=1.0)

    def test_round_trip_losing_short_cash_conservation(self, portfolio):
        """Same invariant but for a losing trade — confirms the sign
        of the cash adjustment is correct."""
        start = portfolio.cash
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        record = portfolio.close_position("RELIANCE", 2550.0)
        end = portfolio.cash
        assert end == pytest.approx(start + record.pnl, abs=1.0)

    def test_long_round_trip_still_works(self, portfolio):
        """Regression: our short changes must not have broken long math."""
        start = portfolio.cash
        portfolio.open_position("RELIANCE", "BUY", 2500.0, 2)
        record = portfolio.close_position("RELIANCE", 2600.0)
        end = portfolio.cash
        assert record.pnl == pytest.approx(200 - record.commission, abs=0.5)
        assert end == pytest.approx(start + record.pnl, abs=1.0)


class TestShortPortfolioTotalValue:
    def test_total_value_unchanged_at_entry_price(self, portfolio):
        """Immediately after opening a short (exit price == entry price),
        total equity should be essentially unchanged — only paid the
        entry commission.

        CHG (2026-06-01): the AngelOne calibration raised intraday
        brokerage from 0.03%-no-floor to 0.1% with a ₹5 floor. Entry
        commission on 2×₹2500 notional is now ~₹7-8 (Rs 5 brokerage +
        small STT/stamp/GST), up from <₹1 pre-CHG. Tolerance widened
        accordingly.
        """
        initial = portfolio.get_total_value({})
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 2)
        at_entry = portfolio.get_total_value({"RELIANCE": 2500.0})
        # Down only by entry commission (< Rs 20 on 5000 notional intraday
        # under any sensible Indian-equity broker model).
        assert at_entry == pytest.approx(initial, abs=20.0)

    def test_total_value_rises_on_profitable_short(self, portfolio):
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        base = portfolio.get_total_value({"RELIANCE": 2500.0})
        profit = portfolio.get_total_value({"RELIANCE": 2450.0})
        assert profit - base == pytest.approx(500.0, abs=1.0)

    def test_total_value_falls_on_losing_short(self, portfolio):
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 10)
        base = portfolio.get_total_value({"RELIANCE": 2500.0})
        loss = portfolio.get_total_value({"RELIANCE": 2550.0})
        assert base - loss == pytest.approx(500.0, abs=1.0)


# ─────────────────────────────────────────────────────────────
# TradingAgent routing — SELL with no position
# ─────────────────────────────────────────────────────────────


class TestTradingAgentShortRouting:
    """Guardrails around when the agent is willing to open a SHORT."""

    def _make_agent_stub(self, *, shorts_enabled: bool, allowed_regimes: set):
        """Minimal TradingAgent that only wires the routing-level state.
        We stub away everything _process_signal could reach so we can
        assert on which sub-method (_open_new_position / _exit_on_signal)
        was called — which is the only thing the routing guarantees."""
        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a._enable_short_selling = shorts_enabled
        a._short_selling_regimes = allowed_regimes
        a._market_context = {"india_vix": 14.0, "nifty_trend": -1}
        # In a fresh/empty portfolio
        a.portfolio = MagicMock()
        a.portfolio.positions = {}
        a.signal_audit = MagicMock()
        # Record what the router decided to do
        a._open_new_position = MagicMock()
        a._exit_on_signal = MagicMock()
        return a

    def _sell_signal(self):
        from strategies.base_strategy import Signal, TradeSignal

        return TradeSignal(
            signal=Signal.SELL, symbol="RELIANCE",
            price=2500.0, timestamp=None,
            strategy_name="rsi_momentum", confidence=0.7,
            stop_loss=2525.0, take_profit=2450.0,
            contributing_strategies={"rsi_momentum": 1.0},
        )

    def _buy_signal(self):
        from strategies.base_strategy import Signal, TradeSignal

        return TradeSignal(
            signal=Signal.BUY, symbol="RELIANCE",
            price=2500.0, timestamp=None,
            strategy_name="rsi_momentum", confidence=0.7,
            stop_loss=2475.0, take_profit=2550.0,
            contributing_strategies={"rsi_momentum": 1.0},
        )

    def test_sell_ignored_when_shorts_disabled(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(shorts_enabled=False, allowed_regimes={"bear_low_vol"})
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        a._open_new_position.assert_not_called()
        # Audit call should record "shorts_disabled"
        calls = a.signal_audit.log.call_args_list
        assert any("shorts_disabled" in str(c) for c in calls)

    def test_sell_ignored_when_regime_disallows_shorts(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(
            shorts_enabled=True,
            allowed_regimes={"bear_low_vol", "bear_high_vol", "sideways"},
        )
        # Force a BULL regime (nifty_trend=1, low vix → bull_low_vol)
        a._market_context = {"india_vix": 14.0, "nifty_trend": 1}
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        a._open_new_position.assert_not_called()
        calls = a.signal_audit.log.call_args_list
        assert any("short_regime" in str(c) for c in calls)

    def test_sell_opens_short_when_enabled_and_regime_allows(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(
            shorts_enabled=True,
            allowed_regimes={"bear_low_vol", "bear_high_vol", "sideways"},
        )
        # VIX=14, nifty_trend=-1 → bear_low_vol ✓
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        a._open_new_position.assert_called_once()
        # Must be called with side="SELL"
        kwargs = a._open_new_position.call_args.kwargs
        assert kwargs.get("side") == "SELL"

    def test_sell_while_long_triggers_exit(self):
        """A SELL signal with an existing LONG should close the long via
        `_exit_on_signal`, not reject as a duplicate."""
        from core.portfolio import Position
        from trading_agent import TradingAgent
        from datetime import datetime

        a = self._make_agent_stub(shorts_enabled=False, allowed_regimes=set())
        a.portfolio.positions = {
            "RELIANCE": Position(
                symbol="RELIANCE", side="BUY", entry_price=2500.0,
                quantity=2, entry_time=datetime.now(),
            )
        }
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        a._exit_on_signal.assert_called_once()
        a._open_new_position.assert_not_called()

    def test_buy_while_short_triggers_cover(self):
        """A BUY signal with an existing SHORT should cover via
        `_exit_on_signal`, not reject as a duplicate."""
        from core.portfolio import Position
        from trading_agent import TradingAgent
        from datetime import datetime

        a = self._make_agent_stub(shorts_enabled=True, allowed_regimes=set())
        a.portfolio.positions = {
            "RELIANCE": Position(
                symbol="RELIANCE", side="SELL", entry_price=2500.0,
                quantity=10, entry_time=datetime.now(),
            )
        }
        TradingAgent._process_signal(a, self._buy_signal(), "1234", 2500.0)
        a._exit_on_signal.assert_called_once()
        a._open_new_position.assert_not_called()

    def test_sell_while_short_rejected_as_duplicate(self):
        from core.portfolio import Position
        from trading_agent import TradingAgent
        from datetime import datetime

        a = self._make_agent_stub(shorts_enabled=True, allowed_regimes={"bear_low_vol"})
        a.portfolio.positions = {
            "RELIANCE": Position(
                symbol="RELIANCE", side="SELL", entry_price=2500.0,
                quantity=10, entry_time=datetime.now(),
            )
        }
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        a._open_new_position.assert_not_called()
        a._exit_on_signal.assert_not_called()
        calls = a.signal_audit.log.call_args_list
        assert any("already_open:duplicate_short" in str(c) for c in calls)


# ─────────────────────────────────────────────────────────────
# Risk-policy short veto: `risk.allow_shorts` (added 2026-05-25)
# ─────────────────────────────────────────────────────────────


class TestRiskAllowShortsGate:
    """The new `risk.allow_shorts` flag is a higher-level kill switch
    on the SHORT side that fires BEFORE the existing capability/regime
    gates. These tests pin the four behaviours that matter:

      1. allow_shorts=True -> behaves identically to before (no regression).
      2. allow_shorts=False + flat -> SELL signal blocked with a distinct
         "allow_shorts:false" audit reason (not "shorts_disabled").
      3. allow_shorts=False does NOT block exits of existing longs (a
         SELL signal while long must still close the long).
      4. allow_shorts=False does NOT block covers of existing shorts (a
         BUY signal while short must still cover the short).
    """

    def _make_agent_stub(self, *, allow_shorts: bool, shorts_enabled: bool = True,
                         allowed_regimes=None):
        """Same shape as TestTradingAgentShortRouting._make_agent_stub but
        also sets the new `_allow_shorts` attribute. Keep the two stubs
        independent so existing tests don't see the new attribute and
        the new tests don't depend on the old fixture."""
        from unittest.mock import MagicMock

        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        a._allow_shorts = allow_shorts
        a._enable_short_selling = shorts_enabled
        a._short_selling_regimes = allowed_regimes or {
            "bear_low_vol", "bear_high_vol", "sideways",
        }
        a._market_context = {"india_vix": 14.0, "nifty_trend": -1}
        a.portfolio = MagicMock()
        a.portfolio.positions = {}
        a.signal_audit = MagicMock()
        a._open_new_position = MagicMock()
        a._exit_on_signal = MagicMock()
        return a

    def _sell_signal(self):
        from strategies.base_strategy import Signal, TradeSignal

        return TradeSignal(
            signal=Signal.SELL, symbol="RELIANCE",
            price=2500.0, timestamp=None,
            strategy_name="rsi_momentum", confidence=0.7,
            stop_loss=2525.0, take_profit=2450.0,
            contributing_strategies={"rsi_momentum": 1.0},
        )

    def _buy_signal(self):
        from strategies.base_strategy import Signal, TradeSignal

        return TradeSignal(
            signal=Signal.BUY, symbol="RELIANCE",
            price=2500.0, timestamp=None,
            strategy_name="rsi_momentum", confidence=0.7,
            stop_loss=2475.0, take_profit=2550.0,
            contributing_strategies={"rsi_momentum": 1.0},
        )

    # 1. Default behaviour preserved when flag is true.
    def test_allow_shorts_true_passes_through(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(allow_shorts=True, shorts_enabled=True)
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        # Should reach _open_new_position with side=SELL, just like the
        # legacy flow.
        a._open_new_position.assert_called_once()
        kwargs = a._open_new_position.call_args.kwargs
        assert kwargs.get("side") == "SELL"
        # Audit must NOT log allow_shorts as a rejection reason.
        calls = a.signal_audit.log.call_args_list
        assert not any("allow_shorts" in str(c) for c in calls)

    # 2. Flag false + flat -> blocked with the right audit reason.
    def test_allow_shorts_false_blocks_new_short(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(allow_shorts=False, shorts_enabled=True)
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        a._open_new_position.assert_not_called()
        a._exit_on_signal.assert_not_called()
        calls = a.signal_audit.log.call_args_list
        assert any("allow_shorts:false" in str(c) for c in calls), \
            f"Expected 'allow_shorts:false' in audit calls, got: {calls}"

    # 2b. Reason is distinct from the older "shorts_disabled".
    def test_allow_shorts_reason_distinct_from_shorts_disabled(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(allow_shorts=False, shorts_enabled=True)
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        calls = a.signal_audit.log.call_args_list
        assert any("allow_shorts:false" in str(c) for c in calls)
        # "shorts_disabled" is the OTHER flag's reason; with
        # shorts_enabled=True it must not have fired.
        assert not any("shorts_disabled" in str(c) for c in calls)

    # 3. Existing long is closed normally even when allow_shorts=False.
    def test_allow_shorts_false_still_exits_long_on_sell(self):
        from datetime import datetime

        from core.portfolio import Position
        from trading_agent import TradingAgent

        a = self._make_agent_stub(allow_shorts=False)
        a.portfolio.positions = {
            "RELIANCE": Position(
                symbol="RELIANCE", side="BUY", entry_price=2500.0,
                quantity=2, entry_time=datetime.now(),
            )
        }
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        # Long-exit path must still fire; gate is scoped to NEW shorts.
        a._exit_on_signal.assert_called_once()
        a._open_new_position.assert_not_called()
        calls = a.signal_audit.log.call_args_list
        # We did NOT block this -- it's a legitimate exit, not an entry.
        assert not any("allow_shorts" in str(c) for c in calls)

    # 4. Existing short is covered normally even when allow_shorts=False.
    def test_allow_shorts_false_still_covers_short_on_buy(self):
        from datetime import datetime

        from core.portfolio import Position
        from trading_agent import TradingAgent

        a = self._make_agent_stub(allow_shorts=False)
        a.portfolio.positions = {
            "RELIANCE": Position(
                symbol="RELIANCE", side="SELL", entry_price=2500.0,
                quantity=10, entry_time=datetime.now(),
            )
        }
        TradingAgent._process_signal(a, self._buy_signal(), "1234", 2500.0)
        a._exit_on_signal.assert_called_once()
        a._open_new_position.assert_not_called()

    # 5. allow_shorts=False fires BEFORE the regime gate -- audit reason
    #    is "allow_shorts:false", not "short_regime:...".
    def test_allow_shorts_fires_before_regime_check(self):
        from trading_agent import TradingAgent

        a = self._make_agent_stub(
            allow_shorts=False, shorts_enabled=True,
            allowed_regimes={"bear_low_vol"},  # would normally allow
        )
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        calls = a.signal_audit.log.call_args_list
        # Exact string check: allow_shorts must short-circuit the gate.
        assert any("allow_shorts:false" in str(c) for c in calls)
        assert not any("short_regime" in str(c) for c in calls)

    # 6. The flag is a defensive `getattr` lookup so test fixtures and
    #    legacy stubs that bypass __init__ (no `_allow_shorts` set)
    #    behave as if the flag were True. This protects every existing
    #    test in TestTradingAgentShortRouting from regressing.
    def test_missing_attribute_defaults_to_allow(self):
        from unittest.mock import MagicMock

        from trading_agent import TradingAgent

        a = object.__new__(TradingAgent)
        # NB: NOT setting _allow_shorts deliberately.
        a._enable_short_selling = True
        a._short_selling_regimes = {"bear_low_vol"}
        a._market_context = {"india_vix": 14.0, "nifty_trend": -1}
        a.portfolio = MagicMock()
        a.portfolio.positions = {}
        a.signal_audit = MagicMock()
        a._open_new_position = MagicMock()
        a._exit_on_signal = MagicMock()
        TradingAgent._process_signal(a, self._sell_signal(), "1234", 2500.0)
        # Should reach _open_new_position despite missing attribute.
        a._open_new_position.assert_called_once()


# ─────────────────────────────────────────────────────────────
# Backtester gate: `BacktestConfig.allow_shorts` (added 2026-05-25)
# ─────────────────────────────────────────────────────────────


class TestBacktestAllowShortsGate:
    """The backtester mirrors the live gate so battery variants can
    test the long-only configuration. Test the dataclass plumbing and
    the gate-stat increment."""

    def test_default_is_true(self):
        from research.backtest_ensemble import BacktestConfig

        cfg = BacktestConfig()
        assert cfg.allow_shorts is True

    def test_gate_stats_includes_shorts_blocked(self):
        from research.backtest_ensemble import GateStats

        gs = GateStats()
        assert gs.shorts_blocked == 0
        gs.shorts_blocked = 5
        assert gs.as_dict()["shorts_blocked"] == 5

    def test_battery_bt_config_propagates_allow_shorts(self):
        """Battery's `_bt_config` must read `risk.allow_shorts` from the
        merged YAML. Default True; explicit False propagates."""
        from research.battery import _bt_config

        cfg_default = {"risk": {}, "execution": {}, "backtest": {}}
        assert _bt_config(cfg_default).allow_shorts is True

        cfg_long_only = {
            "risk": {"allow_shorts": False},
            "execution": {}, "backtest": {},
        }
        assert _bt_config(cfg_long_only).allow_shorts is False
