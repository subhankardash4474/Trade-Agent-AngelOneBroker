# Changes Done — 2026-05-27

> Companion to `docs/findings_2026-05-27.md`. This file is the
> **single-source-of-truth ledger** of every fix that landed in the
> 2026-05-27 audit sweep. Format mirrors `docs/changes_done_2026-05-26.md`:
> one row per finding, grouped by tier, every entry citing exact files
> + the comment tag (`F-NN (audit 2026-05-27)`) added to source so a
> future reviewer can grep-find the rationale.

## Headline

- **Findings raised:** 108 (`docs/findings_2026-05-27.md`).
- **Findings fixed this sweep:** 38 (Tiers A4 → C2).
- **Findings already fixed in previous sweeps:** 65 (Tiers A1 / A2 / A3).
- **Deferred (require user policy / large architectural change):** 5
  (`D-F-01..D-F-05`, see §Deferred below).
- **Tests:** 1598 / 1598 passing (1556 pre-existing + 42 new regression
  tests in `tests/unit/test_audit_2026_05_27_fixes.py`).
- **Static analysis:** unchanged lint footprint; all new code is clean
  on `ruff` (no F-class / B-class warnings introduced).

---

## Tier A4 — Streamlit + alert defensive fixes

| ID    | Fix                                                                                   | Files |
| ----- | ------------------------------------------------------------------------------------- | ----- |
| F-63  | Dashboard "Today" cutoff + Today P&L now use IST date, not host-local datetime        | `packages/monitoring/streamlit_app.py` |
| F-86  | `is_market_open()` honours `NSE_HOLIDAYS` (defensive lazy import; falls through if missing) | `packages/monitoring/streamlit_app.py` |
| F-87  | Risk tab PF capped at `999.99` (matches `trade_analyzer` + `portfolio` sentinel); UI still renders ∞ glyph | `packages/monitoring/streamlit_app.py` |
| F-88  | Alert in-log preview truncated to 200 chars + newline-collapsed (full body still goes by email + spool) | `packages/monitoring/alerts.py` |
| F-83  | XGBoost classifier returns HOLD on exact probability tie (matches LSTM); `reason="prob_tie"` surfaced for diagnostics | `packages/strategies/xgboost_classifier.py` |
| F-84  | Ensemble `min_strategies_agree` now counts **unique strategy names** instead of raw vote count (defence against accidental duplicate registration) | `packages/strategies/ensemble.py` |

---

## Tier B1 — LSTM correctness cluster

