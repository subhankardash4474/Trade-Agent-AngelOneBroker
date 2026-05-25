"""
Numerical-equivalence tests for the strategy_history_window perf fix.

Why these exist
---------------
On 2026-05-25 the V1+V2 nifty50_60d battery showed throughput dropping from
~39 ev/s at sim_date=2026-02-25 to ~8 ev/s instantaneous at sim_date=2026-03-12.
RCA traced it to backtest_ensemble._merge_bars yielding the entire growing
history (`df.iloc[: i + 1]`) on every event. Each strategy then did:

    df = data.copy()                                  # O(N)
    df["rsi"] = self._compute_rsi(df["close"], ...)   # O(N) ewm
    df["rsi_prev"] = df["rsi"].shift(1)               # O(N)
    rsi = df["rsi"].iloc[-1]                          # uses ONLY the last row

So O(N) work per event = O(N^2) per symbol over the run.

The fix caps the per-event slice to the last `strategy_history_window` bars
(default 300). The numerical claim is: for every pure-Python strategy in the
registry, the last-bar signal/confidence/SL/TP computed from the full-history
slice is IDENTICAL (within float epsilon) to the same computed from the
windowed slice, provided the window is comfortably larger than the strategy's
indicator decay length.

EWM/RSI/ATR/ADX use exponential decay with span ~= 5*period. For period=14 the
decay tail is ~70 bars. With window=300 every bar at position >=300 sees the
same 300 most recent values regardless of how much earlier history exists --
the only contribution from the dropped older bars would have been
< (1 - 2/(period+1))^230 ~ 1e-15 of the current EWM value, which sinks below
float64 precision.

This file LOCKS that claim by walking 500 bars of synthetic OHLCV through each
strategy and asserting full-slice == windowed-slice signal for every bar past
the window-warm-up.

Out of scope: xgboost_classifier and lstm_model -- both load model artifacts
that aren't checked in and would slow these unit tests by 10x. The end-to-end
equivalence on those is implicitly covered by the integration battery itself
(post-restart rate observation).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from research.backtest_ensemble import BacktestConfig, EnsembleBacktester
from strategies.base_strategy import Signal
from strategies.mean_reversion import MeanReversion
from strategies.moving_average_crossover import MovingAverageCrossover
from strategies.rsi_momentum import RSIMomentum
from strategies.supertrend_follow import SupertrendFollow
from strategies.vwap_bounce import VWAPBounce


# ---------------------------------------------------------------
# Synthetic OHLCV builder
# ---------------------------------------------------------------


def _make_long_ohlcv(n_bars: int = 500, seed: int = 7) -> pd.DataFrame:
    """Build a deterministic, realistic-looking 5-minute OHLCV series.

    Uses geometric Brownian motion + intraday wave so the strategies actually
    see signal-worthy moves rather than a flat line that always emits HOLD.
    """
    rng = np.random.default_rng(seed)
    # 1% annualised drift, ~25% annualised vol on a 5-min step
    # (12 bars/hour * 6.25 hrs/day * 250 days ~= 18,750 steps/year)
    sigma = 0.25 / math.sqrt(18750)
    mu = 0.10 / 18750
    log_rets = rng.normal(mu, sigma, n_bars)
    # add a slow intraday sine so SR-style entries can trigger
    wave = 0.0008 * np.sin(np.arange(n_bars) * 2 * math.pi / 75)
    log_rets += wave
    closes = 2500.0 * np.exp(np.cumsum(log_rets))
    # Build OHLC from close: open = prev close (with tiny gap), high/low = +/- 0.3% range
    opens = np.concatenate([[2500.0], closes[:-1]])
    spreads = np.abs(rng.normal(0.0, 0.0015, n_bars)) * closes
    highs = np.maximum(opens, closes) + spreads
    lows = np.minimum(opens, closes) - spreads
    volumes = rng.integers(50_000, 200_000, n_bars).astype(float)
    idx = pd.date_range("2026-01-02 09:15", periods=n_bars, freq="5min", tz="Asia/Kolkata")
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


def _signals_equivalent(
    sig_a, sig_b, *, conf_atol: float = 1e-4, price_rtol: float = 1e-4,
) -> tuple[bool, str]:
    """Compare two TradeSignal objects for numerical equivalence.

    Tolerances:
      - signal direction: must be EXACTLY identical (no fuzziness)
      - confidence: 1e-4 absolute. Numerical justification: EWM(span=W,
        adjust=False) decays the contribution of older bars at rate
        (1 - 2/(W+1))^N. For our slowest strategy (MA crossover with
        long_window=50, alpha=0.0392), 300 bars of windowed history
        leaves a tail of (1 - 0.0392)^300 ~ 7e-6 contributed by the
        dropped older bars. So a window=300 worst-case drift on
        confidence is ~1e-5; 1e-4 gives a 10x cushion. For strategies
        with shorter periods (RSI/ATR/ADX at period=14, alpha=0.133),
        the tail at 300 bars is (1 - 0.133)^300 ~ 4e-19 -- below
        float precision -- so they pass at any reasonable tolerance.
      - stop_loss / take_profit: 1e-4 relative (1 basis point). These
        are derived from ATR which is EWM-based; same justification.
    """
    if sig_a.signal != sig_b.signal:
        return False, f"signal: {sig_a.signal} != {sig_b.signal}"
    if not math.isclose(sig_a.confidence, sig_b.confidence, abs_tol=conf_atol, rel_tol=1e-9):
        return False, (
            f"confidence: {sig_a.confidence} != {sig_b.confidence} "
            f"(diff={abs(sig_a.confidence - sig_b.confidence):.2e})"
        )
    if sig_a.signal != Signal.HOLD:
        if sig_a.stop_loss is not None and sig_b.stop_loss is not None:
            if not math.isclose(sig_a.stop_loss, sig_b.stop_loss, rel_tol=price_rtol, abs_tol=1e-6):
                return False, f"stop_loss: {sig_a.stop_loss} != {sig_b.stop_loss}"
        if sig_a.take_profit is not None and sig_b.take_profit is not None:
            if not math.isclose(sig_a.take_profit, sig_b.take_profit, rel_tol=price_rtol, abs_tol=1e-6):
                return False, f"take_profit: {sig_a.take_profit} != {sig_b.take_profit}"
    return True, ""


def _walk_and_compare(strategy, df: pd.DataFrame, *, window: int = 300, start: int = 350):
    """For every bar from `start` to len(df), call generate_signal on the
    full prefix slice and on the last-`window`-bar slice, and assert the
    returned TradeSignal is numerically equivalent.

    `start` must be >= window so the full and windowed prefixes can differ
    in length (else the test is trivially true and proves nothing).
    """
    assert start >= window, "start must be >= window or the test is trivial"
    assert len(df) > start, "df must have more bars than start"
    mismatches = []
    for i in range(start, len(df)):
        full = df.iloc[: i + 1]
        windowed = df.iloc[i + 1 - window : i + 1]
        # Sanity: full is strictly larger than windowed past start
        assert len(full) > len(windowed)
        assert len(windowed) == window
        sig_full = strategy.generate_signal(full, "TEST")
        sig_win = strategy.generate_signal(windowed, "TEST")
        ok, reason = _signals_equivalent(sig_full, sig_win)
        if not ok:
            mismatches.append((i, df.index[i], reason))
    return mismatches


# ---------------------------------------------------------------
# Default value test
# ---------------------------------------------------------------


class TestStrategyHistoryWindowDefault:
    def test_default_value_is_300(self):
        """The default 300 is calibrated for 5x XGBoost (60) and ~21x RSI/ATR (14).
        Changing the default is a numerical-equivalence risk and must be deliberate."""
        cfg = BacktestConfig()
        assert cfg.strategy_history_window == 300

    def test_field_is_int(self):
        cfg = BacktestConfig()
        assert isinstance(cfg.strategy_history_window, int)

    def test_can_override(self):
        cfg = BacktestConfig(strategy_history_window=500)
        assert cfg.strategy_history_window == 500


# ---------------------------------------------------------------
# Strategy-level equivalence (pure-Python strategies only)
# ---------------------------------------------------------------


class TestRSIMomentumEquivalence:
    def test_full_vs_windowed_signal_identical(self):
        df = _make_long_ohlcv(500)
        # period=14, so EWM decay length ~70 bars. window=300 is 21x that.
        strat = RSIMomentum({"period": 14, "trend_filter_pct": None})
        mismatches = _walk_and_compare(strat, df, window=300, start=350)
        assert not mismatches, f"first 3 mismatches: {mismatches[:3]}"


class TestMovingAverageCrossoverEquivalence:
    def test_full_vs_windowed_signal_identical(self):
        df = _make_long_ohlcv(500)
        # SMA(50) is exact O(window) — needs at least 50 bars in the window.
        strat = MovingAverageCrossover({"short_window": 10, "long_window": 50})
        mismatches = _walk_and_compare(strat, df, window=300, start=350)
        assert not mismatches, f"first 3 mismatches: {mismatches[:3]}"


class TestMeanReversionEquivalence:
    def test_full_vs_windowed_signal_identical(self):
        df = _make_long_ohlcv(500)
        strat = MeanReversion()
        mismatches = _walk_and_compare(strat, df, window=300, start=350)
        assert not mismatches, f"first 3 mismatches: {mismatches[:3]}"


class TestSupertrendEquivalence:
    def test_full_vs_windowed_signal_identical(self):
        df = _make_long_ohlcv(500)
        # period=10, ATR/ADX both EWM-based -> well-converged in 300 bars.
        strat = SupertrendFollow({"period": 10, "trend_filter_pct": None})
        mismatches = _walk_and_compare(strat, df, window=300, start=350)
        assert not mismatches, f"first 3 mismatches: {mismatches[:3]}"


class TestVWAPBounceEquivalence:
    def test_full_vs_windowed_signal_identical(self):
        df = _make_long_ohlcv(500)
        # VWAP resets daily; only the current session matters. 300 bars covers
        # ~4 sessions of 5-min bars (75/day), so VWAP and rolling vol windows
        # are both fully populated.
        strat = VWAPBounce()
        mismatches = _walk_and_compare(strat, df, window=300, start=350)
        assert not mismatches, f"first 3 mismatches: {mismatches[:3]}"


# ---------------------------------------------------------------
# _merge_bars window behavior
# ---------------------------------------------------------------


class TestMergeBarsWindow:
    """The fix lives in EnsembleBacktester._merge_bars. These tests pin the
    shape contract: slices are bounded by strategy_history_window."""

    def _stub_backtester(self, window: int) -> EnsembleBacktester:
        # Construct without going through the heavy data_handler / feature
        # engine init paths that the real entrypoint uses. We only need
        # _merge_bars + self.bt for these tests.
        bt = EnsembleBacktester.__new__(EnsembleBacktester)
        bt.bt = BacktestConfig(strategy_history_window=window)
        return bt

    def test_slice_is_bounded_by_window(self):
        bt = self._stub_backtester(window=50)
        df = _make_long_ohlcv(200)
        market_data = {"AAA": df}
        out = list(bt._merge_bars(market_data))
        assert len(out) == 200
        # Past the window, slice length must equal window exactly
        for i, (ts, sym, bar, slice_df) in enumerate(out):
            if i + 1 <= 50:
                assert len(slice_df) == i + 1, (
                    f"warm-up: bar {i} slice should be {i+1}, got {len(slice_df)}"
                )
            else:
                assert len(slice_df) == 50, (
                    f"steady-state: bar {i} slice should be 50, got {len(slice_df)}"
                )

    def test_slice_is_tail_of_history(self):
        """The slice must be the LAST `window` bars, not the first."""
        bt = self._stub_backtester(window=10)
        df = _make_long_ohlcv(50)
        market_data = {"AAA": df}
        out = list(bt._merge_bars(market_data))
        ts, sym, bar, slice_df = out[-1]
        assert len(slice_df) == 10
        # Last row of the slice equals the bar itself
        assert slice_df.index[-1] == ts
        assert slice_df.iloc[-1]["close"] == bar["close"]
        # Slice ends at the bar's position
        assert slice_df.index[-1] == df.index[-1]
        # Slice starts 10 bars before the end
        assert slice_df.index[0] == df.index[-10]

    def test_window_one_yields_single_row_slice(self):
        bt = self._stub_backtester(window=1)
        df = _make_long_ohlcv(20)
        out = list(bt._merge_bars({"AAA": df}))
        for ts, sym, bar, slice_df in out:
            assert len(slice_df) == 1
            assert slice_df.iloc[-1]["close"] == bar["close"]

    def test_window_larger_than_history_returns_full_prefix(self):
        bt = self._stub_backtester(window=10_000)
        df = _make_long_ohlcv(50)
        out = list(bt._merge_bars({"AAA": df}))
        for i, (ts, sym, bar, slice_df) in enumerate(out):
            assert len(slice_df) == i + 1  # never capped, full prefix

    def test_zero_or_negative_window_falls_back_to_min_one(self):
        """Defensive: a misconfigured window must never produce an empty slice
        (which would crash every strategy). The runtime guard floors it to 1."""
        bt = self._stub_backtester(window=0)
        df = _make_long_ohlcv(5)
        out = list(bt._merge_bars({"AAA": df}))
        for ts, sym, bar, slice_df in out:
            assert len(slice_df) == 1
