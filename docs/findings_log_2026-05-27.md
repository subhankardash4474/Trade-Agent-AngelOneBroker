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

Three operational findings and one verdict closure today:

1. **§1 Bug J — `bootstrap_backtester.sh` chowns host data to UID 1001,
   breaking the host-side scheduler** that runs as `opc`. Discovered during
   the 2026-05-27 fresh deploy when `battery-scheduler.service` could not
   write `battery_queue_state.json`. Workaround applied; root cause needs
   a script fix.
2. **§2 Slot-1 regression — `risk.allow_shorts: false` reverted to `true`
   on the trader VM ~16 hours after the 2026-05-26 deploy**, because the
   value was edited *on the VM only* via `sed` and never committed to git.
   A container rebuild silently pulled the upstream default (`true`) back
   in. Detected this morning at 10:42 IST. Re-fixed via the manual VM
   `sed`, then committed durably to git as `8e1e926`.
3. **§3 Bug I closure verdict — the 5 uncommitted trader VM hot-fixes are
   confirmed ops/observability scope only**, not strategy-affecting.
   Manual reconciliation completed by operator on 2026-05-26. Trader HEAD
   is now `e1df9e8`, working tree clean (except backup files and
   operator-local artifacts). The "freeze ledger tracks main, not VM" gap
   identified in `findings_log_2026-05-25.md §17.6` is now actively being
   monitored.
4. **§4 Diagnostic sprint kicked off** per the 2026-05-27 advisor memo.
   10 hypotheses across the regime-classification, candidate-selection,
   and contract-hygiene axes. Mapped to the 5 days Wed–Sun on the
   "Option A" schedule (Friday review-only, no new sprint work that day).
   First two hypothesis instrument-only patches deployed to trader VM at
   11:05 IST (`[REGIME-INPUT]` + `[REGIME-INTRADAY-INPUT]` log lines, now
   confirmed flowing through to `logs/trading_agent_2026-05-27.log`).

Net state: Freeze contract intact, slot-1 now LIVE *and* durable in git;
slot-2 / slot-3 unchanged. No strategy-affecting code changes today. All
of today's edits are either observability or in-place config-value
changes to an already-slotted key.

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

## 5. Cross-references

* `findings_log_2026-05-25.md` §15 (Bug G self-audit), §16 (Bug H —
  xgboost missing from battery), §17 (Bug I — trader VM divergence).
* `changes_done_2026-05-27.md` — formal audit fix sweep (38 items).
* `findings_2026-05-27.md` — F-01..F-108 audit findings catalogue
  (independent from this operational log).
* `FREEZE_v2.1.md` — slot ledger (slot 1 LIVE + DURABLE, slots 2/3
  unchanged).
* `diagnosis_sprint_2026-05-27.md` — 5-day investigative plan
  (next-door file).

---

## 6. Files touched in this finding (writes only)

* `docs/findings_log_2026-05-27.md` — this file
* `docs/diagnosis_sprint_2026-05-27.md` — separate file (next commit)
* `config.yaml` — durability flip committed as `8e1e926`
* `packages/core/regime.py` — observability commit `e1df9e8`
* No strategy code, no risk code, no ensemble code, no model files,
  no broker code touched.
