# Trading Agent — Changes Done · 2026-05-18 (freeze-v2.1)

**Scope:** Lock the trader's *trading behaviour* for the next 3 weeks so a
high-volume battery on a separate VM can build statistical confidence on
the same code/config that's running in paper. **Phase 0** of the
freeze + battery validation plan.

**Why now:** 7-day Phase A paper window closed at -₹1,132 across 17 trades.
The system's own EOD diagnostic flagged `supertrend_follow` as responsible
for ~79 % of the loss; remaining strategies have too few trades for a
verdict. We can't make any of those calls until each strategy has ≥30
decisive trades — which is what this freeze is designed to produce.
**Freeze first, measure second.**

---

## What changed in this freeze commit

### 1. `supertrend_follow` set to base weight 0 (KILL)

System verdict (`logs/diagnostics/eod_2026-05-18.md`):
N=17, WR 35.3 %, PF 0.35, Kelly -0.642, PnL -₹896 over 7 days.

We're not deleting the strategy — its signals still appear in
`signal_audit_*.csv` for diagnostic continuity. Weight=0 means they
contribute nothing to the ensemble vote. Re-enable only after the
battery on Nifty 500 shows PF lower-CI > 1.0 in some regime.

`config.yaml` → `strategies.weights.supertrend_follow: 0.0`

### 2. `long_entry_regimes` widened to include neutral regimes

Pre-freeze: `[bull_low_vol, bull_high_vol]`. Live evidence: the regime
detector classified every day from 2026-05-12 → 2026-05-18 as
`bear_high_vol`, so **8,900 / 8,900 BUY signals were rejected by the
long-regime guard** during Phase A. Longs never fired across the entire
paper window — the system became a short-only bear specialist by
accident.

For Phase A validation we widen the allow-list to **also** include
`sideways` and `bear_low_vol` (the *neutral* regimes). We intentionally
KEEP `bear_high_vol` on the deny list — that's the regime where shorts
have the documented edge and longs are most dangerous (sharp
counter-trend bounces). The intraday risk-off overlay
(`intraday_regime_block_longs: true`) still applies on top.

`config.yaml` → `execution.long_entry_regimes:
[bull_low_vol, bull_high_vol, sideways, bear_low_vol]`

### 3. Scanner watchlist widened 200 → 300

Phase A produced ~6-8 fresh setups/day with `top_n: 200`. The freeze
needs 100 closed trades; bumping to 300 should yield 9-12/day without
overstressing the E2.1.Micro trader VM (1 OCPU / 1 GB). The Nifty 500
sweep happens on the **dedicated backtester VM**, not the live trader.

`config.yaml` → `scanner.top_n: 300`

### 4. Integration tests updated for the freeze-v2.1 regime policy

Two integration guards in
`tests/integration/test_post_backtest_2026_05_05_fixes.py` encoded the
older "no bear_* in longs, full disjoint sets" contract. Updated to
mirror the new contract:

- **`test_long_entry_regimes_never_allow_bear_high_vol`** — narrowed
  from "no bear" to "no `bear_high_vol`". Neutral bear regime is now
  acceptable on the long side.
- **`test_no_overlap_in_strongly_directional_regimes`** — overlap is
  allowed in *neutral* regimes (`sideways`, `bear_low_vol`); still
  forbidden in clearly directional regimes (`bull_*`, `bear_high_vol`).

These are still genuine guards — they catch the most dangerous
misconfigurations — just less restrictive in the legitimate grey zone.

### 5. New: `docs/FREEZE_v2.1.md`

Documents what is frozen, what is NOT, exit criteria, and operator
commitments during the freeze. The contract for any future change
during the freeze window: any commit touching a frozen file must carry
a `freeze-bypass:` line in the message body, otherwise it waits until
the freeze lifts.

---

## Backtester VM scaffolding (also in this commit)

The runbook `docs/backtester_vm_runbook.md` listed five deferred
artefacts. Four of them ship now:

### 6. `tools/cloud/bootstrap_backtester.sh`

Idempotent one-shot installer. Run once from the laptop against the
newly-provisioned VM. Detects distro (Oracle Linux / Ubuntu / Amazon
Linux), installs Docker + git + Python, clones the repo at
`freeze-v2.1`, allocates a 2 GB swapfile (defensive), and builds the
`trading-agent:latest` image. Ends with a smoke test:
`docker run … python tools/run_battery.py --help`.

