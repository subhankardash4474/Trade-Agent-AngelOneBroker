# Backtester VM Runbook

> **Audience:** the operator (you) at the moment something goes wrong on
> the backtester VM, when it's the worst time to be inventing a response.
>
> **Promise:** every section below pre-decides the response so you don't
> improvise. Each scenario lists the trigger, the wrong move, and the
> right sequence.

VM identity (as of 2026-05-19):

| Field | Value |
|---|---|
| Public IPv4 | `80.225.197.125` (OCI) |
| SSH user | `opc` |
| Trader home | `/home/opc/trading-agent` |
| Git ref pinned at bootstrap | `freeze-v2.1` |
| Battery scheduler unit | `battery-scheduler.service` |
| Queue file | `data/battery_queue.yaml` |
| State file | `data/battery_queue_state.json` |
| Results | `logs/battery/<run_id>/` |

---

## 1. Backtester isolation guard fires (`SystemExit(9)`)

**Trigger:** `tools/run_battery_queue.py` or a manual `run_battery`
invocation aborts with `SystemExit(9)` and the message:
`refusing to run battery — broker creds detected with BACKTESTER_MODE=1`.

**Wrong move:** `unset ANGELONE_API_KEY && retry`. Sometimes correct,
sometimes not. The right question is "**how did the credential get
there?**", not "**how do I get past the guard?**"

**Right sequence:**

1. **Do not retry.** The guard is the cheap part of the response.
2. **Find the credential** that triggered the abort:
   ```bash
   ssh opc@80.225.197.125 \
     "env | grep -iE 'angelone|smartapi|broker|kite' || cat /etc/systemd/system/battery-scheduler.service.d/*.env 2>/dev/null || true"
   ```
3. **Locate its source** — usually one of:
   - `~/.env` or `~/trading-agent/.env` (leaked deploy)
   - A systemd drop-in override
   - An exported env var in `~/.bashrc` / `~/.profile`
   - A docker-compose `.env` file mounted into the scheduler container
4. **Remove the credential** from the source.
5. **Rotate the credential at the broker.** It is now potentially
   leaked — `printenv` output, shell history, `cat` to a file — and
   the leak vector is what we just fixed. Issue a new key at Angel One,
   update the live trader's `.env`, restart the live trader, *then*
   come back to the backtester.
6. **Verify the guard passes:**
   ```bash
   ssh opc@80.225.197.125 \
     "cd ~/trading-agent && BACKTESTER_MODE=1 docker compose run --rm backtester python tools/run_battery_queue.py --dry-run"
   ```
