"""Regression tests for the 2026-05-26 audit fixes.

Each test maps 1:1 to a finding in `docs/audit_2026-05-25/BUG_REPORT.md`
or the C-series follow-up. Naming convention: `test_<finding>_<one_line_intent>`.

These tests are intentionally small and fast — they are the "no new
issues" guardrail for the operator-driven fix sweep applied on 2026-05-26.
If anyone breaks these, the corresponding audit finding has regressed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────── Phase 1 ───────────────────────────


def test_c3_close_position_imports_cleanly():
    """C-3: tools/close_position.py used to import a non-existent
    `core.config_loader.load_config`. Just importing the module raised
    ImportError, so the manual-flatten tool was unusable in any incident."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tools_close_position", str(ROOT / "tools" / "close_position.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main"), "close_position.py must expose main()"
    assert callable(mod._get_ltp), "close_position.py must expose _get_ltp()"


def test_c4_close_position_reads_real_config_keys():
    """C-4: pre-fix it read `trading.initial_capital` (never exists) and
    `trading.commission_pct`. Real keys are `capital.initial_balance`
    and `execution.product_type`. Only inspect EXECUTABLE statements, not
    the explanatory comments referencing the old wrong keys."""
    src = (ROOT / "tools" / "close_position.py").read_text(encoding="utf-8")
    # Strip comment-only lines so we only assert on code.
    code_lines = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Drop trailing inline comment too
        if "#" in line:
            line = line.split("#", 1)[0]
        code_lines.append(line)
    code = "\n".join(code_lines)
    # Old wrong key REFERENCES must be gone from executable code
    assert "trading.initial_capital" not in code
    assert "trading.commission_pct" not in code
    assert 'config.get("trading", {})' not in code
    # New right keys (these live in code, not comments)
    assert 'config.get("capital")' in code
    assert 'config.get("execution")' in code


def test_c28_close_position_ltp_fetch_has_timeout(monkeypatch):
    """C-28: _get_ltp used to wrap a synchronous yfinance.download with
    NO outer timeout. A stalled Yahoo endpoint would hang the manual
    flatten tool indefinitely. The fix bounds it via a worker thread
    + join(timeout=...)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tools_close_position_ltp", str(ROOT / "tools" / "close_position.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Patch the timeout to something tiny + make yfinance pretend-hang.
    monkeypatch.setattr(mod, "_LTP_FETCH_TIMEOUT_SECONDS", 0.3)

    class _FakeYF:
        @staticmethod
        def download(*a, **k):
            time.sleep(2.0)  # longer than the bounded timeout
            return None

    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF)

    t0 = time.monotonic()
    result = mod._get_ltp("RELIANCE")
    elapsed = time.monotonic() - t0
    assert result is None
    assert elapsed < 1.5, f"_get_ltp should return within ~timeout; got {elapsed:.2f}s"


def test_b17_no_unresolved_position_forward_ref():
    """B-17: trading_agent.py had `pos: "Position"` with no import for
    Position, so ruff/mypy flagged F821 on every run."""
    import subprocess
    res = subprocess.run(
        ["python", "-m", "ruff", "check", "--select", "F821",
         "trading_agent.py"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert res.returncode == 0, f"ruff F821 found unresolved name(s):\n{res.stdout}\n{res.stderr}"


def test_c19_no_dead_compute_round_trip_import():
    """C-19: `backtest_ensemble.py` imported `compute_round_trip` and never
    called it. Verifies the import has been removed."""
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(encoding="utf-8")
    assert "from core.charges import compute_round_trip" not in src


def test_c26_use_live_universe_in_config():
    """C-26: drift between hardcoded fallback (`use_live_universe=False`)
    and runtime behaviour was invisible because the key was absent from
    config.yaml. The fix adds the key with a documenting comment."""
    src = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "use_live_universe" in src


def test_c27_commission_pct_documented_as_dead():
    """C-27: `execution.commission_pct` and `backtest.commission_pct`
    are loaded into Portfolio.commission_pct but charge math uses
    `core.charges.compute_one_leg/compute_round_trip` exclusively. Make
    sure the config.yaml carries the deprecation note so operators
    don't waste time tuning a no-op."""
    src = (ROOT / "config.yaml").read_text(encoding="utf-8")
    # Comment text from the fix
    assert "DEAD KNOB" in src
    assert "core.charges" in src


