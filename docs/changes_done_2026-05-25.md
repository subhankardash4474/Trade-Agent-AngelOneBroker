# Changes Done — 2026-05-25 (Freeze Week-2)

**Operator:** Subhanda
**Session theme:** Re-analysis of the pre-speed-patch 90-day × 228-stock
battery revealed the short side as structural loss-driver. Pre-staged
the cheapest possible fix for Friday review while keeping the freeze
contract intact.

This document captures every change made on 2026-05-25, with the
explicit accounting for freeze-v2.1 bypass-slot consumption.

---

## Summary

| Category | Files Touched | Freeze Status |
|---|---|---|
| Observability — heartbeat + watchdog deploy fixes | `tools/send_heartbeat.py`, `tools/cloud/install_*_cron.sh`, `tools/watchdog_check.py`, `tools/cloud/install_watchdog_cron.sh`, `docker-compose.yml` | Audit-only |
| Observability — disk rotation tooling | `tools/cloud/prune_old_battery_runs.sh`, `tools/cloud/install_prune_cron.sh` | Audit-only |
| Observability — 90d battery findings analysis | `docs/findings_log_2026-05-25.md`, `docs/post_freeze_v4_proposal.md` | Audit-only |
| Observability — battery harness V17/V18/V19 + queue insert | `packages/research/battery.py`, `packages/research/backtest_ensemble.py`, `data/battery_queue.yaml` | Audit-only |
| **`risk.allow_shorts` flag (pre-stage)** | **`trading_agent.py`, `config.yaml`** | **Slot 1 / 3** |
| Tests | `tests/unit/test_short_selling.py` | Audit-only |

**Bypass slots consumed today: 1 of 3 (now 1 / 3 used, 2 / 3 remaining).**

---

## 1. `risk.allow_shorts` flag (the slot-consuming change)

### What changed

`trading_agent.py`:
- `TradingAgent.__init__`: load `risk.allow_shorts` (default `True`)
  into `self._allow_shorts`.
- `_process_signal`: new gate inside `Signal.SELL` + `pos is None`
  branch that drops new short opens when `self._allow_shorts` is
  `False`, logging `allow_shorts:false` via `_audit_reject` and
  returning before any sizing/safety/order code runs.
- `_rejection_cooldown_skip_reasons`: include `"allow_shorts"` so a
  blocked short doesn't seed a per-symbol cooldown that would outlast
  a flag flip back to `true`.
- `_reason_skips_cooldown` default tuple: same addition for the
  defensive `getattr` fallback used by test stubs.

`config.yaml`:
- New `risk.allow_shorts: true` key with comment block explaining the
  flag's rationale, the layering relationship with
  `execution.enable_short_selling`, and pointers to the evidence
  documents.

### What did NOT change

- **Live agent behaviour**: `risk.allow_shorts` defaults to `true`,
  which is the existing behaviour (no short blocked). Production
  config explicitly sets it to `true` (added today; preserves
  current behaviour).
- **Existing `execution.enable_short_selling` flag**: unchanged. The
  new flag is a higher layer; effective short-open permission =
  `risk.allow_shorts AND execution.enable_short_selling AND
  regime in execution.short_selling_regimes`.
- **Exits, cover, and squareoff paths**: untouched. The gate is
  scoped strictly to `Signal.SELL` + `pos is None` (new short
  opening). SL/TP/trailing/peak-giveback, EOD squareoff, signal-based
  exits of existing longs, and signal-based covers of existing shorts
  all bypass the gate.

### Why this consumes a bypass slot

Per `docs/FREEZE_v2.1.md` §"What WOULD consume a slot":

> A bypass commit is "slot-consuming" iff it modifies any file in:
> - `packages/strategies/` (strategy core)
> - `packages/core/risk_manager.py`, `packages/core/breaker.py`
> - `packages/core/position_sizer.py`
> - `trading_agent.py` (and `_pre_trade_safety_checks` in particular)
> - `config.yaml` strategy or risk blocks
> - `models/xgboost_model.pkl`

This commit touches `trading_agent.py` AND `config.yaml`'s `risk:`
block, so by the strict rule it IS slot-consuming, even though the
default flag value makes it a behavioural no-op.

The slot is being consumed BECAUSE the alternative — flipping the
existing `execution.enable_short_selling: true → false` directly —
would also consume a slot but would lack:

1. Audit-grep-ability (`allow_shorts:false` is distinct from
   `shorts_disabled`).
2. The risk-namespace placement (risk policy ≠ execution capability).
3. The pre-Friday-review opportunity to ratify or reject the long-only
   experiment with the validation data in hand (see §3).

### Evidence supporting the flag

See `docs/findings_log_2026-05-25.md` §3 for full evidence. TL;DR:

