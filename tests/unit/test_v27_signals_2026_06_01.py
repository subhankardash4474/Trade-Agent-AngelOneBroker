"""Pin tests for V27 signal utilities (charter v4 §3.2-§3.5).

Tests live here:
    * research.signals.donchian      — rolling_high/low, chandelier_stop,
                                       entry_signal, exit_signal
    * research.signals.volatility_sizer — vol_target_size
    * research.signals.risk_parity   — daily_return_std, inverse_vol_weights,
                                       allocate, shares_from_allocation
    * research.instruments.etf_universe — load_v4_swing_cash_universe

These are unit tests with synthetic fixtures, NOT integration tests. The
end-to-end V27 backtest result is pinned separately under
`tests/integration/` once the standalone backtester lands a baseline.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from core.signals import donchian, risk_parity, volatility_sizer
from core.instruments import etf_universe


# ============================================================
# fixtures
# ============================================================

def _trending_series(n: int, start: float = 100.0, daily_pct: float = 0.4,
                     vol_seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV with steady uptrend + small noise.

    Use this for "should-fire-entry" tests. The 200-SMA must be below
    today's close, ADX must clear 20, and the 55-day channel must be
    broken on the last bar.
    """
    rng = np.random.default_rng(vol_seed)
    closes = [start]
    for _ in range(n - 1):
        drift = daily_pct / 100.0
        noise = rng.normal(0, 0.005)
        closes.append(closes[-1] * (1.0 + drift + noise))
    closes = np.array(closes)
    # OHLC around close
    spread = closes * 0.01
    highs = closes + spread * rng.random(n)
    lows = closes - spread * rng.random(n)
    opens = closes + rng.normal(0, spread.mean() * 0.5, n)
    # Force the LAST bar to be a clear breakout: max(high) of last 56 bars.
    highs[-1] = highs[-56:].max() * 1.001
    closes[-1] = highs[-1]  # close at the high (volume confirm)
    # Volume: 1.5x the 20-day average on the last bar (passes 1.2x gate)
    base_vol = 1_000_000
    vols = np.full(n, base_vol) + rng.integers(-100_000, 100_000, n)
    vols[-1] = int(vols[-21:-1].mean() * 1.5)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    }, index=pd.date_range("2020-01-01", periods=n, freq="B"))


def _flat_series(n: int, value: float = 100.0) -> pd.DataFrame:
    """Synthetic OHLCV with constant price (no trend, no vol)."""
    closes = np.full(n, value)
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": np.full(n, 1_000_000),
    }, index=pd.date_range("2020-01-01", periods=n, freq="B"))


def _declining_series(n: int, start: float = 100.0,
                      daily_pct: float = -0.3) -> pd.DataFrame:
    """Synthetic OHLCV with steady downtrend."""
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.0 + daily_pct / 100.0))
    closes = np.array(closes)
    return pd.DataFrame({
        "open": closes, "high": closes * 1.005, "low": closes * 0.995,
        "close": closes, "volume": np.full(n, 1_000_000),
    }, index=pd.date_range("2020-01-01", periods=n, freq="B"))


# ============================================================
# donchian.rolling_high / rolling_low
# ============================================================

class TestRollingChannels:
    def test_rolling_high_excludes_today(self):
        # Construct a series where TODAY's high is the highest; the
        # rolling_high(n=5) should return the max of bars -6 to -1
        # (i.e. EXCLUDING today).
        df = pd.DataFrame({
            "open": [10, 11, 12, 13, 14, 99],
            "high": [10, 11, 12, 13, 14, 99],
            "low":  [10, 11, 12, 13, 14, 99],
            "close": [10, 11, 12, 13, 14, 99],
            "volume": [100] * 6,
        })
        assert donchian.rolling_high(df, n=5) == 14.0  # not 99

    def test_rolling_low_excludes_today(self):
        df = pd.DataFrame({
            "open": [10, 9, 8, 7, 6, 1],
            "high": [10, 9, 8, 7, 6, 1],
            "low":  [10, 9, 8, 7, 6, 1],
            "close": [10, 9, 8, 7, 6, 1],
            "volume": [100] * 6,
        })
        assert donchian.rolling_low(df, m=5) == 6.0  # not 1

    def test_rolling_returns_nan_on_insufficient_history(self):
        df = _flat_series(3)
        assert math.isnan(donchian.rolling_high(df, n=10))
        assert math.isnan(donchian.rolling_low(df, m=10))


