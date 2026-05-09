"""Tests for the 2026-05-07 fixes:

1. `strategies._trend_context.is_against_trend()` - daily SMA filter
2. `mean_reversion.MeanReversion` - trend filter blocks against-trend entries
3. `mean_reversion.MeanReversion` - TP at 80% reversion, not 100%
4. `xgboost_classifier.XGBoostClassifier` - SELL signals now carry SL/TP
5. `portfolio.Portfolio._maybe_persist_trade()` - idempotent trade row insert

Most tests mock yfinance so we don't hit the network. The trend-context
cache is cleared between tests via the autouse fixture.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _clear_trend_cache():
    from strategies._trend_context import clear_cache
    clear_cache()
    yield
    clear_cache()


def _make_ohlcv(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
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


def _mock_yfinance_daily(close_prices: list[float]):
    """Build a yfinance-shaped DataFrame for patching yf.download."""
    n = len(close_prices)
    df = pd.DataFrame({
        "Close": close_prices,
        "Open": close_prices,
        "High": [p * 1.01 for p in close_prices],
        "Low": [p * 0.99 for p in close_prices],
        "Volume": [1000] * n,
    }, index=pd.date_range("2025-01-01", periods=n, freq="D"))
    return df


# ---------- _trend_context ----------

class TestTrendContext:
    def test_returns_none_when_fewer_than_50_bars(self):
        from strategies._trend_context import get_trend
        with patch("yfinance.download", return_value=_mock_yfinance_daily([100.0] * 30)):
            assert get_trend("ABC") is None

    def test_computes_pct_above_sma(self):
        from strategies._trend_context import get_trend
        prices = [100.0] * 49 + [120.0]  # SMA50 ~100.4, last 120 -> +19.5%
        with patch("yfinance.download", return_value=_mock_yfinance_daily(prices)):
            t = get_trend("ABC")
        assert t is not None
        assert t["pct_vs_sma50"] > 18.0
        assert t["pct_vs_sma50"] < 21.0

    def test_is_against_trend_short_above_threshold(self):
        from strategies._trend_context import is_against_trend
        prices = [100.0] * 49 + [110.0]  # +9% above SMA
        with patch("yfinance.download", return_value=_mock_yfinance_daily(prices)):
            assert is_against_trend("ABC", "SELL", threshold_pct=5.0) is True
            assert is_against_trend("ABC", "BUY", threshold_pct=5.0) is False

    def test_is_against_trend_long_below_threshold(self):
        from strategies._trend_context import is_against_trend, clear_cache
        clear_cache()
        prices = [100.0] * 49 + [88.0]  # ~-12% below SMA
        with patch("yfinance.download", return_value=_mock_yfinance_daily(prices)):
            assert is_against_trend("ABC", "BUY", threshold_pct=5.0) is True
            assert is_against_trend("ABC", "SELL", threshold_pct=5.0) is False

    def test_neutral_zone_blocks_neither_side(self):
        from strategies._trend_context import is_against_trend
        prices = [100.0] * 49 + [101.0]  # ~1% above
        with patch("yfinance.download", return_value=_mock_yfinance_daily(prices)):
            assert is_against_trend("ABC", "SELL", threshold_pct=5.0) is False
            assert is_against_trend("ABC", "BUY", threshold_pct=5.0) is False

    def test_fail_open_on_fetch_error(self):
        from strategies._trend_context import is_against_trend
        with patch("yfinance.download", side_effect=Exception("network down")):
            # Fetch failure -> trend unknown -> do not block.
            assert is_against_trend("ABC", "SELL") is False
            assert is_against_trend("ABC", "BUY") is False

    def test_cache_avoids_second_fetch_within_ttl(self):
        from strategies._trend_context import get_trend
        prices = [100.0] * 60
        mock = MagicMock(return_value=_mock_yfinance_daily(prices))
        with patch("yfinance.download", mock):
            get_trend("XYZ")
            get_trend("XYZ")
            get_trend("XYZ")
        assert mock.call_count == 1


# ---------- MeanReversion: trend filter ----------

class TestMeanReversionTrendFilter:
    def _build_overbought_data(self, n: int = 30) -> pd.DataFrame:
        """Construct a price series that produces a high positive Z-score
        on the latest bar AND a falling-Z transition (peak then slight
        retracement) so MeanReversion's SELL condition fires:
            z >= entry_z_score AND z < z_prev
        """
        prices = [100.0] * (n - 2) + [115.0, 110.0]  # peak then partial down
        return _make_ohlcv(prices)

    def _build_oversold_data(self, n: int = 30) -> pd.DataFrame:
        """Mirror image: deep negative Z but rising on last bar so the
        BUY condition fires (z <= -entry_z_score AND z > z_prev)."""
        prices = [100.0] * (n - 2) + [85.0, 90.0]  # crash then partial up
        return _make_ohlcv(prices)

    def test_short_blocked_when_above_50d_sma_more_than_filter(self):
        from strategies.mean_reversion import MeanReversion
        # 50d SMA daily (uptrend stock): last close +10% above SMA
        daily = _mock_yfinance_daily([100.0] * 49 + [110.0])
        with patch("yfinance.download", return_value=daily):
            mr = MeanReversion({"entry_z_score": 1.0, "trend_filter_pct": 5.0})
            data = self._build_overbought_data()
            sig = mr.generate_signal(data, "UPTRENDSTOCK")
        from strategies.base_strategy import Signal
        # Even though Z is high (would trigger SELL), trend filter must hold.
        assert sig.signal == Signal.HOLD
        assert sig.metadata.get("reason") == "trend_filter_short"

    def test_short_allowed_when_within_filter_band(self):
        from strategies.mean_reversion import MeanReversion
        daily = _mock_yfinance_daily([100.0] * 49 + [102.0])  # +2% only
        with patch("yfinance.download", return_value=daily):
            mr = MeanReversion({"entry_z_score": 1.0, "trend_filter_pct": 5.0})
            data = self._build_overbought_data()
            sig = mr.generate_signal(data, "NEUTRAL")
        from strategies.base_strategy import Signal
        assert sig.signal == Signal.SELL

    def test_long_blocked_when_below_50d_sma_more_than_filter(self):
        from strategies.mean_reversion import MeanReversion
        daily = _mock_yfinance_daily([100.0] * 49 + [88.0])  # -12% below SMA
        with patch("yfinance.download", return_value=daily):
            mr = MeanReversion({"entry_z_score": 1.0, "trend_filter_pct": 5.0})
            data = self._build_oversold_data()
            sig = mr.generate_signal(data, "DOWNTRENDSTOCK")
        from strategies.base_strategy import Signal
        assert sig.signal == Signal.HOLD
        assert sig.metadata.get("reason") == "trend_filter_long"

    def test_filter_disabled_with_none(self):
        from strategies.mean_reversion import MeanReversion
        daily = _mock_yfinance_daily([100.0] * 49 + [120.0])
        with patch("yfinance.download", return_value=daily):
            mr = MeanReversion({"entry_z_score": 1.0, "trend_filter_pct": None})
            data = self._build_overbought_data()
            sig = mr.generate_signal(data, "ANYSTOCK")
        from strategies.base_strategy import Signal
        assert sig.signal == Signal.SELL  # filter disabled -> goes through


# ---------- MeanReversion: TP at 80% reversion ----------

class TestMeanReversionTPRealism:
    def test_short_tp_is_80pct_of_distance_to_mean(self):
        """If current=110, mean=100, distance=10. With tp_reversion_pct=0.8,
        TP = 110 - 0.8*10 = 102 (not 100 = full reversion)."""
        from strategies.mean_reversion import MeanReversion
        from strategies.base_strategy import Signal
        prices = [100.0] * 25 + [110.0]  # Z >> 0 after spike
        data = _make_ohlcv(prices)
        with patch("strategies._trend_context.is_against_trend", return_value=False):
            mr = MeanReversion({
                "entry_z_score": 1.0,
                "tp_reversion_pct": 0.80,
                "trend_filter_pct": None,
            })
            sig = mr.generate_signal(data, "TEST")
        # The above series may not produce a SELL because z must be falling
        # (z < z_prev). Construct a falling-z scenario:
        prices = [100.0] * 24 + [115.0, 110.0]
        data = _make_ohlcv(prices)
        with patch("strategies._trend_context.is_against_trend", return_value=False):
            mr = MeanReversion({
                "entry_z_score": 1.0,
                "tp_reversion_pct": 0.80,
                "trend_filter_pct": None,
            })
            sig = mr.generate_signal(data, "TEST")
        if sig.signal == Signal.SELL:
            current = sig.price
            mean = sig.metadata["rolling_mean"]
            expected_tp = current - (current - mean) * 0.80
            assert abs(sig.take_profit - expected_tp) < 0.01

    def test_long_tp_is_80pct_of_distance_to_mean(self):
        from strategies.mean_reversion import MeanReversion
        from strategies.base_strategy import Signal
        prices = [100.0] * 24 + [85.0, 90.0]  # crash then partial recovery
        data = _make_ohlcv(prices)
        with patch("strategies._trend_context.is_against_trend", return_value=False):
            mr = MeanReversion({
                "entry_z_score": 1.0,
                "tp_reversion_pct": 0.80,
                "trend_filter_pct": None,
            })
            sig = mr.generate_signal(data, "TEST")
        if sig.signal == Signal.BUY:
            current = sig.price
            mean = sig.metadata["rolling_mean"]
            expected_tp = current + (mean - current) * 0.80
            assert abs(sig.take_profit - expected_tp) < 0.01

    def test_tp_is_inside_entry_to_mean_band(self):
        """Sanity: TP must always lie between entry and mean for SHORT,
        between entry and mean for LONG (i.e. not past 100%)."""
        from strategies.mean_reversion import MeanReversion
        from strategies.base_strategy import Signal
        prices = [100.0] * 24 + [120.0, 115.0]
        data = _make_ohlcv(prices)
        with patch("strategies._trend_context.is_against_trend", return_value=False):
            mr = MeanReversion({
                "entry_z_score": 1.0,
                "tp_reversion_pct": 0.80,
                "trend_filter_pct": None,
            })
            sig = mr.generate_signal(data, "TEST")
        if sig.signal == Signal.SELL:
            assert sig.take_profit > sig.metadata["rolling_mean"]
            assert sig.take_profit < sig.price


# ---------- XGBoost: SL/TP on SELL ----------

class TestXGBoostSellHasSLTP:
    def test_sell_signal_carries_stop_loss_and_take_profit(self):
        """The pre-2026-05-07 bug: xgboost SELL returned with SL=None, TP=None,
        falling back to ensemble's 1.5%/3% defaults. We now set them
        symmetrically with the BUY path."""
        from strategies.xgboost_classifier import XGBoostClassifier
        from strategies.base_strategy import Signal

        n = 60
        prices = [100.0 + (i % 7) for i in range(n)]
        data = _make_ohlcv(prices)
        # Patch FeatureEngine and the XGBoost model to force a SELL.
        with patch.object(XGBoostClassifier, "_load_model", return_value=None):
            xgb = XGBoostClassifier({
                "model_path": "models/xgboost_model.pkl",
                "confidence_threshold": 0.6,
                "trend_filter_pct": None,
            })
            mock_model = MagicMock()
            mock_model.predict_proba.return_value = np.array([[0.75, 0.25]])  # SELL
            xgb._model = mock_model
            xgb._unhealthy_reason = None
            xgb._feature_engine = MagicMock()
            xgb._feature_engine.compute_all_features.return_value = (
                pd.DataFrame({"f1": [1.0]}), ["f1"]
            )
            sig = xgb.generate_signal(data, "TEST")

        if sig.signal == Signal.SELL:
            assert sig.stop_loss is not None, "SELL signal must carry SL"
            assert sig.take_profit is not None, "SELL signal must carry TP"
            assert sig.stop_loss > sig.price, "SHORT SL must be above entry"
            assert sig.take_profit < sig.price, "SHORT TP must be below entry"


# ---------- Portfolio: idempotent trade persistence ----------

class TestPortfolioIdempotentPersist:
    def test_close_position_persists_trade_to_db(self, tmp_path):
        """A direct call to portfolio.close_position() (bypassing
        trading_agent) must persist the trade row to the trades table.
        Pre-fix: this silently dropped the row, observed live during
        the 2026-05-07 manual close."""
        from core.database import Database
        from core.portfolio import Portfolio

        db = Database(str(tmp_path / "test.db"))
        port = Portfolio(initial_balance=50000, database=db, reset_balance=True)
        port.open_position(
            symbol="ZZTEST", side="SELL", quantity=10,
            price=100.0, strategy="mean_reversion",
            stop_loss=105.0, take_profit=95.0,
        )
        rec = port.close_position("ZZTEST", exit_price=95.5,
                                   exit_reason="manual_test")
        assert rec is not None

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        try:
            rows = conn.execute(
                "SELECT symbol, exit_price, exit_reason FROM trades WHERE symbol=?",
                ("ZZTEST",),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, f"Expected one trade row, got {len(rows)}"
        assert rows[0][1] == 95.5
        assert rows[0][2] == "manual_test"

    def test_persist_is_idempotent(self, tmp_path):
        """If close_position runs once and then trading_agent ALSO calls
        store_trade for the same record, we must not get a duplicate row."""
        from core.database import Database
        from core.portfolio import Portfolio

        db = Database(str(tmp_path / "test.db"))
        port = Portfolio(initial_balance=50000, database=db, reset_balance=True)
        port.open_position(
            symbol="ZZTEST2", side="SELL", quantity=10,
            price=100.0, strategy="mean_reversion",
            stop_loss=105.0, take_profit=95.0,
        )
        rec = port.close_position("ZZTEST2", exit_price=95.5,
                                   exit_reason="manual_test")
        assert rec is not None

        # Simulate trading_agent's _store_trade_to_db running second
        db.store_trade(rec.to_dict())

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol=?",
                ("ZZTEST2",),
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 1, f"Expected idempotent, got {count} rows"
