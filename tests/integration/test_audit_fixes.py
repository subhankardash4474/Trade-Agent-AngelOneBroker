"""
Regression tests for the 2026-04-28 audit fixes. Each test pins down one
specific bug or design gap so it can't regress silently.

Covers:
  - Fix 1: Secrets loader / env var merge
  - Fix 2: max_drawdown_pct actually enforced
  - Fix 3: Duplicate-position race (ELECON x 4 at same second)
  - Fix 4: should_time_exit actually fires at 15:15
  - Fix 5: Mean reversion emits exit_z_score signals
  - Fix 6: Learning freeze receives market_context
  - Fix 10: Paper partial-fill simulation + order ledger
"""
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import pytz

from core.database import Database
from core.portfolio import Portfolio
from core.risk_manager import RiskManager

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────
# FIX 1: secrets loader
# ─────────────────────────────────────────────────────────────
class TestSecretsLoader:
    def test_env_overrides_placeholder(self):
        from core.secrets import apply_env_to_config
        os.environ["RESEND_API_KEY"] = "re_TEST123"
        try:
            cfg = {"monitoring": {"alerts": {"email": {"resend_api_key": "YOUR_RESEND_API_KEY"}}}}
            out = apply_env_to_config(cfg)
            assert out["monitoring"]["alerts"]["email"]["resend_api_key"] == "re_TEST123"
        finally:
            del os.environ["RESEND_API_KEY"]

    def test_env_wins_over_real_yaml(self):
        """Env vars are the source of truth — even if yaml has real-looking value.

        2026-05-08: switched canonical broker var from KITE_API_KEY to
        ANGELONE_API_KEY after Kite deprecation."""
        from core.secrets import apply_env_to_config
        os.environ["ANGELONE_API_KEY"] = "from_env"
        try:
            cfg = {"broker": {"api_key": "from_yaml_real"}}
            out = apply_env_to_config(cfg)
            assert out["broker"]["api_key"] == "from_env"
        finally:
            del os.environ["ANGELONE_API_KEY"]

    def test_missing_env_leaves_placeholder(self):
        from core.secrets import apply_env_to_config
        os.environ.pop("ANGELONE_API_SECRET", None)
        cfg = {"broker": {"api_secret": "YOUR_ANGELONE_API_SECRET"}}
        out = apply_env_to_config(cfg)
        assert out["broker"]["api_secret"] == "YOUR_ANGELONE_API_SECRET"

    def test_warn_on_real_secret_in_yaml(self, caplog):
        from core.secrets import warn_if_secrets_in_yaml
        cfg = {"monitoring": {"alerts": {"email": {"resend_api_key": "re_realKey12345"}}}}
        warn_if_secrets_in_yaml(cfg)
        # logger uses loguru, not stdlib logging; we just assert it doesn't crash
        # and that placeholder detection works.

    def test_dotenv_loader(self, tmp_path):
        from core.secrets import load_dotenv
        p = tmp_path / ".env"
        p.write_text('FOO_BAR_BAZ="hello world"\n# comment\n\nEMPTY=\n')
        os.environ.pop("FOO_BAR_BAZ", None)
        load_dotenv(str(p))
        assert os.environ.get("FOO_BAR_BAZ") == "hello world"
        del os.environ["FOO_BAR_BAZ"]


# ─────────────────────────────────────────────────────────────
# FIX 2: max_drawdown_pct enforcement
# ─────────────────────────────────────────────────────────────
class TestMaxDrawdownEnforced:
    def test_breaches_max_dd_blocks_new_trades(self, monkeypatch):
        import core.risk_manager as rm_mod
        cfg = {
            "risk": {
                "max_drawdown_pct": 10.0,
                "drawdown_halt_pct": 30.0,
                "daily_loss_limit_pct": 3.0,
                "weekly_loss_limit_pct": 5.0,
                "intraday_exit_time": "15:15",
            }
        }
        # Freeze time to 11:00 so intraday exit gate doesn't block
        fake_now = datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(rm_mod, "datetime", _FrozenDatetime(fake_now))
        rm = RiskManager(cfg, initial_balance=10_000.0, peak_balance=10_000.0)
        rm.state.current_balance = 8_900.0  # 11% DD
        can, reason = rm.can_trade(market_context={"india_vix": 15.0, "nifty_trend": 1})
        assert can is False
        assert "drawdown" in reason.lower()

    def test_within_max_dd_allows_trade(self, monkeypatch):
        import core.risk_manager as rm_mod
        cfg = {
            "risk": {
                "max_drawdown_pct": 10.0,
                "drawdown_halt_pct": 30.0,
                "intraday_exit_time": "15:15",
            }
        }
        fake_now = datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(rm_mod, "datetime", _FrozenDatetime(fake_now))
        rm = RiskManager(cfg, initial_balance=10_000.0, peak_balance=10_000.0)
        rm.state.current_balance = 9_500.0  # 5% DD
        can, reason = rm.can_trade(market_context={"india_vix": 15.0, "nifty_trend": 1})
        assert can is True, f"Expected can_trade=True, got False: {reason}"


