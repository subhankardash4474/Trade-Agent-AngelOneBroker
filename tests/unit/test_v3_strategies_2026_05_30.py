"""Unit tests for v3.0 swing strategies (Phase A3 deliverable).

Pins the entry-condition logic and SL/TP math for the two charter
strategies:
  - ``packages/strategies/trend_pullback.py`` — Rule 1
  - ``packages/strategies/breakout_20d.py`` — Rule 2

Per ``docs/freeze/freeze_v3.0_charter_2026-05-30.md`` §2 and the Phase A3
plan in the same charter §6.

Test design
-----------
We construct deterministic OHLCV fixtures shaped to satisfy or violate
each rule's conditions one at a time. The fixtures use enough bars
(>= 220) to clear ``required_history_bars`` for both strategies, then
override the LAST bar's values to drive a target signal. Most tests
build on a "baseline rising" series (steady uptrend) so the trend
filters and ADX naturally pass; specific gates (RSI, volume, etc.) are
flipped via single-bar overrides to isolate each filter.

Cross-references
----------------
* `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §2.
* `docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md` §3, §4.
* `packages/strategies/trend_pullback.py`, `packages/strategies/breakout_20d.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from strategies.base_strategy import Signal  # noqa: E402
from strategies.breakout_20d import Breakout20D  # noqa: E402
from strategies.trend_pullback import TrendPullback  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────


def _baseline_uptrend(
    n_bars: int = 250,
    *,
    start_price: float = 100.0,
    daily_drift: float = 0.3,
    daily_noise: float = 0.05,
    seed: int = 11,
) -> pd.DataFrame:
    """Synthetic daily series in a clean uptrend.

    Designed so that:
    * close > 200-DMA (drift compounds upward over the window).
    * close > 50-DMA likewise.
    * RSI will be elevated (60-70) on the last bar — useful for the
      breakout test (no RSI filter there) but NOT for the pullback
      test, which requires RSI in [40, 55]. Pullback tests override
      the last few bars to engineer the cooled RSI.
    * Volume is steady; tests inject single-bar overrides for the
      volume gates.
    * ADX will be elevated (clean trend, low whipsaw).
    """
    rng = np.random.default_rng(seed)
    closes = []
    p = start_price
    for _ in range(n_bars):
        p = p * (1.0 + daily_drift / 100.0) + rng.normal(0.0, daily_noise)
        closes.append(p)
    closes = np.array(closes)
    opens = closes - rng.uniform(0.05, 0.15, size=n_bars)
    highs = np.maximum(opens, closes) + rng.uniform(0.10, 0.25, size=n_bars)
    lows = np.minimum(opens, closes) - rng.uniform(0.10, 0.25, size=n_bars)
    volumes = np.full(n_bars, 100_000.0) + rng.uniform(-5_000, 5_000, size=n_bars)
    idx = pd.date_range(
        "2026-01-01 09:15:00", periods=n_bars, freq="D", tz="Asia/Kolkata"
    )
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=idx,
    )


def _force_pullback_to_sma20(
    df: pd.DataFrame,
    *,
    rsi_target: float = 47.0,
    volume_ratio: float = 1.0,
) -> pd.DataFrame:
    """Engineer the last bar so the strategy sees a "pulled back to
    20-DMA" condition with RSI ~= rsi_target.

    Concretely:
    * Override the last 5 closes to be a flat sequence near the 20-DMA
      so the close ends within 1% of the 20-DMA AND RSI cools into the
      [40, 55] window.
    * Set the last bar's volume to ``volume_ratio * 20d_avg``.
    """
    df = df.copy()
    sma_20 = df["close"].rolling(20).mean().iloc[-6]  # 20-DMA before override
    if pd.isna(sma_20):
        raise ValueError("baseline series too short to compute 20-DMA")

    # Make the last 5 closes a slight downward drift toward sma_20 so
    # RSI cools naturally. Target ending close = 0.998 * sma_20 (within
    # 1% of 20-DMA, charter spec is "within 2%").
    target_close = sma_20 * 0.998
    last_close_before = df["close"].iloc[-6]
    n_taper = 5
    for k in range(1, n_taper + 1):
        weight = k / n_taper
        new_close = (1 - weight) * last_close_before + weight * target_close
        idx = -n_taper - 1 + k
        df.iloc[idx, df.columns.get_loc("close")] = new_close
        df.iloc[idx, df.columns.get_loc("open")] = new_close - 0.10
        df.iloc[idx, df.columns.get_loc("high")] = new_close + 0.10
        df.iloc[idx, df.columns.get_loc("low")] = new_close - 0.20

    # Volume override on the last bar only.
    vol_avg = df["volume"].iloc[-21:-1].mean()
    df.iloc[-1, df.columns.get_loc("volume")] = volume_ratio * vol_avg
    return df


def _force_breakout(
    df: pd.DataFrame,
    *,
    breakout_pct_above_prior_high: float = 1.0,
    volume_ratio: float = 2.0,
) -> pd.DataFrame:
    """Engineer the last bar to break above the prior 20-day high by
    ``breakout_pct_above_prior_high`` % on volume = ``volume_ratio`` *
    20d avg.
    """
    df = df.copy()
    prior_high = df["high"].iloc[-21:-1].max()
    new_close = prior_high * (1.0 + breakout_pct_above_prior_high / 100.0)
    df.iloc[-1, df.columns.get_loc("close")] = new_close
    df.iloc[-1, df.columns.get_loc("open")] = new_close - 0.5
    df.iloc[-1, df.columns.get_loc("high")] = new_close + 0.3
    df.iloc[-1, df.columns.get_loc("low")] = new_close - 1.0  # breakout-day low
    vol_avg = df["volume"].iloc[-21:-1].mean()
    df.iloc[-1, df.columns.get_loc("volume")] = volume_ratio * vol_avg
    return df


# ─────────────────────────────────────────────────────────────────────
# TrendPullback (Rule 1) tests
# ─────────────────────────────────────────────────────────────────────


class TestTrendPullbackInsufficientData:
    def test_short_series_returns_hold(self):
        df = _baseline_uptrend(n_bars=50)
        sig = TrendPullback().generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "insufficient_data"


class TestTrendPullbackEntryConditions:
    def test_clean_pullback_emits_buy_with_3pct_sl_and_8pct_tp(self):
        df = _force_pullback_to_sma20(_baseline_uptrend())
        strat = TrendPullback()
        sig = strat.generate_signal(df, "AAA")
        assert sig.signal == Signal.BUY, (
            f"expected BUY on clean pullback; got {sig.signal} "
            f"reason={(sig.metadata or {}).get('reason')}"
        )
        assert sig.stop_loss is not None and sig.take_profit is not None
        close = float(df["close"].iloc[-1])
        assert sig.stop_loss == pytest.approx(close * 0.97, abs=1e-6)
        assert sig.take_profit == pytest.approx(close * 1.08, abs=1e-6)
        # Metadata sanity: rule + SL/TP pcts surfaced for downstream.
        assert sig.metadata["rule"] == "trend_pullback"
        assert sig.metadata["sl_pct"] == 3.0
        assert sig.metadata["tp_pct"] == 8.0

    def test_close_below_50dma_emits_sell_exit_signal(self):
        """Charter rule: exit on breach of 50-DMA. Strategy emits SELL
        so engine's opposite-signal path closes any held long."""
        df = _baseline_uptrend()
        # Drive the last close BELOW the 50-DMA by tanking the last
        # 30 bars heavily.
        sma_50 = df["close"].rolling(50).mean().iloc[-1]
        df.iloc[-1, df.columns.get_loc("close")] = float(sma_50) * 0.95
        sig = TrendPullback().generate_signal(df, "AAA")
        assert sig.signal == Signal.SELL
        assert (sig.metadata or {}).get("reason") == "exit_close_below_50dma"

    def test_above_50dma_but_not_in_pullback_zone_emits_hold(self):
        """Baseline uptrend has close well above 20-DMA (drift
        compounds), so the pullback proximity check fails."""
        df = _baseline_uptrend()
        sig = TrendPullback().generate_signal(df, "AAA")
        # The baseline uptrend rallies hard, so close >> 20-DMA. The
        # rule should HOLD with proximity_pct > 2.0%.
        assert sig.signal == Signal.HOLD
        reason = (sig.metadata or {}).get("reason")
        assert reason == "not_in_pullback_zone", (
            f"expected pullback gate to reject, got reason={reason}"
        )

    def test_low_volume_pullback_blocks_buy(self):
        """Pullback met but volume is below 80% of 20d avg → HOLD."""
        df = _force_pullback_to_sma20(_baseline_uptrend(), volume_ratio=0.5)
        sig = TrendPullback().generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "low_volume_pullback"

    def test_volume_ratio_at_floor_passes(self):
        """Boundary: volume exactly at 80% of 20d avg should NOT block.
        Uses volume_ratio = 0.81 to clear floor unambiguously."""
        df = _force_pullback_to_sma20(_baseline_uptrend(), volume_ratio=0.81)
        sig = TrendPullback().generate_signal(df, "AAA")
        assert sig.signal == Signal.BUY


