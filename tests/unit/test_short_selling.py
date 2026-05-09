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
        entry commission."""
        initial = portfolio.get_total_value({})
        portfolio.open_position("RELIANCE", "SELL", 2500.0, 2)
        at_entry = portfolio.get_total_value({"RELIANCE": 2500.0})
        # Down only by entry commission (<Rs 1 on 5000 notional intraday)
        assert at_entry == pytest.approx(initial, abs=5.0)

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
