"""Pin tests for the V27 strategy class (charter v4 §3).

Distinct from `test_v27_signals_2026_06_01.py` which tests the
reusable signal primitives. This file tests the BaseStrategy adapter:
  * `required_history_bars` reads as 220 (200 + 20 headroom)
  * `generate_signal` returns HOLD/BUY/SELL `TradeSignal` with the
    correct metadata
  * Defaults (V27_DEFAULTS) match the charter §3.9 manifest exactly
  * Strategy is BUY-only (charter §3.2: short side disabled)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.base_strategy import Signal
from strategies.swing_cash.cross_asset_trend_v27 import (
    CrossAssetTrendV27,
    V27_DEFAULTS,
)


def _build_trending(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """Same builder as `test_v27_signals_2026_06_01._trending_series` —
    duplicated here so the two test files don't share a fixtures module
    (which would couple them; if either breaks, blast-radius stays local).
    """
    rng = np.random.default_rng(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.0 + 0.004 + rng.normal(0, 0.005)))
    closes = np.array(closes)
    spread = closes * 0.01
    highs = closes + spread * rng.random(n)
    lows = closes - spread * rng.random(n)
    opens = closes + rng.normal(0, spread.mean() * 0.5, n)
    highs[-1] = highs[-56:].max() * 1.001
    closes[-1] = highs[-1]
    base_vol = 1_000_000
    vols = np.full(n, base_vol) + rng.integers(-100_000, 100_000, n)
    vols[-1] = int(vols[-21:-1].mean() * 1.5)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    }, index=pd.date_range("2020-01-01", periods=n, freq="B"))


def _build_flat(n: int = 300) -> pd.DataFrame:
    closes = np.full(n, 100.0)
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": np.full(n, 1_000_000),
    }, index=pd.date_range("2020-01-01", periods=n, freq="B"))


# ============================================================
# pinned defaults — these are charter contract values
# ============================================================

class TestCharterDefaults:
    """Charter §3.2 explicitly states V27 params are NOT operator-tunable.
    Any change to these defaults must update charter §3.9 manifest AND
    this test simultaneously."""

    def test_donchian_entry_n_is_55(self):
        assert V27_DEFAULTS["entry_n"] == 55

    def test_donchian_exit_m_is_20(self):
        assert V27_DEFAULTS["exit_m"] == 20

    def test_regime_sma_is_200(self):
        assert V27_DEFAULTS["sma_regime"] == 200

    def test_volume_multiplier_is_1p2(self):
        assert V27_DEFAULTS["volume_multiplier"] == 1.2

    def test_atr_cap_pct_is_5p0(self):
        assert V27_DEFAULTS["atr_cap_pct"] == 5.0

    def test_adx_min_is_20(self):
        assert V27_DEFAULTS["adx_min"] == 20

    def test_chandelier_mult_is_3p0(self):
        assert V27_DEFAULTS["chandelier_mult"] == 3.0

    def test_max_time_in_trade_is_60(self):
        assert V27_DEFAULTS["max_time_in_trade_bars"] == 60


# ============================================================
# required_history_bars
# ============================================================

class TestRequiredHistory:
    def test_default_required_history_is_220(self):
        s = CrossAssetTrendV27()
        # 200-SMA + 20 headroom (charter §3 implies; matches trend_pullback).
        assert s.required_history_bars == 220

    def test_required_history_scales_with_sma_regime(self):
        s = CrossAssetTrendV27({"sma_regime": 100})
        assert s.required_history_bars == 120


# ============================================================
# generate_signal — entry / exit / hold paths
# ============================================================

class TestGenerateSignal:
    def test_insufficient_data_returns_hold(self):
        s = CrossAssetTrendV27()
        df = _build_flat(n=50)  # < 220 bars
        sig = s.generate_signal(df, "TEST.NS")
        assert sig.signal == Signal.HOLD
        assert sig.metadata["reason"] == "insufficient_data"
        assert sig.metadata["have_bars"] == 50

    def test_flat_series_returns_hold(self):
        s = CrossAssetTrendV27()
        df = _build_flat(n=300)
        sig = s.generate_signal(df, "FLAT.NS")
        assert sig.signal == Signal.HOLD

    def test_trending_breakout_returns_buy(self):
        s = CrossAssetTrendV27()
        df = _build_trending(n=300)
        sig = s.generate_signal(df, "TREND.NS")
        assert sig.signal == Signal.BUY, f"metadata={sig.metadata}"
        # Stop-loss must be set and below entry
        assert sig.stop_loss is not None
        assert sig.stop_loss < sig.price
        # V27 has no fixed TP
        assert sig.take_profit is None
        # Metadata records the rule
        assert sig.metadata["rule"] == "v27_donchian_entry"
        assert sig.metadata["chandelier_mult"] == 3.0

    def test_buy_signal_records_chandelier_stop(self):
        s = CrossAssetTrendV27()
        df = _build_trending(n=300)
        sig = s.generate_signal(df, "TREND.NS")
        assert sig.signal == Signal.BUY
        # chandelier_stop_initial = close - 3.0 * ATR(14)
        # ATR computed via EWM; we can't pin to exact value but it
        # should be in a sensible range (1-15% below entry)
        assert sig.metadata["chandelier_stop_initial"] is not None
        ratio = sig.metadata["chandelier_stop_initial"] / sig.price
        assert 0.85 <= ratio <= 0.99, f"stop ratio={ratio} outside [0.85, 0.99]"

    def test_donchian_breakdown_returns_sell(self):
        s = CrossAssetTrendV27()
        df = _build_trending(n=300)
        # Force last bar close below the rolling 20-bar low → Donchian exit.
        df.loc[df.index[-1], "close"] = df["low"].iloc[-21:-1].min() * 0.99
        sig = s.generate_signal(df, "DOWN.NS")
        assert sig.signal == Signal.SELL
        assert sig.metadata["rule"] == "v27_donchian_exit"

    def test_strategy_name_is_pinned(self):
        s = CrossAssetTrendV27()
        assert s.name == "cross_asset_trend_v27"

    def test_signal_carries_strategy_name(self):
        s = CrossAssetTrendV27()
        df = _build_flat(n=300)
        sig = s.generate_signal(df, "X.NS")
        assert sig.strategy_name == "cross_asset_trend_v27"


# ============================================================
# parameter customisation (V28+ retunes via single-param change)
# ============================================================

class TestParameterCustomisation:
    def test_custom_entry_n_overrides_default(self):
        s = CrossAssetTrendV27({"entry_n": 100})
        assert s.entry_n == 100
        # required_history_bars still dominated by sma_regime
        assert s.required_history_bars == 220

    def test_custom_chandelier_mult(self):
        s = CrossAssetTrendV27({"chandelier_mult": 2.5})
        assert s.chandelier_mult == 2.5
