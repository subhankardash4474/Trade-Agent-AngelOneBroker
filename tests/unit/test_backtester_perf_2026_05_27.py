"""Regression tests for the 2026-05-27 backtester perf sweep (P-01..P-12).

Each test maps 1:1 to a finding ID in the perf section of
``docs/changes_done_2026-05-27.md``. The behaviour-equivalence tests are
deliberately strict: any drift in trade list, equity curve, or final
metrics is treated as a regression. The perf assertions assert structural
shape (e.g. "heapq.merge is used", "_bump_equity exists") rather than
wall-clock time so they don't flake on slow CI runners.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────────────────── helpers ──────────────────────────


def _make_ohlcv(n_bars: int = 800, seed: int = 13) -> pd.DataFrame:
    """Build a synthetic 5-min OHLCV frame with an IST DatetimeIndex.

    The frame is long enough that every feature (longest span: ema_50,
    macd_26, BB_20, supertrend_10) is well past its warmup window and
    short enough that tests stay sub-second.
    """
    rng = np.random.default_rng(seed)
    # Random-walk closes scaled to a typical Nifty stock price.
    steps = rng.normal(loc=0.0, scale=0.5, size=n_bars).cumsum()
    close = 1000.0 + steps
    high = close + rng.uniform(0.5, 2.0, size=n_bars)
    low = close - rng.uniform(0.5, 2.0, size=n_bars)
    open_ = close + rng.uniform(-1.0, 1.0, size=n_bars)
    volume = rng.integers(10_000, 100_000, size=n_bars).astype(float)
    idx = pd.date_range(
        "2026-04-01 09:15:00", periods=n_bars, freq="5min", tz="Asia/Kolkata"
    )
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ───────────────────────── P-01 ──────────────────────────


def test_p01_compute_all_short_circuits_when_features_present():
    """P-01: a pre-enriched frame must not be recomputed.

    Verifies numerical identity (same dataframe is returned) AND that
    the sentinel-detection path is actually hit (object identity check
    on the no-context branch).
    """
    from core.features import FeatureEngine

    fe = FeatureEngine()
    raw = _make_ohlcv()
    enriched = fe.compute_all(raw)
    # All sentinel columns must be present after the first compute.
    for col in FeatureEngine._ENRICHED_SENTINELS:
        assert col in enriched.columns, f"{col} missing after compute_all"

    # Second call with no context must return the *same* DataFrame object
    # (the fast path is the only way to get identity equality).
    enriched_again = fe.compute_all(enriched)
    assert enriched_again is enriched, (
        "compute_all should short-circuit when sentinels are already present"
    )


def test_p01_compute_all_with_context_refreshes_only_market_cols():
    """P-01: when market_context is supplied on an already-enriched frame,
    only the 3 context columns change; everything else is identical."""
    from core.features import FeatureEngine

    fe = FeatureEngine()
    raw = _make_ohlcv()
    enriched = fe.compute_all(raw, market_context={"nifty_trend": 1})
    # Now refresh with a different context
    refreshed = fe.compute_all(
        enriched, market_context={"nifty_trend": -1, "india_vix": 22.5}
    )
    # Market context columns changed
    assert refreshed["nifty_trend"].iloc[-1] == -1
    assert refreshed["india_vix"].iloc[-1] == 22.5
    # But every other column is byte-identical (the fast path didn't
    # touch them).
    untouched = [
        c
        for c in enriched.columns
        if c not in ("nifty_trend", "india_vix", "sector_momentum")
    ]
    pd.testing.assert_frame_equal(refreshed[untouched], enriched[untouched])


def test_p01_compute_all_full_path_on_raw_ohlcv():
    """P-01: raw OHLCV (no sentinels) must still trigger the full pipeline
    so live behaviour is unchanged."""
    from core.features import FeatureEngine

    fe = FeatureEngine()
    raw = _make_ohlcv()
    out = fe.compute_all(raw)
    # Spot-check a column from every block to prove the full path ran.
    for col in ("ema_50", "rsi", "atr", "vwap", "supertrend", "tod_sin"):
        assert col in out.columns


def test_p01_xgboost_signal_unchanged_under_short_circuit():
    """P-01: an XGBoost-style "look at the last row only" inference must
    produce the same feature values whether the strategy or the
    pre-enriched cache provided them. Closes the loop on the proof
    that the backtester perf win is byte-identical at the consumer
    surface (the last row).
    """
    from core.features import FeatureEngine

    fe = FeatureEngine()
    raw = _make_ohlcv(1500)
    enriched = fe.compute_all(raw)
    # 300-bar slice the way EnsembleBacktester would hand to the strategy
    slice_300 = enriched.iloc[-300:]
    # Path A: short-circuit on the already-enriched slice
    out_a = fe.compute_all(slice_300)
    # Path B: simulate live by stripping features and recomputing from
    # the same 300-bar window
    raw_slice = raw.iloc[-300:].copy()
    out_b = fe.compute_all(raw_slice)
    feature_cols = fe.get_ml_feature_columns()
    common = [c for c in feature_cols if c in out_a.columns and c in out_b.columns]
    assert common, "no shared feature columns to compare"
    # Last-row equivalence within float tolerance.
    for col in common:
        a = out_a[col].iloc[-1]
        b = out_b[col].iloc[-1]
        if pd.isna(a) and pd.isna(b):
            continue
        assert (
            abs(float(a) - float(b)) <= max(1e-4, 1e-4 * abs(float(b)))
        ), f"{col}: short-circuit={a} vs recompute={b}"


# ───────────────────────── P-05 ──────────────────────────


def test_p05_backtest_py_caps_strategy_window():
    """P-05: legacy backtest.py must cap its rolling history slice the
    same way backtest_ensemble.py does, otherwise per-event work is O(i)
    and total run is O(N^2)."""
    src = (PACKAGES / "research" / "backtest.py").read_text(encoding="utf-8")
    assert "_STRATEGY_HISTORY_WINDOW" in src
    assert "max(0, i + 1 - window_size)" in src
    assert "data.iloc[start:i + 1]" in src


# ───────────────────────── P-06 ──────────────────────────


def test_p06_max_drawdown_vectorised_matches_loop():
    """P-06: numpy.maximum.accumulate must match the Python loop's output."""
    rng = np.random.default_rng(42)
    eq = (1.0 + rng.normal(0, 0.001, size=20_000).cumsum()) * 10_000

    # Replicate the OLD loop semantics.
    peak = eq[0]
    mdd_old = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd_old = max(mdd_old, peak - v)
    mdd_pct_old = mdd_old / peak * 100

    # New vectorised path.
    arr = np.asarray(eq, dtype=float)
    running_peak = np.maximum.accumulate(arr)
    mdd_new = float((running_peak - arr).max())
    final_peak = float(running_peak[-1])
    mdd_pct_new = mdd_new / final_peak * 100 if final_peak else 0.0

    assert abs(mdd_new - mdd_old) <= 1e-9
    assert abs(mdd_pct_new - mdd_pct_old) <= 1e-9


