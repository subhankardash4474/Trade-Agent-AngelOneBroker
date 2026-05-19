"""Tests for the battery worker quiet-logger filter.

The 2026-05-19 perf patch wraps both the per-worker log sink and the
parent run log with `_battery_log_filter` so that per-bar signal
chatter from `strategies.*` and `core.portfolio` no longer balloons
the disk I/O on a 2-vCPU backtester VM.

The contract these tests pin:

  1. Records from noisy modules at INFO-level are dropped by default.
  2. Records from noisy modules at WARNING+ are kept by default.
  3. Records from non-noisy modules at any level are kept by default.
  4. Setting `BATTERY_VERBOSE=1` (and several other truthy aliases)
     disables the filter entirely -- everything passes through.
  5. The filter is a pure function of the record dict -- it doesn't
     touch global state, mutate the record, or require a specific
     loguru handler to be attached. (Important: workers spawn fresh
     subprocesses; we must be able to call the filter from any
     subprocess context.)

If a future refactor changes the filter prefixes or removes
`BATTERY_VERBOSE` support, these tests will fail loud.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from research import battery  # noqa: E402


# Loguru level numbers we use in the test records. The numbers come
# from loguru's default level table (DEBUG=10, INFO=20, SUCCESS=25,
# WARNING=30, ERROR=40, CRITICAL=50). We hard-code them here so the
# test doesn't import loguru just to read them back.
DEBUG_NO   = 10
INFO_NO    = 20
WARNING_NO = 30
ERROR_NO   = 40


def _make_record(name: str, level_no: int) -> dict:
    """Build a minimal loguru-shaped record dict.

    Loguru passes filters a real record object whose `level` exposes
    `.no`, but the filter only reads `.get("name")` and
    `record["level"].no`, so a small SimpleNamespace mock is enough.
    """
    return {
        "name": name,
        "level": SimpleNamespace(no=level_no, name="X"),
        "message": "x",
    }


# ─────────────────────── default mode (filter ON) ───────────────────────

class TestDefaultMode:
    """Without BATTERY_VERBOSE: noisy modules are quieted at INFO."""

    @pytest.fixture(autouse=True)
    def _no_verbose(self, monkeypatch):
        monkeypatch.delenv("BATTERY_VERBOSE", raising=False)

    @pytest.mark.parametrize("name", [
        "strategies.vwap_bounce",
        "strategies.rsi_momentum",
        "strategies.supertrend_follow",
        "strategies.ensemble",
        "strategies.opening_range_breakout",
        "strategies.mean_reversion",
        "strategies.xgboost_classifier",
        "core.portfolio",
    ])
    def test_noisy_module_info_dropped(self, name):
        rec = _make_record(name, INFO_NO)
        assert battery._battery_log_filter(rec) is False

    @pytest.mark.parametrize("name", [
        "strategies.vwap_bounce",
        "core.portfolio",
    ])
    def test_noisy_module_debug_dropped(self, name):
        rec = _make_record(name, DEBUG_NO)
        assert battery._battery_log_filter(rec) is False

    @pytest.mark.parametrize("name", [
        "strategies.vwap_bounce",
        "core.portfolio",
    ])
    def test_noisy_module_warning_kept(self, name):
        rec = _make_record(name, WARNING_NO)
        assert battery._battery_log_filter(rec) is True

    @pytest.mark.parametrize("name", [
        "strategies.vwap_bounce",
        "core.portfolio",
    ])
    def test_noisy_module_error_kept(self, name):
        rec = _make_record(name, ERROR_NO)
        assert battery._battery_log_filter(rec) is True

    @pytest.mark.parametrize("name", [
        "__main__",
        "research.battery",
        "research.backtest_ensemble",
        "core.data_handler",
        "core.features",
        "tools.run_battery",
        "",         # missing/empty name
        None,       # explicitly null name
    ])
    def test_non_noisy_module_info_kept(self, name):
        rec = _make_record(name, INFO_NO)
        assert battery._battery_log_filter(rec) is True

    def test_non_noisy_module_debug_kept(self):
        # WORKER spawn / harness debug should still survive the filter
        # because the perf-cost is only on per-bar emitters.
        rec = _make_record("research.battery", DEBUG_NO)
        assert battery._battery_log_filter(rec) is True

    def test_does_not_mutate_record(self):
        """Filter is a pure function; calling it twice yields the same
        verdict and doesn't perturb the record dict."""
        rec = _make_record("strategies.vwap_bounce", INFO_NO)
        before = dict(rec)
        battery._battery_log_filter(rec)
        battery._battery_log_filter(rec)
        # Mock `level` is a SimpleNamespace, so dict equality works for
        # the rest; we just check name + message + level.no didn't shift.
        assert rec["name"] == before["name"]
        assert rec["message"] == before["message"]
        assert rec["level"].no == before["level"].no


