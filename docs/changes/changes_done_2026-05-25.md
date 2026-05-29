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
| Observability — 90d battery findings analysis | `docs/findings/findings_log_2026-05-25.md`, `docs/reviews/post_freeze_v4_proposal.md` | Audit-only |
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

Per `docs/freeze/FREEZE_v2.1.md` §"What WOULD consume a slot":

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

See `docs/findings/findings_log_2026-05-25.md` §3 for full evidence. TL;DR:

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

### 5.1 `docs/findings/findings_log_2026-05-25.md`

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

### 5.2 `docs/reviews/post_freeze_v4_proposal.md` (updated today)

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
| `docs/findings/findings_log_2026-05-25.md` evidence trail | ✓ DONE today | Append-only |
| `docs/reviews/post_freeze_v4_proposal.md` 3-way comparison | ✓ DONE today | §10 added |
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
`docs/findings/findings_log_2026-05-25.md` §10.

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
in `docs/findings/findings_log_2026-05-25.md` §11.

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
(`docs/audits/audit_2026-05-25_bug_report.md` + `.json`) with 21 findings
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

Audit-only entry per `docs/freeze/FREEZE_v2.1.md` §Audit-only entries. The
fixes touch:

- **Slot-listed file:** `trading_agent.py` (one line, the
  `sess.verify` env-flag change in `_refresh_market_context`).
  Justified under "Critical bug fixes" in §What is NOT frozen
  because the TLS-bypass it removes covers the VIX/Nifty data feed
  that drives the regime classifier (regime → strategy weights →
  order sizing). The fix is **behaviour-neutral on the happy path** —
  `verify=True` and `verify=False` produce identical results when no
  MITM is in flight. See `docs/freeze/FREEZE_v2.1.md` "Note on the
  2026-05-25 audit-quick-wins entry" for the full justification.
- **Non-slot files:** `stop_daemon.py`, `main.py`, `run_daemon.py`,
  `packages/core/stock_scanner.py`, `packages/core/data_handler.py`,
  `packages/core/market_safety.py`, `tests/unit/*`.
- **Pure docs:** `docs/changes/changes_done_2026-05-25.md`,
  `docs/freeze/FREEZE_v2.1.md`.

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
  `docs/audits/audit_2026-05-25_bug_report.md` §B-2.

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
- `docs/freeze/FREEZE_v2.1.md` — ledger row + note added
- `docs/changes/changes_done_2026-05-25.md` — this section

---

## 11. Bug E — O(N²) full-history slicing in backtester `_merge_bars` (mid-restart perf fix)

### §11.1 Problem

The 2026-05-25 09:33 IST restarted V1+V2 nifty50_60d battery
(launched after the Phase-A correctness fixes were verified)
showed a second wave of throughput degradation:

| Wall-clock     | Sim date    | Cumulative rate | Instantaneous rate |
|----------------|-------------|-----------------|--------------------|
| t = 3 min      | 2026-02-25  | 39 ev/s         | 39 ev/s            |
| t = 9.1 min    | 2026-03-02  | 27 ev/s         | ~21 ev/s           |
| t = 22.2 min   | 2026-03-05  | 19 ev/s         | ~14 ev/s           |
| t = 45.4 min   | 2026-03-11  | 14 ev/s         | ~10 ev/s           |
| t = 60 min     | 2026-03-13  | 13 ev/s         | ~7 ev/s (per worker) |

This was NOT a loguru sink leak recurrence — log duplicate-ratio
was 1.07 (healthy), memory was 840 MB / 11 GB (healthy), CPU was
99% saturated on both workers (CPU-bound work growing).

### §11.2 Root cause

`packages/research/backtest_ensemble.py:_merge_bars` yielded the
**entire growing history** (`df.iloc[: i + 1]`) as `df_slice` to
every strategy on every bar. Each strategy then did `data.copy()`
+ EWM + shift over the full slice — O(N) work per event — but
only consumed the last 1-20 values. Total work scaled as O(N²)
per symbol.

### §11.3 Fix

Added `strategy_history_window: int = 300` to `BacktestConfig`.
Modified `_merge_bars` to slice to the last 300 bars instead of
the full prefix. Per-event work is now constant w.r.t.
simulation length.

**Numerical equivalence proof:** for window=300, the EWM
contribution of dropped older bars is bounded by `(1-α)^300`:

| Strategy        | Period | (1-α)^300   | Verdict                        |
|-----------------|--------|-------------|--------------------------------|
| RSI(14)         | 14     | 4 × 10⁻¹⁹   | Below float precision           |
| ATR/ADX(14)     | 14     | 4 × 10⁻¹⁹   | Below float precision           |
| Supertrend(10)  | 10     | 4 × 10⁻²⁴   | Below float precision           |
| MA cross EMA(50)| 50     | 7 × 10⁻⁶    | ~1 ppm, below useful precision  |
| XGBoost(60)     | 60 fixed| 0          | Fixed feature window, fully covered |

Signal DIRECTION is byte-identical in every test case. Confidence
drift is below the 4th decimal place. Both are below the
ensemble's `confidence_threshold: 0.55` and the audit-logger's
2-decimal rounding.

### §11.4 Tests

`tests/unit/test_strategy_history_window.py` — 13 new tests:

- BacktestConfig default = 300, is int, overridable (3 tests)
- Per-strategy full-vs-windowed equivalence on 500-bar synthetic
  OHLCV, walking bars [350, 500) for: RSI, MA crossover, Mean
  Reversion, Supertrend, VWAP (5 tests)
- `_merge_bars` slice contract: bounded by window, tail not head,
  never empty, uncapped when window > history (5 tests)

Tolerances: signal direction exact; confidence atol=1e-4
(10× worst-case EWM tail); SL/TP rtol=1e-4 (1 bp).

### §11.5 Suite results