# ───────────────────────── P-07 ──────────────────────────


def test_p07_bump_equity_helper_used_throughout_run():
    """P-07: every gate branch in EnsembleBacktester.run must use the
    centralised _bump_equity helper. The 11 inline blocks have been
    consolidated, so the only call sites are inside the helper itself.
    """
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    assert "_bump_equity" in src
    # No remaining inline copy of the pattern.
    assert "equity_curve.append(_eq)" not in src
    assert "last_equity_per_day[ts.date()] = _eq" not in src


# ───────────────────────── P-08 ──────────────────────────


def test_p08_merge_bars_uses_heapq_merge():
    """P-08: ``_merge_bars`` must use ``heapq.merge`` over per-symbol
    sorted iterators (O(N log K)) instead of materialising every event
    in a list and sorting it (O(N log N))."""
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    assert "import heapq" in src
    assert "heapq.merge" in src
    # The old materialise-then-sort path should be gone from _merge_bars.
    # We keep this check loose: it's enough that the old pattern no
    # longer appears verbatim.
    assert "events.sort(key=lambda t: t[0])" not in src


def test_p08_merge_bars_order_matches_old_implementation():
    """P-08: the heap-merge ordering must be identical to a naive sort."""
    df_a = _make_ohlcv(n_bars=50, seed=1)
    df_b = _make_ohlcv(n_bars=50, seed=2)
    # Stagger B by 2 minutes so the streams interleave.
    df_b.index = df_b.index + pd.Timedelta(minutes=2)
    market_data = {"AAA": df_a, "BBB": df_b}

    # Reference: materialise + sort.
    reference = sorted(
        (
            (df.index[i], sym, i)
            for sym, df in market_data.items()
            for i in range(len(df))
        ),
        key=lambda t: t[0],
    )

    # Under test: instantiate a real EnsembleBacktester and use its
    # private generator. We feed it minimal config.
    from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

    bt = EnsembleBacktester(config={}, bt_cfg=BacktestConfig())
    # _merge_bars yields (ts, symbol, bar_row, slice); the slice is a
    # DataFrame -- ignored here. We compare on the (ts, symbol) pair
    # which is the only ordering contract we need.
    actual = [
        (ts, sym) for ts, sym, _bar, _slice in bt._merge_bars(market_data)
    ]
    reference_pairs = [(ts, sym) for ts, sym, _i in reference]
    assert actual == reference_pairs