Key difference from the trader bootstrap: **no `trader` system user,
no .env scaffolding**. Backtester role only.

### 7. `tools/cloud/launch_battery.sh`

SSHes to the backtester VM and starts a detached
`docker run --name <run_id>` invocation of the battery. Mounts
`/opt/trading-agent/{logs,data}` so result tarballs survive a
container OOM. Pre-flight refuses to start if any
`(ANGELONE|SMARTAPI|KITE)_*` line is found in `.env` on the VM
(defence-in-depth; the in-process assertion below is the real gate).

Default args: `--days 90 --interval 5m --workers 2
--universe-file tests/fixtures/battery_v2_universe.json`. All extra
args forward verbatim to `run_battery.py`.

### 8. `tools/cloud/pull_battery_results.ps1`

Companion to `pull_logs.ps1`. SCPs `logs/backtests/<run_id>/` from the
backtester VM to the laptop. Without `-RunId` it auto-resolves to the
latest run on the VM (run_ids are ISO timestamps, so `ls | sort | tail
-1` is correct).

### 9. Backtester-mode safety assertion in `packages/research/battery.py`

`_assert_backtester_isolation()` is the **first line** inside
`main()` of the battery harness. When `BACKTESTER_MODE=1` is set in
the environment (wired by `launch_battery.sh`'s `-e
BACKTESTER_MODE=1`), the harness SystemExits with code 9 if any env
var starting with `ANGELONE_`, `SMARTAPI_`, `BROKER_`, or `KITE_`
is present. Aborts **before** any data source opens.

Why: the backtester VM has no broker IP whitelist by design. A
populated `.env` getting rsynced there must crash loudly, not
silently get a chance to reach a broker socket.

**24 new unit tests** in
`tests/unit/test_battery_backtester_isolation.py`, including a
structural regression guard via `inspect.getsource()` that the
assertion call is literally the first body line of `main()` — a
refactor that moves I/O or argparse before the check will fail in
CI.

---

## What is NOT in this commit (deliberately deferred)

| Item | Why deferred |
|------|--------------|
| ML model retrain | Frozen artefact for the duration |
| New strategies / indicators | Whole point of the freeze |
| Risk-gate threshold tweaks | Same |
| `tools/cloud/ampere_capacity_watcher.sh` | The VM is provisioned; not needed now |
| Battery-v2 18-variant slate | Research artefact; runs against the frozen config in Phase 1 |
| Live cloud deploy of this freeze | Operator step (next morning before 09:15 IST) |

---

## Files touched

```
config.yaml
docs/FREEZE_v2.1.md                                    (NEW)
packages/research/battery.py                           (+ ~40 lines safety guard)
tests/integration/test_post_backtest_2026_05_05_fixes.py (updated 2 guards)
tests/unit/test_battery_backtester_isolation.py        (NEW, 24 tests)
tools/cloud/bootstrap_backtester.sh                    (NEW)
tools/cloud/launch_battery.sh                          (NEW)
tools/cloud/pull_battery_results.ps1                   (NEW)
changes_done_2026-05-18.md                             (this file, NEW)
```

## Test results

`pytest tests/ -q`:
**1197 passed in 105 s** (+24 vs. previous freeze; 0 lints; 0
regressions).

## Tag

`git tag freeze-v2.1` after this commit. Any future commit on a frozen
file MUST include `freeze-bypass: <one-line justification>` in the
commit message body.

---

## Operator playbook from here

1. **Tomorrow morning (before 09:15 IST):** deploy `freeze-v2.1` to the
   trader VM. Standard `tools/cloud/deploy.sh` flow.
2. **In parallel:** kick off the backtester VM bootstrap.
   ```
   tools/cloud/bootstrap_backtester.sh <BACKTESTER_IP> \
       https://github.com/<you>/trading-agent.git freeze-v2.1
   ```
3. **First battery run:** once bootstrap finishes (~20 min):
   ```
   export BACKTESTER_VM_HOST=<BACKTESTER_IP>
   bash tools/cloud/launch_battery.sh \
       --days 90 --workers 2 \
       --universe-file tests/fixtures/battery_v2_universe.json
   ```
4. **Weekly review:** every Friday EOD, pull the latest battery output
   and the week's trade CSV. Compare per-strategy stats. Annotate
   surprises directly in `docs/FREEZE_v2.1.md` under a "Week N notes"
   section.
5. **Freeze lift:** when the four exit criteria in `FREEZE_v2.1.md`
   are met. Target date 2026-06-08; slipping is fine.

---

## Late-evening addendum (2026-05-18 ~ 00:00 IST 2026-05-19)

Two pieces shipped after the initial freeze + first-battery deploy.
Both are *observability* / *operational tooling* (NOT frozen files),
so no `freeze-bypass:` required.

### A. Battery queue scheduler (`tools/run_battery_queue.py`)

The backtester VM is up. The trader VM is on `freeze-v2.1`. To get
maximum signal during the 3-week freeze window, the backtester should
not sit idle between manual battery launches -- it should chain
sequentially through a queue.

* `tools/run_battery_queue.py` -- Python orchestrator. Reads a YAML
  queue, for each job spawns `docker run` for `tools/run_battery.py`,
  waits for container exit, marks done in
  `data/battery_queue_state.json`, moves to next. Survives a VM reboot
  via systemd; resumes an in-flight job by reusing its `run_id`
  (battery harness already supports per-folder resume); skips jobs
  already marked completed.
* `tests/fixtures/battery_queue_example.yaml` -- 5-job starter queue
  exploring 4 dimensions of variation: universe (v2 vs nifty50),
  window (60/90/120d), train-vs-holdout slice, and walk-forward split.
  ~70-80 h of continuous compute. When exhausted, the scheduler exits
  cleanly (`Restart=on-failure` only, so systemd doesn't busy-loop).
* `tools/cloud/battery-scheduler.service` -- systemd unit. Runs as
  `opc`, auto-starts on boot.
* `tools/cloud/install_battery_scheduler.sh` -- one-shot installer
  for the VM. Idempotent. Copies the example queue into place,
  installs + enables the unit, runs a dry-run sanity check.
* Defence-in-depth: scheduler itself refuses to start if any
  `ANGELONE_/SMARTAPI_/BROKER_/KITE_*` env var is set (rc=9), even
  though the in-battery `_assert_backtester_isolation()` would catch
  it too.

**On start-up the scheduler waits for any pre-existing `battery_*`
container to finish before processing its queue** -- so installing it
mid-deploy (while the first ad-hoc battery is still running) is
safe.

### B. Per-strategy verdict in audit checkpoint

Exit criterion #4 in `docs/FREEZE_v2.1.md` was:

> "audit_checkpoint.py and the EOD diagnostic produce strategy-level
> reports without manual SQL."

The EOD email side has been doing this for a while (via
`packages/research/diagnostic.py`). The 5-min audit checkpoint did
NOT, so mid-session "how is each strategy doing" required a manual
trades.csv pivot.

`tools/audit_checkpoint.py` now adds `_section_per_strategy()` that
calls into the same `diagnostic` module and embeds a compact table
in every checkpoint:

| Strategy | N | WR% | PF | Kelly | Expectancy | Net PnL | Verdict |

plus a portfolio summary line. Lookback is fixed at 7 days to match
the EOD horizon. Degrades silently when the DB is empty / missing
(returns `enabled=True, n_trades_total=0`); a defensive 0-byte check
also dodges a pre-existing connection leak in `load_trades` that was
keeping Windows tempfile handles open.

### Test deltas

```
tests/unit/test_battery_queue_scheduler.py     (NEW, 25 tests)
tests/unit/test_audit_per_strategy_section.py  (NEW,  7 tests)
```

`pytest tests/`: **1229 passed in 47 s** (+32 vs. previous count;
0 lints; 0 regressions).

### Operator playbook for the new pieces

```bash
# ON THE BACKTESTER VM (after `git pull origin main`):
cd /opt/trading-agent
sudo bash tools/cloud/install_battery_scheduler.sh

# When you're ready to let the queue start (it'll wait for the
# current ad-hoc battery to finish before processing):
sudo systemctl start battery-scheduler

# Watch what it's doing:
sudo journalctl -u battery-scheduler -f

# Edit the queue and re-load (no daemon restart needed):
sudo nano /opt/trading-agent/data/battery_queue.yaml
sudo systemctl restart battery-scheduler
```