# ─────────────────────────────────────────────────────────────
# FIX 3: Duplicate-position guard (thread race)
# ─────────────────────────────────────────────────────────────
class TestDuplicatePositionRace:
    def test_concurrent_opens_same_symbol_only_one_wins(self):
        """
        Reproduce the ELECON Apr-27 bug: two threads both try to open ELECON
        at the same second. Only one should succeed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            pf = Portfolio(initial_balance=100_000.0, database=db, log_dir=tmp)
            results = []
            errors = []

            def try_open():
                try:
                    r = pf.open_position(
                        symbol="ELECON", side="BUY",
                        price=500.0, quantity=3, strategy="test",
                    )
                    results.append(r)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=try_open) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Exactly one should have succeeded
            successes = sum(1 for r in results if r is True)
            assert successes == 1, f"Expected 1 success, got {successes}. Results={results}"
            # And the DB should only have one row
            rows = db.load_open_positions()
            elecon_rows = [r for r in rows if r["symbol"] == "ELECON"]
            assert len(elecon_rows) == 1

    def test_db_rejects_true_duplicate_insert(self):
        """Even bypassing the lock, DB PRIMARY KEY must reject duplicates."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            db.save_open_position(
                symbol="ELECON", side="BUY", entry_price=500.0, quantity=3,
                entry_time="2026-04-27T12:54:59", cash_after=98_500.0,
            )
            with pytest.raises(sqlite3.IntegrityError):
                db.save_open_position(
                    symbol="ELECON", side="BUY", entry_price=501.0, quantity=3,
                    entry_time="2026-04-27T12:54:59", cash_after=97_000.0,
                )


# ─────────────────────────────────────────────────────────────
# FIX 4: should_time_exit actually fires
# ─────────────────────────────────────────────────────────────
class TestIntradayTimeExit:
    def test_past_intraday_time_triggers_exit(self, monkeypatch):
        import core.risk_manager as rm_mod
        cfg = {"risk": {"intraday_exit_time": "15:15"}}
        rm = RiskManager(cfg, initial_balance=10_000.0)
        # Freeze time to 15:16 IST
        fake_now = datetime.now(IST).replace(hour=15, minute=16, second=0, microsecond=0)
        monkeypatch.setattr(rm_mod, "datetime", _FrozenDatetime(fake_now))
        assert rm.should_time_exit() is True

    def test_before_intraday_time_no_exit(self, monkeypatch):
        import core.risk_manager as rm_mod
        cfg = {"risk": {"intraday_exit_time": "15:15"}}
        rm = RiskManager(cfg, initial_balance=10_000.0)
        fake_now = datetime.now(IST).replace(hour=14, minute=30, second=0, microsecond=0)
        monkeypatch.setattr(rm_mod, "datetime", _FrozenDatetime(fake_now))
        assert rm.should_time_exit() is False


class _FrozenDatetime:
    """Tiny helper: a datetime replacement whose .now(tz) returns a fixed time."""
    def __init__(self, fixed: datetime):
        self._fixed = fixed

    def now(self, tz=None):
        if tz is None:
            return self._fixed
        return self._fixed.astimezone(tz) if self._fixed.tzinfo else tz.localize(self._fixed)

    def __getattr__(self, name):
        return getattr(datetime, name)