| | 60d × 50 Nifty (post-patch) | 90d × 228 full universe (pre-patch) |
|---|---|---|
| V1 baseline | -₹298 PF 0.80 | +₹177 PF 1.04 |
| V2 filters off | -₹420 PF 0.75 | +₹659 PF 1.13 |
| V1 LONGS only | n/a | **+₹556 (inferred PF ~1.5)** |
| V1 SHORTS only | n/a | -₹379 (PF ~0.85) |
| V2 SHORTS only | n/a | -₹398 (PF ~0.85) |

The short side has structurally negative edge across both V1 and V2
on 339+ short trades over 90 days. The cheapest fix is
`risk.allow_shorts: false`.

### Tests added

8 new tests in `tests/unit/test_short_selling.py`:

- `TestRiskAllowShortsGate`:
  - `test_allow_shorts_true_passes_through` — default behaviour unchanged
  - `test_allow_shorts_false_blocks_new_short` — flag does its job
  - `test_allow_shorts_reason_distinct_from_shorts_disabled` — audit log
  - `test_allow_shorts_false_still_exits_long_on_sell` — exits unaffected
  - `test_allow_shorts_false_still_covers_short_on_buy` — covers unaffected
  - `test_allow_shorts_fires_before_regime_check` — gate order is right
  - `test_missing_attribute_defaults_to_allow` — test fixture safety
- `TestBacktestAllowShortsGate`:
  - `test_default_is_true` — BacktestConfig default
  - `test_gate_stats_includes_shorts_blocked` — new GateStats field
  - `test_battery_bt_config_propagates_allow_shorts` — battery wiring

All 1194 unit tests passing post-change.

---

## 2. Battery V17 / V18 / V19 (audit-only)

`packages/research/battery.py` — three new variants in the `VARIANTS`
list:

- **V17_long_only_shipped**: `risk.allow_shorts: false` (V1 + flag off)
- **V18_long_only_threshold_3pct**: V4's tuning + flag off
- **V19_long_only_filters_off**: V2's no-filter config + flag off

`packages/research/backtest_ensemble.py`:
- `BacktestConfig.allow_shorts: bool = True` new field
- `GateStats.shorts_blocked: int = 0` new counter
- New gate in the backtest loop after ensemble aggregation, scoped to
  `Signal.SELL` + `symbol not in portfolio.positions`

`packages/research/battery.py:_bt_config`:
- Reads `risk.allow_shorts` from the merged variant config and
  propagates it to `BacktestConfig`.

These touch ONLY `packages/research/` — audit-only per the bypass
rules, no slot consumed.

---

## 3. Battery queue priority validation insert (audit-only)

`data/battery_queue.yaml` — new slot #2:

```yaml
- name: nifty500_v4_long_only_validation_60d
  days: 60
  interval: 5m
  workers: 2
  universe-file: tests/fixtures/battery_v2_universe.json
  variants:
    - V1_baseline_current_shipped
    - V2_all_filters_off
    - V4_threshold_3pct
    - V17_long_only_shipped
    - V18_long_only_threshold_3pct
    - V19_long_only_filters_off
```

Estimated runtime: 6 variants × ~14h ÷ 2 workers = ~42h. Completes
Wed/Thu, ahead of Friday 2026-05-29 review. Existing slot #2
(`v2_baseline_90d`) demoted to slot #3; still queued.

The scheduler re-reads `data/battery_queue.yaml` at the start of each
loop iteration, so the change takes effect automatically when the
current `nifty50_60d` run finishes — no systemctl restart needed.

`data/battery_queue.yaml` is a schedule artefact, not runtime state.
Audit-only.

---

## 4. Operational tooling (audit-only)

### 4.1 Watchdog cron (continued from 2026-05-24)

Earlier on 2026-05-25 morning we shipped the daemon-liveness watchdog
(`tools/watchdog_check.py` + `tools/cloud/install_watchdog_cron.sh`,
`*/5 * * * *`). Fixed two bugs after the first deployment:

- `STALE_SECONDS` raised 300s → 600s to accommodate the daemon's
  ~5m20s heartbeat cadence.
- `_send_alert` return semantics: always returns `True` on successful
  `AlertManager.send_alert()` invocation so `last_alert_unix` is
  recorded, enabling recovery alerts and escalation.

### 4.2 Disk rotation cron (new today)

`tools/cloud/prune_old_battery_runs.sh`:
- Idempotent script that tar.gz's completed battery runs older than
  N days into `logs/backtests/archive/<run_id>.tar.gz`.
- Skip rules: keep N most recent; never touch the run-id of a
  currently-running `battery_*` container; skip already-archived
  entries.

`tools/cloud/install_prune_cron.sh`:
- Installs the `0 2 * * *` cron (02:00 UTC == 07:30 IST daily) with
  `--age-days 7 --keep 2`.