# ============================================================
# donchian.entry_signal
# ============================================================

class TestEntrySignal:
    def test_fires_on_trending_breakout(self):
        df = _trending_series(n=300)
        fires, diag = donchian.entry_signal(df)
        # Pin: all 7 gates should pass on a clean trending breakout.
        assert fires is True, f"diag={diag}"
        for gate_name, gate_data in diag["gates"].items():
            assert gate_data["pass"], f"gate {gate_name} unexpectedly failed: {gate_data}"

    def test_no_fire_on_flat_series(self):
        df = _flat_series(n=300)
        fires, diag = donchian.entry_signal(df)
        assert fires is False, f"diag={diag}"
        # On flat, the volume_confirm gate fails first (today_vol == mean_vol)
        # AND the donchian_breakout gate fails (no breakout). Pinning any one.
        assert not diag["gates"]["donchian_breakout"]["pass"]

    def test_no_fire_on_declining_series(self):
        df = _declining_series(n=300)
        fires, diag = donchian.entry_signal(df)
        assert fires is False
        # Either regime_filter or donchian_breakout must fail.
        regime_pass = diag["gates"]["regime_filter_sma200"]["pass"]
        breakout_pass = diag["gates"]["donchian_breakout"]["pass"]
        assert not (regime_pass and breakout_pass), \
            f"declining series should fail at least one gate: regime={regime_pass}, breakout={breakout_pass}"

    def test_short_side_raises(self):
        df = _trending_series(n=300)
        with pytest.raises(NotImplementedError, match="short"):
            donchian.entry_signal(df, side="short")

    def test_insufficient_history_returns_false(self):
        df = _flat_series(n=50)
        fires, diag = donchian.entry_signal(df)
        assert fires is False
        assert "insufficient_history" in diag["reason"]

    def test_whipsaw_guard_blocks_recent_entry(self):
        df = _trending_series(n=300)
        # Pretend we entered 5 bars ago. The guard requires >= 10.
        fires, diag = donchian.entry_signal(
            df, last_entry_bar_index=len(df) - 1 - 5,
        )
        assert fires is False
        assert "whipsaw_guard" in diag["reason"]


# ============================================================
# donchian.exit_signal + chandelier_stop
# ============================================================

class TestExitSignal:
    def test_donchian_exit_fires_on_breakdown(self):
        # 30 bars climbing, then a single bar that crashes through the
        # last 20 bars' low.
        df = _trending_series(n=100)
        # Force last bar's close BELOW the rolling 20-bar low.
        df.loc[df.index[-1], "close"] = df["low"].iloc[-21:-1].min() * 0.99
        fires, diag = donchian.exit_signal(df, exit_m=20)
        assert fires is True
        assert diag["gates"]["donchian_exit"]["fires"]

    def test_no_exit_on_trending_continuation(self):
        df = _trending_series(n=100)
        fires, diag = donchian.exit_signal(df, exit_m=20, entry_bar_index=None)
        # Trending up; close should not be below 20-bar low.
        assert fires is False

    def test_chandelier_stop_below_high_since_entry(self):
        df = _trending_series(n=100)
        entry_idx = 50  # we "entered" 50 bars ago
        stop = donchian.chandelier_stop(df, entry_index=entry_idx,
                                        period=14, multiplier=3.0)
        max_since = float(df["high"].iloc[entry_idx:].max())
        assert stop < max_since
        # Stop should be above zero (not crazy)
        assert stop > 0

    def test_time_in_trade_forced_exit(self):
        df = _trending_series(n=100)
        # entry 70 bars ago, max_time=60 → should fire on time
        fires, diag = donchian.exit_signal(
            df, entry_bar_index=len(df) - 1 - 70,
            max_time_in_trade_bars=60,
        )
        assert fires is True
        assert diag["gates"]["max_time_in_trade"]["fires"]


# ============================================================
# volatility_sizer.vol_target_size
# ============================================================

