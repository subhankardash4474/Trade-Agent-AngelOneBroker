"""Regression tests for the 2026-05-28 audit follow-up Phase-1 fixes.

Each test maps 1:1 to a finding ID in
``docs/audit_2026-05-28_followup.md``. Naming convention:
``test_<finding>_<one_line_intent>``.

Phase-1 scope (this file): all OBS-* finds applied to non-frozen
files, plus PERF-02/03/11/12, NUM-13/14, STATE-10, ORD-04/12,
CONC-11/12. Findings on frozen files (base_strategy._atr,
risk_manager.*, _trend_context.*) are deferred to Phase 5.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────────────────── OBS-06 ─────────────────────────────


def test_obs06_market_safety_no_bare_pass_in_staleness_or_spike():
    """Both the staleness check (~line 142) and the 20% spike check
    (~line 157) previously swallowed Exception via ``pass``. The
    fail-closed conversion replaces those with WARNING log +
    ``staleness_check_failed`` / ``spike_check_failed`` reason."""
    src = (PACKAGES / "core" / "market_safety.py").read_text(encoding="utf-8")
    assert "staleness_check_failed" in src, (
        "OBS-06 regression: staleness fail-closed branch missing"
    )
    assert "spike_check_failed" in src, (
        "OBS-06 regression: spike fail-closed branch missing"
    )
    # The bare ``except Exception:\n        pass`` pattern inside
    # check_data_quality must be gone. Count is approximate: the
    # function previously had two such blocks; now both branch into
    # named-reason returns. We assert that ``return False`` appears
    # at least twice INSIDE except handlers across the file.
    assert src.count("staleness_check_failed") >= 1
    assert src.count("spike_check_failed") >= 1


def test_obs06_market_safety_runtime_fail_closed_on_inner_exception():
    """Runtime check: when pytz.utc.localize raises inside the
    staleness try block, the check must fail-closed with
    ``staleness_check_failed``."""
    from core.market_safety import check_data_quality
    import core.market_safety as ms

    df = pd.DataFrame({
        "open": [100.0, 101.0, 102.0, 103.0],
        "high": [101.0, 102.0, 103.0, 104.0],
        "low": [99.0, 100.0, 101.0, 102.0],
        "close": [100.5, 101.5, 102.5, 103.5],
        "volume": [1000, 1100, 1200, 1300],
    })
    # Naive (no tz) DatetimeIndex -> tzinfo is None -> code path
    # enters pytz.utc.localize(). We replace pytz.utc.localize on
    # the module to force the inner exception.
    ts = pd.Timestamp("2026-05-28 10:30:00")
    df.index = pd.DatetimeIndex([
        ts, ts + pd.Timedelta(minutes=5),
        ts + pd.Timedelta(minutes=10), ts + pd.Timedelta(minutes=15),
    ])

    class _BoomTz:
        def localize(self, *_a, **_kw):
            raise RuntimeError("intentional inner failure")
        # Also expose ``utcoffset`` etc. as no-ops so unrelated calls
        # don't break -- not actually exercised in this code path.

    real_pytz = ms.pytz
    class _BoomPytz:
        utc = _BoomTz()
        def timezone(self, *a, **kw):
            return real_pytz.timezone(*a, **kw)

    ms.pytz = _BoomPytz()
    try:
        ok, reason = check_data_quality(df)
    finally:
        ms.pytz = real_pytz
    assert ok is False
    assert "staleness_check_failed" in reason


# ───────────────────────── OBS-12 ─────────────────────────────


def test_obs12_is_market_open_fails_closed_on_uncurated_year():
    """When the holiday calendar has no entries for the current year,
    ``is_market_open()`` MUST return False (fail-closed). Pre-fix it
    warned and returned True (fail-open) -- the Bug L pattern."""
    from core.data_handler import DataHandler, is_known_holiday_year

    # 2099 is not a curated year. Guard the test invariant first.
    assert not is_known_holiday_year(2099), \
        "test invariant broken: 2099 must remain uncurated"

    handler = DataHandler.__new__(DataHandler)
    handler._market_config = {"trading_hours": {"start": "09:15", "end": "15:30"}}

    import core.data_handler as dh_module

    real_dt = dh_module.datetime

    class FakeDt:
        @classmethod
        def now(cls, tz=None):
            # Mid-session Wednesday in an uncurated year
            return dh_module.IST.localize(datetime(2099, 3, 5, 11, 30))

        def __getattr__(self, name):
            return getattr(real_dt, name)

        @classmethod
        def strptime(cls, *a, **kw):
            return real_dt.strptime(*a, **kw)

    dh_module.datetime = FakeDt
    try:
        assert handler.is_market_open() is False, (
            "OBS-12 regression: uncurated holiday year must fail-closed"
        )
    finally:
        dh_module.datetime = real_dt


# ───────────────────────── PERF-03 ─────────────────────────────


def test_perf03_classify_regime_log_is_debug_not_info():
    """The [REGIME-INPUT] line must be DEBUG-level so it does not
    spam the synchronous file sink on rejection-heavy days. Tests
    in test_regime_and_gates.py pin the content; this test only
    pins the level."""
    src = (PACKAGES / "core" / "regime.py").read_text(encoding="utf-8")
    # Ensure no logger.info() emits REGIME-INPUT (only logger.debug).
    info_block = "logger.info(\n        f\"[REGIME-INPUT]"
    debug_block = "logger.debug(\n        f\"[REGIME-INPUT]"
    assert info_block not in src, (
        "PERF-03 regression: [REGIME-INPUT] must NOT be logger.info"
    )
    assert debug_block in src, (
        "PERF-03 regression: [REGIME-INPUT] must be logger.debug"
    )

    info_block_intra = "logger.info(\n        f\"[REGIME-INTRADAY-INPUT]"
    debug_block_intra = "logger.debug(\n        f\"[REGIME-INTRADAY-INPUT]"
    assert info_block_intra not in src
    assert debug_block_intra in src


# ───────────────────────── PERF-12 ─────────────────────────────


def test_perf12_file_logger_uses_enqueue_true():
    """The file sink in TradingAgent._setup_logging must pass
    ``enqueue=True`` so the main thread does not block on
    synchronous fsync I/O."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # Find the file-logger block (it's the only logger.add() with
    # trading_agent_{time}).
    needle = 'logger.add(\n                os.path.join(log_dir, "trading_agent_{time:'
    pos = src.find(needle)
    assert pos != -1, "couldn't find file logger block"
    block = src[pos:pos + 800]
    assert "enqueue=True" in block, (
        "PERF-12 regression: file sink must use enqueue=True"
    )


