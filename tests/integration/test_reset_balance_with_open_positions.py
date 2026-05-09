"""Tests for --reset-balance honouring the new cash budget even with
open positions (2026-05-05 fix).

Bug it fixes:
  Live observation 2026-05-05 10:41 IST: user edited config.yaml's
  initial_balance from 10000 -> 25000 and ran:
      python run_daemon.py --paper --reset-balance
  Three SELL positions (ENGINERSIN, PREMIERENE, BLUEJET) were already in
  the DB from the earlier daemon. The new daemon's boot logged:
      Restored 3 open positions from database | Cash: Rs 2,482.60
        (source=equity_curve_snapshot)
  Even though config said Rs 25,000 and --reset-balance was passed.

  Root cause:
    Portfolio.__init__ guarded the standalone cash-restore branch with
    `not reset_balance`, so when the snapshot had `last_positions == 0`
    the flag was honoured. But _restore_positions() had a separate
    snapshot-cash override path that ran unconditionally whenever ANY
    open position existed. The two-branch design predated --reset-balance
    and nobody noticed the gap until you tried to top up cash mid-session
    with positions held.

Fix:
  Portfolio now stashes the reset_balance flag as self._reset_balance.
  In _restore_positions, when self._reset_balance is True we explicitly
  KEEP self.cash at self.initial_balance (the config value) instead of
  overriding it from the snapshot. We also write a fresh equity_curve
  row immediately so the new cash baseline is durable.

Semantics:
  --reset-balance now means: "cash := initial_balance from config,
   regardless of held positions." Positions are still restored intact
  (no auto-close). Open-position cost basis is treated as separate
  committed capital that flows back when the position closes — the
  user's mental model of "top up my budget without disturbing trades
  in flight."
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.database import Database
from core.portfolio import Portfolio

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        db = Database(db_path)
        yield db, tmp


def _seed_open_positions(db: Database):
    """Seed today's actual broken-state — 3 SELL positions matching the
    live 2026-05-05 boot log, plus a stale equity snapshot showing
    Rs 2,482 cash and 3 positions."""
    db.save_open_position(
        symbol="ENGINERSIN", side="SELL", entry_price=258.68,
        quantity=10, entry_time="2026-05-05T09:16:44+05:30",
        stop_loss=262.41, take_profit=249.64,
        strategy="supertrend_follow", order_id="TEST-1",
        cash_after=6894.78,
    )
    db.save_open_position(
        symbol="PREMIERENE", side="SELL", entry_price=1031.64,
        quantity=2, entry_time="2026-05-05T10:13:50+05:30",
        stop_loss=1039.28, take_profit=1013.54,
        strategy="mean_reversion", order_id="TEST-2",
        cash_after=4920.88,
    )
    db.save_open_position(
        symbol="BLUEJET", side="SELL", entry_price=487.61,
        quantity=5, entry_time="2026-05-05T10:26:52+05:30",
        stop_loss=492.41, take_profit=480.64,
        strategy="mean_reversion", order_id="TEST-3",
        cash_after=2482.60,
    )
    db.store_equity_point(equity=9578.36, cash=2482.60, positions=3)


# ─────────────────────────────────────────────────────────────
# The headline fix
# ─────────────────────────────────────────────────────────────


class TestResetBalanceWithOpenPositions:
    def test_reset_balance_uses_config_value_even_with_open_positions(self, fresh_db):
        """The exact 2026-05-05 scenario: 3 open positions in DB, snapshot
        shows Rs 2,482 cash. User reboots with --reset-balance and
        initial_balance=25000. Expected: cash := Rs 25,000 (NOT Rs 2,482)."""
        db, tmp = fresh_db
        _seed_open_positions(db)

        port = Portfolio(
            initial_balance=25_000.0,
            database=db,
            log_dir=tmp,
            reset_balance=True,
        )

        assert port.cash == 25_000.0, (
            f"Expected cash to be reset to Rs 25,000, got Rs {port.cash:,.2f}"
        )

    def test_open_positions_are_still_restored_after_reset(self, fresh_db):
        """Cash reset must NOT close positions — that's the whole point
        of the user-facing semantic 'top up budget without touching
        trades in flight'."""
        db, tmp = fresh_db
        _seed_open_positions(db)

        port = Portfolio(
            initial_balance=25_000.0,
            database=db,
            log_dir=tmp,
            reset_balance=True,
        )

        assert len(port.positions) == 3
        assert "ENGINERSIN" in port.positions
        assert "PREMIERENE" in port.positions
        assert "BLUEJET" in port.positions

    def test_reset_persists_fresh_equity_snapshot(self, fresh_db):
        """The new cash baseline must hit the equity_curve immediately
        so the next restart (without --reset-balance) reads the
        post-reset value, not the stale pre-reset one."""
        db, tmp = fresh_db
        _seed_open_positions(db)

        # Pre-reset snapshot
        pre = db.get_last_equity_point()
        assert pre["cash"] == pytest.approx(2482.60, abs=0.01)

        Portfolio(
            initial_balance=25_000.0,
            database=db,
            log_dir=tmp,
            reset_balance=True,
        )

        # A fresh row must have been written by the post-restore call
        post = db.get_last_equity_point()
        assert post["cash"] == pytest.approx(25_000.0, abs=0.01), (
            f"Equity snapshot must reflect the new cash baseline; "
            f"got Rs {post['cash']:,.2f}"
        )
        assert post["positions"] == 3
        # Equity at cost = cash + sum(entry_price * qty)
        # = 25000 + (258.68*10 + 1031.64*2 + 487.61*5)
        # = 25000 + 2586.80 + 2063.28 + 2438.05
        # = 32088.13
        assert post["equity"] == pytest.approx(32088.13, abs=1.0)