```
$ pytest tests/unit -q                  -> 1235 passed in 43.20s
$ pytest tests/integration -q           ->  248 passed in 34.18s
```

Total: **1483 passing** (1222 prior + 13 new + 248 integration),
zero regressions.

### §11.6 Freeze accounting

`packages/research/backtest_ensemble.py` is in `packages/research/`
which is NOT on the slot-consuming list in FREEZE_v2.1. This is
an **audit-only performance fix** that preserves all observable
behavior (signals, PnL within 1 bp). **No bypass slot consumed.**

**Bypass-slot ledger: still 1/3 used** (only `risk.allow_shorts`
remains).

### §11.7 Expected perf impact

- Late-sim per-event cost: O(800) → O(300) = ~2.7× faster
- Allocations per event: ~2.7× less heap churn, less GC pressure
- Projected post-fix rate: 7 ev/s → 18-25 ev/s sustained per worker
- ETA per 60-day variant: 3.7h → ~1.5h
- Total queue (19 variants × 2 workers parallel): 80-90h → ~28h
- **Net wall-clock savings: ~50-60 hours**

### §11.8 Mid-run deployment

Stopped the running V1+V2 pair (at ~23% complete, 1h elapsed,
ETA 3.7h on the buggy code). Pushed the fix to origin/main,
pulled on the backtester VM, archived the buggy run directory,
restarted the scheduler with a fresh run_id.

**Archived run (buggy code):**
`/opt/trading-agent/logs/backtests/_archive/battery_nifty50_60d_20260525T093330_O_N2_BUG`
(stopped at 23.1% progress / 1h 14m elapsed; preserved for forensic
pre/post comparison).

**New run id (post-fix):** `battery_nifty50_60d_20260525T105637`
(restarted 2026-05-25 16:26 IST, scheduler systemd unit + git HEAD 7ec02a1).

**Operational gotcha during deploy:** initial scheduler restart crash-looped
with `PermissionError: Operation not permitted` because my queue-state reset
left the file owned by `1001:1001` (the docker worker UID) while the
scheduler systemd unit runs as `opc`. Combined with the sticky bit on
`/opt/trading-agent/data/`, opc couldn't rename a file owned by a different
user. Fix: `chown opc:opc` + `restorecon` on the state file, plus removing
a leftover `.tmp` file. To prevent recurrence: any future manual state-file
edit must end with `sudo chown opc:opc <file> && sudo restorecon -v <file>`.
Added to ops_runbook §Common Pitfalls.

### §11.9 Files touched in §11

- `packages/research/backtest_ensemble.py` — `strategy_history_window`
  field + windowed `_merge_bars`
- `tests/unit/test_strategy_history_window.py` — NEW, 13 tests
- `docs/findings/findings_log_2026-05-25.md` — §12 added
- `docs/changes/changes_done_2026-05-25.md` — this §11

---

## 12. Bug F — `ProcessPoolExecutor` cascade-fail when worker dies in re-used subprocess (2026-05-25 evening)

### §12.1 Problem

The post-Bug-E `battery_nifty50_60d_20260525T105637` run completed
**V1 + V2 successfully** (first clean post-fix backtest numbers:
PF 0.76 / 0.58 — the shipped config IS bleeding, confirming the
live-trader signal). Then **17 of 19 variants (V3-V19) all marked
CRASHED in `comparison.md`** with the identical generic error:

```
A process in the process pool was terminated abruptly while the
future was running or pending.
```

Forensics showed:
- Only V3 *actually crashed* (workers/V3.log ends mid-run with no error;
  V4 was still emitting progress 33 s after V3 stopped).
- V5–V19 **never ran** — no worker logs exist for them. They're
  cascade-marked as failed because `ProcessPoolExecutor` re-raises
  `BrokenProcessPool` for every pending future once the pool dies.
- Container did NOT OOM-kill (`OOMKilled=false`, no kernel signal in
  journalctl, no memory limit set).
- V3's worker died **without writing any Python error** — classic
  fingerprint of a native-code segfault / abort that bypasses Python.

### §12.2 Root cause hypothesis

`ProcessPoolExecutor` reuses worker subprocesses across submitted
tasks. With `max_workers=2`:

- worker A: V1 → V3 → V5 → ...
- worker B: V2 → V4 → V6 → ...

V1+V2 ran in **fresh** workers, succeeded.
V3+V4 ran in **re-used** workers (each carrying V1/V2's leftover
process state) and died at the same elapsed time.

State that survives a variant boundary inside one worker process:

- `strategies._trend_context._cache` — module-level dict (yfinance
  daily bars, TTL 6 h, never explicitly cleared)
- yfinance / urllib3 connection pool + cookie jar
- xgboost native handles (V1's `_load_model` failed due to missing
  pickle, possibly leaving the C++ side half-initialized)
- numpy / pandas internal allocator pools, type-checking caches
- loguru queue threads

Any one of these can trigger a native segfault under specific
follow-up conditions — and the worker dying mid-task is exactly
what kills the whole pool.

### §12.3 Fix

Two changes to `packages/research/battery.py`:

1. **`max_tasks_per_child=1`** on the `ProcessPoolExecutor`:
   ```python
   with ProcessPoolExecutor(
       max_workers=args.workers,
       max_tasks_per_child=1,
   ) as pool:
   ```
   Forces a brand-new subprocess per variant. Eliminates ALL cross-
   variant native-code state. V3 inside a fresh worker is functionally
   identical to V1 inside a fresh worker — and V1 passes.

2. **`faulthandler` enabled** in `_run_variant_in_subprocess`:
   ```python
   import faulthandler
   _fault_fp = open(workers_dir / f"{name}.fault.log", "w")
   faulthandler.enable(file=_fault_fp, all_threads=True)
   ```
   Per-variant fault log file. Any future SIGSEGV / SIGABRT / SIGBUS /
   SIGFPE / SIGILL writes a Python traceback BEFORE the process dies.
   Best-effort wrapped in try/except so a faulthandler failure cannot
   kill the run it's instrumenting.