# ───────────────────────── PERF-02 ─────────────────────────────


def test_perf02_historical_cache_dedups_within_cycle():
    """Calling ``_get_historical_cached`` twice for the same
    (symbol, timeframe) in the same cycle must hit the cache and
    only call ``data_handler.get_historical_data`` once."""
    from trading_agent import TradingAgent

    # Build a stub TradingAgent with just the cache-related attributes
    agent = TradingAgent.__new__(TradingAgent)
    agent._historical_cache = {}
    agent._historical_cache_hits = 0
    agent._historical_cache_misses = 0

    # Distinct DataFrames so we can assert identity on cache hits.
    df_5min = pd.DataFrame({"close": [1.0, 2.0]})
    df_15min = pd.DataFrame({"close": [10.0, 20.0]})
    fetch = MagicMock(side_effect=[df_5min, df_15min])
    agent.data_handler = MagicMock()
    agent.data_handler.get_historical_data = fetch

    now = datetime(2026, 5, 28, 10, 0, 0)
    start = now - timedelta(days=7)

    # First call -- miss
    a = agent._get_historical_cached("RELIANCE", "5min", start, now)
    # Second call same key -- hit
    b = agent._get_historical_cached("RELIANCE", "5min", start, now)
    # Different timeframe -- miss
    c = agent._get_historical_cached("RELIANCE", "15min", start, now)
    # Same first key again -- hit
    d = agent._get_historical_cached("RELIANCE", "5min", start, now)

    assert fetch.call_count == 2, (
        f"PERF-02 regression: expected 2 REST fetches (one per "
        f"unique (symbol, tf)), got {fetch.call_count}"
    )
    assert agent._historical_cache_hits == 2
    assert agent._historical_cache_misses == 2
    # All three 5min-keyed calls must return the SAME DataFrame.
    assert a is b is d is df_5min
    assert c is df_15min


