"""Regression tests for the 2026-05-28 audit follow-up Phase-4 fixes.

Each test maps 1:1 (or 1:few) to a finding ID in
``docs/audit_2026-05-28_followup.md``. Naming convention:
``test_<finding>_<one_line_intent>``.

Phase-4 scope (this file): runtime performance.

  * PERF-01:  AngelOneDataSource.get_ltp_batch + DataHandler.get_multiple_ltp
              prefer the batch endpoint over N sequential ltpData calls.
  * PERF-04:  Entry path derives ATR from snap.atr_pct instead of
              re-fetching the same 6h window.
  * PERF-06:  Database.load_trade_patterns + TradeAnalyzer.evaluate_setup
              push (strategy, regime) filter to SQL.
  * PERF-08:  Database.store_candles uses executemany (one round-trip).
  * PERF-09:  TradingAgent reuses self._yahoo_session across refreshes.
  * PERF-10:  Database creates idx_trades_symbol_exit / idx_equity_ts /
              idx_trades_strategy_regime + sets PRAGMA cache_size=-64000.
  * PERF-14:  TradingAgent._run_scan_async exists and the periodic
              rescan call site uses it.
  * PERF-15:  docker-compose.yml caps trader at 1.5 vCPUs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────── PERF-01: batch LTP endpoint ─────────────


def test_perf01_angelone_data_source_exposes_get_ltp_batch():
    from core.data_handler import AngelOneDataSource
    assert hasattr(AngelOneDataSource, "get_ltp_batch"), (
        "PERF-01: AngelOneDataSource must expose get_ltp_batch"
    )


def test_perf01_get_ltp_batch_chunks_at_50_tokens_and_demuxes_response():
    from core.data_handler import AngelOneDataSource
    smart = MagicMock()
    smart.getMarketData = MagicMock(return_value={
        "status": True,
        "data": {"fetched": [
            {"symbolToken": "1001", "ltp": 100.5},
            {"symbolToken": "1002", "ltp": 200.0},
        ]},
    })
    src = AngelOneDataSource(smart, {"exchange": "NSE"})
    instruments = [
        {"symbol": "AAA", "token": "1001"},
        {"symbol": "BBB", "token": "1002"},
        {"symbol": "CCC", "token": ""},  # missing token -> None up-front
    ]
    out = src.get_ltp_batch(instruments)
    assert out["AAA"] == 100.5
    assert out["BBB"] == 200.0
    assert out["CCC"] is None, "missing token must short-circuit to None"
    assert smart.getMarketData.call_count == 1


def test_perf01_get_ltp_batch_chunks_when_above_50():
    from core.data_handler import AngelOneDataSource
    smart = MagicMock()
    # Each chunked call returns one fetched entry per token in the chunk.
    def fake(*args, **kwargs):
        ex_tokens = kwargs.get("exchangeTokens") or args[1]
        toks = list(next(iter(ex_tokens.values())))
        return {
            "status": True,
            "data": {"fetched": [{"symbolToken": t, "ltp": 1.0 + i}
                                  for i, t in enumerate(toks)]},
        }
    smart.getMarketData = MagicMock(side_effect=fake)
    src = AngelOneDataSource(smart, {"exchange": "NSE"})
    instruments = [{"symbol": f"S{i}", "token": str(1000 + i)} for i in range(120)]
    out = src.get_ltp_batch(instruments, chunk_size=50)
    assert len(out) == 120
    # 120 / 50 -> 3 chunks
    assert smart.getMarketData.call_count == 3


def test_perf01_data_handler_get_multiple_ltp_prefers_batch():
    src = (ROOT / "packages" / "core" / "data_handler.py").read_text(encoding="utf-8")
    func = re.search(
        r"def get_multiple_ltp\(self.*?(?=\n    def |\Z)", src, re.DOTALL,
    )
    assert func, "get_multiple_ltp body not found"
    body = func.group(0)
    assert "get_ltp_batch(" in body, (
        "PERF-01: get_multiple_ltp must route through the batch endpoint"
    )


# ───────────── PERF-04: entry-path ATR snapshot reuse ─────────────


def test_perf04_entry_path_derives_atr_from_snapshot():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # Locate the PERF-04 block. The block contains both the snap-derived
    # ATR computation and the legacy _get_latest_atr fallback for the
    # snap-empty path.
    idx = src.find("PERF-04 (audit 2026-05-28)")
    assert idx >= 0, "PERF-04 marker not found in entry path"
    snippet = src[idx: idx + 2000]
    assert 'snap.get("atr_pct")' in snippet, (
        "PERF-04: entry path must derive ATR from snap.atr_pct"
    )
    assert "self._get_latest_atr(symbol)" in snippet, (
        "PERF-04: fallback to _get_latest_atr must remain for snap-empty path"
    )


# ───────────── PERF-06: server-side pattern filter ─────────────


def test_perf06_load_trade_patterns_accepts_strategy_regime_kwargs(tmp_path):
    from core.database import Database
    db = Database(db_path=str(tmp_path / "p.db"))
    rows = db.load_trade_patterns(limit=5, strategy="rsi", regime="bull")
    assert rows == [], "empty DB must return empty list, not raise"


def test_perf06_load_trade_patterns_filters_by_strategy_in_sql(tmp_path):
    from core.database import Database
    db = Database(db_path=str(tmp_path / "p2.db"))
    db.save_trade_pattern({
        "strategy": "rsi", "symbol": "A", "outcome": "GOOD",
        "rsi": 30.0, "atr_pct": 1.0, "volume_ratio": 1.5,
        "hour_of_day": 10, "day_of_week": 1, "market_trend": 1,
        "pnl": 100, "pnl_pct": 1.0, "regime": "bull",
    })
    db.save_trade_pattern({
        "strategy": "supertrend", "symbol": "B", "outcome": "GOOD",
        "rsi": 35.0, "atr_pct": 1.0, "volume_ratio": 1.5,
        "hour_of_day": 11, "day_of_week": 1, "market_trend": 1,
        "pnl": 50, "pnl_pct": 0.5, "regime": "bull",
    })
    rsi_only = db.load_trade_patterns(limit=10, strategy="rsi")
    assert len(rsi_only) == 1
    assert rsi_only[0]["strategy"] == "rsi"


def test_perf06_evaluate_setup_uses_filtered_load(monkeypatch):
    from core.trade_analyzer import TradeAnalyzer
    captured: dict = {}

    class FakeDB:
        def load_trade_patterns(self, *args, **kwargs):
            captured.update(kwargs)
            return []

    ta = TradeAnalyzer(database=FakeDB(), config={
        "learning": {"enabled": True, "pattern_lookback": 200, "min_trades": 1},
    })
    ta.evaluate_setup(
        strategy="rsi_breakout", hour_of_day=10, day_of_week=1,
        rsi=30.0, atr_pct=1.0, volume_ratio=1.5, market_trend=1,
        regime="bull",
    )
    assert captured.get("strategy") == "rsi_breakout"
    assert captured.get("regime") == "bull"


# ───────────── PERF-08: candle store batch ─────────────


def test_perf08_store_candles_uses_executemany():
    src = (ROOT / "packages" / "core" / "database.py").read_text(encoding="utf-8")
    func = re.search(
        r"def store_candles\(self.*?(?=\n    def |\Z)", src, re.DOTALL,
    )
    assert func, "store_candles not found"
    body = func.group(0)
    assert "executemany(" in body, (
        "PERF-08: store_candles must use executemany"
    )


def test_perf08_store_candles_round_trips(tmp_path):
    import pandas as pd
    from core.database import Database
    db = Database(db_path=str(tmp_path / "c.db"))
    df = pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000},
            {"open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 2000},
        ],
        index=pd.to_datetime(["2026-05-28 09:15", "2026-05-28 09:20"]),
    )
    db.store_candles("RELIANCE", "5min", df)
    loaded = db.load_candles("RELIANCE", "5min")
    assert len(loaded) == 2


# ───────────── PERF-09: yahoo session reuse ─────────────


def test_perf09_market_context_refresh_reuses_yahoo_session():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    func = re.search(
        r"def _refresh_market_context\(self.*?(?=\n    def |\Z)", src, re.DOTALL,
    )
    assert func, "_refresh_market_context not found"
    body = func.group(0)
    assert "self._yahoo_session" in body, (
        "PERF-09: _refresh_market_context must stash and reuse a yahoo Session"
    )
    assert "getattr(self, \"_yahoo_session\", None)" in body, (
        "PERF-09: session lookup must use getattr to be idempotent across refreshes"
    )


# ───────────── PERF-10: DB indexes + cache_size ─────────────


def test_perf10_database_creates_index_on_trades_symbol_exit():
    src = (ROOT / "packages" / "core" / "database.py").read_text(encoding="utf-8")
    assert "idx_trades_symbol_exit" in src, "PERF-10: idx_trades_symbol_exit missing"
    assert "idx_equity_ts" in src, "PERF-10: idx_equity_ts missing"
    assert "idx_trades_strategy_regime" in src, "PERF-10: idx_trades_strategy_regime missing"


def test_perf10_database_sets_cache_size_pragma():
    src = (ROOT / "packages" / "core" / "database.py").read_text(encoding="utf-8")
    assert 'PRAGMA cache_size=-64000' in src, (
        "PERF-10: connection helper must set PRAGMA cache_size=-64000"
    )


def test_perf10_indexes_actually_created(tmp_path):
    """Verify the three new indexes exist after schema init."""
    import sqlite3
    from core.database import Database
    db_path = tmp_path / "perf10.db"
    Database(db_path=str(db_path))
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_trades_symbol_exit" in names
    assert "idx_equity_ts" in names
    assert "idx_trades_strategy_regime" in names


# ───────────── PERF-14: background scanner ─────────────


def test_perf14_trading_agent_exposes_run_scan_async():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert "def _run_scan_async(self" in src, (
        "PERF-14: TradingAgent must expose _run_scan_async"
    )


def test_perf14_periodic_rescan_uses_async_path():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # The periodic-rescan call site sits just after the pre-market warm-up
    # block. Match the entire run() loop and ensure the periodic-rescan
    # branch invokes _run_scan_async (not the sync _run_scan).
    run = re.search(
        r"def run\(self, poll_interval.*?\n    def ", src, re.DOTALL,
    )
    assert run, "run() body not located"
    body = run.group(0)
    # Find the "Periodic rescan" comment and inspect the few lines that
    # follow.
    idx = body.find("Periodic rescan to rotate into better stocks")
    assert idx >= 0, "Periodic-rescan comment missing"
    snippet = body[idx: idx + 800]
    assert "self._run_scan_async()" in snippet, (
        "PERF-14: periodic rescan call site must use _run_scan_async"
    )


# ───────────── PERF-15: CPU cap in docker-compose ─────────────


def test_perf15_docker_compose_caps_cpus():
    src = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'cpus: "1.5"' in src, (
        "PERF-15: docker-compose.yml must cap trader at 1.5 vCPUs"
    )
    assert 'cpus: "0.5"' in src, (
        "PERF-15: docker-compose.yml must reserve 0.5 vCPUs for the trader"
    )