# ─────────────────────────── Phase 2 ───────────────────────────


def test_c11_orb_flat_range_returns_hold_no_crash():
    """C-11: an opening-range with high == low (halted/circuit-locked
    stock at open) caused a ZeroDivisionError in the confidence math."""
    from strategies.opening_range_breakout import OpeningRangeBreakout
    strat = OpeningRangeBreakout({"range_minutes": 15})
    idx = pd.date_range("2026-05-26 09:15", periods=30, freq="5min")
    df = pd.DataFrame({
        "open": [100.0] * 30,
        "high": [100.0] * 30,
        "low": [100.0] * 30,
        "close": [100.0] * 30,
        "volume": [1000] * 30,
    }, index=idx)
    sig = strat.generate_signal(df, "HALTED")
    # Must not raise, must HOLD with a documented reason.
    from strategies.base_strategy import Signal
    assert sig.signal == Signal.HOLD
    assert sig.metadata.get("reason") == "flat_opening_range"


def test_c12_orb_range_minutes_60_does_not_raise():
    """C-12: range_minutes >= 60 caused
    `dtime(hour, minute + range_minutes)` to raise ValueError."""
    from strategies.opening_range_breakout import _range_end_time
    from datetime import time as dtime
    end = _range_end_time(dtime(9, 15), 60)
    assert end == dtime(10, 15)
    end = _range_end_time(dtime(9, 15), 75)
    assert end == dtime(10, 30)


# ─────────────────────────── Phase 3 ───────────────────────────


def test_c8_lstm_sell_signal_carries_sl_tp():
    """C-8: LSTM SELL branch returned no stop_loss / take_profit, so
    downstream applied generic 1.5%/3% defaults that are NOT symmetric
    with the BUY branch's 1.5x/2.5x ATR math."""
    src = (PACKAGES / "strategies" / "lstm_model.py").read_text(encoding="utf-8")
    # Must reference ATR-based SL/TP in the SELL branch
    assert "stop_loss = price + 1.5 * atr" in src
    assert "take_profit = price - 2.5 * atr" in src


def test_c9_lstm_has_set_market_context():
    """C-9: LSTM strategy lacked the `set_market_context()` hook so the
    feature engine inferred nifty_trend/india_vix from its neutral
    defaults instead of the live regime context that XGBoost gets."""
    from strategies.lstm_model import LSTMPriceModel
    assert hasattr(LSTMPriceModel, "set_market_context")


def test_c10_xgboost_holds_on_nan_features():
    """C-10: pre-fix the strategy did `latest.fillna(0)` and fed zeros
    to the model, producing spurious high-confidence signals on the
    warmup row. The fix returns HOLD with a `nan_features` reason."""
    src = (PACKAGES / "strategies" / "xgboost_classifier.py").read_text(encoding="utf-8")
    assert '"reason": "nan_features"' in src
    # Original silent fillna line should be gone for that path.
    assert "latest = latest.fillna(0)" not in src


def test_c14_trend_context_cache_cap_configurable():
    """C-14: `_cache` in _trend_context.py was unbounded."""
    from strategies import _trend_context as tc
    assert hasattr(tc, "_CACHE_MAX_ENTRIES")
    assert tc._CACHE_MAX_ENTRIES >= 100


def test_c13_trend_filter_fail_closed_env_flag():
    """C-13: introduced `TREND_FILTER_FAIL_CLOSED` env flag."""
    from strategies import _trend_context as tc
    assert hasattr(tc, "_FAIL_CLOSED")


def test_c30_rsi_momentum_matches_feature_engine():
    """C-30: rsi_momentum._compute_rsi now applies the same flat-window
    overrides as FeatureEngine._add_momentum_features."""
    from strategies.rsi_momentum import RSIMomentum
    import pandas as pd

    # All-up window: every diff is positive -> avg_loss==0, avg_gain>0
    s = pd.Series(range(1, 50)).astype(float)
    rsi = RSIMomentum._compute_rsi(s, 14)
    last = rsi.iloc[-1]
    assert last == 100.0, f"all-up window should yield RSI=100, got {last}"

    # Flat window: every diff is zero -> avg_loss==0 and avg_gain==0
    s_flat = pd.Series([100.0] * 50)
    rsi_flat = RSIMomentum._compute_rsi(s_flat, 14)
    last_flat = rsi_flat.iloc[-1]
    assert last_flat == 50.0, f"flat window should yield RSI=50, got {last_flat}"


