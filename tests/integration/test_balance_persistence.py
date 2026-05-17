"""
Tests for running-balance persistence across agent restarts.

Verifies:
  - Portfolio seeds cash from the DB equity snapshot (not config) when history exists.
  - Fresh DB → uses config initial_balance.
  - `reset_balance=True` ignores DB and uses config.
  - RiskManager uses the historical peak for drawdown, not today's seed.
  - DB snapshot with open positions is handled correctly (position restore
    recomputes cash from cost basis, not equity).
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from core.database import Database
from core.portfolio import Portfolio
from core.risk_manager import RiskManager


@pytest.fixture
def tmp_db_and_log():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        db = Database(db_path)
        yield db, tmp


def _seed_equity(db: Database, equity: float, cash: float, positions: int = 0,
                 when: datetime = None):
    """Directly insert an equity_curve row (bypasses Portfolio for test setup)."""
    ts = (when or datetime.now()).isoformat()
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO equity_curve (timestamp, equity, cash, positions) "
            "VALUES (?, ?, ?, ?)",
            (ts, equity, cash, positions),
        )
        conn.commit()


class TestBalancePersistence:
    def test_fresh_db_uses_config_balance(self, tmp_db_and_log):
        db, log_dir = tmp_db_and_log
        pf = Portfolio(initial_balance=10_000.0, database=db, log_dir=log_dir)
        assert pf.cash == 10_000.0
        assert pf.initial_balance == 10_000.0

    def test_continues_from_last_snapshot(self, tmp_db_and_log):
        db, log_dir = tmp_db_and_log
        _seed_equity(db, equity=9_634.02, cash=9_634.02, positions=0)
        pf = Portfolio(initial_balance=10_000.0, database=db, log_dir=log_dir)
        # Cash should reflect yesterday's loss, not config's seed.
        assert pf.cash == pytest.approx(9_634.02)
        # initial_balance stays as the config value (used for reference only)
        assert pf.initial_balance == 10_000.0

    def test_reset_flag_ignores_history(self, tmp_db_and_log):
        db, log_dir = tmp_db_and_log
        _seed_equity(db, equity=9_634.02, cash=9_634.02, positions=0)
        pf = Portfolio(
            initial_balance=10_000.0,
            database=db,
            log_dir=log_dir,
            reset_balance=True,
        )
        assert pf.cash == 10_000.0

    def test_snapshot_with_open_positions_defers_to_restore(self, tmp_db_and_log):
        """If snapshot shows N open positions, don't naively use its cash —
        the position restore will rebuild cash from cost basis."""
        db, log_dir = tmp_db_and_log
        # Snapshot had 2 positions open at the time it was taken
        _seed_equity(db, equity=9_800.0, cash=3_000.0, positions=2)
        pf = Portfolio(initial_balance=10_000.0, database=db, log_dir=log_dir)
        # No actual open_positions rows exist, so cash falls back to config seed
        # (the restore path found nothing to restore).
        assert pf.cash == 10_000.0

    def test_gains_also_carry_forward(self, tmp_db_and_log):
        """Win day → tomorrow starts richer."""
        db, log_dir = tmp_db_and_log
        _seed_equity(db, equity=10_250.0, cash=10_250.0, positions=0)
        pf = Portfolio(initial_balance=10_000.0, database=db, log_dir=log_dir)
        assert pf.cash == pytest.approx(10_250.0)


class TestPeakBalancePersistence:
    def test_peak_read_from_db(self, tmp_db_and_log):
        db, _ = tmp_db_and_log
        now = datetime.now()
        _seed_equity(db, equity=10_000.0, cash=10_000.0, when=now - timedelta(days=5))
        _seed_equity(db, equity=10_500.0, cash=10_500.0, when=now - timedelta(days=3))
        _seed_equity(db, equity=9_800.0, cash=9_800.0, when=now - timedelta(days=1))
        peak = db.get_peak_equity()
        assert peak == pytest.approx(10_500.0)

    def test_risk_manager_respects_historical_peak(self, tmp_db_and_log):
        db, _ = tmp_db_and_log
        config = {"risk": {"max_drawdown_pct": 10.0, "drawdown_halt_pct": 30.0}}
        # Today's balance is 9_800, but the lifetime peak was 10_500
        # (implied drawdown ~6.7%, well below halt threshold).
        rm = RiskManager(config, initial_balance=9_800.0, peak_balance=10_500.0)
        assert rm.state.current_balance == 9_800.0
        assert rm.state.peak_balance == 10_500.0

    def test_stale_peak_retained_so_real_drawdown_halts_p2_audit_fix(self):
        """P2 logic-edges (2026-05-17): the OLD code silently clamped the
        DB peak down to current balance whenever the implied drawdown
        exceeded the halt threshold ("treating as stale"). That HID the
        very condition the halt was designed to enforce -- a real 30%+
        drawdown should HALT trading, not be hand-waved away.

        New contract: the real peak is RETAINED. The circuit breaker
        fires on the next can_trade check. Operator can explicitly
        recover via --reset-balance if the DB really is stale.
        """
        config = {"risk": {"drawdown_halt_pct": 30.0}}
        # Peak 14_000 vs current 9_000 -> ~36% drawdown -> over halt threshold
        rm = RiskManager(config, initial_balance=9_000.0, peak_balance=14_000.0)
        # Peak is preserved (NOT clamped) so drawdown gate trips
        assert rm.state.peak_balance == 14_000.0
        assert rm.state.current_balance == 9_000.0
        # Compute current drawdown
        dd = (rm.state.peak_balance - rm.state.current_balance) / rm.state.peak_balance * 100
        assert dd >= 30.0  # confirms the halt threshold WILL trip on next check

    def test_risk_manager_falls_back_to_balance_if_no_peak(self, tmp_db_and_log):
        config = {"risk": {}}
        rm = RiskManager(config, initial_balance=10_000.0, peak_balance=None)
        assert rm.state.peak_balance == 10_000.0

    def test_peak_never_goes_below_current(self, tmp_db_and_log):
        """If DB peak is stale/lower than current, current wins."""
        config = {"risk": {}}
        rm = RiskManager(config, initial_balance=10_500.0, peak_balance=9_000.0)
        assert rm.state.peak_balance == 10_500.0


class TestDatabaseHelpers:
    def test_get_last_equity_point_empty(self, tmp_db_and_log):
        db, _ = tmp_db_and_log
        assert db.get_last_equity_point() is None
        assert db.get_peak_equity() is None

    def test_get_last_equity_point_returns_most_recent(self, tmp_db_and_log):
        db, _ = tmp_db_and_log
        now = datetime.now()
        _seed_equity(db, equity=10_000.0, cash=10_000.0, when=now - timedelta(hours=2))
        _seed_equity(db, equity=9_900.0, cash=9_900.0, when=now - timedelta(hours=1))
        _seed_equity(db, equity=9_800.0, cash=9_800.0, when=now)
        snap = db.get_last_equity_point()
        assert snap is not None
        assert snap["equity"] == pytest.approx(9_800.0)
        assert snap["cash"] == pytest.approx(9_800.0)
        assert snap["positions"] == 0
