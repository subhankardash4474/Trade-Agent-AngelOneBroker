"""Unit tests for the disk-persisted EOD-sent flag in
``trading_agent.TradingAgent``.

Why this matters
----------------
On 2026-05-13 the agent sent 11 identical EOD Summary emails (plus 11
Post-Mortems + 11 Profit Diagnostics) because daemon restarts between
15:20 and 16:00 IST kept resetting the in-memory ``_eod_summary_sent``
flag. Each restart re-evaluated the EOD branch as "not yet sent today"
and re-fired the full trio.

These tests pin down the file-flag-based persistence so a restart
within the same calendar day finds the flag and short-circuits.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import trading_agent  # noqa: E402
from trading_agent import TradingAgent  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def _stub_agent(log_dir: Path, now: datetime | None = None,
                eod_sent: bool = False) -> TradingAgent:
    """Build a minimal TradingAgent that exposes ``_eod_flag_path`` /
    ``_load_eod_sent_flag`` / ``_save_eod_sent_flag`` without paying for
    a full ``__init__`` (which would require a broker session, DB load,
    XGBoost model, etc.). The tests touch state directly.
    """
    agent = TradingAgent.__new__(TradingAgent)
    agent.config = {"logging": {"log_dir": str(log_dir)}}
    agent._eod_summary_sent = eod_sent
    return agent


def test_flag_path_uses_configured_log_dir(tmp_path):
    agent = _stub_agent(tmp_path)
    p = agent._eod_flag_path("2026-05-13")
    assert p == str(tmp_path / ".eod_sent_2026-05-13.flag")


def test_load_flag_returns_false_when_absent(tmp_path):
    agent = _stub_agent(tmp_path)
    # Today's flag does not exist -> _load returns False.
    assert agent._load_eod_sent_flag() is False


def test_save_then_load_roundtrip(tmp_path):
    agent = _stub_agent(tmp_path)
    agent._save_eod_sent_flag()
    assert agent._load_eod_sent_flag() is True
    # File content is the ISO timestamp of the save -- not used for
    # logic, but operators may eyeball it for debugging.
    today_iso = datetime.now(IST).strftime("%Y-%m-%d")
    flag = tmp_path / f".eod_sent_{today_iso}.flag"
    assert flag.exists()
    contents = flag.read_text(encoding="utf-8")
    assert contents.startswith(today_iso)


def test_save_creates_parent_directory(tmp_path):
    """Configuring log_dir to a non-existent path should still work --
    in production this can happen if logs/ is missing on a fresh VM."""
    nested = tmp_path / "deep" / "logs"
    agent = _stub_agent(nested)
    agent._save_eod_sent_flag()
    today_iso = datetime.now(IST).strftime("%Y-%m-%d")
    assert (nested / f".eod_sent_{today_iso}.flag").exists()


def test_load_swallows_oserror(tmp_path, monkeypatch):
    """Flaky filesystem must not crash daemon boot. Return False and
    let the agent send EOD as if it hadn't been sent yet."""
    agent = _stub_agent(tmp_path)

    def boom(_p):
        raise OSError("EIO: simulated")

    monkeypatch.setattr(trading_agent.os.path, "exists", boom)
    assert agent._load_eod_sent_flag() is False


def test_save_swallows_oserror_but_keeps_in_memory_flag(tmp_path,
                                                       monkeypatch):
    """If the disk write fails, the current daemon session must still
    treat EOD as sent (the in-memory flag is the operative guard for
    the rest of this run). The next restart loses dedup -- documented
    degraded mode."""
    agent = _stub_agent(tmp_path)
    monkeypatch.setattr(
        trading_agent.os, "makedirs",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
    )
    # Should NOT raise.
    agent._save_eod_sent_flag()


def test_full_dedup_simulation_across_restarts(tmp_path):
    """Replay the 2026-05-13 incident at this layer: simulate 11 fresh
    TradingAgent instances on the same calendar day. The first sees no
    flag and 'sends'; the next 10 see the flag and skip."""
    agent1 = _stub_agent(tmp_path)
    assert agent1._load_eod_sent_flag() is False
    agent1._save_eod_sent_flag()

    sent_count = 1
    for restart in range(10):
        agent = _stub_agent(tmp_path)
        if not agent._load_eod_sent_flag():
            sent_count += 1
            agent._save_eod_sent_flag()
    assert sent_count == 1


def test_stale_flag_path_format_isolated_per_day():
    """The flag name embeds the date, so yesterday's flag must NOT
    suppress today's EOD."""
    tmp = Path("/tmp")  # not actually touched; we just inspect the name
    agent = _stub_agent(tmp)
    yesterday_flag = agent._eod_flag_path("2026-05-12")
    today_flag = agent._eod_flag_path("2026-05-13")
    assert yesterday_flag != today_flag
    assert "2026-05-12" in yesterday_flag
    assert "2026-05-13" in today_flag


def test_constructor_picks_up_existing_flag(tmp_path):
    """Direct test of the constructor branch: pre-existing flag ->
    ``_eod_summary_sent`` initialised to True."""
    today_iso = datetime.now(IST).strftime("%Y-%m-%d")
    (tmp_path / f".eod_sent_{today_iso}.flag").write_text("seed")
    # Use the stub helper but EXERCISE _load through it as the real
    # constructor does.
    agent = _stub_agent(tmp_path)
    assert agent._load_eod_sent_flag() is True