# ─────────────────────────────────────────────────────────────
# Regressions: existing behaviour without the flag must still work
# ─────────────────────────────────────────────────────────────


class TestNoResetStillUsesSnapshot:
    def test_without_reset_cash_still_loads_from_snapshot(self, fresh_db):
        """The default boot path (no --reset-balance) must keep using the
        snapshot's cash — that's the production hot-restart behaviour
        protected by the atomic balance persistence fix from 2026-05-04."""
        db, tmp = fresh_db
        _seed_open_positions(db)

        port = Portfolio(
            initial_balance=10_000.0,  # config value, should be ignored
            database=db,
            log_dir=tmp,
            reset_balance=False,
        )

        assert port.cash == pytest.approx(2482.60, abs=0.01)
        assert len(port.positions) == 3


class TestResetWithNoPositions:
    def test_clean_morning_reset_uses_config_balance(self, fresh_db):
        """No DB history, no open positions, --reset-balance: trivially
        should be the config value."""
        db, tmp = fresh_db

        port = Portfolio(
            initial_balance=25_000.0,
            database=db,
            log_dir=tmp,
            reset_balance=True,
        )

        assert port.cash == 25_000.0
        assert len(port.positions) == 0

    def test_reset_with_history_but_no_open_positions(self, fresh_db):
        """DB has a stale snapshot (e.g. yesterday's EOD) but no open
        positions today. --reset-balance must still hit the config value."""
        db, tmp = fresh_db
        # Yesterday's EOD: Rs 8,500 cash, no positions
        db.store_equity_point(equity=8500.0, cash=8500.0, positions=0)

        port = Portfolio(
            initial_balance=25_000.0,
            database=db,
            log_dir=tmp,
            reset_balance=True,
        )

        assert port.cash == 25_000.0


# ─────────────────────────────────────────────────────────────
# End-to-end: reset → restart-without-reset preserves new baseline
# ─────────────────────────────────────────────────────────────


class TestPostResetIsDurable:
    def test_subsequent_boot_without_flag_picks_up_reset_cash(self, fresh_db):
        """Once --reset-balance has run, the next boot WITHOUT the flag
        should see the new Rs 25,000 baseline (because the reset wrote a
        fresh snapshot whose timestamp is newer than any open position)."""
        db, tmp = fresh_db
        _seed_open_positions(db)

        # First boot: --reset-balance
        Portfolio(
            initial_balance=25_000.0,
            database=db,
            log_dir=tmp,
            reset_balance=True,
        )

        # Second boot: NO flag — should resolve cash from the fresh snapshot
        port2 = Portfolio(
            initial_balance=10_000.0,  # noise: should be ignored
            database=db,
            log_dir=tmp,
            reset_balance=False,
        )
        assert port2.cash == pytest.approx(25_000.0, abs=0.01)
        assert len(port2.positions) == 3
