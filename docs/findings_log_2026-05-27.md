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

### 1.5 Permanent fix (queued, not done today)

Edit `tools/cloud/bootstrap_backtester.sh` to replace the single
`chown -R 1001:1001 logs data models` with the three-way split above.
Add an explicit comment block linking back to this finding and to the
permission table in §1.3.

Cost: ~5 lines of bash, ~10 min testing on a fresh OCI compute provision.
Scope: backtester-VM bootstrap only; the trader-VM bootstrap is unaffected.

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

## 7. Cross-references

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

## 8. Files touched in this finding (writes only)

* `docs/findings_log_2026-05-27.md` — this file (§5 + §6 added)
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
* No strategy code changes (xgboost disable is a config edit).
* No risk-manager / position-sizer / ensemble code changes.
* No model files modified or replaced (the broken .pkl is left in
  place as forensic evidence; it cannot be loaded because the active
  strategies list excludes its consumer).
