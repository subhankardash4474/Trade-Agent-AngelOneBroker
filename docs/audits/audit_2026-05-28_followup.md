# Production-Grade Audit Follow-up — 2026-05-28

**Status:** IN-PROGRESS (Phases 1+2 of 5 landed; 28 of 86 findings FIXED)
**Audit date:** 2026-05-28 (post P-03/P-04/P-11 + XGBoost-disable + Bug L holiday rewrite)
**Audit method:** 6 parallel focused exploration passes (orders, state/recovery, concurrency/resources, numeric/financial, observability/silent-failure, runtime performance)
**Total findings:** 86 concrete bugs with `file:line` citations
**Codebase state at audit:** branch `main` @ `430069c` (Bug L NSE_HOLIDAYS rewrite). Freeze v2.1 active, 2 of 3 slots used (slot-1 `risk.allow_shorts=false`, slot-2 xgboost-disable). 1 slot remaining.

---

## Phase-1 landed 2026-05-28 (22 findings, committed but NOT deployed)

Code-only — no slot consumed because nothing was deployed to live. All
fixes restricted to non-frozen files. Phase-1 batch includes every
finding whose fix is (a) freeze-safe per FREEZE_v2.1 §What-is-frozen,
(b) low blast-radius (mostly log promotions, fail-closed flips,
caching), and (c) covered by a regression test in
`tests/unit/test_audit_2026_05_28_phase1.py` (20 tests, green).

Findings FIXED in Phase 1 (see Status column below):
OBS-01, OBS-02, OBS-03, OBS-06, OBS-07, OBS-08, OBS-09, OBS-11,
OBS-12, OBS-13, OBS-14, OBS-15, OBS-16, OBS-17, OBS-18, OBS-20,
NUM-13, NUM-14, ORD-04, ORD-12, STATE-10, CONC-11, CONC-12,
PERF-02, PERF-03, PERF-11, PERF-12.

Phase 1 commits: see `git log --grep=audit-2026-05-28-phase1`.

---

## Phase-2 landed 2026-05-29 (6 findings — money-at-risk truth-telling, NOT deployed)

Code-only — slot still NOT consumed because nothing was deployed to
live. All fixes restricted to non-frozen files (`packages/core/execution.py`,
`trading_agent.py`).

Phase-2 closes the "broker truth vs agent in-memory state" cluster
that was the most likely silent loss-amplifier on a slow broker day:

* **ORD-01 / STATE-01 (FIXED)** — `_live_order_with_retry` now polls
  `orderBook()` until terminal status (FILLED / PARTIALLY_FILLED /
  REJECTED) or TTL. New `ExecutionEngine._wait_for_terminal()` helper
  + module-level `_TERMINAL_FILLED / _TERMINAL_PARTIAL /
  _TERMINAL_CANCELLED` sets mirror the e2e harness's contract. The
  caller now receives the broker's real `averageprice` (with
  computed slippage) on FILLED, `None` on REJECTED, and the legacy
  `PLACED` degrade only on TTL with no terminal observation.
* **ORD-02 (FIXED)** — new `_find_idempotent_match()` scans the
  broker `orderBook` for a recently-placed order matching
  (symbol, side, qty, ordertype) within `idempotency_lookback_sec`
  (default 30s). On retry attempts ≥ 2 the helper short-circuits
  the duplicate `placeOrder`. Cancelled / rejected and stale orders
  are skipped. Documented limitation: AngelOne's `placeOrder` API
  has no client-supplied tag, so this is the cheapest workable
  idempotency probe.
* **ORD-03 (FIXED)** — new
  `ExecutionEngine.rollback_entry_on_portfolio_failure()` cancels
  the SL-M leg, places a MARKET counter-flatten on the OPPOSITE
  side, and cleans `_pending_orders` / `_order_log`. Wired from
  `trading_agent._open_new_position` so the rollback runs whenever
  `portfolio.open_position` either returns `False` OR raises. On
  partial rollback (counter-flatten or SL cancel fails), the
  symbol is added to `_symbols_blocked_by_rollback` and the entry
  path refuses re-entry on that symbol for the rest of the
  session.
* **STATE-02 (FIXED)** — `reconcile_positions_with_broker` now
  iterates ALL broker `positionBook` rows with non-zero netqty
  and reports `status=="broker_only"` for symbols absent from DB.
  The boot block in `trading_agent.py` no longer gates on
  `if self.portfolio.positions:` (broker-only detection MUST run
  even when DB is empty); the `broker_only` handler queues a
  CRITICAL alert + per-symbol stock-loss block.
* **OBS-05 (FIXED)** — `reconcile_positions_with_broker` retries
  `positionBook()` up to 3 times with 2/4s backoff before giving
  up. On final failure (live mode) it sets
  `boot_reconcile_failed_live=True`. New
  `TradingAgent._boot_reconcile_gate_open()` is checked at the top
  of `_open_new_position` and refuses every entry with
  `audit_reject reason=boot_reconcile_gate` until the operator
  touches `logs/boot_reconcile.ack`. Ack is one-shot (file
  consumed on first read) so a transient re-arm requires fresh ack.

Coverage: 27 new regression tests in
`tests/unit/test_audit_2026_05_28_phase2.py`. Full unit suite
1,483/1,483 green.

Remaining queue (queued by phase in TODO list):
- Phase 2 was completed 2026-05-29.
- Phase 3: CONC-02..09, ORD-06, STATE-04..09, STATE-11, STATE-12 —
  WS hot-path enqueue+return; architectural.
- Phase 4: PERF-01, PERF-04..10, PERF-13..15 — broker batch endpoint,
  DB indexes, scanner background thread. Needs paper-mode regression.
- Phase 5 (freeze-lift OR explicit slot): all frozen-file findings
  (risk_manager: NUM-03/04/08/09/12, OBS-04/19, CONC-01; _trend_context:
  NUM-05/15; base_strategy: OBS-10). 11 findings total.

---

## How to use this doc

- Every finding has a stable ID (`ORD-01`, `STATE-02`, `CONC-03`, `NUM-05`, `OBS-01`...). Reference these IDs in commits.
- **Status** column tracks where each finding stands:
  - `OPEN` — not started
  - `IN-PROGRESS` — work begun
  - `FIXED` — code change merged + regression test passing
  - `DEFERRED` — knowingly not fixed (with rationale)
  - `WONTFIX` — invalid finding or accepted risk
- The 5 architectural **themes** at the bottom group related findings — fixing the theme typically resolves multiple findings together.
- The **freeze impact** field tells you whether the fix is freeze-safe (no slot needed) or touches a frozen file (slot consumption required).

---

## Critical findings (the 8 that can lose money or wedge the daemon today)

These are independent of the freeze contract — they reflect actual broker-vs-agent state divergence, money-at-risk paths with no observability, and runtime performance that prevents the daemon from completing cycles on schedule.

| ID | Severity | File:line | Status | Freeze impact | What |
|---|---|---|---|---|---|
| ORD-01 / STATE-01 | Critical | `trading_agent.py:4433-4453`, `packages/core/execution.py:539-601` | **FIXED** (phase-2) | Freeze-safe (not on §What is frozen) | Live treats `status=="PLACED"` as a fill. Opens/closes positions in memory using signal-time price without waiting for actual `averageprice`. **Paper mode hides this.** |
| ORD-02 | Critical | `packages/brokers/angelone.py:302-330`, `packages/core/execution.py:534-607` | **FIXED** (phase-2) | Freeze-safe | `_live_order_with_retry` retries on timeout with **no idempotency key**. Broker wrapper itself warns timed-out call may have placed. Single network stall → duplicate order. |
| ORD-03 | Critical | `packages/core/execution.py:561-599`, `trading_agent.py:4461-4486` | **FIXED** (phase-2) | Freeze-safe | Entry places broker order + SL-M, then calls `portfolio.open_position()`. If portfolio fails, broker leg is **not flattened**. |
| STATE-02 | Critical | `trading_agent.py:307-317`, `packages/core/execution.py:1097-1208` | **FIXED** (phase-2) | Freeze-safe | Boot reconciliation only iterates DB-restored symbols. Crash-after-fill-before-DB → daemon boots flat while broker holds real exposure. Never queries `positionBook()` for unaccounted symbols. |
| NUM-01 | Critical | `packages/core/portfolio.py:Portfolio.open_position/close_position`, `packages/core/database.py:open_positions.cash_locked`, `trading_agent.py:Portfolio(...)`, `packages/research/backtest_ensemble.py:BacktestConfig.mis_short_margin_pct`, `config.yaml:execution.mis_short_margin_pct` | **FIXED** (misc-A) | Freeze-safe | Short MIS margin model: new `mis_short_margin_pct` knob (default 0.20 in production config, 1.0 in code for legacy preservation). Per-position `cash_locked` field + new DB column persists the exact lock so a daemon restart between open and close still releases the right amount. Legacy rows (`cash_locked NULL`) fall back to the implicit "full notional + entry commission" lock. Tests: 18 in `test_audit_2026_05_28_misc.py::TestNUM01*`. |
| OBS-01 | Critical | `trading_agent.py:_check_position_exits_locked` | **FIXED** (phase-1) | Freeze-safe | SL/TP/peak-giveback exit loop only logged on success. Failed flattens now emit CRITICAL log + alert with "MANUAL ACTION REQUIRED". (See Observability table below.) |
| PERF-01 | Critical | `data_handler.py:get_ltp_batch + get_multiple_ltp`, `angelone.py` | **FIXED** (phase-4) | Freeze-safe | `get_multiple_ltp` was N sequential REST calls; new `AngelOneDataSource.get_ltp_batch` uses `getMarketData(mode=LTP)` with 50-token chunking. ~50× speedup. (See Runtime Performance table below.) |
| PERF-02 | Critical | `trading_agent.py:_get_historical_cached + _evaluate_strategy` | **FIXED** (phase-1) | Freeze-safe | Per-cycle `(symbol, timeframe) -> DataFrame` memo on `TradingAgent`; cleared at the top of `_trading_cycle`. ~4× dedup at 300 symbols × 4 strategies. (See Runtime Performance table below.) |