# ─────────────────────────── Phase 4 ───────────────────────────


def test_b6_holiday_coverage_helper_present():
    """B-6: hardcoded NSE_HOLIDAYS expires 2026-12-25 with no auto-
    extension. `is_known_holiday_year(year)` lets callers fail-loud."""
    from core.data_handler import is_known_holiday_year
    assert is_known_holiday_year(2026) is True
    assert is_known_holiday_year(2027) is False  # not yet curated
    assert is_known_holiday_year(2025) is True


def test_b7_paper_seed_makes_fills_reproducible():
    """B-7: paper-order RNG used module `random` global state, so
    backtests were non-reproducible. With EXECUTION_PAPER_SEED, repeated
    runs must produce identical slippage draws."""
    from core.execution import _set_paper_seed, _paper_rng

    _set_paper_seed(42)
    a = [_paper_rng.uniform(0.0, 1.0) for _ in range(50)]
    _set_paper_seed(42)
    b = [_paper_rng.uniform(0.0, 1.0) for _ in range(50)]
    assert a == b, "Same seed must produce identical slippage trajectories"

    _set_paper_seed(7)
    c = [_paper_rng.uniform(0.0, 1.0) for _ in range(50)]
    assert a != c, "Different seeds must produce different trajectories"


def test_b9_data_handler_cache_is_bounded():
    """B-9: pre-fix DataHandler._cache was an unbounded dict."""
    from core.data_handler import DataHandler
    dh = DataHandler({}, smart_api=None)
    # Cap is conservative; verify private state is wired correctly.
    assert hasattr(dh, "_cache_order")
    assert isinstance(dh._cache_order, list)
    assert hasattr(DataHandler, "_CACHE_MAX_ENTRIES")
    # FIFO behaviour
    dh._CACHE_MAX_ENTRIES = 3
    for i in range(5):
        dh._cache_put(f"key{i}", pd.DataFrame({"x": [i]}))
    assert len(dh._cache) <= 3
    # Oldest two evicted
    assert "key0" not in dh._cache
    assert "key1" not in dh._cache
    assert "key4" in dh._cache


def test_b15_profit_factor_is_json_safe():
    """B-15 / C-17: zero-loss + nonzero-wins must NOT produce
    float('inf') any more — JSON has no representation for infinity."""
    # Inline the fixed branch instead of constructing a TradeAnalyzer
    # (it requires a TradeRecord pipeline).
    src = (PACKAGES / "core" / "trade_analyzer.py").read_text(encoding="utf-8")
    assert "stats[\"profit_factor\"] = 999.99" in src
    # And: never emits inf
    assert "stats[\"profit_factor\"] = round(gw / gl, 3) if gl > 0 else float(\"inf\")" not in src


def test_b16_risk_manager_default_matches_config_yaml():
    """B-16: default in code (True) used to diverge from config.yaml
    (False). The new default is False to match the visible config."""
    from core.risk_manager import RiskManager
    # Empty risk block -> default kicks in. Init signature is
    # (config_dict, initial_balance) -- positional.
    rm = RiskManager({}, 100000)
    assert rm.require_nifty_above_200ema is False


def test_b12_secrets_placeholder_env_does_not_overwrite():
    """B-12: `cp .env.example .env` without filling it in used to
    overwrite a real api_key in config.yaml with the placeholder
    string. The fix treats placeholder-looking env values as 'not set'."""
    from core.secrets import apply_env_to_config
    cfg = {"broker": {"api_key": "real_secret_value"}}
    os.environ["ANGELONE_API_KEY"] = "YOUR_ANGELONE_API_KEY"
    try:
        result = apply_env_to_config(cfg)
        # Placeholder env MUST NOT clobber a real config value
        assert result["broker"]["api_key"] == "real_secret_value"
    finally:
        del os.environ["ANGELONE_API_KEY"]


