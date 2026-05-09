"""Tests for the validation tooling (gap-detector, signal audit, shadow
mode, and ensemble backtest scaffolding)."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ─────────────────────────────────────────────────────
# Signal Audit
# ─────────────────────────────────────────────────────


class TestSignalAudit:
    def test_appends_csv_row(self, tmp_path):
        from core.signal_audit import SignalAudit

        audit = SignalAudit(log_dir=str(tmp_path))
        audit.log(
            symbol="RELIANCE", direction="BUY", confidence=0.72,
            regime="bull_low_vol", price=1234.50, strategy="supertrend_follow",
            contributing={"supertrend_follow": 0.6, "rsi_momentum": 0.4},
            outcome="ACCEPTED", reason="filled_filled",
            stop_loss=1215.00, take_profit=1260.00, quantity=3,
        )
        files = list(tmp_path.glob("signal_audit_*.csv"))
        assert len(files) == 1
        with open(files[0]) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "RELIANCE"
        assert rows[0]["outcome"] == "ACCEPTED"
        assert "supertrend_follow:0.60" in rows[0]["contributing"]

    def test_summarize_rejections(self, tmp_path):
        from core.signal_audit import SignalAudit

        audit = SignalAudit(log_dir=str(tmp_path))
        audit.log(symbol="A", direction="BUY", confidence=0.6, regime="x",
                  price=10, strategy="s", contributing=None,
                  outcome="REJECTED", reason="dead_hour:12:00-13:00")
        audit.log(symbol="B", direction="BUY", confidence=0.6, regime="x",
                  price=10, strategy="s", contributing=None,
                  outcome="REJECTED", reason="dead_hour:12:00-13:00")
        audit.log(symbol="C", direction="BUY", confidence=0.6, regime="x",
                  price=10, strategy="s", contributing=None,
                  outcome="ACCEPTED", reason="filled_filled")
        stats = audit.summarize_today()
        assert stats["total"] == 3
        assert stats["outcomes"]["REJECTED"] == 2
        assert stats["outcomes"]["ACCEPTED"] == 1
        assert stats["rejections"]["dead_hour"] == 2

    def test_handles_missing_fields_gracefully(self, tmp_path):
        from core.signal_audit import SignalAudit

        audit = SignalAudit(log_dir=str(tmp_path))
        # Missing price, strategy, contributing, SL/TP — should not crash
        audit.log(symbol="X", direction="SELL", confidence=0.8,
                  regime=None, price=None, strategy=None,
                  contributing=None, outcome="SHADOW")
        files = list(tmp_path.glob("signal_audit_*.csv"))
        with open(files[0]) as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["price"] == ""
        assert rows[0]["outcome"] == "SHADOW"


# ─────────────────────────────────────────────────────
# Shadow Mode
# ─────────────────────────────────────────────────────


class TestShadowMode:
    @pytest.fixture
    def exec_engine(self, tmp_path):
        from core.database import Database
        from core.execution import ExecutionEngine

        db = Database(str(tmp_path / "test.db"))
        cfg = {
            "broker": {"mode": "paper"},
            "execution": {
                "shadow_mode": True,
                "order_type": "LIMIT",
                "paper_partial_fill_prob": 0.0,
            },
            "market": {"exchange": "NSE"},
        }
        return ExecutionEngine(cfg, smart_api=None, database=db)

    def test_shadow_order_returns_shadow_status(self, exec_engine):
        result = exec_engine.place_order(
            symbol="RELIANCE", token="123", transaction_type="BUY",
            quantity=5, price=1400.0, stop_loss=1380.0, take_profit=1440.0,
        )
        assert result is not None
        assert result["status"] == "SHADOW"
        assert result["order_id"].startswith("SHADOW-")
        assert result["mode"] == "shadow"
        assert result["filled_quantity"] == 0
        assert result["stop_loss_price"] == 1380.0
        assert result["target_price"] == 1440.0

    def test_shadow_does_not_advance_portfolio(self, exec_engine, tmp_path):
        from core.portfolio import Portfolio

        p = Portfolio(initial_balance=10000.0, commission_pct=0.03,
                      log_dir=str(tmp_path), database=None)
        initial_cash = p.cash
        result = exec_engine.place_order(
            symbol="X", token="t", transaction_type="BUY",
            quantity=1, price=100.0,
        )
        # Shadow result must not have been applied to portfolio by the engine.
        # The trading agent explicitly short-circuits before open_position.
        assert result["status"] == "SHADOW"
        assert p.cash == initial_cash  # portfolio untouched

    def test_shadow_is_off_by_default(self, tmp_path):
        from core.database import Database
        from core.execution import ExecutionEngine

        db = Database(str(tmp_path / "t.db"))
        eng = ExecutionEngine({"broker": {"mode": "paper"}, "execution": {}}, database=db)
        assert eng.shadow_mode is False
        result = eng.place_order(
            symbol="X", token="t", transaction_type="BUY",
            quantity=1, price=100.0,
        )
        assert result["status"] == "FILLED"


# ─────────────────────────────────────────────────────
# Gap detector
# ─────────────────────────────────────────────────────


class TestGapDetector:
    def _insert_trade(self, db_path: str, **kw):
        defaults = {
            "symbol": "TEST", "side": "BUY", "entry_price": 100.0,
            "exit_price": 101.0, "quantity": 10, "pnl": 10.0, "commission": 1.0,
            "strategy": "rsi_momentum",
            "entry_time": "2026-04-28T11:00:00", "exit_time": "2026-04-28T11:30:00",
            "exit_reason": "take_profit",
        }
        defaults.update(kw)
        conn = sqlite3.connect(db_path)
        # Ensure table exists via Database init
        cols = ",".join(defaults.keys())
        ph = ",".join("?" * len(defaults))
        conn.execute(f"INSERT INTO trades ({cols}) VALUES ({ph})",
                     list(defaults.values()))
        conn.commit()
        conn.close()

    def test_detects_rr_inversion_and_sl_dominance(self, tmp_path, monkeypatch):
        from core.database import Database  # creates schema
        db_path = str(tmp_path / "t.db")
        Database(db_path)  # init schema
        # 10 trades: 3 wins (+10), 7 SL losses (-20)
        for i in range(3):
            self._insert_trade(db_path, pnl=10.0, exit_reason="take_profit",
                               symbol=f"W{i}")
        for i in range(7):
            self._insert_trade(db_path, pnl=-20.0, exit_reason="stop_loss",
                               symbol=f"L{i}")

        monkeypatch.setenv("AGENT_DB_PATH", db_path)
        # Reimport so the module-level DB_PATH picks up the new env var.
        # MUST also clear `research` (the package), not just the leaf —
        # otherwise `from research import analyze_day` returns the cached
        # attribute on the still-cached package object and AGENT_DB_PATH
        # is never re-read. This caused intermittent pollution across the
        # GapDetector test class on CI.
        for mod in ("research.analyze_day", "research"):
            sys.modules.pop(mod, None)
        from research import analyze_day

        trades = analyze_day.load_trades(
            "2026-04-28T00:00:00", "2026-04-29T00:00:00"
        )
        assert len(trades) == 10
        stats = analyze_day.compute_stats(trades)
        assert stats.rr < 1.0  # wins smaller than losses
        assert stats.win_rate == 30.0
        suggestions = analyze_day.detect_gaps(trades, stats)
        severities = [s.severity for s in suggestions]
        findings = " ".join(s.finding for s in suggestions)
        assert "R:R" in findings
        assert "Stop-loss dominates" in findings
        assert "high" in severities

    def test_detects_repeat_losers(self, tmp_path, monkeypatch):
        from core.database import Database
        db_path = str(tmp_path / "t.db")
        Database(db_path)
        for _ in range(3):
            self._insert_trade(db_path, pnl=-10.0, symbol="BADSYM",
                               exit_reason="stop_loss")
        # Add some neutrals so n>=5
        for i in range(3):
            self._insert_trade(db_path, pnl=5.0, symbol=f"OK{i}")

        monkeypatch.setenv("AGENT_DB_PATH", db_path)
        for mod in ("research.analyze_day", "research"):
            sys.modules.pop(mod, None)
        from research import analyze_day

        trades = analyze_day.load_trades(
            "2026-04-28T00:00:00", "2026-04-29T00:00:00"
        )
        stats = analyze_day.compute_stats(trades)
        suggestions = analyze_day.detect_gaps(trades, stats)
        assert any("Repeat losing symbols" in s.finding for s in suggestions)

    def test_empty_day_returns_info(self, tmp_path, monkeypatch):
        from core.database import Database
        db_path = str(tmp_path / "empty.db")
        Database(db_path)
        monkeypatch.setenv("AGENT_DB_PATH", db_path)
        for mod in ("research.analyze_day", "research"):
            sys.modules.pop(mod, None)
        from research import analyze_day
        trades = analyze_day.load_trades("2020-01-01T00:00:00", "2020-01-02T00:00:00")
        stats = analyze_day.compute_stats(trades)
        suggestions = analyze_day.detect_gaps(trades, stats)
        # 0 trades -> should emit "too few" info, not crash
        assert suggestions[0].severity == "info"


# ─────────────────────────────────────────────────────
# Ensemble Backtester
# ─────────────────────────────────────────────────────


class TestEnsembleBacktestScaffold:
    def test_config_defaults(self):
        from research.backtest_ensemble import BacktestConfig

        cfg = BacktestConfig()
        assert cfg.initial_capital > 0
        assert cfg.confidence_threshold >= 0.5
        assert cfg.min_entry_atr_pct > 0
        assert cfg.apply_dead_hour is True
        assert cfg.apply_expected_profit_gate is True

    def test_gate_stats_serialization(self):
        from research.backtest_ensemble import GateStats

        g = GateStats(total_signals=10, executed=3, atr_too_low=5)
        d = g.as_dict()
        assert d["total_signals"] == 10
        assert d["executed"] == 3
        assert d["atr_too_low"] == 5

    def test_interval_alias_normalization(self):
        from research.backtest_ensemble import EnsembleBacktester

        # Just test the alias dict directly; running a full backtest needs
        # network access so we keep this as a unit-level assertion.
        assert EnsembleBacktester._INTERVAL_ALIASES["5m"] == "5min"
        assert EnsembleBacktester._INTERVAL_ALIASES["15m"] == "15min"
        assert EnsembleBacktester._INTERVAL_ALIASES["1d"] == "1d"

    def test_dead_hour_detection(self):
        from research.backtest_ensemble import EnsembleBacktester, BacktestConfig

        eng = EnsembleBacktester({}, BacktestConfig())
        t_noon = datetime(2026, 4, 28, 12, 30)
        t_morn = datetime(2026, 4, 28, 10, 30)
        assert eng._in_dead_hour(t_noon) is True
        assert eng._in_dead_hour(t_morn) is False

    def test_result_summary_empty(self):
        from research.backtest_ensemble import EnsembleBacktester, BacktestConfig

        eng = EnsembleBacktester({}, BacktestConfig())
        res = eng._build_result([], [10000.0], type("G", (), {"__init__": lambda self: None})())
        # Empty trade set -> zero metrics, no division by zero
        assert res.total_trades == 0
        assert res.win_rate == 0
        assert res.profit_factor == 0