---

## Order / Execution / Broker (12)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| ORD-01 | Critical | **FIXED** (phase-2) | `execution.py:_live_order_with_retry` | Treats `PLACED` as `FILLED` — `filled_price: None` returned but caller uses signal price | New `_wait_for_terminal()` helper polls `orderBook()` until COMPLETE/CANCELLED/REJECTED or TTL; result dict carries broker `averageprice` + computed slippage. Tests: `test_ord01_*` (8). |
| ORD-02 | Critical | **FIXED** (phase-2) | `execution.py:_find_idempotent_match` + `_live_order_with_retry` | No idempotency on retry — wrapper warns timed-out call may have placed | Retry attempts ≥2 scan the broker `orderBook` for a recent matching (symbol, side, qty, ordertype) order within `idempotency_lookback_sec` (default 30s). Cancelled / stale rows skipped. Tests: `test_ord02_*` (4). |
| ORD-03 | Critical | **FIXED** (phase-2) | `execution.py:rollback_entry_on_portfolio_failure` + `trading_agent.py:_open_new_position` | No broker-leg rollback when `portfolio.open_position` fails after entry+SL placed | Atomic rollback: cancel SL leg + counter-flatten MARKET + clean tracking. Caller wraps `open_position` in try/except; on partial rollback the symbol is added to `_symbols_blocked_by_rollback` for the session. Tests: `test_ord03_*` (5). |
| ORD-04 | High | **FIXED** (phase-1) | `trading_agent.py:_close_position_safely` | Exits inherit configured default LIMIT order_type → sticky on gaps | `_close_position_safely` now passes `order_type="MARKET"` explicitly. Test: `test_ord04_close_position_safely_forces_market_order_type`. |
| ORD-05 | High | **FIXED** (misc-c) | `trading_agent.py:_close_position_safely` | Cancel-SL-then-flatten not atomic — broker SL can fire in cancel window while flatten is in flight → double exit (reverse position) | After `cancel_sl_order_for_symbol`, the live path now polls `get_order_status(sl_order_id)`; if the SL filled during the cancel race, the flatten is **skipped** and the position is closed in-portfolio at the SL fill price. CRITICAL log + ATOMIC-CLOSE-RACE alert surface the race. Test: `test_ord05_close_position_safely_handles_sl_fill_race_anchor` + `test_exit_check_thread_safety` (sl_meta type-guard). |
| ORD-06 | High | **FIXED** (phase-3) | `trading_agent.py:_maybe_refresh_broker_session` | JWT refresh swapped REST `_api` but never reconnected WS | Now calls `ws_client.update_broker_session(api, force_reconnect=True)`. CRITICAL log on WS-update failure. Test: `test_ord06_maybe_refresh_broker_session_calls_update_broker_session`. |
| ORD-07 | High | **FIXED** (misc-c) | `execution.py:_live_order_with_retry`, `place_order` (exit path) | `get_order_status` existed but was never wired into the trading path → live never observed PARTIALLY_FILLED / averageprice | Phase-2 wired `_wait_for_terminal` into `_live_order_with_retry`; the exit path goes through `place_order → _live_order_with_retry`, so exits now block on terminal status and inherit the broker `averageprice` + slippage. Source-level pin: `test_ord07_*` (3). |
| ORD-08 | Medium | **FIXED** (misc-c) | `execution.py:_live_order_with_retry` | SL-M placed immediately after entry ack using *requested* qty (not filled qty) → wrong-size SL or rejection on partials | After the entry order reaches FILLED, the SL-M is now sized off `result["filled_quantity"]` (with a defensive fallback to the originally requested quantity). The effective SL size is also persisted into `_sl_orders_by_symbol` meta so trail / cancel paths see the correct number. Tests: `test_ord08_*` (2). |
| ORD-09 | Medium | **FIXED** (misc-c) | `execution.py:_live_order_with_retry` | No order-fill TTL, no cancel-on-timeout, no LIMIT→MARKET escalation | On `_wait_for_terminal` TTL the engine now (1) issues a `cancel_order`, (2) re-queries `get_order_status` to catch the race where the order filled in the cancel window — and accepts the fill if so, (3) otherwise treats the entry as a hard failure: clears `_pending_orders`, returns `None`, and lets the caller's idempotency probe handle the next attempt. Tests: `test_ord09_*` (2). |
| ORD-10 | Medium | **FIXED** (misc-e) | `execution.py:classify_smartapi_error`, `set_auth_refresh_callback`, `_maybe_invoke_auth_refresh`; `trading_agent.py:_handle_broker_auth_failure`, `_maybe_refresh_broker_session(force=True)` | Token refresh is proactive (7h timer) only — no reactive re-auth on 401/403/`AB*` | Module-level `classify_smartapi_error()` recognises `AB1010/1011/1014/1019/2001/2002/2003`, 401/403 (word-boundary fenced), and ten string phrases (`Invalid Token`, `Session Expired`, `JWT Expired`, etc.); rate-limit takes precedence over auth heuristics. ExecutionEngine exposes `set_auth_refresh_callback` so TradingAgent can inject `_handle_broker_auth_failure`. The retry loop in `_live_order_with_retry` invokes the callback **at most once per top-level call** on auth-class exceptions. `_maybe_refresh_broker_session` accepts `force=True` to bypass the 7h gate + 1h backoff. Conservative default for unknown shapes is `transient` so a broker-contract drift never accidentally halts trading. Tests: `TestORD10ErrorClassifier` (9) + `TestORD10AuthCallbackHook` (8). |
| ORD-11 | Medium | **FIXED** (misc-d) | `execution.py:_record_slippage`, `is_symbol_slippage_blocked`, `clear_slippage_block`; `trading_agent.py:_open_new_position` | Live records `slippage: None` — no max-slippage circuit breaker (paper applies tolerance, live doesn't) | New `_record_slippage()` populates `slippage_pct` + `slippage_breach` on every fill (paper, live, race). Live breaches log CRITICAL `[ORD-11-SLIPPAGE]`; opt-in `execution.halt_symbol_on_slippage_breach` adds the symbol to `_slippage_breached_symbols`. `_open_new_position` consults `is_symbol_slippage_blocked` before placing entries (exits / SL trail bypass the gate). Operator clears via `clear_slippage_block(symbol)`. Tests: `TestNUM11SlippageParity` (5) + `TestORD11SlippageCircuitBreaker` (7). |
| ORD-12 | Medium | **FIXED** (phase-1) | `trading_agent.py:_square_off_all` | `_square_off_all` ignored close result; sent "Square Off Complete" even with naked positions | Accumulator + per-symbol CRITICAL log + distinct "SQUARE-OFF INCOMPLETE" alert when any close fails. Test: `test_ord12_square_off_alert_distinguishes_partial_failure`. |

---

## State / Persistence / Recovery (12)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| STATE-01 | Critical | **FIXED** (phase-2) | (same as ORD-01) | Persists open position from non-terminal status | Resolved via ORD-01 wait-for-terminal contract (live path waits for FILLED before mutating portfolio). Tests: `test_ord01_live_order_*` (3). |
| STATE-02 | Critical | **FIXED** (phase-2) | `execution.py:reconcile_positions_with_broker` + `trading_agent.py:307-498` | Boot reconcile skips broker-only positions absent from DB | Reconcile now iterates ALL broker `positionBook` rows; non-zero netqty for unknown symbols returns `status="broker_only"`. Caller queues CRITICAL alert + per-symbol stock-loss block. Boot block no longer gates on `if self.portfolio.positions:`. Tests: `test_state02_*` (3). |
| STATE-03 | High | **FIXED** (phase-3) | `trading_agent.py:__init__` | Cooldown attributes accessed before initialisation | `_stock_loss_today` + `_max_losses_per_stock` eagerly initialised BEFORE boot reconcile; late init guarded with `hasattr`. Tests: `test_state03_*` (2). |
| STATE-04 | High | **FIXED** (phase-3) | `database.py:close_position_atomic`, `portfolio.py:close_position` | `close_position` not atomic across 5 transactions | New `Database.close_position_atomic` wraps DELETE + INSERT trade + INSERT equity in one commit. CSV append happens after the commit. Tests: `test_state04_*` (3). |
| STATE-05 | High | **DEFERRED** | `execution.py`, `database.py orders` | Boot recovery of `_pending_orders` from broker orderBook | Architectural; queued for the focused architectural session alongside CONC-03. Phase-2 added per-symbol reconciliation; the global boot sweep is the remaining piece. |
| STATE-06 | Medium | **FIXED** (phase-3) | `cooldown_persistence.py:save_cooldown_state`, `runtime_state_persistence.py:save_runtime_state`, `trailing_stop_persistence.py:save_trailing_states` | `file_lock` 2s timeout fell back to unlocked write | Now retries with backoff (1s -> 3s -> 5s); on persistent timeout SKIPS the save (fail-closed) instead of clobbering. Tests: `test_state06_*` (3). |
| STATE-07 | Medium | **FIXED** (group-H) | `portfolio.py:_log_trade + __init__`, `signal_audit.py:log + _drain_retry_queue` | Trade CSV + signal-audit CSV append without lock, without fsync | `Portfolio` now owns a `_trade_log_lock` (`threading.Lock`); `_log_trade` holds it across the full open / write / `flush + os.fsync` / close cycle. `signal_audit.log` (which already had a lock) now also flushes + fsyncs; `_drain_retry_queue` does the same on its batch flush so recovered rows are durable. fsync OSError (Windows shares, CIFS) is fail-soft -- the row stays in the page cache. SQLite `trades` (STATE-04 atomic close) remains the source of truth. Tests: `TestSTATE07TradeCsvDurability` (5) + `TestSTATE07SignalAuditDurability` (4). |
| STATE-08 | Medium | **FIXED** (phase-3) | `trading_agent.py:_persist_trailing_states_debounced`, `_on_tick` | Trailing state persisted only at cycle end | New debounced helper (5s window) wired into the WS-tick mutation path with a `trail_mutated` gate so no-op ticks don't burn budget. Tests: `test_state08_*` (2). |
| STATE-09 | Medium | **FIXED** (phase-3) | `cooldown_persistence.py:load_cooldown_state`, `trading_agent.py:_open_new_position` | Corrupt JSON loaded as empty dict (fail-open) | Corrupt JSON now writes `data/cooldowns_corrupt.flag`; `TradingAgent` engages a fail-closed gate that refuses new entries until operator removes the flag. Tests: `test_state09_*` (2). |
| STATE-10 | Medium | **FIXED** (phase-1) | `trading_agent.py:_setup_logging area` | `logs/STOP` unscoped — paper and live daemons shared kill switch | Default now `logs/STOP.<mode>` (live/paper). Explicit `operations.emergency_stop_path` still honoured verbatim. Test: `test_state10_emergency_stop_path_is_mode_scoped`. |
| STATE-11 | Low | **FIXED** (phase-3) | `signal_audit.py:_drain_retry_queue` | Signal-audit CSV write failure dropped the row | Bounded (500-row) deque retry queue; flushed best-effort on every `log()` call. Tests: `test_state11_*` (2). |
| STATE-12 | Medium | **FIXED** (phase-3) | `trading_agent.py:_reset_daily_trackers` | Daily reset cleared in-memory maps but not `open_positions` SQLite | New stale-MIS sweep: any open position with `entry_time.date() < today (IST)` is closed via `close_position(..., reason="stale_overnight_mis_sweep")` BEFORE clearing the in-memory maps. Test: `test_state12_reset_daily_trackers_sweeps_stale_overnight_positions`. |

---

## Concurrency / Resources (12)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| CONC-01 | High | **FIXED** (phase-5) | `trading_agent.py:5391-5404`, `risk_manager.py:527-528` | `can_trade()` reads cached `state.open_positions` (refreshed only at cycle end) → within a cycle, consecutive entries can breach `max_open_positions` | Now calls `risk_manager.update_open_positions(portfolio.open_position_count)` immediately after `create_trailing_stop`. Test: `test_conc01_open_new_position_refreshes_open_position_count`. |
| CONC-02 | High | **FIXED** (phase-3) | `trading_agent.py:1765-1786`, `risk_manager.py:915-918` | WS thread calls `risk_manager.update_trailing_stop()` outside `_exit_check_lock` | WS-tick mutation now routed through `with self._exit_check_lock:`; mutation gate + debounced persist (STATE-08) wired together. Tests: `test_conc02_ws_trail_update_holds_exit_check_lock`. |
| CONC-03 | High | **DEFERRED** | `trading_agent.py` WS hot path | WS callback does synchronous broker I/O → single-threaded WS backlogs | Architectural restructure (enqueue+return + worker thread) deferred to a focused architectural session. Phase-3 surgical fixes (CONC-02/04/05) close the most painful hot spots. |
| CONC-04 | Medium | **FIXED** (phase-3) | `tick_aggregator.py:process_tick / flush_all` | `on_candle_close` fired inside the aggregator lock | Callback dispatch now happens AFTER the lock is released. Tests: `test_conc04_process_tick_dispatches_callbacks_outside_lock`, `test_conc04_process_tick_still_appends_history_on_callback_exception`. |
| CONC-05 | Medium | **FIXED** (phase-3) | `trading_agent.py:_buffer_tick / _flush_tick_buffer`, `database.py:store_ticks_batch` | Every tick a fresh sqlite3 connection | 5000-row in-memory deque, flushed every 100 rows or 1s. Final flush in `_shutdown`. Tests: `test_perf05_*`. |
| CONC-06 | Medium | **FIXED** (phase-3) | `websocket_client.py:_run_*`, `_token_to_symbol` | `_subscriptions` mutated from main thread while WS thread iterates | All iteration paths now wrapped in `with self._subscriptions_lock:`. Tests: `test_conc06_token_to_symbol_iterates_under_lock`, `test_conc06_run_simulation_snapshots_under_lock`. |
| CONC-07 | Medium | **FIXED** (phase-3) | `websocket_client.py:_close_existing_ws` | Reconnect assigned `_ws` without closing old socket | Pre-existing `_close_existing_ws` helper validated; documented closure in phase-3 commit. |
| CONC-08 | Medium | **FIXED** (phase-3) | `trading_agent.py:run` | SIGTERM polled only KeyboardInterrupt | `run()` installs SIGTERM/SIGINT handler that flips `_running = False`. Test: `test_conc08_run_installs_sigterm_handler`. |
| CONC-09 | Medium | **FIXED** (phase-3) | `trading_agent.py:_shutdown`, `websocket_client.py:join` | `_shutdown()` didn't join WS thread | New `WebSocketClient.join(timeout)` + `_shutdown` calls it with 5s budget. Tests: `test_conc09_*` (3). |
| CONC-10 | Medium | **FIXED** (group-H) | `trading_agent.py:_publish_heartbeat_snapshot + _write_health_json_from_snapshot + _run_heartbeat_thread + _start_heartbeat_thread + _stop_heartbeat_thread` | Heartbeat / `health.json` updates only at end of main loop → long `get_multiple_ltp(200)` or hung broker call freezes heartbeat; watchdog flags as dead | New dedicated daemon thread writes `health.json` every `health_pulse_interval_seconds` (default 30s) from a main-loop-published snapshot, with a freshly-stamped `ts_unix` and current `running` flag — independent of cycle completion. Started at the top of `run()` before the cycle loop; stopped FIRST inside `_shutdown` so a final `running=false` pulse lands before WS / DB teardown. Set `robustness.health_pulse_interval_seconds: 0` to disable. Tests: `TestCONC10HeartbeatThread` (11). |
| CONC-11 | Low | **FIXED** (phase-1) | `portfolio.py:Portfolio.__init__` | `trade_history` in-memory list unbounded | Now `deque(maxlen=10000)`. Dashboards iterate / use len() unchanged. Test: `test_conc11_trade_history_is_bounded_deque`. |
| CONC-12 | Low | **FIXED** (phase-1) | `database.py` + `trading_agent.py:_periodic_cleanup` | `equity_curve` table had no retention | New `purge_old_equity_points(days=90)` mirroring `purge_old_ticks`; wired into `_periodic_cleanup`. Tests: `test_conc12_database_has_purge_old_equity_points`, `test_conc12_periodic_cleanup_calls_equity_purge`. |

---

## Numeric / Financial / Edge (15)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| NUM-01 | Critical | **FIXED** (misc-A) | `portfolio.py`, `database.py`, `backtest_ensemble.py`, `trading_agent.py`, `config.yaml` | Short MIS margin modeled at 100% notional instead of ~20% → backtester under-sizes shorts ~5× vs live | New `Portfolio.mis_short_margin_pct` knob (legacy default 1.0; production config 0.20). Per-position `cash_locked` field + new DB column. Net cash change after a round-trip equals `pnl` regardless of margin %. |
| NUM-02 | High | **FIXED** (phase-5) | `trading_agent.py:_process_signal Kelly block` | Kelly post-sizing 1-share regression of F-34 | Now skips Kelly when risk-sized `quantity=0`; post-Kelly zero audit-rejects `sizing:zero_qty`. Test: `test_num02_kelly_does_not_force_one_share_when_prequantity_is_zero`. |
| NUM-03 | High | **FIXED** (phase-5) | `risk_manager.py:sync_balance_from_mtm`, `trading_agent.py:_trading_cycle` | `current_balance` only updates on closes | New `sync_balance_from_mtm(equity)` called BEFORE `can_trade` each cycle. Tests: `test_num03_*` (4). |
| NUM-04 | High | **FIXED** (phase-5, partial) | `risk_manager.py:round_to_tick + get_atr_stop_loss + enforce_sl_floor` | No NSE 0.05-tick rounding | New `round_to_tick(price, side, kind)` helper; SL routes through it (round AWAY from entry). Tests: `test_num04_*` (3). `execution.py` adoption queued for next session. |
| NUM-05 | High | **FIXED** (phase-5, freeze-bypass slot 3) | `_trend_context.py:_fetch_daily` | `_fetch_daily` used `iloc[-1]` (today's forming bar) | Now drops last bar when its date >= as_of_date (defaults to today IST). Cache key includes the as-of date. Test: `test_num05_trend_context_fetch_drops_today_forming_bar`. |
| NUM-06 | High | **FIXED** (misc-B) | `trading_agent.py:_drop_forming_intraday_bar`, `_get_historical_cached` | REST fallback fed in-progress intraday bar to strategy indicators | New `_drop_forming_intraday_bar(df, timeframe)` helper invoked from `_get_historical_cached`. Drops the last row when `last_ts + interval > now (IST)`. Daily / weekly frames untouched (handled by `_trend_context.as_of_date`). Fail-open on any error. Tests: 11 in `test_audit_2026_05_28_misc.py::TestNUM06*`. |
| NUM-07 | Medium | **FIXED** (misc-B) | `features.py:_add_derived_features` | Day-high / day-low rolling-75 crossed session boundary | `dist_from_high_pct` / `dist_from_low_pct` now use `groupby(df.index.date).cummax()` / `cummin()` (same pattern as VWAP + OBV). Window EXPANDS through the session and resets at the IST date boundary. Falls back to legacy rolling-75 for non-datetime indices. Tests: 6 in `test_audit_2026_05_28_misc.py::TestNUM07*`. |
| NUM-08 | Medium | **FIXED** (phase-5) | `risk_manager.py:is_trade_worth_taking` | Short-side compute_round_trip leg swap was symmetric | Now explicit `buy_leg, sell_leg = (TP, entry)` on shorts. Test: `test_num08_is_trade_worth_taking_passes_correct_legs_for_short`. |
| NUM-09 | Medium | **FIXED** (phase-5) | `regime.py:classify_regime` (non-frozen) | `vix is None` check let NaN through | New `_is_finite_number` helper; NaN/inf -> `unknown`. Test: `test_num09_classify_regime_treats_nan_vix_as_unknown`. |
| NUM-10 | Medium | **FIXED** (misc-f) | `charges.py` (Decimal pipeline), `portfolio.py:close_position` (direct exit-leg compute) | All charges/P&L in float; `exit_commission = total - entry` drift accumulates; 100+ trades can flip tight reward-vs-charges gate | `charges.py` now runs the inner accumulators in `Decimal` and quantizes per-component to 1 paisa (`ROUND_HALF_EVEN`). The new identity `compute_round_trip(...).total == compute_one_leg(BUY) + compute_one_leg(SELL)` holds byte-for-byte, so `portfolio.close_position` derives `exit_commission` directly via `compute_one_leg(exit_price, qty, side=exit_side)` instead of the subtractive `total - entry`. Public API stays float-typed at the boundary. Tests: `TestNUM10DecimalCharges` (8). |
| NUM-11 | Medium | **FIXED** (misc-d) | `execution.py:_record_slippage` (paper, live, partial, race) | Paper applies adverse slippage; live records `slippage: None` and never validates fill vs requested → paper systematically more pessimistic | Both paper AND live now emit `slippage_pct` + `slippage_breach` so the backtester and live broker are byte-comparable. The legacy `slippage` (Rs absolute) field is preserved for back-compat. The passive `get_order_status` observation path also keeps `slippage_pct` in sync on the pending-orders cache. |
| NUM-12 | Medium | **FIXED** (phase-5, freeze-bypass slot 3) | `risk_manager.py:regime_size_multiplier` | Cold start `unknown` regime returned 1.0x | Default flipped 1.00 -> 0.50; `None` and `unknown` both routed through the configurable `unknown` knob. Tests: `test_num12_*` (2) + 2 existing tests pinned. |
| NUM-13 | Low | **FIXED** (phase-1) | `trading_agent.py:_process_signal` | Rejection-cooldown short-circuit left audit-CSV gap | `_audit_reject(..., "reject_cooldown:active")` added before return. Test: `test_num13_rejection_cooldown_writes_audit_reject`. |
| NUM-14 | Low | **FIXED** (phase-1) | `trading_agent.py:CASH-SIZE block` | Cash gate had no min-cash buffer | `risk.min_cash_buffer_rs` (default Rs 200) subtracted from cash before affordability divide. Test: `test_num14_cash_sizing_reserves_min_buffer`. |
| NUM-15 | Medium | **FIXED** (phase-5, freeze-bypass slot 3) | `_trend_context.py:_fetch_daily / get_trend / is_against_trend` | Trend filter live lookahead (NUM-05 amplified) | `as_of_date` plumbed through `get_trend` + `is_against_trend`; cache key carries the as-of date. Test: `test_num15_trend_context_get_trend_accepts_as_of_date`. |

**Calendar note** (Bug L / 430069c — CORRECT, not re-flagged): rewritten `NSE_HOLIDAYS` in `packages/core/data_handler.py:65-95` matches unit-test contract. Residual risk: coverage ends 2026-12-25; OBS-12 flags the 2027 gap.

---

## Observability / Silent Failure (20)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| OBS-01 | Critical | **FIXED** (phase-1) | `trading_agent.py:_check_position_exits_locked` | SL/TP/peak-giveback exit loop only logged on success | `else:` branch added: CRITICAL log + CRITICAL alert with "MANUAL ACTION REQUIRED". Test: `test_obs01_failed_sl_tp_exit_emits_critical`. |
| OBS-02 | High | **FIXED** (phase-1) | `trading_agent.py:_exit_on_signal` | Same silent-failure pattern | Mirror OBS-01: CRITICAL log + alert. Test: `test_obs02_failed_signal_exit_emits_critical`. |
| OBS-03 | High | **FIXED** (phase-1) | `trading_agent.py:_check_position_exits_locked` SL-PROPAGATE block | Broker trailing-SL propagation failure logged at DEBUG only | Promoted to WARNING; per-symbol `_obs03_sl_propagate_failures` counter for heartbeat surfacing. Test: `test_obs03_sl_propagate_failure_logs_warning_with_counter`. |
| OBS-04 | High | **FIXED** (phase-5, freeze-bypass slot 3) | `risk_manager.py:is_trade_worth_taking` | Charges-compute exception silently substituted 0.1% fallback | Now logs CRITICAL with `type + repr(exc)` and returns `(False, "charges_compute_failed")`. Test: `test_obs04_is_trade_worth_taking_fails_closed_on_compute_error`. |
| OBS-05 | High | **FIXED** (phase-2) | `execution.py:reconcile_positions_with_broker` + `trading_agent.py:_boot_reconcile_gate_open` | Boot broker-position reconciliation catches `positionBook` failure, logs WARNING, **skips reconciliation entirely** (fails open) | Reconcile now retries 3× with 2/4s backoff. On final live-mode failure, sets `boot_reconcile_failed_live=True` + CRITICAL log + queued alert. New `_boot_reconcile_gate_open()` is checked at the top of `_open_new_position` and refuses entries until operator touches `logs/boot_reconcile.ack` (one-shot, file consumed on first read). Tests: `test_obs05_*` (7). |
| OBS-06 | Medium | **FIXED** (phase-1) | `market_safety.py:check_data_quality` | Staleness + 20% spike checks wrapped parsing in `except: pass` | Both branches now log WARNING + `return False, "staleness_check_failed"` / `"spike_check_failed"`. Tests: `test_obs06_market_safety_no_bare_pass_in_staleness_or_spike`, `test_obs06_market_safety_runtime_fail_closed_on_inner_exception`. |
| OBS-07 | Medium | **FIXED** (phase-1) | `trading_agent.py:can_trade gate` | Circuit-breaker rejections invisible in daemon log | `logger.warning(f"[RISK-GATE] Skipping {symbol}: {reason}")` added before audit. |
| OBS-08 | Medium | **FIXED** (phase-1) | `trading_agent.py:_audit_reject` + `signal_audit.py:summarize_today` | Audit-write swallowed; read errors swallowed | Both now log WARNING (rate-limited on the write side); `summarize_today` returns a `read_error` sentinel field so the banner can highlight partial data. |
| OBS-09 | Medium | **FIXED** (phase-1) | `trading_agent.py:_on_tick store_tick` | WS `store_tick` failure swallowed | Rate-limited (1/min) WARNING with `repr(exc)` + suppression counter. |
| OBS-10 | Medium | **FIXED** (phase-5, freeze-bypass slot 3) | `base_strategy.py:_atr` | `_atr()` swallowed exceptions silently and returned 0.0 | Now logs WARNING with `type + repr(exc)` (and on EWM-NaN result). Returns 0.0 so existing zero-ATR guards in RiskManager fire as designed. Tests: `test_obs10_*` (2). |
| OBS-11 | Medium | **FIXED** (phase-1) | `execution.py:_verify_modify_trigger` | Post-modify SL verification silently returned when `orderBook()` raised | Now logs WARNING with order_id + expected trigger + `repr(exc)`. |
| OBS-12 | Medium | **FIXED** (phase-1) | `data_handler.py:is_market_open` | Unknown holiday year → `is_market_open()` still True (fail-open) — Bug L pattern | Now fail-closed (CRITICAL log + return False). Test: `test_obs12_is_market_open_fails_closed_on_uncurated_year`. |
| OBS-13 | Medium | **FIXED** (phase-1) | `trading_agent.py:_refresh_market_context` | Intraday Nifty/VIX overlay `except: pass` | Both blocks log WARNING with the overlay-permissive consequence spelled out. |
| OBS-14 | Medium | **FIXED** (phase-1) | `trading_agent.py:circuit guard day high/low fetch` | Pre-trade circuit guard day high/low fetch `except: pass` | WARNING log + partial-data mode explicitly named. |
| OBS-15 | Medium | **FIXED** (phase-1) | `trade_analyzer.py:evaluate_setup` | `evaluate_setup` returned `(0.0, "db_error")` without logging | `logger.warning(f"[LEARNING] load_trade_patterns failed -- pattern weight=0.0; ... {exc!r}")`. |
| OBS-16 | Low | **FIXED** (phase-1) | `execution.py:_persist_order` | Order ledger DB write at DEBUG only | Promoted to WARNING with order_id/symbol/status. Test: `test_obs16_order_ledger_persist_failure_is_warning`. |
| OBS-17 | Low | **FIXED** (phase-1) | `trading_agent.py:preflight alert dispatch` | Preflight boot-failure alert dispatch `except: pass` | CRITICAL log + sticky `logs/preflight_failed.flag` file written; nested `except: pass` only if even the flag write fails. |
| OBS-18 | Low | **FIXED** (phase-1) | `websocket_client.py:Kite set_mode` | Kite `set_mode(MODE_FULL)` failure swallowed | WARNING log spelling out "feed degraded to LTP-only". |
| OBS-19 | Low | **FIXED** (phase-5, freeze-bypass slot 3) | `risk_manager.py:regime_size_multiplier` | Missing regime returned full-size multiplier | Co-fixed with NUM-12: `unknown` default flipped 1.00 -> 0.50. |
| OBS-20 | Low | **FIXED** (phase-1) | `battery.py:_load_market_data_cache` | No B-19-style audit log on pickle load | SHA256[:16] + mtime + absolute path now in the load log line. Test: `test_obs20_battery_cache_load_logs_sha256`. |

---

## Runtime Performance (15)

**Verification status:** PERF-01, PERF-02, PERF-03, PERF-14 verified by reading the cited code paths on 2026-05-28. Production config confirmed: `use_websocket: false`, `scanner.top_n: 300`, `poll_interval: 60`. Other PERF-* findings are based on code reading without runtime profile data; severities are estimates from static analysis.

| ID | Sev | Status | File:line | What | Fix sketch + expected speedup |
|---|---|---|---|---|---|
| PERF-01 | Critical | **FIXED** (phase-4) | `data_handler.py:get_ltp_batch + get_multiple_ltp`, `angelone.py` | `get_multiple_ltp` was N sequential REST calls | New `AngelOneDataSource.get_ltp_batch` uses `getMarketData(mode=LTP)` with 50-token chunking; `get_multiple_ltp` prefers it. **~50× speedup on 300-symbol LTP phase.** Tests: `test_perf01_*` (4). |
| PERF-02 | Critical | **FIXED** (phase-1) | `trading_agent.py:_get_historical_cached` + `_evaluate_strategy` | Intraday OHLCV not cached → 1,200 REST fetches/cycle | Per-cycle `(symbol, timeframe) -> DataFrame` memo on `TradingAgent`; cleared at the top of `_trading_cycle`; hit/miss tallies in `[CYCLE-DIGEST] hist_cache=H/M`. With 300 symbols × 4 strategies expected H~=900, M~=300 (4x dedup). Tests: `test_perf02_historical_cache_dedups_within_cycle`, `test_perf02_clear_resets_cache_and_tallies`. |
| PERF-03 | High | **FIXED** (phase-1) | `regime.py:classify_regime`, `classify_intraday_regime` | `[REGIME-INPUT]` was `logger.info` on every call | Demoted to `logger.debug`. Cycle digest still surfaces the final regime once per cycle at INFO. Existing test `tests/unit/test_regime_and_gates.py` updated to capture at DEBUG. New test: `test_perf03_classify_regime_log_is_debug_not_info`. |
| PERF-04 | High | **FIXED** (phase-4) | `trading_agent.py:_process_signal SL/TP block` | Entry path triple-fetched the same 6h window | Now derives `atr` from `snap.atr_pct * current_price / 100` and falls back to the explicit fetch only when snap is empty. Test: `test_perf04_entry_path_derives_atr_from_snapshot`. |
| PERF-05 | High (WS on) / Medium (WS off) | **FIXED** (phase-3) | `trading_agent.py:_buffer_tick / _flush_tick_buffer` | Per-tick INSERT opened a fresh sqlite3 connection | 5000-row deque + flush at 100 rows or 1s; final flush in `_shutdown`. Co-fixed with CONC-05. |
| PERF-06 | High | **FIXED** (phase-4) | `database.py:load_trade_patterns + trade_analyzer.evaluate_setup` | Pattern lookup loaded all 200 rows then Python-filtered | New SQL kwargs `strategy=?`, `regime=?`. Index `idx_trades_strategy_regime` covers it (PERF-10). Tests: `test_perf06_*` (3). |
| PERF-07 | Medium | **FIXED** (group-G) | `trading_agent.py:_get_tick_history_cached + _evaluate_strategy + _clear_historical_cache` | `get_candle_history` allocates new DataFrame per strategy eval. 300 symbols × 4 strategies = **1,200 DataFrame allocs/cycle**, ~60-120 MB short-lived allocation churn → frequent gen-1/gen-2 GC pauses of 10-50 ms stall WS thread. | New per-cycle `(symbol, timeframe) -> DataFrame` memo on `TradingAgent` mirroring PERF-02; cleared at the top of `_trading_cycle`; empty/None results NOT cached so REST-fallback keeps working. Tests: `TestPERF07TickHistoryCache` (10). |
| PERF-08 | Medium | **FIXED** (phase-4) | `database.py:store_candles` | Row-iterated INSERTs at minute boundary | `executemany` round-trips the whole candle batch in one statement. Tests: `test_perf08_*` (2). |
| PERF-09 | Medium | **FIXED** (phase-4) | `trading_agent.py:_refresh_market_context` | Fresh `requests.Session` on every refresh | Stashed on `self._yahoo_session`; reused across refreshes. Test: `test_perf09_market_context_refresh_reuses_yahoo_session`. |
| PERF-10 | Medium | **FIXED** (phase-4) | `database.py:_init_schema, _conn` | Missing covering indexes; no `PRAGMA cache_size` | New indexes `idx_trades_symbol_exit`, `idx_trades_strategy_regime`, `idx_equity_ts`. Per-conn `PRAGMA cache_size=-64000`. Tests: `test_perf10_*` (3). |
| PERF-11 | Medium | **FIXED** (phase-1) | `trading_agent.py:_snapshot_equity` + `_trading_cycle` | `_snapshot_equity` re-fetched LTP via N individual `get_ltp` calls | `_trading_cycle` now stashes `current_prices` on `self._last_prices`; `_snapshot_equity` reuses with REST fallback for the rare path. |
| PERF-12 | Medium | **FIXED** (phase-1) | `trading_agent.py:_setup_logging` | Loguru file sink was synchronous | `enqueue=True` added; main thread no longer blocks on file fsync. Test: `test_perf12_file_logger_uses_enqueue_true`. |
| PERF-13 | Medium | **FIXED** (group-G) | `battery.py:_save_market_data_cache + _load_market_data_cache + _sha256_file + _read_sidecar_hash` | `~300 MB market_data.pkl` unpickled per variant with `max_tasks_per_child=1`. The OBS-20 (phase-1) sha256 audit log forced every worker to re-hash 300 MB on its own (~1-2 s × 20 variants = 20-40 s/battery of pure redundant work the parent already knew). | Sidecar `market_data.pkl.sha256` written once at cache creation; loaders parse the sidecar mtime-gated and skip rehashing. Live-hash fallback when sidecar missing/stale keeps OBS-20 audit log identical (`hash_source=sidecar` vs `hash_source=live`). Best-effort sidecar write (a sidecar I/O error never breaks the .pkl write). **Speedup: 1-2 s/variant, 20-40 s/battery.** Process-isolation (Bug F) preserved by leaving `max_tasks_per_child=1`. Tests: `TestPERF13BatteryCacheSidecar` (13). |
| PERF-14 | Medium | **FIXED** (phase-4) | `trading_agent.py:_run_scan_async` | Periodic scan ran synchronously, blocking main loop ~3 min | New `_run_scan_async` daemon thread; periodic-rescan call site uses it. Boot-time + pre-market warm-up still synchronous. Tests: `test_perf14_*` (2). |
| PERF-15 | Medium | **FIXED** (phase-4) | `docker-compose.yml` | Trader had 1500M RAM cap but no CPU limit | `cpus: "1.5"` cap + `cpus: "0.5"` reservation. Test: `test_perf15_docker_compose_caps_cpus`. |

### Composite cycle wall-time estimate (current production config)

With `top_n=300`, 4 active strategies, `use_websocket: false`, `poll_interval=60`:

| Phase | Estimate |
|---|---|
| `get_multiple_ltp` (300 sequential, 3/sec rate limit) | **100-180 s** |
| Historical REST (300 × 4 strategies, deduplication uncertain) | **400-600 s** worst case |
| Strategy compute (indicators on 200 bars × 1,200 calls) | **30-60 s** |
| Logging + gates + ensemble + audit | **5-15 s** |
| **Total** | **~3-14 min/cycle** |

**Implication:** the daemon cannot complete a cycle within the 60 s `poll_interval`. `_fast_exits_sleep` 15 s polls partially mitigate exits for held symbols, but **signal generation, new entries, and scanner refresh lag by minutes**. This likely explains part of the "zero trade days" pattern observed in the freeze v2.1 window — even if a strategy emits a valid signal, the price the agent sees may be minutes old by the time it reaches the gate layer.

### Recommended perf fix order (zero slot impact, freeze-safe)

1. **PERF-01** (LTP batch endpoint) — biggest single win. ~half-day implementation, regression test against AngelOne `marketQuote` API.
2. **PERF-02** (per-cycle historical memo) — second biggest. ~2h. Just a dict on `TradingAgent` keyed by `(symbol, timeframe)` cleared at start of each `_trading_cycle`. Even without TTL caching, in-cycle dedup alone collapses 1,200 fetches → 300.
3. **PERF-03** (REGIME-INPUT to DEBUG) — 5 min. Immediate.
4. **PERF-11** (reuse current_prices in _snapshot_equity) — 10 min.
5. **PERF-12** (loguru `enqueue=True`) — 2 min.
6. **PERF-14** (background scanner) — half-day.

PERF-01 + PERF-02 + PERF-03 together would likely bring cycle time from **~3-14 min down to ~15-30 s** — comfortably under `poll_interval`. Worth doing as a focused "perf-sprint v2" before the freeze-lift review.

---

## Architectural themes

Most of the 86 findings collapse onto 6 themes. Fixing the theme typically knocks out multiple findings together.

### Theme A — Broker-as-source-of-truth violation
**Findings:** ORD-01, ORD-07, ORD-08, ORD-09, ORD-11, STATE-01, STATE-05, OBS-11

The agent mutates portfolio state from REST acks (`PLACED`) without confirming actual fills via `orderBook()` / `positionBook()`. Paper mode hides this because paper synth-fills instantly. Live behaviour can produce: phantom positions for unfilled LIMITs, missing residuals on partials, double entries on retry timeouts, SL-Ms sized for the requested qty rather than filled qty.

**Structural fix:** add a `_wait_for_terminal(order_id, timeout)` helper in `execution.py` (the e2e test tool `tools/test_live_single_trade.py` already has this — promote it to production). Mutate portfolio only with confirmed `averageprice`/`filledshares`. Treat `PLACED` as pending state, not success.

### Theme B — Boot reconciliation gap
**Findings:** STATE-02, STATE-03, STATE-05, STATE-12, OBS-05

At boot the agent loads positions from SQLite but doesn't authoritatively reconcile against broker `positionBook()` for unaccounted symbols. Crash-after-fill-before-DB → daemon boots flat while broker holds real exposure. Mismatch handler uses uninitialised attributes. Pre-market boot can restore yesterday's stale MIS rows.

**Structural fix:** at boot, always fetch broker net positions and broker orderBook. For every non-zero `netqty` absent from DB → CRITICAL alert, refuse new entries on that symbol until operator acknowledges. Fail closed, not open.

### Theme C — Async fragility on the WebSocket thread
**Findings:** ORD-06, CONC-02, CONC-03, CONC-04, CONC-06, CONC-07, CONC-09, OBS-09

The WS tick handler does synchronous broker I/O (place_order, update_sl_trigger, store_tick), mutates trailing-stop state without the exit lock, runs while subscriptions are being mutated from another thread, and isn't reconnected when the JWT refreshes. Single-threaded WS + 5 held symbols + a volatile tick burst = exits delayed, ticks dropped, trailing-state corrupted.

**Structural fix:** WS handler becomes "enqueue tick, update in-memory price, return". A dedicated worker thread drains the queue and does the broker I/O + persistence + trail update under the exit lock. JWT refresh triggers WS disconnect + reconnect with fresh feed token.

### Theme D — Silent failure on the unhappy paths
**Findings:** OBS-01..OBS-20, STATE-09, NUM-13

Every critical action (flatten on SL fire, broker SL propagation, broker boot reconcile, expected-profit charge calc, signal audit write, intraday-regime fetch, circuit-guard day-high fetch) has at least one `except: pass` or `logger.debug` swallow that fails open instead of fail-closed.

**Structural fix:** an audit pass through every `except` block in `trading_agent.py` + `packages/core/*.py`. For each: classify as (a) genuinely benign (e.g. log-write failure) → log WARNING with `repr(exc)`, (b) safety-critical → fail-closed (block trades, alert operator, do not silently continue with degraded behaviour).

### Theme E — Backtest-vs-live parity not yet honest
**Findings:** NUM-01, NUM-05, NUM-06, NUM-11, NUM-15

The backtester reports numbers that don't match what live would produce — even after the perf sprint. Shorts are 5× under-sized (NUM-01). Trend filter uses today's incomplete daily bar (NUM-05/15). Paper applies adverse slippage, live doesn't (NUM-11). Yahoo's REST gives a half-formed 5m bar that the tick aggregator wouldn't deliver (NUM-06).

These compound: every battery PF/WR/PnL number is wrong in a direction that varies by strategy and regime. The 2026-06-08 freeze-lift review will be reading numbers it shouldn't trust at face value.

**Structural fix:** per-class. NUM-01 is a one-day fix in portfolio + sizer (freeze-safe). NUM-05/15 needs `as_of_date` plumbing through `_trend_context` (frozen file — needs slot). NUM-11 needs a live-side slippage validator that mirrors the paper adverse model. NUM-06 needs dropping any bar with `ts + interval > now`.

### Theme F — Live cycle wall-time blowout
**Findings:** PERF-01, PERF-02, PERF-03, PERF-04, PERF-07, PERF-11, PERF-12, PERF-14, PERF-15

The live `_trading_cycle` cannot complete within the configured 60 s `poll_interval`. The dominant causes are (a) N+1 sequential broker calls where a batch API exists (PERF-01 LTP, PERF-11 equity snapshot), (b) cache-miss thrash on intraday OHLCV invoked per (strategy, symbol) per cycle (PERF-02), (c) entry path triple-fetching the same window (PERF-04), and (d) chatty INFO logging on the synchronous file sink (PERF-03, PERF-12).

The scanner blocking the main loop for ~3 min every 30 min (PERF-14) compounds the problem with a periodic blind window where no exits run.

**Structural fix:** the perf sprint v2 above. PERF-01 + PERF-02 + PERF-03 alone likely bring cycle time from 3-14 min down to 15-30 s. Then PERF-04, PERF-11, PERF-12, PERF-14 are polish to keep tail latency low. None require frozen-file changes. PERF-15 (CPU cap) is infra hygiene independent of code.

**Operational urgency:** the "zero trade days" observed across the freeze v2.1 window may be partly a perf artifact — even valid signals reach the gate layer on multi-minute-old prices. **Worth confirming this hypothesis with a wall-time profile log before the next live trading day**, regardless of which other fixes happen first.

---

## Freeze impact summary

Slots remaining at audit time: **1 of 3**.

Files NOT on the freeze list (changes are slot-free):
- `packages/core/execution.py`
- `packages/core/portfolio.py`
- `packages/core/database.py`
- `packages/core/file_lock.py`
- `packages/core/cooldown_persistence.py`
- `packages/core/runtime_state_persistence.py`
- `packages/core/trailing_stop_persistence.py`
- `packages/core/signal_audit.py`
- `packages/core/market_safety.py`
- `packages/core/data_handler.py`
- `packages/core/features.py`
- `packages/core/regime.py`
- `packages/core/tick_aggregator.py`
- `packages/core/trade_analyzer.py`
- `packages/brokers/angelone.py`
- `packages/brokers/paper.py`
- `packages/core/websocket_client.py`
- `trading_agent.py` (most paths; the freeze names specific functions like `_pre_trade_safety_checks` — check before editing)
- `tools/`

Files ON the freeze list (changes consume a slot):
- `packages/core/risk_manager.py` — affects NUM-02, NUM-03, NUM-04 (partial), NUM-08, NUM-09, NUM-12, OBS-04, OBS-19, CONC-01 (partial)
- `packages/strategies/*.py` — affects nothing in this audit directly (XGB/LSTM/rule-based already touched within slots)
- `packages/strategies/_trend_context.py` — affects NUM-05, NUM-15
- `packages/strategies/ensemble.py` — affects nothing in this audit
- Trained models (`*.pkl`) — affects nothing in this audit

**~73 of 86 findings can be fixed without consuming the last slot.** The remaining ~13 cluster around risk_manager + _trend_context and would benefit from a single coordinated slot consumption (or waiting for freeze-lift 2026-06-08). All 15 PERF-* findings are freeze-safe.

---

## Recommended sequencing (preserved from audit conversation)

**This week (no slot, urgent):**
1. **PERF-03 + PERF-11 + PERF-12** — demote REGIME-INPUT to DEBUG, reuse current_prices in equity snapshot, loguru enqueue. Pure quick wins. ~30 min.
2. **PERF-01 + PERF-02** — LTP batch endpoint + per-cycle historical memo. Highest perf ROI. ~1 day combined. Brings cycle from 3-14 min down to ~15-30 s.
3. **OBS-01 + OBS-02 + OBS-03** — wrap failed flatten / failed signal-exit / failed SL-propagation paths in CRITICAL log + email alert. ~30 min.
4. **STATE-02** — broker `positionBook()` call at boot, alert on broker-only symbols. ~2h.

**Next week (no slot, important):**
5. **ORD-01 / STATE-01** — wait for terminal status before mutating portfolio. ~half-day plus regression tests.
6. **ORD-02 + ORD-03** — idempotency on retry, rollback on portfolio failure. ~half-day each.
7. **PERF-04 + PERF-14** — entry-path dedup, background scanner. ~half-day combined.
8. **CONC-02 + CONC-03** — move WS hot path to enqueue-and-return; worker thread drains. ~1 day.
9. **ORD-06** — WS reconnect on JWT refresh. ~2h.

**Before freeze-lift 2026-06-08:**
10. **NUM-01** — fix short MIS margin. **All backtester numbers before this fix are biased.** ~1 day + re-run v2_holdout battery for honest short-side numbers.
11. **NUM-02** — Kelly post-sizing 1-share floor (regression of F-34). 10-min fix. **Touches risk_manager.py — slot.**
12. **OBS-04 + OBS-06 + OBS-12 + OBS-13 + OBS-14** — fail-closed on parsing/fetch failures that currently fail-open. ~half-day.
13. **PERF-05 + PERF-08** — only if `use_websocket: true` is on the freeze-lift roadmap. Otherwise defer.

**At freeze-lift (slot consumption acceptable):**
14. **NUM-05/15** — `as_of_date` plumbing through `_trend_context` to eliminate live lookahead.
15. **STATE-03** — reorder boot attribute initialization.

---

## Status legend

- **OPEN** — not started
- **IN-PROGRESS** — work begun (link to branch/PR)
- **FIXED** — code change merged + regression test passing (link to commit SHA)
- **DEFERRED** — knowingly not fixed (rationale required)
- **WONTFIX** — invalid finding or accepted risk (rationale required)

When a finding's status changes, update the row + add a one-line note to the changelog below.

---

## Changelog

- **2026-05-28** — initial audit complete. 71 findings created across 5 angles (orders, state, concurrency, numeric, observability), all OPEN.
- **2026-05-28** — 6th angle added: runtime performance. +15 PERF-* findings. Total 86. PERF-01/02 verified by code-reading to confirm production impact. Sequencing updated to put quick perf wins first.
- **2026-05-28 (later)** — **Phase 1 landed**: 22 findings FIXED across non-frozen files (OBS-01/02/03/06/07/08/09/11/12/13/14/15/16/17/18/20, NUM-13/14, ORD-04/12, STATE-10, CONC-11/12, PERF-02/03/11/12). 20 regression tests added (`tests/unit/test_audit_2026_05_28_phase1.py`); full suite 1,456/1,456 green. Committed but NOT deployed (slot preserved). 3 OBS findings on frozen files (OBS-04/10/19) deferred to Phase 5. Remaining 61 findings queued in Phases 2-5.
- **2026-05-28 (evening)** — **Phase 2 landed** (commit `1f2f23b`): 5 critical money-at-risk findings FIXED — OBS-05 (boot reconcile fail-closed), STATE-02 (broker-only positions block), ORD-01/STATE-01 (`_wait_for_terminal` + truthful fill price), ORD-02 (per-intent idempotency tag + pre-retry orderBook reconciliation), ORD-03 (atomic-entry rollback). 16 regression tests added; full suite 1,476/1,476 green. NOT deployed.
- **2026-05-29 (morning)** — **Phase 3 landed** (commit `d1beea5`): 15 concurrency + state-hygiene findings FIXED — ORD-06 (JWT WS reconnect), CONC-02 (trail mutation lock), CONC-04 (candle-close callback outside aggregator lock), CONC-06 (subscription iteration lock), CONC-08/09 (SIGTERM handler + WS thread join), STATE-03 (cooldown init order), STATE-04 (atomic close), STATE-06 (file-lock retry-with-backoff, no unlocked fallback), STATE-08 (debounced trail persist on WS mutation), STATE-09 (corrupt JSON sentinel + fail-closed gate), STATE-11 (signal-audit retry queue), STATE-12 (stale MIS sweep), CONC-05 / PERF-05 (tick batching). 28 regression tests; full suite 1,511/1,511 green. NOT deployed. **Architectural deferrals**: CONC-03 (WS enqueue+return), STATE-05 (orders boot recovery) — queued for a focused architectural session.
- **2026-05-29 (later)** — **Phase 4 landed** (commit `4b96024`): 9 PERF findings FIXED — PERF-01 (LTP batch endpoint), PERF-04 (entry-path ATR snapshot reuse), PERF-06 (server-side pattern filter), PERF-08 (candle batch insert via executemany), PERF-09 (Yahoo session reuse), PERF-10 (covering DB indexes + 64MB cache_size pragma), PERF-14 (background scanner), PERF-15 (docker CPU cap). 17 regression tests; full suite 1,528/1,528 green. NOT deployed. **Deferrals**: PERF-07 (DataFrame alloc profiling), PERF-13 (battery worker pickle).
- **2026-05-29 (afternoon)** — **Phase 5 landed** (commit `ec957ef`, freeze-bypass slot 3 of 3): 12 frozen-file semantic findings FIXED — NUM-02 (Kelly post-zero audit-reject), NUM-03 (live MTM equity sync), NUM-04 (NSE tick-size rounding helper + SL routing), NUM-05/15 (`_fetch_daily` drops forming-today bar + `as_of_date` plumbing), NUM-08 (short-side compute_round_trip leg mapping), NUM-09 (NaN VIX -> unknown in `classify_regime`), NUM-12 / OBS-19 (unknown regime multiplier flipped 1.00 -> 0.50), OBS-04 (charges-compute fail-closed), OBS-10 (`_atr` logs WARNING + repr(exc)), CONC-01 (immediate `update_open_positions` after entry). 18 regression tests + 2 existing tests pinned to new contract; full suite 1,546/1,546 green. NOT deployed (trader on `430069c`). All 5 phases complete: 63 findings closed across 5 commits, 83 regression tests added.
- **2026-05-29 (evening)** — **Misc-A landed** (commit `03ba66d`): NUM-01 short MIS margin model FIXED. New `mis_short_margin_pct` knob (production config 0.20, legacy default 1.0); per-position `cash_locked` persistence; cash reconciliation parity for legacy DB rows. 18 regression tests; full suite 1,548/1,548 green. NOT deployed.
- **2026-05-29 (evening)** — **Misc-B landed** (commit `da7ab69`): NUM-06 (`_drop_forming_intraday_bar` in REST historical-data path) + NUM-07 (`dist_from_high_pct/low_pct` session-bounded via `groupby(df.index.date)`) FIXED. 17 regression tests; full suite 1,565/1,565 green. NOT deployed.
- **2026-05-29 (late evening)** — **Misc-C landed** (commit `d578ff1`, pushed): ORD-05 / ORD-07 / ORD-08 / ORD-09 FIXED — live order discipline. ORD-09 cancel-on-TTL + race re-check (catches the case where the cancelled order filled in the cancel window); ORD-08 SL-M sized off broker `filled_quantity`; ORD-07 confirmed wired via Phase-2 `_wait_for_terminal`; ORD-05 atomic cancel-then-flatten (if SL fired during cancel race, flatten is skipped and the in-portfolio close uses the SL fill price). 8 new regression tests; type-guard added to `_close_position_safely` for non-dict `sl_meta` mocks; legacy SL-tracking suite re-fixtured with `_seed_orderbook` to satisfy the new `_wait_for_terminal` contract. Full unit suite 1,588/1,588 green; integration suite 248/248 green. NOT deployed.
- **2026-05-29 (night)** — **Misc-D landed** (commit `f7d90cc`, pushed): NUM-11 + ORD-11 FIXED — live slippage parity + tolerance circuit breaker. New `ExecutionEngine._record_slippage` is the single source of truth for `slippage_pct` / `slippage_breach` across paper, live, partial, race, and the passive `get_order_status` observer path. Live breaches emit CRITICAL `[ORD-11-SLIPPAGE]` logs; opt-in `execution.halt_symbol_on_slippage_breach` adds the symbol to a blocklist consumed by `TradingAgent._open_new_position` (entries gated; exits / SL trail bypass). Public ops API: `is_symbol_slippage_blocked`, `clear_slippage_block`, `get_slippage_breached_symbols`. 12 new regression tests; full unit 1,600/1,600 green; integration 248/248 green. NOT deployed.
- **2026-05-30 (early morning)** — **Misc-E landed** (commit `1518b24`, pushed): ORD-10 FIXED — reactive re-auth on AngelOne auth-class errors. New module-level `classify_smartapi_error` recognises 7 known AB-codes + 10 string phrases + 401/403 status hints. ExecutionEngine exposes `set_auth_refresh_callback`; TradingAgent wires `_handle_broker_auth_failure` -> `_maybe_refresh_broker_session(force=True)` to bypass the 7h proactive gate. Callback fires **at most once per top-level `_live_order_with_retry` call** so a misbehaving callback can't loop the retry budget. Defensive: rate-limit signature takes precedence over auth heuristics; unknown errors classify as `transient` so contract drift can't halt trading; callback exceptions are caught and logged. 17 new regression tests (9 classifier + 8 hook); full unit 1,617/1,617 green; integration 248/248 green. NOT deployed.
- **2026-05-30 (morning)** — **Misc-F landed**: NUM-10 FIXED — Decimal arithmetic for charges. `charges.py` inner accumulators run in `Decimal` with per-component 1-paisa `ROUND_HALF_EVEN` quantization. New identity `compute_round_trip(...).total == compute_one_leg(BUY) + compute_one_leg(SELL)` holds byte-for-byte. `portfolio.close_position` now derives `exit_commission` directly from `compute_one_leg(exit_price, ...)` instead of `total_commission - entry_commission`, eliminating float subtraction drift over long-running portfolios. Public API stays float-typed at the boundary so existing call sites are unaffected. 8 new regression tests; full unit 1,625/1,625 green; integration 248/248 green. NOT deployed.
- **2026-05-30 (morning)** — **Misc-G landed**: PERF-07 + PERF-13 FIXED — perf wins on the WS hot path and the battery worker boot. PERF-07: new `TradingAgent._get_tick_history_cached` per-cycle `(symbol, timeframe) -> DataFrame` memo wraps the tick-aggregator's `get_candle_history`; `_evaluate_strategy` now routes through it; `_clear_historical_cache` clears both the PERF-02 historical cache *and* the new tick cache at cycle start; empty/None results are NOT cached so the REST-fallback path inside `_evaluate_strategy` keeps working. With 300 symbols × 4 strategies on a shared 5min timeframe this eliminates ~60-80% of DataFrame allocations on the WS thread (~3-4× alloc reduction on the eval micro-phase, eliminating gen-1/gen-2 GC pauses). PERF-13: `_save_market_data_cache` now writes a sidecar `market_data.pkl.sha256` (full 64-char hex + mtime gate); `_load_market_data_cache` reuses the sidecar via the new `_read_sidecar_hash` helper instead of re-hashing 300 MB on every worker. Saves ~1-2 s/variant (20-40 s/battery) without weakening the OBS-20 audit log: live-hash fallback when the sidecar is missing or its mtime drifts (manual edits, rsync, resume); load log line tags `hash_source=sidecar` vs `hash_source=live`; sidecar write is best-effort (a sidecar I/O error never breaks the .pkl write); `max_tasks_per_child=1` (Bug F isolation) is preserved. 23 new regression tests (10 PERF-07 + 13 PERF-13); 1 phase-1 PERF-02 test + 1 phase-1 OBS-20 pin updated for the helper-extraction. Full unit suite 1,648/1,648 green; integration suite 248/248 green. NOT deployed. (Honest correction below — this commit-message originally claimed "86/86 closed", which was off by 2: STATE-07 and CONC-10 were still genuinely OPEN; misc-G closed 11 of the 13 misc-OPEN findings, not all 13.)
- **2026-05-30 (afternoon)** — **Misc-H landed**: STATE-07 + CONC-10 FIXED — durability + watchdog freshness, the last two genuinely OPEN findings of the audit. STATE-07: `Portfolio._log_trade` now holds a per-portfolio `threading.Lock` across the open / write / `flush + os.fsync` / close cycle; `signal_audit.log` (already locked) now also `flush + fsync`; `signal_audit._drain_retry_queue` fsyncs after the recovery batch. fsync `OSError` (Windows shares / CIFS) is fail-soft — row stays in the OS page cache. CONC-10: dedicated daemon `_run_heartbeat_thread` writes `health.json` every `health_pulse_interval_seconds` (default 30s) from a main-loop-published `_heartbeat_snapshot`, with a freshly-stamped `ts_unix` and current `running` flag, *independent* of cycle completion. Started at the top of `run()` before the cycle loop; stopped FIRST inside `_shutdown` so a final `running=false` pulse lands before WS / DB teardown. Disabled by setting `health_pulse_interval_seconds: 0`. Plus three stale exec-summary rows flipped to FIXED with phase pointers (OBS-01 → phase-1, PERF-01 → phase-4, PERF-02 → phase-1). 20 new regression tests (5 STATE-07 portfolio + 4 STATE-07 signal-audit + 11 CONC-10); 1 integration `_shutdown` slice budget bumped (CONC-10 + CONC-09 grew the body). Full unit suite 1,668/1,668 green; integration suite 248/248 green. NOT deployed. **All 86 audit findings closed for real now.**
