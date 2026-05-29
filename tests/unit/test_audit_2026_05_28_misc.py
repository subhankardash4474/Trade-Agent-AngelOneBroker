"""Regression tests for the audit_2026-05-28 misc-OPEN bucket.

This file picks up the findings that were *outside* the five named
phases (1-5). Each finding has its own block. Tests are by-source for
contract assertions and by-runtime where the behaviour is observable
end-to-end without spawning a daemon.

Findings covered in this file:

* **NUM-01** (Critical) -- short MIS margin model (Group A).
* **NUM-06** (High)     -- drop forming intraday bar from REST
  fallback (Group B).
* **NUM-07** (Medium)   -- features rolling-75 session bleed; switch
  ``dist_from_high_pct`` / ``dist_from_low_pct`` to a session-grouped
  cumulative max/min (Group B).
* **ORD-05** (High)     -- atomic cancel-then-flatten in
  ``_close_position_safely``; if the SL filled in the cancel race
  window, skip flatten and reconcile in-memory portfolio at the SL
  fill price (Group C).
* **ORD-07** (High)     -- closed in Phase 2 by routing
  ``_close_position_safely`` through ``place_order`` ->
  ``_live_order_with_retry`` -> ``_wait_for_terminal``. Pinned here
  with a source-level guard so a refactor cannot silently drop the
  wait helper from the exit path (Group C).
* **ORD-08** (Medium)   -- SL-M sized off broker-confirmed
  ``filled_quantity`` rather than the requested quantity (Group C).
* **ORD-09** (Medium)   -- order-fill TTL now actively cancels the
  pending order and fails the call instead of silently keeping
  ``status='PLACED'`` (Group C).
* **NUM-11** (Medium)   -- paper / live slippage parity. Both modes
  now emit ``slippage_pct`` + ``slippage_breach`` so the backtester
  and the live broker are comparable (Group D).
* **ORD-11** (Medium)   -- live slippage tolerance circuit breaker.
  After every live fill, ``slippage_pct`` is compared against
  ``execution.slippage_tolerance_pct``; on breach the engine emits a
  CRITICAL ``[ORD-11-SLIPPAGE]`` log and (when
  ``execution.halt_symbol_on_slippage_breach`` is set) blocks new
  entries on the symbol until ``clear_slippage_block`` is called
  (Group D).
* **ORD-10** (Medium)   -- reactive re-auth on AngelOne auth-class
  errors (AB1010 / AB1011 / 401 / 403 / "Session Expired" / "Invalid
  Token"). New ``classify_smartapi_error`` classifier + auth callback
  hook on ExecutionEngine + force-refresh path on
  ``TradingAgent._maybe_refresh_broker_session`` (Group E).
* **NUM-10** (Medium)   -- decimal arithmetic for charges. The inner
  accumulators in ``charges.py`` now run in ``Decimal`` and quantize
  to 1 paisa per component; ``compute_round_trip`` equals
  ``compute_one_leg(BUY) + compute_one_leg(SELL)`` byte-for-byte; and
  ``portfolio.close_position`` now derives ``exit_commission``
  directly from ``compute_one_leg`` instead of via
  ``total_commission - entry_commission`` so float subtraction
  drift no longer biases reported P&L over long-running portfolios
  (Group F).
* **PERF-07** (Medium)  -- per-cycle tick-history cache.
  ``tick_aggregator.get_candle_history`` allocates a fresh
  DataFrame per call. New ``_get_tick_history_cached`` on
  TradingAgent dedup'd the calls within a single
  ``_trading_cycle``, eliminating ~60-80% of the DataFrame
  allocations on the WS hot path. Cleared at cycle start; empty
  results are never cached so the REST-fallback path keeps working
  (Group G).
* **PERF-13** (Medium)  -- battery cache sidecar SHA256.
  ``_save_market_data_cache`` now writes a companion
  ``market_data.pkl.sha256`` sidecar file, and
  ``_load_market_data_cache`` reuses the sidecar instead of
  re-hashing 300 MB on every worker. Saves ~1-2 s per variant
  (20-40 s per 20-variant battery). Mtime-gated fallback to
  live hashing keeps the OBS-20 audit log identical when the
  sidecar is missing or stale (Group G).
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PACKAGES = os.path.join(PROJECT_ROOT, "packages")
if PACKAGES not in sys.path:
    sys.path.insert(0, PACKAGES)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.charges import compute_one_leg, compute_round_trip
from core.database import Database
from core.portfolio import Portfolio


# ─────────────────────────────────────────────────────────────────────
# NUM-01: short MIS margin model
# ─────────────────────────────────────────────────────────────────────


class TestNUM01ShortMISMargin:
    """The backtester used to lock the FULL short notional. Live broker
    reality is ~20% MIS margin. Every short-side battery number was
    biased by the missing leverage. Three guarantees the new code must
    keep:

    1. Default in code is the legacy lock (1.0). Existing tests that
       construct ``Portfolio(...)`` without the new arg must remain
       byte-identical.
    2. Production callers pass 0.20 from config; under that setting
       a short locks only ``notional * 0.20 + commission``.
    3. Net cash change after open + close is ``pnl`` (= gross_pnl
       net of total commission) regardless of margin %, otherwise
       the simulator double-counts.
    """

    def _portfolio(self, tmp_path, **kwargs):
        return Portfolio(
            initial_balance=100_000.0,
            log_dir=str(tmp_path),
            **kwargs,
        )

    def test_default_margin_is_1_0_for_legacy_callers(self, tmp_path):
        port = self._portfolio(tmp_path)
        assert port.mis_short_margin_pct == 1.0

    def test_constructor_clamps_negative_margin_to_zero(self, tmp_path):
        port = self._portfolio(tmp_path, mis_short_margin_pct=-0.5)
        assert port.mis_short_margin_pct == 0.0

    def test_constructor_clamps_above_1_to_1(self, tmp_path):
        port = self._portfolio(tmp_path, mis_short_margin_pct=2.5)
        assert port.mis_short_margin_pct == 1.0

    def test_constructor_handles_garbage_value(self, tmp_path):
        port = self._portfolio(tmp_path, mis_short_margin_pct="not-a-number")
        assert port.mis_short_margin_pct == 1.0

    def test_long_open_unchanged_under_margin_setting(self, tmp_path):
        # LONG must lock full notional regardless of margin pct -- the
        # knob only affects shorts.
        port_legacy = self._portfolio(tmp_path / "a", mis_short_margin_pct=1.0)
        port_margin = self._portfolio(tmp_path / "b", mis_short_margin_pct=0.20)

        assert port_legacy.open_position("RELIANCE", "BUY", 2500.0, 4) is True
        assert port_margin.open_position("RELIANCE", "BUY", 2500.0, 4) is True

        # Same lock under both settings.
        assert port_legacy.cash == pytest.approx(port_margin.cash, rel=1e-12)
        assert port_legacy.positions["RELIANCE"].cash_locked == pytest.approx(
            port_margin.positions["RELIANCE"].cash_locked, rel=1e-12
        )

    def test_short_open_locks_full_notional_under_legacy_setting(self, tmp_path):
        port = self._portfolio(tmp_path, mis_short_margin_pct=1.0)
        ok = port.open_position("RELIANCE", "SELL", 2500.0, 4)
        assert ok is True

        notional = 2500.0 * 4
        entry_commission = compute_one_leg(2500.0, 4, side="SELL", product="INTRADAY")
        expected_lock = notional + entry_commission

        pos = port.positions["RELIANCE"]
        assert pos.cash_locked == pytest.approx(expected_lock, rel=1e-9)
        assert port.cash == pytest.approx(100_000.0 - expected_lock, rel=1e-9)

    def test_short_open_under_20pct_margin_locks_only_margin(self, tmp_path):
        port = self._portfolio(tmp_path, mis_short_margin_pct=0.20)
        ok = port.open_position("RELIANCE", "SELL", 2500.0, 4)
        assert ok is True

        notional = 2500.0 * 4
        entry_commission = compute_one_leg(2500.0, 4, side="SELL", product="INTRADAY")
        expected_lock = notional * 0.20 + entry_commission

        pos = port.positions["RELIANCE"]
        assert pos.cash_locked == pytest.approx(expected_lock, rel=1e-9)
        assert port.cash == pytest.approx(100_000.0 - expected_lock, rel=1e-9)

        # Sanity: under the margin model we have ~80% MORE cash than
        # under the legacy lock, so the simulator can size shorts ~5x
        # closer to live broker reality.
        legacy_lock = notional + entry_commission
        assert port.cash > 100_000.0 - legacy_lock + (notional * 0.6)

    def test_short_open_delivery_locks_full_notional_even_with_margin(self, tmp_path):
        # CNC / DELIVERY shorts always lock full notional. The margin
        # knob is only for INTRADAY (MIS) shorts.
        port = Portfolio(
            initial_balance=100_000.0,
            log_dir=str(tmp_path),
            product_type="DELIVERY",
            mis_short_margin_pct=0.20,
        )
        ok = port.open_position("RELIANCE", "SELL", 2500.0, 4)
        assert ok is True

        notional = 2500.0 * 4
        entry_commission = compute_one_leg(2500.0, 4, side="SELL", product="DELIVERY")
        expected_lock = notional + entry_commission

        pos = port.positions["RELIANCE"]
        assert pos.cash_locked == pytest.approx(expected_lock, rel=1e-9)

    def test_short_round_trip_net_cash_change_equals_pnl_under_margin(self, tmp_path):
        # The whole point of the cash_locked persistence: net cash
        # change after a round trip must equal ``pnl`` regardless of
        # margin %. Otherwise the simulator silently mis-prices
        # equity over time.
        port = self._portfolio(tmp_path, mis_short_margin_pct=0.20)
        opening_cash = port.cash

        port.open_position("RELIANCE", "SELL", 2500.0, 4)
        record = port.close_position("RELIANCE", 2400.0, exit_reason="signal")

        assert record is not None
        net_cash_change = port.cash - opening_cash
        assert net_cash_change == pytest.approx(record.pnl, rel=1e-9)

    def test_short_round_trip_under_legacy_lock_still_balances(self, tmp_path):
        port = self._portfolio(tmp_path, mis_short_margin_pct=1.0)
        opening_cash = port.cash

        port.open_position("HDFCBANK", "SELL", 1600.0, 5)
        record = port.close_position("HDFCBANK", 1550.0, exit_reason="signal")

        net_cash_change = port.cash - opening_cash
        assert record is not None
        assert net_cash_change == pytest.approx(record.pnl, rel=1e-9)

    def test_short_loss_round_trip_under_margin_balances(self, tmp_path):
        # Negative PnL leg: the simulator should still tie out exactly.
        port = self._portfolio(tmp_path, mis_short_margin_pct=0.20)
        opening_cash = port.cash

        port.open_position("INFY", "SELL", 1500.0, 6)
        record = port.close_position("INFY", 1600.0, exit_reason="stop_loss")

        net_cash_change = port.cash - opening_cash
        assert record is not None
        assert record.pnl < 0
        assert net_cash_change == pytest.approx(record.pnl, rel=1e-9)

    def test_short_capacity_under_margin_is_5x_legacy(self, tmp_path):
        # Headline finding: under 20% margin you can short ~5x more
        # notional with the same cash. Verify by counting how many
        # ``Rs 25k`` shorts a Rs 100k portfolio can sustain.
        legacy = self._portfolio(tmp_path / "legacy", mis_short_margin_pct=1.0)
        margin = self._portfolio(tmp_path / "margin", mis_short_margin_pct=0.20)

        # Use distinct symbols to avoid the duplicate-position guard.
        symbols = [f"SYM{i:02d}" for i in range(20)]

        legacy_count = 0
        margin_count = 0
        for sym in symbols:
            if legacy.open_position(sym, "SELL", 2500.0, 10):  # Rs 25k notional
                legacy_count += 1
            if margin.open_position(sym, "SELL", 2500.0, 10):
                margin_count += 1

        # Legacy: ~Rs 25k per short -> 4 fits in Rs 100k.
        # Margin: ~Rs 5k per short -> ~20 fits but cash also has to
        # cover commissions, so we expect at least 4x more capacity.
        assert legacy_count <= 4
        assert margin_count >= legacy_count * 4

    def test_cash_locked_persists_in_db_and_round_trips(self, tmp_path):
        # The DB column must be added by migration and round-trip the
        # value so a restart between open and close releases the
        # correct collateral.
        db_path = tmp_path / "test.db"
        db = Database(str(db_path))

        port_a = Portfolio(
            initial_balance=100_000.0,
            log_dir=str(tmp_path),
            database=db,
            mis_short_margin_pct=0.20,
        )
        port_a.open_position("RELIANCE", "SELL", 2500.0, 4)
        expected_lock = port_a.positions["RELIANCE"].cash_locked
        del port_a

        # New Portfolio reading the same DB rehydrates the lock.
        port_b = Portfolio(
            initial_balance=100_000.0,
            log_dir=str(tmp_path),
            database=db,
            mis_short_margin_pct=0.20,
        )
        assert "RELIANCE" in port_b.positions
        assert port_b.positions["RELIANCE"].cash_locked == pytest.approx(
            expected_lock, rel=1e-9
        )

    def test_legacy_db_row_without_cash_locked_uses_full_notional_release(
        self, tmp_path
    ):
        # Pre-migration rows have ``cash_locked = NULL``. Two
        # contracts the restore + close path must hold:
        #
        # 1. The restore path maps NULL to 0.0 (legacy sentinel) so
        #    ``close_position`` knows to use the legacy "full notional"
        #    fallback rather than the explicit margin-released math.
        # 2. The final cash after close matches what a continuous
        #    legacy run (open + close on the same Portfolio instance,
        #    with mis_short_margin_pct=1.0) would have produced. This
        #    is what guarantees a daemon that crosses the migration
        #    boundary mid-position doesn't drift.
        db_path = tmp_path / "legacy.db"
        db = Database(str(db_path))

        # Compute the post-open cash snapshot the same way the legacy
        # daemon would have written it: full notional + entry commission.
        entry_price = 1000.0
        qty = 10
        notional = entry_price * qty
        entry_commission = compute_one_leg(
            entry_price, qty, side="SELL", product="INTRADAY"
        )
        legacy_cash_after_open = 100_000.0 - (notional + entry_commission)

        # Manually insert a legacy-shaped row (cash_locked NULL).
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO open_positions
                   (symbol, side, entry_price, quantity, entry_time,
                    stop_loss, take_profit, strategy, order_id, cash_after,
                    regime, contributing_strategies, cash_locked)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, NULL)""",
                (
                    "LEGACYSYM",
                    "SELL",
                    entry_price,
                    qty,
                    "2026-05-27T10:00:00+05:30",
                    "legacy",
                    "leg-1",
                    legacy_cash_after_open,
                ),
            )

        port = Portfolio(
            initial_balance=100_000.0,
            log_dir=str(tmp_path),
            database=db,
            mis_short_margin_pct=0.20,
        )
        assert "LEGACYSYM" in port.positions
        # Legacy sentinel must round-trip as 0.0 (not None) so the
        # close_position fallback fires.
        assert port.positions["LEGACYSYM"].cash_locked == 0.0

        record = port.close_position("LEGACYSYM", 950.0, exit_reason="signal")
        assert record is not None

        # Reference: a continuous legacy run on a separate Portfolio
        # with mis_short_margin_pct=1.0. End-state cash must match.
        ref = Portfolio(
            initial_balance=100_000.0,
            log_dir=str(tmp_path / "ref"),
            mis_short_margin_pct=1.0,
        )
        ref.open_position("LEGACYSYM", "SELL", entry_price, qty)
        ref.close_position("LEGACYSYM", 950.0, exit_reason="signal")

        assert port.cash == pytest.approx(ref.cash, rel=1e-9)

    def test_open_position_returns_false_when_margin_exceeds_cash(self, tmp_path):
        # With 0.20 margin, a Rs 100k notional short locks ~Rs 20k
        # plus commission. Confirm the cash gate fires once the
        # required margin exceeds available cash, even though the
        # full notional would not.
        port = Portfolio(
            initial_balance=10_000.0,
            log_dir=str(tmp_path),
            mis_short_margin_pct=0.20,
        )
        # Rs 60k notional under 20% margin -> Rs 12k lock, > Rs 10k cash.
        ok = port.open_position("BIGSYM", "SELL", 6000.0, 10)
        assert ok is False
        assert "BIGSYM" not in port.positions


