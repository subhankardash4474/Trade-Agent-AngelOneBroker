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
