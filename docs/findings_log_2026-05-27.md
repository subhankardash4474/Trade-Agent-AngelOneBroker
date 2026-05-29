# Findings Log — 2026-05-27 (Day-11 of Freeze v2.1)

**Author:** Operator + automated audit + investigative agent
**Status:** Living document, append-only
**Purpose:** Capture today's operational findings (Bug J, slot-1 regression),
the Bug I closure verdict, and the kick-off of the 5-day diagnostic sprint
proposed by the advisor memo on the morning of 2026-05-27.

Continuation of `docs/findings_log_2026-05-25.md` (sections 1–17). New
numbering in this file starts at 1 for local readability; cross-references
to the prior log use the form `findings_log_2026-05-25.md §N`.

---

## Executive summary (TL;DR)

Six findings today. The big one is **§5: the xgboost model in production
is provably broken**, has been since at least 2026-05-11, and the regime
gate has been the sole circuit breaker preventing disaster (11,605/11,775
xgb BUYs rejected on 2026-05-19 alone). Acted on it: **slot-2 consumed,
xgboost disabled live at 11:26:38 IST**.

1. **§1 Bug J — `bootstrap_backtester.sh` chowns host data to UID 1001**,
   breaking the host-side scheduler running as `opc`. Workaround applied;
   permanent fix queued for sprint Day 4.
2. **§2 Slot-1 regression — `risk.allow_shorts: false` reverted to `true`
   on the trader VM ~16 hours after the 2026-05-26 deploy** because the
   value was sed-edited on the VM only, never committed. Re-fixed manually
   on the VM, then made durable in git as commit `8e1e926`.
3. **§3 Bug I closure verdict — the 5 uncommitted trader VM hot-fixes are
   confirmed ops/observability scope only**, not strategy-affecting.
   Manual reconciliation completed by operator on 2026-05-26. Live trade
   record from May 13 → May 25 remains valid evidence about freeze-v2.1
   behaviour. Diff archived via `73c26bf` merge into main.
4. **§4 Diagnostic sprint kicked off** per the 2026-05-27 morning advisor
   memo. 10 hypotheses, 5-day Option-A schedule. First two observability
   patches deployed (commit `e1df9e8`).
5. **§5 Forensic audit — XGBoost broken model**: independently verified
   ~95% SELL → 100% BUY directional flip on 2026-05-11 (model trained in
   the 2026-05-14 14:55 IST panic patch, commit `35adcd2c`). 4 known
   training-pipeline bugs are fixed in code but none applied to the .pkl
   on disk. Acted: commit `f32009c` removes `xgboost_classifier` from
   `strategies.active`; trader VM redeployed + verified at 11:26:38 IST.
   **Slot-2 consumed: critical-bug-fix bypass.** Backtester scheduler +
   in-flight worker container stopped to prevent further tainted compute.
6. **§6 Trades.csv hygiene** — 38 manual_test rows (ZZTEST/ZZTEST2,
   2026-05-26 16:45–16:53 IST, falsely tagged `strategy=mean_reversion`)
   moved to `logs/trades_manual_test_archive_2026-05-26.csv`. The
   remaining 31 real trades span 2026-05-12 → 2026-05-26; the last
   3 (HFCL/TATAINVEST/TATACHEM, all xgb BUYs, total −₹453.04) are
   exactly the audit's "May 26 long-side famine break" smoking gun.

Net state at end-of-day-11:
- **Bypass ledger: 2 / 3 used.** Slot 1 (allow_shorts: false), slot 2
  (xgboost disabled). Slot 3 reserved for the eventual retrained-model
  redeploy.
- **Live strategies: 4** (rsi_momentum, vwap_bounce, opening_range_breakout,
  supertrend_follow). In `bear_low_vol`/`bear_high_vol` with `allow_shorts:
  false`, expected behaviour today is minimal-to-zero new entries.
- **Backtester: paused.** The V3-V19 re-run was burning compute on the
  same broken pkl bind-mounted; resume requires retrain first.

---

**Update 2026-05-28 (market holiday).** Three additional sections
landed during the holiday window while the trader VM idled:

7. **§7 Strategy hot-path performance sprint (P-03/P-04/P-11)** -- byte-
   identical refactors of SupertrendFollow + 5 rule-based strategies +
   LSTM. Backtester throughput jumped from 19-40 ev/s to 75-104 ev/s.
   Honest attribution: ~2x from the xgboost-disable (§5) cutting the
   per-event ML cost, ~1.3-1.5x from the refactors themselves. 1395
   unit tests green.
8. **§8 Battery queue trim** -- old 6-job queue would have spent ~160h
   re-validating the broken-pkl ensemble post-§5. Trimmed to 3 jobs
   (~36h total). The remaining jobs were intended as: slot #1 (50
   stocks 60d, 19 variants), slot #2 (232 stocks 60d, 6 variants),
   slot #3 (232 stocks holdout-30d, 19 variants).
9. **§9 Bug K -- `--holdout-window-days` / `--train-window-days`
   silently ignored in parallel-worker path.** Caught 2026-05-28
   11:55 IST: slot #3 of the trimmed queue produced byte-identical
   V1+V2 results to slot #2, exposing that the slice logic in
   `battery.py:1305-1334` runs in main *after* the market_data
   cache is saved -- workers reload pre-slice data and never see
   the slice. Audit-only research-tool defect, no live-trader
   impact. Fix queued for post-Friday. Slot #3 reframed as
   "wider variant sweep on 232 stocks" rather than a p-hack
   guard. Bug J's permanent fix (§1.5) also landed today --
   bootstrap script three-way ownership split + writer probes
   + 7 unit tests. Trader VM trades.csv verified clean (no
   manual_test pollution, no archive needed).
10. **§12 Bug L -- `NSE_HOLIDAYS` missing 2026-05-28 (Bakri Eid).**
    The trader daemon ran a full ~7h trading pipeline on a market
    holiday because the date was absent from
    `packages/core/data_handler.py:NSE_HOLIDAYS`. Damage was zero
    (defensive stack absorbed: opening-lockout + allow_shorts:false
    + xgb-disabled + paper-mode). Cross-source diff revealed the
    pre-fix 2026 set was actually wrong in 16 of 18 entries (9
    spurious + 7 missing -- the next gap is 2026-06-26 Muharram in
    29 days). Fixed today: rewrote 2026 calendar with festival-name
    inline comments + curator contract, added 20 calendar tests,
    added a YELLOW POSSIBLE_MISSED_HOLIDAY detector to the audit
    checkpoint with 14 tests of its own. Full unit suite:
    **1436/1436 green**. Audit-only, no freeze slot consumed.

**Update 2026-05-29 (Friday morning).**

11. **§13 Audit-2026-05-28 follow-up — Phase 1 of 5 landed.** 22
    findings closed in code (16 OBS, 4 PERF, 2 NUM, 1 STATE,
    2 ORD, 2 CONC). 20 new regression tests, full suite
    1456/1456 green. NOT deployed; freeze slot preserved.
12. **§14 Audit-2026-05-28 follow-up — Phase 2 of 5 landed.** 6
    findings closed in code: ORD-01/STATE-01 (wait-for-terminal +
    broker `averageprice` truth), ORD-02 (pre-retry orderBook
    idempotency probe), ORD-03 (atomic-entry rollback on portfolio
    failure), STATE-02 (broker-only position detection at boot),
    OBS-05 (boot reconcile fail-CLOSED with operator ack file).
    27 new regression tests, full suite **1483/1483 green**. NOT
    deployed; freeze slot preserved. **6 of 8 audit-tagged
    Critical findings now FIXED in code** (only NUM-01 and PERF-01
    remain, both Phase 4).

**Update 2026-05-29 14:08 IST (Friday afternoon).**

13. **§22 Diagnostic-sprint Friday read-out — V15 transfer test
    = FAIL.** Slot #3's V15 (mr+xgb only) result landed at
    10:26 IST: 444 trades, WR 47.3%, **PnL -₹326, PF 0.94**,
    MaxDD 8.8%, Ret% -3.23%. Per `friday_review_2026-05-29.md
    §7` decision matrix `PF < 0.95 on 232 stocks` row →
    **slot-1 V15's +₹10 / PF 1.02 was small-universe noise;
    XGBoost retrain DEFERRED INDEFINITELY.** No V* variant
    profitable; no candidate promoted to live; bypass slot-3
    NOT consumed. **Capital stays paused under freeze-v2.1.**
    Backlog reorder: H3 entry-lag forensic and H1 regime
    classifier diagnostic promoted to next-sprint top
    priorities; retrain stays documented but is no longer
    next-up. V18/V19 still in flight (~16:00 / ~17:00 IST
    ETA tonight); informational only and cannot change the
    verdict.

---

## 1. Bug J — `tools/cloud/bootstrap_backtester.sh` chowns to container UID, breaking host-side scheduler

### 1.1 Discovery context

While fresh-deploying the backtester VM on 2026-05-27 after the 2026-05-26
purge, `battery-scheduler.service` (running as host user `opc`) failed
its first cycle with:

```
PermissionError: [Errno 13] Permission denied:
  '/opt/trading-agent/data/battery_queue_state.json.tmp'
```

The scheduler is a *host-side* Python process (systemd unit, not a
container). It writes the queue checkpoint to `data/` on the host
filesystem so a host restart doesn't lose progress. The container then
sees that checkpoint via the standard `./data:/app/data:rw` bind mount.

### 1.2 Root cause

`tools/cloud/bootstrap_backtester.sh` line 159:

```bash
sudo chown -R 1001:1001 ${TRADER_HOME}/logs ${TRADER_HOME}/data ${TRADER_HOME}/models
```

The bootstrap script unconditionally `chown`s those three directories to
**UID 1001** — the *container's* `trader` user, which exists *only inside
the trader image*. On the host, UID 1001 maps to an unrelated (or
non-existent) system user. The host scheduler runs as `opc` (UID 1000),
which has no write permission on a 1001-owned tree.

This is a copy-paste from the *trader-VM* bootstrap, where the host has
no scheduler and the only writer is the container. On the backtester VM
the host has a writer (the scheduler) **and** the container has a writer
(the battery workers), and they need different UIDs.

### 1.3 What was actually wanted

Three-way ownership split:

| Directory | Writers | Correct owner |
|---|---|---|
| `data/` | host scheduler (opc) reads/writes queue state | `opc:opc` (1000) |
| `data/research/overnight_batteries/` | container workers write per-run artifacts | `1001:1001` |
| `logs/battery_scheduler/` | host scheduler writes operator log | `opc:opc` |
| `logs/<run>/` | container workers write per-run trade logs | `1001:1001` |
| `models/` | both read-only | either user fine; left at `1001:1001` |

### 1.4 Workaround applied today (already live)

The fix was applied out-of-band by an ad-hoc script (`.tmp_bt_fix_perms.sh`,
which has since been removed). Concretely:

```bash
sudo chown -R opc:opc /opt/trading-agent/data
sudo chown -R 1001:1001 /opt/trading-agent/data/research
sudo chown -R opc:opc /opt/trading-agent/logs/battery_scheduler
```

This is what the backtester VM is running on right now. The fix is
**volatile** — if `bootstrap_backtester.sh` is re-run (e.g. a future
fresh deploy), the bug re-appears.

### 1.5 Permanent fix — **DONE 2026-05-28**

Landed on `main` during the holiday holiday-window backtester sweep.
Three deliverables:

1. **`tools/cloud/bootstrap_backtester.sh` rewritten step [4/8]** to
   apply the three-way ownership split documented in §1.3:
   * `data/` and `logs/battery_scheduler/` -> `$USER:$USER` (host-owned)
   * `data/research/` and `logs/backtests/` -> `1001:1001` (container)
   * `models/` -> `1001:1001` (read-only by both, container default)
   The new block carries a 30-line comment that links back to this
   finding and explains the two-writer rationale, so a future
   maintainer sees the trap before re-introducing it.

2. **Two new self-verification steps [7/8] and [8/8]** added to the
   bootstrap. After build + smoke-test the script exercises BOTH
   writers end-to-end:
   * **Host-side probe:** `touch data/.bug_j_probe_host && rm ...` and
     same for `logs/battery_scheduler/`. Runs as the bootstrap SSH
     user.
   * **Container-side probe:** `docker run --rm ... bash -c 'mkdir
     /app/logs/backtests/.bug_j_probe && rmdir ...'` and same for
     `data/research/`. Runs as in-container UID 1001.
   If either probe fails, the script exits non-zero with a clear "cannot
   write here" message. Bug J would have been caught on day 1 by these
   probes -- they're the cheapest possible regression guard.

3. **`tests/unit/test_bootstrap_backtester_perms.py`** -- 7 file-text
   assertions on the bootstrap script:
   * `test_no_blanket_1001_chown_of_full_tree` -- explicitly asserts
     the broken pre-fix line is gone (would catch the exact regression).
   * `test_data_root_is_host_owned`,
     `test_battery_scheduler_log_dir_is_host_owned` -- assert the host-
     owned paths are chown'd to `$USER`.
   * `test_logs_backtests_is_container_owned`,
     `test_models_dir_chowned_for_container_read` -- assert the
     container-owned paths are chown'd to `1001:1001` and that the
     1001 chown command actually includes the right paths.
   * `test_writer_probes_present` -- pins the two probes from step
     [7/8] / [8/8].
   * `test_bug_j_documented_in_script` -- asserts the script
     references "Bug J" and `findings_log_2026-05-27.md` so a future
     maintainer can grep their way to root cause.

   All 7 tests pass on `main`. The `_extract_chown_block` helper
   matches `sudo chown -R <owner>` (with sudo prefix) so it doesn't
   pick up the comment-block discussion of the pre-fix behaviour.

**Verification on the backtester VM:**
* The currently-live VM is operating on the workaround from §1.4 (out-
  of-band chown applied 2026-05-27). The fix only affects FRESH
  bootstraps -- it doesn't touch the running VM.
* On any future fresh OCI provision, the bootstrap will land the
  three-way split natively and self-verify.
* `bash -n tools/cloud/bootstrap_backtester.sh` on the backtester VM
  passes (syntax OK after the patch).

Cost actual: ~50 lines of bash (chown + comments + 2 probe steps),
+ 1 unit test file (7 tests). Total ~25 min of work. No freeze slot
consumed -- the bootstrap is an operator tool, not on the live-trading
code path.

### 1.6 Why this is NOT a freeze-policy concern

`tools/cloud/bootstrap_backtester.sh` is an operator-tool, executed only
when provisioning a fresh VM. It is not on the live-trading code path
and not in `packages/`. Fixing it is audit-only and consumes no
freeze-bypass slot. The fix will land in a normal commit when prepared.

---

## 2. Slot-1 (`risk.allow_shorts: false`) regression on trader VM, 2026-05-26 deploy → 2026-05-27 detection

### 2.1 Timeline

| Time (IST) | Event |
|---|---|
| 2026-05-26 09:10 | First attempt to deploy slot-1 via `sed` on trader VM — **failed silently** (key wasn't in the trader's `config.yaml` because the trader was at `868d5ad`, behind main; see `findings_log_2026-05-25.md §17`). |
| 2026-05-26 14:37 | Operator initiated manual VM rebuild (Bug I reconciliation). |
| 2026-05-26 14:41 | Trader container healthy after manual rebuild. HEAD now `73c26bf`. |
| 2026-05-26 15:19:54 | Operator re-ran the `sed` flip on the trader VM (`allow_shorts: true → false`) and `docker compose restart trader`. |
| 2026-05-26 15:19:55 | Container restarted; daemon confirmed `allow_shorts = False` (verified via `docker exec trader python3 -c "..."`). |
| **2026-05-26 ~18:28** | **Unknown deploy / rebuild operation overwrites trader's `config.yaml` with the upstream default (`true`). Slot-1 silently regresses.** |
| 2026-05-27 09:00 | Heartbeat cron fires; daemon reports cash + DD + risk_state but does NOT report the `allow_shorts` flag (gap in heartbeat schema). |
| 2026-05-27 10:42 | Periodic health check by investigative agent detects `allow_shorts = True` on the running daemon. Slot-1 unwound for ~16h. |
| 2026-05-27 10:50 | Operator re-applied `sed` flip to `false` + `docker compose restart trader`. Daemon confirmed `False`. |
| 2026-05-27 ~10:54 | Operator + investigative agent ack-back to advisor memo + decision to "Fix all 4". |
| 2026-05-27 11:00 | Investigative agent flips `config.yaml` locally to `false`, commits as `8e1e926`, pushes to origin/main. Slot-1 now durable in git. |
| 2026-05-27 11:05 | Trader VM git-pulls `8e1e926` + observability commit `e1df9e8`; image rebuilt + container recreated; daemon re-confirmed `allow_shorts = False`. |

### 2.2 Root cause

`sed` edits on the trader VM are **not version-controlled**. Slot-1 was
ledger-recorded as "LIVE" on 2026-05-26 (`docs/FREEZE_v2.1.md` slot 1
entry), but the change existed only as a working-tree delta on the
trader VM. Any operation that touches `git`'s opinion of the working
tree (a `git pull` with `--strategy theirs`, a `git reset`, a container
image rebuild that re-`COPY`s `config.yaml` from the build context, or
a fresh checkout) can revert the file to the committed HEAD's blob,
which has `true`. The 2026-05-26 ~18:28 IST event was almost certainly
the container rebuild done as part of yet-another adjustment cycle the
operator performed after the official 15:19 IST deploy (multiple
`.bak_*` files on the trader VM confirm at least 5 successive
`config.yaml` edits between 05:08 and 05:13 UTC on 2026-05-27, e.g.).

The Dockerfile's `COPY --chown=trader:trader config.yaml ./` is the
operative line: every `docker compose build` snapshots the host's
*current on-disk* `config.yaml` into the image layer. If between deploy
and rebuild the host's `config.yaml` was touched by anything that reset
the file (most plausibly a `git pull` that auto-merged or a
`git checkout config.yaml`), the next image bake would bake `true` in
rather than the operator's `false`.

### 2.3 Detection mechanism

Pure *outside-in* check via the periodic health-probe:

```bash
sudo docker exec trader python3 -c \
  "import yaml; print('allow_shorts =', \
   yaml.safe_load(open('/app/config.yaml'))['risk'].get('allow_shorts'))"
```

`docs/eod_report_2026-05-26.md` confirmed the deploy worked at 15:19 IST.
The 09:00 IST heartbeat on 2026-05-27 did NOT include the flag value
(heartbeat schema gap, see §2.5 below), so the regression silently went
~16 hours before a follow-up health check caught it.

### 2.4 Fix (durable this time)

1. **In-place fix on the VM** (immediate exposure window closure):
   Operator re-flipped `config.yaml` via `sed` + restarted container.
   Done at ~10:50 IST. `allow_shorts = False` confirmed by daemon
   re-read.
