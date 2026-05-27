# Bug I — Trader VM divergence diff archival

**Created:** 2026-05-27 (post operator-rebuild)
**Status:** Reconciled. Trader VM at `e1df9e8` (origin/main) as of
2026-05-27 11:26:38 IST.
**Purpose:** Single-page archival reference for the diff that existed
between origin/main and the trader VM working tree at the time of
discovery on 2026-05-26 14:30 IST.

This document is a pointer / summary. The exhaustive evidence lives in:

* `docs/findings_log_2026-05-25.md` §17 — full discovery + diff
  breakdown, file-by-file justification, 5 modified-tracked + 6
  untracked.
* `docs/findings_log_2026-05-27.md` §3 — closure verdict (the diff
  is operationally real but **strategy-neutral**).
* Commit `ee00bd0 Bug I -- log trader VM divergence (2 weeks of
  uncommitted hot-fixes)` — the immutable in-git record of what was
  on the trader VM at 14:30 IST 2026-05-26.

---

## The diff (as of 2026-05-26 14:30 IST, pre-rebuild)

Trader VM HEAD: `868d5ad` (2026-05-19).
Origin/main HEAD at time of discovery: `73c26bf` (2026-05-26 14:24 IST).
On-disk delta: 7 commits behind + 5 locally-modified tracked files + 6
untracked files.

### Modified-tracked files (5)

| File | Local delta vs `868d5ad` | Category | Strategy-affecting? |
|---|---|---|---|
| `docker-compose.yml` | +16 lines bind-mounts `./tools:/app/tools:ro` and `./packages:/app/packages:ro` (enables host-side hot-patch during freeze) | Infrastructure | **No** — config only, not in `packages/strategies/` or `packages/core/risk_manager.py` |
| `packages/core/stock_scanner.py` | rewritten (523 lines vs 418) — NSE archives CSV path hot-fix | Data-ingest | **No** — `stock_scanner` is data-handler scope, not strategy-vote / risk-rule scope |
| `packages/monitoring/alerts.py` | rewritten (665 lines vs 644) — TLS verify default flip + markdown → HTML render for emails | Alerting | **No** — `packages/monitoring/` is observability scope |
| `tools/send_heartbeat.py` | rewritten (463 lines vs 362) — container-exec mode refactor so cron can fire `docker exec trader python ...` | Operator tool | **No** — `tools/` is operator-tool scope |
| `tools/cloud/install_heartbeat_cron.sh` | +92 lines — `--container` mode + log-file pre-create dance | Operator tool / ops | **No** — `tools/cloud/` is provisioning scope |

### Untracked files (6, of which 3 production-relevant)

| File | Production-relevant? | Why |
|---|---|---|
| `tools/cloud/install_watchdog_cron.sh` | Yes | Watchdog installer (responded to 2026-05-22 11h silent-hang) |
| `tools/watchdog_check.py` | Yes | The watchdog daemon — 5-min cron-fired probe of `logs/health.json` mtime |
| `docker-compose.override.yml` | Yes | OCI VM memory limits (750M cap, 400M reservation) |
| `EMERGENCY_STOP` | No (operator-local sentinel) | Empty file the daemon checks for; controls new-position gate |
| `_deploy.sh` | No (operator-local script) | Operator's manual `git reset --hard origin/main` + image rebuild helper |
| `logs/.eod_sent_2026-05-15.flag` | No (dedup residue) | EOD-summary dedup marker; harmless |

---

## Closure path (executed 2026-05-26)

The operator (not the investigative agent) performed the manual rebuild
documented as `findings_log_2026-05-25.md §17.5`:

1. Created a feature branch `trader-hotfixes-2026-05-26` on the trader
   VM.
2. Committed the 5 modified files + 3 production-relevant untracked
   files onto that branch.
3. Pushed to origin.
4. Pulled origin/main into the trader VM (which now had the
   trader-hotfixes commit + the upstream main commits).
5. Resolved merges, rebuilt container, restarted.
6. Verified container healthy + daemon reading current config.

Result: trader VM HEAD advanced to `73c26bf` (post-merge). Subsequent
pulls today (2026-05-27 11:02 + 11:26 IST) advanced it further to
`e1df9e8` then `f32009c` — slot-1 durability + regime observability +
slot-2 (xgboost disable).

---

## Strategy-neutrality verdict

Every modified-tracked file falls into one of these scopes:
**infrastructure, data-ingest, alerting, operator-tool, ops**.
None are in:

* `packages/strategies/` — strategy logic (frozen)
* `packages/core/risk_manager.py` — risk rules (frozen)
* `packages/core/position_sizer.py` — sizing (frozen)
* `packages/core/regime.py` — regime classifier (today's slot-1
  observability work touched this, but Bug I did not)
* `packages/strategies/ensemble.py` — ensemble logic (frozen)
* `config.yaml` strategy + risk blocks (frozen)
* `models/xgboost_model.pkl` (frozen and -- as of today's slot-2
  finding -- broken; addressed separately)

**Conclusion:** the 2-week divergence was operationally real but
**strategy-neutral**. The live trade record from 2026-05-13 →
2026-05-25 (28 trades, -₹1,505) is therefore valid evidence about
freeze-v2.1's strategy behaviour. This answers the 2026-05-27
advisor memo's "concrete question for the Friday review".

---

## Lessons captured (queued, not yet fixed)

From `findings_log_2026-05-25.md §17.6`:

* Run a daily reconciliation check on each VM (`git status --porcelain`
  alert if non-empty).
* Add VM-config snapshot to forensic logs (every container restart
  should snapshot `config.yaml` + the staged blob hash, so a future
  regression has a paper trail). See also today's slot-1 regression
  (`findings_log_2026-05-27.md §2`) which is an independent occurrence
  of the same class of failure.
* Either run the daily reconciliation check OR pin VMs to a tag with
  read-only working tree (mounts only, no `git pull` capacity from
  the host).

Queued for the post-Friday-review changelog. No code change today.

---

## Files referenced

* `findings_log_2026-05-25.md` §17 (full evidence)
* `findings_log_2026-05-27.md` §3 (closure verdict)
* Commit `ee00bd0` (in-git record)
* Commit `73c26bf` (post-rebuild HEAD)
* Commit `f32009c` (today's slot-2 deploy, trader VM now at this HEAD)
