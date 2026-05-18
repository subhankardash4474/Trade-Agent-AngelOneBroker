"""Unit tests for the per-strategy audit-checkpoint section.

This is the freeze-v2.1 exit-criterion #4 piece: audit_checkpoint.py
now embeds the same per-strategy verdict block the EOD email derives.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import audit_checkpoint as ac  # noqa: E402


def _seed_db(tmp_path: Path) -> Path:
    """Build a minimal trades table that matches what diagnostic.load_trades
    actually reads. We do NOT need the full schema -- only the columns
    diagnostic.aggregate touches."""
    db = tmp_path / "trading_agent.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT,
                strategy TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                quantity INTEGER,
                pnl REAL,
                commission REAL DEFAULT 0,
                exit_time TEXT,
                exit_reason TEXT,
                regime TEXT,
                hour_of_day INTEGER DEFAULT 10
            );
        """)
        # 4 supertrend trades: 1W 3L (bad)
        # 3 rsi_momentum trades: 2W 1L (good but small sample)
        from datetime import datetime, timedelta
        recent = datetime.now() - timedelta(days=1)
        rows = [
            ("RELIANCE", "supertrend_follow", "BUY", 2500.0, 2480.0, 10, -200.0, 5.0,
             recent.isoformat(), "sl_hit", "bear_high_vol"),
            ("TCS",      "supertrend_follow", "BUY", 3200.0, 3180.0, 5,  -100.0, 5.0,
             recent.isoformat(), "sl_hit", "bear_high_vol"),
            ("INFY",     "supertrend_follow", "SELL", 1500.0, 1520.0, 8, -160.0, 5.0,
             recent.isoformat(), "sl_hit", "bear_high_vol"),
            ("WIPRO",    "supertrend_follow", "BUY", 450.0,  455.0, 20, +100.0, 5.0,
             recent.isoformat(), "signal", "bear_high_vol"),
            ("HDFCBANK", "rsi_momentum", "BUY", 1500.0, 1530.0, 10, +300.0, 5.0,
             recent.isoformat(), "tp_hit", "sideways"),
            ("ICICIBANK","rsi_momentum", "BUY", 950.0,  970.0, 15,  +300.0, 5.0,
             recent.isoformat(), "tp_hit", "sideways"),
            ("AXISBANK", "rsi_momentum", "BUY", 1100.0, 1080.0, 12, -240.0, 5.0,
             recent.isoformat(), "sl_hit", "sideways"),
        ]
        conn.executemany(
            "INSERT INTO trades (symbol,strategy,side,entry_price,exit_price,"
            "quantity,pnl,commission,exit_time,exit_reason,regime) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db


class TestSectionPerStrategy:
    def test_returns_disabled_when_db_missing(self, tmp_path):
        out = ac._section_per_strategy(str(tmp_path / "absent.db"))
        # diagnostic.load_trades returns [] for a missing DB, so we hit
        # the "no closed trades" path -- enabled True, 0 strategies.
        assert out.get("enabled") is True
        assert out.get("n_trades_total") == 0
        assert out.get("strategies") == []

    def test_returns_strategies_with_full_stats(self, tmp_path):
        db = _seed_db(tmp_path)
        out = ac._section_per_strategy(str(db), lookback_days=7)
        assert out["enabled"] is True
        assert out["n_trades_total"] == 7
        names = {s["strategy"] for s in out["strategies"]}
        assert names == {"supertrend_follow", "rsi_momentum"}
        # Stats must include all the fields the markdown renderer expects.
        required_keys = {"strategy", "n", "wr_pct", "pf", "kelly",
                         "expectancy", "net_pnl", "verdict"}
        for s in out["strategies"]:
            assert required_keys.issubset(s.keys()), \
                f"missing keys in {s}"

    def test_strategies_sorted_by_net_pnl_descending(self, tmp_path):
        # rsi_momentum should rank above supertrend_follow (positive
        # net vs deeply negative).
        db = _seed_db(tmp_path)
        out = ac._section_per_strategy(str(db))
        ordered = [s["strategy"] for s in out["strategies"]]
        assert ordered[0] == "rsi_momentum"
        assert ordered[-1] == "supertrend_follow"

    def test_portfolio_block_present_and_keyed(self, tmp_path):
        db = _seed_db(tmp_path)
        out = ac._section_per_strategy(str(db))
        port = out["portfolio"]
        assert {"trades", "wr_pct", "pf", "kelly", "net_pnl"}.issubset(port.keys())
        assert port["trades"] == 7

    def test_section_is_wired_into_safe_calls(self):
        # Structural regression guard. If a refactor drops the section
        # from the safe_calls tuple, the audit checkpoint silently loses
        # the per-strategy block; this test catches that.
        import inspect
        src = inspect.getsource(ac.run_and_save)
        assert '"per_strategy"' in src or "'per_strategy'" in src, \
            "_section_per_strategy is not wired into run_and_save()"

    def test_markdown_includes_per_strategy_block(self, tmp_path):
        # Render a full markdown report and assert the new section is
        # visible. Uses the section's own dict shape so we're isolated
        # from the rest of the renderer's inputs.
        from datetime import datetime
        now = ac.IST.localize(datetime(2026, 5, 18, 14, 0))
        data = {
            "timestamp": now.isoformat(),
            "window": {"since": "13:00:00", "until": "14:00:00", "minutes": 60},
            "health": {"alive": True, "pid": 1, "ram_mb": 100, "threads": 5,
                       "uptime_minutes": 60, "status": "ok"},
            "log_anomalies": {"error_count": 0, "warning_count": 0,
                              "traceback_count": 0},
            "positions": {"round_trip_ok": True, "open_positions": []},
            "trades": {"closed": []},
            "day_pnl": {"realised_inr": 0.0, "closed_trades": 0},
            "signal_pipeline": {},
            "xgb": {},
            "risk_state": {},
            "per_strategy": {
                "enabled": True,
                "lookback_days": 7,
                "n_trades_total": 2,
                "strategies": [
                    {"strategy": "rsi_momentum", "n": 2, "wr_pct": 100.0,
                     "pf": 99.0, "kelly": 0.5, "expectancy": 50.0,
                     "net_pnl": 100.0, "verdict": "SCALE"},
                ],
                "portfolio": {
                    "trades": 2, "wr_pct": 100.0, "pf": 99.0,
                    "kelly": 0.5, "net_pnl": 100.0,
                },
            },
            "self_sufficiency": {"enabled": False},
        }
        md = ac._render_markdown(now, data, delta=None)
        assert "## Per-strategy (last 7d)" in md
        assert "rsi_momentum" in md
        assert "SCALE" in md
        assert "Portfolio:" in md