2. **Durability fix** (this finding's authoritative remedy):
   Investigative agent edited the *local repo's* `config.yaml` to
   `false` and committed as **`8e1e926 fix(config): commit allow_shorts:false to git (slot-1 durability fix)`**.
   Pushed to origin/main at 11:00 IST.
3. **Backfill on trader VM**: `git pull --ff-only origin main` on the
   trader pulled `8e1e926` cleanly (the stash of the manual edit was
   identical to the incoming commit, so the working tree was unchanged
   after stash-drop). `docker compose up -d --build trader` re-baked
   the image from the now-`false` git copy. Confirmed at 11:05 IST.

### 2.5 What is NOT yet fixed

* **Heartbeat schema gap.** The daily 09:10 IST heartbeat reports
  `Risk state: cash Rs <X> | DD <Y>%` but does NOT echo the value of
  any `risk.*` config flag. A flag regression like this one is
  invisible to the operator's daily inbox check. Recommended fix:
  add a `Risk config: allow_shorts=False, max_position_pct=15, ...`
  line to the heartbeat body. Not done today; queued for the post-
  Friday review changelog.
* **Daily VM-vs-main drift check.** As called out in
  `findings_log_2026-05-25.md §17.6`. Not done today; queued.
* **The 2026-05-26 ~18:28 regression event has no audit trail.** No
  log line on the trader VM identifies *which* deploy/restart caused
  the revert. Recommended fix: every container restart should snapshot
  the current `config.yaml` + the staged blob hash to a forensic log,
  so a future regression has a paper trail. Not done today; queued.

### 2.6 Bypass-slot accounting impact

* Slot-1 still consumed (`risk.allow_shorts: false` is the change that
  consumed it on 2026-05-26).
* Commit `8e1e926` is **not** an additional bypass — it is the in-git
  representation of the slot-1 change. Flipping the value of an
  already-slotted key is a config edit, not a new bypass. The
  `FREEZE_v2.1.md` slot 1 entry already covers it.
* Status: **slot 1 LIVE + DURABLE (was: LIVE-but-volatile)**.
* No change to slot 2 or slot 3.

---

## 3. Bug I closure verdict — trader VM divergence reconciled by operator

### 3.1 What was reconciled

Per `findings_log_2026-05-25.md §17`, the trader VM was at HEAD
`868d5ad` with 5 modified-tracked files + several untracked production
artifacts. On 2026-05-26 ~14:37 IST the operator performed the manual
rebuild called out in §17.5 of the prior log:

1. Created a feature branch on the trader VM, committed the 5 hot-fixes
   + the operationally-relevant untracked files
   (`docker-compose.override.yml`, `tools/watchdog_check.py`,
   `tools/cloud/install_watchdog_cron.sh`).
2. Pushed the feature branch to origin (commit hash not captured here;
   verifiable via `git log --all --oneline` on origin).
3. Pulled origin/main into the trader VM.
4. Container rebuilt + restarted at 14:41 IST. Healthy at 14:42 IST.
5. Slot-1 sed flip applied at 15:19 IST (see §2.1 above).

Trader HEAD after operator rebuild: **`73c26bf`** (audit-2026-05-27
sweep). Post-pull today (2026-05-27 11:02 IST), trader HEAD advanced to
**`e1df9e8`** (slot-1 durability + regime observability).

### 3.2 Strategy-impact assessment

Of the 5 hot-fixes detailed in `findings_log_2026-05-25.md §17.2`, none
modify strategy or risk code. Categorisation:

| File | Category | Touches frozen surface? |
|---|---|---|
| `docker-compose.yml` (bind-mount additions) | Infrastructure | No — docker-compose.yml is not in the freeze "What is frozen" list |
| `packages/core/stock_scanner.py` (NSE CSV path) | Data-ingest hot-fix | No — `stock_scanner` is data-handler scope, not in the strategy/risk freeze surface |
| `packages/monitoring/alerts.py` (TLS + HTML) | Alerting | No — monitoring scope |
| `tools/send_heartbeat.py` (container-exec mode) | Operator tool | No — `tools/` is operator-tool scope |
| `tools/cloud/install_heartbeat_cron.sh` (--container mode) | Operator tool | No — `tools/cloud/` is operator-tool scope |

**Conclusion:** Bug I is closed. The 2-week divergence was operationally
real but **strategy-neutral**. The live trade record from 2026-05-13 →
2026-05-25 (28 trades, -₹1,505) is therefore valid evidence about
freeze-v2.1's strategy behaviour — the hot-fixes did not affect entry
selection, position sizing, exit logic, or strategy weighting.

The advisor's "Concrete question for the Friday review" (memo §3) is
answered: **no hot-fixes were on the live-trading path**.

### 3.3 Freeze-policy lesson (retained from §17.6)

The drift-from-main monitoring recommendation remains open and queued.
Today's events (§2 above) are an independent occurrence of the same
class of bug (VM-side changes not in main). The recommendation is now
*more* urgent than yesterday, not less.

---

## 4. Diagnostic sprint 2026-05-27 — kick-off

### 4.1 Trigger

Advisor memo received 2026-05-27 morning (paraphrased):

* The pre-speed-patch 90d × 228-stock battery (V1 = +₹177, PF 1.04;
  V2 = +₹659, PF 1.13) was misclassified as stale and re-examined; it
  showed the long side carries all the edge (longs +₹556, shorts −₹379
  on V1).
* All 3 freeze-bypass slots now consumed.
* Long-side famine broke on 2026-05-26: xgboost produced 3 LONG trades,
  all stopped out for −₹453.
* Bug H (xgboost silently OFF in battery) and Bug I (trader VM
  divergence) both confirmed and fixed.

Memo verdict: **PASS-via-candidate is now the modal outcome** (35–45%
probability by June 8). Five honest concerns flagged. Recommended a
5-day diagnostic sprint to reduce ambiguity before the Friday review.

### 4.2 Today's sprint actions (deployed at 11:00–11:05 IST)

| Action | Status |
|---|---|
| (a) Re-apply `allow_shorts: false` durable in git + on trader VM | **DONE** (§2.4) |
| (b) Create `docs/diagnosis_sprint_2026-05-27.md` | **DONE** (separate file) |
| (c) Identify regime classifier source; draft `[REGIME-INPUT]` log patch; commit + deploy | **DONE** (commit `e1df9e8`; verified `[REGIME-INTRADAY-INPUT]` flowing at 11:05 IST) |
| (d) Append today's findings (Bug J + slot-1 regression) | **DONE** (this file) |

Each action was scoped narrowly to *observability* or *durability of an
already-slotted change*. None consumes a new freeze-bypass slot.

### 4.3 Full 10-hypothesis layout

Tracked in `docs/diagnosis_sprint_2026-05-27.md` with day assignments.
Three are confirmed already (H3, H5, H6) and noted in that doc with the
evidence link.

---

## 5. Forensic audit — XGBoost model is broken; slot-2 consumed

### 5.1 The smoking gun (signal-audit aggregation, verified)

The advisor memo received 2026-05-27 ~11:10 IST claimed a clean
directional flip in the xgboost model output on/around 2026-05-11.
I aggregated every `logs/signal_audit_*.csv` over 2026-05-06 → 2026-05-26
filtering on `strategy = xgboost_classifier` and tallied direction:

| Date | xgb total | BUY | SELL | direction bias |
|---|---:|---:|---:|---|
| 2026-05-06 | 536 | 15 | **521** | 97% SELL |
| 2026-05-07 | 369 | 37 | **332** | 90% SELL |
| 2026-05-08 | 140 | 3 | **137** | 98% SELL |
| **2026-05-11** | **270** | **270** | **0** | **100% BUY — flip** |
| 2026-05-12 | 1,990 | 1,990 | 0 | 100% BUY |
| 2026-05-15 | 605 | 604 | 0 | 100% BUY |
| 2026-05-18 | 8,900 | 8,900 | 0 | 100% BUY |
| 2026-05-19 | 11,775 | 11,775 | 0 | 100% BUY |
| 2026-05-20 | 1,969 | 1,969 | 0 | 100% BUY |
| 2026-05-21 | 2,227 | 2,226 | 0 | 100% BUY |
| 2026-05-22 | 145 | 145 | 0 | 100% BUY |
| 2026-05-26 | 3,868 | 3,868 | 0 | 100% BUY |

The market did not flip between May 6 and May 11 — Nifty was below the
200-day EMA the entire window, VIX above 16 the entire window. The
features fed to the model came out of the same `FeatureEngine`. The
ensemble logic in `packages/strategies/ensemble.py` was unchanged. The
classifier in `packages/core/regime.py` was unchanged.

**Only the model output reversed.** The fingerprint is unambiguous.

### 5.2 Other strategies on the same data — proves the change is model-only

For the worst day, 2026-05-19 (the 11,775-BUY day):

```
strategy            direction      count
rsi_momentum        SELL              9
supertrend_follow   SELL              9
xgboost_classifier  BUY          11,775
```

Same regime, same features. The two trend-following strategies emitted
the directionally-correct SELLs (sparse but right). The xgboost model
emitted 11,775 BUYs into a falling tape.

Outcome of the 11,793 rows that day:

```
REJECTED  11,791
ACCEPTED       2
```

Top rejection reasons (May 19):

```
11,605  long_regime:bear_high_vol   (98% of rejections)
   178  opening_lockout
     6  late_cutoff:14:30
     1  expected_profit:poor_rr(...)
     1  atr_gate:0.20<0.50@bear_high_vol
```

The two ACCEPTED trades on May 19 were both `rsi_momentum` SELLs (VOLTAS
+ SWIGGY), with `contributing = rsi_momentum:1.00`. **Zero xgboost
trades executed.** The "ensemble" is xgboost emitting 99% of votes that
all get blocked by the regime gate, plus rsi/supertrend producing the
occasional regime-aligned short that slips through.

The audit's framing — "the regime gate is the only thing protecting the
agent from disaster right now" — is literally true.

### 5.3 Model file fingerprint matches the panic-patch commit

```
$ stat models/xgboost_model.pkl
LastWriteTime: 2026-05-14 12:51:09 IST

$ git log -1 --format=%cd 35adcd2c
2026-05-14 14:55:02 IST
```

The model file mtime is exactly 2h04m before the panic-patch commit's
timestamp. Commit `35adcd2c` body confirms the link:

> "Cloud daemon is scheduled to flip from PAPER → LIVE on Mon
> 2026-05-19 with ₹5k seed capital. Anything in the LIVE-mode-safety
> bucket below is a hard prerequisite. **Today's morning losses
> (-₹592.14) confirmed two of the gaps were not theoretical.**"

The commit message lists 8 substantive changes bundled together,
including "ML model is re-trained with new market-context features and
probability calibration." That's the model file currently in production.

### 5.4 Four training-pipeline bugs fixed in code, NONE in the .pkl

| # | Bug | Code-fix commit | In .pkl? |
|---|---|---|---|
| 1 | Bull-default for missing nifty_trend → out-of-distribution serve | P1 #8 (`a3145c8`, 2026-05-17) | No |
| 2 | Same-day daily Nifty/VIX close tagged onto intraday bars (lookahead) | F-24 (`69d4883`, 2026-05-27) | No |
| 3 | Calibration fit on the same X_test used for held-out reporting | C-23 (`73c26bf`, 2026-05-26) | No |
| 4 | Early-stopping evaluated on the official held-out test set | F-22 (`69d4883`, 2026-05-27) | No |

`prepare_dataset.py` lines 107–110 carries the explicit warning from
the previous run:

> "NOTE FOR PRODUCTION: models/xgboost_model.pkl trained 2026-05-14
> used the OLD bull default. After this fix, retrain before the next
> live deploy to remove the residual train/serve skew. Until then the
> loaded model still has the old bias baked in."

The retrain never happened.

### 5.5 Action taken — disable + commit + deploy + verify

Selected option (A) of the four operator options presented after the
audit verification: comment out `xgboost_classifier` from
`strategies.active` in `config.yaml`. Mirrors how `moving_average_crossover`
(2026-05-05) and `mean_reversion` (2026-05-09) were disabled — the
strategy class is not instantiated at boot, emits no signals, cannot
contribute to the ensemble. Cleanest possible disable; fully reversible
by reverting the commit.

Commit chain landed:

```
f32009c fix(strategies): DISABLE xgboost_classifier in live config (slot-2: broken model)
8bcc360 docs(freeze): reclassify slot-2 + slot-3 audit sweeps to audit-only (bypass 3/3 -> 1/3)
35927ea docs+test(diag-sprint): findings_log + sprint plan + regime-log regression tests
e1df9e8 feat(regime): per-cycle [REGIME-INPUT] observability log (diag-sprint H1)
8e1e926 fix(config): commit allow_shorts:false to git (slot-1 durability fix)
```

Trader VM deploy (executed 11:26:38 IST):

* Git pull origin main: `e1df9e8..f32009c` fast-forward.
* `sudo docker compose up -d --build trader` — image rebuilt, container
  recreated.
* Daemon boot line confirms: `Strategies: ['rsi_momentum', 'vwap_bounce',
  'opening_range_breakout', 'supertrend_follow']`.
* Post-restart `signal_audit_2026-05-27.csv` xgb-row count after
  11:27:00 IST: **0**.
* Post-restart per-symbol signal lines no longer include
  `xgboost_classifier=...`.

### 5.6 Backtester paused

The V3-V19 re-run launched 2026-05-26 had the same broken
`models/xgboost_model.pkl` bind-mounted (the Bug H fix made the file
available; it didn't make the file *correct*). Every result the
scheduler is about to produce would carry the same 100%-BUY pathology
into the backtest, polluting absolute-PF numbers.

Action (11:35 IST):

* `sudo systemctl stop battery-scheduler.service` — scheduler PID gone.
* `sudo docker stop battery_nifty50_60d_20260527T025811` — in-flight
  worker killed (had been Up 3h, ~0 of 16 variants completed).
* Marker file `data/PAUSED_20260527T060131Z.md` left on the backtester
  VM with resume criteria.

**Resume criteria (pre-committed):**

1. Retrain `models/xgboost_model.pkl` with the corrected pipeline (all
   4 bugs above absent from the training run).
2. Held-out backtest on a fresh window confirms edge (target: PF > 1.0,
   AUC > 0.60).
3. New `freeze-bypass: critical-bug-fix` slot consumption (slot 3 is
   available; ledger entry required).
4. Deploy new .pkl to BOTH the trader VM and the backtester VM
   (`models/` bind-mount picks up host-side changes).
5. `sudo systemctl start battery-scheduler.service` resumes the queue.

### 5.7 Slot-2 ledger accounting

| Slot | Status | Description |
|---|---|---|
| 1 | LIVE + DURABLE | `risk.allow_shorts: false` (committed in git as `8e1e926`). |
| 2 | **LIVE today** | `xgboost_classifier` removed from `strategies.active` (`f32009c`). |
| 3 | Reserved | For the retrained-model redeploy (per resume criteria above). |

Per the 2026-05-27 morning advisor memo, slot 2 + 3 were already
reclassified from audit sweeps to audit-only (`8bcc360`), freeing them
for actual behaviour-changing fixes. Slot 2 now consumed by this
critical-bug-fix. Slot 3 reserved.

### 5.8 Expected behaviour today (rest of session)

With slot 1 (`allow_shorts: false`) + slot 2 (xgboost disabled) both
LIVE, in the current `bear_low_vol`/`bear_high_vol` regimes the
remaining 4 strategies (rsi_momentum, vwap_bounce, opening_range_breakout,
supertrend_follow) have:

* All SHORT signals → rejected by `allow_shorts: false`.
* All BUY signals → regime-suppressed (per
  `STRATEGY_REGIME_PREF` in `packages/core/regime.py`):
  * rsi_momentum BUY: 0.7 × ensemble weight
  * vwap_bounce BUY: 0.9
  * opening_range_breakout BUY: 0.8
  * supertrend_follow BUY: 0.6

This is intentional. The audit's framing: "no trades" is far cheaper
than "more broken-model trades" until the retrain lands.

### 5.9 Retrain runbook (queued, not executed today)

To be executed once slot 3 is approved:

1. **Pipeline self-check.** Re-read `packages/training/prepare_dataset.py`
   + `packages/training/train_xgboost.py` end-to-end. Confirm:
   - F-24 lookahead-shift present (`prepare_dataset.py` line 205:
     `ctx_shifted.index = ctx_shifted.index + pd.Timedelta(days=1)`).
     **CONFIRMED in code.**
   - P1 #8 neutral default present (`prepare_dataset.py` lines 112, 115:
     `ctx["nifty_trend"] = 0` and `.fillna(0)`). **CONFIRMED in code.**
   - F-22 chronological-tail validation present (`train_xgboost.py`
     lines 99–118: `X_fit / y_fit` carved from X_train, not X_test).
     **CONFIRMED in code.**
   - C-23 out-of-sample calibration present (to be re-verified during
     the retrain pre-flight).
   - P1 #7 cross-symbol calendar-leak fix (to be re-verified).

2. **Training run.** From the backtester VM (where compute is sized
   for this):
   ```bash
   sudo systemctl stop battery-scheduler.service  # already stopped
   sudo docker run --rm \
     -v /opt/trading-agent:/app \
     -v /opt/trading-agent/data:/app/data \
     -v /opt/trading-agent/models:/app/models \
     trading-agent:latest \
     python -m packages.training.prepare_dataset \
       --symbols-source nse_all --interval 5min --window 60d \
       --label-horizon 3 --label-threshold-pct 0.3
   sudo docker run --rm \
     -v /opt/trading-agent:/app \
     trading-agent:latest \
     python -m packages.training.train_xgboost \
       --train data/train_dataset.csv --test data/test_dataset.csv \
       --model-output models/xgboost_model_retrain_$(date -u +%Y%m%dT%H%MZ).pkl
   ```

3. **Validation.** Held-out backtest on a window the model has never
   seen (e.g. 2026-04-01 → 2026-04-30 if the training window is
   2024-01-01 → 2026-03-31).
   - Required: AUC > 0.60 on the held-out, Brier improving over a
     dumb majority-class predictor, calibration plot near-diagonal.
   - Required: per-side direction balance (BUY/SELL count not extreme;
     audit-style >97% one-sided is the failure mode to detect).

4. **Bench-test on the live universe.** Run the new .pkl through
   the backtester's V1 (live shipped) and V4 (threshold-3%) variants
   on Nifty 50 60d and on the 228-stock 60d universe. Compare to the
   recorded V1/V4 numbers from the 2026-05-25 batteries.

5. **Slot 3 ledger entry + deploy.** Write `FREEZE_v2.1.md` slot 3
   rationale + activation timestamp. Replace
   `models/xgboost_model.pkl` on both VMs. Re-enable
   `xgboost_classifier` in `strategies.active`. Restart trader
   container. Verify.

6. **Resume the backtester queue.** `sudo systemctl start
   battery-scheduler.service`. The V3-V19 re-run now produces
   trustworthy data.

### 5.10 Retrain pre-flight decision — 2026-05-28 13:25 IST

**Decision:** Pre-flight + training **deferred until Friday morning**
after slot #3 V15 transfer evidence lands. No code or backtester
action taken today.

**Why not retrain on the holiday:**

1. **Circular dependency on the backtester scheduler.** §5.9 step 2
   requires `sudo systemctl stop battery-scheduler.service` to free
   the VM for training. Slot #3 of the trimmed queue is still
   running V3..V19; killing it now would destroy the V15-transfer
   evidence that the §7 decision matrix in
   `docs/friday_review_2026-05-29.md` uses to decide whether to
   retrain in the first place. Starting the retrain would blind us
   to whether we should be starting the retrain.
2. **Two unverified bug fixes.** §5.9 step 1 lists C-23 (out-of-sample
   calibration) and P1 #7 (cross-symbol calendar-leak fix) as "to be
   re-verified during the retrain pre-flight". The earlier broken pkl
   was produced by a 2026-05-14 panic patch that skipped pre-flight.
   Repeating that mistake is the dominant retrain-failure mode.
3. **Phase A (train+validate+benchmark) is reversible until Phase B
   (deploy).** Phase B consumes bypass slot-3 of FREEZE_v2.1 -- the
   last slot -- and requires Friday's V15-transfer data anyway. No
   real time is saved by starting Phase A on Thursday.

**Pre-flight checklist (queued for Friday morning, ~2h total):**

| Step | What | Where | Effort | Gating |
|------|------|-------|--------|--------|
| A | Code-read `packages/training/prepare_dataset.py`. Verify F-24 lookahead-shift + P1 #8 neutral default (CONFIRMED) AND re-verify **P1 #7 cross-symbol calendar-leak fix**. | Local | 20 min | None |
| B | Code-read `packages/training/train_xgboost.py`. Verify F-22 chronological-tail validation (CONFIRMED) AND re-verify **C-23 out-of-sample calibration**. | Local | 20 min | None |
| C | Pick training window + holdout window. Avoid the 2024-Q1 -> 2026-Q1 window that trained the broken pkl. Candidates documented + chosen. | Local | 30 min | Pre-flight A+B done |
| D | Write `tests/unit/test_training_pipeline_preflight.py` -- file-text + signature assertions for the 5 known bug fixes. Belt-and-braces regression guard. | Local | 30 min | A+B done |
| E | Run `prepare_dataset.py` locally on a small (~10-stock) slice. Assert label balance not extreme (no 95% one-sided). | Local | 15 min compute, no VM impact | A+B+C done |

After pre-flight: §5.9 steps 2-5 land on the backtester VM (step 2
training run ~16-20h; step 3 validation ~30 min; step 4 bench-test
~6h; step 5 deploy ~15 min). Total wall-clock from Friday GO to
live: ~30 hours (Sat midday ish).

**Alternative considered + dismissed:** spin up a separate training
VM right now (now possible since Bug J fix landed). Dismissed
because (a) doubles OCI cost, (b) doesn't bypass the V15-transfer
gate which is about *deployment* not training, and (c) adds a
fresh VM into the operational surface during freeze-v2.1.

**Owner of the GO/NO-GO call on Friday morning:** operator + advisor
review using the §7 decision matrix in friday_review_2026-05-29.md.

---

## 6. Trades.csv hygiene — manual_test rows archived

### 6.1 Contamination

`logs/trades.csv` had 70 lines (1 header + 69 rows). Of the 69 rows:

* 38 were `ZZTEST` / `ZZTEST2` symbols, all dated 2026-05-26 between
  16:45:01 and 16:53:16 IST.
* All falsely tagged with `strategy=mean_reversion` — which is
  DISABLED in `config.yaml` (line 107 commented out 2026-05-09).
* All had `exit_reason=manual_test`.
* All had identical PnL (+₹43.96 each = +₹1670 across the 38 fake
  rows) — clearly synthetic injection from a test fixture, not real
  trades.

Any operator-facing tool that reads `trades.csv` (e.g. EOD diagnostic
spreadsheets, postmortem aggregators, P&L scrubbers run outside the
daemon) would have:

* Inflated trade count by 55%.
* Inflated cumulative P&L by ~₹1,670.
* Misattributed wins to a strategy that's been disabled for 18 days.

The daemon itself reads from the SQLite DB, not the CSV, so the live
risk/portfolio path was clean.

### 6.2 Fix applied (local snapshot only)

* Moved the 38 manual_test rows to
  `logs/trades_manual_test_archive_2026-05-26.csv` (header + 38 rows).
* Rewrote `logs/trades.csv` with header + 31 real-trade rows.
* Date range of the 31 real trades: 2026-05-12 → 2026-05-26 (9
  trading days).
* Last 3 trades: HFCL, TATAINVEST, TATACHEM — all `BUY` from
  `xgboost_classifier`, all `stop_loss` on 2026-05-26 — exactly the
  "3 long-side famine break" trades the §5 audit highlights (sum
  of pnl = −₹453.04, matches audit memo to the rupee).

### 6.3 Trader VM (queued)

The trader VM's `/opt/trading-agent/logs/trades.csv` still has the
manual_test rows. NOT cleaned today because the daemon writes to that
file continuously and we don't want to race the writer mid-session.
Queued for tonight after-hours: same archive-and-rewrite there.

---

## 7. Strategy hot-path performance sprint (P-03, P-04, P-11)

### 7.1 Trigger

Strategy-level audit memo (afternoon 2026-05-27) flagged three
behavior-preserving perf hotspots in the rule-based / LSTM hot paths:

* **P-03** — `SupertrendFollow._compute_supertrend` drove a
  `pd.Series.iloc[i] = ...` loop, ~12 ms per 1500-bar slice. Same file
  also called `_compute_atr(df, period=10)` twice per event (once
  inside the Supertrend computation, once for SL/TP sizing). Same file
  also did the standard `df = data.copy()` opener.
* **P-04** — Five other rule-based strategies (`rsi_momentum`,
  `vwap_bounce`, `mean_reversion`, `opening_range_breakout`,
  `moving_average_crossover`) each opened with `df = data.copy()`
  purely to be able to write 3-7 derived columns back to the frame
  for local `.iloc[-1]` reads. `_make_signal` only consumes
  `data["close"]` + `data.index`, so the copy was strictly waste.
* **P-11** — `LSTMPriceModel` inference round-tripped through pandas
  four times (DataFrame -> fillna DataFrame -> sklearn -> DataFrame
  rewrap -> numpy -> torch). Could be 1 numpy block + in-place fill
  + sklearn + `torch.from_numpy` (zero-copy).

### 7.2 Decision: bundle vs. defer

Memo asked whether to fold these into Slot-2 (xgboost disable) since
it was already being live-deployed. Verdict: **no -- keep separate**,
treat as audit-only:

* Slot-2 is consumed (xgboost disable, commit `f32009c`).
* The May-14 panic-patch lesson is exactly this: do not bundle a
  strategy-altering change with operational/perf changes in the same
  deploy. Independent revertability matters.
* Perf fixes are mechanically byte-identical (mathematics unchanged,
  only allocation pattern + loop substrate change). They qualify as
  **audit-only under freeze-v2.1** provided tests prove the equality.

### 7.3 Implementation

Three commits, each independently revertable:

| Commit  | Scope                                         | Files |
|---------|-----------------------------------------------|-------|
| `1fe1deb` | P-03 SupertrendFollow + perf-invariants test  | 2 |
| `7f19990` | P-04 5 strategies + F-46 string-assert tweak  | 6 |
| `0809cf5` | P-11 LSTMPriceModel numpy handoff             | 1 |

P-03 details:

* `_compute_supertrend` now operates on numpy arrays internally (same
  algorithm: bar-by-bar direction comparison, trailing-band carry-
  forward within a direction segment, direction-segment-flip reset).
  Each iteration is a direct array index instead of a pandas
  BlockManager lookup.
* `_compute_atr_cached(self, df, period)` -- instance-level cache
  keyed by `(id(df), period)`. Within a single `generate_signal` call
  the two `_compute_atr` callers collapse to one compute + one cache
  hit. Across events the cache invalidates automatically (new slice
  has a new id).
* `data.copy()` removed; `supertrend`/`st_dir`/`adx` held as local
  Series instead of being written back to the frame.

P-04 details:

* All 5 strategies refactored to compute derived columns as locals.
* ORB never wrote to the frame in the first place -- the copy was
  pure waste, removed.
* `_make_signal` calls now pass the original `data` frame
  (`_make_signal` only reads `data["close"]` + `data.index`, so this
  is safe).

P-11 details:

* `self._ml_feature_cols` cached at `__init__` (was being re-queried
  from `FeatureEngine.get_ml_feature_columns()` every cycle).
* `window_df.to_numpy(dtype=float32)` -> in-place `np.nan_to_num` ->
  `scaler.transform(numpy)` -> `torch.from_numpy(...).unsqueeze(0)`.
* `torch.from_numpy` shares memory with the numpy array; we never
  mutate it after constructing the tensor, so aliasing is safe.
* NaN-skew detector (F-14) ported to numpy (`np.isnan(arr).sum()`).
* Behavior unchanged today because `lstm_price_model` is not in
  `strategies.active`; commit is forward prep for re-enablement
  post-retrain.

### 7.4 Verification

* `tests/unit/test_strategy_perf_invariants.py` (new file, 19 tests,
  all passing):
  - `test_supertrend_vectorised_matches_pandas_loop` -- the **pre-fix
    pandas loop is preserved verbatim** as a reference function;
    output asserted byte-identical to the new vectorised path across
    5 RNG seeds.
  - `test_no_caller_frame_mutation[*]` -- sha256 of input frame
    before vs after the call, parametrised across all 6 strategies.
  - `test_strategy_output_is_deterministic[*]` -- two consecutive
    calls produce bit-identical TradeSignals; parametrised across
    all 6 strategies (guards against cache-state leakage).
  - `test_supertrend_atr_cache_invalidates_on_new_frame` -- distinct
    DataFrames yield distinct ATR values.
* Full unit suite: **1,395 tests pass** (was 1,394 + 1 stale string
  assert; updated as part of P-04 commit).
* `tests/unit/test_strategy_history_window.py` -- the pre-existing
  full-vs-windowed equivalence suite (TestSupertrendEquivalence,
  TestRSIMomentumEquivalence, TestMeanReversionEquivalence,
  TestMACrossoverEquivalence, TestVWAPBounceEquivalence) **continues
  to pass**, which is independent confirmation that the refactor
  preserved windowing semantics.

### 7.5 Measured speedup

**Clean, isolated measurement (local micro-benchmark):**

| Stage                           | Pre-fix | Post-fix | Speedup |
|---------------------------------|--------:|---------:|--------:|
| `_compute_supertrend` (n=1500)  | ~12 ms  | 2.8 ms   | ~4.3x   |
| `generate_signal` (n=1500)      | ~25 ms  | 5.6 ms   | ~4.5x   |

This is the cleanest signal we have for P-03 specifically. It was
run via `time.perf_counter` in a local Python REPL on a single
strategy instance, no xgboost involved, no daemon, no other
strategies competing for CPU. The 4.5x is real and byte-identity is
proven by `test_supertrend_vectorised_matches_pandas_loop`.

**End-to-end backtester throughput (confounded):**

| Window                                  | Throughput    | What changed     |
|:----------------------------------------|--------------:|:-----------------|
| Pre-pause (May 25-27 morning)           | 19-40 ev/s    | xgb active, no perf fixes |
| Post-restart (this run, May 27 12:27+)  | 75-104 ev/s   | xgb disabled AND perf fixes |

This is a 2-3x throughput improvement at the queue level -- BUT
it cannot cleanly attribute between "xgboost removed from active"
(commit f32009c, slot-2) and "P-03/P-04 perf fixes" (today). Both
shipped in the same image rebuild during the backtester restart.

The decomposition I currently believe is most likely:

* **xgboost-disable: ~2x** -- xgboost was very probably 40-60% of
  per-event compute (FeatureEngine.compute_all + XGB model.predict
  on every signal cycle, dwarfing any single rule strategy). Dropping
  it from `strategies.active` skips that entire branch.
* **P-03/P-04 perf fixes: ~1.3-1.5x** on top -- consistent with the
  isolated supertrend measurement scaled by supertrend's share of the
  remaining 4-strategy mix (~25-30%).
* **Multiplicative: ~2.5-3x combined** -- matches the observed peak.

**Earlier framing was sloppy:** I previously called this a
"~25-30% wall-clock reduction from the perf fixes" / "perf fixes
landed harder than projected -- 2-4x". That was conflating the
xgb-disable effect with the perf-fix effect. The perf fixes
contribute meaningfully but are NOT the dominant factor. The
honest summary is:

> The perf fixes deliver the ~25-30% improvement they were designed
> for. The 2-3x throughput we are observing in this run is mostly
> the xgboost-disable being a much bigger win than initially
> credited.

**Clean A/B not run** because it would have cost ~50 min of
backtester time mid-queue, and the variants themselves (what
configs beat V1) are the actually-decision-relevant signal --
not the perf attribution. If we want the isolated perf-fix number
later, the cleanest path is a single-variant A/B between
HEAD~3 (xgb-disabled, no perf fixes) and HEAD (xgb-disabled, perf
fixes) on identical market_data, run after the current queue
completes.

### 7.6 Freeze v2.1 ledger impact

Slot count unchanged at **2/3 used**:

* Slot 1: `risk.allow_shorts: false` durability (`8e1e926`) -- LIVE.
* Slot 2: xgboost disable (`f32009c`) -- LIVE today.
* Slot 3: reserved (model retrain, when the new pkl lands).

Today's three perf commits (`1fe1deb`, `7f19990`, `0809cf5`) are
**audit-only**, evidenced by the byte-identical reference-loop test
and the full-vs-windowed equivalence suite. No bypass slot consumed.

### 7.7 Deploy plan

* **Backtester VM**: pull origin/main (post-P-11 HEAD), rebuild
  `trading-agent:latest`, cleanup partial run artifacts, restart
  scheduler. This commit completes the unblock work for resuming
  the V1-V19 nifty50_60d re-run on the now-cleaner 4-strategy
  config. **Deploying immediately** per operator instruction.
* **Trader VM**: **NO deploy today.** Trader image stays on
  `f32009c` (xgboost-disabled config + observability). Perf fixes
  do not affect live signal generation behavior, only throughput;
  validating them on the backtester first is the May-14 lesson.
  Trader image will be rolled forward at the post-Friday review
  along with any other accumulated changes.

---

## 8. Battery queue trim — drop ~144h of pre-retrain compute

**Status:** DEPLOYED 2026-05-27 ~15:42 IST (10:12 UTC).
**Commit:** `84f5acd` (queue config), `ae847e3` (§7.5 honest perf
attribution amendment — preceding doc-only commit).
**Freeze-v2.1 class:** audit-only (queue config only; no behaviour
change in the harness, the strategies, or the live trader). **No
bypass slot consumed.**

### 8.1 Context — why trim now

Operator question 15:35 IST asked what the queue's ~7h ETA actually
buys us, and whether V1→V19 gets re-run on different data after
`nifty50_60d` finishes. The honest answer was: yes — under the
old queue, V1→V19 would re-run on 232 stocks × 60d (job 3,
v2_baseline_90d, ~48h), then again on 232 × 60d train (~32h),
then 232 × 30d holdout (~16h), then 232 × 120d (~64h). Total
~160h of compute downstream of slot #2.

Those four jobs were designed pre-2026-05-26 to characterise the
SHIPPED 5-strategy ensemble across multiple windows and regimes.
But yesterday's slot-2 (commit `f32009c`) disabled
`xgboost_classifier` live because the production .pkl was
forensically confirmed broken (§5). So those long-history v2_*
jobs would now be re-validating a 4-strategy ensemble that the
**post-retrain** ensemble will strictly dominate — ~144h of
backtester time spent on a question the retrain (slot-3 candidate,
sprint Day 4-5) will moot.

### 8.2 What we trimmed

Old queue (6 jobs):

1. `nifty50_60d`                              ~7h     all 19 variants
2. `nifty500_v4_long_only_validation_60d`     ~10-12h 6 variants
3. `v2_baseline_90d`                          ~48h    all 19 variants
4. `v2_train_60d`                             ~32h    all 19 variants
5. `v2_holdout_30d`                           ~16h    all 19 variants
6. `v2_baseline_120d`                         ~64h    all 19 variants

New queue (3 jobs):

1. `nifty50_60d`                              ~7h     all 19 variants
   — *currently running, untouched*
2. `nifty500_v4_long_only_validation_60d`     ~10-12h 6 variants
   — *Friday 2026-05-29 review evidence (V4/V17/V18/V19 on full
     232-stock universe)*
3. `v2_holdout_30d`  *(promoted from slot #5)*  ~16h  all 19 variants
   — *p-hack guard: if a slot-#1 winner crumbles on the last 30d,
     the slot-#1 ranking was overfit. We keep the holdout (the
     guard) and drop the matching train-60d slot, since slot #1
     already covers a 60d-window ranking on a comparable universe.*

**Dropped (preserved in git at HEAD~):**

* `v2_baseline_90d`   (~48h) — broken-ensemble re-validation, moot
* `v2_train_60d`      (~32h) — redundant given slot #1 + slot #3
* `v2_baseline_120d`  (~64h) — regime-dependence check, useful only
                                post-retrain

**Net compute saved:** ~144h ≈ 6 days.

### 8.3 Why this is audit-only (not a freeze slot)

* Edits a queue config file (`data/battery_queue.yaml`), not the
  trader, harness, strategies, or risk code.
* Does not change variant definitions in
  `packages/research/battery.py`.
* Does not start, stop, or alter the in-flight job (`nifty50_60d`,
  the slot #1 entry that was already running).
* Removed jobs were future-scheduled work, not commitments — they
  hadn't appeared in `data/battery_queue_state.json` yet (state
  file is written on job *start*).
* Post-retrain, the dropped windows will be re-queued as a
  separate block validating the NEW ensemble. We are not making
  the no-xgb config the long-term shipping target; we are skipping
  validation of a known-dead ensemble.

### 8.4 Verification before commit

| Check | Result |
|:------|:-------|
| `python -c "import yaml; yaml.safe_load(...)"` parses | OK — 3 jobs, expected keys |
| `pytest tests/unit/test_battery_queue_scheduler.py -q` | 29/29 pass |
| Diff is queue-config only (no .py touched) | confirmed |

### 8.5 Deploy verification on backtester VM

Captured by `/tmp/bt_queue_deploy.sh`:

| Checkpoint | Before deploy | After deploy |
|:-----------|:--------------|:-------------|
| HEAD | `9ac6435` | `84f5acd` |
| Queue entries | 6 | **3** |
| scheduler PID | 1035215 | 1050628 (restarted) |
| In-flight container | `Up 3 hours` | **`Up 3 hours`** *(same container, undisturbed)* |
| state file | nifty50_60d="running" | nifty50_60d="running" *(unchanged — scheduler re-attaches via `wait_for_running_battery`)* |

Scheduler journal line confirming the new shape:

```
May 27 10:12:27 backtester battery-scheduler[1050628]: \
    [scheduler] queue: /opt/trading-agent/data/battery_queue.yaml (3 jobs)
May 27 10:12:27 backtester battery-scheduler[1050628]: \
    [scheduler] waiting for pre-existing battery \
    'battery_nifty50_60d_20260527T065700' to finish (poll every 90s)...
```

The scheduler correctly:
1. Loaded the new 3-job queue on restart.
2. Detected the in-flight `battery_nifty50_60d_20260527T065700`
   container still running (uptime 3 hours, untouched by the
   restart because it's a detached docker container, not a child
   of the systemd unit).
3. Entered `wait_for_running_battery()` to wait it out.
4. Will resume queue processing from the new slot #2 (validation)
   once `nifty50_60d` completes (~3h from deploy time).

### 8.6 Expected end-to-end timeline

Sequential, single-VM (workers=2 inside each job's container):

| Slot | Job | Wall-clock | Expected completion (IST) |
|:----:|:----|:----------:|:--------------------------|
| 1 | `nifty50_60d`                              | ~3h remaining | Wed 19:30 |
| 2 | `nifty500_v4_long_only_validation_60d`     | ~10-12h       | Thu 07:00 |
| 3 | `v2_holdout_30d`                            | ~16h          | Thu 23:00 |

**Friday 2026-05-29 review** has evidence from all 3 jobs in
hand. Backtester VM is then free for post-review work (e.g., the
model-retrain job — slot-3 candidate).

### 8.7 Rollback

If we change our minds, the dropped jobs are at `git show HEAD~1:data/battery_queue.yaml`. Restore with:

```bash
sudo -u opc git show HEAD~1:data/battery_queue.yaml > \
    /opt/trading-agent/data/battery_queue.yaml
sudo systemctl restart battery-scheduler.service
```

The in-flight job is unaffected by either roll-forward or
roll-back of the queue file, because the queue is only consulted
*between* jobs.

---

## 9. Bug K — `--holdout-window-days` / `--train-window-days` silently ignored in parallel-worker path

**Discovered 2026-05-28 11:55 IST while spot-checking the supposed
"holdout-30d" job (slot #3 of the trimmed queue) on the backtester
VM.** The job is structurally a duplicate of slot #2's 60d run; it
gives no walk-forward evidence, contrary to what `data/battery_queue.yaml`
advertised when the queue was trimmed yesterday.

### 9.1 The smoking-gun observation

`battery_v2_holdout_30d_20260528T011921` and
`battery_nifty500_v4_long_only_validation_60d_20260527T142630` both
ran on 232 stocks. They were *supposed* to test different time
windows -- the holdout job with `--days 90 --holdout-window-days 30`
should have backtested only the LAST 30 days; the validation job
with `--days 60 --holdout-window-days <unset>` backtested the full
60d. Their V1+V2 results came out byte-identical:

| Variant | Trades | WR% | PnL | PF | Sharpe | MaxDD% | Ret% |
|---------|--------|-----|------|----|--------|--------|------|
| V1 (holdout job) | 235 | 36.2 | -₹693 | 0.78 | -2.57 | 8.77 | -6.68% |
| V1 (validation job) | 235 | 36.2 | -₹693 | 0.78 | -2.57 | 8.77 | -6.68% |
| V2 (holdout job) | 266 | 34.6 | -₹981 | 0.69 | -4.10 | 11.21 | -9.58% |
| V2 (validation job) | 266 | 34.6 | -₹981 | 0.69 | -4.10 | 11.21 | -9.58% |

Identical to every decimal place. That can only happen if both jobs
saw the same market_data over the same window.

### 9.2 Triangulating evidence

Three independent log signals confirm the slice didn't propagate to
the workers:

1. **Main process DID slice.** `log.txt` for the holdout run shows:
   ```
   [BATTERY] walk-forward slice (last 30d, applied to 224/224 symbols):
            351829 bars (was 974726, ratio 36.1%)
   ```
   So `args.holdout_window_days = 30` was parsed and the slice loop
   in `packages/research/battery.py:1305-1334` executed correctly --
   in the **main** process's in-memory `market_data` dict.

2. **Workers did NOT see the slice.** `workers/V1_baseline_current_shipped.log`
   shows progress against `974,726` (the *pre-slice* total bars):
   ```
   [BATTERY-PROGRESS] 8,736/974,726 (0.9%) | sim_date=2026-02-26 | ...
   ```
   And `sim_date=2026-02-26`. The data window starts in late
   February, NOT in late April (which is what last-30d would mean
   for a Thu-2026-05-28 job kickoff).

3. **Cache write order is wrong.** Reading `battery.py` line 525 +
   line 1305-1334 confirms it: `_save_market_data_cache()` is called
   on line 525 with the FULL pre-slice 974k-bar dataset; the slice
   logic at line 1305 mutates only the in-process dict, never the
   on-disk cache. Workers (line 539) then *reload from the cache*
   (`_load_market_data_cache(...)`) and never see the slice.

### 9.3 Root cause

Order of operations in `battery.main()`:

```python
# Line ~525  -- cache saved with FULL window
_save_market_data_cache(market_data, out_root)
[BATTERY] market_data cached (417.2 MB) -> .../market_data.pkl

# Line 1305 -- slice applied AFTER cache, in main only
if args.train_window_days or args.holdout_window_days:
    n = args.train_window_days or args.holdout_window_days
    keep = "first" if args.train_window_days else "last"
    for sym in list(market_data.keys()):
        df = market_data[sym]
        if keep == "last":
            cutoff = df.index.max() - pd.Timedelta(days=n)
            market_data[sym] = df[df.index >= cutoff]
        else:
            cutoff = df.index.min() + pd.Timedelta(days=n)
            market_data[sym] = df[df.index < cutoff]
    [BATTERY] walk-forward slice (...): 351829 bars (was 974726, ratio 36.1%)

# Workers in subprocesses then call _load_market_data_cache(...)
# from disk -> they see the FULL pre-slice 974k bars again.
```

The fix is to reorder: apply the slice BEFORE the cache write, so
workers reload the already-sliced data. There is no other dynamic
state to preserve -- the slice is deterministic from `args`.

### 9.4 Severity & freeze-policy classification

* **Audit-only research-tool defect.** This is in
  `packages/research/battery.py`, the offline backtester harness.
  It does not affect the live trading code path on the trader VM
  -- the live daemon never calls `--holdout-window-days`.
* **Consumes no freeze-bypass slot.** Same reasoning as Bug J §1.6.
* **Affects every battery run we've ever shipped that used these
  flags.** Best evidence we have, looking back: zero. The flags
  were documented in §11.5 of the README and listed in the script
  help text, but no production battery run before 2026-05-28
  actually invoked them. Slot #3 of yesterday's trimmed queue was
  the first real use, and Bug K immediately killed its decision
  value. So the cross-history blast radius is small.

### 9.5 What slot #3 of the current queue *does* give us

Even though the holdout slice is dead, the running job is **not**
worthless:

* It runs **all 19 variants** on the **232-stock universe** (slot #2
  only ran 6 variants -- V1, V2, V4, V17, V18, V19). So slot #3
  fills in V3, V5..V16 on the bigger universe, including:
  * **V15_mr_xgb_only** -- the only *positive* variant on slot #1's
    50-stock run (PF 1.02). If V15 stays positive on 232 stocks, the
    XGBoost-retrain priority jumps.
  * **V16_completely_naked** -- catastrophic on 50 stocks (-40.48%).
    Confirms-or-not whether the gates' value transfers to the bigger
    universe.
  * **V18 anomaly check** -- V18's 3% threshold went missing on slot
    #2 (V18 = V2, 266 trades, instead of V18 = V4, 229 trades). If
    slot #3 reproduces this, we have a separate config-merge bug to
    investigate post-Friday.
* It runs on the SAME 60d window as slot #2 -- so cross-job
  consistency checks on the 6 shared variants (V1, V2, V4, V17,
  V18, V19) become a free determinism contract: byte-identity
  expected.

So slot #3 is a *de-facto wider variant sweep on 232 stocks*, NOT
the p-hack guard the queue header promised. The Friday review will
explicitly disclose this so the reader doesn't take the holdout
framing at face value.

### 9.6 Permanent fix plan (post-Friday, ~30 min work)

1. Move the slice block from `packages/research/battery.py:1305-1334`
   to *before* the `_save_market_data_cache()` call on line 525. The
   refactor is mechanical -- the slice loop only depends on
   `market_data` and `args`, both available pre-cache.
2. Add a unit test:
   `tests/unit/test_battery_walk_forward_slice.py::test_workers_see_sliced_market_data`.
   The test:
   * Builds a fake market_data dict with a known 90-day index.
   * Runs `_save_market_data_cache` then `_load_market_data_cache`
     after a `--holdout-window-days 30` slice has been applied
     (post-fix order).
   * Asserts the reloaded dict has the LAST 30d only.
   The test should *fail* on the current code (proving Bug K),
   then *pass* after the reorder.
3. Add a log assertion in `_run_variant_in_subprocess`: if
   `args.train_window_days or args.holdout_window_days` is set,
   log `[WORKER] post-slice market_data: <bars> bars` and assert
   the count is < pre-slice. Belt-and-braces guard against future
   refactors that re-introduce the gap.
4. Once the fix lands, re-queue a real holdout job for the next
   weekend run.

### 9.7 Disclosure to the Friday review

The Friday morning review (`docs/friday_review_2026-05-29.md`) will:

* Reframe slot #3 as "wider variant sweep on 232 stocks" rather
  than "holdout-30d p-hack guard".
* Use slot #2 + slot #3 *only* for variant ranking on the bigger
  universe; treat the cross-window comparison as "60d slot-#1 (50
  stocks) vs 60d slot-#2/#3 (232 stocks) on the SAME window" --
  which is still useful for cross-universe transfer but is NOT a
  walk-forward / p-hack guard.
* Defer the V4-as-live-config decision until a real holdout run
  has been completed post-fix. The slot-3 retrain go/no-go will
  be conditioned on the V15 result on 232 stocks alone.

### 9.8 Files touched (this finding)

* `packages/research/battery.py` -- *no changes today*. Fix queued
  for post-Friday week.
* `docs/findings_log_2026-05-27.md` §9 -- this section (added
  2026-05-28).
* `docs/friday_review_2026-05-29.md` -- to be drafted today,
  incorporates §9.7 disclosure.

---

## 12. Bug L — NSE_HOLIDAYS missing 2026-05-28 (Bakri Eid); daemon ran 7h on a closed market

**Discovered 2026-05-28 17:00 IST after the operator noticed the
broker app showed market closed but the trader VM had run all day.**
Live miss of the Bakri Eid holiday because the date was absent from
`packages/core/data_handler.py:NSE_HOLIDAYS`.

### 12.1 Today's evidence

`logs/trading_agent_2026-05-28.log` shows the daemon ran a full
trading pipeline from 08:00:57 to 16:00:53 IST:

* 193 cycles completed (~2.5 min/cycle, normal cadence).
* 169 symbols with data, fetched into the scanner every cycle.
* `[REGIME-INPUT] nifty_trend=-1 india_vix=14.98 high_vol=False -> regime=bear_low_vol`.
* **One** ensemble-vote signal generated all day:
  `09:16:11 [ENSEMBLE] SELL ICICIPRULI | conf=0.629 | strategies=['rsi_momentum']`.
  Blocked by `[OPENING-LOCKOUT] Skipping SELL ICICIPRULI: in 15-min opening window`.
* SESSION SUMMARY: 0 trades, ₹+0.00 day P&L, 0 positions.
* `logs/health.json`: `state: idle_off_hours, running: false, daily_trades: 0`.
* `logs/audit/2026-05-28/checkpoint_1600.md` verdict: **GREEN**
  (which is technically accurate -- no errors, no trades -- but
  misleading; the agent was scanning a closed market).

### 12.2 Root cause

`packages/core/data_handler.py:NSE_HOLIDAYS` did not contain
`"2026-05-28"`. `DataHandler.is_market_open()` therefore returned
True at 09:15 IST, the daemon entered its normal cycle loop, and the
data fetcher served (presumably yesterday's close) bars for the 231
symbols on which strategies then voted HOLD all day.

The pre-Bug-L 2026 set contained **18 entries**, of which a diff
against the authoritative NSE list (cross-checked via
Samco/Upstox/Zerodha/ET/Outlook Business) showed only 9 were correct.
**9 dates were spurious** (`02-17, 03-20, 03-30, 05-25, 07-07, 08-15,
08-17, 10-09, 10-21`) and **7 real holidays were missing** (`01-15,
03-26, 03-31, 05-28, 06-26, 09-14, 11-10`). Notably:

* `2026-05-28` (Bakri Id, Thursday) -- the date that bit us today.
* `2026-06-26` (Muharram, Friday) -- the next gap; would have bitten us in 29 days.
* `2026-07-07` was incorrectly tagged as Muharram (the real date is `06-26`).
* `2026-09-14` (Ganesh Chaturthi, Monday), `2026-11-10` (Diwali Balipratipada, Tuesday) -- both missing.

The previous audit finding B-6 (2026-05-25) caught the *year-coverage*
edge case (what happens after 2026-12-25 with no 2027 entries) but
did not check *within-year completeness*. The hardcoded set was
treated as authoritative without ever being diffed against the
official NSE schedule.

### 12.3 Why damage was zero today

The defensive stack absorbed everything:

1. **Opening lockout** (15-min post-open suppression) blocked the
   one SELL signal at 09:16.
2. **`allow_shorts: false`** (slot-1, live since 2026-05-26)
   would have blocked the same SELL signal anyway.
3. **`xgboost_classifier` disabled** (slot-2, live since 2026-05-27)
   prevented cross-signal amplification or directional flip.
4. **`mode: paper`** (per `health.json`): even an accepted signal
   would have produced a simulated trade, not a real broker order.

So the only cost was ~7h of CPU compute and ~169 wasted yfinance/
SmartAPI fetches per cycle × 193 cycles. The compute is on the
trader VM (not the user's laptop) and the broker session was idle,
not abused.

Had any of those 4 defenses been off (e.g. live mode + shorts
allowed) we'd have placed a real SELL order on a closed market at
09:16 IST. The broker would have rejected with a market-closed
error, generating an error in the log -- which would have flipped
the audit checkpoint to RED. So the damage trajectory degrades
gracefully, but only because we currently sit on a 4-layer defense.

### 12.4 Fix landed today

Three deliverables, all on `main`:

1. **`packages/core/data_handler.py:NSE_HOLIDAYS` rewritten for 2026.**
   * 16 entries (matches the official NSE count).
   * Each entry carries a `# DAY  Holiday Name` inline comment so a
     future maintainer can audit against any third-party source
     without re-reading code.
   * 30-line header block documenting the Bug L incident + a new
     **CONTRACT for future curators**: every entry MUST carry the
     festival name + day-of-week. A bare ISO date with no comment
     is a code-review red flag.
   * 2025 entries left untouched (Bug L scope was 2026 only;
     touching 2025 would risk breaking backtests).

2. **`tests/unit/test_holiday_calendar.py` -- 20 tests, all green.**
   * `test_2026_holiday_set_matches_authoritative_list` -- exact
     diff against the 16-date cross-source list. The other 19
     tests can pass while this one is broken; this one is the
     contract.
   * `test_2026_has_exactly_16_holidays` -- pins the count.
   * `test_bakri_id_2026_in_set` and `test_muharram_2026_date_correct`
     -- the two date-specific regression guards.
   * `test_no_spurious_2026_entries[...]` -- parametrized over all
     9 spurious dates; any of them coming back triggers a test
     fail.
   * `test_known_holiday_year_excludes_2027` -- enforces the
     B-6 contract.
   * Four `test_is_market_open_returns_false_on_known_2026_holidays`
     parametric tests + one `..._returns_true_on_regular_trading_day`
     sanity check. Uses a metaclass-based fake datetime so the
     mock doesn't break `datetime.strptime` calls downstream in
     `is_market_open()`.

3. **`tools/audit_checkpoint.py` -- new
   `_possible_missed_holiday_verdict()` + 14 tests
   (`tests/unit/test_audit_checkpoint_holiday_detector.py`).**
   The function returns a `YELLOW -- POSSIBLE_MISSED_HOLIDAY`
   verdict iff ALL of:

   * `now.weekday() < 5` (Mon-Fri)
   * `today_iso NOT IN NSE_HOLIDAYS` (else daemon would have idled)
   * `now >= 12:30 IST` (gives morning a chance)
   * `cycles_completed >= 5` (daemon was actively running)
   * `total_ensemble_acts == 0`
   * `closed_trades_today == 0`
   * `avg_directional_votes < 15`

   Today's 16:00 checkpoint would have produced this verdict
   instead of GREEN (cycles=6, acts=0, trades=0, votes=6.0, today
   was a Thursday not yet in NSE_HOLIDAYS). All 14 tests pass; the
   detector is intentionally conservative (only fires when ALL
   gates are met), and the live `NSE_HOLIDAYS` integration is
   tested via `test_uses_live_holiday_set_by_default` which uses
   the now-curated 2026-05-28 entry to verify the
   "today-is-already-a-known-holiday short-circuit" path.

4. **Full unit suite: 1436/1436 green** (was 1436 before, holiday
   tests added net +34 -- 20 calendar + 14 detector).

### 12.5 Severity & freeze-policy classification

* **Audit-only fix.** No live-trader behaviour changed -- the
   daemon already had a holiday check; we just gave it accurate
   data. The two new test files are unit-only.
* **No freeze-bypass slot consumed.** Same reasoning as Bug J
   (§1) and Bug K (§9).
* **Deployment:** trader VM pulls + restarts the container. No
   model reload, no config change beyond the holiday set, no
   risk of perturbing the backtester (which has its own VM).

### 12.6 What this catches going forward

* **2026-06-26 Muharram (Friday)** -- the next gap that would
   have repeated today's pattern in 29 days, now in the set.
* **2026-07-09 +** -- if any future NSE unscheduled closure
   slips past the curator, the audit checkpoint's YELLOW
   POSSIBLE_MISSED_HOLIDAY verdict will flag it within 60 minutes
   of the first checkpoint after 12:30 IST. The `trading-audit`
   Cursor skill already treats YELLOW as "highlight + suggest
   action" so the operator gets a direct prompt to cross-check
   the NSE calendar.

### 12.7 Files touched (this finding)

* `packages/core/data_handler.py` -- NSE_HOLIDAYS 2026 rewrite +
   30-line Bug L header.
* `tools/audit_checkpoint.py` -- `_possible_missed_holiday_verdict()`
   helper + wire into `_verdict()`.
* `tests/unit/test_holiday_calendar.py` -- new, 20 tests.
* `tests/unit/test_audit_checkpoint_holiday_detector.py` -- new,
   14 tests.

### 12.8 Backlog: Option B (API-fetched calendar)

Today's fix is Option D from the 17:00 IST plan-of-record: fix the
hardcoded calendar + add a YELLOW detector. Option B (replace the
hardcoded set with a daily NSE-bulletin scrape + fallback to
hardcoded on failure) is deferred. The case for it is real: even
with the perfect 2026 calendar, the next missed holiday is a
curator-attention question. The case against right now is that
fetching from NSE adds an external dependency to daemon startup
which could itself fail. Revisit post-Friday review.

---

## 10. Cross-references

* `findings_log_2026-05-25.md` §15 (Bug G self-audit), §16 (Bug H —
  xgboost missing from battery), §17 (Bug I — trader VM divergence).
* `changes_done_2026-05-27.md` — formal audit fix sweep (38 items).
* `findings_2026-05-27.md` — F-01..F-108 audit findings catalogue
  (independent from this operational log).
* `FREEZE_v2.1.md` — slot ledger (slot 1 LIVE+DURABLE, slot 2 LIVE
  today, slot 3 reserved).
* `diagnosis_sprint_2026-05-27.md` — 5-day investigative plan; §5
  here supersedes the H2 hypothesis (xgboost model bias) — answered
  CONFIRMED, with the empirical pkl-on-disk being the root cause
  rather than a calibration drift.

---

## 11. Files touched in this finding (writes only)

* `docs/findings_log_2026-05-27.md` — this file (§5 + §6 + §7 + §8 added)
* `docs/diagnosis_sprint_2026-05-27.md` — created earlier today
* `docs/FREEZE_v2.1.md` — slot reclassification (`8bcc360`)
* `config.yaml`:
  - `allow_shorts: true → false` (`8e1e926`)
  - `xgboost_classifier` commented out of `strategies.active`
    (`f32009c`)
* `packages/core/regime.py` — observability log (`e1df9e8`)
* `tests/unit/test_regime_and_gates.py` — 5 regression tests (`35927ea`)
* `logs/trades.csv` — manual_test rows removed (this commit)
* `logs/trades_manual_test_archive_2026-05-26.csv` — created (this
  commit)
* `packages/strategies/supertrend_follow.py` — P-03 vectorise + ATR
  cache + drop copy (`1fe1deb`)
* `packages/strategies/{rsi_momentum,vwap_bounce,mean_reversion,
  opening_range_breakout,moving_average_crossover}.py` — P-04 drop
  copy (`7f19990`)
* `packages/strategies/lstm_model.py` — P-11 numpy handoff + cached
  feature cols (`0809cf5`)
* `tests/unit/test_strategy_perf_invariants.py` — new (P-03 byte-
  identical + all-strategies mutation/determinism contracts)
* `tests/unit/test_audit_2026_05_27_fixes.py` — F-46 string assert
  updated for P-04 variable rename
* `data/battery_queue.yaml` — queue trim (`84f5acd`); slots #3-6
  dropped, holdout promoted to slot #3; ~144h compute saved.
* `tools/cloud/bootstrap_backtester.sh` — Bug J permanent fix
  (`31703bc`); three-way ownership split + writer probes (steps
  [7/8] and [8/8]) + 30-line comment linking back to §1.
* `tests/unit/test_bootstrap_backtester_perms.py` — new
  (`31703bc`); 7 file-text regression tests pinning the chown
  contract and probe presence so Bug J can't sneak back in.
* No risk-manager / position-sizer / ensemble code changes.
* No model files modified or replaced (the broken .pkl is left in
  place as forensic evidence; it cannot be loaded because the active
  strategies list excludes its consumer).
* `packages/research/battery.py` — *no changes today* despite §9
  (Bug K) documenting a defect in this file; the fix is queued
  for post-Friday so we don't disturb the running slot #3
  worker. The bug is also audit-only (research tool, not live
  trading), so deferring is safe.

---

## 13. Audit-2026-05-28 follow-up — Phase 1 of 5 landed (22 findings FIXED)

Today (2026-05-28, the Bakri Eid market holiday), in addition to the
Bug L holiday-calendar work in §12, a 6-angle production audit produced
**86 concrete findings with `file:line` citations** captured in
`docs/audit_2026-05-28_followup.md`. Operator directive: "fix all, but
don't deploy so the freeze slot won't be consumed". Interpretation:
make the fixes in code on `main`, ship across multiple sessions; slot
consumption is the deploy action, not the commit.

### 13.1 Phase split (5 phases for tractability)

* **Phase 1 (this session)** — 22 cheap, low-blast-radius, all-non-frozen
  findings. Mostly log promotions, fail-closed flips on silent-failure
  paths, in-cycle dedup caching, and the 4 perf quick-wins. Effort:
  ~3h of code + 20 regression tests.
* **Phase 2 (next session)** — 6 substantial findings: OBS-05 (boot
  reconcile fail-closed), STATE-02 (broker positionBook reconcile at
  boot), ORD-01/STATE-01 (wait for terminal status before mutating
  portfolio), ORD-02 (idempotency on retry), ORD-03 (broker-leg
  rollback on portfolio failure).
* **Phase 3 (next session)** — architectural: CONC-02..09 (WS hot path
  becomes enqueue-and-return + worker thread), ORD-06 (WS reconnect on
  JWT refresh), STATE-04..09 + STATE-11/12 (atomicity, persistence
  on lock-timeout, fail-closed on corrupt JSON, day-boundary
  validate). Larger blast radius; requires paper-mode regression.
* **Phase 4 (separate session)** — PERF-01 (LTP batch endpoint via
  AngelOne marketQuote), PERF-04..PERF-15. Touches broker code,
  needs paper regression.
* **Phase 5 (freeze-lift OR explicit slot)** — 11 findings on frozen
  files: 8 in `risk_manager.py` (NUM-03/04/08/09/12, OBS-04/19,
  CONC-01), 2 in `_trend_context.py` (NUM-05/15), 1 in
  `base_strategy.py` (OBS-10). Each touches `§What-is-frozen` per
  `FREEZE_v2.1.md` so must wait.

### 13.2 Phase 1 changeset (committed but NOT deployed)

#### Observability promotions / fail-closed flips (16)

| Finding | File | What changed |
|---|---|---|
| OBS-01 | `trading_agent.py:_check_position_exits_locked` | Failed SL/TP/peak-giveback exit → CRITICAL log + CRITICAL alert ("MANUAL ACTION REQUIRED") |
| OBS-02 | `trading_agent.py:_exit_on_signal` | Failed counter-signal exit → CRITICAL log + alert |
| OBS-03 | `trading_agent.py:_check_position_exits_locked` SL-PROPAGATE | DEBUG → WARNING + per-symbol failure counter (`_obs03_sl_propagate_failures`) |
| OBS-06 | `market_safety.py:check_data_quality` | Staleness/spike `except: pass` → WARNING + `staleness_check_failed` / `spike_check_failed` returns |
| OBS-07 | `trading_agent.py:risk_gate` | Added `logger.warning("[RISK-GATE] ...")` before audit row |
| OBS-08 | `trading_agent.py:_audit_reject` + `signal_audit.py:summarize_today` | Both swallows → rate-limited WARNING; read errors return `read_error` sentinel field |
| OBS-09 | `trading_agent.py:_on_tick store_tick` | Rate-limited (1/min) WARNING + suppression counter |
| OBS-11 | `execution.py:_verify_modify_trigger` | `orderBook()` failure → WARNING with order_id + expected trigger |
| OBS-12 | `data_handler.py:is_market_open` | Uncurated year fails CLOSED (was fail-open warning) — the Bug L pattern hardening |
| OBS-13 | `trading_agent.py:_refresh_market_context` | Nifty/VIX overlay `except: pass` → WARNING with "regime gating permissive" consequence |
| OBS-14 | `trading_agent.py:circuit_guard day high/low` | `except: pass` → WARNING with "partial-data mode" tag |
| OBS-15 | `trade_analyzer.py:evaluate_setup` | `except: pass` → WARNING with `repr(exc)` |
| OBS-16 | `execution.py:_persist_order` | DEBUG → WARNING with order_id/symbol/status |
| OBS-17 | `trading_agent.py:preflight alert` | `except: pass` → CRITICAL log + `logs/preflight_failed.flag` sticky file |
| OBS-18 | `websocket_client.py:Kite set_mode` | `except: pass` → WARNING with "feed degraded to LTP-only" |
| OBS-20 | `battery.py:_load_market_data_cache` | Added SHA256[:16] + mtime + absolute path to load log |

#### Numeric / correctness (2)

| Finding | File | What changed |
|---|---|---|
| NUM-13 | `trading_agent.py:_process_signal` | Rejection-cooldown short-circuit now calls `_audit_reject(..., "reject_cooldown:active")` |
| NUM-14 | `trading_agent.py:CASH-SIZE block` | `risk.min_cash_buffer_rs` (default Rs 200) reserved before affordability divide |

#### Operational (3)

| Finding | File | What changed |
|---|---|---|
| STATE-10 | `trading_agent.py:_setup_logging area` | Default kill-switch path is now `logs/STOP.<mode>` (live or paper) — distinct files per instance |
| ORD-04 | `trading_agent.py:_close_position_safely` | `place_order(..., order_type="MARKET")` forced on every exit path — no more LIMIT-pending exits on gapping symbols |
| ORD-12 | `trading_agent.py:_square_off_all` | Per-symbol close result accumulator; distinct "SQUARE-OFF INCOMPLETE" alert when any close fails; CRITICAL log per failure |

#### Concurrency / resource (2)

| Finding | File | What changed |
|---|---|---|
| CONC-11 | `portfolio.py:Portfolio.__init__` | `trade_history: List[TradeRecord]` → `deque(maxlen=10000)`; iteration/`len()` semantics unchanged |
| CONC-12 | `database.py` + `trading_agent.py:_periodic_cleanup` | New `purge_old_equity_points(days=90)` mirroring `purge_old_ticks`; called from the 100-cycle cleanup hook |

#### Runtime performance (4)

| Finding | File | What changed |
|---|---|---|
| PERF-02 | `trading_agent.py:_get_historical_cached` + `_evaluate_strategy` + `_trading_cycle` | Per-cycle `(symbol, timeframe) -> DataFrame` memo; cleared at cycle entry; hit/miss tallies tail-appended to `[CYCLE-DIGEST]` as `hist_cache=H/M`. With 300 symbols × 4 strategies expected dedup ratio ~4:1. |
| PERF-03 | `regime.py:classify_regime` + `classify_intraday_regime` | `[REGIME-INPUT]` / `[REGIME-INTRADAY-INPUT]` lines INFO → DEBUG. Test `tests/unit/test_regime_and_gates.py` updated to capture at DEBUG so the contract still pins content. |
| PERF-11 | `trading_agent.py:_snapshot_equity` + `_trading_cycle` | `_trading_cycle` stashes the just-fetched `current_prices` on `self._last_prices`; `_snapshot_equity` reuses (fallback to N+1 fetch only on the rare pre-market boot snapshot path) |
| PERF-12 | `trading_agent.py:_setup_logging` | File sink now `logger.add(..., enqueue=True)` — main thread no longer blocks on fsync I/O during chatty cycles |

### 13.3 Test coverage

`tests/unit/test_audit_2026_05_28_phase1.py` — **20 tests, all green**.
Mix of source-level assertions (cheaper, catch reverts via grep)
and runtime assertions (PERF-02 cache identity, OBS-06 pytz patch,
OBS-12 uncurated-year fake-datetime). One existing test pinned the
PERF-03 log level (`tests/unit/test_regime_and_gates.py::_capture_logs`)
and was updated to capture at DEBUG.

Full unit suite: **1,456 / 1,456 PASS** (was 1,436 before Phase 1
landed; +20 from the new file).

### 13.4 Deploy posture

**No deploy.** Per operator directive ("fix all, just don't deploy so
the slot won't be consumed"), Phase 1 is committed to `main` and
pushed but **not** rolled to the trader VM. The freeze policy treats
slot consumption as triggered by deploy / live behaviour change, not
by commit-on-main. Phase 5 (frozen files) is the only phase that
would otherwise need slot accounting; we will revisit when the bundle
is ready to deploy or when freeze-v2.1 lifts on 2026-06-08.

The trader VM continues to run commit `430069c` (Bug L). The next
deploy will batch some subset of Phase 1 + Phase 2 + Phase 3 once we
have paper-mode regression evidence — likely during the freeze-lift
review window.

### 13.5 Severity reassessment after Phase 1

The Phase 1 set is dominated by Medium-severity findings (silent-failure
fail-open paths). Critical findings still OPEN:

* **ORD-01 / STATE-01** — Live treats `status=="PLACED"` as fill. Phase 2.
* **ORD-02** — No idempotency on retry. Phase 2.
* **ORD-03** — No broker-leg rollback on portfolio failure. Phase 2.
* **STATE-02** — Boot reconcile skips broker-only positions. Phase 2.
* **NUM-01** — Short MIS margin 100% instead of 20% in backtester. Frozen-adjacent (portfolio.py is non-frozen but the change re-runs every battery → has to be paired with v2_holdout re-run). Phase 4.
* **OBS-01** — **FIXED** in Phase 1 (CRITICAL alert on failed flatten).
* **PERF-01** — LTP batch endpoint (needs broker work). Phase 4.
* **PERF-02** — **FIXED** in Phase 1 (in-cycle dedup; the in-cycle part
  of the audit's "4× immediately" claim).

So of the 8 audit-tagged Critical findings, 2 are now closed (OBS-01,
PERF-02). The remaining 6 (ORD-01/02/03, STATE-01/02, NUM-01, PERF-01)
are the order-state-truth / boot-reconcile / backtester-bias / broker-
batch-endpoint cluster — all targeted in Phases 2-4.

### 13.6 Files touched this commit batch

* `packages/core/market_safety.py` — OBS-06
* `packages/core/data_handler.py` — OBS-12
* `packages/core/execution.py` — OBS-11, OBS-16
* `packages/core/trade_analyzer.py` — OBS-15
* `packages/core/websocket_client.py` — OBS-18
* `packages/core/portfolio.py` — CONC-11
* `packages/core/database.py` — CONC-12 (new method)
* `packages/core/regime.py` — PERF-03
* `packages/core/signal_audit.py` — OBS-08
* `packages/research/battery.py` — OBS-20
* `trading_agent.py` — OBS-01/02/03/07/08/09/13/14/17, NUM-13/14, ORD-04/12, STATE-10, CONC-12 wiring, PERF-02/11/12
* `tests/unit/test_audit_2026_05_28_phase1.py` — new (20 regression tests)
* `tests/unit/test_regime_and_gates.py` — capture handler updated to DEBUG to track PERF-03 demotion
* `docs/audit_2026-05-28_followup.md` — Status column updated for 22 FIXED findings + phase 1 changelog entry

---

## 14. Audit-2026-05-28 follow-up — Phase 2 of 5 landed (6 findings, money-at-risk truth-telling)

**Date:** 2026-05-29 (Friday)
**Commit:** see `git log --grep=audit-2026-05-28-phase2`
**Deployment:** NOT deployed; freeze slot still NOT consumed.

### 14.1 What the phase closes

Phase 1 was about reducing silent-failure fail-open paths. Phase 2 is
the harder fix: **the daemon's in-memory model of broker truth was a
fiction in five distinct ways**, and every one of them could lose money
silently on a slow-fill or network-blip day. Each finding is now closed
in code with a regression test that pins the contract:

* **ORD-01 / STATE-01** — `_live_order_with_retry` previously returned
  immediately after `placeOrder` succeeded with `status="PLACED"` and
  `filled_price=None`. The caller then opened a portfolio position
  using the **signal-time price** as the fill price. On any LIMIT
  order this is a lie; on a fast-moving symbol with MARKET it's a
  bias; on a slow fill the portfolio thinks the trade is open while
  the broker still has it pending. Paper mode hid all of this
  (paper synth-fills instantly), which is why the bug went uncaught.
  The fix introduces a production `_wait_for_terminal()` helper
  patterned after `tools/test_live_single_trade.py::_wait_for_terminal`
  with shared terminal-status sets (`_TERMINAL_FILLED / _PARTIAL /
  _CANCELLED`). The wrapper now blocks for at most
  `live_order_fill_timeout_sec` (default 10s, configurable),
  populates `filled_price` from broker `averageprice`, computes
  slippage against the requested price, and returns `None` on
  REJECTED so the caller treats it as a failure.

* **ORD-02** — The broker wrapper itself documents the hazard:
  `placeOrder` may have placed the order even when it raises a
  timeout. The pre-fix retry would call `placeOrder` again,
  duplicating the position. The fix is the cheapest workable
  idempotency probe given that AngelOne has no client-supplied
  order tag: `_find_idempotent_match()` scans the broker
  `orderBook` for a recent order matching `(symbol, side, qty,
  ordertype)` within `idempotency_lookback_sec` (default 30s).
  Cancelled / rejected and stale rows are skipped. On retry
  attempts ≥ 2 the helper short-circuits the duplicate
  `placeOrder` and reuses the existing order_id.

* **ORD-03** — The pre-fix entry path placed the broker order +
  SL-M, then called `portfolio.open_position()`. If the portfolio
  call failed (DB write error, UNIQUE constraint, JSON-serialisation
  bug, future schema mismatch, exception in side-effect chain), the
  daemon logged "[TRADE-OPEN-FAILED]" and **moved on**, leaving the
  broker holding a real position + a real SL-M leg. New
  `ExecutionEngine.rollback_entry_on_portfolio_failure()` runs three
  steps best-effort: (1) cancel SL leg so it can't fire as a reverse
  trade; (2) place MARKET counter-flatten on the OPPOSITE side; (3)
  pop tracking artifacts from `_pending_orders` / `_order_log`. The
  caller wraps `portfolio.open_position` in try/except so both the
  return-False path AND the raise path land in the rollback. On
  partial rollback (counter-flatten or SL cancel fails) the symbol
  is added to `_symbols_blocked_by_rollback` and `_open_new_position`
  refuses re-entry on that symbol for the rest of the session.

* **STATE-02** — The pre-fix boot reconcile only iterated
  DB-restored positions. Crash-after-fill-before-DB-write window
  meant the daemon would boot "flat" while the broker held real
  exposure; the next cycle's entry on the same symbol would
  compound it into a double position. The reconcile now iterates
  every broker `positionBook` row with non-zero netqty and
  reports `status="broker_only"` for symbols absent from DB. The
  boot block in `trading_agent.py` no longer gates on
  `if self.portfolio.positions:` — broker-only detection MUST run
  even when DB is empty. The `broker_only` handler queues a
  CRITICAL alert and adds the symbol to `_stock_loss_today` so
  the per-symbol blacklist gate refuses new entries on that name
  for the session.

* **OBS-05** — Pre-fix, when `positionBook()` raised, the reconcile
  caught the exception, logged a WARNING, and returned every DB
  symbol as `skipped: api_error`. The caller treated this as
  "nothing to reconcile" and allowed entries to flow against
  possibly-stale state. The fix retries `positionBook()` up to 3
  times with 2/4s backoff. On final live-mode failure, the engine
  sets `boot_reconcile_failed_live=True`. New
  `TradingAgent._boot_reconcile_gate_open()` is checked at the top
  of `_open_new_position` and refuses every entry with audit
  reason `boot_reconcile_gate` until the operator touches
  `logs/boot_reconcile.ack`. Ack is one-shot (file consumed on
  first read) so a transient re-arm requires fresh ack.

### 14.2 Phase 2 changeset (committed but NOT deployed)

| Finding | File | What changed |
|---|---|---|
| ORD-01 / STATE-01 | `packages/core/execution.py` | New module-level `_TERMINAL_FILLED / _TERMINAL_PARTIAL / _TERMINAL_CANCELLED` sets. New `ExecutionEngine._wait_for_terminal()` poll loop. `_live_order_with_retry` now waits on the helper and populates `filled_price` / `filled_quantity` / `slippage` from broker truth. Returns `None` on terminal REJECTED. |
| ORD-02 | `packages/core/execution.py` | New `_find_idempotent_match()` and `_parse_broker_timestamp()` helpers. `_live_order_with_retry` retry loop short-circuits attempts ≥ 2 when an in-flight match is found. `idempotency_lookback_sec` config knob (default 30s). |
| ORD-03 | `packages/core/execution.py` + `trading_agent.py:_open_new_position` | New `rollback_entry_on_portfolio_failure()` (cancel SL → counter-flatten MARKET → cleanup). Caller wraps `open_position` in try/except; on rollback failure adds symbol to `_symbols_blocked_by_rollback`. New gate at top of `_open_new_position` refuses re-entry on rollback-blocked symbols. |
| STATE-02 | `packages/core/execution.py` + `trading_agent.py:307-498` | Reconcile iterates all broker positions; non-zero netqty for unknown symbols → `status="broker_only"`. Boot block now always invokes reconcile (not gated on `self.portfolio.positions`). New `broker_only` handler queues CRITICAL + stock-loss block. |
| OBS-05 | `packages/core/execution.py` + `trading_agent.py:_boot_reconcile_gate_open` | 3× retry with 2/4s backoff before fail-closed. New `boot_reconcile_failed_live` flag on engine. New `_boot_reconcile_gate_open()` checks flag + ack file. New global gate at top of `_open_new_position`. |

### 14.3 Test coverage

`tests/unit/test_audit_2026_05_28_phase2.py` — **27 tests, all green**:

* `test_ord01_*` (8) — `_wait_for_terminal` semantics, `averageprice`
  extraction, live order's `filled_price` contract, terminal-rejected
  → None, TTL behaviour.
* `test_ord02_*` (4) — idempotent-match positive case, cancelled-skip,
  stale-skip, retry-skips-placeOrder when match found.
* `test_ord03_*` (5) — rollback live happy-path, counter-flatten
  failure, SL cancel failure, paper-mode no-op + cleanup, source-level
  caller-side wiring assertion.
* `test_state02_*` (3) — broker-only detection, zero-netqty rows
  ignored, source-level caller-side handler assertion.
* `test_obs05_*` (7) — 3× retry contract, transient recovery doesn't
  trip the gate, paper-mode never trips, gate-open semantics, ack-file
  consumption, gate-cleared-when-flag-never-set, source-level
  `_open_new_position` gate-check assertion.

Full unit suite: **1,483 / 1,483 PASS** (was 1,456 before Phase 2; +27
from the new file).

### 14.4 Deploy posture (unchanged)

**No deploy.** Same as Phase 1: code is on `main` and pushed, but the
trader VM continues to run commit `430069c` (Bug L). The freeze policy
treats slot consumption as triggered by deploy / live behaviour change,
not by commit-on-main. Phase 2 includes paths that *will* matter at
deploy time (live `_wait_for_terminal` blocks up to 10s per entry; the
boot-reconcile gate could refuse entries on a flaky positionBook day),
so the deploy plan should batch Phase 1 + Phase 2 + Phase 3 once we
have paper-mode regression evidence and the freeze lifts on
2026-06-08.

### 14.5 Severity reassessment after Phase 2

The 8 audit-tagged Critical findings are now:

* ORD-01 / STATE-01 — **CLOSED in Phase 2.**
* ORD-02 — **CLOSED in Phase 2.**
* ORD-03 — **CLOSED in Phase 2.**
* STATE-02 — **CLOSED in Phase 2.**
* OBS-01 — closed in Phase 1.
* PERF-02 — closed in Phase 1.
* NUM-01 — Phase 4 (backtester re-run gating).
* PERF-01 — Phase 4 (LTP batch endpoint, broker work).

So **6 of 8 audit-tagged Critical findings are now FIXED in code**, all
freeze-safe, none deployed. Two remain (NUM-01 and PERF-01), both in
Phase 4 territory because they touch broker-batch endpoints or the
backtester re-run policy.

### 14.6 Files touched this commit batch

* `packages/core/execution.py` — ORD-01, ORD-02, ORD-03 helpers + `_wait_for_terminal` integration in `_live_order_with_retry` + STATE-02 broker-only loop + OBS-05 retry/backoff + new `boot_reconcile_failed_live` flag.
* `trading_agent.py` — ORD-03 entry-path try/except + rollback wiring + `_symbols_blocked_by_rollback` gate + STATE-02 `broker_only` handler + OBS-05 boot-reconcile gate state + new `_boot_reconcile_gate_open()` helper + new `Path` import.
* `tests/unit/test_audit_2026_05_28_phase2.py` — new (27 regression tests).
* `docs/audit_2026-05-28_followup.md` — Status column updated for the 6 Phase-2 FIXED findings; new "Phase-2 landed" header section.
* `docs/findings_log_2026-05-27.md` — this section (§14).

---

## 15. Phases 3-5 sprint (2026-05-29) — concurrency, performance, and frozen-file closure

### 15.1 Where we landed

After Phase 2 closed the money-at-risk truth-telling cluster (ORD-01,
STATE-01/02, ORD-02/03, OBS-05), the remaining 38 audit findings split
naturally into three buckets:

* **Phase 3** — concurrency + state hygiene (CONC-02..09, STATE-03/04/06/08/09/11/12, ORD-06).
* **Phase 4** — runtime performance (PERF-01/04/05/06/08/09/10/14/15).
* **Phase 5** — frozen-file semantic correctness (NUM-02/03/04/05/08/09/12/15, OBS-04/10/19, CONC-01).

All three were landed back-to-back on 2026-05-29 with NO deploy. The
trader VM remains on `430069c` and the backtester on `84f5acd`.

### 15.2 Phase 3 (commit `d1beea5`)

15 findings closed. Highlights:

* **ORD-06** — JWT refresh now propagates the new SmartConnect handle
  to `ws_client.update_broker_session(force_reconnect=True)`. Pre-fix
  the WS thread kept running on the stale auth_token + feed_token
  until AngelOne stopped servicing them, which silently killed the
  tick feed for the rest of the session. CRITICAL log on WS-update
  failure makes the partial-state visible to the operator.

* **CONC-02 / CONC-04 / CONC-06** — three race conditions on the WS
  hot path: trail mutation outside the exit lock, candle-close
  callback fired under the aggregator lock (DB writes blocked tick
  ingestion), and `_subscriptions` iteration paths racing watchlist
  hot-loads. All three closed by lock-placement edits.

* **CONC-08 / CONC-09** — `TradingAgent.run` installs SIGTERM/SIGINT
  handlers that flip `_running = False`; `_shutdown` joins the WS
  worker (5s budget) before tearing down the DB. Eliminates the
  daemon-thread WS race that occasionally wrote a tick to a half-
  closed sqlite handle.

* **STATE-04** — atomic close. `Database.close_position_atomic`
  wraps DELETE open_positions + INSERT trades + INSERT equity_curve
  in one commit. `Portfolio.close_position` routes through it; the
  CSV append happens AFTER the DB commit so a crash mid-close can
  no longer leave the on-disk record set inconsistent.

* **STATE-06** — file-lock retry-with-backoff (1s -> 3s -> 5s) across
  cooldown / runtime-state / trail persistence. The pre-fix unlocked-
  fallback was itself the clobber-on-restart bug it was trying to
  avoid.

* **STATE-08** — debounced (5s) trail persist on every WS-tick
  mutation. A `trail_mutated` gate (highest / lowest / active /
  breakeven flips) prevents no-op ticks from burning the debounce
  budget. Closes the "crash 5s before next persist restored the
  wide initial SL" hole.

* **STATE-09** — corrupt cooldown JSON now writes
  `data/cooldowns_corrupt.flag`; `TradingAgent` reads it at boot
  and engages a fail-closed gate that refuses new entries until
  the operator deletes the flag. Replaces the previous "graceful
  empty-dict load" that quietly let blacklisted symbols trade
  again.

* **STATE-11** — signal-audit retry queue. Bounded (500-row) deque;
  flushed best-effort on every `log()` call. A 1s NFS hiccup no
  longer permanently loses a row.

* **STATE-12** — daily reset stale-MIS sweep. Any open position with
  `entry_time.date() < today (IST)` is closed via
  `close_position(..., reason="stale_overnight_mis_sweep")` BEFORE
  the in-memory maps clear. The next reconcile catches anything the
  broker is genuinely holding.

* **CONC-05 / PERF-05** — tick batching. Per-tick INSERT replaced
  with a 5000-row in-memory deque; flushed at 100 rows or 1s. Final
  flush in `_shutdown`. Eliminates the ~50 sqlite connections/sec
  WS hot path on a 50-symbol watchlist.

**Architectural deferrals**: CONC-03 (WS enqueue+return + worker
thread) and STATE-05 (boot recovery of `_pending_orders`) are
queued for a focused architectural session. The phase-3 surgical
fixes above close the most painful concurrency hot spots; the
worker-thread restructure changes the threading model end-to-end
and deserves its own session.

Test coverage: 28 new tests in `tests/unit/test_audit_2026_05_28_phase3.py`.
Full suite: 1,511 / 1,511.

### 15.3 Phase 4 (commit `4b96024`)

9 PERF findings closed. Highlights:

* **PERF-01** — `AngelOneDataSource.get_ltp_batch` wraps
  `getMarketData(mode=LTP)` with 50-token chunking + per-chunk
  rate-limited dispatch. `DataHandler.get_multiple_ltp` now prefers
  the batch endpoint and falls back to the per-symbol `ltpData`
  loop only for tokens the batch returned None for. At 300
  symbols/cycle this collapses ~300 REST calls into ~6.

* **PERF-04** — entry-path ATR derived from `snap.atr_pct *
  current_price / 100` instead of a redundant 6h fetch. Saves
  ~1-2 s per entry attempt before gate logic fires. Falls back
  to the explicit fetch when snap is empty.

* **PERF-06** — server-side filter on `(strategy, regime)` in
  `Database.load_trade_patterns` (covered by the new
  `idx_trades_strategy_regime` index). Pre-fix every entry attempt
  loaded the most recent 200 rows then Python-filtered ~150 of
  them away.

* **PERF-08 / PERF-09 / PERF-10** — candle-store via `executemany`,
  Yahoo session reuse across refreshes, and three covering DB
  indexes + 64MB per-conn `cache_size` pragma. Together prevent
  the 10x degradation we'd see as the trades / equity_curve / patterns
  tables age past 30 days.

* **PERF-14** — `TradingAgent._run_scan_async` runs the scanner on a
  daemon thread; the periodic-rescan call site uses it. Atomic
  watchlist swap on completion. Boot-time + pre-market warm-up still
  call `_run_scan` synchronously because the initial watchlist has
  to settle before trading starts.

* **PERF-15** — `docker-compose.yml` caps trader at 1.5 vCPUs (and
  reserves 0.5) so the WS thread, healthcheck, and audit_checkpoint
  always have headroom on the 2-vCPU OCI box.

**Deferrals**: PERF-07 (DataFrame allocation profiling) and PERF-13
(battery worker pickle).

Test coverage: 17 new tests in `tests/unit/test_audit_2026_05_28_phase4.py`.
Full suite: 1,528 / 1,528.

### 15.4 Phase 5 (commit `ec957ef`, freeze-bypass slot 3 of 3)

12 findings closed. Touches three frozen files
(`risk_manager.py`, `_trend_context.py`, `base_strategy.py`) -- this
commit consumed the 3rd FREEZE_v2.1 bypass slot. NOT deployed.

Highlights:

* **NUM-02** — Kelly post-sizing zero now audit-rejects
  `sizing:zero_qty` instead of forcing 1 share. F-34 regression
  closed.

* **NUM-03** — new `RiskManager.sync_balance_from_mtm(equity)`,
  called BEFORE `can_trade` each cycle so sizing / drawdown reads
  fresh equity. Pre-fix the balance only updated on closes, leaving
  sizing math blind to mid-session drawdown.

* **NUM-04** — `round_to_tick(price, side, kind, tick=0.05)`
  helper added; `get_atr_stop_loss` and `enforce_sl_floor` route
  SL prices through it (round AWAY from entry). `execution.py`
  adoption queued for next session.

* **NUM-05 / NUM-15** — `_trend_context._fetch_daily(symbol,
  as_of_date=...)` drops the LAST daily bar when its date >= the
  as-of date (defaults to today IST). Cache key includes the as-of
  date so backtest sweeps with different cutoffs no longer cross-
  pollute. Closes the live-lookahead in every strategy with
  `trend_filter_pct` set.

* **NUM-08** — `is_trade_worth_taking` short-side
  `compute_round_trip` mapping: explicit `(buy_leg, sell_leg) =
  (TP, entry)` for shorts. Pre-fix the symmetric max/min mapping
  fed the charges calculator the WRONG leg (STT undercounted ~20%
  on shorts).

* **NUM-09** — `classify_regime` (regime.py, NOT frozen) now uses
  `_is_finite_number` so NaN/inf VIX returns "unknown" instead of
  falling through to `bull_low_vol` with full multipliers.

* **NUM-12 / OBS-19** — `regime_size_multiplier` returns the
  configured `unknown` multiplier (default flipped 1.00 -> 0.50)
  when regime is None / "unknown". Cold-boot before first
  market_context refresh now sizes at HALF instead of full.

* **OBS-04** — `is_trade_worth_taking` fail-closes on
  `compute_round_trip` exception (CRITICAL log + `(False,
  "charges_compute_failed")`). Replaces the previous fabricated
  0.1% charges fallback.

* **OBS-10** — `BaseStrategy._atr` logs WARNING with `type +
  repr(exc)` on exception (and on EWM-NaN result). Returns 0.0 so
  existing zero-ATR guards in `RiskManager` fire as designed.

* **CONC-01** — `TradingAgent` calls
  `risk_manager.update_open_positions(portfolio.open_position_count)`
  immediately after `create_trailing_stop`. Pre-fix the count
  refreshed only at cycle end, allowing two consecutive entries
  in the same cycle to BOTH read the pre-cycle count and breach
  `max_open_positions` by 1.

Test coverage: 18 new tests in `tests/unit/test_audit_2026_05_28_phase5.py`,
plus 2 existing tests in `test_risk_manager.py` updated to pin the
new conservative-default contract. Full suite: 1,546 / 1,546.

### 15.5 Bypass ledger

* **Slot 1** — `8e1e926` (allow_shorts=false durable).
* **Slot 2** — `f32009c` (xgboost_classifier disable).
* **Slot 3** — `ec957ef` (Phase 5 frozen-file fixes; NOT deployed).

All three slots are now consumed. Any further frozen-file edit
before the freeze lifts on 2026-06-08 requires explicit lift /
override.

### 15.6 Severity reassessment after all 5 phases

The 8 audit-tagged Critical findings are now:

* **ORD-01 / STATE-01** — CLOSED (Phase 2).
* **ORD-02** — CLOSED (Phase 2).
* **ORD-03** — CLOSED (Phase 2).
* **STATE-02** — CLOSED (Phase 2).
* **OBS-01** — CLOSED (Phase 1).
* **PERF-02** — CLOSED (Phase 1).
* **PERF-01** — CLOSED (Phase 4).
* **NUM-01** — OPEN (backtester sizing; awaits the post-Friday
  policy review and a separate fix in `portfolio.py`).

So **7 of 8 audit-tagged Critical findings are now FIXED in code**.
The one remaining (NUM-01) is a backtester-only correctness issue;
the live trader is unaffected.

Total findings closed across phases 1-5: **63 of 86** (73%). The
remaining 23 are split between architectural deferrals (CONC-03,
STATE-05), the misc-OPEN bucket (NUM-01/06/07/10/11, ORD-05/07/08/
09/10/11), and the two PERF deferrals (PERF-07/13).

### 15.7 Files touched (phases 3-5)

* `packages/core/cooldown_persistence.py` — STATE-06, STATE-09.
* `packages/core/database.py` — STATE-04, PERF-06, PERF-08, PERF-10.
* `packages/core/data_handler.py` — PERF-01.
* `packages/core/portfolio.py` — STATE-04.
* `packages/core/regime.py` — NUM-09.
* `packages/core/risk_manager.py` — NUM-03, NUM-04, NUM-08, NUM-12,
  OBS-04, OBS-19. (Frozen file; freeze-bypass slot 3.)
* `packages/core/runtime_state_persistence.py` — STATE-06.
* `packages/core/signal_audit.py` — STATE-11.
* `packages/core/tick_aggregator.py` — CONC-04.
* `packages/core/trade_analyzer.py` — PERF-06.
* `packages/core/trailing_stop_persistence.py` — STATE-06.
* `packages/core/websocket_client.py` — CONC-06, CONC-09.
* `packages/strategies/_trend_context.py` — NUM-05, NUM-15. (Frozen.)
* `packages/strategies/base_strategy.py` — OBS-10. (Frozen.)
* `trading_agent.py` — ORD-06, CONC-02, CONC-08, STATE-03, STATE-08,
  STATE-09 gate, STATE-12, CONC-05/PERF-05 buffer, NUM-02, NUM-03
  call site, CONC-01 wiring, PERF-04 ATR derivation, PERF-09 session
  reuse, PERF-14 async scan dispatcher.
* `docker-compose.yml` — PERF-15.
* `tests/unit/test_audit_2026_05_28_phase3.py` — new (28 tests).
* `tests/unit/test_audit_2026_05_28_phase4.py` — new (17 tests).
* `tests/unit/test_audit_2026_05_28_phase5.py` — new (18 tests).
* `tests/unit/test_risk_manager.py` — 2 tests updated to pin the
  NUM-12 conservative-default contract.
* `docs/audit_2026-05-28_followup.md` — Status column updated for
  the remaining 36 newly-FIXED findings; CONC-03 and STATE-05
  re-tagged DEFERRED with rationale; changelog appended.
* `docs/findings_log_2026-05-27.md` — this section (§15).

---

## 16. Misc-OPEN bucket — Group C: live order discipline (ORD-05/07/08/09)

**Date:** 2026-05-29 (late evening IST)
**Commit:** PENDING (this section is being written before the commit)
**Status:** 4 findings FIXED, NOT deployed.

### 16.1 What was broken

Even after Phase-2 wired `_wait_for_terminal` into `_live_order_with_retry`,
four "live order discipline" findings remained: the engine could
still mis-account fills around the cancel-race window, place SLs at
the wrong size on partial fills, and silently abandon timed-out
orders without any reconciliation hook.

**ORD-09** — `_live_order_with_retry` would return `None` on a
TTL-expiry **without cancelling** the order. The order could still
fill at the broker minutes later; the daemon had already moved on
and would NOT see the fill.

**ORD-08** — When the entry order partially filled (e.g. 7 of 10
shares), the SL-M was sized off the **requested** quantity (10),
producing an over-sized standing SL that, if it triggered, would
open a 3-share reverse position on top of the legitimate close.

**ORD-07** — `get_order_status` and the `_wait_for_terminal` helper
existed but were not wired into the **exit** path. The Phase-2 fix
covered entries; this confirms exits inherit the same contract via
`place_order → _live_order_with_retry`.

**ORD-05** — `_close_position_safely` issued a cancel-then-flatten
sequence with no atomicity. If the broker SL fired after we sent
the cancel but before it processed (the cancel-race window), the
flatten was sent on top of an already-flat position → the next tick
opened an unintended **reverse** position. This is the same race
we already pin under `test_atomic_close_*` for the entry side
(ORD-03); the exit side was missing equivalent protection.

### 16.2 Fixes

**ORD-09 — TTL cancel-and-fail with race re-check**
(`packages/core/execution.py:_live_order_with_retry`).

```
last_seen = _wait_for_terminal(order_id, ttl_sec)
if last_seen is None:
    cancelled = cancel_order(order_id, variety="NORMAL")
    terminal_after_cancel = get_order_status(order_id)
    if terminal_after_cancel is FILLED / PARTIALLY_FILLED:
        # Race: filled in the cancel window. Accept the fill,
        # promote PARTIAL → FILLED if filledshares > 0, log
        # ORD-09-RACE-FILLED warning. Continue to SL placement.
    else:
        # True timeout. Pop _pending_orders[order_id], log
        # ORD-09 error, return None. Caller's idempotency probe
        # will catch any stragglers in the next attempt.
```

The race re-check is the critical bit: a naive cancel-and-return-None
would lose any fill that landed in the ~50 ms cancel-race. We've
seen this pattern in production logs from 2026-05-21 onwards.

**ORD-08 — SL sized off filled_quantity**
(`packages/core/execution.py:_live_order_with_retry`).

```
effective_sl_qty = int(result.get("filled_quantity") or quantity)
if effective_sl_qty <= 0:
    effective_sl_qty = quantity   # defensive fallback
sl_id = _place_sl_order(symbol, token, effective_sl_qty, sl, sl_side)
_sl_orders_by_symbol[symbol]["quantity"] = effective_sl_qty
```

The reconciliation path (`reconcile_sl_orders_from_broker`) already
respects the broker's reported SL quantity, so this is consistent
across boot recovery as well.

**ORD-07 — exit path inherits Phase-2 wait-for-terminal.**
No code change needed: `place_order` already routes through
`_live_order_with_retry` for both entries and exits. Three
source-level test pins guard against future regressions:

* `test_ord07_place_order_calls_live_order_with_retry` —
  `place_order` always invokes `_live_order_with_retry` in live mode.
* `test_ord07_live_order_uses_wait_for_terminal` —
  `_live_order_with_retry` calls `_wait_for_terminal` after
  the broker `placeOrder` returns.
* `test_ord07_close_position_safely_uses_place_order` —
  `_close_position_safely` invokes `execution.place_order`
  for the flatten leg.

**ORD-05 — atomic cancel-then-flatten race**
(`trading_agent.py:_close_position_safely`).

```
sl_meta = execution.get_sl_order_for_symbol(symbol)   # ← was missing
cancel_ok = execution.cancel_sl_order_for_symbol(symbol)

if sl_meta and not paper_mode:
    sl_status = execution.get_order_status(sl_meta["order_id"])
    if sl_status indicates FILLED:
        # Race won by SL. Skip flatten; reconcile portfolio
        # using the SL fill price + broker filledshares. Log
        # ATOMIC-CLOSE-RACE alert. Return early.
# else: SL was cancelled cleanly OR was already absent → continue
# to original flatten logic.
```

The "skip flatten" path uses `portfolio.close_position(price=sl_fill_price)`
so the books are correct without an extra round-trip. The legacy
flatten path is preserved for the common case.

A small hardening also went in alongside: the `sl_meta` reference
is type-guarded (`if not isinstance(sl_meta, dict): sl_meta = None`)
because some legacy mock setups in `test_exit_check_thread_safety.py`
returned a string — the production code never observed this in
practice but the guard prevents a crash if a future mock or shim
returns the wrong type.

### 16.3 Test coverage

**New regression suite** (`tests/unit/test_audit_2026_05_28_misc.py`):

* **ORD-05** (1 test) — source-level anchor verifying
  `_close_position_safely` retrieves `sl_meta`, calls
  `get_order_status` after cancel, branches on `sl_filled_first`,
  and emits the `ATOMIC-CLOSE-RACE` alert string.
* **ORD-07** (3 tests) — source-level anchors confirming the
  exit path is wired through `place_order` →
  `_live_order_with_retry` → `_wait_for_terminal`.
* **ORD-08** (2 tests) — source-level anchors confirming the SL
  is sized off `filled_quantity` AND the size is persisted into
  `_sl_orders_by_symbol`.
* **ORD-09** (2 tests) — one runtime test (cancel-on-TTL behaviour
  + `_pending_orders` cleanup + `None` return) and one
  source-level anchor verifying the race re-check via
  `get_order_status` after the cancel.

**Existing test fixups** (legacy suites pinned to the new contract):

* `tests/unit/test_execution_sl_tracking.py` — 12 tests refixtured
  with the new `_seed_orderbook(api, *order_ids)` helper and an
  ultra-short `live_order_fill_timeout_sec=0.05` to keep runtime
  flat. The pre-fix suite assumed `placeOrder` returning an id
  was equivalent to a fill; under ORD-09 that's now a TTL miss.
* `tests/unit/test_audit_2026_05_28_phase2.py::test_ord01_live_order_keeps_placed_status_on_ttl_with_no_terminal`
  — re-pinned to the new ORD-09 contract: TTL with no terminal
  returns `None` AND issues a cancel call.
* `tests/integration/test_trade_perspective_fixes.py::test_floor_disabled_when_zero`
  — already aligned in Misc-A to `99.50` (NUM-04 tick rounding).

**Suite results:**

* Unit: **1,588 / 1,588** PASSED.
* Integration: **248 / 248** PASSED.

### 16.4 Files touched

* `packages/core/execution.py` — ORD-08 + ORD-09.
* `trading_agent.py` — ORD-05 + sl_meta type-guard.
* `tests/unit/test_audit_2026_05_28_misc.py` — 8 new tests for
  ORD-05/07/08/09.
* `tests/unit/test_execution_sl_tracking.py` — `_seed_orderbook`
  helper + `live_order_fill_timeout_sec` shortening + 12 tests
  re-fixtured.
* `tests/unit/test_audit_2026_05_28_phase2.py` — 1 test re-pinned
  to ORD-09 contract.

### 16.5 What's left in the misc-OPEN bucket

Done in this commit: ORD-05, ORD-07, ORD-08, ORD-09.

Remaining (4 groups, 6 findings):

* **Group D** — NUM-11 (live slippage capture parity with paper)
  + ORD-11 (per-symbol slippage tolerance circuit breaker).
* **Group E** — ORD-10 (reactive re-auth on `401` / `AB*` error
  classes; the 7-hour proactive timer is too coarse).
* **Group F** — NUM-10 (decimal arithmetic for charges; touches
  `charges.py` + `portfolio.py`).
* **Group G** — PERF-07 (DataFrame allocation cache) +
  PERF-13 (battery worker pickle); deferrable, backtester-only.

The Critical-tagged finding NUM-01 is already CLOSED (commit
`03ba66d`); ORD-* remaining are all Medium-tagged.

---

## 17. Misc-OPEN bucket — Group D: live slippage parity + tolerance circuit breaker (NUM-11/ORD-11)

**Date:** 2026-05-29 (night IST)
**Commit:** PENDING (this section is being written before the commit)
**Status:** 2 findings FIXED, NOT deployed.

### 17.1 What was broken

**NUM-11** — Paper applied an *adverse* slippage draw of [0,
slippage_tolerance_pct]% on every fill, so the backtester
systematically under-reported headline P&L vs live.

**ORD-11** — Live fills recorded `slippage` as the absolute Rs
delta between fill and requested price, but never validated it
against `slippage_tolerance_pct`. A runaway fill (illiquid name,
RMS lifting the protective gate, MARKET order on a wide quote)
was invisible to ops until the next manual P&L review.

The combination meant the backtester and the live broker were
emitting non-comparable shapes and the live-side guardrail was
absent.

### 17.2 Fixes

**Single source of truth: `ExecutionEngine._record_slippage(result, requested_price)`.**

```
def _record_slippage(self, result, requested_price):
    fp = float(result.get("filled_price"))
    if fp is None or requested_price <= 0 or fp <= 0:
        result["slippage_pct"] = None
        result["slippage_breach"] = False
        return
    slip_pct = abs(fp - requested_price) / requested_price * 100.0
    result["slippage_pct"] = round(slip_pct, 4)
    result["slippage_breach"] = slip_pct > (slippage_tolerance + 1e-9)
    if result["slippage_breach"] and result["mode"] == "live":
        logger.critical("[ORD-11-SLIPPAGE] ...")
        if halt_symbol_on_slippage_breach:
            self._slippage_breached_symbols.add(symbol)
```

Wired into every result-emission path:

* `_paper_order` — paper fills (replaces the inline `slippage`
  computation; the legacy `slippage` Rs absolute field is preserved
  for back-compat).
* `_live_order_with_retry` — live FILLED + PARTIALLY_FILLED branch.
* `_live_order_with_retry` — ORD-09 race-FILLED + race-PARTIAL
  branches (so a fill that lands in the cancel window also gets
  the breach check).
* `get_order_status` — passive observation path, so cycle-end
  snapshots also see breach state.

**Trading-agent gate (entry path only).**

`TradingAgent._open_new_position` now consults
`execution.is_symbol_slippage_blocked(symbol)` immediately after the
`ROLLBACK-BLOCK` check. If True it logs `[SLIPPAGE-BLOCK]`, calls
`_audit_reject(signal, current_price, "slippage_block:breach")`, and
returns. Exits / square-off / SL trail paths route through
`execution.place_order` directly and are NOT gated -- this matters
because the operator might WANT to flatten a position whose entry
slippage breached.

**Configuration knobs.**

* `execution.slippage_tolerance_pct` — already existed (default
  0.10%); now also used as the live breach threshold.
* `execution.halt_symbol_on_slippage_breach` — new (default False).
  Conservative default: ops opts in. When True, breaches add to the
  in-memory blocklist and gate new entries until cleared.

**Public ops API.**

* `ExecutionEngine.is_symbol_slippage_blocked(symbol) -> bool`
* `ExecutionEngine.clear_slippage_block(symbol) -> bool`
* `ExecutionEngine.get_slippage_breached_symbols() -> set` (returns
  a copy so callers can't mutate state).

### 17.3 Test coverage

**New `TestNUM11SlippageParity`** (5 tests):

* `test_paper_result_carries_slippage_pct_and_breach_flag` -- paper
  result dict has both new keys; paper draws stay within the
  tolerance band.
* `test_live_filled_result_carries_slippage_pct` -- live FILLED
  result has slippage_pct + breach=False for a 0.05% slip vs 0.10%
  tolerance.
* `test_slippage_pct_is_zero_when_fill_matches_request` -- exact
  fill = 0% slip, no breach.
* `test_record_slippage_returns_none_when_inputs_invalid` --
  defensive: missing fill_price OR requested_price=0 yields
  slippage_pct=None and breach=False without raising.
* `test_get_order_status_path_also_records_slippage_pct` -- the
  passive observer keeps the pending-orders cache in sync.

**New `TestORD11SlippageCircuitBreaker`** (7 tests):

* `test_breach_on_live_fill_logs_critical_anchor` -- 1.0% slip vs
  0.10% tolerance → result["slippage_breach"] is True; halt
  disabled so symbol NOT blocked.
* `test_breach_with_halt_flag_blocks_symbol` -- with halt enabled,
  breached symbol is in the blocklist; snapshot is a copy.
* `test_clear_slippage_block_lifts_gate` -- explicit clear lifts
  the gate; clearing an unknown symbol returns False without
  raising (idempotent).
* `test_within_tolerance_does_not_block` -- 0.10% slip == 0.10%
  tolerance: epsilon-aware → NOT a breach.
* `test_partial_fill_also_records_slippage_pct` -- paper partial
  fills also emit slippage_pct + breach.
* `test_open_new_position_consults_slippage_block` -- source-level
  pin so a future refactor can't silently drop the entry-path
  gate. Also pins the audit_reject reason string so ops dashboards
  remain stable.
* `test_anchor_in_execution_source` -- pins `_record_slippage`,
  `is_symbol_slippage_blocked`, `clear_slippage_block`,
  `[ORD-11-SLIPPAGE]`, and `halt_symbol_on_slippage_breach` so
  greppable audit IDs survive future refactors.

**Suite results:**

* Unit: **1,600 / 1,600** PASSED.
* Integration: **248 / 248** PASSED.

### 17.4 Files touched

* `packages/core/execution.py` -- `_record_slippage` helper +
  `_slippage_breached_symbols` state + 3 public methods +
  wiring into 4 result-emission paths.
* `trading_agent.py` -- `_open_new_position` slippage block gate.
* `tests/unit/test_audit_2026_05_28_misc.py` -- 12 new tests
  (TestNUM11SlippageParity + TestORD11SlippageCircuitBreaker).

### 17.5 Honest caveat

The default is `halt_symbol_on_slippage_breach=False` so the
log-level alert fires but no auto-halt happens. This is deliberate:
NSE has plenty of legitimate ~0.30% spreads on illiquid mid-caps
right at open, and a hair-trigger circuit breaker would block more
real entries than runaway fills. Ops should review the
`[ORD-11-SLIPPAGE]` log entries for a few days, calibrate
`slippage_tolerance_pct` per universe, and then flip
`halt_symbol_on_slippage_breach` to True once the false-positive
rate is acceptable.

### 17.6 What's left in the misc-OPEN bucket

Done in this commit: NUM-11, ORD-11.
Done in `d578ff1`: ORD-05, ORD-07, ORD-08, ORD-09.
Done in `da7ab69`: NUM-06, NUM-07.
Done in `03ba66d`: NUM-01.

Remaining (3 groups, 4 findings):

* **Group E** — ORD-10 (reactive re-auth on `401` / `AB*` error
  classes; the 7-hour proactive timer is too coarse).
* **Group F** — NUM-10 (decimal arithmetic for charges; touches
  `charges.py` + `portfolio.py`).
* **Group G** — PERF-07 (DataFrame allocation cache) +
  PERF-13 (battery worker pickle); deferrable, backtester-only.

---

## 18. Misc-OPEN bucket — Group E: reactive re-auth on auth-class broker errors (ORD-10)

**Date:** 2026-05-30 (early morning IST)
**Commit:** PENDING (this section is being written before the commit)
**Status:** 1 finding FIXED, NOT deployed.

### 18.1 What was broken

Pre-fix, `TradingAgent._maybe_refresh_broker_session` re-logged in
**only** when the local clock said the JWT was older than 7 hours.
That covered the most common AngelOne case (8h JWT lifetime) but
missed every scenario where the broker invalidates the token EARLIER:

* operator logs in from another device → AngelOne force-logs out the
  original session,
* broker-side session reset for compliance / RMS reasons,
* network MITM strips the auth header so AngelOne returns 401 even
  though our clock says the JWT is fresh.

In any of those cases, every API call comes back with an auth-class
error (`AB1010`, `AB1011`, `AB2001`, `Session Expired`, `Invalid
Token`, 401/403). `_live_order_with_retry` would burn its three
retry attempts on a stale JWT and silently report
`"Order FAILED after 3 attempts"`. Held positions could end up
without a working broker connection until the proactive 7-hour
timer kicked in -- which, on a freshly-restarted daemon, was hours
away.

### 18.2 Fix design

Three pieces:

**(1) Module-level error classifier in `execution.py`.**

```python
def classify_smartapi_error(payload) -> str:
    # Returns one of: "auth", "rate_limit", "transient", "fatal".
```

`payload` may be an exception, a SmartAPI dict
(`{"status": False, "errorcode": "AB1011", "message": "..."}`), or a
plain string. The classifier looks at:

* Known AngelOne codes: `AB1010`, `AB1011`, `AB1014`, `AB1019`,
  `AB2001`, `AB2002`, `AB2003`.
* Word-fenced 401 / 403 status hints (so an order id like
  `OID-4012345` doesn't trip the heuristic).
* Ten string phrases: `invalid token`, `token invalid`,
  `token expired`, `session expired`, `not logged in`,
  `unauthorized`, `unauthorised`, `logged out`, `please login`,
  `jwt expired`.
* Rate-limit phrases (`AB429`, `too many requests`, `rate limit`,
  `throttle`) take **precedence** over auth heuristics so an
  `AB429 Session ...` blob still classifies as rate_limit.

Conservative default: anything we don't recognise is `transient`.
Auto-halting on a contract drift would do more damage than the bug
itself.

**(2) Auth callback hook on `ExecutionEngine`.**

```python
class ExecutionEngine:
    def __init__(...):
        self._auth_failure_callback = None
        self._auth_refresh_attempted = False  # per-call latch

    def set_auth_refresh_callback(self, callback):
        self._auth_failure_callback = callback

    def _maybe_invoke_auth_refresh(self, payload) -> bool:
        if classify_smartapi_error(payload) != "auth":
            return False
        if self._auth_failure_callback is None:
            return False
        if self._auth_refresh_attempted:
            return False
        self._auth_refresh_attempted = True
        try:
            return bool(self._auth_failure_callback())
        except Exception:
            # Callback must not crash trading.
            return False
```

The per-call latch (`_auth_refresh_attempted`) is critical: a
misbehaving callback that returns True without actually refreshing
the JWT must NOT loop the retry budget. The latch is reset at the
top of every `_live_order_with_retry` invocation.

`_live_order_with_retry`'s `except` block now calls
`self._maybe_invoke_auth_refresh(e)` after logging the failure.
If the callback succeeded the next retry sees the fresh
`self._api`; if it didn't, the retry loop falls back to the
existing `time.sleep(retry_delay)` between attempts.

**(3) `TradingAgent` wires the callback.**

```python
class TradingAgent:
    def __init__(...):
        self.execution = ExecutionEngine(...)
        ...
        self.execution.set_auth_refresh_callback(
            self._handle_broker_auth_failure
        )

    def _handle_broker_auth_failure(self) -> bool:
        try:
            return bool(self._maybe_refresh_broker_session(force=True))
        except Exception:
            return False

    def _maybe_refresh_broker_session(self, *, force: bool = False) -> bool:
        # When force=True, bypass the 7h age gate AND the 1h backoff.
        # Returns True iff the swap succeeded.
        ...
```

`_maybe_refresh_broker_session` now also returns True/False so the
callback chain can communicate success up to the retry loop.

### 18.3 What does NOT change

* The proactive 7h timer still runs. The reactive path is
  **complementary**, not a replacement -- so a daemon that's still
  running at hour 8 still gets refreshed even if no auth error
  ever surfaces.
* Cancel / modify / orderBook paths are NOT wired to the auth
  callback yet. They live in the same retry pattern so adding the
  hook is mechanical, but it's deliberately out of scope for this
  audit closure; the entry/exit path is where the user-visible
  damage materialises.
* Rate-limit and transient errors continue to flow through the
  existing retry loop unchanged.

### 18.4 Test coverage

**`TestORD10ErrorClassifier`** (9 tests):

* All 7 known auth codes classify as `auth`.
* Each of 9 string phrases (Invalid Token / Session Expired / JWT
  Expired / etc.) classifies as `auth`.
* `401 Unauthorized` and `403 Forbidden` classify as `auth`.
* `OID-4012345` style strings DO NOT misfire.
* Rate-limit phrases classify as `rate_limit`.
* Unknown errors (`Connection reset`, `RMS rejected`) classify as
  `transient` (the conservative default).
* Dict payloads with `error_code` field work.
* `None` payloads classify as `transient`.
* `AB429 session ...` resolves to `rate_limit`, not `auth`.

**`TestORD10AuthCallbackHook`** (8 tests):

* `set_auth_refresh_callback` installs the hook.
* Auth-class exception triggers the callback.
* Callback fires at most once per top-level call (3 retries → 1
  callback invocation).
* Transient exception does NOT trigger the callback.
* Callback raising does NOT crash the retry loop.
* Per-call latch resets between top-level calls (2 calls → 2
  invocations).
* Source-level pin: `TradingAgent.__init__` wires the callback.
* Source-level pin: `_maybe_refresh_broker_session` exposes a
  `force` kwarg that bypasses the 7h age gate.

**Suite results:**

* Unit: **1,617 / 1,617** PASSED.
* Integration: **248 / 248** PASSED.
* Combined: **1,865 / 1,865**.

### 18.5 Files touched

* `packages/core/execution.py` -- `classify_smartapi_error` module
  helper + `_AUTH_CODES` / `_AUTH_PHRASES` / `_RATE_LIMIT_PHRASES`
  constants + `_auth_failure_callback` attribute +
  `set_auth_refresh_callback` method + `_maybe_invoke_auth_refresh`
  + per-call latch reset + retry-loop wiring.
* `trading_agent.py` -- `_handle_broker_auth_failure` method +
  `set_auth_refresh_callback` wiring in `__init__` +
  `_maybe_refresh_broker_session(force=False)` kwarg + return
  type changed to `bool`.
* `tests/unit/test_audit_2026_05_28_misc.py` -- 17 new tests.

### 18.6 Honest caveat

The reactive re-auth path is engineered to be **safe by default**:
unknown errors are transient, callback exceptions are absorbed, the
per-call latch prevents callback loops. The cost is that we will
occasionally miss an auth error whose phrasing doesn't match any of
the 17 signatures the classifier knows about (the `_AUTH_CODES` and
`_AUTH_PHRASES` tuples). When that happens the daemon falls back
to the pre-fix behaviour: burn three retries on a stale JWT then
fail the order. Ops should monitor `[ORD-10]` log entries for the
first few weeks and feed any new phrasings back into
`_AUTH_PHRASES`. The classifier is a single-file change; updating
it does NOT need a freeze-bypass slot.

### 18.7 What's left in the misc-OPEN bucket

Done in this commit: ORD-10.
Done in `f7d90cc`: NUM-11, ORD-11.
Done in `d578ff1`: ORD-05, ORD-07, ORD-08, ORD-09.
Done in `da7ab69`: NUM-06, NUM-07.
Done in `03ba66d`: NUM-01.

Remaining (2 groups, 3 findings):

* **Group F** — NUM-10 (decimal arithmetic for charges; touches
  `charges.py` + `portfolio.py`).
* **Group G** — PERF-07 (DataFrame allocation cache) +
  PERF-13 (battery worker pickle); deferrable, backtester-only.

---

## 19. Misc-OPEN bucket — Group F: Decimal arithmetic for charges (NUM-10)

**Date:** 2026-05-30 (morning IST)
**Commit:** PENDING (this section is being written before the commit)
**Status:** 1 finding FIXED, NOT deployed.

### 19.1 What was broken

`charges.py` accumulated brokerage / STT / GST / etc. as IEEE-754
floats with no quantization step. `portfolio.close_position` then
derived the exit-leg commission via subtraction:

```python
total_commission = compute_round_trip(...).total
entry_commission = compute_one_leg(entry_price, ...)
exit_commission = total_commission - entry_commission
```

Two compounding issues:

1. **Component drift.** Each component (brokerage, STT, GST, ...)
   was computed at full IEEE-754 precision but then displayed at
   2-decimal precision. Over thousands of trades the cumulative
   gap between displayed-and-stored numbers and broker truth
   (which actually rounds to 1 paisa per component) was
   non-trivial.

2. **Subtractive drift.** `total - entry` is not guaranteed to be
   numerically equal to `compute_one_leg(exit, side=exit_side)`
   when both operands are floats. Over a portfolio of 1000+
   trades the accumulated jitter biased reported P&L vs broker
   contract notes by a few rupees per day -- enough to
   flip-flop a tight reward-vs-charges gate in the strategy
   sizer.

### 19.2 Fix

**Decimal pipeline in `packages/core/charges.py`.**

```python
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
getcontext().prec = max(getcontext().prec, 28)
_PAISA = Decimal("0.01")

def _q(value) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))   # avoid Decimal(0.1) jitter
    return value.quantize(_PAISA, rounding=ROUND_HALF_EVEN)
```

Every component (brokerage, STT, txn, SEBI, GST, stamp, DP) is now
computed in `Decimal` and quantized to 1 paisa **per leg, per
component** before being summed. `compute_one_leg` quantizes its
own components; `compute_round_trip` quantizes per-leg components
the same way and sums them. By construction:

* `compute_round_trip(buy, sell, qty, INTRADAY).total` ==
  `compute_one_leg(BUY, buy, qty, INTRADAY) + compute_one_leg(SELL, sell, qty, INTRADAY)`

byte-for-byte (modulo a 1e-9 IEEE-754 round-trip into / out of
`float`). This was NOT true before -- the round-trip path
folded brokerage / GST into a single quantization vs the legs'
two-stage quantization, so the two paths could disagree by a
fraction of a paisa per trade.

**Direct exit-leg compute in `portfolio.py:close_position`.**

```python
# pre-fix:
exit_commission = total_commission - entry_commission

# post-fix:
exit_commission = compute_one_leg(
    exit_price, pos.quantity, side=exit_side, product=self.product_type,
)
```

The subtractive form is gone. The new identity (above) makes the
two values mathematically equal, but routing through
`compute_one_leg` directly is robust to any future changes in
`compute_round_trip` that might break the symmetry.

**Public API stays float-typed.** `compute_one_leg` returns
`float`; `TradeCharges` fields are `float`. Callers see no
behavioural change beyond the per-component values now matching
broker contract notes to the paisa.

### 19.3 Test coverage

**`TestNUM10DecimalCharges`** (8 tests):

* `test_round_trip_total_equals_sum_of_legs_intraday` -- pin the
  new identity for INTRADAY at a deliberately float-jittery
  price (1234.567 * 137).
* `test_round_trip_total_equals_sum_of_legs_delivery` -- same for
  DELIVERY.
* `test_components_are_quantized_to_paisa` -- every reported
  component must be representable as N hundredths of a rupee
  (matches broker contract-note resolution).
* `test_legs_are_quantized_to_paisa` -- same for `compute_one_leg`.
* `test_exit_commission_no_longer_uses_subtraction` -- source-level
  pin that `close_position` doesn't regress to
  `total_commission - entry_commission`. Strips comments first so
  the explanatory NUM-10 annotation in the source doesn't trip
  the regression regex.
* `test_round_trip_pnl_equals_gross_minus_total_charges` -- end-
  to-end: 4 round-trips through `Portfolio` (with deliberately
  float-jittery prices) must satisfy `(cash_after - cash_before)
  == sum(rec.pnl)` to ~1e-6.
* `test_charges_helpers_are_still_float_typed_at_boundary` --
  defensive: the public API contract is unchanged.
* `test_anchor_in_charges_source` -- pins the audit ID + the
  `Decimal` import + `_PAISA` constant + `ROUND_HALF_EVEN` mode
  so future refactors can grep for the audit context.

**Suite results:**

* Unit: **1,625 / 1,625** PASSED.
* Integration: **248 / 248** PASSED.
* Combined: **1,873 / 1,873**.

### 19.4 Files touched

* `packages/core/charges.py` -- `Decimal` import + `_PAISA`
  constant + `_q()` quantizer + `_brokerage_dec()` helper +
  Decimal-pipeline rewrites of `compute_round_trip` and
  `compute_one_leg`. Public types unchanged (still float).
* `packages/core/portfolio.py:close_position` -- replaced
  `exit_commission = total_commission - entry_commission` with
  a direct `compute_one_leg(exit_price, ...)` call.
* `tests/unit/test_audit_2026_05_28_misc.py` -- 8 new tests.

### 19.5 Honest caveats

* **Performance.** `Decimal` arithmetic is meaningfully slower
  than float (typically 30-50x in Python). The backtester runs
  `compute_one_leg` / `compute_round_trip` once per simulated
  trade; even a 5,000-trade battery only adds milliseconds in
  aggregate. Live trading invokes them once per
  `open_position` and once per `close_position` -- well below the
  per-cycle latency budget. No measurable impact.
* **Float boundary.** The public API still returns `float` so
  callers aren't forced to refactor their downstream
  arithmetic. Numerically the two are equivalent at 1-paisa
  resolution; if downstream callers later need exact rationals
  for compounding, exposing the `Decimal` form is a one-line
  change.

### 19.6 What's left in the misc-OPEN bucket

Done in this commit: NUM-10.
Done in `1518b24`: ORD-10.
Done in `f7d90cc`: NUM-11, ORD-11.
Done in `d578ff1`: ORD-05, ORD-07, ORD-08, ORD-09.
Done in `da7ab69`: NUM-06, NUM-07.
Done in `03ba66d`: NUM-01.

Remaining (1 group, 2 findings, both deferrable):

* **Group G** — PERF-07 (DataFrame allocation cache) +
  PERF-13 (battery worker pickle); backtester-only perf wins.

10 of 13 misc-OPEN findings now closed. The two PERF deferrals
are backtester throughput knobs, not correctness fixes, so they
can be picked up opportunistically alongside the next perf
sprint.


## 20. Misc-OPEN bucket — Group G: WS hot-path + battery boot perf (PERF-07, PERF-13)

**Audit IDs:** PERF-07, PERF-13.
**Severity:** both Medium (perf, not correctness).
**Date:** 2026-05-30.
**Files touched:** `trading_agent.py` (PERF-07),
`packages/research/battery.py` (PERF-13),
`tests/unit/test_audit_2026_05_28_misc.py` (new tests),
`tests/unit/test_audit_2026_05_28_phase1.py`
(PERF-02 + OBS-20 pins updated to follow the helper extraction
in PERF-13 and the second cache in PERF-07).

### 20.1 What was broken

**PERF-07 — DataFrame allocation churn on the WS hot path.**
Every strategy on every symbol calls
`tick_aggregator.get_candle_history(symbol, timeframe, limit=200)`
through `_evaluate_strategy`. The aggregator builds a fresh
DataFrame on every call. With ~300 symbols × ~4 strategies on a
shared 5min timeframe that's ~1,200 DataFrame allocations per
trading cycle. The allocations are short-lived (one cycle) and
identical per `(symbol, timeframe)` key, so most of that work is
duplicate. The follow-on cost is gen-1/gen-2 GC pauses on the
WS thread of 10-50 ms each, which compete with tick processing
and noticeably stretch the digest line. The `_get_historical_cached`
PERF-02 cache already proves this pattern works for the REST
historical path; the WS aggregator path was just the other half
that hadn't been wired yet.

**PERF-13 — battery cache rehash redundancy.**
The OBS-20 phase-1 fix added a SHA256 to the
`_load_market_data_cache` log line for research reproducibility
(any worker's cache load can be cross-referenced against the
parent's cache write). The implementation re-hashed the
`~300 MB market_data.pkl` *inside every worker*. With
`max_tasks_per_child=1` (Bug F isolation) and ~20 variants per
battery that's ~20 × 1-2 s = 20-40 s of pure redundant work per
battery — the parent process already knew the digest at
cache-write time. The rehash also pages 300 MB through the
worker's read buffer (in addition to the unpickle), doubling
the I/O for no audit benefit.

### 20.2 Fix

**PERF-07 design.**
*Mirror PERF-02's per-cycle memo for the tick-aggregator path.*

* New attributes on `TradingAgent.__init__`:
  `_tick_history_cache: Dict[(symbol, timeframe), DataFrame]`,
  `_tick_history_cache_hits`, `_tick_history_cache_misses`.
* New helper `_get_tick_history_cached(symbol, timeframe, limit=200)`:
  * keyed by `(symbol, timeframe)` only — first writer wins,
    subsequent callers in the same cycle get the cached frame
    even if their requested `limit` differs (consistent with
    PERF-02's window semantics).
  * Empty/None results are **not** cached. Reasoning: an empty
    return is a "tick stream not warmed up yet" signal; if we
    cached it we'd starve the REST-fallback path inside
    `_evaluate_strategy` for the rest of the cycle. Letting it
    miss again on the next strategy eval gives the aggregator a
    chance to publish data that arrived part-way through the
    cycle.
* `_evaluate_strategy` now calls
  `self._get_tick_history_cached(symbol, timeframe, limit=200)`
  instead of `self.tick_aggregator.get_candle_history(...)`
  directly.
* `_clear_historical_cache` (renamed-but-actually-same; called at
  cycle start) clears both caches plus their counters together.

Expected impact at 300 symbols × 4 strategies on a shared
5min timeframe: ~75% miss rate (one miss per symbol-timeframe,
three hits per symbol-timeframe) → ~3-4× alloc reduction on
the eval micro-phase. The strategies are still doing their own
internal copies, so the wall-time win is dominated by the GC
pauses we no longer take.

**PERF-13 design.**
*Move the SHA256 from the worker boot to the parent's cache
write — one hash per battery instead of one hash per worker.*

* New helper `_sha256_file(path, chunk_bytes=1<<20)` factored
  out of the prior inline hashing so writer and reader share a
  single implementation (a drift between them would silently
  invalidate every cache and undo the whole optimisation).
* `_save_market_data_cache` now writes a sidecar
  `market_data.pkl.sha256` next to the cache:
  `<64-hex>  market_data.pkl  mtime=<float>\n`. The format is
  intentionally compatible with `sha256sum -c` style consumers
  and human readers.
* `_read_sidecar_hash(cache_path)` parses the sidecar, returns
  the 64-char digest only when:
  * the sidecar exists,
  * the digest is exactly 64 chars of lowercase hex,
  * an `mtime=<float>` token is present, **and**
  * the sidecar's mtime is within 1 second of the .pkl's
    current mtime.
  Otherwise returns `None` and the load path falls back to live
  hashing (== identical to the OBS-20 implementation).
* `_load_market_data_cache` log line now tags the source:
  `hash_source=sidecar` vs `hash_source=live`. Operators
  inspecting a battery log can tell at a glance which workers
  paid the rehash budget.
* Sidecar writes are **best-effort**: a `monkeypatch` test
  forces `Path.write_text` to raise on the .sha256 file and
  asserts the .pkl write itself still succeeds. Workers fall
  back to live hashing in that scenario — no audit regression.

Expected impact: ~1-2 s/variant × ~20 variants =
**20-40 s saved per 20-variant battery**. Process-isolation
(Bug F) is fully preserved (`max_tasks_per_child=1` unchanged).

### 20.3 Test coverage

23 new regression tests added to
`tests/unit/test_audit_2026_05_28_misc.py`:

`TestPERF07TickHistoryCache` (10):

* `test_first_call_misses_and_invokes_aggregator` — initial
  call records a miss and invokes the aggregator.
* `test_second_call_same_key_hits_cache` — the second call
  doesn't touch the aggregator at all and records a hit.
* `test_different_symbol_misses_separately` — keys are
  per-symbol.
* `test_different_timeframe_misses_separately` — keys are
  per-timeframe (so 5min and 15min both cache).
* `test_empty_dataframe_is_not_cached` — empty frames must NOT
  be cached (REST-fallback would starve otherwise).
* `test_none_result_is_not_cached` — None results must NOT be
  cached either.
* `test_clear_resets_cache_and_counters` — `_clear_historical_cache`
  drops both caches and their counters in one shot.
* `test_evaluate_strategy_uses_cached_helper` — source-level
  pin that `_evaluate_strategy` routes through
  `_get_tick_history_cached` and does NOT call the aggregator
  directly (negative + positive form, with comments stripped so
  documentation can't trip the regex).
* `test_clear_historical_cache_clears_tick_cache_too` —
  source-level pin that the clear helper resets both caches.
* `test_init_seeds_tick_cache_attributes` — source-level pin
  that `TradingAgent.__init__` initialises the three cache
  attributes (skipping this would AttributeError on first
  call).

`TestPERF13BatteryCacheSidecar` (13):

* `test_save_writes_sidecar_with_full_64char_hash` — the
  saved sidecar's digest matches `_sha256_file(pkl)` exactly.
* `test_save_sidecar_includes_mtime_field` — mtime is the
  staleness-detection mechanism; it must be present.
* `test_load_uses_sidecar_when_fresh` — patches `_sha256_file`
  with a tripwire and asserts the load path doesn't call it
  when the sidecar is fresh (this is the actual perf win).
* `test_load_falls_back_to_live_hash_when_sidecar_missing` —
  delete sidecar, prove the load path lives-hashes.
* `test_load_falls_back_when_sidecar_mtime_stale` — write a
  bad mtime in the sidecar, prove the gate rejects it and
  the loader falls back.
* `test_read_sidecar_hash_rejects_corrupt_digest` — non-hex
  in the digest field → reject.
* `test_read_sidecar_hash_rejects_wrong_length_digest` — 32
  chars instead of 64 → reject.
* `test_read_sidecar_hash_rejects_missing_mtime_field` —
  no mtime token → reject.
* `test_read_sidecar_hash_returns_full_digest_on_fresh_pair` —
  positive case round-trip.
* `test_load_log_line_marks_hash_source` — the load log
  contains `hash_source=sidecar` on the fast path.
* `test_load_log_line_marks_live_source_when_sidecar_missing` —
  the load log contains `hash_source=live` on the fallback
  path.
* `test_save_failure_to_write_sidecar_does_not_fail_save` —
  monkeypatch sidecar write to raise; .pkl write still
  succeeds; subsequent load works via live hashing.
* `test_source_pins_perf13` — anchor the audit ID + helper
  symbols (`_sha256_file`, `_read_sidecar_hash`) in the
  source so a future refactor can't silently drop them.

Two phase-1 tests updated to follow the helper extraction:

* `test_perf02_clear_resets_cache_and_tallies` — now seeds
  the new tick cache attributes too and asserts they're
  cleared (fails closed if `_clear_historical_cache` ever
  forgets the second cache).
* `test_obs20_battery_cache_load_logs_sha256` — pin relaxed
  to accept either `hashlib`, `_sha256_file`, or
  `_read_sidecar_hash` as the path through which the load
  reaches a SHA256 implementation. The `sha256[:16]` log
  field check is unchanged, so the OBS-20 audit contract is
  enforced exactly as before.

### 20.4 Suite results

* `tests/unit/test_audit_2026_05_28_misc.py::TestPERF07TickHistoryCache` — 10/10 PASS.
* `tests/unit/test_audit_2026_05_28_misc.py::TestPERF13BatteryCacheSidecar` — 13/13 PASS.
* Full unit suite — **1,648/1,648 PASS** (39.91s).
* Full integration suite — **248/248 PASS** (29.60s).

### 20.5 Honest caveats

* PERF-07's caching layer is keyed by `(symbol, timeframe)`
  and doesn't honour `limit`. Every existing in-cycle caller
  passes `limit=200`, so this is fine today; if a future
  caller needs a different limit the comment in
  `_get_tick_history_cached` documents the bypass route
  (call `self.tick_aggregator.get_candle_history` directly).
  This trade-off is identical to the PERF-02 cache.
* PERF-13's mtime gate is 1-second granular. On a fast
  filesystem with sub-second .pkl rewrites the sidecar could
  theoretically be accepted on the very next read after a
  rewrite. We accept this because (a) the actual payload
  hashes are still consistent (`_sha256_file` over the new
  bytes would yield a different digest, and the next
  re-saving would regenerate the sidecar), and (b) the
  battery harness only writes the cache once per
  battery-creation, never re-writing during a battery.
* The PERF-07 win is observed via gen-1/gen-2 GC pause
  reduction more than via the per-call wall-time. The unit
  tests verify the contract (hits, misses, no double-call
  to the aggregator); the actual perf delta will surface on
  the next live-mode `[CYCLE-DIGEST]`.
* The PERF-13 win is observed in the per-variant boot phase
  before `bt.run()` begins. The unit tests verify the
  sidecar logic; the actual perf delta will surface on the
  next 20-variant battery as a 20-40 s drop in the
  cumulative `[WORKER] starting variant ... market_data
  loaded ...` interval.

### 20.6 What's left in the misc-OPEN bucket

Done in this commit: PERF-07, PERF-13.
Done in `3d2e962`: NUM-10 (Group F).
Done in `1518b24`: ORD-10 (Group E).
Done in `f7d90cc`: NUM-11, ORD-11 (Group D).
Done in `d578ff1`: ORD-05, ORD-07, ORD-08, ORD-09 (Group C).
Done in `da7ab69`: NUM-06, NUM-07 (Group B).
Done in `03ba66d`: NUM-01 (Group A).

**11 of the 13 misc-OPEN findings closed by Group G.**
Honest re-count after this commit:

* STATE-07 (CSV durability) and CONC-10 (heartbeat thread) are
  still OPEN. Group G's commit-message claim of "86/86" was
  off-by-two; corrected in §21 below.

These two are picked up immediately as Group H so the audit
actually reaches 86/86 before the deploy decision.


## 21. Misc-OPEN bucket — Group H: durability + watchdog freshness (STATE-07, CONC-10)

**Audit IDs:** STATE-07, CONC-10.
**Severity:** both Medium (one operational-hygiene, one
watchdog-correctness).
**Date:** 2026-05-30 (afternoon).
**Files touched:** `packages/core/portfolio.py` (STATE-07
trade-CSV path), `packages/core/signal_audit.py` (STATE-07
signal-audit-CSV paths), `trading_agent.py` (CONC-10 thread +
helpers + lifecycle), `tests/unit/test_audit_2026_05_28_misc.py`
(20 new regression tests),
`tests/integration/test_eod_audit_fixes.py` (1 source-pin
slice-budget bump).

### 21.1 Why this group exists at all

When Group G landed, the commit message claimed "all 13
misc-OPEN findings now closed" and "86/86". On a re-read of
the audit table (prompted by the user), three rows in the
exec summary still read OPEN (OBS-01, PERF-01, PERF-02), and
two rows in the per-angle tables (STATE-07, CONC-10) read
OPEN. The exec-summary rows were stale (the per-angle tables
already showed them FIXED in phases 1 + 4); STATE-07 and
CONC-10 were genuinely never touched. Group H closes those
two findings and updates the exec-summary stale rows so
counts agree.

### 21.2 What was broken

**STATE-07 — trade-CSV + signal-audit-CSV durability.**
The DB `trades` table (written atomically by
`Database.close_position_atomic` — STATE-04) is the source of
truth, but `trades.csv` and the per-day `signal_audit_*.csv`
are consumed by tooling that doesn't have the DB:
`tools/ledger_diff.py`, the friday review prep, the EOD
report generators, the dashboards. Pre-fix:

* `Portfolio._log_trade` opened the CSV in append mode, wrote
  one row, and closed — **no lock**, **no fsync**. Two
  concurrent close paths (e.g. SL fill + manual flatten on
  different symbols) could interleave bytes within a single
  CSV line. A kernel panic / SIGKILL between the close and
  the next write would lose the row even though the daemon
  reported success and the DB had it.
* `signal_audit.log` had a `threading.Lock` (STATE-11 added
  it earlier) but **no fsync**. Same crash-window risk: a
  cycle's worth of rejected signals could disappear despite
  the daemon claiming success. The EOD diagnostic would then
  read "0 signals today".
* `signal_audit._drain_retry_queue` (STATE-11 recovery path)
  flushed without fsync, so rows recovered from a transient
  outage were durable only if the OS happened to flush
  before the next crash — exactly the race STATE-07 was
  meant to close.

**CONC-10 — heartbeat freshness.**
`health.json` was written only at the end of each main-loop
cycle. A 3-4 min `get_multiple_ltp(300)` (now PERF-01-fixed
but the watchdog model needs to handle any future slow path)
or a hung broker call left the file stale, so the cloud
watchdog SIGTERM'd a perfectly healthy daemon. The cycle-end
write also blocked on loguru file-IO and risk-summary
construction, which compounded the staleness.

### 21.3 Fix

**STATE-07.**

* `Portfolio.__init__` now creates a per-instance
  `_trade_log_lock = threading.Lock()`. Per-instance — not
  module-global — because the test harness builds many
  Portfolios and they should be independent.
* `Portfolio._log_trade` now holds the lock across the whole
  open / write / `flush + os.fsync` / close cycle. fsync
  failures (Windows shares, CIFS) are caught and logged at
  WARNING; the row stays in the page cache (no worse than
  pre-fix).
* `signal_audit.log` now does `f.flush()` + `os.fsync(...)`
  inside the existing `self._lock` block. Same fail-soft
  policy on `OSError`.
* `signal_audit._drain_retry_queue` does the same fsync
  after the batch flush so recovered rows are durable
  before we declare success.

**CONC-10.**

* New `_heartbeat_snapshot: Dict[str, Any]` on `TradingAgent`.
  The main loop publishes it via `_publish_heartbeat_snapshot`
  (single-statement dict assignment — atomic under the GIL).
* New `_write_health_json_from_snapshot()` reads the
  snapshot, stamps a fresh `ts` / `ts_unix` / `running`,
  and atomically writes `health.json` via the existing
  `.tmp` + rename pattern. Returns False (no-op) on an empty
  snapshot so the boot phase doesn't emit a half-blank
  health file.
* New `_run_heartbeat_thread()` is the daemon-thread loop:
  write-then-wait at `_health_pulse_interval_seconds`
  cadence (default 30s, set to 0 to disable), exits on
  `_heartbeat_stop_event`.
* New `_start_heartbeat_thread()` is **idempotent**: a second
  call while the first thread is alive is a no-op (matters
  for JWT-refresh restart paths that might re-enter
  `run()`).
* New `_stop_heartbeat_thread(timeout=2.0)` joins the thread
  cleanly. Daemon=True is the fallback if the join times
  out.
* `run()` now calls `_start_heartbeat_thread()` BEFORE the
  main while loop so cycle 0 is already covered.
* `_shutdown()` now calls `_stop_heartbeat_thread()` FIRST
  (before `ws_client.stop()`, before broker / DB teardown)
  so the watchdog sees a final `running=false` pulse before
  the rest of shutdown.

The main-loop `_log_heartbeat()` keeps its loguru summary
line — that's still useful for human-readable logs — but it
no longer writes `health.json` directly. The thread is the
single writer, which removes any race on the `.tmp` file.

### 21.4 Test coverage

20 new tests in `tests/unit/test_audit_2026_05_28_misc.py`:

`TestSTATE07TradeCsvDurability` (5):

* `test_log_trade_acquires_lock` — replaces the lock with a
  tripwire context manager and counts entries / exits.
* `test_log_trade_fsyncs_after_write` — monkeypatches
  `os.fsync` with a tripwire and asserts at least one call.
* `test_log_trade_survives_fsync_oserror` — `os.fsync`
  raises; `_log_trade` must not raise; the row must still
  appear in the file (page-cache).
* `test_log_trade_concurrent_writes_do_not_tear` — 8 threads
  × 50 rows = 400 rows; the resulting CSV must have exactly
  401 lines (header + 400 data) and every row must have the
  right column count.
* `test_portfolio_init_creates_trade_log_lock` — source pin
  on `__init__` so a refactor can't drop the lock and break
  `_log_trade` at runtime.

`TestSTATE07SignalAuditDurability` (4):

* `test_log_fsyncs_after_write` — same tripwire pattern.
* `test_log_survives_fsync_oserror` — same fail-soft check.
* `test_drain_retry_queue_also_fsyncs` — pre-queue a row,
  drain, assert fsync was called.
* `test_source_pin_state07_anchors` — both `portfolio.py`
  and `signal_audit.py` source files must contain `STATE-07`,
  `f.flush()`, and `os.fsync` in the relevant function
  bodies.

`TestCONC10HeartbeatThread` (11):

* `test_publish_snapshot_atomically_swaps_dict` — fields
  flow correctly from publisher into the snapshot.
* `test_write_from_snapshot_no_op_when_empty` — empty
  snapshot returns False and writes nothing.
* `test_write_from_snapshot_stamps_current_ts` — even with a
  stale snapshot the on-disk `ts_unix` is the wall-clock at
  pulse time. **This is the key correctness property.**
* `test_write_from_snapshot_reflects_current_running` —
  `running=False` is mirrored immediately when the daemon is
  shutting down.
* `test_write_from_snapshot_atomic_via_tmp_rename` — spies
  on `Path.write_text` + `Path.replace` to confirm the
  atomic-rename pattern is preserved.
* `test_run_thread_exits_on_stop_event` — actually spawns a
  thread, runs it for ~3 ticks, sets the event, joins, and
  asserts the thread exited within 2s and `health.json`
  exists.
* `test_start_thread_idempotent` — a second
  `_start_heartbeat_thread()` call must NOT spawn a duplicate
  thread.
* `test_start_thread_disabled_when_interval_zero` — config
  knob `health_pulse_interval_seconds: 0` skips the spawn.
* `test_run_method_starts_heartbeat_thread` — source pin:
  `run()` calls `_start_heartbeat_thread` BEFORE the
  `while self._running` loop entry.
* `test_shutdown_stops_heartbeat_thread` — source pin:
  `_shutdown` calls `_stop_heartbeat_thread` BEFORE
  `ws_client.stop`.
* `test_init_seeds_heartbeat_attributes` — source pin: all
  four heartbeat-thread attributes are seeded in
  `__init__`.

One integration source-pin test bumped (not a test failure
on the fix itself — just a slice-budget that was too tight
for the new shutdown body):

* `tests/integration/test_eod_audit_fixes.py::TestEODDeduplication::test_shutdown_skips_daily_report_when_eod_already_sent`
  — was using `src[i:i+4000]` which no longer reached
  `send_daily_report` after CONC-10's additions. Replaced
  with a `find("\n    def ", i+1)` slice that follows method
  growth.

### 21.5 Suite results

* `tests/unit/test_audit_2026_05_28_misc.py::TestSTATE07TradeCsvDurability` — 5/5 PASS.
* `tests/unit/test_audit_2026_05_28_misc.py::TestSTATE07SignalAuditDurability` — 4/4 PASS.
* `tests/unit/test_audit_2026_05_28_misc.py::TestCONC10HeartbeatThread` — 11/11 PASS.
* Full unit suite — **1,668/1,668 PASS** (94.03s).
* Full integration suite — **248/248 PASS** (56.84s).

### 21.6 Honest caveats

* The CONC-10 thread reads several `TradingAgent` fields
  via the snapshot, which is published from
  `_log_heartbeat`. Until `_log_heartbeat` runs at least
  once (a few cycles into the boot), the snapshot is empty
  and the thread skips its writes. That's by design — the
  alternative is publishing a snapshot from `__init__`,
  which would expose half-built state. Watchdogs that want
  a guaranteed pulse at boot should rely on the daemon's
  systemd / docker-compose startup notification instead of
  on `health.json` being touched.
* fsync on Windows shares / CIFS is unreliable; we
  fail-soft on `OSError`. In practice the trader VM uses
  ext4 (Linux), so this only matters for local-dev
  smoke tests.
* The STATE-07 lock is per-Portfolio. If a future tool
  spawns a *second* `Portfolio` pointing at the same
  `trades.csv` (don't), the two locks won't coordinate.
  This is an explicitly documented constraint — the
  daemon is single-Portfolio by design.

### 21.7 Audit closure

With Group H landed, every finding from the 2026-05-28 audit
is now FIXED or explicitly DEFERRED with a documented
architectural-session follow-up:

* **FIXED**: 84 findings (phases 1-5 + misc Groups A-H).
* **DEFERRED** (architectural session):
  * **CONC-03** — WS hot-path enqueue+return (architectural
    restructure).
  * **STATE-05** — orders boot recovery from `orderBook`.

**Total: 86 / 86 findings addressed.** This time the count
agrees with the per-angle and exec-summary tables. Next move
is the deploy decision (still gated on the Friday morning
V15 verdict; see `friday_review_2026-05-29.md`).


## 22. Diagnostic-sprint Friday read-out — V15 transfer test = FAIL

**Date:** 2026-05-29 14:08 IST (V15 result landed at 10:26 IST
today; appended after the slot-#3 status check confirmed it).
**Context:** This is the §7 retrain decision-matrix gate from
`docs/friday_review_2026-05-29.md`. The full decision tree +
backlog reorder lives in §10 of that review; this section
is the operational-log mirror so anyone reading the findings
log without the friday review still gets the correct
conclusion.

### 22.1 What landed

| Slot | Universe | V15 trades | V15 WR% | V15 PnL | V15 PF | V15 MaxDD% | V15 Ret% |
|------|----------|-----------:|--------:|---------:|-------:|-----------:|---------:|
| #1   | 50 stocks (Nifty50) | 56 | 50.0 | **+₹10** | **1.02** | 1.92 | +0.10% |
| #3   | 232 stocks (v4 universe) | 444 | 47.3 | **-₹326** | **0.94** | 8.80 | **-3.23%** |

PnL flipped sign across universes (+₹10 → -₹326). Trade
count scaled 56 → 444 (~8× on a 5× universe size; the MR
strategy fires more aggressively on the bigger universe and
the additional trades land net-negative).

### 22.2 Verdict

Per `friday_review_2026-05-29.md §7` decision matrix
`PF < 0.95 on 232 stocks` row:

> Slot-1 V15 was small-universe noise. **Defer retrain
> indefinitely.** Look for alpha elsewhere (regime
> classifier, entry-lag, position sizing).

* **Capital stays paused** under freeze-v2.1 (zero trades
  since 2026-05-27, see `docs/eod_report_2026-05-27.md`).
* **Bypass slot-3 is NOT consumed.**
* **No V* variant promoted to live.** Best slot-#3 variant
  (V15) loses -3.23% over 60d; second-best (V5_threshold_7pct)
  loses -4.26%; baseline V1 loses -6.68%. The 4-strategy
  ensemble is intrinsically negative on this universe.

### 22.3 Backlog reorder (effective immediately)

1. **DEFERRED INDEFINITELY** -- XGBoost retrain pre-flight
   (steps A-E in §5.10) and training (steps 2-6 of §5.9). The
   runbook stays documented for re-activation if H1/H3
   forensics later argue retrain is the next move; it is no
   longer the highest-leverage open item.
2. **PROMOTED to next-sprint top priority** -- Hypothesis H3
   (entry-lag forensic). Live trades may be systematically
   late vs the backtester's ideal-fill model; if so, the
   backtester PnL is an upper bound on what live can deliver.
   Concrete deliverable: histogram of `(broker_fill_ts -
   strategy_emit_ts)` from the last 30d of trader logs vs the
   backtester ideal-fill model. Estimated 3-4 days.
3. **PROMOTED to next-sprint priority 2** -- Hypothesis H1
   (regime classifier mis-firing). Cross-universe PF
   degradation (V10 PF 0.88 → 0.79; V15 PF 1.02 → 0.94)
   suggests the regime classifier may be tagging windows
   differently across universe sizes. Diagnostic: per-regime
   PnL slice for V1+V10+V15 from slot-3 data. ~1-2 days.
4. **STILL HIGH** -- Bug K fix (move slice block before
   `_save_market_data_cache`; add unit test); re-queue an
   actual holdout-30d batch the following weekend. Without
   walk-forward evidence, every "best variant" ranking is
   window-conditional. ~30 min code + 1h test.

### 22.4 Not-yet-final cells

V18 (slot #3) is still in flight (64.1% as of 14:08 IST,
ETA ~16:00 IST tonight) and V19 just started
(~17:00 IST ETA). Both are informational only; neither can
change the §22.2 verdict because:

* V18 either confirms the §6 V18-anomaly is universe-specific
  (V18 = V2 = -₹981) or reveals it was a one-off (V18 = V4 =
  -₹489). Neither outcome makes V18 profitable -- the best it
  can be is "matches V4", which §5 already calls "least-bad
  loser, do not promote".
* V19 should equal V2 by symmetry (long-only-filters-off ≡
  all-filters-off when shorts are already disabled live).
  If V19 ≠ V2 we'd have a separate config-merge bug to
  investigate, but that wouldn't unblock retrain or
  live-promotion either.

If V18 / V19 deliver any unexpected positive variant, this
§22 + the friday review §10 will be re-opened. Otherwise
this is the closing call for the diagnostic sprint's H1+H2+H3
checkpoint.

### 22.5 Honest framing

The diagnostic sprint produced a **conclusive negative result on
H2** (XGBoost retrain alone is not sufficient to unblock live
profitability) and a **strong steer toward H1+H3** as the
next-sprint focus. That is a successful sprint outcome even
though the headline answer is "nothing to ship today" -- a
loud "do not deploy" signal that prevents another round of
capital decay is a valuable result.