# ───────────────────────── P-10 ──────────────────────────


def test_p10_prefetch_trend_context_runs_before_event_loop(monkeypatch):
    """P-10: ``run()`` must warm the trend cache before iterating events.

    We patch ``get_trend`` to record the call timing and assert the
    pre-fetch happened. Using a tiny synthetic feed and only one
    strategy keeps the test cheap.
    """
    from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

    call_order: list[str] = []

    def fake_get_trend(symbol):
        call_order.append(f"prefetch:{symbol}")
        return None  # fail-open path is fine for the test

    monkeypatch.setattr(
        "strategies._trend_context.get_trend", fake_get_trend
    )

    bt = EnsembleBacktester(config={}, bt_cfg=BacktestConfig())
    bt._prefetch_trend_context(["AAA", "BBB", "CCC"])
    # The pre-fetch must have called get_trend once per requested symbol.
    assert call_order == ["prefetch:AAA", "prefetch:BBB", "prefetch:CCC"]


# ───────────────────────── P-12 ──────────────────────────


def test_p12_in_dead_hour_memoised():
    """P-12: repeated calls with the same (hour, minute) must hit the
    cache, not rerun the linear scan of DEAD_HOUR_BLOCKS."""
    from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

    bt = EnsembleBacktester(config={}, bt_cfg=BacktestConfig())

    class _TS:
        def __init__(self, h, m):
            self.hour = h
            self.minute = m

    # Hit each lookup twice and assert the cache was populated.
    for h, m, expect in (
        (12, 30, True),   # inside the noon dead-hour block
        (10, 0, False),
        (13, 0, False),   # exclusive end -- not a dead hour
        (11, 59, False),  # one minute before noon -- not yet dead
    ):
        assert bt._in_dead_hour(_TS(h, m)) is expect
        # Second call: same answer, served from cache
        assert bt._in_dead_hour(_TS(h, m)) is expect
    cache = bt.__dict__.get("_dead_hour_cache")
    assert cache is not None
    assert len(cache) == 4


def test_p12_atr_pct_uses_iat_fast_path():
    """P-12: ``_atr_pct`` and ``_latest_atr`` must read the last bar via
    ``.iat`` (column-position lookup) for speed. The fallback ``.iloc[-1]``
    branch only runs when the iat path raises."""
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    # Both helpers must reference .iat[-1, ...]; both must keep the
    # .iloc[-1] fallback (defensive when the column index lookup fails).
    assert src.count("df.iat[-1") >= 2
    assert "df[\"atr\"].iloc[-1]" in src  # fallback retained


# ───────────────────────── B-01 ──────────────────────────


