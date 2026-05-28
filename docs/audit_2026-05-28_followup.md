# Production-Grade Audit Follow-up — 2026-05-28

**Status:** IN-PROGRESS (Phase 1 of 5 landed; 22 of 86 findings FIXED)
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

Remaining queue (queued by phase in TODO list):
- Phase 2: OBS-04 (frozen), OBS-05, STATE-02, ORD-01/STATE-01, ORD-02,
  ORD-03 — wait-for-terminal, broker boot reconcile, idempotency, rollback.
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
| ORD-01 / STATE-01 | Critical | `trading_agent.py:4433-4453`, `packages/core/execution.py:539-601` | OPEN | Freeze-safe (not on §What is frozen) | Live treats `status=="PLACED"` as a fill. Opens/closes positions in memory using signal-time price without waiting for actual `averageprice`. **Paper mode hides this.** |
| ORD-02 | Critical | `packages/brokers/angelone.py:302-330`, `packages/core/execution.py:534-607` | OPEN | Freeze-safe | `_live_order_with_retry` retries on timeout with **no idempotency key**. Broker wrapper itself warns timed-out call may have placed. Single network stall → duplicate order. |
| ORD-03 | Critical | `packages/core/execution.py:561-599`, `trading_agent.py:4461-4486` | OPEN | Freeze-safe | Entry places broker order + SL-M, then calls `portfolio.open_position()`. If portfolio fails, broker leg is **not flattened**. |
| STATE-02 | Critical | `trading_agent.py:307-317`, `packages/core/execution.py:1097-1208` | OPEN | Freeze-safe | Boot reconciliation only iterates DB-restored symbols. Crash-after-fill-before-DB → daemon boots flat while broker holds real exposure. Never queries `positionBook()` for unaccounted symbols. |
| NUM-01 | Critical | `packages/core/portfolio.py:393-405`, `trading_agent.py:4294-4296` | OPEN | Freeze-safe (portfolio.py not frozen) | Short MIS margin modeled at 100% notional instead of ~20%. Backtester under-sizes shorts ~5× vs live reality. **Every short-side battery number is biased.** |
| OBS-01 | Critical | `trading_agent.py:4740-4778` | OPEN | Freeze-safe | SL/TP/peak-giveback exit loop only logs on `if order and record:`. **Failed flattens produce no log, no alert.** |
| PERF-01 | Critical | `packages/core/data_handler.py:505-514`, `packages/brokers/angelone.py:383-388` | OPEN | Freeze-safe | `get_multiple_ltp` is N sequential REST calls, rate-limited to 3/sec. 300 symbols = **≥100 s/cycle** before network latency. Cycle cannot fit in 60s poll. |
| PERF-02 | Critical | `packages/core/data_handler.py:433-434`, `trading_agent.py:3326-3344` | OPEN | Freeze-safe | Intraday OHLCV explicitly NOT cached + `_evaluate_strategy` fires per strategy per symbol. With `use_websocket: false` (current production config), each cycle hits ~**1,200 REST historical fetches** at 3/sec = **400+ s/cycle**. Daemon is permanently backlogged. |

---