class TestTrendPullbackParamConfig:
    def test_custom_sl_tp_pcts_propagate_to_signal(self):
        df = _force_pullback_to_sma20(_baseline_uptrend())
        strat = TrendPullback({"sl_pct": 2.5, "tp_pct": 6.0})
        sig = strat.generate_signal(df, "AAA")
        assert sig.signal == Signal.BUY
        close = float(df["close"].iloc[-1])
        assert sig.stop_loss == pytest.approx(close * 0.975, abs=1e-6)
        assert sig.take_profit == pytest.approx(close * 1.06, abs=1e-6)


# ─────────────────────────────────────────────────────────────────────
# Breakout20D (Rule 2) tests
# ─────────────────────────────────────────────────────────────────────


class TestBreakout20DInsufficientData:
    def test_short_series_returns_hold(self):
        df = _baseline_uptrend(n_bars=40)
        sig = Breakout20D().generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "insufficient_data"


class TestBreakout20DEntryConditions:
    def test_clean_breakout_emits_buy_with_pct_sl_and_12pct_tp(self):
        df = _force_breakout(_baseline_uptrend())
        sig = Breakout20D().generate_signal(df, "AAA")
        assert sig.signal == Signal.BUY, (
            f"expected BUY on clean breakout; got {sig.signal} "
            f"reason={(sig.metadata or {}).get('reason')}"
        )
        close = float(df["close"].iloc[-1])
        assert sig.take_profit == pytest.approx(close * 1.12, abs=1e-6)
        # SL = max(pct_sl, breakout_low). The fixture's breakout_low =
        # close - 1.0; pct_sl = close * 0.96. For a typical
        # close ~ 100-200 region, pct_sl ~= close - 4 to 8, so the
        # PERCENTAGE bound is tighter (LOWER SL = farther downside)
        # while breakout_low is tighter (HIGHER SL = less downside).
        # Therefore SL = max(pct_sl, breakout_low) = breakout_low.
        breakout_low = float(df["low"].iloc[-1])
        pct_sl = close * 0.96
        expected_sl = max(pct_sl, breakout_low)
        assert sig.stop_loss == pytest.approx(expected_sl, abs=1e-6)
        # Source metadata records which bound binds; for this fixture
        # it's the breakout_low (tighter SL).
        assert sig.metadata["sl_source"] == "breakout_low"

    def test_no_breakout_returns_hold(self):
        """Last close below prior 20-day high → HOLD."""
        df = _baseline_uptrend()
        # Tame the last bar so it's not above prior 20d high.
        prior_high = df["high"].iloc[-21:-1].max()
        df.iloc[-1, df.columns.get_loc("close")] = float(prior_high) * 0.95
        sig = Breakout20D().generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "no_breakout"

    def test_weak_volume_breakout_blocks(self):
        """Breakout met but volume < 1.5x 20d avg → HOLD."""
        df = _force_breakout(_baseline_uptrend(), volume_ratio=1.0)
        sig = Breakout20D().generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "weak_volume_breakout"

    def test_low_adx_blocks(self):
        """Breakout + volume met but ADX <= threshold → HOLD. We test
        the gate by configuring an absurdly high adx_threshold on an
        otherwise-passing fixture. This isolates the ADX gate from
        the brittleness of trying to construct a "low-ADX-but-still-
        breakout-y" fixture (the two conditions tend to fight each
        other and small drift gets ADX above 20 even on visually-flat
        series — see the previous attempt with random_clip noise).
        """
        df = _force_breakout(_baseline_uptrend())
        # 200 > theoretical ADX max (100) so this gate ALWAYS rejects.
        strat_strict = Breakout20D({"adx_threshold": 200.0})
        sig = strat_strict.generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "adx_below_threshold"
        # Sanity: actual ADX < threshold by definition of a real series.
        assert (sig.metadata or {}).get("adx") <= 100.0


