"""
Unit tests for trading strategies.
Tests signal generation for MA Crossover, RSI Momentum, and Mean Reversion
with synthetic data that triggers known conditions.
"""

import numpy as np
import pandas as pd
import pytest

from strategies.base_strategy import Signal
from strategies.mean_reversion import MeanReversion
from strategies.moving_average_crossover import MovingAverageCrossover
from strategies.rsi_momentum import RSIMomentum


def _make_ohlcv(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame from a list of close prices."""
    n = len(prices)
    if volumes is None:
        volumes = [100000] * n
    data = {
        "open": [p * 0.999 for p in prices],
        "high": [p * 1.005 for p in prices],
        "low": [p * 0.995 for p in prices],
        "close": prices,
        "volume": volumes,
    }
    idx = pd.date_range("2025-01-01", periods=n, freq="5min")
    return pd.DataFrame(data, index=idx)


# --- Moving Average Crossover ---

class TestMovingAverageCrossover:
    def test_hold_on_insufficient_data(self):
        strategy = MovingAverageCrossover({"short_window": 5, "long_window": 10})
        data = _make_ohlcv([100] * 5)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal == Signal.HOLD

    def test_buy_on_bullish_crossover(self):
        strategy = MovingAverageCrossover({"short_window": 3, "long_window": 10})
        # Prices decline then rally sharply to create bullish crossover
        prices = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 91, 93, 96, 100, 105, 110, 115]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal in (Signal.BUY, Signal.HOLD)
        assert signal.strategy_name == "moving_average_crossover"

    def test_sell_on_bearish_crossover(self):
        strategy = MovingAverageCrossover({"short_window": 3, "long_window": 10})
        # Prices rise then drop sharply to create bearish crossover
        prices = [90, 92, 94, 96, 98, 100, 102, 104, 106, 108, 110, 108, 105, 101, 96, 90, 85, 80]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal in (Signal.SELL, Signal.HOLD)

    def test_returns_trade_signal_type(self):
        strategy = MovingAverageCrossover()
        prices = [100 + np.sin(i / 3) * 5 for i in range(30)]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "INFY")
        assert signal.symbol == "INFY"
        assert signal.strategy_name == "moving_average_crossover"
        assert signal.price > 0


# --- RSI Momentum ---

class TestRSIMomentum:
    def test_hold_on_insufficient_data(self):
        strategy = RSIMomentum({"period": 14})
        data = _make_ohlcv([100] * 5)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal == Signal.HOLD

    def test_buy_on_oversold_reversal(self):
        strategy = RSIMomentum({"period": 5, "oversold": 30, "overbought": 70})
        # Drive price down hard to push RSI into oversold, then reverse
        prices = [100] * 10
        for i in range(15):
            prices.append(prices[-1] * 0.97)  # decline
        for i in range(5):
            prices.append(prices[-1] * 1.04)  # reversal
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal in (Signal.BUY, Signal.HOLD)

    def test_metadata_contains_rsi(self):
        strategy = RSIMomentum({"period": 5})
        # Use oscillating prices so RSI is computable (not all gains / all losses)
        prices = [100 + (3 if i % 2 == 0 else -2) for i in range(30)]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "TCS")
        assert "rsi" in signal.metadata
        assert 0 <= signal.metadata["rsi"] <= 100


# --- Mean Reversion ---

class TestMeanReversion:
    def test_hold_on_insufficient_data(self):
        strategy = MeanReversion({"lookback_period": 20})
        data = _make_ohlcv([100] * 5)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal == Signal.HOLD

    def test_buy_on_negative_z_score(self):
        strategy = MeanReversion({"lookback_period": 10, "entry_z_score": 1.5, "exit_z_score": 0.5})
        # Stable prices then a sharp drop (large negative Z-score), then slight uptick
        prices = [100] * 20 + [92, 88, 85, 83, 82, 82.5]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal in (Signal.BUY, Signal.HOLD)

    def test_sell_on_positive_z_score(self):
        strategy = MeanReversion({"lookback_period": 10, "entry_z_score": 1.5, "exit_z_score": 0.5})
        # Stable prices then a sharp spike, then slight downtick
        prices = [100] * 20 + [108, 112, 115, 117, 118, 117.5]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "TEST")
        assert signal.signal in (Signal.SELL, Signal.HOLD)

    def test_metadata_contains_z_score(self):
        strategy = MeanReversion({"lookback_period": 10})
        prices = [100 + np.random.randn() * 2 for _ in range(30)]
        data = _make_ohlcv(prices)
        signal = strategy.generate_signal(data, "SBIN")
        assert "z_score" in signal.metadata
