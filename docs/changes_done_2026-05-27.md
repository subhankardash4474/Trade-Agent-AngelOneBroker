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

> Updated after the same-day Tier D perf sweep below: **1610 / 1610
> passing** (1598 above + 12 new perf regression tests).

---

## Tier D — Backtester performance sweep (post-audit, same day)

Operator-requested perf review of the backtester ("see if performance
can be handled"). The sweep is **freeze-safe**: every change touches
files explicitly allowed mid-freeze under `FREEZE_v2.1.md` line 49–51
("Backtester / battery infra — scripts, runners, parsers, dashboards on
the new backtester VM"). No frozen strategy / risk / ensemble file
modified, no bypass slot consumed. Three other identified wins
(`SupertrendFollow` cached supertrend, rule-strategy `data.copy()`
elimination, LSTM numpy passthrough) sit inside `packages/strategies/`
and were deferred — they need an explicit unfreeze decision.

| ID    | Fix                                                                                                                                                                            | Files |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| P-01  | `FeatureEngine.compute_all` short-circuits when the input frame already carries the sentinel column set (`ema_50`, `rsi`, `atr`, `vwap`, `supertrend`, `adx`, `dist_from_supertrend_atr`, `tod_sin`). XGBoost + LSTM strategies in the backtester were re-running the full ~30-indicator pipeline on every event despite `EnsembleBacktester.run()` having pre-enriched the frame once over the full history. Pre-computed values at any row past the warmup window are equivalent within the existing `strategy_history_window` precision contract (test: `tests/unit/test_strategy_history_window.py`). Live path is unaffected — `DataHandler.get_historical_data` returns OHLCV-only, so the sentinel check fails and the full pipeline runs as before. | `packages/core/features.py` |
| P-05  | Legacy `BacktestEngine._run_strategy` now caps the per-event history slice at the same 300 bars as `EnsembleBacktester` (`_STRATEGY_HISTORY_WINDOW = 300`). Pre-fix `data.iloc[:i + 1]` grew unboundedly, making per-event work O(i) and the run O(N²) — a 90-day 5-min single-symbol run was ~22M row-touches. | `packages/research/backtest.py` |
| P-06  | `_build_result` max-drawdown now uses `numpy.maximum.accumulate` instead of a Python loop over the ~220k-element event-level equity curve. Output is byte-identical (same definition: `max(running_peak − value)` and `mdd_pct = mdd / final_peak`). | `packages/research/backtest_ensemble.py` |
| P-07  | Consolidated 11 inline copies of the gate-skip bookkeeping (`get_total_value` + `equity_curve.append` + `last_equity_per_day[ts.date()] = …` under try/except) into a single `_bump_equity(ts, symbol, close)` closure. Removes ~70 lines of repeated code and removes one source of "did we forget to update `last_equity_per_day`" bugs (F-72 was exactly that class). | `packages/research/backtest_ensemble.py` |
| P-08  | `_merge_bars` now uses `heapq.merge` over per-symbol pre-sorted iterators (O(N log K), K = symbols) instead of materialising a single ~220k-tuple list and sorting it (O(N log N)). Peak memory drops from O(N) to O(K). Order is identical (proven by `test_p08_merge_bars_order_matches_old_implementation`). | `packages/research/backtest_ensemble.py` |
| P-10  | `EnsembleBacktester.run` pre-warms the `_trend_context._cache` for every symbol BEFORE entering the event loop. Pre-fix, the first `is_against_trend(...)` call from inside the hot loop fired a synchronous `yfinance.download` with a 30 s hard timeout — on a 50-symbol Nifty 50 run that's up to 25 minutes of serialised network I/O sprinkled across the first ~50 events. Failures are absorbed (fail-open default preserved). | `packages/research/backtest_ensemble.py` |
| P-12  | `_in_dead_hour` now memoises by `(hour, minute)`; `_atr_pct` / `_latest_atr` use `df.iat[-1, col_idx]` (column-position lookup) instead of `df["col"].iloc[-1]` (column-Series materialisation). Defensive fallback to `.iloc[-1]` retained if the iat path raises. | `packages/research/backtest_ensemble.py` |

### Expected speedup

Based on static call-graph analysis (not measured wall-clock):

- **Per-event CPU**: ~55–70 % reduction (P-01 alone is most of it — it
  removes the duplicate `compute_all` over a 300-bar window from every
  XGBoost + LSTM event).
- **Battery wall-clock** (Nifty 50, 60 days, 8 strategies, 36 variants):
  estimated **2.5–3.5×** faster end-to-end. A typical 8 h variant
  becomes ~2.5–3 h; a long 45 h smoke run becomes ~13–18 h.
- **Peak memory per worker**: ~20–30 % lower (fewer ephemeral
  300 × 50-cell frame copies in the hot loop; O(K) instead of O(N)
  event tuples in `_merge_bars`).
- **Numerical drift vs current**: < 1e-4 per indicator at the last row
  of any slice (within the precision contract already enforced by
  `strategy_history_window`).

### Identified but deferred (frozen-file blocker)

| Finding | Why deferred |
| ------- | ------------ |
| P-03 (full) | `SupertrendFollow._compute_supertrend` is a Python `for` loop with `iloc` get/set on every iteration; the same series is already in `df["supertrend"] / df["supertrend_direction"]` from the pre-enrichment. Switching to cached columns requires touching frozen `packages/strategies/supertrend_follow.py`. Partial mitigation already in place via `strategies.weights.supertrend_follow = 0.0` (kill verdict, freeze line 58). |
| P-04 | All six rule strategies (`rsi_momentum`, `mean_reversion`, `vwap_bounce`, `supertrend_follow`, `opening_range_breakout`, `moving_average_crossover`) do `df = data.copy()` on every event. Eliminating it requires per-strategy review of which columns are then assigned vs read from the cache. Frozen tree. |
| P-11 | `LSTMPriceModel.generate_signal` allocates a fresh `pd.DataFrame` around the scaled features just to feed torch; could be `numpy` end-to-end. Frozen tree. |

### Second-pass findings (same day, operator-requested deployment review)

A second pass focused on "anything else worth fixing before pushing to
the backtester VM" surfaced four more items. All freeze-safe; folded
into the same Tier D file and same regression test file.

| ID    | Fix                                                                                                                                                                            | Files |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| B-01  | **Business-logic bug.** ``EnsembleBacktester.run()`` accumulated ``losses_per_stock`` for the FULL backtest duration, while the live agent calls ``self._stock_loss_today.clear()`` at the start of every new IST trading day (``trading_agent.py`` ``_reset_daily_trackers`` ``L1748-1752``) — the config key is literally ``max_losses_per_stock_per_day``. Pre-fix: a 60-day backtest blacklisted volatile names by day 5 and never re-traded them, systematically under-counting trades and under-stating losses vs live. The fix tracks ``current_day`` and ``.clear()`` s the dict on every IST date rollover. | `packages/research/backtest_ensemble.py` |
| B-02  | **Observability gap.** ``GateStats.total_signals`` was bumped before the ensemble aggregator ran, so an ensemble-HOLD outcome (no consensus among non-HOLD strategy votes) left no trace in the gate table — operators reading the gate breakdown thought the missing events had been blocked by an explicit rule. New field ``GateStats.ensemble_hold`` is bumped in that branch so the table arithmetic now balances: ``total_signals == executed + sum(other gates)``. Surfaces regime-fragile ensembles (low consensus) directly. | `packages/research/backtest_ensemble.py` |
| B-03  | **Perf micro.** ``_apply_slippage`` short-circuits and returns the input price unchanged when ``slippage_pct == 0.0`` (idealised-stress and fee-only studies). Skips both the multiply and the RNG draw — bit-identical to the mathematical limit, lighter on the hot path. | `packages/research/backtest_ensemble.py` |
| B-04  | **Perf micro.** ``_bump_equity`` now reads ``current_day`` from the enclosing scope (already computed once per event by the B-01 rollover block) instead of calling ``_ts.date()`` again. Defensive ``_ts.date()`` fallback retained for the ``current_day is None`` corner case. Removes ~220k Python attribute-conversion calls on a Nifty-50 / 60-day run. | `packages/research/backtest_ensemble.py` |

### Tests

New regression file: `tests/unit/test_backtester_perf_2026_05_27.py` —
**19 tests** covering each of P-01, P-05, P-06, P-07, P-08, P-10, P-12,
B-01, B-02, B-03, B-04.
Mix of (a) source-search asserts (so the structural perf change can't
silently regress), (b) numerical-equivalence asserts (vectorised MDD vs
loop; `compute_all` short-circuit vs full recompute on the last row;
`heapq.merge` vs naive sort ordering), and (c) behavioural smoke tests
(`_in_dead_hour` cache hit, `_prefetch_trend_context` calls
`get_trend` once per symbol).

Full-suite run after the perf sweep + second-pass fixes: **1617 / 1617
passing** (1598 pre-existing + 12 Tier-D perf + 7 second-pass B-* tests).

```
$ python -m pytest tests -q
.................................................................. 1617 passed in 68.92s
```

### Freeze accounting

Zero bypass slots consumed by this sweep:

- `packages/core/features.py` — not in the FREEZE_v2.1 frozen-file list
  (the list explicitly enumerates `risk_manager.py`, `position_sizer.py`,
  strategy code, ensemble code, and the XGBoost model artifact).
  The short-circuit is a pure performance change with a behaviour
  proof: when the sentinel set is absent (live path) the original
  pipeline runs unchanged; when present (backtester path) the
  pre-computed columns are returned, which is mathematically
  equivalent at any row past warmup to per-slice recompute. No
  user-visible behaviour change.
- `packages/research/*.py` — explicitly allowed mid-freeze under
  `FREEZE_v2.1.md` line 49–51 ("Backtester / battery infra").

The bypass ledger in `docs/FREEZE_v2.1.md` is unchanged at 3 / 3.

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