class TestVolTargetSizer:
    def test_basic_sizing_uses_risk_target(self):
        # 100,000 equity, 0.5% risk = 500 inr risk budget
        # ATR = 2 inr/share, price = 100 → shares = 500/2 = 250
        # 250 shares * 100 = 25,000 notional = 25% of equity → CAPPED at 8% = 8000
        # So binding = max_position, shares = 8000 / 100 = 80
        result = volatility_sizer.vol_target_size(
            equity_inr=100_000, price_inr=100.0,
            atr_14_inr_per_share=2.0,
        )
        assert result.shares == 80
        assert result.binding_constraint == "max_position"
        assert result.notional_inr == 80 * 100.0
        assert result.risk_inr == 80 * 2.0

    def test_risk_target_binds_when_atr_large(self):
        # ATR = 10 inr/share, price = 100, equity = 100,000, risk 0.5%
        # shares_at_risk = 500/10 = 50
        # shares_at_max = 8000/100 = 80
        # 50 < 80 → risk_target binds; shares = 50
        result = volatility_sizer.vol_target_size(
            equity_inr=100_000, price_inr=100.0,
            atr_14_inr_per_share=10.0,
        )
        assert result.shares == 50
        assert result.binding_constraint == "risk_target"

    def test_zero_atr_returns_zero_shares(self):
        result = volatility_sizer.vol_target_size(
            equity_inr=100_000, price_inr=100.0,
            atr_14_inr_per_share=0.0,
        )
        assert result.shares == 0
        assert result.binding_constraint == "atr_zero"

    def test_nan_atr_returns_zero_shares(self):
        result = volatility_sizer.vol_target_size(
            equity_inr=100_000, price_inr=100.0,
            atr_14_inr_per_share=float("nan"),
        )
        assert result.shares == 0
        assert result.binding_constraint == "atr_zero"

    def test_zero_price_returns_zero_shares(self):
        result = volatility_sizer.vol_target_size(
            equity_inr=100_000, price_inr=0.0,
            atr_14_inr_per_share=2.0,
        )
        assert result.shares == 0
        assert result.binding_constraint == "price_zero"

    def test_equity_too_small_returns_zero(self):
        # Risk 0.5% of 100 INR = 0.5 INR, ATR = 100 → shares = 0.005 → 0
        result = volatility_sizer.vol_target_size(
            equity_inr=100, price_inr=10.0,
            atr_14_inr_per_share=100.0,
        )
        assert result.shares == 0
        assert result.binding_constraint == "equity_too_small"

    def test_lot_size_rounding_down(self):
        # 100 shares would be risk_target binding; lot=75 should round to 75
        result = volatility_sizer.vol_target_size(
            equity_inr=1_000_000, price_inr=100.0,
            atr_14_inr_per_share=50.0, lot_size=75,
        )
        # risk: 1M * 0.5% = 5000 budget; atr = 50/share → shares_at_risk = 100
        # max_position: 1M * 8% = 80,000 / 100 = 800 → not binding
        # 100 shares, rounded down to lot=75 → 75
        assert result.shares == 75
        assert result.binding_constraint == "risk_target"


# ============================================================
# risk_parity allocator
# ============================================================

