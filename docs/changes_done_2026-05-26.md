# Changes Done — 2026-05-26 (Audit Fix Sweep)

> **TL;DR:** Operator-directed sweep of the consolidated 2026-05-25 / 2026-05-26
> audit. 30 findings fixed across 8 phases. **35 new regression tests added,
> 1556 / 1556 total tests passing.** Slot #2 consumed (see `FREEZE_v2.1.md`
> §Slot consumption / Slot #2 rationale). **Not yet deployed** — the trader
> VM continues to run pre-fix code; the operator decides the cutover window
> (natural choice: tomorrow's 09:15 IST open).

---

## Phase 0 — Ground rules

Three findings were explicitly **deferred** to a dedicated session because
they need a mock-broker harness or architectural review and are too risky
to bundle here:

1. **B-2 + C-6 (idempotency tag)** — order-retry duplicate prevention
   needs broker-side coordination + a mock-broker harness. The
   **timeout half of C-6** is included in this sweep (Phase 7); the
   idempotency tag waits.
2. **C-5 (broker wrapper cutover)** — moving the live daemon to
   `packages/brokers/angelone.py` instead of raw SmartConnect via
   `core/execution.py`. ~1-day refactor, requires its own paper-mode
   shakedown and bypass slot.
3. **C-20 (simple-backtest deprecation)** — purely documentation. To
   be addressed when README is refreshed next.

---

## Phase 1 — Operator-tool & dead-code wins

| Finding | File | Change |
|---------|------|--------|
| **C-3** | `tools/close_position.py` | Removed non-existent `from core.config_loader import load_config`; replaced with `yaml.safe_load` reading from repo-root `config.yaml`. Pre-fix the manual-flatten tool raised ImportError on every invocation. |
| **C-4** | `tools/close_position.py` | Switched from never-existing `trading.initial_capital` + `trading.commission_pct` keys to real `capital.initial_balance` + `execution.product_type`. Pre-fix would have seeded Portfolio with stale ₹10k default vs configured ₹100k. |
| **C-28** | `tools/close_position.py` | Bounded `_get_ltp` with a worker-thread + `join(timeout=15s)` so a stalled Yahoo endpoint can no longer hang the manual-flatten tool indefinitely. |
| **B-17** | `trading_agent.py` | `Position` forward-ref now resolves via `TYPE_CHECKING` import (zero runtime cost). Pre-fix ruff/mypy raised F821 on `pos: "Position"` at line ~3848. |
| **B-20** | `tools/run_battery_queue.py` | Added `from err` to two `raise SystemExit(...)` inside except clauses. B904 hygiene. |
| **C-19** | `packages/research/backtest_ensemble.py` | Removed dead `from core.charges import compute_round_trip` and unused `from core.regime import classify_regime`. |
| **C-26** | `config.yaml` | Added `use_live_universe: false` with documenting comment, surfacing the previously-implicit hardcoded-fallback behaviour. |
| **C-27** | `config.yaml` | Marked `execution.commission_pct` and `backtest.commission_pct` as DEAD KNOB with comments pointing to `packages/core/charges.py` as the real cost engine. |

---

## Phase 2 — Operator safety (kill-switch + ORB guards)

| Finding | File | Change |
|---------|------|--------|
| **C-1** | `trading_agent.py` | `_fast_exits_sleep` now polls `_emergency_stop_file_present()` each 15s slice; sets `_running = False` and returns immediately on detection. Operator's `touch logs/STOP` is honoured within one slice instead of one poll_interval (was up to ~75s). |
| **C-2** | `trading_agent.py` | `_on_tick` (WebSocket path) short-circuits when the stop file is present, so the WS thread stops driving `_check_position_exits` (i.e. modify/cancel orders) immediately after STOP. |
| **Bonus** | `trading_agent.py` | Added `_emergency_stop_file_present()` lightweight helper and a one-shot `_emergency_stop_triggered` latch in `_check_emergency_stop` so the heavy alert/flatten path runs at most once even when called from the main loop, the fast-exits slice, AND the WS thread concurrently. |
| **C-11** | `packages/strategies/opening_range_breakout.py` | Flat opening range (`high == low`, e.g. halted / circuit-locked stock at open) now returns HOLD with `reason=flat_opening_range` instead of raising ZeroDivisionError and killing the strategy for the whole scan cycle. |
| **C-12** | `packages/strategies/opening_range_breakout.py` | `range_minutes >= 60` (or any value pushing `minute + range_minutes >= 60`) used to raise ValueError from `dtime(hour, minute + range_minutes)`. Now uses timedelta arithmetic via new `_range_end_time()` helper. |

---

## Phase 3 — Strategy correctness

| Finding | File | Change |
|---------|------|--------|
| **C-8** | `packages/strategies/lstm_model.py` | LSTM SELL branch now returns explicit `stop_loss = price + 1.5*atr`, `take_profit = price - 2.5*atr`, mirroring the BUY branch. Pre-fix the missing SL/TP forced ensemble defaults (1.5%/3%), making short signals asymmetric in R:R vs long signals from the same |prob - 0.5|. |
| **C-9** | `packages/strategies/lstm_model.py` | Added `set_market_context()` so the orchestrator's per-cycle Nifty trend + India VIX push reaches the LSTM feature engine, matching how XGBoost gets it. Without this, LSTM inference used the engine's neutral defaults (0 / 15.0) — silent train/serve skew. |
| **C-10** | `packages/strategies/xgboost_classifier.py` | NaN-bearing warmup row (insufficient history for rolling RSI / MACD / BBANDS) now returns HOLD with `reason=nan_features` + `nan_cols` for the audit log, instead of silently calling `latest.fillna(0)` and feeding zeros (which the model interprets as real bearish feature values, producing spurious high-confidence signals on instruments where features are literally unknown). |
| **C-13** | `packages/strategies/_trend_context.py` | Added `TREND_FILTER_FAIL_CLOSED` env flag. Default unchanged (fail-open: trade through on yfinance outage). Operators running live can flip to fail-closed so a data outage doesn't silently disable the trend filter and admit counter-trend entries. |
| **C-14** | `packages/strategies/_trend_context.py` | `_cache` is now LRU-bounded at `TREND_CACHE_MAX_ENTRIES=2000` (env-tunable). Pre-fix it was unbounded; long battery sweeps across 200+ symbols × dozens of variants leaked memory. |
| **C-30** | `packages/strategies/rsi_momentum.py` | `_compute_rsi` now applies the same flat-window overrides as `FeatureEngine._add_momentum_features` (all-up→100, all-down→0, truly-flat→50). Pre-fix the strategy left RSI = NaN on those cases while the feature pipeline returned explicit values; ensemble votes were inconsistent on degenerate windows. |

---

## Phase 4 — Core / data layer

| Finding | File | Change |
|---------|------|--------|
| **B-6** | `packages/core/data_handler.py` | Added `is_known_holiday_year(year)` helper derived from the hardcoded `NSE_HOLIDAYS` set. `is_market_open()` now WARNs loudly when the current year is outside coverage, so the operator gets a year-end heads-up to extend the calendar. (`NSE_HOLIDAYS` currently covers 2025–2026; first 2027 entry triggers the warning.) |
| **B-7** | `packages/core/execution.py` | Paper-order slippage + partial-fill draws now use a dedicated module RNG (`_paper_rng`) seedable via env (`EXECUTION_PAPER_SEED`) or the `_set_paper_seed(seed)` hook. Pre-fix used the global `random` module so backtests were not reproducible and battery variants couldn't compare apples-to-apples. Live paper unchanged when env unset. |
| **B-9** | `packages/core/data_handler.py` | `_cache` is now FIFO-bounded at `DATA_HANDLER_CACHE_MAX_ENTRIES=256` (env-tunable). Pre-fix the unbounded dict accumulated every (symbol, interval, start, end) tuple ever requested; multi-month daemons / multi-window batteries leaked into GB-scale RSS. |
| **B-10** | `packages/monitoring/alerts.py` | SMTP `server.login()` now uses `email.utils.parseaddr(sender)[1]` to extract the bare email address (or `smtp_user` if explicitly set). Pre-fix would pass a display-formatted `"Trading Agent <agent@host>"` straight to `login()`, which most SMTP servers reject as a 501 syntax error. |
| **B-12** | `packages/core/secrets.py` | `apply_env_to_config` now skips env values that look like the `.env.example` placeholders (start with `YOUR_` etc.). Pre-fix, `cp .env.example .env` without filling in the values would clobber real config secrets with the placeholder strings. Warns when skipping so the operator notices. |
| **B-13** | `packages/core/tick_aggregator.py` + `trading_agent.py` | Added `TickAggregator.cap_history(max_per_symbol)` (takes the new `_lock`). `_periodic_cleanup` now calls this instead of mutating `tick_aggregator._history` directly — fixes both the encapsulation break and the race with the WS thread's `process_tick`. |
| **B-14** | `packages/core/portfolio.py` | `_maybe_persist_trade` now routes exclusively through `Database.store_trade()`. Pre-fix opened its own raw `sqlite3.connect(self._db._db_path)` for a pre-check that was redundant (Database is already idempotent on the (symbol, exit_time) primary key) and that violated Python 3.14's stricter sqlite3-thread rules. |
| **B-15 / C-17** | `packages/core/trade_analyzer.py` | `profit_factor` now caps at 999.99 instead of emitting `float("inf")` for the all-wins case. Strict JSON has no representation for infinity so downstream audit checkpoints + EOD diagnostics export + cloud sync used to crash on serialization. Updated `tests/unit/test_trade_analyzer.py` to match the new sentinel. |
| **B-16** | `packages/core/risk_manager.py` | Default for `require_nifty_above_200ema` flipped from True → False to match the value `config.yaml` ships. Pure default alignment for callers that pass an empty risk block. |
| **C-7** | `packages/brokers/angelone.py` | `get_funds(use_cache=True)` now returns a defensive `dict(self._cached_funds)` copy. Pre-fix, callers that mutated the returned dict corrupted future reads. |
| **C-15** | `packages/core/tick_aggregator.py` | Added `threading.RLock` (`self._lock`) protecting every mutating + reading path (`process_tick`, `flush_all`, `get_candle_history`, `get_current_candle`, `cap_history`). Pre-fix the WS thread's `process_tick` raced with the main thread's `flush_all` and `_periodic_cleanup` — could drop a candle or raise mid-iteration. |
| **C-16** | `packages/core/self_sufficiency.py` | `days_since_deployment` now uses `datetime.now(IST).date()` instead of `date.today()`, matching how `deployed_on` was seeded. Pre-fix, UTC cloud VMs reported off-by-one near IST midnight. |
| **C-18** | `packages/core/historical_cache.py` | Parquet writes now go through a sibling `.tmp.{pid}` file + `os.replace`. Pre-fix, two concurrent battery workers writing the same key could leave a half-written file that subsequent reads failed to parse. |
| **C-21** | `packages/research/backtest_ensemble.py` | `BacktestConfig.paper_seed` field added. `BacktestEnsemble.run()` calls `_set_paper_seed(self.bt.paper_seed)` at the start of every run. Same seed → byte-identical fill ledger across runs. Foundation for apples-to-apples battery variant comparisons (the parity half of C-21). |
| **C-24** | `packages/research/analyze_day.py` | Default `--date` is now IST today (via `pytz`), not host-local. Pre-fix, the EOD post-close run on UTC cloud VMs analysed yesterday between 18:30 UTC and 23:59 UTC. |

---

## Phase 5 — Multi-process persistence

| Finding | File | Change |
|---------|------|--------|
| **NEW** | `packages/core/file_lock.py` | New module: cross-platform advisory file lock (`fcntl` on POSIX, `msvcrt` on Windows). Locks a sibling `.lock` file (NOT the data file) so the lock doesn't block `os.replace` on Windows. |
| **B-8** | `packages/monitoring/alerts.py` | `_record_send` (alert dedup RMW) now wraps its read-modify-write in `file_lock(path, timeout=2.0)`. On lock timeout, falls through without dedup — preferring an extra email over a missed alert. |
| **C-29** | `packages/core/cooldown_persistence.py`, `runtime_state_persistence.py`, `trailing_stop_persistence.py` | All three `save_*` paths now wrap `_atomic_write_json` in the file lock so overlapping daemon restarts (the canonical race window) can't lose protective state via lost-update. |

---

## Phase 6 — Training pipeline (model frozen; no immediate effect)

| Finding | File | Change |
|---------|------|--------|
| **C-22** | `packages/training/train_lstm.py` | Added documentation block clarifying that `shuffle=True` operates on already-assembled sequence-shaped samples (each row of `X_train_t` is `(seq_len, features)` from `create_sequences()`) — NOT within-sequence timesteps. Sample-level shuffling is the correct ML practice. C-22 was reclassified as a documentation issue (no code change). |
| **C-23** | `packages/training/train_xgboost.py` | Calibration now fits on `X_test[:half]` and evaluates Brier / LogLoss / AUC on the held-out `X_test[half:]` slice, with a same-slice raw baseline for the AUC sanity check. Pre-fix the calibrator fit + the calibrator eval ran on the same `X_test`, so Brier was in-sample for the calibrator. Effective on next retrain only; current frozen model artifact unchanged. |

---

## Phase 7 — Tools / security / hygiene

| Finding | File | Change |
|---------|------|--------|
| **B-3 residual / B-18** | `packages/brokers/angelone.py` | `_diagnose`'s IP-whitelist hint now consults `TRADER_DISABLE_SSL_VERIFY` env flag, mirroring the rest of the codebase. Default (env unset) = full SSL verification. |
| **B-19** | `packages/strategies/lstm_model.py` + `packages/strategies/xgboost_classifier.py` | Every `torch.load` / `pickle.load` now logs an absolute path at `[security]` level so the audit trail can verify model file provenance. (Migrating to `weights_only=True` is tracked separately; needs a retrain that uses `state_dict()` + reconstruct.) |
| **C-25** | `packages/core/event_calendar.py` | `is_blackout` now counts trading days (excluding weekends + NSE holidays) via new `_trading_days_between` helper. Pre-fix used calendar days, so e.g. `blackout_days_before=1` + Monday event effectively only blocked the event day (Sunday isn't tradeable). Now correctly blocks the previous trading session (Friday). |
| **C-6 (timeout half)** | `packages/brokers/angelone.py` | `place_order` now runs the SmartConnect call in a worker thread + `join(timeout=ANGELONE_PLACE_ORDER_TIMEOUT_SEC)` (default 15s, env-tunable). A wedged broker socket can no longer stall the scan cycle. NOTE: the idempotency-tag half of C-6 is deferred; in the wedged-thread case the broker may still place the order in the background — operator runbook to add a reconcile-on-next-cycle step. |
| **B-21 (narrow)** | `tools/run_battery_queue.py`, `packages/research/backtest_ensemble.py`, `packages/brokers/angelone.py` | Removed local unused imports + fixed one `F541 f-string without placeholders`. The full project-wide B-21 cleanup (~200 lint nits across files I didn't otherwise touch) is left as a separate hygiene PR per the "no new issues" guardrail. |

---

## Phase 8 — Regression tests

New file: `tests/unit/test_audit_2026_05_26_fixes.py` — **35 tests**, one per
finding above. Each test maps 1:1 to a finding so a future reviewer can see
at a glance which audit item it guards.

**Full suite:** `1556 / 1556 passing` (1521 pre-existing + 35 new), run in
`82s` on a clean run.

**Lint:** `ruff check --select F821,B904` clean across all touched files.

---

## Slot accounting

Per the strict reading of `FREEZE_v2.1.md` §Bypass discipline, this sweep
consumes **slot #2 of 3**. The argument for behaviour-preservation on the
happy path (and therefore reclassifying to audit-only, mirroring the
2026-05-25 audit-quick-wins precedent on B-1/B-3/B-4/B-5/B-11) is recorded
in §Slot #2 rationale inside `FREEZE_v2.1.md` so a future reviewer can
make that call as a one-line edit, not a fresh investigation.

**Remaining slots: 1.** Reserved for either the C-5 broker cutover or the
B-2 / C-6 idempotency work, both of which should land before
freeze-lift on 2026-06-08.

---

## Deployment

**Not deployed in this session.** Operator decision. Natural cutover
window: tomorrow's 09:15 IST open, because today's daily kill switch is
already tripped (per `docs/eod_report_2026-05-26.md`).

Pre-deploy checklist (operator):
1. `git status` → should show only the files listed above + this doc.
2. `python -m pytest tests/ -q` → 1556 passing (verified locally).
3. `git diff --stat` → sanity check the changeset size.
4. Tag the commit: `git tag freeze-v2.1-slot2-audit-sweep` (so the
   rollback target is obvious).
5. `docker compose down && git pull && docker compose up -d --build`.
6. Watch `logs/trading_agent_2026-05-27.log` for the 09:15 IST first
   scan; verify no `[security]` warnings (sign that model file paths
   are absolute as expected).

Rollback: `git reset --hard <pre-sweep-sha> && docker compose up -d --build`.
The new file `packages/core/file_lock.py` will be a stale module after
rollback but causes no error (it's imported lazily inside try-except).
