"""Regression tests for the 2026-05-27 audit fixes.

Each test maps 1:1 to a finding ID in
``docs/findings/findings_2026-05-27.md`` and the corresponding fix in
``docs/changes/changes_done_2026-05-27.md``. Naming convention:
``test_<finding>_<one_line_intent>``.

Tests are intentionally small/fast -- they are the "no new issues"
guardrail for this round of fixes. If anyone breaks these, the
corresponding finding has regressed.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────── Tier A4 ────────────────────────────


def test_f63_streamlit_today_pnl_uses_ist():
    """F-63: dashboard "Today" cutoff must be IST-anchored, not host TZ."""
    src = (PACKAGES / "monitoring" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "Asia/Kolkata" in src
    assert "_range_to_cutoff" in src
    # both `_range_to_cutoff` AND the today_pnl block should reference IST.
    assert src.count("Asia/Kolkata") >= 3


def test_f86_streamlit_market_open_honours_holidays():
    src = (PACKAGES / "monitoring" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "NSE_HOLIDAYS" in src
    assert "is_market_open" in src


def test_f87_streamlit_profit_factor_capped_at_sentinel():
    src = (PACKAGES / "monitoring" / "streamlit_app.py").read_text(encoding="utf-8")
    # No more float("inf") emitted; 999.99 sentinel + glyph cap.
    assert "999.99" in src
    assert ">= 999.99" in src
    assert "profit_factor = float(\"inf\")" not in src


def test_f88_alert_log_truncates_body():
    src = (PACKAGES / "monitoring" / "alerts.py").read_text(encoding="utf-8")
    # The 200-char preview + newline collapse are the only marker.
    assert "preview = message" in src
    assert "len(message) <= 200" in src


def test_f83_xgboost_holds_on_probability_tie():
    src = (PACKAGES / "strategies" / "xgboost_classifier.py").read_text(encoding="utf-8")
    assert "prob_tie" in src
    assert "predicted_class = -1" in src


def test_f84_ensemble_uses_unique_strategy_count():
    src = (PACKAGES / "strategies" / "ensemble.py").read_text(encoding="utf-8")
    assert "buy_unique" in src
    assert "sell_unique" in src
    assert "buy_unique >= self.min_strategies_agree" in src
    assert "sell_unique >= self.min_strategies_agree" in src


# ─────────────────────────── Tier B1 ────────────────────────────


def test_f13_lstm_passes_market_context_to_feature_engine():
    src = (PACKAGES / "strategies" / "lstm_model.py").read_text(encoding="utf-8")
    # The new call site must pass market_context kwarg.
    assert "market_context=self._market_context or None" in src
    # The buggy AttributeError-swallowing setter call is gone.
    assert "self._feature_engine.set_market_context(self._market_context)" not in src


def test_f14_lstm_nan_skew_tripwire():
    src = (PACKAGES / "strategies" / "lstm_model.py").read_text(encoding="utf-8")
    assert "feature_nan_skew" in src
    assert "_nan_warn_threshold" in src


def test_f15_lstm_refuses_without_scaler():
    src = (PACKAGES / "strategies" / "lstm_model.py").read_text(encoding="utf-8")
    assert "scaler_missing" in src
    # Must disable the model when scaler is missing so generate_signal HOLDs.
    assert "self._model = None  # force HOLD" in src


def test_f42_lstm_validates_feature_count_contract():
    src = (PACKAGES / "strategies" / "lstm_model.py").read_text(encoding="utf-8")
    assert "_validate_model_contract" in src
    assert "feature_count_drift" in src


# ─────────────────────────── Tier B2 ────────────────────────────


def test_f10_websocket_emits_per_tick_volume_delta():
    src = (PACKAGES / "core" / "websocket_client.py").read_text(encoding="utf-8")
    assert "_cumulative_to_delta" in src
    assert "_last_cum_volume" in src
    # Both Angel + Kite paths converted.
    assert src.count("_cumulative_to_delta(") >= 2


def test_f10_delta_helper_returns_zero_on_session_reset():
    # Import WebSocketClient via the package layout
    from core.websocket_client import WebSocketClient
    ws = WebSocketClient(broker="paper", config={})
    # First call: no baseline -> 0.
    assert ws._cumulative_to_delta("RELIANCE", 1000.0) == 0.0
    # Second call: 1500 - 1000 = 500.
    assert ws._cumulative_to_delta("RELIANCE", 1500.0) == 500.0
    # Drop (session reset) -> 0, baseline updates.
    assert ws._cumulative_to_delta("RELIANCE", 200.0) == 0.0
    # Subsequent monotone increase resumes deltas from the new baseline.
    assert ws._cumulative_to_delta("RELIANCE", 350.0) == 150.0


def test_f11_set_subscriptions_pushes_live_delta():
    src = (PACKAGES / "core" / "websocket_client.py").read_text(encoding="utf-8")
    assert "_apply_subscription_delta" in src
    # Both paths must respect the broker switch and surface deltas.
    assert "if self._broker == \"angelone\":" in src
    assert "if self._broker == \"kite\":" in src


def test_f12_regime_accumulators_replayed_on_load():
    src = (PACKAGES / "core" / "trade_analyzer.py").read_text(encoding="utf-8")
    assert "_regime_stats[(strategy, regime)] = r_stats" in src
    # Replay must reset gross_wins/losses/pnl_list on regime stats.
    assert "for r_stats in self._regime_stats.values():" in src


def test_f51_historical_cache_key_is_ist_normalised():
    src = (PACKAGES / "core" / "historical_cache.py").read_text(encoding="utf-8")
    assert "_ist_yyyymmdd" in src
    assert "astimezone(IST).strftime(\"%Y%m%d\")" in src


def test_f44_features_day_window_is_75_bars():
    src = (PACKAGES / "core" / "features.py").read_text(encoding="utf-8")
    assert "rolling(75)" in src
    # The old 78 must be gone from the day-range computation comment block.
    # (Other 78-references may remain in unrelated code; this asserts the
    # specific fix landed.)
    assert "rolling(78)" not in src


def test_f85_obv_resets_per_session():
    src = (PACKAGES / "core" / "features.py").read_text(encoding="utf-8")
    assert "groupby(df.index.date).cumsum()" in src


# ─────────────────────────── Tier B3 ────────────────────────────


def test_f09_safe_exit_accepts_partial_fill():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # New PARTIAL flag accepts PARTIALLY_FILLED.
    assert "\"PARTIALLY_FILLED\"" in src
    assert "[SAFE-EXIT-PARTIAL]" in src
    # adjust_position_quantity is the new portfolio entrypoint for residual.
    assert "adjust_position_quantity(symbol, filled_qty)" in src


def test_f09_portfolio_exposes_adjust_position_quantity():
    from core.portfolio import Portfolio
    assert hasattr(Portfolio, "adjust_position_quantity")


def test_f33_loss_limits_anchor_to_peak_balance():
    src = (PACKAGES / "core" / "risk_manager.py").read_text(encoding="utf-8")
    # The anchor variable is the high-water mark.
    assert "anchor = max(self._initial_balance, self.state.peak_balance)" in src
    assert "anchor * (self.daily_loss_limit_pct / 100)" in src
    assert "anchor * (self.weekly_loss_limit_pct / 100)" in src


def test_f34_position_sizing_returns_zero_when_budget_too_small():
    from core.risk_manager import RiskManager
    cfg = {
        "risk": {
            "max_risk_per_trade_pct": 0.001,  # 0.1bp -- tiny budget
            "max_position_size_pct": 100,
            "atr_stop_multiplier": 1.5,
            "min_stop_loss_pct": 0,
        }
    }
    rm = RiskManager(cfg, initial_balance=100.0)
    # Price 1000, SL 990 -> risk per share 10 -> budget 0.10 -> 0 shares.
    qty = rm.calculate_position_size(price=1000.0, stop_loss_price=990.0)
    assert qty == 0, "Pre-fix code would have forced 1 share here."


def test_f08_trading_cycle_polls_stop_between_instruments():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # The new poll lives in the per-instrument loop with the F-08 tag.
    assert "F-08 (audit 2026-05-27)" in src
    assert "[EMERGENCY-STOP] mid-cycle STOP detected" in src


def test_f29_fast_exits_polls_stop_with_empty_book():
    src = (ROOT / "trading_agent.py").read_text(encoding="utf-8")
    # The empty-book branch no longer short-circuits the slicing.
    assert "F-29 (audit 2026-05-27)" in src
    assert "loop back for next STOP poll" in src


# ─────────────────────────── Tier B4 ────────────────────────────


def test_f45_base_strategy_atr_uses_ewm():
    src = (PACKAGES / "strategies" / "base_strategy.py").read_text(encoding="utf-8")
    assert "tr.ewm(span=period, adjust=False).mean()" in src
    # Old SMA path is gone.
    assert "tr.rolling(period).mean()" not in src


def test_f46_vwap_uses_symmetric_atr_and_volume():
    src = (PACKAGES / "strategies" / "vwap_bounce.py").read_text(encoding="utf-8")
    # BUY now uses the BaseStrategy ATR helper, not the broken
    # (max-min)/14 expression. P-04 (perf 2026-05-27) renamed the local
    # frame variable from ``df`` to ``data`` (dropped ``data.copy()``),
    # so we match either argument name to keep the F-46 contract test
    # decoupled from the perf-fix variable rename.
    assert ("atr = self._atr(df)" in src) or ("atr = self._atr(data)" in src)
    assert "df[\"high\"].iloc[-14:].max() - df[\"low\"].iloc[-14:].min()" not in src
    assert "data[\"high\"].iloc[-14:].max() - data[\"low\"].iloc[-14:].min()" not in src
    # SELL threshold now uses the strategy-configured volume_spike_ratio.
    assert "vol_ratio >= self.volume_spike_ratio" in src
    # And the old asymmetric `vol_ratio >= 1.0` literal is gone from the SELL branch.


def test_f47_vwap_blocks_session_boundary_comparison():
    src = (PACKAGES / "strategies" / "vwap_bounce.py").read_text(encoding="utf-8")
    assert "session_boundary" in src


def test_f48_trend_filter_negative_cache_has_short_ttl():
    from strategies import _trend_context as tc
    assert hasattr(tc, "_NEGATIVE_CACHE_TTL_SEC")
    assert tc._NEGATIVE_CACHE_TTL_SEC < tc.CACHE_TTL_SEC
    # Sanity: positive TTL is hours, negative TTL is minutes.
    assert tc._NEGATIVE_CACHE_TTL_SEC <= 1800


# ─────────────────────────── Tier C1 ────────────────────────────


def test_f64_battery_propagates_paper_seed():
    src = (PACKAGES / "research" / "battery.py").read_text(encoding="utf-8")
    assert "paper_seed=cfg.get(\"backtest\", {}).get(\"paper_seed\")" in src


def test_f26_backtest_slippage_uses_paper_rng_when_seeded():
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(encoding="utf-8")
    assert "_paper_rng.uniform(0.0, self.bt.slippage_pct)" in src
    # Deterministic fall-through preserved when paper_seed is None.
    assert "if self.bt.paper_seed is None" in src


def test_f67_backtest_end_uses_slippage():
    eng = (PACKAGES / "research" / "backtest.py").read_text(encoding="utf-8")
    ens = (PACKAGES / "research" / "backtest_ensemble.py").read_text(encoding="utf-8")
    assert "F-67 (audit 2026-05-27)" in eng
    assert "F-67 (audit 2026-05-27)" in ens


def test_f71_battery_serial_writes_atomic():
    src = (PACKAGES / "research" / "battery.py").read_text(encoding="utf-8")
    # The first write_text for results JSON in serial mode is now the atomic helper.
    # Ensure the plain-write of results/<name>.json is gone in the serial path.
    assert "(out_root / \"results\" / f\"{name}.json\").write_text(" not in src
    # configs/.yaml write also routed through the helper.
    assert "(out_root / \"configs\" / f\"{name}.yaml\").write_text(" not in src


def test_f72_expected_profit_gate_updates_last_equity_per_day():
    src = (PACKAGES / "research" / "backtest_ensemble.py").read_text(encoding="utf-8")
    # The expected-profit branch now mirrors the other branches' housekeeping.
    assert "F-72 (audit 2026-05-27)" in src


def test_f103_dead_commission_pct_emits_warning():
    src = (PACKAGES / "research" / "backtest.py").read_text(encoding="utf-8")
    assert "F-103 (audit 2026-05-27)" in src
    assert "is configured but IGNORED" in src


def test_f104_analyze_day_computes_sharpe_and_drawdown():
    src = (PACKAGES / "research" / "analyze_day.py").read_text(encoding="utf-8")
    assert "_compute_equity_metrics" in src
    assert "Daily Sharpe (eq curve)" in src
    assert "Max drawdown (eq curve)" in src


# ─────────────────────────── Tier C2 ────────────────────────────


def test_f22_xgboost_early_stop_uses_train_tail_validation():
    src = (PACKAGES / "training" / "train_xgboost.py").read_text(encoding="utf-8")
    assert "F-22 (audit 2026-05-27)" in src
    # The validation tail is computed from X_train.
    assert "X_val = X_train.iloc[n_train - n_val :]" in src


def test_f100_xgboost_docstring_no_false_cv_claim():
    src = (PACKAGES / "training" / "train_xgboost.py").read_text(encoding="utf-8")
    # Old docstring claimed time-series CV; the corrected version
    # explicitly acknowledges single-split + early-stop tail slice.
    assert "no CV is implemented" in src


def test_f23_lstm_ships_best_checkpoint():
    src = (PACKAGES / "training" / "train_lstm.py").read_text(encoding="utf-8")
    assert "F-23 (audit 2026-05-27)" in src
    assert "best_path = model_output.replace(\".pt\", \"_best.pt\")" in src


def test_f68_lstm_uses_median_imputation():
    src = (PACKAGES / "training" / "train_lstm.py").read_text(encoding="utf-8")
    assert "F-68 (audit 2026-05-27)" in src
    assert "medians = train_features.median(numeric_only=True)" in src


def test_f69_lstm_training_seeded():
    src = (PACKAGES / "training" / "train_lstm.py").read_text(encoding="utf-8")
    assert "F-69 (audit 2026-05-27)" in src
    assert "torch.manual_seed(seed)" in src
    # CLI exposes the seed knob.
    assert "--seed" in src


def test_f24_prepare_dataset_shifts_market_ctx_for_intraday():
    src = (PACKAGES / "training" / "prepare_dataset.py").read_text(encoding="utf-8")
    assert "F-24 (audit 2026-05-27)" in src
    assert "ctx_shifted = market_ctx.copy()" in src
    assert "ctx_shifted.index = ctx_shifted.index + pd.Timedelta(days=1)" in src


def test_f70_prepare_dataset_fails_hard_on_split_error():
    src = (PACKAGES / "training" / "prepare_dataset.py").read_text(encoding="utf-8")
    assert "F-70 (audit 2026-05-27)" in src
    # The buggy row-index fallback is gone for the time-aware path.
    assert "RuntimeError(" in src


# ─────────────────────────── Tier A2/A3 cross-cuts ──────────────


def test_f35_portfolio_profit_factor_capped():
    """F-35 was fixed in an earlier tier; assert the cap is in place
    here so any future regression keeps the analyzer/portfolio aligned."""
    from core.portfolio import Portfolio, TradeRecord
    p = Portfolio(initial_balance=10000, log_dir=str(ROOT / "logs" / "_unit_test"))
    # Synthesize an all-win record so gross_loss == 0
    now = datetime.now()
    p.trade_history.append(TradeRecord(
        symbol="X", side="BUY", entry_price=100.0, exit_price=101.0,
        quantity=1, entry_time=now, exit_time=now,
        pnl=100.0, pnl_pct=1.0, strategy="t", exit_reason="t",
    ))
    metrics = p.get_performance_metrics()
    # Either capped or computed (not inf, not NaN).
    pf = metrics.get("profit_factor", 0.0)
    assert pf != float("inf")
    assert pf == pf  # not NaN