class TestRiskParity:
    def test_inverse_vol_weights_sum_to_one(self):
        sigmas = {"A": 0.01, "B": 0.02, "C": 0.04}
        w = risk_parity.inverse_vol_weights(sigmas)
        assert abs(sum(w.values()) - 1.0) < 1e-9
        # Lowest sigma gets the largest weight
        assert w["A"] > w["B"] > w["C"]

    def test_inverse_vol_drops_nan(self):
        sigmas = {"A": 0.01, "B": float("nan"), "C": 0.04}
        w = risk_parity.inverse_vol_weights(sigmas)
        assert "B" not in w
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_allocate_caps_per_name(self):
        # Two symbols, equal sigma → 50/50 weights → 50k each on 100k.
        # Cap at 8% = 8000. Both should be capped to 8000.
        sigmas = {"A": 0.01, "B": 0.01}
        alloc = risk_parity.allocate(100_000, sigmas, max_per_name_pct=8.0)
        assert alloc["A"] == pytest.approx(8_000.0)
        assert alloc["B"] == pytest.approx(8_000.0)

    def test_allocate_redistributes_to_uncapped(self):
        # 3 symbols: A is the lowest vol (gets capped); the freed
        # capital is redistributed to B + C in proportion to their
        # ORIGINAL (uncapped) weights, without pushing them over the
        # cap.
        #
        # Inverse-vol weights: A=100/175=0.571, B=50/175=0.286, C=25/175=0.143
        # Initial on 100k:    A=57,143, B=28,571, C=14,286
        # Cap = 50,000 (50%): A over → cap at 50,000, freed=7,143
        # Redistribute to B (weight 0.286) + C (weight 0.143):
        #   B += 7,143 * 0.667 = 4,762 → B = 33,333
        #   C += 7,143 * 0.333 = 2,381 → C = 16,667
        # Both now under cap; algorithm terminates.
        # Sum = 50,000 + 33,333 + 16,667 = 100,000  ✓
        sigmas = {"A": 0.01, "B": 0.02, "C": 0.04}
        alloc = risk_parity.allocate(100_000, sigmas, max_per_name_pct=50.0)
        assert alloc["A"] == pytest.approx(50_000.0, abs=1.0)  # capped
        assert alloc["B"] == pytest.approx(33_333.0, abs=10.0)
        assert alloc["C"] == pytest.approx(16_667.0, abs=10.0)
        # Sum should be exactly the total (no residual when redistribution
        # successfully placed all of A's overflow).
        assert sum(alloc.values()) == pytest.approx(100_000.0, abs=2.0)
        # Risk-parity ordering preserved among uncapped names: lower
        # sigma → larger allocation.
        assert alloc["B"] > alloc["C"]

    def test_allocate_residual_when_all_names_hit_cap(self):
        # 3 symbols, each sigma=0.001 (equal weights → 33.3% each), but
        # cap=10% → each name capped at 10,000; total allocated = 30,000;
        # residual = 70,000 (would go to LIQUIDBEES per charter §3.6).
        sigmas = {"A": 0.001, "B": 0.001, "C": 0.001}
        alloc = risk_parity.allocate(100_000, sigmas, max_per_name_pct=10.0)
        assert alloc["A"] == pytest.approx(10_000.0)
        assert alloc["B"] == pytest.approx(10_000.0)
        assert alloc["C"] == pytest.approx(10_000.0)
        # 70k unallocated — caller (orchestrator) sweeps to LIQUIDBEES.
        assert sum(alloc.values()) == pytest.approx(30_000.0)

    def test_allocate_empty_on_no_capital(self):
        assert risk_parity.allocate(0, {"A": 0.01}) == {}

    def test_daily_return_std_basic(self):
        df = _trending_series(n=50)
        s = risk_parity.daily_return_std(df, window=20)
        assert math.isfinite(s)
        assert s > 0

    def test_daily_return_std_nan_on_short_history(self):
        df = _flat_series(n=5)
        assert math.isnan(risk_parity.daily_return_std(df, window=20))

    def test_shares_from_allocation(self):
        alloc = {"A": 8_000.0, "B": 50_000.0}
        prices = {"A": 100.0, "B": 250.0}
        shares = risk_parity.shares_from_allocation(alloc, prices)
        assert shares["A"] == 80    # 8000/100
        assert shares["B"] == 200   # 50000/250

    def test_shares_zero_on_nan_price(self):
        alloc = {"A": 8_000.0}
        prices = {"A": float("nan")}
        shares = risk_parity.shares_from_allocation(alloc, prices)
        assert shares["A"] == 0


# ============================================================
# universe loader
# ============================================================

class TestUniverseLoader:
    def test_loads_75_with_cash_sweep(self):
        u = etf_universe.load_v4_swing_cash_universe(exclude_cash_sweep=False)
        assert len(u) == etf_universe.EXPECTED_UNIVERSE_SIZE
        assert "LIQUIDBEES" in u

    def test_loads_74_without_cash_sweep(self):
        u = etf_universe.load_v4_swing_cash_universe(exclude_cash_sweep=True)
        assert len(u) == etf_universe.EXPECTED_SIGNAL_SIZE
        assert "LIQUIDBEES" not in u

    def test_yfinance_suffix(self):
        u = etf_universe.load_v4_swing_cash_universe(yfinance_suffix=True)
        assert all(s.endswith(".NS") for s in u)

    def test_categories_present(self):
        cats = etf_universe.universe_categories()
        # Charter §3.1 — expected category labels.
        expected = {
            "Nifty 50 stocks", "Nifty Next 50",
            "Equity-broad ETFs", "Commodity ETFs",
            "Debt ETF", "Sector ETFs",
        }
        assert expected.issubset(set(cats.keys())), \
            f"missing categories: {expected - set(cats.keys())}"

    def test_list_signal_candidates_alias(self):
        a = etf_universe.list_signal_candidates()
        b = etf_universe.load_v4_swing_cash_universe(exclude_cash_sweep=True)
        assert a == b