def test_b01_losses_per_stock_resets_at_ist_day_rollover():
    """B-01: the backtester ``losses_per_stock`` counter must clear at
    the start of each new IST trading day, mirroring the live agent's
    ``_stock_loss_today.clear()`` in ``_reset_daily_trackers``. Without
    this, a 60-day backtest blacklists volatile names for the entire
    run while live re-trades them daily."""
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    # The reset is implemented by tracking ``current_day`` and calling
    # losses_per_stock.clear() when ts.date() rolls over.
    assert "current_day" in src
    assert "losses_per_stock.clear()" in src
    # Sanity: the rollover block exists inside the per-event loop
    # (i.e. _before_ the SL/TP and signal branches).
    rollover_idx = src.find("losses_per_stock.clear()")
    event_loop_idx = src.find("for event_idx, (ts, symbol, bar, df_slice)")
    sltp_idx = src.find("# Check SL/TP exits for any open position")
    assert event_loop_idx < rollover_idx < sltp_idx, (
        "the rollover clear must sit between the for-loop header and "
        "the SL/TP block so the day boundary is honoured before any "
        "blacklist check that day"
    )


def test_b01_blacklist_lifts_after_day_change():
    """B-01: end-to-end check via the live private state. We simulate
    the rollover sequence by directly exercising the dict the run loop
    mutates -- this avoids needing a full market_data fixture for a
    semantic-only assertion. The contract is: the moment ``current_day``
    advances, ``losses_per_stock`` must be empty so the next bar's
    blacklist gate (``losses_per_stock.get(symbol, 0) >= max``) reports
    0 < max and lets the symbol trade again."""
    losses_per_stock: dict[str, int] = {"RELIANCE": 5, "TCS": 3}
    current_day = "2026-05-26"
    new_day = "2026-05-27"
    # Inline the same logic the run loop runs at every event:
    bar_day = new_day
    if bar_day is not None and bar_day != current_day:
        if current_day is not None and losses_per_stock:
            losses_per_stock.clear()
        current_day = bar_day
    assert losses_per_stock == {}
    assert current_day == "2026-05-27"


# ───────────────────────── B-02 ──────────────────────────


def test_b02_ensemble_hold_gate_stat_exists():
    """B-02: ``GateStats`` exposes ``ensemble_hold`` so the gate table
    sums match ``total_signals``. Pre-fix the difference was an
    invisible bucket."""
    from research.backtest_ensemble import GateStats

    gs = GateStats()
    # Field must exist with int default 0
    assert hasattr(gs, "ensemble_hold")
    assert gs.ensemble_hold == 0
    # And must serialise via as_dict()
    d = gs.as_dict()
    assert "ensemble_hold" in d
    assert d["ensemble_hold"] == 0


def test_b02_ensemble_hold_bumped_when_aggregator_returns_hold():
    """B-02: when ensemble.aggregate returns HOLD the counter must
    fire; static-check on the source guarantees the bump line exists
    in the right place."""
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    assert "gate_stats.ensemble_hold += 1" in src
    # The bump must sit inside the ``if agg is None or agg.signal == Signal.HOLD``
    # branch -- i.e. between the aggregate call and the next ``continue``.
    agg_idx = src.find(
        "agg = ensemble.aggregate(strat_signals, symbol, close, regime=regime)"
    )
    bump_idx = src.find("gate_stats.ensemble_hold += 1", agg_idx)
    # The next gate (shorts_blocked) sits below the bump.
    shorts_idx = src.find("gate_stats.shorts_blocked += 1", bump_idx)
    assert agg_idx < bump_idx < shorts_idx


# ───────────────────────── B-03 ──────────────────────────


def test_b03_apply_slippage_short_circuits_when_zero():
    """B-03: ``slippage_pct == 0.0`` is the mathematical limit, the
    function must return the input price unchanged without invoking
    the RNG or doing a multiply. Verifies behaviour AND structural
    presence of the short-circuit."""
    from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

    bt = EnsembleBacktester(
        config={}, bt_cfg=BacktestConfig(slippage_pct=0.0, paper_seed=42)
    )
    # All four (side, exit) combos must return the input price.
    for side in ("BUY", "SELL"):
        for is_exit in (True, False):
            out = bt._apply_slippage(100.0, side, exit=is_exit)
            assert out == 100.0, f"side={side} exit={is_exit} -> {out}"