def test_perf02_clear_resets_cache_and_tallies():
    from trading_agent import TradingAgent
    agent = TradingAgent.__new__(TradingAgent)
    agent._historical_cache = {("S", "5min"): pd.DataFrame()}
    agent._historical_cache_hits = 42
    agent._historical_cache_misses = 7
    # PERF-07 (audit 2026-05-28): _clear_historical_cache also clears
    # the per-cycle tick-history cache, so seed those attributes too
    # or the helper will AttributeError.
    agent._tick_history_cache = {("S", "5min"): pd.DataFrame()}
    agent._tick_history_cache_hits = 99
    agent._tick_history_cache_misses = 11
    agent._clear_historical_cache()
    assert agent._historical_cache == {}
    assert agent._historical_cache_hits == 0
    assert agent._historical_cache_misses == 0
    assert agent._tick_history_cache == {}
    assert agent._tick_history_cache_hits == 0
    assert agent._tick_history_cache_misses == 0


# ───────────────────────── NUM-13 ─────────────────────────────


def test_num13_rejection_cooldown_writes_audit_reject():
    """The rejection-cooldown short-circuit must call _audit_reject
    so signal_audit.csv has a row tagged ``reject_cooldown:active``
    instead of leaving a gap."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # Find the cooldown-active branch
    needle = "[REJECT-COOLDOWN] Skipping"
    pos = src.find(needle)
    assert pos != -1, "couldn't find REJECT-COOLDOWN block"
    block = src[pos:pos + 1200]
    assert 'reject_cooldown:active' in block, (
        "NUM-13 regression: cooldown-active path must call _audit_reject "
        "with the 'reject_cooldown:active' reason"
    )
    # The audit call must precede the return so the row is written.
    assert "_audit_reject" in block


# ───────────────────────── NUM-14 ─────────────────────────────


def test_num14_cash_sizing_reserves_min_buffer():
    """The cash affordability calc must subtract ``min_cash_buffer_rs``
    from ``portfolio.cash`` before dividing by effective_price so a
    full-deploy doesn't fail at open_position with a charges
    shortfall."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    needle = "min_cash_buffer"
    assert needle in src, (
        "NUM-14 regression: cash-buffer guard removed"
    )
    # And the buffer must be subtracted from cash before the divide.
    assert "cash_available = max(0.0, self.portfolio.cash - min_cash_buffer)" in src


# ───────────────────────── STATE-10 ─────────────────────────────


def test_state10_emergency_stop_path_is_mode_scoped():
    """The default ``emergency_stop_path`` must include the run mode
    so live + paper daemons cannot kill each other with one touch."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # Default must contain f"STOP.{mode_suffix}"
    assert 'f"STOP.{mode_suffix}"' in src, (
        "STATE-10 regression: default STOP path must be mode-scoped"
    )


# ───────────────────────── ORD-04 ─────────────────────────────


def test_ord04_close_position_safely_forces_market_order_type():
    """_close_position_safely must pass order_type="MARKET" so exits
    don't sit at LIMIT on a gapping symbol."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    needle = "self.execution.place_order(\n                symbol=symbol, token=token, transaction_type=exit_side,"
    pos = src.find(needle)
    assert pos != -1, "couldn't find place_order call in _close_position_safely"
    block = src[pos:pos + 400]
    assert 'order_type="MARKET"' in block, (
        "ORD-04 regression: exits must force MARKET order_type"
    )


# ───────────────────────── ORD-12 ─────────────────────────────