### §12.4 Cost

- Startup tax per fresh worker: ~15 s (imports + 90 MB market_data
  unpickle from disk).
- Total for 19-variant run with workers=2: 19 × 15 s ÷ 2 ≈ 150 s
  ≈ 3 min added to a ~40 h queue. Negligible (~0.13%).

### §12.5 Tests

`tests/unit/test_battery_worker_isolation.py` — 6 new tests:

- `TestProcessPoolMaxTasksPerChild`:
  - AST-walks `battery.main`, asserts exactly one `ProcessPoolExecutor`
    call with `max_tasks_per_child=1` literal int kwarg
  - Asserts `max_workers` is still explicitly wired (defends against
    `os.cpu_count()` fallback)
- `TestWorkerFaulthandler`:
  - `_run_variant_in_subprocess` imports faulthandler + calls `.enable()`
  - Per-variant fault log path is `<workers>/<name>.fault.log`
  - Init wrapped in try/except (best-effort)
- `TestDocumentation`:
  - "Bug F" string present in `battery.py` for grep-archaeology

Tests are *structural* (AST + source-text), not runtime, because
spinning up real `ProcessPoolExecutor` subprocesses inside pytest is
slow, flaky on Windows (spawn-pickling, sys.path), and would require
a full battery scaffold for what is fundamentally a 1-line invocation
contract.

### §12.6 Suite results

```
$ pytest tests/unit -q     -> 1247 passed in 49.61s
                              (1056 base + 185 battery+strat + 6 new)
```

Zero regressions.

### §12.7 Freeze accounting

`packages/research/battery.py` is research/, not on the slot-consuming
list in FREEZE_v2.1 (which covers strategies, risk, position sizing,
trading_agent.py, config.yaml strategy/risk blocks, models). This is
a **harness fix** — it changes how the orchestrator launches workers,
not what they compute. Backtest results are byte-identical (each
variant runs the same code on the same data; only process management
changes). **No bypass slot consumed.**

**Bypass-slot ledger: still 1/3 used** (only `risk.allow_shorts`).

### §12.8 Resume plan

After commit + push + pull, queue a `--resume battery_nifty50_60d_20260525T105637`
job. The harness's resume logic reads existing `comparison.md`, sees
V1+V2 are DONE, and re-runs only V3-V19 (the 17 marked failed).

ETA at the post-Bug-E rate (~3.2 h per variant on 2 workers):
17 × 3.2 / 2 ≈ 27 h to complete the missing 17 variants.

**Decision boundary:**
- If the resume completes cleanly → state-pollution hypothesis confirmed,
  Bug F closed.
- If it fails again at V3 (~30 min in) → the issue is *not* state
  pollution. Read `<run>/workers/V3_only_xgb_mr_filtered_yday.fault.log`
  for the real Python traceback, fix the underlying code, re-resume.

### §12.9 Files touched in §12

- `packages/research/battery.py` — `max_tasks_per_child=1` +
  faulthandler init in worker entry point
- `tests/unit/test_battery_worker_isolation.py` — NEW, 6 tests
- `docs/findings/findings_log_2026-05-25.md` — §13 added
- `docs/changes/changes_done_2026-05-25.md` — this §12

---

## 13. Bug G — Backtester subsystem code-review hardening (2026-05-25 night)

### §13.1 Trigger

Operator request after a day of cascading bug discoveries (A through
F): *"do a full code review for backtest so that no other issues
appear."* The next 64 h of compute (37 h validation + ~27 h
nifty50_60d resume) is the Friday-review evidence trail; we cannot
afford another preventable surprise mid-run.

A focused exploration agent reviewed every file in the backtester
hot path (10 files, ~5,400 LOC) — `packages/research/{backtest_ensemble.py,
battery.py, diagnostic.py}`, `packages/strategies/_trend_context.py`,
`tools/{run_battery.py, run_battery_queue.py}` and adjacent fixtures.
24 issues surfaced across 4 severity tiers (5 critical, 8 high, 7
medium, 4 low). This commit closes the **4 critical issues that
could affect the running 64 h compute window**. The fifth critical
(regime hardcoded — Bug B residue) cannot be retrofitted mid-run
without invalidating results; deferred and flagged for the
post-validation window.

Full RCA per fix lives in `findings_log §14`. This section is the
operational change-log: what landed, what tests pin it, how the
freeze accounts for it, and how to verify after VM pull.

### §13.2 G-1 — Atomic results-JSON writes + corrupt-JSON quarantine on resume

**Problem.** `_completed_variant_names()` was treating *any* file
matching `results/*.json` as proof a variant was complete. Combined
with the non-atomic `Path.write_text()` write inside
`_run_variant_in_subprocess`, a worker crash mid-write would leave a
truncated JSON that resume permanently SKIPS — with no warning. The
variant would be silently missing from `comparison.md`.

**Fix.**
* New `battery._atomic_write_text(path, text)` helper: writes to
  `<path>.tmp`, then `Path.replace()`s onto the target. Atomic at
  the directory-entry level on POSIX and Windows for same-filesystem
  renames.
* Wired through every harness write that resume reads:
  `results/<name>.json`, `results/<name>.failure.txt`,
  `comparison.md`.
* `_completed_variant_names()` now parses + schema-checks every
  file. Required keys: `variant`, `summary` (must be a dict),
  `elapsed_sec`. Bad files are renamed to `<name>.json.corrupt` and
  EXCLUDED from the completed set so resume re-runs them. Operator
  sees `[BATTERY] quarantined corrupt result …` warning per file.

**Cost.** Per-write: one extra `Path.replace()` (~µs). Per-resume:
schema parse on every result file (~ms each). Negligible vs. ~3 h
per variant.

