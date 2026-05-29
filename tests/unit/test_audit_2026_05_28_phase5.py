"""Regression tests for the 2026-05-28 audit follow-up Phase-5 fixes.

Each test maps 1:1 (or 1:few) to a finding ID in
``docs/audits/audit_2026-05-28_followup.md``. Naming convention:
``test_<finding>_<one_line_intent>``.

Phase-5 scope (this file): semantic correctness + frozen-file fixes.

  * NUM-02:   Kelly post-sizing 1-share regression of F-34.
  * NUM-03:   Live MTM equity sync via RiskManager.sync_balance_from_mtm.
  * NUM-04:   round_to_tick helper + SL/TP routing through it.
  * NUM-08:   compute_round_trip side-aware buy/sell leg mapping.
  * NUM-09:   classify_regime treats NaN VIX as 'unknown'.
  * NUM-12:   regime_size_multiplier(unknown) returns 0.5 (conservative).
  * NUM-05/15: _trend_context._fetch_daily drops the forming-today bar.
  * OBS-04:   is_trade_worth_taking fail-closes on charges-compute error.
  * OBS-10:   base_strategy._atr logs WARNING on exception/NaN.
  * OBS-19:   Same as NUM-12 (unknown regime -> conservative multiplier).
  * CONC-01:  TradingAgent calls update_open_positions immediately
              after a successful entry.
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


# ───────────── NUM-02: Kelly post-sizing 1-share regression ─────────────


def test_num02_kelly_does_not_force_one_share_when_prequantity_is_zero():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # Locate the Kelly multiplier block in _process_signal.
    idx = src.find("Kelly-lite sizing multiplier")
    assert idx >= 0, "Kelly block not found in entry path"
    snippet = src[idx: idx + 2500]
    assert "if quantity > 0:" in snippet, (
        "NUM-02: Kelly must only apply when risk-sized quantity > 0"
    )
    # The literal ``max(1, int(round(quantity * kelly_mult)))`` may
    # still appear inside the explanatory comment block; assert the
    # CODE form (not under indentation followed by ``#``) is gone.
    code_lines = [
        line for line in snippet.splitlines()
        if "max(1, int(round(quantity * kelly_mult)))" in line
        and not line.lstrip().startswith("#")
    ]
    assert not code_lines, (
        f"NUM-02: pre-fix max(1,...) regression still present in code: {code_lines}"
    )
    assert 'sizing:zero_qty' in snippet, (
        "NUM-02: post-Kelly zero must audit-reject with reason sizing:zero_qty"
    )


# ───────────── NUM-03: live MTM equity sync ─────────────


def test_num03_risk_manager_exposes_sync_balance_from_mtm():
    from core.risk_manager import RiskManager
    assert hasattr(RiskManager, "sync_balance_from_mtm"), (
        "NUM-03: RiskManager must expose sync_balance_from_mtm"
    )


def _make_rm(**overrides):
    """RiskManager builder for tests; threads through the required
    initial_balance positional. The constructor reads risk knobs from
    ``config["risk"]``, so any override dict is wrapped accordingly."""
    from core.risk_manager import RiskManager
    risk = {"max_open_positions": 3, "max_risk_per_trade_pct": 1.0}
    risk.update(overrides.pop("config", {}) or {})
    return RiskManager(
        config={"risk": risk},
        initial_balance=overrides.get("balance", 100000.0),
    )


def test_num03_sync_balance_from_mtm_updates_state(tmp_path):
    rm = _make_rm()
    rm.state.peak_balance = 100000.0
    rm.state.current_balance = 100000.0
    rm.sync_balance_from_mtm(95000.0)
    assert rm.state.current_balance == 95000.0
    rm.sync_balance_from_mtm(110000.0)
    assert rm.state.current_balance == 110000.0
    assert rm.state.peak_balance == 110000.0


def test_num03_sync_balance_rejects_invalid_inputs():
    rm = _make_rm()
    rm.state.current_balance = 100000.0
    rm.sync_balance_from_mtm(float("nan"))
    assert rm.state.current_balance == 100000.0
    rm.sync_balance_from_mtm(-50.0)
    assert rm.state.current_balance == 100000.0
    rm.sync_balance_from_mtm("oops")
    assert rm.state.current_balance == 100000.0


def test_num03_trading_cycle_calls_sync_before_can_trade():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    cycle = re.search(
        r"def _trading_cycle\(self.*?(?=\n    def |\Z)", src, re.DOTALL,
    )
    assert cycle, "_trading_cycle body not found"
    body = cycle.group(0)
    sync_at = body.find("sync_balance_from_mtm(")
    can_trade_at = body.find("self.risk_manager.can_trade(")
    assert sync_at >= 0, "NUM-03: _trading_cycle must call sync_balance_from_mtm"
    assert can_trade_at >= 0
    assert sync_at < can_trade_at, (
        "NUM-03: sync must happen BEFORE can_trade so the gate sees fresh balance"
    )


# ───────────── NUM-04: tick-size rounding ─────────────


def test_num04_round_to_tick_helper_exists_and_rounds_correctly():
    from core.risk_manager import round_to_tick
    # SL on a long: round DOWN (away from entry, deeper stop)
    assert round_to_tick(1142.37, side="BUY", kind="sl") == 1142.35
    # SL on a short: round UP (away from entry)
    assert round_to_tick(1142.37, side="SELL", kind="sl") == 1142.40
    # Limit BUY rounds DOWN (we pay no more)
    assert round_to_tick(1142.39, side="BUY", kind="limit") == 1142.35
    # Limit SELL rounds UP (we receive no less)
    assert round_to_tick(1142.31, side="SELL", kind="limit") == 1142.35


def test_num04_get_atr_stop_loss_rounds_to_tick():
    rm = _make_rm(config={"atr_stop_multiplier": 1.0})
    sl = rm.get_atr_stop_loss(entry_price=1000.0, atr=10.37, side="BUY")
    # 1000 - 10.37 = 989.63 -> round DOWN to 989.60 (deeper stop)
    assert sl == 989.60
    sl_short = rm.get_atr_stop_loss(entry_price=1000.0, atr=10.37, side="SELL")
    # 1000 + 10.37 = 1010.37 -> round UP to 1010.40 (deeper stop)
    assert sl_short == 1010.40


def test_num04_enforce_sl_floor_rounds_to_tick():
    rm = _make_rm(config={"min_stop_loss_pct": 1.2})
    # Strategy-supplied SL inside the 1.2% floor on a BUY at 1000.
    floored = rm.enforce_sl_floor(entry_price=1000.0, proposed_sl=995.07, side="BUY")
    # Floor = 988.00 -> already on tick, but the rounding policy applies
    # to anything that lands off-grid.
    assert abs((floored * 100) % 5) < 1e-6, "SL must land on the 0.05 tick grid"


# ───────────── NUM-08: short-side compute_round_trip mapping ─────────────


def test_num08_is_trade_worth_taking_passes_correct_legs_for_short(monkeypatch):
    from core import risk_manager as rm_mod
    captured: dict = {}

    def fake_compute(*, buy_price, sell_price, quantity, product):
        captured["buy_price"] = buy_price
        captured["sell_price"] = sell_price
        # Return enough headroom so the gate doesn't reject on charges.
        class _R:
            total = 1.0
        return _R()

    monkeypatch.setattr(rm_mod, "compute_round_trip", fake_compute)
    rm = _make_rm(config={
        "min_profit_to_charges_ratio": 0.1,
        "min_absolute_reward_rs": 1.0,
        "default_min_rr": 0.1,
    })
    # SHORT setup: entry at 1100, TP at 1080 (profit on a short).
    ok, reason = rm.is_trade_worth_taking(
        entry_price=1100.0, take_profit=1080.0, stop_loss=1110.0,
        quantity=10, side="SELL", product="INTRADAY",
    )
    assert ok, f"unexpected reject: {reason}"
    assert captured["buy_price"] == 1080.0, (
        "NUM-08: SHORT exit price (TP=1080) must be the buy_leg"
    )
    assert captured["sell_price"] == 1100.0, (
        "NUM-08: SHORT entry price (1100) must be the sell_leg"
    )


# ───────────── OBS-04: charges-compute fail-closed ─────────────


def test_obs04_is_trade_worth_taking_fails_closed_on_compute_error(monkeypatch):
    from core import risk_manager as rm_mod

    def boom(**kwargs):
        raise ZeroDivisionError("fake")

    monkeypatch.setattr(rm_mod, "compute_round_trip", boom)
    rm = _make_rm(config={
        "min_profit_to_charges_ratio": 0.1,
        "min_absolute_reward_rs": 1.0,
        "default_min_rr": 0.1,
    })
    ok, reason = rm.is_trade_worth_taking(
        entry_price=100.0, take_profit=110.0, stop_loss=95.0, quantity=10,
        side="BUY", product="INTRADAY",
    )
    assert ok is False
    assert reason == "charges_compute_failed", (
        "OBS-04: charges-compute exception must fail-closed with explicit reason"
    )


# ───────────── NUM-09: NaN VIX in classify_regime ─────────────


def test_num09_classify_regime_treats_nan_vix_as_unknown():
    from core.regime import classify_regime
    assert classify_regime({"nifty_trend": 1, "india_vix": float("nan")}) == "unknown"
    assert classify_regime({"nifty_trend": 1, "india_vix": float("inf")}) == "unknown"
    assert classify_regime({"nifty_trend": 1, "india_vix": "n/a"}) == "unknown"
    assert classify_regime({"nifty_trend": 1, "india_vix": 14.5}) == "bull_low_vol"


# ───────────── NUM-12 / OBS-19: unknown regime -> conservative multiplier ─────────────


def test_num12_obs19_regime_size_multiplier_unknown_is_conservative():
    rm = _make_rm()
    # No regime supplied -> conservative
    assert rm.regime_size_multiplier(None) == 0.5
    # Explicit "unknown" -> conservative
    assert rm.regime_size_multiplier("unknown") == 0.5


def test_num12_unknown_multiplier_is_overridable_via_config():
    rm = _make_rm(config={"regime_size_multipliers": {"unknown": 0.25}})
    assert rm.regime_size_multiplier("unknown") == 0.25


# ───────────── NUM-05 / NUM-15: trend lookahead drop ─────────────


def test_num05_trend_context_fetch_drops_today_forming_bar(monkeypatch):
    """When the daily series ends on today's date, ``_fetch_daily``
    must drop that bar before computing the rolling SMA."""
    import pandas as pd
    from datetime import datetime, timedelta
    import pytz

    from strategies import _trend_context as tc

    # Build a fake series of 60 days ending TODAY (IST).
    ist = pytz.timezone("Asia/Kolkata")
    end = datetime.now(ist).date()
    dates = [end - timedelta(days=i) for i in range(60)][::-1]
    closes = [100.0 + i * 0.5 for i in range(60)]
    # Inflate today's bar to verify it is NOT included in the SMA.
    closes[-1] = 999.0
    df = pd.DataFrame({"Close": closes}, index=pd.to_datetime(dates))

    monkeypatch.setattr(tc, "_yf_download_with_timeout", lambda *a, **k: df)
    out = tc._fetch_daily("DUMMY")
    assert out is not None
    # last_close is the LAST surviving close (yesterday), NOT 999.0
    assert out["last_close"] != 999.0, (
        "NUM-05/15: today's forming bar must be dropped from the trend snapshot"
    )


def test_num15_trend_context_get_trend_accepts_as_of_date(monkeypatch):
    import pandas as pd
    from datetime import date
    from strategies import _trend_context as tc

    monkeypatch.setattr(tc, "_yf_download_with_timeout", lambda *a, **k: pd.DataFrame())
    # Should accept the kwarg without raising.
    tc.clear_cache()
    result = tc.get_trend("DUMMY", force_refresh=True, as_of_date=date(2026, 5, 28))
    assert result is None  # empty DF -> _fetch_daily returns None


# ───────────── OBS-10: base_strategy._atr logs ─────────────


def test_obs10_base_strategy_atr_logs_and_returns_zero_on_exception():
    src = (ROOT / "packages" / "strategies" / "base_strategy.py").read_text(encoding="utf-8")
    idx = src.find("def _atr(")
    assert idx >= 0, "_atr not found"
    next_def = src.find("\n    def ", idx + 1)
    body = src[idx: next_def] if next_def > 0 else src[idx:]
    assert "_logger.warning(" in body, (
        "OBS-10: _atr exception path must log at WARNING"
    )
    assert "computation RAISED" in body, "OBS-10: log line must include the cause"


def test_obs10_atr_handles_missing_columns_with_warning():
    import pandas as pd
    from strategies.base_strategy import BaseStrategy

    bad = pd.DataFrame({"foo": [1, 2, 3]})  # missing high/low/close
    val = BaseStrategy._atr(bad, period=3)
    assert val == 0.0, "OBS-10: caller still receives 0.0 (zero-ATR guard fires upstream)"


# ───────────── CONC-01: live count refresh after entry ─────────────


def test_conc01_open_new_position_refreshes_open_position_count():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    open_pos_idx = src.find("def _open_new_position(")
    assert open_pos_idx >= 0
    next_def = src.find("\n    def ", open_pos_idx + 1)
    body = src[open_pos_idx: next_def] if next_def > 0 else src[open_pos_idx:]
    create_at = body.find("self.risk_manager.create_trailing_stop(")
    assert create_at >= 0, "create_trailing_stop call missing in _open_new_position"
    after = body[create_at: create_at + 1500]
    assert "self.risk_manager.update_open_positions(" in after, (
        "CONC-01: TradingAgent must refresh open-position count immediately after entry"
    )
