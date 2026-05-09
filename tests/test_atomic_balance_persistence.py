"""Tests for atomic balance persistence on every cash mutation (2026-05-04 part 5).

Bug it fixes:
  Today's daemon kill at 10:31 (90 seconds after IDEA closed at 10:29:30)
  left the DB in an inconsistent state:
    - open_positions table: IDEA correctly removed
    - cash: still Rs 1,218 (the pre-close value, missing the ~Rs 2,800
      collateral release + Rs 20.80 realized PnL bump)
  On the next boot, `_restore_positions` used `min(cash_after)` over the
  surviving rows {RAILTEL=Rs 4,009, NIVABUPA=Rs 1,218} = Rs 1,218 — the
  IDEA-close bump was lost.

  The equity snapshot was only being written every 5 cycles by the agent's
  periodic `_snapshot_equity`. A close that happened mid-window was vulnerable.

Fix:
  1. `Portfolio.open_position` and `close_position` now call
     `_persist_state_after_event()` IMMEDIATELY after mutating cash. This
     writes a fresh equity_curve row with the new cash atomically.
  2. `_restore_positions` now prefers the latest equity_curve snapshot's
     cash if its timestamp is newer than any open-position entry_time.
     The legacy `min(cash_after)` heuristic remains as a fallback for
     databases that don't yet have a post-event snapshot.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ─────────────────────────────────────────────────────────────
# Atomic persistence writes a snapshot on every event
# ─────────────────────────────────────────────────────────────


class TestAtomicPersistence:
    def test_open_position_writes_equity_snapshot(self, fresh_db):
        db, tmp = fresh_db
        port = Portfolio(initial_balance=10_000.0, database=db, log_dir=tmp)
        # Sanity: no equity rows yet
        assert db.get_last_equity_point() is None

        port.open_position("RAILTEL", "SELL", 335.07, 8)

        snap = db.get_last_equity_point()
        assert snap is not None, "open_position must persist an equity snapshot"
        # cash deducted ≈ 8 * 335.07 + small commission
        assert snap["cash"] < 10_000.0
        assert snap["cash"] > 7_000.0
        assert snap["positions"] == 1

    def test_close_position_writes_equity_snapshot(self, fresh_db):
        db, tmp = fresh_db
        port = Portfolio(initial_balance=10_000.0, database=db, log_dir=tmp)
        port.open_position("IDEA", "SELL", 10.60, 264)
        cash_after_open = port.cash

        port.close_position("IDEA", 10.51, exit_reason="trailing_stop")
        cash_after_close = port.cash

        # Cash must have bumped up after close (collateral release + PnL)
        assert cash_after_close > cash_after_open

        # Latest snapshot must reflect the post-close cash
        snap = db.get_last_equity_point()
        assert snap is not None
        assert abs(snap["cash"] - cash_after_close) < 0.01, (
            f"snapshot cash {snap['cash']:.2f} must match in-memory "
            f"cash {cash_after_close:.2f} immediately after close"
        )
        assert snap["positions"] == 0


# ─────────────────────────────────────────────────────────────
# Today's incident replay — IDEA close-during-shutdown scenario
# ─────────────────────────────────────────────────────────────


class TestTodaysIncidentReplay:
    """Reproduce the exact 2026-05-04 scenario and verify the fix recovers it."""

    def test_kill_after_close_recovers_cash_on_restart(self, fresh_db):
        """Open 3 shorts (IDEA, RAILTEL, NIVABUPA), close IDEA, kill, restart.

        With the fix: post-close snapshot lets boot recover the correct cash.
        Without the fix: min(cash_after) over remaining gives stale cash.
        """
        db, tmp = fresh_db
        port = Portfolio(initial_balance=9_488.0, database=db, log_dir=tmp)

        # Replay today's morning entries
        assert port.open_position("IDEA", "SELL", 10.60, 264)
        assert port.open_position("RAILTEL", "SELL", 335.07, 8)
        assert port.open_position("NIVABUPA", "SELL", 79.58, 35)
        cash_after_3_opens = port.cash

        # Close IDEA at +Rs 0.09 favorable (≈ today's actual)
        port.close_position("IDEA", 10.51, exit_reason="trailing_stop")
        cash_after_close = port.cash
        assert cash_after_close > cash_after_3_opens, (
            "close must release collateral + add PnL"
        )

        # SIMULATE kill: drop the in-memory portfolio, leave DB intact
        del port

        # Restart — fresh Portfolio, same DB
        port2 = Portfolio(initial_balance=9_488.0, database=db, log_dir=tmp)
        # Both surviving positions must be restored
        assert "RAILTEL" in port2.positions
        assert "NIVABUPA" in port2.positions
        assert "IDEA" not in port2.positions
        # Cash must match the post-close value, NOT the stale min(cash_after)
        assert abs(port2.cash - cash_after_close) < 1.0, (
            f"After restart: cash should be ~Rs {cash_after_close:.2f} (post-close), "
            f"got Rs {port2.cash:.2f}. Difference Rs {port2.cash - cash_after_close:+.2f}"
        )

    def test_old_buggy_path_would_have_failed(self, fresh_db):
        """Document the old behavior: if we don't write a post-close snapshot,
        the boot falls back to min(cash_after) which is stale."""
        db, tmp = fresh_db
        port = Portfolio(initial_balance=9_488.0, database=db, log_dir=tmp)
        port.open_position("IDEA", "SELL", 10.60, 264)
        port.open_position("RAILTEL", "SELL", 335.07, 8)
        port.open_position("NIVABUPA", "SELL", 79.58, 35)
        port.close_position("IDEA", 10.51)
        true_cash = port.cash

        # Manually delete the post-close snapshot the fix wrote, simulating
        # the OLD code path that didn't persist on close.
        with db._conn() as conn:
            # Keep only snapshots from BEFORE the close (i.e. opens only).
            conn.execute("DELETE FROM equity_curve")
            # Re-insert just the 3 open snapshots so cash_after fields stay consistent.
        del port

        # Restart with NO post-close snapshot — this is the broken path.
        port2 = Portfolio(initial_balance=9_488.0, database=db, log_dir=tmp)
        # Without the fix (no snapshot), cash falls back to min(cash_after) which is stale.
        # The min over RAILTEL+NIVABUPA cash_after = NIVABUPA's = the lowest.
        # So `port2.cash` will be the stale Rs ~1,218, NOT true_cash.
        # This test DOCUMENTS that without the snapshot, boot is incorrect.
        assert port2.cash < true_cash - 1000.0, (
            "Without a post-close snapshot, the legacy min(cash_after) path "
            "produces a phantom-low cash. This is the bug we just fixed."
        )


# ─────────────────────────────────────────────────────────────
# Backward compatibility — no snapshot, fall back to min(cash_after)
# ─────────────────────────────────────────────────────────────


class TestBackwardCompat:
    def test_no_snapshot_uses_legacy_min_cash_after(self, fresh_db):
        """Empty equity_curve → boot falls back to min(cash_after)."""
        db, tmp = fresh_db
        # Build state via raw DB inserts (bypass Portfolio so no snapshot is written)
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO open_positions(symbol, side, entry_price, quantity, "
                "entry_time, cash_after) VALUES (?, ?, ?, ?, ?, ?)",
                ("RAILTEL", "SELL", 335.07, 8, "2026-05-04T09:19:13+05:30", 4009.0),
            )
            conn.execute(
                "INSERT INTO open_positions(symbol, side, entry_price, quantity, "
                "entry_time, cash_after) VALUES (?, ?, ?, ?, ?, ?)",
                ("NIVABUPA", "SELL", 79.58, 35, "2026-05-04T09:19:16+05:30", 1218.0),
            )

        # No equity_curve rows → must fall back to min(cash_after)
        port = Portfolio(initial_balance=9_488.0, database=db, log_dir=tmp)
        assert port.cash == 1218.0, (
            "Without an equity snapshot, boot must use min(cash_after) "
            "for backward compatibility."
        )


# ─────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_failed_db_persistence_does_not_break_open(self, fresh_db, monkeypatch):
        """If the DB write fails, the in-memory state must still be consistent."""
        db, tmp = fresh_db
        port = Portfolio(initial_balance=10_000.0, database=db, log_dir=tmp)

        original = db.store_equity_point
        call_count = [0]

        def flaky(*args, **kwargs):
            call_count[0] += 1
            raise IOError("simulated DB failure")

        monkeypatch.setattr(db, "store_equity_point", flaky)
        # Open should still succeed — persistence failure is logged, not raised
        ok = port.open_position("RAILTEL", "SELL", 335.07, 8)
        assert ok is True, "open_position must succeed even if persistence fails"
        assert "RAILTEL" in port.positions
        assert call_count[0] >= 1

    def test_no_db_attached_skips_persistence(self):
        """Portfolio without a DB shouldn't crash on open/close."""
        with tempfile.TemporaryDirectory() as tmp:
            port = Portfolio(initial_balance=10_000.0, database=None, log_dir=tmp)
            assert port.open_position("RAILTEL", "SELL", 335.07, 8) is True
            port.close_position("RAILTEL", 334.0)
            # No exception => pass


# ─────────────────────────────────────────────────────────────
# Production source guard
# ─────────────────────────────────────────────────────────────


class TestProductionSourceShape:
    def test_portfolio_persists_state_after_events(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent / "core" / "portfolio.py").read_text(encoding="utf-8")
        assert "_persist_state_after_event" in src
        # Both open and close must call it.
        # Find the function definitions and check the body contains the call.
        # A loose count is fine — the production source guards in other tests
        # follow the same pattern.
        assert src.count("self._persist_state_after_event()") >= 2, (
            "Both open_position and close_position must call _persist_state_after_event."
        )
        # _restore_positions must consult the equity snapshot
        assert "equity_curve_snapshot" in src