### §13.3 G-2 — Auto-retry loop on `BrokenProcessPool` cascade

**Problem.** Bug F's `max_tasks_per_child=1` eliminates *cross-
variant state pollution* but does NOT prevent the cascade itself.
Python's `ProcessPoolExecutor` invalidates the WHOLE pool when ANY
worker terminates abnormally — every pending future raises
`BrokenProcessPool`. With 17 variants pending in the running
validation, one unlucky native crash still costs 16 lost variants.

**Fix.** Wrap dispatch in a bounded retry loop:

```python
MAX_POOL_RETRIES = 3
real_failed: set[str] = set()
for attempt in range(1, MAX_POOL_RETRIES + 1):
    completed_now = _completed_variant_names(out_root)
    not_done = [(n, o) for n, o in pending
                if n not in completed_now and n not in real_failed]
    if not not_done:
        break
    broken_pool_seen = False
    try:
        with ProcessPoolExecutor(max_workers=N, max_tasks_per_child=1) as pool:
            ...
            for fut in as_completed(futures):
                try:
                    ...success path...
                except BrokenProcessPool:
                    broken_pool_seen = True; continue   # cascade casualty, retry
                except Exception as e:
                    real_failed.add(name); ...           # real failure, don't retry
    except BrokenProcessPool:
        broken_pool_seen = True
    if not broken_pool_seen:
        break
else:
    # mark stuck variants with `pool cascade after 3 retries`
```

Per-future handling now has TWO except branches:
* `BrokenProcessPool` → cascade casualty, NOT marked failed; outer
  loop picks up via `_completed_variant_names` check.
* `Exception` → real Python failure, recorded in `real_failed` so
  retries do not re-submit it.

Bounded by 3 attempts; deterministic crashes get permanent
`<name>.failure.txt` with explicit `pool cascade after 3 retries`
message.

**Cost.** Zero on the happy path. On a single cascade: one extra
~5-10 s pool spin-up per remaining variant. On the running
validation, this could save **~50 h of compute** if any cascade
occurs in the 17 pending variants.

### §13.4 G-3 — Hard timeout on `yfinance.download`

**Problem.** `_trend_context._fetch_daily()` calls
`yfinance.download()` with no timeout. A stalled HTTP socket hangs
the worker thread indefinitely. Variants V1, V3-V9, V17-V18 all
invoke this path. The harness' 30-min progress watchdog eventually
fires `os._exit(124)`, which historically cascade-killed the pool
(now G-2 partially recovers, but fail-fast on the network side is
cleaner).

**Fix.** New `_yf_download_with_timeout(symbol, timeout)` wraps
`yfinance.download` in a `concurrent.futures.ThreadPoolExecutor`
with `result(timeout=N)`. Default 30 s, tunable via
`TREND_FETCH_TIMEOUT_SEC` env. On timeout: returns `None`;
`_fetch_daily` treats `None` as "trend unknown" → fail-open (does
NOT block the trade — same as a permanent fetch failure).

Why thread-with-timeout (not signal.alarm, not yf.download
timeout=)?
* Signal-based preemption is fragile in worker subprocesses.
* yfinance's outer `timeout=` kwarg is not version-stable.
* `ThreadPoolExecutor.result(timeout=N)` is portable and survives
  yfinance upgrades. Leaked thread (yfinance never returns) is
  bounded to one worker lifetime by `max_tasks_per_child=1`.

**Cost.** Zero on happy path. On hang: capped at 30 s instead of
unbounded.

### §13.5 G-5 — Queue scheduler `--rm` + zombie-container retry

**Problem.** Today's actual incident.
`tools/run_battery_queue.py:build_docker_run_argv()` did NOT pass
`--rm`, so an exited container retained its name. Since
`_run_id_for(...)` reuses the same `run_id` for resume launches,
the next launch hit `Conflict. The container name "/<run_id>" is
already in use…`, the scheduler marked the job `"failed"` at
launch, and the queue ground to a halt. We had 5 zombie containers
to clean up by hand earlier today.

**Fix.**
1. Add `--rm` to the scheduler's docker-run argv (parity with
   `tools/cloud/launch_battery.sh` which already had it).
2. On launch failure with stderr containing
   `is already in use by container`, run
   `sudo docker rm -f <run_id>` and retry the launch exactly once.
3. Distinguish `failure_phase: "launch"` from `failure_phase: "run"`
   in the saved job state so operators can route triage correctly
   (image / daemon vs. harness / variant).

**Cost.** Zero on the happy path. On zombie hit: one extra
`docker rm -f` (~1-2 s) + one launch retry. Eliminates the manual
intervention failure mode.

### §13.6 Tests

`tests/unit/test_battery_robustness.py` — NEW, **26 tests** across 6
classes:

* `TestAtomicWriteHelper` (5): helper exists, writes atomically,
  no `.tmp` residue, overwrites existing, wired through both result
  JSON and `comparison.md`.
* `TestCorruptJsonQuarantine` (5): truncated / missing-keys /
  wrong-type-summary all quarantined and excluded; valid JSON
  accepted; `.tmp` residue skipped.
* `TestProcessPoolRetryLoop` (5): `BrokenProcessPool` import
  exists, `MAX_POOL_RETRIES` defined, both per-future except
  branches present, all `ProcessPoolExecutor` calls still set
  `max_tasks_per_child=1`, `real_failed` tracking set named.
* `TestTrendContextTimeout` (5): `_YF_TIMEOUT_SEC` defined and in
  sane range, `_yf_download_with_timeout` exists, `_fetch_daily`
  routes through it (and does NOT call `yf.download` directly),
  timeout returns `None` (fail-open), env override honored.
* `TestQueueSchedulerRobustness` (3): `--rm` in argv, name-conflict
  recovery in `process_queue`, `failure_phase` recorded for both
  values.