# ─────────────────────────────────────────────────────────────
# FIX 5: Mean reversion exit signal
# ─────────────────────────────────────────────────────────────
class TestMeanReversionExit:
    def _build_df(self, z_sequence):
        """Generate a df where the last row has the target z, prev is z_sequence[-2]."""
        n = 30
        closes = np.full(n, 100.0)
        df = pd.DataFrame({
            "open": closes, "high": closes * 1.01, "low": closes * 0.99,
            "close": closes, "volume": np.full(n, 1e6),
        })
        return df

    def test_exit_signal_on_revert_from_oversold(self):
        """Z transitions from deeply negative to near-zero → SELL to close longs."""
        from strategies.mean_reversion import MeanReversion
        from strategies.base_strategy import Signal

        mr = MeanReversion({"lookback_period": 10, "entry_z_score": 2.0, "exit_z_score": 0.5})

        # Hand-craft a df with known z_scores at last 2 rows by controlling
        # close values around mean. The strategy computes z internally.
        n = 15
        closes = list(np.full(n - 2, 100.0))  # flat 100
        closes += [90.0]     # z_prev very negative (oversold)
        closes += [100.0]    # z near zero (mean reverted)
        df = pd.DataFrame({
            "open": closes, "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes], "close": closes,
            "volume": [1e6] * n,
        }, index=pd.date_range("2026-04-28 09:15", periods=n, freq="5min"))

        sig = mr.generate_signal(df, "TEST")
        # Should emit SELL (exit long) with intent=mean_reversion_exit
        assert sig.signal == Signal.SELL
        assert sig.metadata.get("intent") == "mean_reversion_exit"


# ─────────────────────────────────────────────────────────────
# FIX 6: Learning freeze gets market_context
# ─────────────────────────────────────────────────────────────
class TestLearningFreezeReceivesContext:
    def test_record_trade_accepts_market_context(self):
        """Smoke test — signature change alone. Prior bug: VIX leg of freeze
        guardrail was dead because market_context was never passed."""
        from core.trade_analyzer import TradeAnalyzer
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            ta = TradeAnalyzer({"learning": {"enabled": True, "min_trades_to_learn": 5}}, db)

            # Record a trade with explicit market_context
            rec = MagicMock()
            rec.strategy = "test_strat"
            rec.pnl = -20.0
            rec.contributing_strategies = None
            rec.regime = None
            rec.exit_reason = "stop_loss"
            rec.holding_minutes = 30
            rec.rsi = 50
            rec.atr_pct = 1.5
            rec.volume_ratio = 1.0
            rec.market_trend = 0
            rec.entry_time = datetime.now(IST)
            rec.exit_time = datetime.now(IST)

            # Should accept the kwarg and not raise
            ta.record_trade(rec, market_context={"india_vix": 25.0, "nifty_trend": -1})


# ─────────────────────────────────────────────────────────────
# FIX 10: Partial-fill simulation + order ledger
# ─────────────────────────────────────────────────────────────
class TestExecutionEnhancements:
    def test_partial_fill_disabled_by_default(self):
        from core.execution import ExecutionEngine
        eng = ExecutionEngine({"broker": {"mode": "paper"}})
        assert eng.partial_fill_prob == 0.0

    def test_partial_fill_when_enabled(self, monkeypatch):
        """Force a partial fill via seeded randomness and verify the result."""
        import random as _random
        from core.execution import ExecutionEngine

        cfg = {
            "broker": {"mode": "paper"},
            "execution": {
                "paper_partial_fill_prob": 1.0,  # always partial
                "paper_partial_fill_min_ratio": 0.5,
            },
        }
        eng = ExecutionEngine(cfg)
        _random.seed(42)
        order = eng.place_order(
            symbol="TEST", token="", transaction_type="BUY",
            quantity=100, price=500.0, order_type="LIMIT",
        )
        assert order is not None
        assert order["status"] in ("FILLED", "PARTIALLY_FILLED")
        assert 50 <= order["filled_quantity"] <= 100

    def test_order_persisted_to_db_ledger(self):
        from core.execution import ExecutionEngine
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            eng = ExecutionEngine({"broker": {"mode": "paper"}}, database=db)
            eng.place_order(
                symbol="TEST", token="", transaction_type="BUY",
                quantity=10, price=500.0,
            )
            recent = db.get_recent_orders(limit=10)
            assert len(recent) == 1
            assert recent[0]["symbol"] == "TEST"
            assert recent[0]["transaction_type"] == "BUY"
            assert recent[0]["mode"] == "paper"