# ─────────────────────── verbose mode (filter OFF) ───────────────────────

class TestVerboseMode:
    """With BATTERY_VERBOSE=1 (any truthy alias) the filter is bypassed."""

    @pytest.mark.parametrize("verbose_value", [
        "1", "true", "True", "TRUE", "yes", "Yes", "on",
    ])
    def test_truthy_aliases_bypass(self, monkeypatch, verbose_value):
        monkeypatch.setenv("BATTERY_VERBOSE", verbose_value)
        rec = _make_record("strategies.vwap_bounce", INFO_NO)
        assert battery._battery_log_filter(rec) is True
        assert battery._battery_verbose_enabled() is True

    @pytest.mark.parametrize("non_value", [
        "0", "false", "False", "no", "off", "", "  ",
    ])
    def test_falsy_or_empty_does_not_bypass(self, monkeypatch, non_value):
        monkeypatch.setenv("BATTERY_VERBOSE", non_value)
        rec = _make_record("strategies.vwap_bounce", INFO_NO)
        assert battery._battery_log_filter(rec) is False
        assert battery._battery_verbose_enabled() is False

    def test_unset_does_not_bypass(self, monkeypatch):
        monkeypatch.delenv("BATTERY_VERBOSE", raising=False)
        rec = _make_record("strategies.vwap_bounce", INFO_NO)
        assert battery._battery_log_filter(rec) is False
        assert battery._battery_verbose_enabled() is False

    def test_verbose_keeps_warning_too(self, monkeypatch):
        # Sanity: verbose mode shouldn't change WARNING behaviour
        # (which is "kept" either way) -- just confirms we don't
        # accidentally invert.
        monkeypatch.setenv("BATTERY_VERBOSE", "1")
        rec = _make_record("strategies.vwap_bounce", WARNING_NO)
        assert battery._battery_log_filter(rec) is True


# ─────────────────────── structural guards ───────────────────────

class TestStructuralGuards:
    """Pin the constants so a future refactor can't silently shrink the
    quiet-list (= bring the log-spam back)."""

    def test_quiet_prefixes_include_all_strategy_emitters(self):
        # Every strategy module that can hit per-bar INFO must be
        # covered by some prefix in _BATTERY_QUIET_PREFIXES.
        strategy_modules = [
            "strategies.mean_reversion",
            "strategies.xgboost_classifier",
            "strategies.supertrend_follow",
            "strategies.rsi_momentum",
            "strategies.vwap_bounce",
            "strategies.opening_range_breakout",
            "strategies.ensemble",
        ]
        for mod in strategy_modules:
            assert any(
                mod.startswith(p) for p in battery._BATTERY_QUIET_PREFIXES
            ), f"{mod!r} not covered by any prefix in _BATTERY_QUIET_PREFIXES"

    def test_quiet_prefixes_include_portfolio(self):
        # core.portfolio.close_position emits one INFO per fill; that's
        # easily 1k+ lines per variant on a 220-symbol run.
        assert any(
            "core.portfolio".startswith(p)
            for p in battery._BATTERY_QUIET_PREFIXES
        )

    def test_filter_signature_contract(self):
        # Loguru's filter contract: callable(record) -> bool. Anything
        # else and the sink will raise at attach-time inside the
        # worker, blowing up the whole battery silently (worker logs
        # are the only progress signal mid-run).
        assert callable(battery._battery_log_filter)
        rec = _make_record("strategies.vwap_bounce", INFO_NO)
        result = battery._battery_log_filter(rec)
        assert isinstance(result, bool)

    def test_verbose_helper_signature(self):
        assert callable(battery._battery_verbose_enabled)
        result = battery._battery_verbose_enabled()
        assert isinstance(result, bool)