def test_b10_smtp_login_uses_email_address_only():
    """B-10: pre-fix `server.login(self._email_cfg["sender"], ...)` would
    pass a display-formatted string like 'Trading Agent <a@b>' to
    SMTP login, which most servers reject. The fix uses parseaddr."""
    from email.utils import parseaddr
    sender = 'Trading Agent <agent@example.com>'
    assert parseaddr(sender)[1] == "agent@example.com"
    # Smoke: the fix should be present in source
    src = (PACKAGES / "monitoring" / "alerts.py").read_text(encoding="utf-8")
    assert "parseaddr" in src


def test_b14_portfolio_persist_uses_database_class():
    """B-14: Portfolio._maybe_persist_trade used to open its own raw
    sqlite3 connection (reaching into self._db._db_path). The fix
    routes exclusively through Database.store_trade()."""
    src = (PACKAGES / "core" / "portfolio.py").read_text(encoding="utf-8")
    # Raw sqlite3 import should no longer appear inside _maybe_persist_trade
    persist_block = src.split("def _maybe_persist_trade")[1].split("def _persist_state_after_event")[0]
    assert "import sqlite3" not in persist_block
    assert "sqlite3.connect" not in persist_block


def test_c7_angelone_get_funds_returns_defensive_copy():
    """C-7: pre-fix `get_funds(use_cache=True)` returned the cached dict
    by reference; mutations leaked across calls."""
    src = (PACKAGES / "brokers" / "angelone.py").read_text(encoding="utf-8")
    funds_block = src.split("def get_funds")[1].split("def get_positions")[0]
    assert "return dict(self._cached_funds)" in funds_block
    assert "return dict(funds)" in funds_block


def test_c15_tick_aggregator_has_lock_and_cap_history():
    """C-15: pre-fix the WS thread's process_tick raced with the main
    thread's flush_all / history read. C-15 adds an RLock; B-13 exposes
    `cap_history(max)` to replace direct _history mutation."""
    from core.tick_aggregator import TickAggregator
    agg = TickAggregator(["1min"])
    assert hasattr(agg, "_lock")
    assert hasattr(agg, "cap_history")
    # cap_history is a no-op on empty history
    assert agg.cap_history(100) == 0
    # And it trims correctly
    agg._history["1min"]["FOO"] = list(range(20))
    dropped = agg.cap_history(5)
    assert dropped == 15
    assert len(agg._history["1min"]["FOO"]) == 5
    assert agg._history["1min"]["FOO"] == list(range(15, 20))


def test_c16_self_sufficiency_uses_ist_days():
    """C-16: days_since_deployment used `date.today()` (host-local) so
    UTC cloud VMs reported the wrong day near IST midnight. Inspect
    only EXECUTABLE lines of the function so a docstring referencing
    the old broken line doesn't falsely pass/fail."""
    src = (PACKAGES / "core" / "self_sufficiency.py").read_text(encoding="utf-8")
    sub = src.split("def days_since_deployment")[1].split("def status")[0]
    code_lines = []
    for line in sub.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0]
        code_lines.append(line)
    code = "\n".join(code_lines)
    assert "datetime.now(IST).date()" in code
    assert "date.today()" not in code  # the broken line is gone


def test_c18_historical_cache_atomic_write_uses_replace():
    """C-18: parquet writes are now temp + os.replace, so two concurrent
    battery workers can't leave a half-written file."""
    src = (PACKAGES / "core" / "historical_cache.py").read_text(encoding="utf-8")
    block = src.split('df.to_parquet')[0:2]
    # The block immediately around the write must call os.replace
    assert "os.replace(tmp_path, path)" in src
    assert ".tmp." in src  # tmp suffix marker


def test_c24_analyze_day_default_uses_ist():
    """C-24: `analyze_day.py` default-today now uses IST."""
    src = (PACKAGES / "research" / "analyze_day.py").read_text(encoding="utf-8")
    assert "datetime.now(_IST).date()" in src


# ─────────────────────────── Phase 5 ───────────────────────────


