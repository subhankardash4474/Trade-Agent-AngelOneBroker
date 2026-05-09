"""
Unit tests for the FeatureEngine module.
Validates that all feature categories are computed correctly.
"""

import numpy as np
import pandas as pd
import pytest

from core.features import FeatureEngine


def _make_ohlcv(n: int = 100) -> pd.DataFrame:
    np.random.seed(42)
    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    data = {
        "open": close * (1 + np.random.randn(n) * 0.001),
        "high": close * (1 + abs(np.random.randn(n) * 0.005)),
        "low": close * (1 - abs(np.random.randn(n) * 0.005)),
        "close": close,
        "volume": np.random.randint(10000, 100000, n).astype(float),
    }
    idx = pd.date_range("2025-01-01", periods=n, freq="5min")
    return pd.DataFrame(data, index=idx)


@pytest.fixture
def engine():
    return FeatureEngine()


@pytest.fixture
def data():
    return _make_ohlcv(100)


class TestTrendFeatures:
    def test_emas_computed(self, engine, data):
        result = engine.compute_all(data)
        assert "ema_9" in result.columns
        assert "ema_21" in result.columns
        assert "ema_50" in result.columns
        assert not result["ema_9"].iloc[-1:].isna().all()

    def test_macd_computed(self, engine, data):
        result = engine.compute_all(data)
        assert "macd" in result.columns
        assert "macd_signal" in result.columns
        assert "macd_histogram" in result.columns

    def test_adx_computed(self, engine, data):
        result = engine.compute_all(data)
        assert "adx" in result.columns
        assert not result["adx"].iloc[-1:].isna().all()

    def test_supertrend_computed(self, engine, data):
        result = engine.compute_all(data)
        assert "supertrend" in result.columns
        assert "supertrend_direction" in result.columns


class TestMomentumFeatures:
    def test_rsi(self, engine, data):
        result = engine.compute_all(data)
        rsi = result["rsi"].dropna()
        assert len(rsi) > 0
        assert rsi.min() >= 0
        assert rsi.max() <= 100

    def test_stochastic(self, engine, data):
        result = engine.compute_all(data)
        assert "stoch_k" in result.columns
        assert "stoch_d" in result.columns

    def test_williams_r(self, engine, data):
        result = engine.compute_all(data)
        assert "williams_r" in result.columns

    def test_roc(self, engine, data):
        result = engine.compute_all(data)
        assert "roc" in result.columns


class TestVolatilityFeatures:
    def test_atr(self, engine, data):
        result = engine.compute_all(data)
        assert "atr" in result.columns
        assert result["atr"].dropna().iloc[-1] > 0

    def test_bollinger_bands(self, engine, data):
        result = engine.compute_all(data)
        assert "bb_upper" in result.columns
        assert "bb_lower" in result.columns
        assert "bb_width" in result.columns

    def test_keltner_channels(self, engine, data):
        result = engine.compute_all(data)
        assert "kc_upper" in result.columns
        assert "kc_lower" in result.columns


class TestVolumeFeatures:
    def test_vwap(self, engine, data):
        result = engine.compute_all(data)
        assert "vwap" in result.columns

    def test_obv(self, engine, data):
        result = engine.compute_all(data)
        assert "obv" in result.columns

    def test_volume_ratio(self, engine, data):
        result = engine.compute_all(data)
        assert "volume_ratio" in result.columns


class TestPriceAction:
    def test_candle_patterns(self, engine, data):
        result = engine.compute_all(data)
        assert "is_doji" in result.columns
        assert "is_hammer" in result.columns
        assert "is_bullish_engulfing" in result.columns

    def test_pivot_levels(self, engine, data):
        result = engine.compute_all(data)
        assert "pivot" in result.columns
        assert "support_1" in result.columns
        assert "resistance_1" in result.columns


class TestMLFeatures:
    def test_ml_columns_subset(self, engine):
        ml_cols = engine.get_ml_feature_columns()
        all_cols = engine.get_feature_columns()
        for col in ml_cols:
            assert col in all_cols

    def test_all_ml_features_numeric(self, engine, data):
        result = engine.compute_all(data)
        ml_cols = [c for c in engine.get_ml_feature_columns() if c in result.columns]
        for col in ml_cols:
            assert result[col].dtype in (np.float64, np.int64, np.float32, np.int32), f"{col} is {result[col].dtype}"