One-shot manual run at `--age-days 6 --keep 2` reclaimed ~262M net
(514M raw → 252M archive) from the 2026-05-18 run. Daily cron will
keep trending it down as the queue produces new runs.

### 4.3 Heartbeat env-overlay fix (audit-only, deployed earlier today)

Fixed bug where `tools/send_heartbeat.py` wasn't reading API key from
`.env` after `docker compose down/up` cycle. Root cause was the
script's bypass of `packages.core.secrets.apply_env_to_config`.
Patched via `_load_config()` to use the same overlay pattern as the
main daemon. Made the patch persistent by mounting `./tools` as
read-only in `docker-compose.yml`.

### 4.4 Scanner universe fix gated behind config flag (audit-only)

The `scanner.use_live_universe` flag added earlier today gates the
504-stock NSE archive CSV fetch behind opt-in. Default `false`
preserves the existing ~231-stock hardcoded fallback (which is what
the live agent has always been using). No live behaviour change.

---

## 5. Findings documentation (audit-only)

### 5.1 `docs/findings_log_2026-05-25.md`

New append-only document that preserves the analysis from this
session. Sections:

1. Live agent performance (Week 1 + 2)
2. Battery 60d × 50 Nifty (post-patch)
3. Battery 90d × 228 full universe (pre-patch, killed) — THE major finding
4. Three candidate fixes ranked
5. Validation runs needed
6. V7 finalisation observation
7. Open questions deferred
8. Friday review decision matrix
9. Pointers to raw data

### 5.2 `docs/post_freeze_v4_proposal.md` (updated today)

Originally drafted as V4-only deploy proposal in the morning. Expanded
in the afternoon (after the 90d × 228 re-analysis) into a 3-way
comparison (V4-tighten / V2-filters-off / V1-longs-only) with
§10 capturing the new evidence and decision options for Friday.

---

## 6. Friday 2026-05-29 review decision matrix

The Friday review now has decision-quality artefacts:

| Decision | Status | Notes |
|---|---|---|
| `risk.allow_shorts` flag exists and defaults true | ✓ DONE today | Slot 1 / 3 consumed |
| Validation queue insert (V1/V2/V4/V17/V18/V19 on 200-stock) | ✓ DONE today | Auto-starts after current run |
| `docs/findings_log_2026-05-25.md` evidence trail | ✓ DONE today | Append-only |
| `docs/post_freeze_v4_proposal.md` 3-way comparison | ✓ DONE today | §10 added |
| Validation run completion | ⏳ Wed/Thu | Auto-scheduled |
| Friday decision: flip `risk.allow_shorts: false` for 2026-06-08? | ⏳ depends on validation | Pre-staged |

The pre-staged flag means Friday's review can do "flip a YAML value
on 2026-06-08 IF validation data supports it" without additional code
review or test passes. The work is front-loaded.

---

*Last updated 2026-05-25 14:00 IST. Session log; no live trading
during the session (market closed / agent autonomous).*

---

## 7. (Afternoon append) Battery throughput + queue-resume bug fixes

Triggered by operator question "Why is the battery test degrading
with time?" — log analysis revealed two latent bugs that had
silently slowed every battery run since `enqueue=True` was added
to the per-variant logger sinks. Full forensics in
`docs/findings_log_2026-05-25.md` §10.

### Code changes (bug fixes, NO bypass slot consumed)

| File | Type | Change |
|---|---|---|
| `packages/research/battery.py` | Bug fix | Capture sink id from `logger.add()`, wrap variant body in try/finally with `logger.remove(sink_id)` so per-variant log files don't leak across the worker subprocess's lifetime. Adds comment block referencing the smoking-gun arithmetic-progression file sizes. |
| `tools/run_battery_queue.py` | Bug fix + ops | (1) New bind mount `-v $TRADER_HOME/packages:/app/packages:ro` so host-side code edits deploy without rebuilding the image. (2) `build_docker_run_argv(..., resuming: bool)` now emits `--resume <id>` when continuing an existing run (was always emitting `--run-id <id>`, which makes the harness ignore completed variants and re-run them). (3) `_run_id_for()` docstring corrected (it previously claimed the harness auto-resumes from a populated folder — wrong). |
| `tests/unit/test_battery_queue_scheduler.py` | Test coverage | 3 new tests: (a) packages mount pin, (b) fresh runs use `--run-id`, (c) resume runs use `--resume`. |

### VM operations