class TestBreakout20DSlSource:
    def test_pct_sl_binds_when_breakout_low_far_below(self):
        """If the breakout_day low is significantly below entry, then
        pct_sl (tighter, less downside) should bind. We engineer a
        breakout day where the low gaps DOWN well past the 4% pct SL.
        """
        df = _force_breakout(_baseline_uptrend())
        # Move the breakout-day low to 10% below close (vs 4% pct SL),
        # so pct_sl > breakout_low and pct_sl binds.
        close = float(df["close"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")] = close * 0.90  # -10%
        sig = Breakout20D().generate_signal(df, "AAA")
        assert sig.signal == Signal.BUY
        assert sig.metadata["sl_source"] == "pct"
        assert sig.stop_loss == pytest.approx(close * 0.96, abs=1e-6)


class TestBreakout20DParamConfig:
    def test_custom_volume_multiplier_changes_block_decision(self):
        """Set volume_multiplier higher than the fixture's ratio so
        the strategy rejects what would otherwise pass."""
        df = _force_breakout(_baseline_uptrend(), volume_ratio=2.0)
        # Default multiplier 1.5 → 2.0 vol passes.
        assert Breakout20D().generate_signal(df, "AAA").signal == Signal.BUY
        # Bump multiplier to 3.0 → 2.0 vol fails.
        strat_strict = Breakout20D({"volume_multiplier": 3.0})
        sig = strat_strict.generate_signal(df, "AAA")
        assert sig.signal == Signal.HOLD
        assert (sig.metadata or {}).get("reason") == "weak_volume_breakout"


# ─────────────────────────────────────────────────────────────────────
# Registry + ensemble integration smokes
# ─────────────────────────────────────────────────────────────────────


class TestStrategyRegistryAndDefaultWeights:
    """Both strategies must be importable from STRATEGY_REGISTRY and
    must have a default weight in the ensemble. Without registration,
    v3 swing variants can't reference them by name in
    ``strategies.active``; without weights, the ensemble drops their
    signals at the aggregation step."""

    def test_strategies_in_registry(self):
        from strategies import STRATEGY_REGISTRY

        assert "trend_pullback" in STRATEGY_REGISTRY
        assert "breakout_20d" in STRATEGY_REGISTRY
        assert STRATEGY_REGISTRY["trend_pullback"] is TrendPullback
        assert STRATEGY_REGISTRY["breakout_20d"] is Breakout20D

    def test_ensemble_default_weights_include_v3_strategies(self):
        from strategies.ensemble import DEFAULT_WEIGHTS

        assert "trend_pullback" in DEFAULT_WEIGHTS
        assert "breakout_20d" in DEFAULT_WEIGHTS
        # Sanity: positive non-zero — a 0-weight strategy is silently
        # ignored by the aggregator.
        assert DEFAULT_WEIGHTS["trend_pullback"] > 0
        assert DEFAULT_WEIGHTS["breakout_20d"] > 0