| ID   | Fix                                                                                                                                | Files |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------- | ----- |
| F-13 | LSTM `generate_signal` now passes `self._market_context` to `FeatureEngine.compute_all` (parity with XGBoost). Removed broken `FeatureEngine.set_market_context(...)` call that silently `except AttributeError`'d every cycle. | `packages/strategies/lstm_model.py` |
| F-14 | Train/serve NaN-skew tripwire: if >25% of feature cells in the inference window are NaN, HOLD with `reason="feature_nan_skew"` and one-shot WARN per symbol | `packages/strategies/lstm_model.py` |
| F-15 | If model loads but scaler is missing, model is disabled (`_unhealthy_reason="scaler_missing"`) and generate_signal HOLDs — un-scaled features would otherwise produce confidently-wrong predictions | `packages/strategies/lstm_model.py` |
| F-42 | New `_validate_model_contract()` reads `input_size` off the first `nn.LSTM` module and refuses to predict on feature-count drift (mirrors XGBoost's `n_features_in_` check) | `packages/strategies/lstm_model.py` |

LSTM now has an `is_healthy()` method (parity with XGBoost) so the
orchestrator can uniformly query both ML strategies.

---

## Tier B2 — Data layer correctness

| ID   | Fix                                                                                                                                                                            | Files |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| F-10 | WebSocket tick `volume` field now emits per-tick **delta** instead of broker-supplied cumulative session volume; new helper `_cumulative_to_delta()` + `_last_cum_volume` baseline; treats `cur < last` as a session reset and re-baselines silently. `reset_session_volume_baseline()` exposed for the daemon's day-rollover hook. | `packages/core/websocket_client.py` |
| F-11 | `set_subscriptions()` / `unsubscribe()` now push the delta to the live socket (Angel + Kite branches) via new `_apply_subscription_delta()`; falls back to "applied on next reconnect" on broker-SDK exceptions | `packages/core/websocket_client.py` |
| F-12 | `_rehydrate_internal_accumulators()` now replays trades into per-`(strategy, regime)` accumulators too, so PF / Sharpe are correct on restart for the per-regime weighter (not just the global one) | `packages/core/trade_analyzer.py` |
| F-51 | Historical cache key path IST-normalises both `start_date` and `end_date` before formatting `%Y%m%d`, so naive vs UTC-aware callers no longer produce duplicate cache entries (or miss each other's hits) | `packages/core/historical_cache.py` |
| F-44 | `_add_derived_features` day high/low window changed from 78 → **75** bars (NSE cash session = 6h15 = 375 min = exactly 75 five-min candles); eliminates ~15-min leak from prior session into today's breakout/mean-rev decisions near the open | `packages/core/features.py` |
| F-85 | OBV now resets per session (`groupby(df.index.date).cumsum()`), aligning with VWAP which has been per-session since forever; ends cross-session A/D mixing on multi-day windows | `packages/core/features.py` |

---

## Tier B3 — Risk + exit symmetry

| ID   | Fix                                                                                                                                                                            | Files |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| F-09 | `_close_position_safely` accepts `PARTIALLY_FILLED` as a partial exit instead of treating it as a hard failure: in-memory position is down-sized via new `Portfolio.adjust_position_quantity()`, residual stays open for the next exit cycle, CRITICAL alert raised, DB row updated via new `DB.update_position_quantity()`. | `trading_agent.py`, `packages/core/portfolio.py`, `packages/core/database.py` |
| F-33 | Daily + weekly loss limits anchored to `max(_initial_balance, peak_balance)` (high-water mark) instead of boot-time `_initial_balance`. Limits grow with the account, never shrink during drawdowns. | `packages/core/risk_manager.py` |
| F-34 | Position sizing no longer forces `max(1, ...)` when the risk budget says 0 shares; returns 0 so the orchestrator skips the trade. Previously the 1-share floor silently exceeded the per-trade risk budget by an unknown multiple. | `packages/core/risk_manager.py` |
| F-08 | `_trading_cycle` polls the STOP file **between instruments** in the per-symbol loop; previously a STOP dropped mid-cycle had to wait for the entire watchlist iteration to finish (60+s on a 250-name list). | `trading_agent.py` |
| F-29 | `_fast_exits_sleep` empty-book branch now slices the sleep and polls STOP per slice (instead of sleeping the full poll_interval in one shot). Uniform STOP-latency upper bound regardless of book state. | `trading_agent.py` |

---

## Tier B4 — Strategy symmetry + safety

| ID   | Fix                                                                                                                                                                            | Files |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| F-45 | `BaseStrategy._atr` switched from `tr.rolling(period).mean()` (SMA) to `tr.ewm(span=period, adjust=False).mean()` (EWM) so this helper produces the SAME value as `FeatureEngine` + ADX/Supertrend ATR. Pre-fix, the strategy-side SL math and the regime/conviction gating math disagreed. | `packages/strategies/base_strategy.py` |
| F-46 | VWAP-bounce BUY now uses `self._atr(df)` (same as SELL) instead of the broken `(14-bar high - 14-bar low) / 14` expression that was ~14x smaller than ATR. SELL volume threshold raised from `>= 1.0` to `>= self.volume_spike_ratio` so both sides demand the same volume confirmation. | `packages/strategies/vwap_bounce.py` |
| F-47 | VWAP-bounce now HOLDs with `reason="session_boundary"` when `prev` and `current` bars are on different calendar dates. Comparing `prev["close"] vs prev["vwap"]` across the daily VWAP reset was meaningless and triggered spurious signals near every session open. | `packages/strategies/vwap_bounce.py` |
| F-48 | Trend filter negative cache TTL: a failed `_fetch_daily` now caches the `None` result for `TREND_NEG_TTL_SEC` (default **300s**) instead of the positive `CACHE_TTL_SEC` (6h). Single transient yfinance hiccup at the open no longer silently disables the trend filter for the whole session. | `packages/strategies/_trend_context.py` |

---

## Tier C1 — Backtest fidelity

| ID    | Fix                                                                                                                                                                            | Files |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| F-26  | `BacktestRunner._apply_slippage()` now samples `_paper_rng.uniform(0, slippage_pct)` when `paper_seed is not None`, mirroring the live `_paper_order` distribution. Deterministic-mean formula preserved when `paper_seed` is `None` (backwards compat). | `packages/research/backtest_ensemble.py` |
| F-64  | Battery's `_bt_config()` now plumbs `cfg["backtest"]["paper_seed"]` through to `BacktestConfig.paper_seed`, so the dataclass field is no longer dead. | `packages/research/battery.py` |
| F-67  | End-of-backtest flatten now applies slippage (both `backtest.py` and `backtest_ensemble.py`). Pre-fix, residual positions exited at the exact last-close, systematically over-reporting final equity by ~one round-trip slippage per residual position. | `packages/research/backtest.py`, `packages/research/backtest_ensemble.py` |
| F-71  | Battery serial-mode writes (`results/<name>.json`, `configs/<name>.yaml`) now go through `_atomic_write_text` (parity with the parallel-mode path). A crash mid-write no longer leaves a truncated JSON that `_already_done` later mistakes for a complete variant on resume. | `packages/research/battery.py` |
| F-72  | Expected-profit-gate branch in `backtest_ensemble.py` now updates BOTH `equity_curve` and `last_equity_per_day` (the other gate branches already did); per-day Sharpe / daily-pct downstream no longer attributes a bar's equity change to the wrong day. | `packages/research/backtest_ensemble.py` |
| F-103 | Simple-backtest `backtest.commission_pct` is now flagged as a dead knob: still accepted for backwards compat, but a loud one-shot WARN tells the operator that charges actually come from `packages/core/charges.py` (env-driven). | `packages/research/backtest.py` |
| F-104 | `analyze_day.py` now computes daily Sharpe + max-DD% from the loaded `equity_curve` rows (`_compute_equity_metrics` helper) and renders them in the report summary. Pre-fix the equity series was loaded then thrown away. | `packages/research/analyze_day.py` |

---

## Tier C2 — Training pipeline

| ID    | Fix                                                                                                                                                                            | Files |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| F-22  | XGBoost training now carves a chronological **tail slice** of `X_train` (default 15%) as the early-stopping validation set. Test set is no longer used for `best_iteration` selection — held-out metrics below are now uncontaminated. Fall-through if training set too small (with loud WARN). | `packages/training/train_xgboost.py` |
| F-100 | Module docstring updated: previously claimed "time-series cross-validation" but no CV is implemented. New docstring explicitly says single chronological split + tail-slice early stop (consistent with F-22). | `packages/training/train_xgboost.py` |
| F-23  | LSTM training now loads the best-checkpoint (`<model>_best.pt`) before final save when the best-epoch beat the final-epoch on test accuracy. Pre-fix, deployed model was always the LAST-epoch weights even when they were objectively worse. | `packages/training/train_lstm.py` |
| F-68  | LSTM training now uses **per-column median imputation** (computed on train, applied to test — no leakage) instead of `.fillna(0)` before scaling. Loud WARN with NaN inventory. | `packages/training/train_lstm.py` |
| F-69  | LSTM training now seeds `python.random`, `numpy.random`, and `torch.manual_seed` (default `seed=42`, CLI-overridable via `--seed`). Runs reproducible across invocations. | `packages/training/train_lstm.py` |
| F-24  | `prepare_dataset.py` now shifts the daily Nifty/VIX context by +1 day before tagging intraday bars (`ctx_shifted.index = ctx_shifted.index + pd.Timedelta(days=1)`). Closes the daily-close lookahead where today's 09:30 bar was tagged with today's end-of-day Nifty trend. | `packages/training/prepare_dataset.py` |
| F-70  | `prepare_dataset.py` time-aware-split branch now `raise RuntimeError` instead of silently falling back to the buggy row-index split on exception. Fail loudly so the operator fixes the index (the row-index fallback was exactly the leakage path P1 #7 eliminated). | `packages/training/prepare_dataset.py` |

---

## Tests

New regression file: `tests/unit/test_audit_2026_05_27_fixes.py` —
**42 tests**, one per finding-fix above (some grouping where a single
multi-file fix is asserted at the source-search level). Every test is
fast (<200ms) and binds the fix to a specific marker in source so a
future regression is caught at PR time.

Full-suite run after the final fix: **1598 / 1598 passing**
(1556 pre-existing + 42 new).

```
$ python -m pytest -x -q
.................................................................. 1598 passed in 76.97s
```

---

## Deferred items (require user policy / large architectural change)

| ID       | Original finding | Why deferred |
| -------- | ---------------- | ------------ |
| D-F-01   | F-25: battery backtester hardcodes `regime="unknown"` | Requires Nifty + India VIX bars in `market_data.pkl` + a per-bar regime classifier. Architectural; multi-day. |
| D-F-02   | F-101: `pickle.load` on battery cache without integrity check | Threat model: cache files live under operator-owned `data/cache/`. Hashing + signing requires a key-distribution policy. |
| D-F-03   | F-102: battery `--run-id` reuse without `--resume` mixes prior-run artifacts | Two valid policies (auto-resume vs reject). Operator decision. |
| D-F-04   | F-105: `pickle.load` / `torch.load(weights_only=False)` are arbitrary-code-on-load. Logged at audit B-19. | Migration plan: switch artifacts to `state_dict` + reconstruction so `weights_only=True` is safe. Multi-week. |
| D-F-05   | F-106: Telegram alert path referenced in audit scope but never implemented. | Feature request, not a bug. |

Each deferred item is documented inline in the source with a
`(deferred)` tag near the relevant code, pointing back to this file.