| Step | Action | Outcome |
|---|---|---|
| 1 | Captured worker-log sizes as smoking gun before any change | Logged in findings §10 |
| 2 | Deployed `battery.py` (sink-leak fix) + `run_battery_queue.py` (packages mount) to VM | OK |
| 3 | Stopped current degraded container | Lost ~50% V9/V10 in-flight progress (~1.5 h of compute, acceptable) |
| 4 | Restarted `battery-scheduler.service` | New container spawned with `/app/packages` mount |
| 5 | **Discovered Bug #2** — new container ran with `resume=False`, started V1 from scratch, threatening V1-V8 results | Stopped within ~2 minutes; no JSON overwritten |
| 6 | `cp -r results results.backup_before_rerun_20260525T084845` | V1-V8 backed up; verified PF/trades match prior analysis |
| 7 | Deployed `run_battery_queue.py` v2 (resume-aware argv) | OK |
| 8 | Dry-run scheduler — confirmed `--resume` emitted for in-flight job, `--run-id` for new jobs | OK |
| 9 | Restarted scheduler; new container spawned with `--resume` | `resume=True \| completed=8 \| pending=11` ✓ |
| 10 | 2-min observation: workers are V9 + V10 (correct), rate 44-55 ev/s (5x recovery), V1-V8 logs frozen at pre-restart size (sink leak gone) | All clear |

### Performance impact

- Before fix: V9-V16 ETA was 30-50 h of wall time (degrading rate).
- After fix: V9-V16 ETA is ~6-8 h.
- **Net recovery: ~24-42 h.** Friday-review-blocking job
  `nifty500_v4_long_only_validation_60d` now expected to start
  Wed 22:00 IST instead of Fri 10:00 IST.

### Freeze accounting

These are pure bug fixes in research / tooling code paths. Not a
behavior change to the live trading agent. **Bypass-slot ledger
unchanged: still 1/3 used (by `risk.allow_shorts`).**

Verified by inspection: the touched files are
`packages/research/battery.py` (battery harness only — never
imported by `trading_agent.py` or `core/*`), `tools/run_battery_queue.py`
(scheduler — never imported by live agent), and a test file.

### Open follow-ups carried forward

- Add CI test that asserts the scheduler emits `--resume` (not
  `--run-id`) when state file shows a prior run_id. Today's test
  covers the function; a higher-level integration would close
  the loop end-to-end.
- Consider a "panic-mode" backup of `results/*.json` at the start
  of every harness invocation (cheap insurance — V1-V8 backups
  were ad-hoc today, would prefer automatic).

---

## 8. (Evening append) Senior dev + algo expert backtester scan

Triggered by operator: "draw on a senior exp dev hat for this small
role and do a full scan for backtester logic. Don't put down the
algo expert hat so that we miss some other thing." Full forensics
in `docs/findings_log_2026-05-25.md` §11.

### Bugs found

| ID | Severity | Status |
|---|---|---|
| A | HIGH | FIXED — intra-bar SL/TP not modeled (close-only check, +10-30% PnL bias) |
| B | HIGH | DEFERRED — `regime` hardcoded `"unknown"` in backtester (documented divergence) |
| C | MED | FIXED — opposite-signal exit dropped (held positions never closed on opposite ensemble vote) |
| D | MED | FIXED — Sharpe annualized on event-level returns × sqrt(252) |
| E-I | LOW | Documented in code at the relevant call sites |

### Code changes (bug fixes, NO bypass slot consumed)

| File | Change |
|---|---|
| `packages/research/backtest_ensemble.py` | New `_detect_intrabar_exit(pos, open, high, low, close)` helper using bar OHLC; `last_equity_per_day` daily tracker; `_build_result(..., daily_equities=...)` signature change. Documented Bug B as a known divergence in the `regime = "unknown"` site so future readers see it before grepping. |
| `tests/unit/test_backtest_ensemble_helpers.py` | 13 new tests: 11 cases for `TestIntrabarExitDetection` (long/short × wick/gap × both-hit/missing-SL + defensive corner cases), 2 for `TestSharpeUsesDailyEquities` (daily path + legacy fallback). |

### VM operations