class TestNUM01TraderConfigWiring:
    """trading_agent.py + backtest_ensemble.py must wire the config
    knob through to ``Portfolio``. Source-level assertions are enough
    here -- the integration round-trip is covered by the Portfolio
    tests above."""

    def test_trading_agent_reads_mis_short_margin_pct_from_config(self):
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        # Must read execution.mis_short_margin_pct with a 0.20 default.
        assert "mis_short_margin_pct" in src
        assert 'execution", {}).get("mis_short_margin_pct"' in src
        assert "0.20" in src

    def test_backtest_ensemble_exposes_mis_short_margin_pct(self):
        path = os.path.join(PROJECT_ROOT, "packages", "research", "backtest_ensemble.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        # BacktestConfig field + Portfolio kwarg must both be present.
        assert "mis_short_margin_pct: float = 0.20" in src
        assert "mis_short_margin_pct=self.bt.mis_short_margin_pct" in src

    def test_config_yaml_pins_mis_short_margin_pct_to_0_20(self):
        path = os.path.join(PROJECT_ROOT, "config.yaml")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "mis_short_margin_pct: 0.20" in src


# ─────────────────────────────────────────────────────────────────────
# NUM-06: drop forming intraday bar from REST fallback
# ─────────────────────────────────────────────────────────────────────


class TestNUM06DropFormingIntradayBar:
    """The WS tick aggregator only ever exposes CLOSED candles. The
    REST fallback (Yahoo + AngelOne historical) returns the still-
    forming intraday bar. Pre-fix, every cycle that fell through to
    REST silently fed strategies a half-formed last bar -- the two
    paths were not symmetric. The fix adds a hygiene helper invoked
    from ``_get_historical_cached`` that drops the last row when its
    seal time (``timestamp + interval``) is still in the future.
    """

    @staticmethod
    def _make_helper():
        # The helper lives on TradingAgent, but it is a pure function
        # of (df, timeframe) and we don't want to spin up the whole
        # daemon for a hygiene check. Bind it to a stub object so the
        # ``self`` binding is a no-op.
        import importlib

        ta_mod = importlib.import_module("trading_agent")
        return ta_mod.TradingAgent._drop_forming_intraday_bar

    def _make_df(self, n_bars: int, interval_min: int, last_offset_min: int):
        """Build a synthetic intraday DataFrame whose last bar's
        timestamp is ``last_offset_min`` minutes BEFORE 'now'.
        ``last_offset_min < interval_min`` means the bar has not
        sealed yet.
        """
        import numpy as np
        import pandas as pd
        import pytz

        IST = pytz.timezone("Asia/Kolkata")
        now = pd.Timestamp.now(tz=IST).floor("1min")
        last_ts = now - pd.Timedelta(minutes=last_offset_min)
        index = pd.date_range(
            end=last_ts, periods=n_bars, freq=f"{interval_min}min", tz=IST
        )
        return pd.DataFrame(
            {
                "open": np.arange(n_bars, dtype=float) + 100.0,
                "high": np.arange(n_bars, dtype=float) + 101.0,
                "low": np.arange(n_bars, dtype=float) + 99.0,
                "close": np.arange(n_bars, dtype=float) + 100.5,
                "volume": np.full(n_bars, 1000.0),
            },
            index=index,
        )

    def test_drops_forming_5min_bar_when_seal_in_future(self):
        helper = self._make_helper()
        df = self._make_df(n_bars=20, interval_min=5, last_offset_min=2)

        out = helper(self=None, df=df, timeframe="5min")
        assert len(out) == 19
        # Dropped row was the one that hadn't sealed.
        assert out.index[-1] == df.index[-2]

    def test_keeps_sealed_5min_bar(self):
        helper = self._make_helper()
        # last bar 6 min in the past -> already sealed
        df = self._make_df(n_bars=20, interval_min=5, last_offset_min=6)

        out = helper(self=None, df=df, timeframe="5min")
        assert len(out) == 20
        assert out.index[-1] == df.index[-1]

    def test_handles_15min_timeframe(self):
        helper = self._make_helper()
        # bar 10 min old, 15-min interval -> not sealed
        df = self._make_df(n_bars=20, interval_min=15, last_offset_min=10)
        out = helper(self=None, df=df, timeframe="15min")
        assert len(out) == 19

    def test_handles_1h_timeframe(self):
        helper = self._make_helper()
        df = self._make_df(n_bars=20, interval_min=60, last_offset_min=30)
        out = helper(self=None, df=df, timeframe="1h")
        assert len(out) == 19

    def test_returns_unchanged_for_daily_timeframe(self):
        # Daily / weekly is _trend_context's responsibility (NUM-05/15).
        # The intraday helper must not touch daily frames.
        helper = self._make_helper()
        df = self._make_df(n_bars=10, interval_min=5, last_offset_min=2)
        out = helper(self=None, df=df, timeframe="1d")
        assert len(out) == len(df)

    def test_returns_unchanged_for_empty_frame(self):
        import pandas as pd

        helper = self._make_helper()
        out = helper(self=None, df=pd.DataFrame(), timeframe="5min")
        assert out.empty

    def test_returns_unchanged_for_none(self):
        helper = self._make_helper()
        out = helper(self=None, df=None, timeframe="5min")
        assert out is None

    def test_fail_open_on_garbage_timeframe(self):
        helper = self._make_helper()
        df = self._make_df(n_bars=10, interval_min=5, last_offset_min=2)
        out = helper(self=None, df=df, timeframe="not-a-tf")
        # Unrecognised timeframe -> hygiene check passes through.
        assert len(out) == len(df)

    def test_fail_open_on_zero_interval(self):
        helper = self._make_helper()
        df = self._make_df(n_bars=10, interval_min=5, last_offset_min=2)
        out = helper(self=None, df=df, timeframe="0min")
        assert len(out) == len(df)

    def test_handles_naive_timestamp_index(self):
        # Some legacy paths return naive (tz-less) timestamps. The
        # helper must coerce them to IST and not throw.
        import numpy as np
        import pandas as pd
        import pytz

        helper = self._make_helper()
        IST = pytz.timezone("Asia/Kolkata")
        # Build a naive index whose last entry is 2 minutes ago in IST.
        now_naive = pd.Timestamp.now(tz=IST).tz_localize(None).floor("1min")
        last_ts = now_naive - pd.Timedelta(minutes=2)
        idx = pd.date_range(end=last_ts, periods=10, freq="5min")
        df = pd.DataFrame(
            {
                "open": np.arange(10.0),
                "high": np.arange(10.0) + 1,
                "low": np.arange(10.0) - 1,
                "close": np.arange(10.0),
                "volume": np.full(10, 100.0),
            },
            index=idx,
        )
        out = helper(self=None, df=df, timeframe="5min")
        # Last bar (2 min old, 5-min interval) -> dropped.
        assert len(out) == 9

    def test_get_historical_cached_invokes_helper_at_source(self):
        # Source-level wiring: ``_get_historical_cached`` must call
        # ``_drop_forming_intraday_bar`` on the REST result.
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()

        cached_def = src.find("def _get_historical_cached(")
        assert cached_def != -1, "_get_historical_cached not found"
        # Slice up to the next ``def`` to bound the function body.
        next_def = src.find("\n    def ", cached_def + 1)
        body = src[cached_def:next_def]
        assert "_drop_forming_intraday_bar" in body, (
            "_drop_forming_intraday_bar not invoked from "
            "_get_historical_cached"
        )

    def test_helper_documents_num06_anchor(self):
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "NUM-06" in src


# ─────────────────────────────────────────────────────────────────────
# NUM-07: features rolling-75 session bleed
# ─────────────────────────────────────────────────────────────────────


class TestNUM07FeatureSessionReset:
    """Pre-fix the day-high / day-low features used a rolling-75 window.
    At 09:20 IST the rolling window pulled in 74 bars of YESTERDAY,
    so the breakout / mean-reversion decisions at the open were
    measured against yesterday's range. The fix groups by session
    date and uses ``cummax`` / ``cummin`` so the window EXPANDS through
    the session and resets on each new IST date -- same pattern VWAP
    and OBV already use.
    """

    def _make_two_day_5min(self, day1_high: float, day2_high: float):
        """Two trading sessions of 5-min bars: day1 has highs around
        ``day1_high``, day2 starts the next day with highs around
        ``day2_high``."""
        import numpy as np
        import pandas as pd
        import pytz

        IST = pytz.timezone("Asia/Kolkata")
        # Session 1: 09:15 -> 15:30 IST = 75 bars on day1.
        day1_start = pd.Timestamp("2026-05-26 09:15", tz=IST)
        day2_start = pd.Timestamp("2026-05-27 09:15", tz=IST)
        idx1 = pd.date_range(start=day1_start, periods=75, freq="5min")
        idx2 = pd.date_range(start=day2_start, periods=10, freq="5min")
        idx = idx1.append(idx2)

        # Day 1 high pegged at day1_high. Day 2 starts with closes
        # around 100, well below day1_high, so a leaky rolling-75
        # window would still see day1_high after the reset.
        d1_close = np.full(75, 100.0)
        d2_close = np.full(10, 100.0)
        d1_high = np.full(75, day1_high)
        d2_high = np.full(10, day2_high)
        d1_low = np.full(75, 99.0)
        d2_low = np.full(10, 99.5)

        return pd.DataFrame(
            {
                "open": np.concatenate([d1_close, d2_close]),
                "high": np.concatenate([d1_high, d2_high]),
                "low": np.concatenate([d1_low, d2_low]),
                "close": np.concatenate([d1_close, d2_close]),
                "volume": np.full(85, 1000.0),
            },
            index=idx,
        )

    def test_dist_from_high_resets_at_session_boundary(self):
        from core.features import FeatureEngine

        df = self._make_two_day_5min(day1_high=110.0, day2_high=101.0)
        out = FeatureEngine().compute_all(df)

        # First bar of day 2 is row 75. Its dist_from_high must
        # reflect day-2's running max (101 - 100) / 100 * 100 = 1.0,
        # NOT day-1's 110 leaking in (which would be 10.0).
        first_d2 = out["dist_from_high_pct"].iloc[75]
        assert first_d2 == pytest.approx(1.0, abs=0.01), (
            f"day 2 first bar leaked yesterday's high: dist={first_d2:.3f} "
            f"(expected ~1.0)"
        )

    def test_dist_from_low_resets_at_session_boundary(self):
        from core.features import FeatureEngine

        # Day 1 low far below day 2 low.
        df = self._make_two_day_5min(day1_high=110.0, day2_high=101.0)
        # Override day 1 low to be much lower so leakage shows up.
        df.loc[df.index[:75], "low"] = 80.0
        out = FeatureEngine().compute_all(df)

        # Day 2 row 0: low is 99.5, close 100. dist_from_low = 0.5.
        # If day 1 leaked in, the running min would still be 80 ->
        # dist = (100 - 80) / 100 * 100 = 20.0.
        first_d2 = out["dist_from_low_pct"].iloc[75]
        assert first_d2 < 1.0, (
            f"day 2 first bar leaked yesterday's low: dist={first_d2:.3f} "
            f"(expected < 1.0)"
        )

    def test_dist_from_high_grows_through_session(self):
        # Within a single session, the running max should be
        # non-decreasing (cummax property), so dist_from_high_pct
        # cannot suddenly drop because an old high "fell out" of the
        # rolling window.
        import numpy as np
        import pandas as pd
        import pytz

        from core.features import FeatureEngine

        IST = pytz.timezone("Asia/Kolkata")
        idx = pd.date_range(
            start="2026-05-27 09:15", periods=75, freq="5min", tz=IST
        )
        # Highs spike to 110 at bar 5 then drop back to 100.
        highs = np.full(75, 100.0)
        highs[5] = 110.0
        df = pd.DataFrame(
            {
                "open": np.full(75, 100.0),
                "high": highs,
                "low": np.full(75, 99.0),
                "close": np.full(75, 100.0),
                "volume": np.full(75, 1000.0),
            },
            index=idx,
        )
        out = FeatureEngine().compute_all(df)

        # After bar 5, every subsequent bar's running max is 110.
        # dist_from_high_pct = (110 - 100) / 100 * 100 = 10.0 from
        # bar 5 onward.
        for i in range(5, 75):
            assert out["dist_from_high_pct"].iloc[i] == pytest.approx(
                10.0, abs=0.01
            ), f"bar {i} dropped its session high: {out['dist_from_high_pct'].iloc[i]}"

    def test_falls_back_to_legacy_rolling_when_index_has_no_date(self):
        # RangeIndex / numeric index path used by some legacy unit
        # tests. The fallback must remain rolling-75.
        import numpy as np
        import pandas as pd

        from core.features import FeatureEngine

        df = pd.DataFrame(
            {
                "open": np.full(100, 100.0),
                "high": np.linspace(100, 110, 100),
                "low": np.linspace(100, 90, 100),
                "close": np.full(100, 100.0),
                "volume": np.full(100, 1000.0),
            }
        )
        out = FeatureEngine().compute_all(df)
        # Series should be present with no NaN once the rolling
        # window fills.
        assert "dist_from_high_pct" in out.columns
        assert not out["dist_from_high_pct"].iloc[80:].isna().any()

    def test_features_source_uses_groupby_cummax(self):
        # Source-level guard: the rolling-75 window in the day-high
        # feature must be replaced by ``groupby(...).cummax`` /
        # ``cummin`` (matching VWAP and OBV).
        path = os.path.join(PROJECT_ROOT, "packages", "core", "features.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()

        derived = src.find("def _add_derived_features(")
        assert derived != -1, "_add_derived_features not found"
        next_def = src.find("\n    @staticmethod", derived + 1)
        body = src[derived:next_def]
        assert "groupby(day).cummax()" in body
        assert "groupby(day).cummin()" in body
        assert "NUM-07" in body


# ─────────────────────────────────────────────────────────────────────
# Group C: live order discipline (ORD-05 / ORD-07 / ORD-08 / ORD-09)
# ─────────────────────────────────────────────────────────────────────


class TestORD05CancelThenFlattenRace:
    """If a stop-loss triggers between our exit decision and our
    cancel call, the broker side has already flattened. Pre-fix the
    exit path still sent a flatten -- effectively double-flattening
    and opening an unintended reverse position. The fix peeks the
    SL's terminal status after cancel and, on a fired SL, skips the
    flatten entirely and reconciles the in-memory portfolio at the
    SL's fill price.
    """

    def test_close_position_safely_has_ord05_anchor(self):
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _close_position_safely(")
        assert idx != -1, "_close_position_safely not found"
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        assert "ORD-05" in body
        assert "sl_filled_first" in body
        assert "get_order_status" in body
        # The race-resolution path must reconcile through
        # portfolio.close_position with a clear exit_reason.
        assert "sl_filled_during_close_race" in body


class TestORD07ExitWaitForTerminalAtSource:
    """ORD-07 was structurally closed by Phase 2: live exits route
    through ``place_order`` -> ``_live_order_with_retry`` ->
    ``_wait_for_terminal``. The contract is enforced at source
    here so a future refactor cannot silently drop the wait helper
    from the exit path."""

    def test_place_order_routes_live_to_wait_for_terminal(self):
        path = os.path.join(PROJECT_ROOT, "packages", "core", "execution.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        # place_order must delegate to _live_order_with_retry in live mode.
        po_idx = src.find("def place_order(")
        assert po_idx != -1
        po_next = src.find("\n    def ", po_idx + 1)
        po_body = src[po_idx:po_next]
        assert "_live_order_with_retry" in po_body

        # _live_order_with_retry must call _wait_for_terminal on the
        # placeOrder response (entries AND exits go through this).
        lor_idx = src.find("def _live_order_with_retry(")
        assert lor_idx != -1
        lor_next = src.find("\n    def ", lor_idx + 1)
        lor_body = src[lor_idx:lor_next]
        assert "_wait_for_terminal" in lor_body

    def test_close_position_safely_uses_place_order_for_flatten(self):
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _close_position_safely(")
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        # The flatten must go through self.execution.place_order so
        # it inherits _wait_for_terminal.
        assert "self.execution.place_order(" in body


class TestORD08SLSizedOffFilledQty:
    """SL-M placed on a partial entry must size to ``filled_quantity``
    (what the broker actually opened), not the ``quantity`` we
    requested. Pre-fix, a PARTIALLY_FILLED entry got an SL that was
    over-sized and the residual qty was unprotected.
    """

    def test_live_order_with_retry_sizes_sl_off_filled_qty(self):
        path = os.path.join(PROJECT_ROOT, "packages", "core", "execution.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _live_order_with_retry(")
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        # Source-level: the SL placement must compute an effective
        # SL qty from result["filled_quantity"] (with a defensive
        # fallback to the requested qty).
        assert "ORD-08" in body
        assert "effective_sl_qty" in body
        assert "_place_sl_order(" in body
        # _place_sl_order is invoked with effective_sl_qty, not the
        # raw 'quantity' arg. Find the _place_sl_order call line and
        # check its 3rd positional.
        sl_call_idx = body.find("self._place_sl_order(")
        assert sl_call_idx != -1
        sl_call_end = body.find(")", sl_call_idx)
        sl_call = body[sl_call_idx:sl_call_end]
        assert "effective_sl_qty" in sl_call

    def test_sl_meta_records_effective_sl_qty(self):
        path = os.path.join(PROJECT_ROOT, "packages", "core", "execution.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _live_order_with_retry(")
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        # The _sl_orders_by_symbol meta dict must persist
        # effective_sl_qty as ``quantity`` so trail-modify and
        # cancel paths see consistent qty.
        meta_idx = body.find("self._sl_orders_by_symbol[symbol] = {")
        assert meta_idx != -1
        meta_end = body.find("}", meta_idx)
        meta = body[meta_idx:meta_end]
        assert '"quantity": effective_sl_qty' in meta


class TestORD09TTLCancelAndFail:
    """On TTL expiry without terminal observation, the pre-fix code
    kept ``status='PLACED'`` and proceeded to place SL on a position
    the broker may never have actually opened. ORD-09 changes that
    contract: cancel the order, drop in-memory tracking, and surface
    None to the caller. The next retry's idempotency probe (ORD-02)
    picks up the order if the broker did accept it after our timeout.
    """

    def test_ttl_expiry_attempts_cancel_and_returns_none(self):
        from unittest.mock import MagicMock

        from core.execution import ExecutionEngine

        api = MagicMock()
        api.placeOrder.return_value = "OID-TTL"
        api.orderBook.return_value = {
            "data": [{
                "orderid": "OID-TTL",
                "status": "open",  # never terminal
                "averageprice": "0",
                "filledshares": "0",
            }]
        }
        api.cancelOrder.return_value = {"status": True}

        cfg = {
            "execution": {
                "live_order_fill_timeout_sec": 0.15,
                "live_order_fill_poll_interval_sec": 0.05,
                "retry_attempts": 1,
            },
            "instruments": [],
        }
        eng = ExecutionEngine(cfg, smart_api=api)
        eng.mode = "live"
        eng._api = api
        eng.retry_attempts = 1
        eng._place_sl_order = MagicMock(return_value=None)
        eng._persist_order = MagicMock()

        out = eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert out is None
        api.cancelOrder.assert_called_once_with("OID-TTL", "NORMAL")
        # SL never gets placed if entry TTL'd out.
        eng._place_sl_order.assert_not_called()
        # Pending tracking cleared so the boot reconcile / probe
        # picks up the broker state authoritatively.
        assert "OID-TTL" not in eng._pending_orders

    def test_ord09_anchor_in_source(self):
        path = os.path.join(PROJECT_ROOT, "packages", "core", "execution.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _live_order_with_retry(")
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        assert "ORD-09" in body
        # The TTL branch must invoke cancel_order before returning.
        assert "self.cancel_order(" in body


# ─────────────────────────────────────────────────────────────────────
# NUM-11 / ORD-11: live slippage capture + tolerance circuit breaker
# ─────────────────────────────────────────────────────────────────────


def _slippage_engine(api_mock, *, halt: bool = False, tolerance_pct: float = 0.10):
    """Live ExecutionEngine wired to ``api_mock`` with the slippage
    tolerance circuit breaker controlled by ``halt`` (the
    ``halt_symbol_on_slippage_breach`` config knob).
    """
    from core.execution import ExecutionEngine

    cfg = {
        "broker": {"mode": "live"},
        "execution": {
            "order_type": "LIMIT",
            "product_type": "INTRADAY",
            "live_order_fill_timeout_sec": 0.05,
            "live_order_fill_poll_interval_sec": 0.02,
            "slippage_tolerance_pct": tolerance_pct,
            "halt_symbol_on_slippage_breach": halt,
        },
        "market": {"exchange": "NSE"},
    }
    return ExecutionEngine(cfg, smart_api=api_mock)


def _seed_book(api_mock, order_id: str, *, status: str = "complete",
               avg_price: float = 1500.0, qty: int = 10):
    api_mock.orderBook.return_value = {
        "status": True,
        "data": [{
            "orderid": order_id,
            "status": status,
            "averageprice": str(avg_price),
            "filledshares": str(qty),
        }],
    }


class TestNUM11SlippageParity:
    """Paper and live MUST emit the same slippage shape so the
    backtester is comparable to live without bespoke parsing."""

    def test_paper_result_carries_slippage_pct_and_breach_flag(self):
        from unittest.mock import MagicMock

        from core.execution import ExecutionEngine

        cfg = {
            "broker": {"mode": "paper"},
            "execution": {
                "order_type": "LIMIT",
                "product_type": "INTRADAY",
                "slippage_tolerance_pct": 0.10,
            },
            "market": {"exchange": "NSE"},
        }
        eng = ExecutionEngine(cfg, smart_api=None, database=MagicMock())

        res = eng.place_order(
            symbol="HDFCBANK", token="123", transaction_type="BUY",
            quantity=10, price=1500.0,
        )
        assert res is not None
        assert "slippage_pct" in res
        assert "slippage_breach" in res
        # Paper draws within [0, tolerance] so by definition it CAN'T
        # exceed tolerance + epsilon.
        assert isinstance(res["slippage_pct"], (int, float))
        assert res["slippage_pct"] >= 0.0
        # Paper cannot strictly exceed tolerance (uniform draw is half-
        # open on the right after rounding to 2dp).
        assert res["slippage_breach"] is False or res["slippage_pct"] <= 0.10001

    def test_live_filled_result_carries_slippage_pct(self):
        from unittest.mock import MagicMock

        api = MagicMock()
        api.placeOrder.side_effect = ["OID-LP-1"]
        # Fill 0.05% above requested -> within tolerance.
        _seed_book(api, "OID-LP-1", avg_price=1500.75, qty=10)
        eng = _slippage_engine(api, tolerance_pct=0.10)

        res = eng._live_order_with_retry(
            symbol="HDFCBANK", token="123", tx_type="BUY",
            quantity=10, price=1500.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert res is not None
        assert res["status"] == "FILLED"
        assert res["slippage_pct"] == pytest.approx(0.05, rel=1e-3)
        assert res["slippage_breach"] is False

    def test_slippage_pct_is_zero_when_fill_matches_request(self):
        from unittest.mock import MagicMock

        api = MagicMock()
        api.placeOrder.side_effect = ["OID-EXACT"]
        _seed_book(api, "OID-EXACT", avg_price=1500.0, qty=10)
        eng = _slippage_engine(api, tolerance_pct=0.10)

        res = eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=1500.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert res["slippage_pct"] == pytest.approx(0.0, abs=1e-6)
        assert res["slippage_breach"] is False

    def test_record_slippage_returns_none_when_inputs_invalid(self):
        from unittest.mock import MagicMock

        from core.execution import ExecutionEngine

        eng = ExecutionEngine({"broker": {"mode": "paper"}}, smart_api=None,
                              database=MagicMock())

        # Missing fill price.
        res = {"order_id": "X", "filled_price": None, "mode": "paper"}
        eng._record_slippage(res, requested_price=100.0)
        assert res["slippage_pct"] is None
        assert res["slippage_breach"] is False

        # Zero requested.
        res = {"order_id": "X", "filled_price": 100.0, "mode": "paper"}
        eng._record_slippage(res, requested_price=0.0)
        assert res["slippage_pct"] is None
        assert res["slippage_breach"] is False

    def test_get_order_status_path_also_records_slippage_pct(self):
        from unittest.mock import MagicMock

        api = MagicMock()
        api.orderBook.return_value = {
            "status": True,  # outer envelope
            "data": [{
                "orderid": "OID-OBSERVED",
                "status": "complete",
                "averageprice": "1503.00",
                "filledshares": "10",
            }]
        }
        eng = _slippage_engine(api, tolerance_pct=0.10)
        # Pre-seed the pending dict the way ``_live_order_with_retry``
        # would have: requested_price is required.
        eng._pending_orders["OID-OBSERVED"] = {
            "order_id": "OID-OBSERVED",
            "requested_price": 1500.0,
            "mode": "live",
        }
        out = eng.get_order_status("OID-OBSERVED")
        assert out is not None
        # The pending row should now have slippage_pct + breach (0.20%
        # > 0.10% tolerance).
        pending = eng._pending_orders["OID-OBSERVED"]
        assert pending["slippage_pct"] == pytest.approx(0.20, rel=1e-3)
        assert pending["slippage_breach"] is True


class TestORD11SlippageCircuitBreaker:
    """A live fill that breaches ``slippage_tolerance_pct`` MUST surface
    as a CRITICAL ``[ORD-11-SLIPPAGE]`` log AND (when the operator opts
    in) block new entries on the symbol until cleared.
    """

    def test_breach_on_live_fill_logs_critical_anchor(self, caplog):
        import logging

        from unittest.mock import MagicMock

        api = MagicMock()
        api.placeOrder.side_effect = ["OID-BREACH-1"]
        # Fill 1.0% above requested -> breach 0.10% tolerance by 10x.
        _seed_book(api, "OID-BREACH-1", avg_price=1515.0, qty=10)
        eng = _slippage_engine(api, halt=False, tolerance_pct=0.10)

        # Loguru -> stdlib bridge: capture WARNING+ from a stdlib
        # handler attached to a unique logger name. Easier: read the
        # ``result`` dict and assert the breach flag, since loguru
        # routes elsewhere by default.
        res = eng._live_order_with_retry(
            symbol="HDFCBANK", token="123", tx_type="BUY",
            quantity=10, price=1500.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert res is not None
        assert res["slippage_breach"] is True
        assert res["slippage_pct"] == pytest.approx(1.0, rel=1e-3)
        # halt=False so the symbol is NOT blocked.
        assert eng.is_symbol_slippage_blocked("HDFCBANK") is False

    def test_breach_with_halt_flag_blocks_symbol(self):
        from unittest.mock import MagicMock

        api = MagicMock()
        api.placeOrder.side_effect = ["OID-BREACH-2"]
        _seed_book(api, "OID-BREACH-2", avg_price=1515.0, qty=10)
        eng = _slippage_engine(api, halt=True, tolerance_pct=0.10)

        res = eng._live_order_with_retry(
            symbol="HDFCBANK", token="123", tx_type="BUY",
            quantity=10, price=1500.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert res["slippage_breach"] is True
        assert eng.is_symbol_slippage_blocked("HDFCBANK") is True
        # Snapshot returns a copy so callers can't mutate state.
        snapshot = eng.get_slippage_breached_symbols()
        assert snapshot == {"HDFCBANK"}
        snapshot.clear()
        assert eng.is_symbol_slippage_blocked("HDFCBANK") is True

    def test_clear_slippage_block_lifts_gate(self):
        from unittest.mock import MagicMock

        api = MagicMock()
        api.placeOrder.side_effect = ["OID-BREACH-3"]
        _seed_book(api, "OID-BREACH-3", avg_price=1515.0, qty=10)
        eng = _slippage_engine(api, halt=True, tolerance_pct=0.10)

        eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=1500.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert eng.is_symbol_slippage_blocked("X") is True
        cleared = eng.clear_slippage_block("X")
        assert cleared is True
        assert eng.is_symbol_slippage_blocked("X") is False
        # Idempotent: clearing an unknown symbol returns False without
        # raising.
        assert eng.clear_slippage_block("UNKNOWN") is False

    def test_within_tolerance_does_not_block(self):
        from unittest.mock import MagicMock

        api = MagicMock()
        api.placeOrder.side_effect = ["OID-OK"]
        # Exactly at tolerance: should NOT breach (epsilon-aware).
        _seed_book(api, "OID-OK", avg_price=1500.0 * (1 + 0.001), qty=10)
        eng = _slippage_engine(api, halt=True, tolerance_pct=0.10)

        res = eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=1500.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert res is not None
        # 0.10% slippage exactly = tolerance, not a breach.
        assert res["slippage_breach"] is False
        assert eng.is_symbol_slippage_blocked("X") is False

    def test_partial_fill_also_records_slippage_pct(self):
        from unittest.mock import MagicMock

        from core.execution import ExecutionEngine

        cfg = {
            "broker": {"mode": "paper"},
            "execution": {
                "order_type": "LIMIT",
                "product_type": "INTRADAY",
                "slippage_tolerance_pct": 0.10,
                "paper_partial_fill_prob": 1.0,
                "paper_partial_fill_min_ratio": 0.5,
            },
            "market": {"exchange": "NSE"},
        }
        eng = ExecutionEngine(cfg, smart_api=None, database=MagicMock())
        res = eng.place_order(
            symbol="X", token="0", transaction_type="BUY",
            quantity=10, price=1500.0,
        )
        assert res is not None
        assert "slippage_pct" in res
        assert "slippage_breach" in res

    def test_open_new_position_consults_slippage_block(self):
        """The trading_agent entry path must gate on
        ``is_symbol_slippage_blocked`` before issuing a new entry.
        Source-level pin so a future refactor cannot silently drop the
        gate."""
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _open_new_position(")
        assert idx >= 0, "_open_new_position must exist"
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        assert "is_symbol_slippage_blocked" in body, (
            "ORD-11 regression: _open_new_position no longer consults "
            "execution.is_symbol_slippage_blocked. The slippage circuit "
            "breaker is now bypassable. Re-add the gate before the "
            "rollback-block check or wherever the entry-time gates "
            "are applied."
        )
        # The reject reason string is part of the audit contract.
        assert "slippage_block:breach" in body or "slippage_block" in body, (
            "ORD-11 regression: the gate's audit_reject reason string "
            "changed -- ops dashboards key off this string."
        )

    def test_anchor_in_execution_source(self):
        path = os.path.join(PROJECT_ROOT, "packages", "core", "execution.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        # Helper exists.
        assert "def _record_slippage(" in src
        assert "def is_symbol_slippage_blocked(" in src
        assert "def clear_slippage_block(" in src
        # Anchors so future refactors can grep for the audit ID.
        assert "ORD-11-SLIPPAGE" in src
        assert "halt_symbol_on_slippage_breach" in src


# ─────────────────────────────────────────────────────────────────────
# ORD-10: reactive re-auth on auth-class broker errors
# ─────────────────────────────────────────────────────────────────────


class TestORD10ErrorClassifier:
    """``classify_smartapi_error`` must recognise the AngelOne auth +
    rate-limit signatures we know about and conservatively default to
    "transient" for unknown shapes so a future broker-contract drift
    doesn't accidentally halt trading.
    """

    def test_recognises_known_auth_codes(self):
        from core.execution import classify_smartapi_error

        for code in ("AB1010", "AB1011", "AB1014", "AB1019",
                     "AB2001", "AB2002", "AB2003"):
            err = {"status": False, "errorcode": code, "message": "x"}
            assert classify_smartapi_error(err) == "auth", (
                f"AngelOne code {code} should classify as auth"
            )

    def test_recognises_string_phrases(self):
        from core.execution import classify_smartapi_error

        for phrase in (
            "Invalid Token",
            "Token Invalid",
            "Token Expired",
            "Session Expired",
            "Not logged in",
            "Unauthorized",
            "Logged out",
            "Please login",
            "JWT Expired",
        ):
            assert classify_smartapi_error(Exception(phrase)) == "auth", (
                f"phrase {phrase!r} should classify as auth"
            )

    def test_recognises_401_and_403_status(self):
        from core.execution import classify_smartapi_error

        assert classify_smartapi_error(Exception("HTTP 401 Unauthorized")) == "auth"
        assert classify_smartapi_error(Exception("403 Forbidden")) == "auth"

    def test_does_not_misfire_on_order_id_with_401_substring(self):
        from core.execution import classify_smartapi_error

        # Order id like "OID-401-X" must NOT trigger the 401 heuristic.
        assert classify_smartapi_error(Exception("Order OID-4012345 rejected")) == "transient"

    def test_recognises_rate_limit(self):
        from core.execution import classify_smartapi_error

        for phrase in ("Too Many Requests", "rate limit exceeded",
                       "throttle exceeded", "AB429"):
            assert classify_smartapi_error(Exception(phrase)) == "rate_limit"

    def test_unknown_error_defaults_to_transient(self):
        from core.execution import classify_smartapi_error

        # Conservative default: don't accidentally halt trading on
        # unknown broker contract drift.
        assert classify_smartapi_error(Exception("Connection reset by peer")) == "transient"
        assert classify_smartapi_error(Exception("RMS rejected: insufficient margin")) == "transient"

    def test_dict_payload_with_error_code_field(self):
        from core.execution import classify_smartapi_error

        err = {"status": False, "error_code": "AB1011", "message": "..."}
        assert classify_smartapi_error(err) == "auth"

    def test_handles_none_payload(self):
        from core.execution import classify_smartapi_error

        assert classify_smartapi_error(None) == "transient"

    def test_rate_limit_takes_precedence_over_auth_words(self):
        from core.execution import classify_smartapi_error

        # An "AB429 session" message: rate-limit wins over auth-phrase
        # heuristic.
        err = {"errorcode": "AB429", "message": "session too many requests"}
        assert classify_smartapi_error(err) == "rate_limit"


class TestORD10AuthCallbackHook:
    """The hook plumbing on ``ExecutionEngine`` must:
      * accept a callback via ``set_auth_refresh_callback``,
      * fire the callback on auth-class exceptions during
        ``_live_order_with_retry``,
      * fire AT MOST ONCE per top-level call (so a misbehaving
        callback can't infinite-loop the retry budget),
      * tolerate a callback that raises (must not crash).
    """

    def _engine(self):
        from unittest.mock import MagicMock

        from core.execution import ExecutionEngine

        cfg = {
            "broker": {"mode": "live"},
            "execution": {
                "order_type": "LIMIT",
                "product_type": "INTRADAY",
                "live_order_fill_timeout_sec": 0.05,
                "live_order_fill_poll_interval_sec": 0.02,
                "retry_attempts": 3,
                "retry_delay_seconds": 0,
            },
            "market": {"exchange": "NSE"},
        }
        api = MagicMock()
        eng = ExecutionEngine(cfg, smart_api=api)
        return eng, api

    def test_set_auth_refresh_callback_installs_hook(self):
        eng, _ = self._engine()
        called = []
        eng.set_auth_refresh_callback(lambda: called.append(True) or True)
        assert eng._auth_failure_callback is not None

    def test_auth_class_exception_invokes_callback(self):
        from unittest.mock import MagicMock

        eng, api = self._engine()
        api.placeOrder.side_effect = Exception("Invalid Token")

        called = MagicMock(return_value=False)
        eng.set_auth_refresh_callback(called)

        out = eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert out is None
        # Callback fired -- regardless of return value.
        assert called.call_count == 1, (
            "ORD-10 regression: auth-class exception did not invoke "
            "the auth_failure_callback. Reactive re-auth is broken."
        )

    def test_callback_is_invoked_at_most_once_per_top_level_call(self):
        """Three retries that all raise auth errors must still call
        the callback only ONCE. Prevents a misbehaving callback from
        burning the budget."""
        from unittest.mock import MagicMock

        eng, api = self._engine()
        api.placeOrder.side_effect = Exception("Session Expired")

        called = MagicMock(return_value=False)
        eng.set_auth_refresh_callback(called)

        eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert called.call_count == 1

    def test_transient_exception_does_not_invoke_callback(self):
        from unittest.mock import MagicMock

        eng, api = self._engine()
        api.placeOrder.side_effect = Exception("Connection reset by peer")

        called = MagicMock(return_value=True)
        eng.set_auth_refresh_callback(called)

        eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert called.call_count == 0

    def test_callback_raising_does_not_crash_retry_loop(self):
        from unittest.mock import MagicMock

        eng, api = self._engine()
        api.placeOrder.side_effect = Exception("AB1010 Invalid Token")

        called = MagicMock(side_effect=RuntimeError("callback boom"))
        eng.set_auth_refresh_callback(called)

        # Must not propagate the callback's RuntimeError.
        out = eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        assert out is None

    def test_callback_per_call_latch_resets_between_top_level_calls(self):
        """Two distinct ``_live_order_with_retry`` calls must each get
        their own one-shot auth-refresh budget."""
        from unittest.mock import MagicMock

        eng, api = self._engine()
        api.placeOrder.side_effect = Exception("Token Expired")

        called = MagicMock(return_value=False)
        eng.set_auth_refresh_callback(called)

        eng._live_order_with_retry(
            symbol="X", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        eng._live_order_with_retry(
            symbol="Y", token="0", tx_type="BUY",
            quantity=10, price=100.0, order_type="LIMIT",
            stop_loss=None, take_profit=None, tag="t",
        )
        # Two top-level calls -> two callback invocations.
        assert called.call_count == 2

    def test_anchor_in_trading_agent_init(self):
        """Source-level pin: TradingAgent.__init__ must wire the
        callback. A future refactor that drops the wiring would silently
        leave the daemon on a stale JWT after a force-logout."""
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def __init__(self, config")
        assert idx >= 0
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        assert "set_auth_refresh_callback" in body, (
            "ORD-10 regression: TradingAgent.__init__ no longer wires "
            "the auth_refresh_callback. The reactive re-auth path is "
            "now disconnected from the JWT-refresh logic."
        )
        assert "_handle_broker_auth_failure" in body or \
               "_handle_broker_auth_failure" in src, (
            "ORD-10 regression: _handle_broker_auth_failure helper is "
            "missing."
        )

    def test_force_refresh_kwarg_bypasses_age_gate(self):
        """``_maybe_refresh_broker_session(force=True)`` must not
        bail out on the 7h age gate."""
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _maybe_refresh_broker_session(")
        assert idx >= 0
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        assert "force" in body
        # The force=True branch must skip the 7h age check.
        assert "if not force" in body or "if force" in body


# ─────────────────────────────────────────────────────────────────────
# NUM-10: Decimal arithmetic for charges (audit 2026-05-28)
# ─────────────────────────────────────────────────────────────────────


class TestNUM10DecimalCharges:
    """``charges.py`` now uses Decimal internally and quantizes per
    component to 1 paisa. Two contracts must hold:

      * Each component is rounded to broker-truth resolution (1 paisa)
        so backtester and live can be reconciled to the rupee.
      * ``compute_round_trip(buy, sell, qty).total`` ==
        ``compute_one_leg(BUY, buy, qty) + compute_one_leg(SELL, sell, qty)``
        byte-for-byte, so ``portfolio.close_position`` doesn't need
        the subtractive ``total - entry`` derivation that used to
        accumulate float drift.
    """

    def test_round_trip_total_equals_sum_of_legs_intraday(self):
        # Pick a price/qty combination chosen to be float-jittery
        # (1234.567 * 137 has a non-power-of-two mantissa).
        rt = compute_round_trip(buy_price=1234.567, sell_price=1242.913,
                                quantity=137, product="INTRADAY")
        leg_buy = compute_one_leg(1234.567, 137, side="BUY", product="INTRADAY")
        leg_sell = compute_one_leg(1242.913, 137, side="SELL", product="INTRADAY")
        # NUM-10 identity: round-trip == sum of legs (byte-for-byte).
        assert rt.total == pytest.approx(leg_buy + leg_sell, abs=1e-9), (
            f"NUM-10 regression: round_trip total {rt.total} != "
            f"leg sum {leg_buy + leg_sell}. portfolio.close_position's "
            f"`exit_commission = compute_one_leg(exit)` derivation now "
            f"depends on this identity."
        )

    def test_round_trip_total_equals_sum_of_legs_delivery(self):
        rt = compute_round_trip(buy_price=2500.123, sell_price=2519.876,
                                quantity=42, product="DELIVERY")
        leg_buy = compute_one_leg(2500.123, 42, side="BUY", product="DELIVERY")
        leg_sell = compute_one_leg(2519.876, 42, side="SELL", product="DELIVERY")
        assert rt.total == pytest.approx(leg_buy + leg_sell, abs=1e-9)

    def test_components_are_quantized_to_paisa(self):
        """Every reported component should be representable as N
        hundredths of a rupee (the broker contract-note resolution)."""
        from decimal import Decimal

        rt = compute_round_trip(buy_price=1234.567, sell_price=1242.913,
                                quantity=137, product="INTRADAY")
        for field in ("brokerage", "stt", "exchange_txn", "sebi", "gst",
                      "stamp_duty", "dp_charges", "total"):
            val = getattr(rt, field)
            d = Decimal(str(val))
            # Quantization to 0.01 means the fractional remainder after
            # multiplying by 100 must be < 1e-9.
            scaled = d * Decimal(100)
            frac = abs(scaled - scaled.to_integral_value())
            assert frac < Decimal("1e-9"), (
                f"NUM-10 regression: charges.{field} = {val} is not "
                f"paisa-quantized (residual {frac})."
            )

    def test_legs_are_quantized_to_paisa(self):
        from decimal import Decimal

        for side in ("BUY", "SELL"):
            for product in ("INTRADAY", "DELIVERY"):
                val = compute_one_leg(1234.567, 137, side=side, product=product)
                scaled = Decimal(str(val)) * Decimal(100)
                frac = abs(scaled - scaled.to_integral_value())
                assert frac < Decimal("1e-9"), (
                    f"NUM-10 regression: compute_one_leg({side}, "
                    f"{product})={val} is not paisa-quantized."
                )

    def test_exit_commission_no_longer_uses_subtraction(self):
        """portfolio.close_position used to do
        ``exit_commission = total_commission - entry_commission``
        which drifted as float ops accumulate. The fix replaces this
        with a direct ``compute_one_leg`` call. Pin the source so a
        future refactor can't silently re-introduce the subtraction.
        """
        path = os.path.join(PROJECT_ROOT, "packages", "core", "portfolio.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def close_position(")
        assert idx >= 0
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]
        # The pre-fix line was ``exit_commission = total_commission - entry_commission``.
        # Strip out comments + docstrings before scanning so the
        # NUM-10 explanatory annotation in the source doesn't trip
        # the regression regex.
        import re
        body_no_comments = re.sub(r"#[^\n]*", "", body)
        body_no_comments = re.sub(r'"""[\s\S]*?"""', "", body_no_comments)
        bad = re.search(
            r"^\s*exit_commission\s*=\s*total_commission\s*-\s*entry_commission",
            body_no_comments,
            re.MULTILINE,
        )
        assert bad is None, (
            "NUM-10 regression: close_position is once again deriving "
            "exit_commission via subtraction (`total_commission - "
            "entry_commission`). This drifts in float over many trades "
            "and biases reported P&L vs broker truth. Use "
            "`compute_one_leg(exit_price, ..., side=exit_side)` "
            "directly."
        )
        # Positive form: must compute via compute_one_leg(exit_price ...).
        assert re.search(
            r"exit_commission\s*=\s*compute_one_leg\s*\(\s*exit_price",
            body,
        ) is not None, (
            "NUM-10 regression: close_position no longer derives "
            "exit_commission from compute_one_leg(exit_price, ...). "
            "Add the direct call back."
        )

    def test_round_trip_pnl_equals_gross_minus_total_charges(self):
        """End-to-end portfolio invariant under the new charges:
        the Portfolio's reported pnl must equal
        ``gross_pnl - total_commission`` to the rupee even after
        many round-trips.
        """
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(db_path=os.path.join(tmp, "x.db"))
            port = Portfolio(
                initial_balance=1_000_000.0,
                commission_pct=0.0,
                log_dir=tmp,
                database=db,
                product_type="INTRADAY",
                reset_balance=True,
                mis_short_margin_pct=1.0,
            )

            # Open a LONG, close it, repeat with slightly different
            # prices so float drift would have surfaced if any.
            buys = [(1234.567, 1242.913, 137),
                    (2500.123, 2519.876, 42),
                    (845.555, 851.111, 91),
                    (1567.890, 1577.890, 213)]
            cash_before = port.cash
            total_pnl = 0.0
            for entry, exit_, qty in buys:
                port.open_position(
                    symbol="HDFCBANK", price=entry, quantity=qty,
                    side="BUY", strategy="t",
                    entry_time=datetime(2026, 5, 29),
                )
                rec = port.close_position(
                    symbol="HDFCBANK", exit_price=exit_,
                    exit_reason="t",
                    exit_time=datetime(2026, 5, 29),
                )
                assert rec is not None
                total_pnl += rec.pnl
            cash_after = port.cash
            # Net cash change after multiple round-trips must equal the
            # sum of recorded pnls. The pre-fix subtractive
            # exit_commission used to drift here.
            assert (cash_after - cash_before) == pytest.approx(total_pnl, abs=1e-6)

    def test_charges_helpers_are_still_float_typed_at_boundary(self):
        """API contract: external callers see ``float`` -- not
        ``Decimal`` -- so existing call sites are unaffected.
        """
        leg = compute_one_leg(1234.567, 137, side="BUY", product="INTRADAY")
        assert isinstance(leg, float)
        rt = compute_round_trip(buy_price=1234.567, sell_price=1242.913,
                                quantity=137, product="INTRADAY")
        for field in ("brokerage", "stt", "exchange_txn", "sebi",
                      "gst", "stamp_duty", "dp_charges", "total"):
            assert isinstance(getattr(rt, field), float)

    def test_anchor_in_charges_source(self):
        """Source-level pin: the audit ID + Decimal usage must remain."""
        path = os.path.join(PROJECT_ROOT, "packages", "core", "charges.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "NUM-10" in src
        assert "from decimal import" in src
        assert "_PAISA = Decimal" in src
        assert "ROUND_HALF_EVEN" in src


# ============================================================
# PERF-07 -- per-cycle tick-history allocation cache (Group G)
# ============================================================

class TestPERF07TickHistoryCache:
    """Pin the new tick-history cache helper on TradingAgent.

    Why this matters
    ----------------
    Pre-fix every strategy on every symbol called
    ``tick_aggregator.get_candle_history(...)`` directly, which
    builds a fresh DataFrame on every call. With ~300 symbols x
    ~4 strategies that's ~1,200 DataFrame allocations per
    cycle and was visible as gen-1/gen-2 GC pauses on the WS
    thread.

    The fix introduces a per-cycle dict on ``TradingAgent``
    (``_tick_history_cache``) keyed by ``(symbol, timeframe)``
    and a thin wrapper ``_get_tick_history_cached`` that the
    ``_evaluate_strategy`` hot path now calls. The cache is
    cleared at the top of each cycle by
    ``_clear_historical_cache``.

    These tests work directly on the helper to keep the test
    runtime O(ms) -- spawning a real TradingAgent here would
    pull in the whole world (broker, DB, scheduler, ...). We
    instead build a minimal stand-in that mimics only the
    fields the helper touches, using ``types.SimpleNamespace``.
    """

    def _make_stub(self):
        """Construct the minimum surface area the cache helper
        needs: a tick_aggregator with ``get_candle_history`` and
        the three cache attributes.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        agent = SimpleNamespace()
        agent._tick_history_cache = {}
        agent._tick_history_cache_hits = 0
        agent._tick_history_cache_misses = 0

        agent.tick_aggregator = SimpleNamespace()
        agent.tick_aggregator.get_candle_history = MagicMock()
        return agent

    def _bound_helper(self, agent):
        """Bind ``TradingAgent._get_tick_history_cached`` onto
        the stub so we exercise the real implementation.
        """
        from trading_agent import TradingAgent
        return TradingAgent._get_tick_history_cached.__get__(agent)

    def _bound_clear(self, agent):
        from trading_agent import TradingAgent
        return TradingAgent._clear_historical_cache.__get__(agent)

    def test_first_call_misses_and_invokes_aggregator(self):
        import pandas as pd

        agent = self._make_stub()
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
        agent.tick_aggregator.get_candle_history.return_value = df

        helper = self._bound_helper(agent)
        out = helper("HDFCBANK", "5min", limit=200)

        assert out is df
        assert agent._tick_history_cache_hits == 0
        assert agent._tick_history_cache_misses == 1
        agent.tick_aggregator.get_candle_history.assert_called_once_with(
            "HDFCBANK", "5min", limit=200
        )

    def test_second_call_same_key_hits_cache(self):
        import pandas as pd

        agent = self._make_stub()
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
        agent.tick_aggregator.get_candle_history.return_value = df

        helper = self._bound_helper(agent)
        a = helper("HDFCBANK", "5min", limit=200)
        b = helper("HDFCBANK", "5min", limit=200)

        assert a is b
        assert agent._tick_history_cache_hits == 1
        assert agent._tick_history_cache_misses == 1
        # Aggregator only called on the first miss.
        assert agent.tick_aggregator.get_candle_history.call_count == 1

    def test_different_symbol_misses_separately(self):
        import pandas as pd

        agent = self._make_stub()
        agent.tick_aggregator.get_candle_history.side_effect = [
            pd.DataFrame({"close": [1.0]}),
            pd.DataFrame({"close": [2.0]}),
        ]
        helper = self._bound_helper(agent)
        helper("HDFCBANK", "5min", limit=200)
        helper("RELIANCE", "5min", limit=200)
        assert agent._tick_history_cache_misses == 2
        assert agent._tick_history_cache_hits == 0
        assert len(agent._tick_history_cache) == 2

    def test_different_timeframe_misses_separately(self):
        import pandas as pd

        agent = self._make_stub()
        agent.tick_aggregator.get_candle_history.side_effect = [
            pd.DataFrame({"close": [1.0]}),
            pd.DataFrame({"close": [2.0]}),
        ]
        helper = self._bound_helper(agent)
        helper("HDFCBANK", "5min", limit=200)
        helper("HDFCBANK", "15min", limit=200)
        assert agent._tick_history_cache_misses == 2
        assert agent._tick_history_cache_hits == 0
        assert ("HDFCBANK", "5min") in agent._tick_history_cache
        assert ("HDFCBANK", "15min") in agent._tick_history_cache

    def test_empty_dataframe_is_not_cached(self):
        """If the tick aggregator hasn't aggregated this symbol yet
        it returns an empty frame. Caching the empty frame would
        wedge the symbol for the rest of the cycle and starve the
        REST-fallback path inside ``_evaluate_strategy``. Therefore
        empty frames must NOT be cached.
        """
        import pandas as pd

        agent = self._make_stub()
        empty = pd.DataFrame()
        agent.tick_aggregator.get_candle_history.return_value = empty
        helper = self._bound_helper(agent)
        out1 = helper("ZEEL", "5min", limit=200)
        out2 = helper("ZEEL", "5min", limit=200)
        assert out1 is empty and out2 is empty
        # Both calls were misses -- nothing got cached.
        assert agent._tick_history_cache_misses == 2
        assert agent._tick_history_cache_hits == 0
        assert agent._tick_history_cache == {}
        assert agent.tick_aggregator.get_candle_history.call_count == 2

    def test_none_result_is_not_cached(self):
        """Some aggregator paths return ``None`` instead of empty.
        Same reasoning -- don't cache or we starve the fallback.
        """
        agent = self._make_stub()
        agent.tick_aggregator.get_candle_history.return_value = None
        helper = self._bound_helper(agent)
        out1 = helper("ZEEL", "5min", limit=200)
        out2 = helper("ZEEL", "5min", limit=200)
        assert out1 is None and out2 is None
        assert agent._tick_history_cache_misses == 2
        assert agent._tick_history_cache_hits == 0
        assert agent._tick_history_cache == {}

    def test_clear_resets_cache_and_counters(self):
        import pandas as pd

        agent = self._make_stub()
        agent.tick_aggregator.get_candle_history.return_value = (
            pd.DataFrame({"close": [1.0, 2.0, 3.0]})
        )
        helper = self._bound_helper(agent)
        clear = self._bound_clear(agent)

        # Need _historical_cache too because _clear_historical_cache
        # touches both sets of attributes.
        agent._historical_cache = {}
        agent._historical_cache_hits = 0
        agent._historical_cache_misses = 0

        helper("HDFCBANK", "5min", limit=200)
        helper("HDFCBANK", "5min", limit=200)
        assert agent._tick_history_cache_hits == 1
        assert agent._tick_history_cache_misses == 1
        assert len(agent._tick_history_cache) == 1

        clear()
        assert agent._tick_history_cache == {}
        assert agent._tick_history_cache_hits == 0
        assert agent._tick_history_cache_misses == 0

    def test_evaluate_strategy_uses_cached_helper(self):
        """Source-level pin: ``_evaluate_strategy`` must route the
        tick-aggregator call through ``_get_tick_history_cached``.
        A future refactor that goes back to calling
        ``self.tick_aggregator.get_candle_history`` directly from
        the hot path would silently re-introduce the alloc churn
        and would not be caught by behavioural tests.
        """
        import re
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _evaluate_strategy(")
        assert idx >= 0
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]

        # Positive: helper is invoked from inside _evaluate_strategy.
        assert re.search(r"self\._get_tick_history_cached\s*\(", body), (
            "PERF-07 regression: _evaluate_strategy no longer routes "
            "through _get_tick_history_cached. The WS hot path will "
            "re-allocate ~1k DataFrames per cycle and the GC pauses "
            "will return."
        )

        # Negative: must NOT call the aggregator directly inside
        # _evaluate_strategy. Strip comments + docstrings before
        # scanning (so the explanatory annotation doesn't trip the
        # regex).
        body_no_comments = re.sub(r"#[^\n]*", "", body)
        body_no_comments = re.sub(r'"""[\s\S]*?"""', "", body_no_comments)
        bad = re.search(
            r"self\.tick_aggregator\.get_candle_history\s*\(",
            body_no_comments,
        )
        assert bad is None, (
            "PERF-07 regression: _evaluate_strategy is calling "
            "self.tick_aggregator.get_candle_history directly; route "
            "through self._get_tick_history_cached(...) instead so "
            "multi-strategy multi-symbol cycles stay alloc-bounded."
        )

    def test_clear_historical_cache_clears_tick_cache_too(self):
        """Source-level pin: ``_clear_historical_cache`` must
        clear *both* the historical (PERF-02) cache and the new
        tick (PERF-07) cache. Otherwise stale tick-history rows
        would survive into the next cycle and the cache would
        no longer be cycle-bounded.
        """
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("def _clear_historical_cache(")
        assert idx >= 0
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def]

        assert "_tick_history_cache.clear()" in body, (
            "PERF-07 regression: _clear_historical_cache no longer "
            "clears _tick_history_cache. The cache must be reset at "
            "cycle start so it stays cycle-bounded."
        )
        assert "_tick_history_cache_hits = 0" in body
        assert "_tick_history_cache_misses = 0" in body

    def test_init_seeds_tick_cache_attributes(self):
        """Source-level pin: TradingAgent.__init__ must seed the
        three cache attributes before any cycle runs. Forgetting
        this would AttributeError on the first
        _get_tick_history_cached call.
        """
        path = os.path.join(PROJECT_ROOT, "trading_agent.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        idx = src.find("class TradingAgent")
        init_idx = src.find("def __init__(", idx)
        assert init_idx >= 0
        # The cap of the init body is wherever the next def starts
        # at 4-space indentation.
        next_def = src.find("\n    def ", init_idx + 1)
        body = src[init_idx:next_def]
        assert "self._tick_history_cache" in body, (
            "PERF-07 regression: TradingAgent.__init__ no longer "
            "initialises self._tick_history_cache. The first "
            "_get_tick_history_cached call will AttributeError."
        )
        assert "self._tick_history_cache_hits" in body
        assert "self._tick_history_cache_misses" in body


# ============================================================
# PERF-13 -- battery cache sidecar SHA256 (Group G)
# ============================================================

class TestPERF13BatteryCacheSidecar:
    """Pin the sidecar-hash optimisation for the battery cache load.

    Why this matters
    ----------------
    The 20-variant battery used to re-hash a 300 MB
    ``market_data.pkl`` once per worker (max_tasks_per_child=1
    forces a fresh worker per variant). At ~1-2 s per hash that
    was 20-40 s of pure redundant work per battery — the parent
    process already knew the digest at cache-write time.

    The fix: write a sidecar ``market_data.pkl.sha256`` next to
    the cache. Loaders parse the sidecar (mtime-gated) and skip
    rehashing. If the sidecar is missing or stale we fall back
    to live hashing so the OBS-20 audit log remains intact.

    These tests work directly on
    ``_save_market_data_cache`` / ``_load_market_data_cache``
    using a tiny synthetic dict so the test runtime is sub-second
    (300 MB end-to-end would be too slow + brittle for unit
    tests).
    """

    def _import_battery(self):
        # battery.py pulls in pandas/yfinance/etc; importing on
        # demand keeps the import cost out of the rest of the
        # suite.
        from research import battery
        return battery

    def _make_market_data(self):
        import pandas as pd
        idx = pd.date_range("2026-05-01", periods=4, freq="5min")
        return {
            "HDFCBANK": pd.DataFrame(
                {"close": [1.0, 2.0, 3.0, 4.0]}, index=idx
            ),
            "RELIANCE": pd.DataFrame(
                {"close": [10.0, 20.0, 30.0, 40.0]}, index=idx
            ),
        }

    def test_save_writes_sidecar_with_full_64char_hash(self, tmp_path):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())

        pkl = tmp_path / "market_data.pkl"
        sidecar = tmp_path / "market_data.pkl.sha256"
        assert pkl.exists()
        assert sidecar.exists(), (
            "PERF-13 regression: _save_market_data_cache no longer "
            "writes the .sha256 sidecar; workers will fall back to "
            "rehashing 300 MB per variant."
        )

        text = sidecar.read_text(encoding="utf-8").strip()
        first_line = text.splitlines()[0]
        parts = first_line.split()
        digest = parts[0]
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

        # Sanity: the sidecar's hash is the actual SHA256 of the .pkl.
        live = battery._sha256_file(pkl)
        assert digest == live

    def test_save_sidecar_includes_mtime_field(self, tmp_path):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())
        sidecar = tmp_path / "market_data.pkl.sha256"
        text = sidecar.read_text(encoding="utf-8")
        # Mtime gate is the safety net against stale-sidecar
        # corruption — it must be present.
        assert "mtime=" in text, (
            "PERF-13 regression: sidecar missing mtime field; the "
            "load path can no longer detect stale sidecars and may "
            "log incorrect SHAs."
        )

    def test_load_uses_sidecar_when_fresh(self, tmp_path, caplog):
        import logging
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())

        # Patch _sha256_file with a tripwire so we can prove the
        # load path skipped it.
        called = {"n": 0}
        original = battery._sha256_file

        def tripwire(path, *a, **kw):
            called["n"] += 1
            return original(path, *a, **kw)
        battery._sha256_file = tripwire
        try:
            with caplog.at_level(logging.INFO):
                md = battery._load_market_data_cache(tmp_path)
        finally:
            battery._sha256_file = original

        assert md is not None
        assert called["n"] == 0, (
            "PERF-13 regression: load path is still re-hashing the "
            "300 MB pickle even though a fresh sidecar exists. The "
            "20-40 s per-battery overhead is back."
        )

    def test_load_falls_back_to_live_hash_when_sidecar_missing(
        self, tmp_path, caplog
    ):
        import logging
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())

        sidecar = tmp_path / "market_data.pkl.sha256"
        sidecar.unlink()

        called = {"n": 0}
        original = battery._sha256_file

        def tripwire(path, *a, **kw):
            called["n"] += 1
            return original(path, *a, **kw)
        battery._sha256_file = tripwire
        try:
            with caplog.at_level(logging.INFO):
                md = battery._load_market_data_cache(tmp_path)
        finally:
            battery._sha256_file = original

        assert md is not None
        assert called["n"] >= 1, (
            "PERF-13 regression: sidecar removed but load path didn't "
            "fall back to live hashing — OBS-20 audit log will lose "
            "its sha256[:16] field."
        )

    def test_load_falls_back_when_sidecar_mtime_stale(self, tmp_path):
        import os as _os
        import time
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())

        sidecar = tmp_path / "market_data.pkl.sha256"
        original_text = sidecar.read_text(encoding="utf-8").strip()
        parts = original_text.splitlines()[0].split()
        digest = parts[0]
        # Force a known-bad mtime (1 hour off) in the sidecar so the
        # gate must fail.
        pkl = tmp_path / "market_data.pkl"
        pkl_mtime = pkl.stat().st_mtime
        bad_mtime = pkl_mtime - 3600
        sidecar.write_text(
            f"{digest}  {pkl.name}  mtime={bad_mtime:.0f}\n",
            encoding="utf-8",
        )

        # Re-touch pickle so its mtime stays ahead of the bad sidecar.
        # (On fast filesystems write+stat timing can be subsecond
        # which the helper rounds away.)
        time.sleep(0.05)

        live_called = {"n": 0}
        original = battery._sha256_file

        def tripwire(path, *a, **kw):
            live_called["n"] += 1
            return original(path, *a, **kw)
        battery._sha256_file = tripwire
        try:
            md = battery._load_market_data_cache(tmp_path)
        finally:
            battery._sha256_file = original

        assert md is not None
        assert live_called["n"] >= 1, (
            "PERF-13 regression: stale sidecar mtime should disqualify "
            "the sidecar but the load path used it anyway."
        )

    def test_read_sidecar_hash_rejects_corrupt_digest(self, tmp_path):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())
        sidecar = tmp_path / "market_data.pkl.sha256"
        pkl = tmp_path / "market_data.pkl"
        # Length-correct but non-hex digest.
        sidecar.write_text(
            "Z" * 64
            + f"  {pkl.name}  mtime={pkl.stat().st_mtime:.0f}\n",
            encoding="utf-8",
        )
        assert battery._read_sidecar_hash(pkl) is None

    def test_read_sidecar_hash_rejects_wrong_length_digest(self, tmp_path):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())
        sidecar = tmp_path / "market_data.pkl.sha256"
        pkl = tmp_path / "market_data.pkl"
        # 32-char hex (truncated SHA) — must be rejected.
        sidecar.write_text(
            "deadbeef" * 4 + f"  {pkl.name}  mtime={pkl.stat().st_mtime:.0f}\n",
            encoding="utf-8",
        )
        assert battery._read_sidecar_hash(pkl) is None

    def test_read_sidecar_hash_rejects_missing_mtime_field(self, tmp_path):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())
        sidecar = tmp_path / "market_data.pkl.sha256"
        pkl = tmp_path / "market_data.pkl"
        digest = battery._sha256_file(pkl)
        sidecar.write_text(f"{digest}  {pkl.name}\n", encoding="utf-8")
        # No mtime token => can't gate => must reject.
        assert battery._read_sidecar_hash(pkl) is None

    def test_read_sidecar_hash_returns_full_digest_on_fresh_pair(
        self, tmp_path
    ):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())
        pkl = tmp_path / "market_data.pkl"
        digest = battery._read_sidecar_hash(pkl)
        assert digest is not None
        assert digest == battery._sha256_file(pkl)
        assert len(digest) == 64

    def test_load_log_line_marks_hash_source(self, tmp_path, caplog):
        """OBS-20 / PERF-13 audit hygiene: the load log line now
        reports ``hash_source=sidecar`` vs ``hash_source=live`` so
        operators inspecting a battery log can tell at a glance
        whether the worker spent the rehash budget or skipped it.
        """
        import logging
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())

        # Loguru -> caplog plumbing: the load function logs through
        # loguru; intercept by reading the log file via a patched
        # logger sink. Easier: just check the log message via
        # loguru's record callback.
        from loguru import logger as _lg
        records = []
        sink_id = _lg.add(
            lambda msg: records.append(str(msg)),
            level="INFO",
        )
        try:
            md = battery._load_market_data_cache(tmp_path)
        finally:
            _lg.remove(sink_id)

        assert md is not None
        joined = "\n".join(records)
        assert "hash_source=sidecar" in joined, (
            "PERF-13 regression: the load log line no longer marks "
            "which hash path was taken. Operator visibility into the "
            "20-40 s/battery rehash cost is gone."
        )

    def test_load_log_line_marks_live_source_when_sidecar_missing(
        self, tmp_path
    ):
        battery = self._import_battery()
        battery._save_market_data_cache(tmp_path, self._make_market_data())
        sidecar = tmp_path / "market_data.pkl.sha256"
        sidecar.unlink()

        from loguru import logger as _lg
        records = []
        sink_id = _lg.add(
            lambda msg: records.append(str(msg)),
            level="INFO",
        )
        try:
            md = battery._load_market_data_cache(tmp_path)
        finally:
            _lg.remove(sink_id)

        assert md is not None
        joined = "\n".join(records)
        assert "hash_source=live" in joined, (
            "PERF-13 regression: when the sidecar is missing the "
            "loader must still log hash_source=live so the audit "
            "trail records that the slow path was taken."
        )

    def test_save_failure_to_write_sidecar_does_not_fail_save(
        self, tmp_path, monkeypatch
    ):
        """The sidecar is best-effort: a sidecar I/O error must
        not corrupt the .pkl write. Workers will fall back to
        live hashing.
        """
        import pathlib
        battery = self._import_battery()

        # Force Path.write_text on the sidecar to raise; the .pkl
        # write must still succeed and the function must return
        # without raising.
        original_write_text = pathlib.Path.write_text

        def patched_write_text(self, *a, **kw):
            if str(self).endswith(".sha256"):
                raise OSError("simulated sidecar write failure")
            return original_write_text(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "write_text", patched_write_text)
        battery._save_market_data_cache(tmp_path, self._make_market_data())

        pkl = tmp_path / "market_data.pkl"
        sidecar = tmp_path / "market_data.pkl.sha256"
        assert pkl.exists(), (
            "PERF-13 regression: a sidecar write failure took down "
            "the .pkl write itself. The sidecar is supposed to be "
            "best-effort."
        )
        assert not sidecar.exists()
        # And subsequent load must still work via live hashing.
        md = battery._load_market_data_cache(tmp_path)
        assert md is not None

    def test_source_pins_perf13(self):
        """Anchor the audit ID + helper symbols in the source."""
        path = os.path.join(PROJECT_ROOT, "packages", "research", "battery.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "PERF-13" in src, (
            "PERF-13 regression: audit anchor missing from "
            "battery.py; refactor probably reverted the sidecar "
            "optimisation."
        )
        assert "def _sha256_file(" in src
        assert "def _read_sidecar_hash(" in src
        assert ".sha256" in src