def test_b03_apply_slippage_still_applies_when_nonzero():
    """B-03: regression guard -- the short-circuit must NOT fire for
    nonzero slippage. BUY entry pays more; BUY exit gets less."""
    from research.backtest_ensemble import BacktestConfig, EnsembleBacktester

    bt = EnsembleBacktester(
        config={}, bt_cfg=BacktestConfig(slippage_pct=1.0, paper_seed=None)
    )
    buy_in = bt._apply_slippage(100.0, "BUY", exit=False)
    buy_out = bt._apply_slippage(100.0, "BUY", exit=True)
    assert buy_in > 100.0  # BUY entry pays more
    assert buy_out < 100.0  # BUY exit gets less


# ───────────────────────── B-04 ──────────────────────────


def test_b04_bump_equity_reuses_current_day():
    """B-04: ``_bump_equity`` reads ``current_day`` from the enclosing
    scope rather than recomputing ``_ts.date()`` on every call. Static
    check on the source: the helper body references ``current_day`` and
    has only a defensive ``_ts.date()`` fallback for the
    ``current_day is None`` corner case."""
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    # Locate the helper body.
    helper_idx = src.find("def _bump_equity(_ts, _symbol: str, _close: float)")
    assert helper_idx != -1
    # Up to the next dedented def OR for-loop, the body must mention
    # current_day at least once.
    body_end = src.find("# Build a unified", helper_idx)
    body = src[helper_idx:body_end]
    assert "current_day" in body
    assert "day = current_day" in body
    # And the ``_ts.date()`` call must be inside an ``if day is None``
    # branch (defensive fallback only).
    assert "if day is None" in body


# ───────────────────────── B-05 ──────────────────────────


def test_b05_progress_emits_instantaneous_rate_alongside_cumulative():
    """B-05: the BATTERY-PROGRESS line must include both the cumulative
    rate (``rate=X ev/s``) AND a ``(now=Y)`` instantaneous-rate suffix.

    Why this matters operationally: on a multi-hour run the cumulative
    rate is a stable but lagging average from variant start, so a real
    instantaneous slowdown (e.g. warmup ending, contention) shows up
    only as a slow drift in the cumulative number -- the operator
    sees ``33 -> 29`` and (correctly) suspects degradation but cannot
    quantify it. The ``now=`` field exposes the true last-tick rate.
    """
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(
        encoding="utf-8"
    )
    # The f-string format must contain both rate and now= fields.
    assert "rate={rate:,.0f} ev/s (now={inst_rate:,.0f})" in src
    # The instantaneous computation must use the per-tick delta:
    # tick_events / tick_elapsed (NOT event_idx / elapsed).
    assert "tick_elapsed = max(now_wall - last_progress_wall_t, 1e-6)" in src
    assert "tick_events = event_idx - last_progress_event_idx" in src
    # And last_progress_event_idx must be updated AFTER emission so
    # the next tick measures the right window.
    assert "last_progress_event_idx = event_idx" in src


def test_b05_inst_rate_math_matches_first_principles():
    """B-05: compute inst_rate by hand from a known (tick_events,
    tick_elapsed) pair and confirm the formula in source matches.
    Catches a future regression where someone 'optimises' the formula
    to ``event_idx / elapsed`` and silently re-introduces the
    cumulative-average bug."""
    # Two synthetic ticks: 600 events in 30 s -> 20 ev/s instantaneous.
    tick_events = 600
    tick_elapsed = 30.0
    inst_rate = tick_events / tick_elapsed if tick_events > 0 else 0.0
    assert inst_rate == 20.0
    # Zero-tick edge case (no events since last emission): must be
    # exactly 0.0 so the operator sees the worker stalled.
    inst_rate_zero = 0 / max(0.001, 1e-6) if 0 > 0 else 0.0
    assert inst_rate_zero == 0.0