## Order / Execution / Broker (12)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| ORD-01 | Critical | OPEN | `execution.py:539-601` | Treats `PLACED` as `FILLED` — `filled_price: None` returned but caller uses signal price | Poll `orderBook()` until terminal status or TTL; only then mutate portfolio. Treat `PLACED` as non-terminal. |
| ORD-02 | Critical | OPEN | `angelone.py:302-330` | No idempotency on retry — wrapper warns timed-out call may have placed | Pre-retry `orderBook` reconciliation with per-intent client tag (symbol+side+qty+timestamp bucket) stored in DB before send. |
| ORD-03 | Critical | OPEN | `execution.py:561-599` | No broker-leg rollback when `portfolio.open_position` fails after entry+SL placed | Atomic entry: pending-state DB row first; on `open_position` failure, emergency counter-flatten + SL cancel; block symbol until reconciled. |
| ORD-04 | High | **FIXED** (phase-1) | `trading_agent.py:_close_position_safely` | Exits inherit configured default LIMIT order_type → sticky on gaps | `_close_position_safely` now passes `order_type="MARKET"` explicitly. Test: `test_ord04_close_position_safely_forces_market_order_type`. |
| ORD-05 | High | OPEN | `trading_agent.py:3903-3915` | Cancel-SL-then-flatten not atomic — broker SL can fire in cancel window while flatten is in flight → double exit (reverse position) | Before flatten, poll `orderBook`/`positionBook`; if SL already completed, skip flatten + reconcile portfolio from broker state. |
| ORD-06 | High | OPEN | `trading_agent.py:1717-1720`, `websocket_client.py:254-258` | JWT refresh swaps REST `_api` but **never reconnects WebSocket** → tick feed silently stops after ~7-8h | On successful JWT refresh, update `ws_client._api`, rewrite feed/auth tokens, force WS reconnect. |
| ORD-07 | High | OPEN | `execution.py:540-555`, `:1002-1022` | `get_order_status` exists, parses `averageprice`, never called from trading path → live never sees PARTIALLY_FILLED → partial-fill branch in `_close_position_safely` is dead code in production | Integrate fill-wait helper (already in `tools/test_live_single_trade.py::_wait_for_terminal`) into `_live_order_with_retry` + `_close_position_safely`. |
| ORD-08 | Medium | OPEN | `execution.py:561-575` | SL-M placed immediately after entry ack using *requested* qty (not filled qty) → wrong-size SL or rejection on partials | Poll entry fill first; size SL-M to confirmed `filledshares`. |
| ORD-09 | Medium | OPEN | `execution.py:508-610` (absence) | No order-fill TTL, no cancel-on-timeout, no LIMIT→MARKET escalation | Add `order_fill_timeout_sec`; on expiry cancel + reconcile. |
| ORD-10 | Medium | OPEN | `trading_agent.py:1670-1732`, `execution.py:603-604` | Token refresh is proactive (7h timer) only — no reactive re-auth on 401/403/`AB*` | Classify SmartAPI errors; auth failure → immediate re-login + single retry; non-retryable → halt new entries. |
| ORD-11 | Medium | OPEN | `execution.py:137`, `:397-408` | Live records `slippage: None` — no max-slippage circuit breaker (paper applies tolerance, live doesn't) | After live fill, compute slippage vs decision price; if > tolerance, alert + optionally halt new entries on that symbol. |
| ORD-12 | Medium | **FIXED** (phase-1) | `trading_agent.py:_square_off_all` | `_square_off_all` ignored close result; sent "Square Off Complete" even with naked positions | Accumulator + per-symbol CRITICAL log + distinct "SQUARE-OFF INCOMPLETE" alert when any close fails. Test: `test_ord12_square_off_alert_distinguishes_partial_failure`. |

---

## State / Persistence / Recovery (12)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| STATE-01 | Critical | OPEN | (same as ORD-01) | Persists open position from non-terminal status | (see ORD-01) |
| STATE-02 | Critical | OPEN | `trading_agent.py:307-317` | Boot reconcile skips broker-only positions absent from DB | Always fetch broker `positionBook()` at boot; for every non-zero `netqty` absent from DB, CRITICAL alert + block new entries until manual ack. |
| STATE-03 | High | OPEN | `trading_agent.py:348-398` vs `:522`, `:646` | Mismatch handler uses `_stock_loss_today` / `_max_losses_per_stock` before they're initialized → silent failure | Initialize attributes before reconcile, or move reconcile after cooldown init; on mismatch call `_persist_cooldown_state()`. |
| STATE-04 | High | OPEN | `portfolio.py:625-660`, `database.py:84-96` | `close_position` not atomic across 5 transactions (CSV append, dict del, DB delete, trade insert, equity insert) | Single DB transaction wrapping delete-open + insert-trade + equity snapshot; append CSV only after commit. |
| STATE-05 | High | OPEN | `execution.py:153`, `:557`, `database.py:492-523` | In-flight `_pending_orders` in-memory only — no boot recovery from `orders` table or broker orderBook | At boot, reconcile broker orderBook + SQLite `orders` for non-terminal statuses; block duplicate entries per symbol. |
| STATE-06 | Medium | OPEN | `cooldown_persistence.py:145-157`, `runtime_state_persistence.py:126-132`, `trailing_stop_persistence.py:121-128` | `file_lock` 2s timeout falls back to unlocked write — rolling restart can lose blacklist counts via clobber | Fail closed on lock timeout (retry/backoff); never write without lock during PID transition. |
| STATE-07 | Medium | OPEN | `portfolio.py:721-732`, `signal_audit.py:100-117` | Trade CSV + signal-audit CSV append without lock, without fsync | Treat SQLite `trades` as source of truth; derive CSV from DB on boot/EOD, or append only post-commit + periodic fsync. |
| STATE-08 | Medium | OPEN | `trailing_stop_persistence.py:102-132`, `trading_agent.py:1518-1528` | Trailing state persisted only at cycle end — crash 5s before next persist restores wide initial SL | Persist trailing snapshot after every mutation (debounced) or on broker SL modify success; include `saved_at` staleness check. |
| STATE-09 | Medium | OPEN | `cooldown_persistence.py:215-232`, etc. | Corrupt JSON → graceful empty-dict load → blacklisted symbol becomes tradeable again (fail-open) | On CORRUPT load, refuse new entries until operator ack (fail-closed mode), or restore from last good backup. |
| STATE-10 | Medium | **FIXED** (phase-1) | `trading_agent.py:_setup_logging area` | `logs/STOP` unscoped — paper and live daemons shared kill switch | Default now `logs/STOP.<mode>` (live/paper). Explicit `operations.emergency_stop_path` still honoured verbatim. Test: `test_state10_emergency_stop_path_is_mode_scoped`. |
| STATE-11 | Low | OPEN | `trading_agent.py:4405-4534`, `signal_audit.py:100-117` | Signal audit failure swallowed; trade still executes (intentional but creates EOD "0 signals" gap) | Best-effort with retry queue (similar to alert drain pattern). |
| STATE-12 | Medium | OPEN | `trading_agent.py:1748-1786`, `portfolio.py:173-175` | Daily reset clears in-memory maps but not `open_positions` SQLite — pre-market boot restores yesterday's stale MIS | At day boundary (or boot on new IST date), validate `open_positions.entry_time` date; force broker reconcile before pre-market idle. |

---

## Concurrency / Resources (12)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| CONC-01 | High | OPEN | `trading_agent.py:4137`, `risk_manager.py:527-528` | `can_trade()` reads cached `state.open_positions` (refreshed only at cycle end) → within a cycle, consecutive entries can breach `max_open_positions` | Call `risk_manager.update_open_positions(portfolio.open_position_count)` immediately after every successful `open_position`, or read live count. |
| CONC-02 | High | OPEN | `trading_agent.py:1518-1528`, `:4703-4706`, `risk_manager.py:915-918` | WS thread calls `risk_manager.update_trailing_stop()` outside `_exit_check_lock` while main thread mutates same `TrailingStop` objects inside the lock | Route all trailing-stop mutations through `_exit_check_lock`, or add dedicated lock around `_trailing_stops`. |
| CONC-03 | High | OPEN | `trading_agent.py:1506-1528`, `:4713-4716`, `:3912-3915` | WS callback does synchronous broker I/O (`update_sl_trigger_for_symbol`, `place_order`, `store_tick`) → single-threaded WS backlogs, ticks queue, exits delayed | WS handler becomes enqueue-and-return; worker thread drains queue and does broker I/O + persistence + trail update under exit lock. |
| CONC-04 | Medium | OPEN | `tick_aggregator.py:127-133`, `trading_agent.py:1592-1600` | `on_candle_close` fires inside `process_tick()` while holding `_lock` → DB write under the lock → concurrent ticks stall | Append completed candles under lock; dispatch persistence on background thread or defer to main cycle. |
| CONC-05 | Medium | OPEN | `trading_agent.py:1506-1511`, `database.py:321-328` | Every tick inserts a row via `store_tick()` — no batching, no sampling | Batch ticks + flush periodically, or sample (held symbols only), or dedicated writer thread with bounded queue. |
| CONC-06 | Medium | OPEN | `websocket_client.py:92`, `:581-583`, `trading_agent.py:1453` | `_subscriptions` mutated from main thread while WS thread iterates → potential `RuntimeError: dictionary changed size during iteration` | Guard with lock, or build snapshot (`dict(self._subscriptions)`) before iteration. |
| CONC-07 | Medium | OPEN | `websocket_client.py:323-324`, `:439-440`, `:586-604` | Reconnect assigns `self._ws = new_client` without closing old socket → FD leak | Before assigning new client, `close()` existing `_ws` and null it; join prior WS thread. |
| CONC-08 | Medium | OPEN | `run_daemon.py:80-83`, `trading_agent.py:1333-1336` | SIGTERM sets `_shutdown_requested` in `run_daemon.py` but `TradingAgent.run()` polls only `KeyboardInterrupt` → `docker stop` mid-cycle gets SIGKILL before graceful shutdown | Install signal handler inside `TradingAgent.run()` that sets `_running = False`; poll in `_fast_exits_sleep` / `_trading_cycle`. |
| CONC-09 | Medium | OPEN | `trading_agent.py:5033-5037`, `websocket_client.py:195-202` | `_shutdown()` doesn't join WS or reconnect threads → in-flight `_on_tick` races with teardown | Add shutdown event; after `ws_client.stop()`, `join()` WS/reconnect threads with timeout; gate `_on_tick` on shutdown event. |
| CONC-10 | Medium | OPEN | `trading_agent.py:3099-3100`, `:2249-2266` | Heartbeat / `health.json` updates only at end of main loop → long `get_multiple_ltp(200)` or hung broker call freezes heartbeat; watchdog flags as dead | Emit heartbeat from dedicated timer thread independent of cycle completion. |
| CONC-11 | Low | **FIXED** (phase-1) | `portfolio.py:Portfolio.__init__` | `trade_history` in-memory list unbounded | Now `deque(maxlen=10000)`. Dashboards iterate / use len() unchanged. Test: `test_conc11_trade_history_is_bounded_deque`. |
| CONC-12 | Low | **FIXED** (phase-1) | `database.py` + `trading_agent.py:_periodic_cleanup` | `equity_curve` table had no retention | New `purge_old_equity_points(days=90)` mirroring `purge_old_ticks`; wired into `_periodic_cleanup`. Tests: `test_conc12_database_has_purge_old_equity_points`, `test_conc12_periodic_cleanup_calls_equity_purge`. |

---

## Numeric / Financial / Edge (15)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| NUM-01 | Critical | OPEN | `portfolio.py:393-405` | Short MIS margin modeled at 100% notional instead of ~20% → backtester under-sizes shorts ~5× vs live | Replace collateral lock with `margin = notional * mis_short_margin_pct` (config-driven, default 0.20). |
| NUM-02 | High | OPEN | `trading_agent.py:4239-4244` | Kelly multiplier `max(1, int(round(qty * kelly_mult)))` regresses F-34 — if risk-sized qty=0, Kelly forces 1 share → silently exceeds per-trade risk budget | Only apply Kelly when `quantity > 0`; if post-Kelly is 0, audit-reject `sizing:zero_qty`. |
| NUM-03 | High | OPEN | `risk_manager.py:652-674`, `:1035-1051` | `current_balance` only updates on closes — sizing + drawdown read stale equity while positions are open | Each cycle, sync `current_balance` from MTM equity (`portfolio.get_total_value(current_prices)`) before sizing/drawdown checks. |
| NUM-04 | High | OPEN | `execution.py:529-532`, `:697-698`, `risk_manager.py:730-731`, `:889-890` | No NSE tick-size (Rs 0.05) rounding — SL/TP/limit prices like Rs 1,142.37 can be rejected or silently adjusted | Add `round_to_tick(price, tick=0.05, side)` — round SL away from entry, TP toward profit, limits conservatively. |
| NUM-05 | High | OPEN | `_trend_context.py:160-168`, `:218-242` | `_fetch_daily` uses `closes.iloc[-1]` → today's half-formed daily candle = live lookahead in 50d SMA | Use `closes.iloc[-2]` when last bar's date equals today (IST), or fetch prior completed session explicitly. **Frozen file — needs slot.** |
| NUM-06 | High | OPEN | `trading_agent.py:3334-3344`, `data_handler.py:320-349` | REST fallback feeds in-progress 5m bar into strategy indicators (Yahoo returns it; aggregator doesn't) — data-source asymmetry | Drop last row if `timestamp + interval > now (IST)`, or append only closed bars; never evaluate strategies on forming bar. |
| NUM-07 | Medium | OPEN | `features.py:339-342` | `dist_from_high_pct` / `dist_from_low_pct` rolling-75 crosses session boundary → 09:20 includes yesterday's bars | Replace rolling with `groupby(index.date)` cumulative max/min (same pattern as VWAP at `:266-269`). |
| NUM-08 | Medium | OPEN | `risk_manager.py:1002-1006` | Short-side expected-profit gate swaps buy/sell prices wrong → STT undercounted ~20% on that component | For shorts, pass `buy_price=exit_buy_price`, `sell_price=entry_sell_price` to `compute_round_trip` (mirror `portfolio.close_position`). |
| NUM-09 | Medium | OPEN | `regime.py:206-217`, `risk_manager.py:558-566` | `classify_regime` checks `vix is None` but not `isnan(vix)` → NaN VIX yields `bull_low_vol` with full multipliers while `can_trade` simultaneously rejects | Reuse `_parse_finite_number` in `classify_regime`; return `"unknown"` (or block) when VIX/trend are non-finite. |
| NUM-10 | Medium | OPEN | `portfolio.py:448`, `:566-571`, `charges.py:160-201` | All charges/P&L in float; `exit_commission = total - entry` drift accumulates; 100+ trades can flip tight reward-vs-charges gate | Compute charges in `Decimal` (quantize to 2 paise at leg boundaries); store/display as float only at persistence. |
| NUM-11 | Medium | OPEN | `execution.py:397-407`, `:548-555` | Paper applies adverse slippage; live records `slippage: None` and never validates fill vs requested → paper systematically more pessimistic | After live fill, reject/reconcile if `|fill - requested| / requested > tolerance`. |
| NUM-12 | Medium | OPEN | `regime.py:203-209`, `:349`, `risk_manager.py:628-630` | Cold start before first context refresh = `regime="unknown"` with 1.0× multiplier (fully permissive) | Treat `unknown` like intraday: block new entries or apply conservative multiplier (e.g. 0.5×) until first valid refresh. |
| NUM-13 | Low | **FIXED** (phase-1) | `trading_agent.py:_process_signal` | Rejection-cooldown short-circuit left audit-CSV gap | `_audit_reject(..., "reject_cooldown:active")` added before return. Test: `test_num13_rejection_cooldown_writes_audit_reject`. |
| NUM-14 | Low | **FIXED** (phase-1) | `trading_agent.py:CASH-SIZE block` | Cash gate had no min-cash buffer | `risk.min_cash_buffer_rs` (default Rs 200) subtracted from cash before affordability divide. Test: `test_num14_cash_sizing_reserves_min_buffer`. |
| NUM-15 | Medium | OPEN | All 7 strategies + `_trend_context.py:119-122` | Trend filter live lookahead (NUM-05 amplified) — every strategy with `trend_filter_pct` set has parity question mark in backtest | Pass `as_of_date` into `_fetch_daily`; truncate daily series; pre-cache per (symbol, sim_date) in backtester. **Frozen file — needs slot.** |

**Calendar note** (Bug L / 430069c — CORRECT, not re-flagged): rewritten `NSE_HOLIDAYS` in `packages/core/data_handler.py:65-95` matches unit-test contract. Residual risk: coverage ends 2026-12-25; OBS-12 flags the 2027 gap.

---

## Observability / Silent Failure (20)

| ID | Sev | Status | File:line | What | Fix sketch |
|---|---|---|---|---|---|
| OBS-01 | Critical | **FIXED** (phase-1) | `trading_agent.py:_check_position_exits_locked` | SL/TP/peak-giveback exit loop only logged on success | `else:` branch added: CRITICAL log + CRITICAL alert with "MANUAL ACTION REQUIRED". Test: `test_obs01_failed_sl_tp_exit_emits_critical`. |
| OBS-02 | High | **FIXED** (phase-1) | `trading_agent.py:_exit_on_signal` | Same silent-failure pattern | Mirror OBS-01: CRITICAL log + alert. Test: `test_obs02_failed_signal_exit_emits_critical`. |
| OBS-03 | High | **FIXED** (phase-1) | `trading_agent.py:_check_position_exits_locked` SL-PROPAGATE block | Broker trailing-SL propagation failure logged at DEBUG only | Promoted to WARNING; per-symbol `_obs03_sl_propagate_failures` counter for heartbeat surfacing. Test: `test_obs03_sl_propagate_failure_logs_warning_with_counter`. |
| OBS-04 | High | **DEFERRED** (phase-5, frozen) | `risk_manager.py:1009-1010` | `is_trade_worth_it` catches Exception around `compute_round_trip`, substitutes fabricated 0.1% charge estimate, no log | Log `repr(exc)` + traceback; fail-closed (`return False, "charges_compute_failed"`). Touches frozen `risk_manager.py`. |
| OBS-05 | High | OPEN (phase-2) | `execution.py:1106-1113` | Boot broker-position reconciliation catches `positionBook` failure, logs WARNING, **skips reconciliation entirely** (fails open) | Fail-closed on live boot (refuse entries / require operator ack) or retry with backoff + CRITICAL alert. |
| OBS-06 | Medium | **FIXED** (phase-1) | `market_safety.py:check_data_quality` | Staleness + 20% spike checks wrapped parsing in `except: pass` | Both branches now log WARNING + `return False, "staleness_check_failed"` / `"spike_check_failed"`. Tests: `test_obs06_market_safety_no_bare_pass_in_staleness_or_spike`, `test_obs06_market_safety_runtime_fail_closed_on_inner_exception`. |
| OBS-07 | Medium | **FIXED** (phase-1) | `trading_agent.py:can_trade gate` | Circuit-breaker rejections invisible in daemon log | `logger.warning(f"[RISK-GATE] Skipping {symbol}: {reason}")` added before audit. |
| OBS-08 | Medium | **FIXED** (phase-1) | `trading_agent.py:_audit_reject` + `signal_audit.py:summarize_today` | Audit-write swallowed; read errors swallowed | Both now log WARNING (rate-limited on the write side); `summarize_today` returns a `read_error` sentinel field so the banner can highlight partial data. |
| OBS-09 | Medium | **FIXED** (phase-1) | `trading_agent.py:_on_tick store_tick` | WS `store_tick` failure swallowed | Rate-limited (1/min) WARNING with `repr(exc)` + suppression counter. |
| OBS-10 | Medium | **DEFERRED** (phase-5, frozen) | `base_strategy.py:108-109` | `_atr()` catches all exceptions, returns 0.0 with no log → strategies build wrong SLs on "no volatility" | Log at DEBUG/WARNING and return `None` or propagate so callers fail-closed. Touches frozen `strategies/*.py`. |
| OBS-11 | Medium | **FIXED** (phase-1) | `execution.py:_verify_modify_trigger` | Post-modify SL verification silently returned when `orderBook()` raised | Now logs WARNING with order_id + expected trigger + `repr(exc)`. |
| OBS-12 | Medium | **FIXED** (phase-1) | `data_handler.py:is_market_open` | Unknown holiday year → `is_market_open()` still True (fail-open) — Bug L pattern | Now fail-closed (CRITICAL log + return False). Test: `test_obs12_is_market_open_fails_closed_on_uncurated_year`. |
| OBS-13 | Medium | **FIXED** (phase-1) | `trading_agent.py:_refresh_market_context` | Intraday Nifty/VIX overlay `except: pass` | Both blocks log WARNING with the overlay-permissive consequence spelled out. |
| OBS-14 | Medium | **FIXED** (phase-1) | `trading_agent.py:circuit guard day high/low fetch` | Pre-trade circuit guard day high/low fetch `except: pass` | WARNING log + partial-data mode explicitly named. |
| OBS-15 | Medium | **FIXED** (phase-1) | `trade_analyzer.py:evaluate_setup` | `evaluate_setup` returned `(0.0, "db_error")` without logging | `logger.warning(f"[LEARNING] load_trade_patterns failed -- pattern weight=0.0; ... {exc!r}")`. |
| OBS-16 | Low | **FIXED** (phase-1) | `execution.py:_persist_order` | Order ledger DB write at DEBUG only | Promoted to WARNING with order_id/symbol/status. Test: `test_obs16_order_ledger_persist_failure_is_warning`. |
| OBS-17 | Low | **FIXED** (phase-1) | `trading_agent.py:preflight alert dispatch` | Preflight boot-failure alert dispatch `except: pass` | CRITICAL log + sticky `logs/preflight_failed.flag` file written; nested `except: pass` only if even the flag write fails. |
| OBS-18 | Low | **FIXED** (phase-1) | `websocket_client.py:Kite set_mode` | Kite `set_mode(MODE_FULL)` failure swallowed | WARNING log spelling out "feed degraded to LTP-only". |
| OBS-19 | Low | **DEFERRED** (phase-5, frozen) | `risk_manager.py:628-630`, `:349` | Missing/empty regime label returns `regime_size_multiplier = 1.0` (full size) — fails open on regime outage | Default `unknown` to conservative multiplier (<1.0) or refuse entries until known. Touches frozen `risk_manager.py`. |
| OBS-20 | Low | **FIXED** (phase-1) | `battery.py:_load_market_data_cache` | No B-19-style audit log on pickle load | SHA256[:16] + mtime + absolute path now in the load log line. Test: `test_obs20_battery_cache_load_logs_sha256`. |

---

## Runtime Performance (15)

**Verification status:** PERF-01, PERF-02, PERF-03, PERF-14 verified by reading the cited code paths on 2026-05-28. Production config confirmed: `use_websocket: false`, `scanner.top_n: 300`, `poll_interval: 60`. Other PERF-* findings are based on code reading without runtime profile data; severities are estimates from static analysis.

| ID | Sev | Status | File:line | What | Fix sketch + expected speedup |
|---|---|---|---|---|---|
| PERF-01 | Critical | OPEN | `data_handler.py:505-514`, `angelone.py:383-388` | `get_multiple_ltp` is N sequential REST calls. With 300 symbols + 3/sec rate limit → **≥100 s floor** per cycle. Config comment at `config.yaml:612` already acknowledges 3-4 min cycles. | Implement AngelOne `marketQuote`/batch endpoint (≤50 tokens/call). **Speedup: ~50-100× on LTP phase.** |
| PERF-02 | Critical | **FIXED** (phase-1) | `trading_agent.py:_get_historical_cached` + `_evaluate_strategy` | Intraday OHLCV not cached → 1,200 REST fetches/cycle | Per-cycle `(symbol, timeframe) -> DataFrame` memo on `TradingAgent`; cleared at the top of `_trading_cycle`; hit/miss tallies in `[CYCLE-DIGEST] hist_cache=H/M`. With 300 symbols × 4 strategies expected H~=900, M~=300 (4x dedup). Tests: `test_perf02_historical_cache_dedups_within_cycle`, `test_perf02_clear_resets_cache_and_tallies`. |
| PERF-03 | High | **FIXED** (phase-1) | `regime.py:classify_regime`, `classify_intraday_regime` | `[REGIME-INPUT]` was `logger.info` on every call | Demoted to `logger.debug`. Cycle digest still surfaces the final regime once per cycle at INFO. Existing test `tests/unit/test_regime_and_gates.py` updated to capture at DEBUG. New test: `test_perf03_classify_regime_log_is_debug_not_info`. |
| PERF-04 | High | OPEN | `trading_agent.py:4143`, `:3407-3426`, `:4206-4207`, `:4814-4827` | Entry path triple-fetches the same 6h window: `_get_indicator_snapshot` (REST + `compute_all`) + `_get_latest_atr` (re-fetches same window + recomputes ATR) — even though `snap` already contains `atr_pct`. **~1-2 s per entry attempt before gate logic.** | Derive ATR from snapshot; fetch OHLCV once per `(symbol, cycle)`. **Speedup: ~2-3× on entry latency.** |
| PERF-05 | High (WS on) / Medium (WS off) | OPEN | `trading_agent.py:1507-1509`, `database.py:84-96`, `:321-328` | Per-tick `store_tick` opens new sqlite3 connection, sets WAL pragma, INSERT, commit, close. WS on 50 symbols at 1 tick/s = **~50 conn/s × 2-5 ms each = 100-250 ms/s** DB overhead + WAL lock contention with main loop. (Extends CONC-05.) | Batch buffer + `store_ticks_batch` flushed every 1 s or 100 ticks; single writer thread. **Speedup: ~10-20×.** |
| PERF-06 | High | OPEN | `trading_agent.py:4190-4198`, `trade_analyzer.py:736-767`, `database.py:616-622` | Each entry: `SELECT * FROM trade_patterns ORDER BY created_at DESC LIMIT 200`, builds 200 dicts, filters Python-side, runs similarity loop. **~5-20 ms DB + 1-5 ms Python per entry.** | In-memory pattern index keyed by `(strategy, regime)` refreshed every N trades; or SQL `WHERE strategy=? AND regime=? LIMIT 50`. **Speedup: ~5-10×.** |
| PERF-07 | Medium | OPEN | `tick_aggregator.py:148-158`, `trading_agent.py:3326` | `get_candle_history` allocates new DataFrame per strategy eval. 300 symbols × 4 strategies = **1,200 DataFrame allocs/cycle**, ~60-120 MB short-lived allocation churn → frequent gen-1/gen-2 GC pauses of 10-50 ms stall WS thread. | Per-cycle `(symbol, interval) → DataFrame` cache; or return numpy structured array from aggregator. **Speedup: ~3-5× on eval micro-phase.** |
| PERF-08 | Medium | OPEN | `trading_agent.py:1592-1600`, `database.py:277-289` | Candle-close: `pd.DataFrame([candle])` + row-iterated INSERTs in `store_candles`. WS on full watchlist + 3 intervals = **~900 allocs+INSERTs/min** at minute roll. | Pass candle dict directly to single-row INSERT; batch closures per second. **Speedup: ~5-10× at minute boundary.** |
| PERF-09 | Medium | OPEN | `trading_agent.py:2851-2858`, `:2870-2893` | `_refresh_market_context` instantiates fresh `requests.Session` every 10 min + 4-5 sequential Yahoo calls without connection reuse. ~1-3 s per refresh + TCP/TLS handshake overhead. | Store `self._yahoo_session` on agent init (mirror `YahooFinanceDataSource._session`). **Speedup: ~20-30% on refresh.** |
| PERF-10 | Medium | OPEN | `database.py:98-241` (schema), `:353-356`, `:417-419`, `:672-677` | Missing covering indexes: `trades(symbol, exit_time)`, `equity_curve(timestamp)`. `purge_old_ticks` index has wrong leading column. No `PRAGMA cache_size`. **Point lookups degrade 10× as DB ages past 30 days.** | Add `idx_trades_symbol_exit`, `idx_equity_ts`, `PRAGMA cache_size=-64000`. **Speedup: prevents 10× regression with age.** |
| PERF-11 | Medium | **FIXED** (phase-1) | `trading_agent.py:_snapshot_equity` + `_trading_cycle` | `_snapshot_equity` re-fetched LTP via N individual `get_ltp` calls | `_trading_cycle` now stashes `current_prices` on `self._last_prices`; `_snapshot_equity` reuses with REST fallback for the rare path. |
| PERF-12 | Medium | **FIXED** (phase-1) | `trading_agent.py:_setup_logging` | Loguru file sink was synchronous | `enqueue=True` added; main thread no longer blocks on file fsync. Test: `test_perf12_file_logger_uses_enqueue_true`. |
| PERF-13 | Medium | OPEN | `battery.py:565-567`, `:667-670`, `:1576-1578` | `~300 MB market_data.pkl` unpickled per variant with `max_tasks_per_child=1`. 20-variant battery pays **20-80 s** pure-load overhead. Deliberate (Bug F isolation) but quantifiable. | Memory-mapped shared cache via `multiprocessing.shared_memory`, or raise `max_tasks_per_child=5` with explicit cache clear. **Speedup: 1-3 s/variant.** |
| PERF-14 | Medium | OPEN | `trading_agent.py:1270-1273`, `stock_scanner.py:379-382`, `config.yaml:52` | Every 30 min, `_run_scan` runs synchronously on main thread with sequential Yahoo fetches → **~3 min blind window** with no trading cycle, no exit checks. | Run scan in background thread; swap watchlist atomically on completion. Or parallelize with `ThreadPoolExecutor(8)` + rate limit. **Speedup: removes 3-min blind windows.** |
| PERF-15 | Medium | OPEN | `docker-compose.yml:94-101` | Trader container has 1500M RAM cap but **no CPU limit**. A long cycle can saturate both vCPUs while WS thread, healthcheck, audit checkpoint compete. Under CPU starvation, WS tick latency rises. | Set `cpus: "1.5"` or `cpu_shares`; or fix PERF-01/02 so cycle fits <15 s. **Speedup: caps tail latency on WS exit path.** |

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