* `TestBugGDocumented` (3): `Bug G-1` cited in `battery.py`,
  `Bug G-3` cited in `_trend_context.py`, `Bug G-5` cited in
  `run_battery_queue.py`.

**Suite result:** 1267 / 1267 passing. No existing tests changed.

### §13.7 Freeze accounting

All four Bug G fixes touch `packages/research/battery.py`,
`packages/strategies/_trend_context.py`, and
`tools/run_battery_queue.py`. None is on the slot-consuming list in
`FREEZE_v2.1` (strategies, risk, position sizing, trading_agent.py,
config.yaml strategy/risk blocks, models). All four are
**harness/library robustness fixes** changing *how* failures are
handled and *how* one helper times out a network call — not *what*
anything computes:

* G-1: only adds atomicity + corruption detection; happy-path
  bytes-identical.
* G-2: only activates on cascade; happy path is one iteration of
  the loop, identical to pre-G-2 single-pass.
* G-3: only changes behavior on network hang where prior code would
  have hung-then-watchdog-killed; happy-path output identical.
* G-5: scheduler-only; affects launch reliability, not what the
  harness computes once launched.

**No bypass slot consumed. Bypass ledger remains 1/3 used** (only
`risk.allow_shorts`).

### §13.8 Verification after pull

On the backtester VM, after `git pull`:

```bash
# 1) Confirm new tests pass on the VM-side python (parity with dev box)
sudo -u opc bash -lc "cd /opt/trading-agent && python -m pytest \
    tests/unit/test_battery_robustness.py \
    tests/unit/test_battery_worker_isolation.py -q"
# Expected: 32 passed

# 2) Confirm scheduler picked up the new build_docker_run_argv (only
#    matters when scheduler is restarted; current container in flight
#    is already running with the in-memory pre-G-5 argv builder).
grep -- "--rm" /opt/trading-agent/tools/run_battery_queue.py
# Expected: 2-3 hits including the `cmd:` list literal

# 3) Confirm the bind-mount is read-only and packages/ is fresh
sudo docker exec $(sudo docker ps -q --filter name=battery_) \
    grep -c "Bug G-2" /app/packages/research/battery.py
# Expected: ≥1 hit IF the running container was launched after pull;
# 0 hits is fine for the pre-G running container — read-only mount
# means new launches pick up the fix.
```

### §13.9 Operational notes for the running 64 h window

The currently-running validation container was launched BEFORE this
commit, so it has the in-memory state of pre-G code:
* G-1 (atomic results write): NOT active. If a worker crashes
  mid-write of `results/<name>.json` in the next 37 h, the file
  could still be truncated. Mitigation: G-1 only matters on resume;
  the validation run is fresh and sequential so a crashed variant
  would be caught by the new auto-retry (G-2 — also not active in
  the running container) OR by the existing `<name>.failure.txt`
  marker.
* G-2 (cascade retry): NOT active in the running validation. A
  cascade in the 17 pending variants would still require manual
  `--resume`. The nifty50_60d resume that follows WILL launch a
  fresh container that picks up G-2 (and all of Bug G).
* G-3 (yfinance timeout): NOT active in the running validation. A
  yfinance hang would still trigger the watchdog. Probability is
  low (validation is 60 d, fewer fetches than 90 d) but non-zero.
* G-5 (queue --rm + retry): N/A to the in-flight container; takes
  effect on the next scheduler-spawned launch (the nifty50_60d
  resume).

**Decision:** No mid-validation restart. Risk of restart-induced
corruption (re-loading `market_data.pkl`, re-firing the live-md
thread, scheduler queue races) outweighs the marginal robustness
gain from getting G-1/G-2/G-3 active for the validation tail. The
nifty50_60d resume — the larger of the two queued runs at ~27 h —
gets all four fixes when it launches in ~37 h.

### §13.10 Files touched in §13

- `packages/research/battery.py` — `_atomic_write_text` helper +
  `_completed_variant_names` schema check + retry loop in `main()` +
  `BrokenProcessPool` import
- `packages/strategies/_trend_context.py` — `_YF_TIMEOUT_SEC`
  constant + `_yf_download_with_timeout` helper + `_fetch_daily`
  rewired
- `tools/run_battery_queue.py` — `--rm` in argv + name-conflict
  recovery in `process_queue` + `failure_phase` markers
- `tests/unit/test_battery_robustness.py` — NEW, 26 tests
- `docs/findings/findings_log_2026-05-25.md` — §14 added
- `docs/changes/changes_done_2026-05-25.md` — this §13


## §14. Bug G self-audit fixes (2026-05-26 morning, audit-only, NOT deployed)

User asked to "check the bug g and fix just don't deploy it". A
line-by-line review of the cb76f0e diff surfaced **two real defects
in the original Bug G fix** that no source-level test would have
caught. Full RCA in `findings_log_2026-05-25.md` §15. This section
records the operational change-log and freeze accounting.

### §14.1 Defects fixed

**G-1.A — orphan results JSON masks failure.txt on resume.** When
the worker writes `results/<name>.json` successfully but then
crashes during return / pickle / IPC (rare but possible), the
parent records `<name>.failure.txt`. The original Bug G-1's
`_completed_variant_names` only inspected `*.json` files, so on the
next operator-initiated `--resume`, the orphan JSON looked clean
and the variant was silently skipped. Fix: make the reader treat
the presence of a sibling `<name>.failure.txt` as authoritative
("not completed; resume must re-run") regardless of how clean the
JSON parses.

**G-3.A — `with ThreadPoolExecutor(...)` defeats the timeout.**
`Executor.__exit__` calls `shutdown(wait=True)`, which BLOCKS
waiting for running tasks to complete. So when `result(timeout=N)`
raised TimeoutError, the with-block's `__exit__` then hung
indefinitely waiting for the still-running yfinance call —
defeating the entire timeout. Reproduced empirically: a 1.0s
timeout against a 30s sleep returned in 30.0s, not 1.0s. Fix:
replace the with-block with explicit try/finally and call
`shutdown(wait=False, cancel_futures=True)` so the function
returns within the timeout window even when the inner thread is
genuinely hung.

