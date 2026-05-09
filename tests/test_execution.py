"""
Unit tests for the ExecutionEngine module.
Tests paper trading mode order placement, validation, and cancellation.
"""

import pytest

from core.execution import ExecutionEngine


@pytest.fixture
def config():
    return {
        "broker": {"mode": "paper"},
        "execution": {
            "order_type": "LIMIT",
            "product_type": "INTRADAY",
            "retry_attempts": 3,
            "retry_delay_seconds": 0,
            "slippage_tolerance_pct": 0.1,
        },
        "market": {"exchange": "NSE"},
    }


@pytest.fixture
def engine(config):
    return ExecutionEngine(config)


class TestPaperOrders:
    def test_buy_order(self, engine):
        result = engine.place_order(
            symbol="RELIANCE", token="2885",
            transaction_type="BUY", quantity=10, price=2500.0,
        )
        assert result is not None
        assert result["status"] == "FILLED"
        assert result["mode"] == "paper"
        assert result["quantity"] == 10
        assert result["filled_price"] >= 2500.0  # slippage may increase fill price
        assert "PAPER-" in result["order_id"]

    def test_sell_order(self, engine):
        result = engine.place_order(
            symbol="TCS", token="11536",
            transaction_type="SELL", quantity=5, price=3800.0,
        )
        assert result is not None
        assert result["status"] == "FILLED"
        assert result["transaction_type"] == "SELL"

    def test_invalid_transaction_type(self, engine):
        result = engine.place_order(
            symbol="INFY", token="1594",
            transaction_type="INVALID", quantity=1, price=1500.0,
        )
        assert result is None

    def test_zero_quantity(self, engine):
        result = engine.place_order(
            symbol="INFY", token="1594",
            transaction_type="BUY", quantity=0, price=1500.0,
        )
        assert result is None

    def test_order_history(self, engine):
        engine.place_order("SBIN", "3045", "BUY", 20, 600.0)
        engine.place_order("SBIN", "3045", "SELL", 20, 610.0)
        assert len(engine.order_history) == 2

    def test_cancel_paper_order(self, engine):
        result = engine.place_order("RELIANCE", "2885", "BUY", 5, 2500.0)
        assert engine.cancel_order(result["order_id"]) is True

    def test_is_paper_mode(self, engine):
        assert engine.is_paper_mode is True


class TestLiveModeWithoutAPI:
    def test_live_mode_no_api_returns_none(self, config):
        config["broker"]["mode"] = "live"
        engine = ExecutionEngine(config, smart_api=None)
        result = engine.place_order("RELIANCE", "2885", "BUY", 5, 2500.0)
        assert result is None