7. **Re-enable the scheduler:** `sudo systemctl restart battery-scheduler`.
8. **Document.** Add a one-line `freeze-bypass:` entry to the bypass
   ledger in `docs/FREEZE_v2.1.md` only if the fix touched a frozen
   file (almost certainly it didn't — the fix is operational).

The guard exists precisely so this can't go wrong silently. When it
fires, treat it as a useful event, not a friction point.

---

## 2. Battery scheduler stopped

**Trigger:** no new run for > 2 hours when the queue says one should
have started. `systemctl status battery-scheduler` shows `inactive` or
`failed`.

**Right sequence:**

1. **Capture the failure** before restarting:
   ```bash
   ssh opc@80.225.197.125 "sudo journalctl -u battery-scheduler --since '2 hours ago' --no-pager" > scheduler_failure_$(date +%Y%m%d_%H%M%S).log
   ```
2. **Check the state file** — `data/battery_queue_state.json` should
   tell you which job was last completed. The next job should resume
   on restart (`run_battery_queue.py` has resume semantics built in).
3. **Restart:**
   ```bash
   ssh opc@80.225.197.125 "sudo systemctl restart battery-scheduler"
   ```
4. **Verify it started cleanly:**
   ```bash
   ssh opc@80.225.197.125 "sudo journalctl -u battery-scheduler --since '5 minutes ago' --no-pager"
   ```
   Expected: `loaded N jobs from queue`, then either `picking up where
   we left off` or `waiting for in-flight container`.
5. **If it crashes within 60 s of restart:** read the captured log
   carefully — most failures are queue-file parse errors after an
   edit. Validate the YAML with:
   ```bash
   ssh opc@80.225.197.125 "cd ~/trading-agent && python -c 'import yaml; print(yaml.safe_load(open(\"data/battery_queue.yaml\")))'"
   ```

Do NOT manually run individual batteries while the scheduler is also
running. The scheduler's wait-loop will detect a pre-existing container
and back off, but two concurrent batteries can race on the results
directory.

---

## 3. Backtester VM reclaimed by OCI

**Trigger:** SSH to `80.225.197.125` returns connection refused or
"host unreachable" for > 15 minutes. The OCI console shows the
instance as terminated or in a non-RUNNING state. Free-tier A1.Flex
shapes are sometimes reclaimed under capacity pressure.

**Right sequence:**

1. **Confirm it's the VM, not the network.** Try pinging a known-up
   host first.
2. **Provision a replacement** through the OCI console (Always-Free
   tier still works for new VMs after a reclaim).
3. **Run the bootstrap script:**
   ```powershell
   tools/cloud/bootstrap_backtester.sh <new_ip>
   ```
   Bootstrap is idempotent and clones at `freeze-v2.1` automatically.
4. **Restore the queue state if you snapshot it.** If the previous
   VM's `data/battery_queue_state.json` was snapshotted (see §5), copy
   it to the new VM. Otherwise the new scheduler will start the queue
   from job 1 — which is acceptable; nothing is lost beyond compute
   time.
5. **Install the scheduler:**
   ```bash
   ssh opc@<new_ip> "cd ~/trading-agent && bash tools/cloud/install_battery_scheduler.sh"
   ```
6. **Document** the reclaim event in the next `changes_done_*.md`
   with the new IP. Update §VM identity at the top of THIS file.

The trader VM is unaffected by this scenario.

---

## 4. Battery run produces zero trades

**Trigger:** `pull_battery_results.ps1 -RunId <id>` shows 0 closed
trades across all symbols, or only a few trivial trades.

**Likely cause** (in descending order of probability):

1. The universe file is empty or malformed — the Docker container
   resolved a 0-line `tests/fixtures/<universe>.json`.
2. The window is on a non-trading-day range (e.g. a misconfigured
   `--start-date` lands on a weekend cluster).
3. The data source upstream of the historical cache failed and the
   battery silently fell back to empty bars.
4. A code path that the live trader doesn't exercise was broken by an
   observability-only edit.

**Right sequence:**

1. **Read `logs/battery/<run_id>/battery.log`** — the first line
   typically tells you the universe size and date range. If
   `universe size: 0` is present, the file is wrong.
2. **Verify the universe file** locally:
   ```powershell
   Get-Content tests/fixtures/<universe>.json | ConvertFrom-Json | Measure-Object
   ```
3. **Verify the date range** matches a real trading window. Days like
   2026-04-14 (BR Ambedkar Jayanti) or 2026-05-01 (Maharashtra Day)
   are non-trading and skew automated date math.
4. **If both look right**, the bug is in code. Re-run with a single
   symbol and verbose logging to narrow:
   ```bash
   ssh opc@80.225.197.125 \
     "cd ~/trading-agent && docker compose run --rm backtester \
        python -m packages.research.battery \
        --universe INFY --days 7 --workers 1 --verbose 2>&1 | tee debug.log"
   ```
5. **Triage** based on the verbose output. Treat any fix to backtester
   code as observability — does NOT touch a frozen file — so does not
   count against the bypass cap.

---

## 5. Snapshot routine (preventive)

To survive §3 (VM reclaim) gracefully, snapshot the backtester state
periodically. Suggested daily cadence, before the queue starts the
next overnight job:

```bash
ssh opc@80.225.197.125 \
  "cd ~/trading-agent && tar czf /tmp/bt_snapshot_$(date +%Y%m%d).tgz \
     data/battery_queue.yaml data/battery_queue_state.json \
     logs/battery/ models/"
scp opc@80.225.197.125:/tmp/bt_snapshot_*.tgz ./snapshots/
```

Snapshots live outside both VMs so they survive a double-failure
(trader + backtester) scenario.

---

## 6. What this runbook does NOT cover

- **Live trader incidents** — those are in `docs/healthcheck_runbook.md`
  (if it exists) and the daemon's own alerting.
- **Strategy edits** — frozen. See `FREEZE_v2.1.md`.
- **Model retraining** — frozen for the duration of freeze-v2.1.
- **Anything that requires touching `config.yaml` strategy/risk
  blocks** — that's a freeze decision, not a runbook step.

If the situation isn't covered here, the right move is: **stop, write
the new scenario into this file, decide the response, then act.**
Improvising at 02:00 IST is the failure mode.

---

*Author: Trading Agent dev (Subhanda) + Claude.*
*Drafted: 2026-05-19, post-freeze-v2.1 deployment.*