| Step | Action | Outcome |
|---|---|---|
| 1 | Deployed `backtest_ensemble.py` to `/opt/trading-agent/packages/` (mounted into containers read-only by today's earlier change). | OK |
| 2 | Stopped in-flight container `battery_nifty50_60d_20260522T085929`. | V9/V10 progress lost (~30 min); V1-V8 results preserved on disk. |
| 3 | Operator chose **Option A — Clean restart**: archived legacy results, removed scheduler state entry, restarted scheduler. | Fresh run_id `battery_nifty50_60d_20260525T093330` started. |
| 4 | Verified the new container loaded ALL THREE bug fixes via `python3 -c "from research.backtest_ensemble import EnsembleBacktester; ..."` introspection. | All fixes confirmed present. |
| 5 | Verified scheduler emits `--run-id <fresh>` (not `--resume <stale>`) when state is clear. | Confirmed in `docker inspect`. |
| 6 | 3 min post-start: V1 + V2 running, 1.5-2.8% complete, 44-53 ev/s, no sink-leak signs (V3-V19 logs empty as expected). | Healthy. |

### Test count

- Before this scan: 1196 unit tests passing.
- After: **1209 unit tests passing** (+13 from the new
  `TestIntrabarExitDetection` and `TestSharpeUsesDailyEquities`
  classes).

### Freeze accounting

All changes today are pure correctness bug fixes in research /
tooling code paths:

- `packages/research/backtest_ensemble.py` — battery harness only;
  not imported by `trading_agent.py` runtime
- `packages/research/battery.py` — sink-leak fix (earlier)
- `tools/run_battery_queue.py` — scheduler argv fix (earlier)
- `tests/unit/*` — new + updated tests

**Bypass-slot ledger unchanged. Still 1/3 used (by
`risk.allow_shorts`).**

### Projected timeline

- New 60d Nifty 50 battery: completes ~02:30 IST Tuesday 2026-05-26
- `nifty500_v4_long_only_validation_60d` (queue slot #2): starts
  ~03:00 IST Tuesday, completes ~07:00-09:00 IST Wednesday
- `v2_baseline_90d` and rest of queue: Wednesday onwards
- Friday 2026-05-29 review: has fully-consistent fixed-code results
  for V1, V2, V4, V17, V18, V19 on BOTH the 50-stock and 200-stock
  universes.

### Trader VM (sanity preamble, recorded for completeness)

Operator also asked "is the market really silent?" — answer was NO.
Market is active (Nifty 23,944-23,984, VIX 16.8-17.1). The agent is
silent BY DESIGN: `bear_high_vol` regime gates strategy weights to
`{supertrend_follow: 0.1, rsi_momentum: 5.0}`, effectively
rsi_momentum-only, and that strategy's RSI trigger is not firing
because Nifty is mid-range. 10-day trade count decay
(8→8→4→1→2→0→2→2→2→0) is consistent with §3 finding that shorts are
the structural loss-driver; the live agent is doing the right thing
by being conservative until the freeze-lift decision.

---

## §9. Ops runbook + Friday 2026-05-22 silent-hang RCA

**Operator request:** "keep some documentation which clearly mention
which vm and how to login so that u don't miss it and command that
you use and all for reference purpose. Sometime something break and
causes issue like friday, i think because of the wrong db command all
that break down happend we have not done the RCA for it."

### What was created

New consolidated reference document **`docs/ops_runbook.md`**. Single
source of truth for:
1. The two VMs (trader at `ubuntu@80.225.251.79`, backtester at
   `opc@80.225.197.125`), SSH keys, container names.
2. The PowerShell base64-bash pattern (the single biggest source of
   reliability on this system).
3. Canonical commands for both VMs (status, restart, logs, cron).
4. The actual SQLite schema (table list + `trades` columns) and the
   four queries the agent actually uses, in copy-paste form.
5. Seven specific pitfalls encountered in prior sessions, each with
   its workaround.
6. A 6-step diagnostic flow for "something looks wrong".
7. **§9: RCA for the Friday 2026-05-22 silent-hang event** —
   answering the operator's open question.

### Friday 2026-05-22 RCA — bottom line

**The "wrong DB command" was NOT the proximate cause.** Forensic
evidence on the trader VM:

- The mangled `docker exec trading-agent python3 -c '... FROM
  positions WHERE status=OPEN ...'` command in `sudo` journald lives
  at **Thursday May 21 12:37 IST** — not Friday. It used the old
  container name `trading-agent` and a non-existent `positions` table
  (correct table is `open_positions`); it errored out at the Python
  parse stage, held no SQLite locks, and the daemon ran fine for the
  next 24 hours.
- On Friday May 22, the daemon went GREEN through 12:23 IST audit
  checkpoint (cycle 87, positions=0, day P&L=+₹80), wrote its 12:24
  scanner banner line `Scanning 500 NSE stocks...`, and then went
  TOTALLY SILENT until containerd restarted the host at 23:34 IST.
- Healthcheck timeouts in `journalctl` started 40 minutes BEFORE the
  full silence (at 11:44 IST), indicating progressively longer
  scanner cycles.
- DB integrity is still `ok`; no OOM kill, no SIGSEGV, no traceback.
- The last action was network-bound (scanner reaching out to NSE +
  yfinance), not DB-bound.

**Proximate cause:** the scanner blocked on an outbound HTTP call
with no timeout (Python's `requests`/`urllib3` default to *no*
timeout — a slow remote = forever wait). Related but separate from
the C6 NSE Nifty 500 universe-shrinkage bug fixed in `eb5bb84`.

### What we have already shipped in response

- `05eaea0` (May 24): heartbeat email at 09:10 IST (gives same-day
  notice of yesterday's silence).
- `eb5bb84` (May 25): intra-day silent-hang watchdog cron (every
  5 min, alerts on stale `health.json`).
- `3d07219` (May 25): watchdog STALE-threshold tuning to suppress
  false-positives.
- `0cc51c7` (May 25): heartbeat schema-drift fix.
- `eb5bb84` C6: NSE Nifty 500 archive-CSV fallback (closes the
  cloud-IP-blocked endpoint that contributed to the May 22 scanner
  load surge).

### What is still open (recorded for follow-up, NOT done in this slot)

1. **Add `timeout=` to every outbound HTTP call** in
   `packages/core/scanner.py`, `packages/data/`, `packages/brokers/`.
   This is the actual root-cause fix.
2. **Container auto-restart on `unhealthy`** — Docker does not do
   this natively. Either a sidecar that polls
   `docker inspect --format '{{.State.Health.Status}}'` or systemd
   supervision with `sd_notify` + `WatchdogSec=`.
3. **Hard time budget per scanner cycle** via
   `concurrent.futures.ThreadPoolExecutor` with `result(timeout=90)`.

These are queued under "post-freeze-v4 hardening". Filing them as
known follow-ups; not pulled into this session because none of them
are freeze-lift blockers and the silent-hang watchdog (already
shipped) catches the symptom even if the root-cause fix is deferred.

### Files touched in §9

- `docs/ops_runbook.md` — NEW, 10 sections + RCA + index of deep dives.

**Bypass-slot ledger:** still 1/3 used (`risk.allow_shorts`). The
ops runbook is pure documentation; no freeze-bypass slot consumed.

---

## §10. Audit 2026-05-25 quick wins (B-1, B-3, B-4, B-5, B-11)

**Operator delivered** a senior-SWE / algo-trader audit report
(`docs/audit_2026-05-25/BUG_REPORT.md` + `.json`) with 21 findings
classified Critical / High / Medium / Low. This session validates the
report and ships the five "quick wins" the auditor identified as the
highest leverage per minute.

### §10.1 Validation pass — every Critical and High verified against source

Every Critical (3) and High (5) finding was read against the actual
source files before any code was touched:

| ID | Audit claim | Source check | Real bug? |
|---|---|---|---|
| **B-1** | `stop_daemon.py:39` future-import after sys.path bootstrap → SyntaxError | Lines 32-39 confirmed | ✅ |
| **B-2** | `_live_order_with_retry` has no idempotency key; can double-fill on post-commit timeout | execution.py:495-571, order_params at :479-489 has no `ordertag` | ✅ — **deferred to dedicated session** |
| **B-3** | SSL bypass default = "true" in main.py / run_daemon.py + hard-codes elsewhere | 4 distinct sites confirmed (main.py:30, run_daemon.py:47, stock_scanner.py:102, data_handler.py:201) PLUS 4 EXTRA sites I found on a sweep | ✅ — audit underreported by 4 |
| **B-4** | `main.py:67` discards `feed_token` | Confirmed; function returns `api` without writing config | ✅ |
| **B-5** | GNFC duplicate (Chemicals at :246, Agri at :311) | Both lines present in NSE_SECTOR_MAP | ✅ — PLUS a second harmless PFC dup also found |
| **B-6** | NSE holiday calendar ends 2026-12-25 | data_handler.py:27-38 confirmed | ✅ — deferred, not freeze-blocker |
| **B-7** | `_paper_order` uses unseeded global random | Confirmed at execution.py:362-385 | ✅ — deferred (slot-touch in canonical-list ambiguous) |
| **B-8** | Alert dedup file is process-unsafe | Confirmed at alerts.py:321-373 | ✅ — deferred, Group D batch |
| **B-11** | `run_daemon.py:119` reads non-existent `initial_capital` key | Confirmed | ✅ |

**One audit framing correction surfaced.** B-3's "cloud VMs ship
insecure unless env-flag set" is partially wrong: the cloud VM's
`docker-compose.yml` already sets `TRADER_DISABLE_SSL_VERIFY=false`
explicitly, so the cloud posture is secure. The Python defaults of
`"true"` only fire on a laptop running `python main.py` directly —
which IS a real exposure for dev work, but not the deployed posture.
Severity stays Critical because the hard-coded `verify=False`
literals at stock_scanner.py / data_handler.py / trading_agent.py
DO bypass the env var entirely and DID make the cloud VM insecure
for those specific Yahoo Finance + NSE-archive HTTPS calls.

**One deliverable discrepancy noted.** The audit's appendix claims
`logs/ruff_report.txt`, `logs/ruff_critical.txt`,
`logs/bandit_report.txt`, `logs/pytest_unit_report.txt`,
`logs/pytest_integration_report.txt` were produced. **None exist on
disk.** Only `BUG_REPORT.md` + `BUG_REPORT.json` actually landed.
Not blocking; the findings are well-cited (file:line) so the raw
ruff/bandit appendices are reproducible on demand.

### §10.2 Fixes shipped

#### B-1 — `stop_daemon.py` SyntaxError → emergency kill switch restored

Moved `from __future__ import annotations` to **immediately after the
module docstring**, before the sys.path bootstrap. Order is now:
docstring → future-import → sys.path bootstrap → regular imports.
PEP 236 invariant satisfied.

**Verified** with `python -c "import importlib.util; ..."` plus three
regression tests (`TestB1StopDaemonImports`) covering: (i) plain
`compile()` of the source, (ii) full `importlib.util` exec, (iii) AST
assertion that the future-import lineno precedes every other
non-docstring statement.

#### B-4 — feed_token written into `config["broker"]["feed_token"]`

Added `config.setdefault("broker", {})["feed_token"] = feed_token`
inside the success branch of `main.connect_angelone`, right after
the `api.getfeedToken()` call. Defuses the latent WebSocket
auth-time-bomb that would have fired the day someone flips
`data_pipeline.use_websocket: true` in live mode.

**Verified** with a mock-based test that patches `SmartApi`/`pyotp`
and asserts `config["broker"]["feed_token"]` equals the mock return
value after `connect_angelone` returns.

#### B-11 — Idle-heartbeat `cash` reads `capital.initial_balance`

Replaced `config.get("initial_capital", 0.0)` with
`(config.get("capital") or {}).get("initial_balance", 0.0)` in
`run_daemon._write_idle_heartbeat`. The `or {}` defensively handles
the case where `capital` is explicitly None (vs. missing).

**Verified** with two tests: one asserts `cash == 123456.78` for a
config that sets `capital.initial_balance`; the other asserts the
fall-back `0.0` for a config missing the `capital` block entirely.

#### B-5 — GNFC mapped to Chemicals; PFC duplicate also cleaned up

- Removed the `"GNFC": "Agri"` entry at market_safety.py:311 (the
  one that was silently winning due to dict-literal dedup).
- Kept the `"GNFC": "Chemicals"` entry at :246 — operator decision
  per AskQuestion (audit's first instinct + alignment with the
  company's specialty-chemicals segment).
- **Additionally** removed the redundant `"PFC": "NBFC"` entry at
  :268 (a second copy of the same NBFC mapping at :179). The audit
  called this "harmless" but the AST-level test treats any dup as
  an error.

**Verified** with two tests: (i) AST walk asserting zero duplicate
string keys in any `dict` literal in `market_safety.py`; (ii) pin
asserting `NSE_SECTOR_MAP["GNFC"] == "Chemicals"` so a future refactor
that flips it back to Agri is caught.

**Risk-management impact:** sector concentration cap now correctly
counts GNFC against the Chemicals exposure (alongside TATACHEM, RCF,
FACT) rather than against Agri. The supersector rollup
(`use_supersectors: true`) does not span Chemicals + Agri, so the
prior misclassification was real risk-control drift.

#### B-3 — SSL verification: polarity flipped + every hard-code removed

Five distinct sites, all unified to the same env-driven flag pattern:

| File:line | Before | After |
|---|---|---|
| `main.py:30` | `os.environ.get("TRADER_DISABLE_SSL_VERIFY", "true")` | `..., "false")` + WARNING when bypass on |
| `run_daemon.py:47` | same as above | same as above |
| `packages/core/stock_scanner.py:21-25` | unconditional `urllib3.disable_warnings` at import | gated behind `_SSL_BYPASS` module flag |
| `packages/core/stock_scanner.py:48,51,166,307,375` | hard-coded `verify=False` (5 sites) | `verify=not _SSL_BYPASS` |
| `packages/core/data_handler.py:18-22` | unconditional `urllib3.disable_warnings` at import | gated behind `_SSL_BYPASS` module flag |
| `packages/core/data_handler.py:201` | `self._session.verify = False` | `self._session.verify = not _SSL_BYPASS` |
| `trading_agent.py:2791` | `sess.verify = False` (Yahoo Finance market-context fetch) | local `_bypass` check from same env var |

**WARNING emission:** when `TRADER_DISABLE_SSL_VERIFY` is truthy,
both `main.py` and `run_daemon.py` now emit a clear
`logger.warning(...)` at startup so misuse appears in
`logs/trading_agent_*.log` rather than silent. Cloud VMs (with
`docker-compose.yml` setting it to `false`) see no warning.

**Verified** with five tests: (i) AST scan asserting `main.py`
defaults to `"false"`, (ii) same for `run_daemon.py`, (iii)
exhaustive sweep of `packages/core/*.py` for any remaining hard-coded
`verify=False` or `.verify = False` assignment — fails if any
appears, (iv-v) module-attribute checks that
`stock_scanner._SSL_BYPASS` and `data_handler._SSL_BYPASS` exist
and default to `False` when the env var is unset.

### §10.3 Test outcome

| Suite | Before fix | After fix |
|---|---|---|
| `tests/unit` | 1,209 passing | **1,222 passing** (+13 new in `test_audit_2026_05_25_quick_wins.py`) |
| `tests/integration` | 248 passing | **248 passing** (unchanged) |
| Total | 1,457 | **1,470 (+13)** |

Zero regressions. The 13 new tests collectively act as the spec for
the post-fix invariants — each would fail on the pre-fix tree.

### §10.4 Freeze-bypass slot accounting

Audit-only entry per `docs/FREEZE_v2.1.md` §Audit-only entries. The
fixes touch:

- **Slot-listed file:** `trading_agent.py` (one line, the
  `sess.verify` env-flag change in `_refresh_market_context`).
  Justified under "Critical bug fixes" in §What is NOT frozen
  because the TLS-bypass it removes covers the VIX/Nifty data feed
  that drives the regime classifier (regime → strategy weights →
  order sizing). The fix is **behaviour-neutral on the happy path** —
  `verify=True` and `verify=False` produce identical results when no
  MITM is in flight. See `docs/FREEZE_v2.1.md` "Note on the
  2026-05-25 audit-quick-wins entry" for the full justification.
- **Non-slot files:** `stop_daemon.py`, `main.py`, `run_daemon.py`,
  `packages/core/stock_scanner.py`, `packages/core/data_handler.py`,
  `packages/core/market_safety.py`, `tests/unit/*`.
- **Pure docs:** `docs/changes_done_2026-05-25.md`,
  `docs/FREEZE_v2.1.md`.

**Bypass-slot ledger: still 1/3 used** (the `risk.allow_shorts`
flag remains the only slot-consuming entry of this freeze window).

### §10.5 Deferred follow-ups (queued, not in this commit)

#### Dedicated session needed
- **B-2** — order-retry idempotency. Single biggest money-loss
  exposure on the system; on the live-execution path
  (`packages/core/execution.py:_live_order_with_retry`). Needs a
  mock-broker harness, a decision on whether AngelOne's `ordertag`
  field is honoured for dedup, and a fresh slot-consumption
  discussion. Live cutover is gated on this. Tracked in
  `docs/audit_2026-05-25/BUG_REPORT.md` §B-2.

#### Operator-decision-pending
- **B-6** — NSE holiday calendar 2027 freshness. Easy 10-min
  startup-assertion path is freeze-compatible; auto-fetch path is
  too. Choice between the two is the only blocker.

#### Group D batch (whenever convenient, all freeze-compatible)
- **B-8** alert dedup file lock
- **B-9** DataHandler cache bound
- **B-10** SMTP login addr-spec extraction
- **B-12** `re_xxx` secrets placeholder
- **B-14** Portfolio raw sqlite → Database.store_trade_if_absent
- **B-15** profit_factor `inf` → None for JSON safety
- **B-18** urllib → requests on AB1050 path
- **B-19** pickle/torch.load hardening
- **B-20** B904 in run_battery_queue.py
- **B-21** ruff hygiene noise (`F401`, `F841`, `E402`, `SIM105`)

#### Slot-consuming, defer to next freeze-lift window
- **B-7** RNG seeding in execution.py (research reproducibility)
- **B-13** `_periodic_cleanup` reaching into TickAggregator
  private attribute (trading_agent.py)
- **B-16** `require_nifty_above_200ema` default mismatch
  (risk_manager.py) — pin in operator deploy checklist meanwhile
- **B-17** cosmetic `F821 Position` forward type-hint in trading_agent.py

### §10.6 Files touched in §10

- `stop_daemon.py` — B-1 fix
- `main.py` — B-3 (default flip + WARNING) + B-4 (feed_token plumb)
- `run_daemon.py` — B-3 (default flip + WARNING) + B-11 (cash key)
- `packages/core/market_safety.py` — B-5 (GNFC + PFC dedup)
- `packages/core/stock_scanner.py` — B-3 (6 sites)
- `packages/core/data_handler.py` — B-3 (2 sites)
- `trading_agent.py` — B-3 (1 site, justified under critical-bug-fix clause)
- `tests/unit/test_audit_2026_05_25_quick_wins.py` — NEW, 13 tests
- `docs/FREEZE_v2.1.md` — ledger row + note added
- `docs/changes_done_2026-05-25.md` — this section