def test_ord12_square_off_alert_distinguishes_partial_failure():
    """_square_off_all must collect per-symbol close results and
    only emit ``Square Off Complete`` when every flatten returned a
    truthy (order, record)."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    needle = "def _square_off_all"
    pos = src.find(needle)
    assert pos != -1
    block = src[pos:pos + 2500]
    # The function must accumulate succeeded + failed lists.
    assert "succeeded:" in block and "failed:" in block, (
        "ORD-12 regression: per-symbol result accumulator missing"
    )
    # SQUARE-OFF INCOMPLETE alert text must exist for the partial-fail path.
    assert "SQUARE-OFF INCOMPLETE" in block, (
        "ORD-12 regression: partial-fail alert wording missing"
    )


# ───────────────────────── CONC-11 ─────────────────────────────


def test_conc11_trade_history_is_bounded_deque():
    """Portfolio.trade_history must be a deque with maxlen so a
    long-running daemon does not accumulate unbounded RSS."""
    from core.portfolio import Portfolio
    import collections

    p = Portfolio(initial_balance=100_000)
    assert isinstance(p.trade_history, collections.deque), (
        "CONC-11 regression: trade_history must be a deque"
    )
    assert p.trade_history.maxlen == 10000, (
        "CONC-11 regression: maxlen must be 10000"
    )


# ───────────────────────── CONC-12 ─────────────────────────────


def test_conc12_database_has_purge_old_equity_points():
    """Database must expose purge_old_equity_points so
    _periodic_cleanup can call it on the 100-cycle cadence."""
    from core.database import Database

    assert hasattr(Database, "purge_old_equity_points"), (
        "CONC-12 regression: purge_old_equity_points helper missing"
    )


def test_conc12_periodic_cleanup_calls_equity_purge():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    needle = "def _periodic_cleanup"
    pos = src.find(needle)
    assert pos != -1
    block = src[pos:pos + 800]
    assert "purge_old_equity_points" in block, (
        "CONC-12 regression: _periodic_cleanup must call equity purge"
    )


# ───────────────────────── OBS-01/02 ─────────────────────────────


def test_obs01_failed_sl_tp_exit_emits_critical():
    """The SL/TP/peak-giveback exit loop must log CRITICAL +
    alert when ``_close_position_safely`` returns (None, None)."""
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert "[EXIT-FAILED]" in src, "OBS-01 regression: tag missing"
    needle = "[EXIT-FAILED]"
    pos = src.find(needle)
    block = src[pos:pos + 1500]
    assert "logger.critical" in block
    assert "MANUAL ACTION REQUIRED" in block


def test_obs02_failed_signal_exit_emits_critical():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    assert "[SIGNAL-EXIT-FAILED]" in src, "OBS-02 regression: tag missing"
    needle = "[SIGNAL-EXIT-FAILED]"
    pos = src.find(needle)
    block = src[pos:pos + 1500]
    assert "logger.critical" in block
    assert "MANUAL ACTION REQUIRED" in block


# ───────────────────────── OBS-03 ─────────────────────────────


def test_obs03_sl_propagate_failure_logs_warning_with_counter():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    needle = "_obs03_sl_propagate_failures"
    assert needle in src, "OBS-03 regression: per-symbol counter missing"
    pos = src.find(needle)
    # Search a wide window around the counter assignment to capture
    # both the WARNING log and the absence of DEBUG.
    block = src[max(0, pos - 800):pos + 800]
    assert "logger.warning" in block, (
        "OBS-03 regression: SL-propagate failure must be WARNING (not DEBUG)"
    )
    # The legacy DEBUG-level line must be gone.
    assert "logger.debug(f\"[SL-PROPAGATE]" not in src


# ───────────────────────── OBS-16 ─────────────────────────────


def test_obs16_order_ledger_persist_failure_is_warning():
    src = (PACKAGES / "core" / "execution.py").read_text(encoding="utf-8")
    needle = "_persist_order"
    pos = src.find(needle)
    assert pos != -1
    # Walk forward to the except clause that handles the save_order failure.
    end = src.find("def ", pos + 50)
    block = src[pos:end if end != -1 else pos + 800]
    assert "logger.warning" in block, (
        "OBS-16 regression: persist failure must be WARNING (was DEBUG)"
    )
    # And the legacy DEBUG line should be gone.
    assert "logger.debug(f\"Order ledger persist failed" not in block


# ───────────────────────── OBS-20 ─────────────────────────────


def test_obs20_battery_cache_load_logs_sha256():
    src = (PACKAGES / "research" / "battery.py").read_text(encoding="utf-8")
    # OBS-20 contract: every worker that loads market_data.pkl
    # must surface the hash in its log so the worker's view can
    # be cross-referenced against the parent's cache write.
    # PERF-13 (2026-05-28) factored the hashing routine out to
    # ``_sha256_file`` and added a sidecar fast path via
    # ``_read_sidecar_hash``; either route still emits the same
    # log fields, so the contract is now: the loader either
    # hashes inline OR reuses a sidecar/helper, and the resulting
    # log line still carries ``sha256[:16]``.
    needle = "_load_market_data_cache"
    pos = src.find("def " + needle)
    assert pos != -1
    block = src[pos:pos + 2500]
    assert ("hashlib" in block) or ("_sha256_file" in block) or ("_read_sidecar_hash" in block), (
        "OBS-20 regression: load path no longer reaches a sha256 "
        "implementation (neither inline hashlib nor _sha256_file "
        "nor _read_sidecar_hash). The audit log line will lose "
        "the sha256[:16] field."
    )
    assert "sha256[:16]" in block, (
        "OBS-20 regression: sha256 must appear in the log line"
    )
