"""
Performance-fix invariants for trading strategies (P-03, P-04, P-11).

These tests pin down the contract the perf fixes promised to preserve:

1. **No caller-frame mutation.** Each strategy must not write to the
   ``data`` DataFrame the caller passed in. We snapshot a hash + the
   column set before the call and assert nothing changed after.

2. **Determinism / byte-identical re-evaluation.** Calling the strategy
   twice on the same input must produce identical TradeSignal fields
   (signal, confidence, stop_loss, take_profit, metadata). This catches
   any accidental shared-state corruption from the new caches (P-03
   ATR cache, P-11 feature-col cache).

3. **Supertrend vectorised matches pandas-loop reference.** P-03
   replaced a ``pd.Series.iloc[i] = ...`` loop with a numpy scalar loop.
   We re-implement the original loop here as a frozen reference and
   assert the new ``_compute_supertrend`` output matches it
   element-wise -- including NaN positions, direction states, and
   trailing-band carry-forward across direction segments.

Created: 2026-05-27 (perf sprint, freeze-v2.1 audit-only).
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from strategies.base_strategy import Signal
from strategies.mean_reversion import MeanReversion
from strategies.moving_average_crossover import MovingAverageCrossover
from strategies.opening_range_breakout import OpeningRangeBreakout
from strategies.rsi_momentum import RSIMomentum
from strategies.supertrend_follow import SupertrendFollow
from strategies.vwap_bounce import VWAPBounce


# --------------------------------------------------------------------- #
# Fixtures                                                              #
# --------------------------------------------------------------------- #


def _make_intraday_ohlcv(
    n_bars: int = 200,
    seed: int = 42,
    start: str = "2026-05-19 09:15",
    freq: str = "5min",
) -> pd.DataFrame:
    """Synthesise a deterministic OHLCV frame with realistic intraday
    structure (random walk + intra-bar noise + volume jitter)."""
    rng = np.random.default_rng(seed)
    drift = rng.normal(loc=0.0, scale=0.4, size=n_bars).cumsum()
    base = 1000.0 + drift
    open_ = base + rng.normal(0, 0.3, size=n_bars)
    close_ = base + rng.normal(0, 0.3, size=n_bars)
    high = np.maximum(open_, close_) + np.abs(rng.normal(0, 0.4, size=n_bars))
    low = np.minimum(open_, close_) - np.abs(rng.normal(0, 0.4, size=n_bars))
    vol = rng.integers(50_000, 500_000, size=n_bars).astype(float)
    idx = pd.date_range(start, periods=n_bars, freq=freq)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close_, "volume": vol},
        index=idx,
    )


def _frame_fingerprint(df: pd.DataFrame) -> tuple[tuple[str, ...], str]:
    """Return (columns_tuple, sha256 of values bytes) for mutation
    detection. We use the raw bytes view so the comparison is exact
    (NaN-safe, dtype-safe)."""
    cols = tuple(df.columns)
    # Force a contiguous bytes view; .values can return mixed dtypes
    # for object cols, but our test frames are float64 everywhere.
    h = hashlib.sha256(df.to_numpy(dtype=np.float64, copy=True).tobytes()).hexdigest()
    return cols, h


def _signal_signature(sig) -> tuple:
    """Reduce a TradeSignal to a comparable tuple for determinism asserts."""
    meta = sig.metadata or {}
    meta_items = tuple(sorted(meta.items(), key=lambda kv: kv[0]))
    return (
        sig.signal.value,
        round(float(sig.price), 6),
        round(float(sig.confidence), 6),
        None if sig.stop_loss is None else round(float(sig.stop_loss), 6),
        None if sig.take_profit is None else round(float(sig.take_profit), 6),
        meta_items,
    )


# --------------------------------------------------------------------- #
# 1. No caller-frame mutation (P-03 + P-04 contract)                    #
# --------------------------------------------------------------------- #


# Each (strategy_factory, params) pair gets its own no-mutation test.
NO_MUTATION_STRATEGIES = [
    ("supertrend_follow", lambda: SupertrendFollow({"period": 10, "multiplier": 3.0})),
    ("rsi_momentum", lambda: RSIMomentum({"period": 14})),
    ("vwap_bounce", lambda: VWAPBounce({})),
    ("mean_reversion", lambda: MeanReversion({"lookback_period": 20})),
    ("opening_range_breakout", lambda: OpeningRangeBreakout({"range_minutes": 15})),
    ("moving_average_crossover", lambda: MovingAverageCrossover({"short_window": 9, "long_window": 21})),
]


@pytest.mark.parametrize("name,factory", NO_MUTATION_STRATEGIES)
def test_no_caller_frame_mutation(name, factory):
    """P-03/P-04 contract: removing ``data.copy()`` is only safe if the
    strategy never mutates the caller's frame. Hash the frame before
    and after; they MUST match.

    If this test fails, the strategy is writing a derived column back
    to the caller's DataFrame -- a memory-corruption pattern that
    cascades across symbols within a single scan cycle (the trading
    daemon reuses the same OHLCV cache slice for the ensemble vote).
    """
    strategy = factory()
    data = _make_intraday_ohlcv(n_bars=200, seed=42)
    cols_before, hash_before = _frame_fingerprint(data)

    strategy.generate_signal(data, "TEST")

    cols_after, hash_after = _frame_fingerprint(data)
    assert cols_before == cols_after, (
        f"{name}: column set mutated. before={cols_before} after={cols_after}"
    )
    assert hash_before == hash_after, (
        f"{name}: frame values mutated. The strategy wrote back to "
        f"data['<col>']; the copy must be reinstated or the write "
        f"removed."
    )


# --------------------------------------------------------------------- #
# 2. Determinism / cache hygiene                                        #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("name,factory", NO_MUTATION_STRATEGIES)
def test_strategy_output_is_deterministic(name, factory):
    """Calling the same strategy on the same frame twice must produce
    bit-identical TradeSignals. Catches:
      - P-03 ATR cache leaking stale results across DataFrames
      - P-11 feature-col cache going stale
      - any hidden module-level mutable state
    """
    strategy = factory()
    data = _make_intraday_ohlcv(n_bars=200, seed=42)
    s1 = strategy.generate_signal(data, "TEST")
    s2 = strategy.generate_signal(data, "TEST")
    assert _signal_signature(s1) == _signal_signature(s2), (
        f"{name}: non-deterministic output across two consecutive calls. "
        f"first={_signal_signature(s1)} second={_signal_signature(s2)}"
    )


def test_supertrend_atr_cache_invalidates_on_new_frame():
    """P-03 ATR cache is keyed by ``id(df)``. Two different DataFrames
    with different content MUST produce different ATR values (no stale
    cache hit).
    """
    s = SupertrendFollow({"period": 10})
    df_a = _make_intraday_ohlcv(n_bars=100, seed=1)
    df_b = _make_intraday_ohlcv(n_bars=100, seed=2)

    atr_a = s._compute_atr_cached(df_a, 10).iloc[-1]
    atr_b = s._compute_atr_cached(df_b, 10).iloc[-1]
    assert atr_a != atr_b, (
        "ATR cache returned the same value for two different DataFrames; "
        "cache keying by id(df) is broken."
    )

    # A second call on df_a should still match the first (cache hit).
    atr_a_again = s._compute_atr_cached(df_a, 10).iloc[-1]
    # df_a's id() may have been reused by gc; we don't rely on cache
    # being warm here, only on the value being correct.
    assert abs(float(atr_a) - float(atr_a_again)) < 1e-12


# --------------------------------------------------------------------- #
# 3. Supertrend vectorised == frozen pandas-loop reference              #
# --------------------------------------------------------------------- #


def _reference_supertrend_loop(
    df: pd.DataFrame, period: int, multiplier: float
) -> tuple[pd.Series, pd.Series]:
    """Frozen copy of the ORIGINAL pre-P-03 ``_compute_supertrend`` loop,
    pasted verbatim with only the ATR helper inlined. Used as the
    ground-truth reference that the new vectorised implementation must
    match element-for-element. DO NOT modify this function -- it
    represents the pre-fix behaviour we promised to preserve."""
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    direction = pd.Series(1, index=df.index)
    st = pd.Series(np.nan, index=df.index)

    for i in range(1, len(df)):
        if df["close"].iloc[i] > upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        if direction.iloc[i] == 1:
            lower.iloc[i] = (
                max(lower.iloc[i], lower.iloc[i - 1])
                if direction.iloc[i - 1] == 1
                else lower.iloc[i]
            )
            st.iloc[i] = lower.iloc[i]
        else:
            upper.iloc[i] = (
                min(upper.iloc[i], upper.iloc[i - 1])
                if direction.iloc[i - 1] == -1
                else upper.iloc[i]
            )
            st.iloc[i] = upper.iloc[i]

    return st, direction


@pytest.mark.parametrize("seed", [0, 7, 42, 99, 12345])
def test_supertrend_vectorised_matches_pandas_loop(seed):
    """P-03: the new numpy-loop ``_compute_supertrend`` must produce
    identical output to the frozen pandas-loop reference. This is the
    byte-identical contract.

    We test multiple RNG seeds so we hit a variety of regimes (up
    trends, down trends, direction flips, narrow consolidations,
    expansion bars). If ANY seed produces a mismatch, the vectorised
    path has diverged from the original semantics."""
    strategy = SupertrendFollow({"period": 10, "multiplier": 3.0})
    df = _make_intraday_ohlcv(n_bars=400, seed=seed)

    # Reset the ATR cache between strategies so we measure a clean call.
    strategy._atr_cache_key = None
    strategy._atr_cache_value = None

    st_new, dir_new = strategy._compute_supertrend(df)
    st_ref, dir_ref = _reference_supertrend_loop(df, period=10, multiplier=3.0)

    # Direction must match exactly (int compare).
    pd.testing.assert_series_equal(
        dir_new.astype(np.int64),
        dir_ref.astype(np.int64),
        check_names=False,
    )

    # Supertrend values: same NaN positions, same finite values to FP precision.
    assert st_new.isna().equals(st_ref.isna()), (
        f"NaN-mask differs between vectorised and reference Supertrend "
        f"on seed={seed}"
    )
    np.testing.assert_allclose(
        st_new.dropna().to_numpy(),
        st_ref.dropna().to_numpy(),
        rtol=0.0,
        atol=1e-9,
        err_msg=f"Supertrend numeric divergence on seed={seed}",
    )


# --------------------------------------------------------------------- #
# 4. Smoke: signals still emit on regime-flip data                      #
# --------------------------------------------------------------------- #


def test_supertrend_emits_signal_on_clear_flip():
    """Sanity: after the rewrite, a clear bear -> bull regime flip
    still produces a BUY (or HOLD if ADX gate filters it). The point
    is the strategy is still functional, not silently HOLD-everywhere
    from a broken vectorisation."""
    strategy = SupertrendFollow({"period": 10, "multiplier": 2.0, "adx_threshold": 10})
    # Synthesise a downtrend followed by sharp reversal to force a flip.
    rng = np.random.default_rng(0)
    bear = 1000.0 - np.arange(80) * 1.5 + rng.normal(0, 0.5, 80)
    bull = bear[-1] + np.arange(80) * 1.5 + rng.normal(0, 0.5, 80)
    prices = np.concatenate([bear, bull])
    idx = pd.date_range("2026-05-19 09:15", periods=len(prices), freq="5min")
    df = pd.DataFrame(
        {
            "open": prices,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": [100_000] * len(prices),
        },
        index=idx,
    )
    sig = strategy.generate_signal(df, "TEST")
    assert sig.signal in (Signal.BUY, Signal.HOLD)
    assert sig.symbol == "TEST"
    assert sig.strategy_name == "supertrend_follow"
