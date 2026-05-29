"""Regression tests for the audit_2026-05-28 misc-OPEN bucket.

This file picks up the findings that were *outside* the five named
phases (1-5). Each finding has its own block. Tests are by-source for
contract assertions and by-runtime where the behaviour is observable
end-to-end without spawning a daemon.

Findings covered in this initial commit:

* **NUM-01** (Critical) -- short MIS margin model. Backtester pre-fix
  locked 100% of short notional as collateral; live broker reality is
  ~20% (MIS margin). New ``Portfolio.mis_short_margin_pct`` knob,
  default 1.0 in code (legacy preservation) and 0.20 in production
  config. Per-position ``cash_locked`` field + DB column persists the
  exact lock so a daemon restart between open and close still
  releases the right amount.

Group A in the misc-OPEN sequencing.
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
