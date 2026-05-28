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