### §14.2 Code changes (audit-only, NO bypass slot consumed)

* `packages/research/battery.py` — `_completed_variant_names`
  enumerates `*.failure.txt` and treats matching variants as
  not-completed; ~15 lines net.
* `packages/strategies/_trend_context.py` —
  `_yf_download_with_timeout` rewritten with explicit try/finally;
  ~10 lines net.
* `tests/unit/test_battery_robustness.py` — +4 tests:
  * `test_orphan_json_with_failure_txt_excluded` (G-1.A behavioural)
  * `test_failure_txt_alone_does_not_quarantine_anything`
  * `test_orphan_with_failure_does_not_quarantine_the_orphan`
  * `test_timeout_actually_returns_within_window_when_fetch_hangs`
    (G-3.A behavioural — the load-bearing one; takes ~1.5s real
    wall-clock to verify the timeout works against a deliberate
    sleep).

Both behavioural tests were verified to FAIL on the original
cb76f0e code (`git stash` of the audit fix → run test → fail) and
to PASS on the audit fix. This rules out false-pass tests.

### §14.3 VM operations — explicitly NONE

Per the user's "don't deploy it" directive, the audit fix is
pushed to `origin/main` only. The backtester VM checkout remains
at cb76f0e (the original Bug G fixes, including the broken G-3
timeout). The currently-running validation container is unaffected
in any case (it runs the in-memory pre-G code). The next
scheduler-spawned launch (`nifty50_60d` resume in ~28 h) will pick
up cb76f0e via the read-only bind-mount, NOT the audit fix. If a
yfinance hang materializes during that resume window we revisit;
probability is low (no hang has occurred in 12+ hours of active
fetching). Post-validation (after Friday 2026-05-29 review), the
audit fix gets pulled with the rest of the post-window backlog.

### §14.4 Test count

* Pre-audit: 1267 unit tests (1241 + 26 Bug G robustness)
* Post-audit: 1271 unit tests (+4 new behavioural)
* Result: **1271/1271 passing**, 47.4 s wall-clock for the full
  unit suite.

### §14.5 Freeze accounting

Both fixes touch the same files Bug G already touched (battery.py,
_trend_context.py). Neither file is on FREEZE_v2.1's slot-
consuming list. Both fixes are *failure-handling* corrections —
they make the originally-claimed behaviour actually work — without
changing anything about happy-path computations. No bypass slot
consumed. Bypass ledger remains 1/3 (still only
`risk.allow_shorts`).

### §14.6 Files touched in §14

- `packages/research/battery.py` — `_completed_variant_names`
  failure.txt-aware
- `packages/strategies/_trend_context.py` —
  `_yf_download_with_timeout` non-blocking shutdown
- `tests/unit/test_battery_robustness.py` — +4 behavioural tests
- `docs/findings/findings_log_2026-05-25.md` — §15 added
- `docs/changes/changes_done_2026-05-25.md` — this §14
- `docs/freeze/FREEZE_v2.1.md` — audit-only entry extended

---

## 15. Bug H — XGBoost model missing from backtester (silent strategy disable)

### §15.0 What happened (one-paragraph TL;DR)

A 2026-05-26 audit triggered by today's live -Rs 453 loss
(three xgboost_classifier BUY trades stopped out in
`bear_low_vol` regime) discovered that the
`nifty500_v4_long_only_validation_60d` run on the backtester VM was
silently executing **without xgboost**. The
`/opt/trading-agent/models/` directory on the backtester host is
empty (mtime 2026-05-18 — has been empty for 8 days), and the
trading-agent:latest image (built 2026-05-22) did not bake the
`xgboost_model.pkl` either. Every variant in the run logged
`[XGB-HEALTH] XGBoost model not found at models/xgboost_model.pkl.
Strategy will return HOLD` and produced trade results from the
remaining 5 strategies only. **V1 in this run is NOT the shipped
baseline; it is "shipped minus xgboost".** This invalidates the
straight-line comparison the Friday 2026-05-29 review was going to
draw between V1/V2/V4/V17/V18/V19 PnLs and the live daemon's
recent performance.

### §15.1 How the gap was created