def test_file_lock_serialises_two_threads(tmp_path):
    """B-8 / C-29: cross-process file_lock — single-process two-thread
    smoke check verifying the lock semantics."""
    from core.file_lock import file_lock

    target = tmp_path / "shared.json"
    order = []
    started = threading.Event()
    proceed = threading.Event()

    def first():
        with file_lock(target, timeout=5.0):
            order.append("A_in")
            started.set()
            proceed.wait(2.0)
            time.sleep(0.1)
            order.append("A_out")

    def second():
        started.wait(2.0)
        with file_lock(target, timeout=5.0):
            order.append("B_in")
            order.append("B_out")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start(); t2.start()
    started.wait(2.0)
    time.sleep(0.2)
    proceed.set()
    t1.join(); t2.join()

    # B must enter AFTER A exited (lock is mutually exclusive)
    assert order == ["A_in", "A_out", "B_in", "B_out"], order


def test_file_lock_uses_sibling_lock_file_not_data_file(tmp_path):
    """The lock helper must NOT hold the data file open (would break
    os.replace on Windows). Verify by acquiring the lock and writing
    a fresh file via temp+rename to the same data path."""
    from core.file_lock import file_lock

    target = tmp_path / "data.json"
    target.write_text(json.dumps({"x": 0}))
    with file_lock(target, timeout=2.0):
        tmp = tmp_path / "data.json.tmp"
        tmp.write_text(json.dumps({"x": 1}))
        os.replace(tmp, target)  # MUST NOT raise on Windows

    assert json.loads(target.read_text())["x"] == 1


# ─────────────────────────── Phase 6 ───────────────────────────


def test_c23_train_xgboost_calibration_uses_held_out_eval():
    """C-23: pre-fix the calibrator fit AND eval ran on the same X_test,
    so Brier was in-sample for the calibrator. The fix splits. Inspect
    only executable lines (the comment block describes the pre-fix bug)."""
    src = (PACKAGES / "training" / "train_xgboost.py").read_text(encoding="utf-8")
    code_lines = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0]
        code_lines.append(line)
    code = "\n".join(code_lines)
    # Calibrator must NOT fit on the full X_test any more (executable)
    assert "calibrated.fit(X_test, y_test)" not in code
    assert "X_calib_fit" in code
    assert "X_calib_eval" in code
    assert "brier_score_loss(y_calib_eval" in code


# ─────────────────────────── Phase 7 ───────────────────────────


def test_b3residual_angelone_diagnose_respects_ssl_env(monkeypatch):
    """B-3 / B-18: AngelOne._diagnose's IP-whitelist hint used the
    default SSL context. Now it consults TRADER_DISABLE_SSL_VERIFY so
    behaviour is consistent across modules."""
    src = (PACKAGES / "brokers" / "angelone.py").read_text(encoding="utf-8")
    diag = src.split("def _diagnose")[1].split("# ── Internal helpers")[0]
    assert "TRADER_DISABLE_SSL_VERIFY" in diag
    assert "_create_unverified_context" in diag


def test_b19_lstm_model_load_logs_path_for_audit():
    """B-19: pickle / torch.load is arbitrary-code-execution at load
    time. We don't migrate to weights_only=True yet (model artifact
    incompat), but every load logs an absolute path so the audit trail
    can verify provenance."""
    src = (PACKAGES / "strategies" / "lstm_model.py").read_text(encoding="utf-8")
    assert "[security] Loading LSTM model from trusted path:" in src
    xgb_src = (PACKAGES / "strategies" / "xgboost_classifier.py").read_text(encoding="utf-8")
    assert "[security] Loading XGBoost model from trusted path:" in xgb_src


def test_c25_event_calendar_uses_trading_days():
    """C-25: blackout window now counts trading days, not calendar."""
    from core.event_calendar import _trading_days_between

    # Friday -> Monday across a weekend = 0 trading days strictly between
    fri = date(2026, 5, 22)  # Friday
    mon = date(2026, 5, 26)  # Tuesday (Monday was holiday but pick a clean test pair)
    # Test against the unambiguous case: count strictly between Mon 26 and Wed 28
    mon = date(2026, 5, 26)
    wed = date(2026, 5, 28)
    assert _trading_days_between(mon, wed) == 1  # only Tue between

    # Start >= end is 0
    assert _trading_days_between(wed, mon) == 0


def test_c6_place_order_has_timeout_env_knob():
    """C-6 (timeout half): wall-clock cap on placeOrder."""
    from brokers.angelone import _PLACE_ORDER_TIMEOUT_SEC
    assert _PLACE_ORDER_TIMEOUT_SEC > 0