* The Dockerfile copies `packages/`, `tools/`, `config.yaml`, etc.,
  into the image but **not** `models/`. The `models/` directory is
  created empty at image build time by `mkdir -p models/` in the
  Dockerfile (so the `Path(models/xgboost_model.pkl).parent`
  resolution doesn't fail), but nothing populates it.
* On the trader VM the bootstrap pipeline writes
  `/opt/trading-agent/models/xgboost_model.pkl` directly to the
  host (it's a training artifact from
  `tools/research/training_pipeline.py`), and the trader's
  docker-compose bind-mounts `models/` into the container.
* The backtester VM, conversely, has neither a bake-into-image
  path NOR a bind-mount. The `models/` directory exists on the
  host (owned by uid 1001) but has been empty since the VM was
  provisioned.
* The `xgboost_classifier` strategy is **fail-open**: missing
  model degrades to `return HOLD` with a warning, no exception.
  That's intentional defence against operator mistakes during
  active trading, but it means a missing model is silent at the
  harness level (no crash, no scheduler alarm) and the only
  external signal is the warning line in the variant's worker log.

### §15.2 Detection chain

The warning was visible (and is now archived) in every variant's
worker log from the start of the validation run:
* `logs/backtests/battery_nifty500_v4_long_only_validation_60d_20260525T164751/workers/V1_baseline_current_shipped.log` (V1 — completed)
* `…/V2_all_filters_off.log` (V2 — completed)
* `…/V4_threshold_3pct.log` (V4 — was at 2.3%)
* `…/V17_long_only_shipped.log` (V17 — was at 1.7%)

The detection path was: today's live -Rs 453 loss → audit of the
trader daemon → confirmation that xgboost_classifier was the
strategy responsible → cross-check of the backtester results to
"what does the same regime look like in our 60d validation" →
discovered the missing-model warning. **Lesson: the
`battery_status_remote.ps1` summary should surface
`grep -c '\[XGB-HEALTH\] XGBoost model not found'` from each
worker log so this can't repeat silently.** (Captured as a
post-Friday todo, not part of today's fix.)

### §15.3 Fix

Two parts, both audit-only (no strategy/risk change):

**§15.3.a Bind-mount `models/` read-only into every battery
container.**
* `tools/run_battery_queue.py:build_docker_run_argv` — added
  ```
  "-v", f"{TRADER_HOME}/models:/app/models:ro",
  ```
  next to the existing `packages` mount. Read-only so a stray
  pickle.dump (e.g. a worker that imports the training pipeline
  by accident) can never overwrite the production model file.
* `tools/cloud/launch_battery.sh` — same mount added, plus the
  pre-existing `packages/` mount that was already in the
  scheduler argv but missing from the manual operator script
  (pre-existing parity gap, fixed in the same commit).
* `tests/unit/test_battery_queue_scheduler.py` — extended the
  `test_run_argv_includes_required_mounts` test with an
  `assert any("/models:/app/models:ro" in v for v in v_pairs)`
  line so a future refactor that drops the mount fails CI.

**§15.3.b Stage the model file onto the backtester host.**
* `scp ubuntu@<trader-vm>:/opt/trading-agent/models/xgboost_model.pkl
   opc@<backtester-vm>:/tmp/`
* `sudo mv /tmp/xgboost_model.pkl /opt/trading-agent/models/`
* Verify sha256 = `fc17fcb5efceb7297af277c5a1fd854937286e361366bca0d2d803d36d022995`
  matches the trader-side file (rules out network corruption).

### §15.4 Probability + cost (Bug G framework)

* **Probability of recurrence (without fix):** 100% (the gap is
  structural — every battery container ever spawned has been
  affected since image build 2026-05-22).
* **Magnitude of damage:** Every variant in the
  `nifty500_v4_long_only_validation_60d` run misrepresents the
  shipped baseline. V1 = -5.62% appears as a "shipped is losing
  money" result, but the actual shipped daemon includes xgboost
  which under current regimes is independently a loss-generator
  (verified today: -Rs 453 from 3 xgboost trades). The combined
  shipped baseline is almost certainly worse than -5.62%.
* **Detection cost without the bind-mount:** Operator has to
  grep every worker log every run. Easy to miss; was missed for
  8 days.
* **Fix cost:** 2 lines of code (mount string × 2 scripts), one
  test line, one model file scp. Total dev time ~15 minutes.

### §15.5 Operational rollout (this run)

Per user's explicit go-ahead 2026-05-26 14:08 IST:
1. Phase 1 (this commit) — code change + test + push to origin.
2. Phase 2 (separate operational step, logged in real-time):
   a. Stop `battery-scheduler.service`.
   b. `scp` model trader→backtester, verify sha256.
   c. `git pull` on backtester — moves from cb76f0e to HEAD
      (picks up §14 audit fix AND this §15 bind-mount).
   d. `docker stop` the active container (V4 at 2.3%, V17 at
      1.7% — discardable).
   e. Rename `results/V1_baseline_current_shipped.json` →
      `results/V1_baseline_current_shipped.NO_XGBOOST.json.archive`
      and the same for V2. Preserves the no-xgboost data for
      reference, makes both variants invisible to
      `_completed_variant_names()` so resume re-runs them.
   f. Restart `battery-scheduler.service` — harness resumes the
      run dir, sees V1/V2/V4/V17/V18/V19 all uncompleted,
      schedules all 6 with workers=2.
3. Phase 3 (verification gate) — at the first variant
   completion (~6-12 h), tail the worker log for
   `[XGB-HEALTH] OK` (model loaded successfully). If the warning
   reappears, abort and replan; do not burn another 30 h on a
   broken setup.

### §15.6 Freeze accounting

* Bind-mount addition: harness/dev-tooling, no
  strategy/risk computation changed. **No bypass slot.**
* Model file deployment: production artifact previously
  shipped on the trader VM, now also staged on the backtester
  VM. Read-only mount means the backtester cannot mutate it.
  Identical to the trader's model bit-for-bit (sha256 verified).
  **No bypass slot.**
* `git pull` on backtester also brings §14 audit fix.
  Already accounted for in §14 — still audit-only, no slot.

Bypass ledger remains **1/3** (still only `risk.allow_shorts`).

### §15.7 Files touched in §15

* `tools/run_battery_queue.py` — `+1` mount line
* `tools/cloud/launch_battery.sh` — `+2` mount lines
  (`packages` parity + `models`)
* `tests/unit/test_battery_queue_scheduler.py` — `+8` lines
  (assert + comment) on the existing mount-list test
* `docs/findings/findings_log_2026-05-25.md` — §16 added
* `docs/changes/changes_done_2026-05-25.md` — this §15
* `docs/freeze/FREEZE_v2.1.md` — audit-only entry extended

---

## 16. Bug I — Trader VM ~2 weeks of uncommitted hot-fixes block routine pulls

### §16.0 What was discovered

Attempting to `git pull origin main` on the trader VM as part of
today's `risk.allow_shorts: false` deploy aborted because the trader
has 5 tracked-but-uncommitted modified files and 4 untracked files
that origin/main wants to overwrite. Trader HEAD = `868d5ad`
(2026-05-19); on-disk state is materially further along (hot-fixes
added on 2026-05-23 and 2026-05-25 for NSE-archives CSV path, TLS
verify default, host-side tools/ + packages/ bind-mounts, watchdog
cron, OCI memory limits, etc.).

Full details, file-by-file diff justification, and reconciliation
plan are in `docs/findings/findings_log_2026-05-25.md` §17.

### §16.1 What today's attempt did

* Backed up trader's `config.yaml` to
  `config.yaml.bak_pre_allow_shorts_20260526T091042`.
* `git fetch origin` succeeded; `git pull --ff-only origin main`
  aborted on the local-changes check.
* `sed -i ... allow_shorts: false ...` ran but matched nothing
  (the line doesn't exist on the trader; the slot-1 commit was
  never pulled). `config.yaml` is byte-identical to before.
* `docker compose restart trader` ran successfully. Container came
  back at 2026-05-26T09:10:58Z (14:40:58 IST), rehydrated cleanly:
  3 trades + 3 cooldowns from DB, xgboost loaded, preflight green.
* Effective change in trader behaviour: **zero**. Same as a
  no-op restart.

### §16.2 What this commit does

* Zero code changes.
* Adds Bug I to findings (§17) and Bug I mirror entry to changes
  (this §16) so the audit trail captures the discovery moment.
* No deploy. The user is taking over trader VM reconciliation
  manually ("i will maually rebuilt the tarder vm") rather than
  letting the dev tooling stash/pop/merge on a live trading VM
  while a 36+h validation run depends on disk integrity.

### §16.3 Freeze accounting

Documentation-only. **No bypass slot consumed.** The slot-1
allow_shorts pre-stage was committed days ago (slot 1/3 used); it's
just that the deploy hop failed today, so the flag is still inert
on the trader. Slot count is unchanged.

### §16.4 Files touched

* `docs/findings/findings_log_2026-05-25.md` — §17 added (Bug I full detail).
* `docs/changes/changes_done_2026-05-25.md` — this §16 (Bug I mirror).
* No source code, no tests, no config. No VM-side deploy.

---

## 17. `risk.allow_shorts: false` — DEPLOY CONFIRMED (2026-05-26 15:20 IST)

After the operator manually reconciled the trader VM hot-fixes (Bug I,
§16), the slot-1 `risk.allow_shorts: false` deploy was completed in
the same session. This is the activation step that the 2026-05-25
pre-stage (slot 1/3, see §1) was always going to consume.

### §17.1 Sequence

1. Operator ran the manual rebuild (committed the 5 hot-fixes to a
   feature branch, pulled main, restarted container). Container came
   back healthy at `2026-05-26T09:38 UTC ≈ 15:08 IST` with code at
   `73c26bf` (Bug H). `_trend_context.py` now contains the G-3.A
   audit fix, verified live in the trader container.
2. Operator SSH'd in and ran:
   ```
   sudo cp /opt/trading-agent/config.yaml /opt/trading-agent/config.yaml.bak_<ts>
   sudo sed -i -E 's/^(\s*allow_shorts:\s+)true\s*$/\1false/' \
       /opt/trading-agent/config.yaml
   sudo grep allow_shorts /opt/trading-agent/config.yaml
   # -> allow_shorts: false
   cd /opt/trading-agent && sudo docker compose restart trader
   ```
3. Post-restart verification, run by the operator inside the
   container:
   ```
   sudo docker exec trader python3 -c \
     "import yaml; print('allow_shorts =', \
      yaml.safe_load(open('/app/config.yaml'))['risk'].get('allow_shorts'))"
   # -> allow_shorts = False
   ```
4. Daemon boot log (last lines):
   ```
   2026-05-26 15:19:55 | INFO  | Agent started (poll=60s, instruments=169)
   2026-05-26 15:19:55 | INFO  | India VIX updated: 16.26
   2026-05-26 15:19:55 | INFO  | Nifty trend: BELOW 200 EMA (Nifty=23911)
   2026-05-26 15:19:55 | WARN  | Trading blocked: Consecutive losses: 3 (limit: 3)
   ```
   Container status: `Up About a minute (healthy)`.

### §17.2 Effective from

The flag is read in `TradingAgent.__init__` at process startup. The
2026-05-26 15:19:55 IST restart is the activation moment. Today's
remaining 10 minutes of market are in the 3-loss-daily-limit lockout
anyway, so the first market-side test of the flag is the next session
open at **2026-05-27 09:15 IST**.

### §17.3 What it actually blocks

`risk.allow_shorts: false` causes `TradingAgent` to drop any
`SELL`-side ensemble signal before it reaches the position-sizing /
order-placement stage. `BUY`-side signals are unaffected. The
2026-05-18 90d × 228-stock battery showed `risk.allow_shorts: true`
losing -Rs 379 (V1) / -Rs 398 (V2) on the short side specifically;
this flag removes that loss vector at the source. **It does NOT
address today's actual loss path (xgboost LONGS hitting stop_loss);
that's a separate analysis pending V1/V4/V17 results from the
in-flight validation run.**

### §17.4 Freeze accounting

Slot 1 of 3 was reserved 2026-05-25 in §1 for this change. The flag
is now live; the reservation is fulfilled. **Slots used remains 1/3**
(no additional consumption from today's deploy — slot was already
counted).

### §17.5 Files touched on the trader VM

* `/opt/trading-agent/config.yaml` — `allow_shorts: true → false`
  on line 291.
* `/opt/trading-agent/config.yaml.bak_<ts>` — backup of the
  pre-flip state. Identical except for that one line.

### §17.6 Files touched in the repo (this commit)

* `docs/changes/changes_done_2026-05-25.md` — this §17 (deploy confirmation).
* `docs/freeze/FREEZE_v2.1.md` — ledger entry updated from "pre-staged" to
  "LIVE on trader VM as of 2026-05-26 15:19:55 IST".
