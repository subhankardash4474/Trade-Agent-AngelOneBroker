# Ops Runbook — VMs, Logins, Canonical Commands

> **Audience:** future-me (the agent) at the moment something is on fire, and the
> operator who needs to know what the agent is about to type.
>
> **Promise:** every IP, key, container name, table, column, and copy-pasteable
> command on this system is in this single document. If you would otherwise
> consult `docs/stage3_runbook.md` or `docs/backtester_vm_runbook.md`, those
> are scenario-specific deep dives — start here first.
>
> **Last verified:** 2026-05-25 (live trader + backtester VMs)

---

## 1. The fleet at a glance

There are **exactly two** cloud VMs in this system. Both are on Oracle Cloud
Mumbai (OCI). Local dev machine is the third surface but it is not
described here.

| Role         | Public IPv4       | SSH user  | SSH key (host)                     | Repo path          | Purpose                            |
|--------------|-------------------|-----------|-------------------------------------|--------------------|------------------------------------|
| **Trader**   | `80.225.251.79`   | `ubuntu`  | `$HOME\.ssh\oci_trader_key`         | `/opt/trading-agent` | Runs the live/paper trading daemon |
| **Backtester** | `80.225.197.125` | `opc`     | `$HOME\.ssh\oci_trader_key`         | `/opt/trading-agent` | Runs battery jobs (24/7)          |

Both VMs share the **same SSH key** (`oci_trader_key`). Different login users
because the cloud images differ (Ubuntu 22.04 vs Oracle Linux 9).

### Critical gotchas about identity

- The Docker container on the trader VM is named **`trader`** (not
  `trading-agent`). Past commands using `docker exec trading-agent ...` were
  stale references from before the rename and will silently fail with
  `No such container`. **Always use `trader`.**
- The repo lives at **`/opt/trading-agent`** on both VMs. The path
  `/home/opc/trading-agent` referenced in some older docs is wrong.
- Battery containers on the backtester VM are named
  `battery_<job>_<YYYYMMDDTHHMMSS>` (e.g. `battery_nifty50_60d_20260525T093330`).
  There is no fixed container name — match by the `battery_*` prefix.

---

## 2. SSH from the local Windows host

### 2.1 The basic login

```powershell
# Trader VM
ssh -i $HOME\.ssh\oci_trader_key ubuntu@80.225.251.79

# Backtester VM
ssh -i $HOME\.ssh\oci_trader_key opc@80.225.197.125
```

### 2.2 The base64-bash pattern (THE single most reliable trick on this system)

PowerShell mangles multi-line bash strings, escapes, and quotes when you
pipe them via `ssh "..."`. Many "wrong DB command"-style failures have
been caused by lost backslashes, eaten quotes, or `\\n` becoming literal.

**The robust pattern:** wrap the bash payload in a here-string, base64-encode
it on Windows, and decode-then-pipe on the remote box. This works for any
length of script and never loses quotes:

```powershell
$bashCmd = @'
echo "remote work happens here -- write normal bash with no escaping anxiety"
sudo docker ps
sudo tail -n 50 /opt/trading-agent/logs/trading_agent_$(date +%Y-%m-%d).log
'@
$normalized = $bashCmd -replace "`r`n", "`n"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($normalized))
ssh -i $HOME\.ssh\oci_trader_key ubuntu@80.225.251.79 "echo $b64 | base64 -d | bash"
```

The CRLF→LF normalisation is non-optional — without it the remote `bash`
chokes on `\r` characters and gives misleading "syntax error near unexpected
token" messages.

Use this pattern for anything more complex than `sudo docker ps`. It is
the single biggest source of correctness on this system.

---

## 3. Trader VM — canonical commands

### 3.1 Quick status checks

```bash
# Is the container alive and running?
sudo docker ps --filter "name=trader" --format "{{.Names}} | {{.Status}} | {{.RunningFor}}"

# How long has the daemon been up (PID 7 inside the container)?
sudo docker inspect trader --format='Started: {{.State.StartedAt}}{{println}}RestartCount: {{.RestartCount}}'

# Latest audit checkpoint (the freshest signal that the daemon is functioning)
sudo ls -t /opt/trading-agent/logs/audit/$(date +%Y-%m-%d)/checkpoint_*.md 2>/dev/null | head -1 | xargs -r sudo cat | head -40
```

### 3.2 Logs — where each one lives

| File                                                | What it contains                                |
|-----------------------------------------------------|-------------------------------------------------|
| `logs/trading_agent_YYYY-MM-DD.log`                 | Main strategy log (one line per signal eval)    |
| `logs/daemon_YYYY-MM-DD.log`                        | Supervisor wrapper log (start/stop/sleep)       |
| `logs/signal_audit_YYYY-MM-DD.csv`                  | Per-signal accept/reject audit row              |
| `logs/audit/YYYY-MM-DD/checkpoint_HHMM.md`          | Hourly markdown checkpoints (the audit skill)   |
| `logs/diagnostics/eod_YYYY-MM-DD.md`                | EOD diagnostic snapshot (post-15:35 IST)        |
| `logs/heartbeat_cron.log`                           | Output of the daily 09:10 IST heartbeat cron    |
| `logs/watchdog_cron.log`                            | Output of the */5 min silent-hang watchdog      |

```bash
# Tail today's main log
sudo tail -F /opt/trading-agent/logs/trading_agent_$(date +%Y-%m-%d).log

# Find the last log line for a specific symbol
sudo grep "RELIANCE" /opt/trading-agent/logs/trading_agent_$(date +%Y-%m-%d).log | tail -3

# Count errors / warnings in today's log
sudo grep -c "ERROR" /opt/trading-agent/logs/trading_agent_$(date +%Y-%m-%d).log
sudo grep -c "WARNING" /opt/trading-agent/logs/trading_agent_$(date +%Y-%m-%d).log
```

### 3.3 Container lifecycle (use these — don't improvise)

```bash
# Restart the container (preserves DB, logs, all bind mounts)
cd /opt/trading-agent
sudo docker compose restart trader

# Rebuild + restart (after `git pull` or code change)
sudo docker compose up -d --build trader

# Stop cleanly (use the EMERGENCY_STOP file — NEVER docker kill in market hours)
sudo touch /opt/trading-agent/EMERGENCY_STOP
# wait ~30s for the daemon to notice, then:
sudo docker compose stop trader

# View container env-vars (useful when "is the API key loaded?" comes up)
sudo docker exec trader env | grep -iE "angelone|resend|trader_" | sed 's/=.*$/=***REDACTED***/'
```

### 3.4 Cron jobs on the trader VM

Both are owned by `ubuntu` and run via the host's `sudo -n docker exec`:

```cron
# heartbeat (09:10 IST daily, Mon-Fri)
40 3 * * 1-5 sudo -n docker exec trader python tools/send_heartbeat.py >> /opt/trading-agent/logs/heartbeat_cron.log 2>&1

# silent-hang watchdog (every 5 min, 24/7)
*/5 * * * *  sudo -n docker exec trader python tools/watchdog_check.py >> /opt/trading-agent/logs/watchdog_cron.log 2>&1
```

```bash
# Inspect the current crontab
sudo crontab -u ubuntu -l

# Force a heartbeat right now (test path)
sudo docker exec trader python tools/send_heartbeat.py --force-send

# Run the watchdog probe manually
sudo docker exec trader python tools/watchdog_check.py
```

---

## 4. SQLite — the actual schema (no guessing)

The trading agent's only persistent store is `/app/data/trading_agent.db`
inside the container, bind-mounted from `/opt/trading-agent/data/trading_agent.db`
on the host.

### 4.1 Authoritative table list

```text
candles, ticks, trades, equity_curve, strategy_scores,
trade_patterns, open_positions, regime_weights, orders, sqlite_sequence
```

**There is no `positions` table.** Open positions live in **`open_positions`**.
Any query of the form `SELECT ... FROM positions WHERE status='OPEN'` is
**guaranteed to fail**. (This was the root of the "wrong db command"
operator memory — see §9 RCA.)

### 4.2 `trades` (closed trades only — append-only)

| col              | type     | notes                                         |
|------------------|----------|-----------------------------------------------|
| `id`             | INTEGER  | PK auto-increment                             |
| `symbol`         | TEXT     | NSE symbol (e.g. `RELIANCE`)                  |
| `side`           | TEXT     | `LONG` or `SHORT`                             |
| `entry_price`    | REAL     |                                               |
| `exit_price`     | REAL     |                                               |
| `quantity`       | INTEGER  |                                               |
| `entry_time`     | TEXT     | **ISO-8601** — use for date filtering         |
| `exit_time`      | TEXT     | ISO-8601                                      |
| `pnl`            | REAL     | Rupees, post-commission                       |
| `pnl_pct`        | REAL     |                                               |
| `strategy`       | TEXT     | which strategy triggered entry                |
| `exit_reason`    | TEXT     | `signal`/`stop_loss`/`take_profit`/`eod`/…    |
| `commission`     | REAL     |                                               |
| `slippage`       | REAL     |                                               |
| `market_context` | TEXT     | JSON blob                                     |
| `regime`         | TEXT     | regime classification at entry                |
| `holding_minutes`| REAL     |                                               |

**There is no `date` column.** Aggregating by day uses
`substr(entry_time, 1, 10)` or `DATE(entry_time)`. Past errors of the form
`OperationalError: no such column: date` come from forgetting this.

### 4.3 The four queries I actually use (copy-paste safe)

Use the base64-bash pattern from §2.2 to send these.

#### A. Last N closed trades (sanity check)

```bash
sudo docker exec trader python3 -c "
import sqlite3, json
con = sqlite3.connect('/app/data/trading_agent.db')
c = con.cursor()
c.execute('''
  SELECT entry_time, symbol, side, quantity, entry_price, exit_price, pnl, exit_reason, strategy
  FROM trades
  ORDER BY id DESC
  LIMIT 10
''')
for r in c.fetchall(): print(r)
"
```

#### B. Daily P&L for the last 14 days

```bash
sudo docker exec trader python3 -c "
import sqlite3
con = sqlite3.connect('/app/data/trading_agent.db')
c = con.cursor()
c.execute('''
  SELECT substr(entry_time, 1, 10) AS dt,
         COUNT(*) AS n,
         ROUND(SUM(pnl), 2) AS pnl_rs,
         SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
         SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS losses
  FROM trades
  WHERE entry_time >= date('now', '-14 days')
  GROUP BY dt
  ORDER BY dt
''')
print('date        n   pnl_rs  W   L')
for r in c.fetchall(): print(f'{r[0]}  {r[1]:>3}  {r[2]:>+7}  {r[3]:>2}  {r[4]:>2}')
"
```

#### C. Currently open positions (`open_positions`, NOT `positions`)

```bash
sudo docker exec trader python3 -c "
import sqlite3
con = sqlite3.connect('/app/data/trading_agent.db')
c = con.cursor()
c.execute('SELECT * FROM open_positions')
cols = [d[0] for d in c.description]
rows = c.fetchall()
print('columns:', cols)
print(f'open_positions count: {len(rows)}')
for r in rows: print(dict(zip(cols, r)))
"
```

#### D. DB integrity + last trade timestamp (health probe)

```bash
sudo docker exec trader python3 -c "
import sqlite3
con = sqlite3.connect('/app/data/trading_agent.db')
c = con.cursor()
c.execute('PRAGMA integrity_check'); print('integrity:', c.fetchone()[0])
c.execute('SELECT COUNT(*) FROM trades'); print('trades:', c.fetchone()[0])
c.execute('SELECT MAX(entry_time) FROM trades'); print('last trade:', c.fetchone()[0])
"
```

### 4.4 Rules of thumb to prevent the next "wrong DB command"

1. **Never inline a multi-quote SQL into `docker exec ... -c "..."`** —
   use `python3 -c "..."` with a triple-quoted SQL inside, sent via the
   base64-bash wrapper. Shell escaping of `'`, `"`, and `\` across two
   shells (host → docker) eats characters silently.
2. **Always print column names before assuming a schema.** The agent has
   changed the schema once (added `holding_minutes`, `regime`); the next
   change will silently break old queries.
3. **Read-only queries are still queries.** A SELECT can leave the WAL
   file in a state that confuses concurrent writers if Python exits
   uncleanly. Always close the connection (`con.close()`) when running
   manually.

---

## 5. Backtester VM — canonical commands

### 5.1 Quick status

```bash
# Is the scheduler running?
sudo systemctl status battery-scheduler.service --no-pager | head -10

# What battery container(s) are running?
sudo docker ps --filter "name=battery" --format "{{.Names}} | {{.Status}}"

# Current queue state
sudo cat /opt/trading-agent/data/battery_queue_state.json
```

### 5.2 Battery scheduler lifecycle

```bash
# Restart the scheduler (does NOT touch a running battery container)
sudo systemctl restart battery-scheduler.service

# Stop the scheduler (a running container keeps running until it finishes)
sudo systemctl stop battery-scheduler.service

# Tail scheduler stdout
sudo journalctl -u battery-scheduler.service -f --no-pager
```

### 5.3 Battery results

```bash
# All battery runs (newest first)
sudo ls -lt /opt/trading-agent/logs/battery/ | head -20

# Live progress of the current run (per-variant tail)
RUN_ID=$(sudo cat /opt/trading-agent/data/battery_queue_state.json | python3 -c "import json,sys; print(json.load(sys.stdin)['jobs']['nifty50_60d']['run_id'])")
sudo ls /opt/trading-agent/logs/battery/$RUN_ID/
sudo tail -n 5 /opt/trading-agent/logs/battery/$RUN_ID/V*.log 2>&1 | tail -50

# Use the helper script for a one-screen summary
pwsh -File tools/battery_status_remote.ps1   # from local Windows
```

### 5.4 Forcing a clean restart of a job (rare)

```bash
# 1. Stop the scheduler
sudo systemctl stop battery-scheduler.service

# 2. Stop the currently-running container (if any)
sudo docker stop $(sudo docker ps -q --filter "name=battery")

# 3. Archive the old run directory before deleting (forensics)
RUN_DIR=/opt/trading-agent/logs/battery/<run_id>
sudo tar -czf "${RUN_DIR}_LEGACY_<reason>.tar.gz" -C "$(dirname $RUN_DIR)" "$(basename $RUN_DIR)"
sudo rm -rf "$RUN_DIR"

# 4. Clear the job from state (use sudo bash, not sudo python -c — see §6.3)
sudo bash -c "python3 -c '
import json
p = \"/opt/trading-agent/data/battery_queue_state.json\"
with open(p) as f: s = json.load(f)
s[\"jobs\"].pop(\"<job_name>\", None)
with open(p, \"w\") as f: json.dump(s, f, indent=2)
'"

# 5. Restart scheduler — it will spawn a fresh run with a new run_id
sudo systemctl start battery-scheduler.service
```

The `--resume <run_id>` vs `--run-id <run_id>` distinction is handled by
`tools/run_battery_queue.py` automatically based on the `resuming` field
in the state file (fixed 2026-05-25; see `docs/findings_log_2026-05-25.md` §10).

---

## 6. PowerShell + cross-VM pitfalls I keep hitting

These are the failures that have wasted real time on this system. Each has
a documented workaround.

### 6.1 PowerShell "Add-Content: Stream was not readable"

This is a **cosmetic PowerShell error** triggered by `ssh` writing both
stdout and stderr to the same console. It does not indicate a real
failure. The remote command still ran and returned its real exit code in
`$LASTEXITCODE`. **Ignore it** — read the actual stdout/stderr above the
error.

### 6.2 PowerShell `Write {}` JSON parsing error

A transient PSReadLine / pwsh integration glitch. Re-run the exact same
command; it succeeds the second time. Not a real problem.

### 6.3 `sudo python -c "json.load(...)"` failing with `PermissionError`

When the file is owned by `trader` (uid 1001) and you `sudo python -c`,
the Python process inherits root's environment but `sudo` does not set
the python executable's effective uid correctly for nested write
operations on certain filesystems. **Workaround:** wrap in `sudo bash -c
"python3 -c '...'"`. The extra shell hop fixes the file-write path.

### 6.4 `Error response from daemon: No such container: trading-agent`

You used the old container name. The container is **`trader`**, not
`trading-agent`. The image is still tagged `trading-agent:latest` (see
`docker-compose.yml`), which is what causes the confusion.

### 6.5 `sqlite3.OperationalError: no such column: date`

The `trades` table has `entry_time` (ISO-8601 string), not `date`. Use
`substr(entry_time, 1, 10)` or `DATE(entry_time)` to group by day. See
§4.2.

### 6.6 `ModuleNotFoundError: No module named 'pandas'` during a verification step

Misleading. You ran `python3 -c "import pandas"` on the **host**, where
pandas isn't installed. Pandas is inside the **container**. Either:
- Run inside the container: `sudo docker exec trader python3 -c "import pandas; print(pandas.__version__)"`
- Or just `grep` the source file instead of importing — usually faster.

### 6.7 ssh "Permission denied (publickey)" to `opc@140.245.10.21`

That IP is **wrong**. The Trader VM is `ubuntu@80.225.251.79`, the
Backtester VM is `opc@80.225.197.125`. Neither is `140.245.10.21` — that
was a decommissioned IP from an earlier deployment.

---

## 7. Diagnostic flow — when something looks wrong

Use this order. Each step is fast and disambiguates the next.

```text
Step 1: Is the daemon's audit checkpoint fresh?
        sudo ls -t /opt/trading-agent/logs/audit/$(date +%Y-%m-%d)/ | head -3
        If newest checkpoint is <90 min old → daemon is fine, look elsewhere
        If >90 min old or missing → continue

Step 2: Is the container still running?
        sudo docker ps --filter "name=trader"
        If not → check `docker inspect trader --format='{{.State.ExitCode}} {{.State.Error}}'`

Step 3: Is the daemon process alive inside the container?
        sudo docker exec trader ps -ef | grep -v grep | head
        Expect PID 1 = supervisor, PID 7 (or similar) = trading_agent.py

Step 4: Was the daemon writing logs until recently?
        sudo tail -F /opt/trading-agent/logs/trading_agent_$(date +%Y-%m-%d).log
        Look for [HEARTBEAT] every ~5 min during market hours

Step 5: Did the watchdog fire?
        sudo tail -50 /opt/trading-agent/logs/watchdog_cron.log

Step 6: Are healthchecks timing out?
        sudo journalctl -u docker.service --since "1 hour ago" | grep -i health
```

If steps 1-4 are healthy but the agent isn't trading, it's a **regime
or signal gating** issue, not a crash — read the audit checkpoint's
"Day P&L" + "Why no trades?" sections.

---

## 8. Committing on the trader VM (only when needed)

We try **never** to make manual edits on the cloud VM — all changes go via
the local repo → git push → VM `git pull`. But operator hot-fixes happen.
The right pattern:

```bash
# On the trader VM
cd /opt/trading-agent
sudo -u trader git status               # confirm what changed
sudo -u trader git diff <file>          # review hot-fix

# Pull from main BEFORE committing the hot-fix to avoid divergence
sudo -u trader git pull origin main

# Then commit on your local machine after pulling those changes back.
```

If something on the VM was edited with `sudo` (root-owned), reset
ownership first:

```bash
sudo chown -R trader:trader /opt/trading-agent
```

The container itself runs as uid **1001** (matches host `trader` user)
courtesy of the `TRADER_UID` build arg in `docker-compose.yml`. Mismatched
ownership causes `Permission denied` on the bind-mounted `data/`, `logs/`,
or `models/`.

---

## 9. RCA — Friday 2026-05-22 silent-hang event

This is the incident the operator remembered as "the wrong db command broke
everything on Friday". The forensic answer is more nuanced: the mangled DB
command happened on Thursday and didn't cause the failure, but several
operational gaps that the incident exposed are real and have follow-ups.

### 9.1 What actually happened (timeline, IST)

| Time (IST)          | Event                                                                                                               |
|---------------------|---------------------------------------------------------------------------------------------------------------------|
| Thu 2026-05-21 12:37 | Operator runs `docker exec trading-agent python3 -c '... FROM positions WHERE status=OPEN ...'` — heavily PowerShell-mangled, with old container name `trading-agent` and non-existent `positions` table. **Errors and exits cleanly.** No daemon impact. |
| Thu 2026-05-21      | Trader runs full session, 345 KB signal_audit (normal volume), GREEN throughout.                                    |
| Fri 2026-05-22 11:44 | Docker healthcheck for `trader` container begins timing out (warning level, not error).                            |
| Fri 2026-05-22 11:44 → 12:23 | Daemon **still emits logs** but with growing latency; healthcheck timeouts repeat ~every 2 min for 40 min. |
| Fri 2026-05-22 12:23:11 | Last `[HEARTBEAT]` line, cycle 87, positions=0, day P&L=+₹80, regime=`bear_high_vol`.                          |
| Fri 2026-05-22 12:23:53 | Last audit checkpoint written (`checkpoint_1223.md`, verdict GREEN).                                          |
| Fri 2026-05-22 12:24:55 | Last log line: `Scanning 500 NSE stocks...`                                                                    |
| Fri 2026-05-22 12:25 → 23:34 | **Total silence.** Daemon process technically alive but blocked. No logs, no checkpoints, no signal_audit rows. 3 hours of remaining market session lost. |
| Fri 2026-05-22 23:34 | `containerd` restarts (likely host-level OCI maintenance event), but the trader container stays up in zombie state. |
| Sat 2026-05-23 06:47 | Container `a8c39d1d...` killed by `docker stop` (probably the operator). New container starts, sleeps until Monday. |

### 9.2 What the proximate cause was

The daemon **blocked inside a scanner network call with no timeout**. The
last log line is `Scanning 500 NSE stocks...` (from the universe scanner's
top-of-cycle banner). The next expected line — `Scan complete in X.Xs ...`
— never appears.

Evidence:
- Healthcheck timeouts started 40 min before the full silence, suggesting
  progressively longer scanner cycles (network calls degrading).
- DB integrity is `ok`, table count is intact, no corruption.
- `journalctl` shows no OOM kill, no SIGSEGV, no traceback anywhere.
- Last action was network-bound (scanner), not DB-bound.

**Python's `requests`/`urllib3` have NO default timeout.** A slow or
hung remote endpoint (NSE indices API, yfinance, broker LTP service)
blocks indefinitely. This is the textbook silent-hang pattern.

### 9.3 What the "wrong DB command" actually was, and why it wasn't the root cause

The operator's memory is real — the May 21 command in `sudo` history is:

```text
sudo docker exec trading-agent python3 -c 'import json,sqlite3;
  con=sqlite3.connect(" /app/data/trading_agent.db\);
  cur=con.cursor();
  cur.execute(\SELECT symbol,side,quantity,... FROM positions WHERE status=\\OPEN\\\);
  ...'
```

Three layers of failure in one line:
1. **Wrong container name** (`trading-agent` → should be `trader`).
2. **Wrong table** (`positions` → should be `open_positions`).
3. **Wrong escapes** — PowerShell ate the `"`/`'`/`\` characters; the
   Python expression isn't even parseable as Python.

The command therefore **did not execute**. It errored out at the
container-lookup or Python-parse stage, held no SQLite locks, wrote no
files. The daemon kept running fine for 24+ hours after.

But the command was so obviously a misfire that the operator
(correctly) flagged it as a process gap. The real fix is the canonical
queries in §4.3 above.

### 9.4 Why we didn't detect the silent hang sooner

Multiple defense layers were missing on May 22:

| Layer                       | State on 2026-05-22 | State now (2026-05-25)            |
|-----------------------------|---------------------|------------------------------------|
| Outbound HTTP timeouts      | Not enforced        | Still not enforced — **OPEN**     |
| Docker auto-restart on unhealthy | Not configured | Still not — **OPEN**              |
| Silent-hang watchdog cron   | Did not exist       | Added in `eb5bb84` (5-min cadence)|
| Heartbeat email             | Did not exist       | Added in `05eaea0` (09:10 IST)    |
| Audit-checkpoint freshness  | Existed but no alert| Watchdog now alerts on stale checkpoint |
| Healthcheck timeout in journald | Visible but not surfaced | Watchdog reads health.json directly |

### 9.5 What we have already fixed since

- **`05eaea0`** (May 24): daily 09:10 IST heartbeat email to operator.
- **`eb5bb84`** (May 25): intra-day silent-hang watchdog cron (every 5 min);
  alerts if `health.json` mtime > threshold seconds.
- **`3d07219`** (May 25): tuned watchdog STALE threshold to avoid
  false-positives caused by 5-cycle heartbeat cadence.
- **`0cc51c7`** (May 25): heartbeat schema-drift fix.
- **`eb5bb84`** also fixed C6 (NSE Nifty 500 universe shrinkage), which
  was a related symptom of cloud-IP-blocked NSE endpoints — same family
  of "outbound HTTP failure not handled" bugs.

### 9.6 What is still open

1. **Add explicit timeouts to every outbound network call.** Audit
   `packages/core/scanner.py`, `packages/data/`, `packages/brokers/` for
   `requests.get(...)` and `urlopen(...)` without `timeout=`. This is the
   actual root-cause fix. **(Not yet done.)**
2. **Container auto-restart on `unhealthy`.** Docker does not restart on
   healthcheck failure alone. Either:
   - Wrap with a sidecar that polls `docker inspect ... --format
     '{{.State.Health.Status}}'` and restarts on `unhealthy`, or
   - Move to systemd-level supervision with `Restart=on-failure` and a
     `WatchdogSec=` directive that the daemon notifies (`sd_notify`).
3. **Hard time budget per scanner cycle.** Each top-of-cycle scan should
   abort if not complete within 90s (cycle is 60s; one stale cycle is
   acceptable, indefinite hangs are not). Implement via
   `concurrent.futures.ThreadPoolExecutor` with a hard `result(timeout=)`.
4. **Operator runbook for `unhealthy` state.** Document the exact
   sequence: confirm via journald, capture `py-spy` dump of the
   container's PID 7, then restart.

### 9.7 Lessons distilled

- A daemon that **does not crash** but stops working is harder to detect
  than one that crashes loudly. Add liveness probes that test
  *responsiveness*, not just *aliveness*.
- The "wrong DB command" memory was a misattribution — but the underlying
  insight ("I improvised a query under pressure and don't know if it broke
  something") is correct and worth addressing structurally. §4.3
  canonical queries are the durable answer.
- **Always use the base64-bash pattern (§2.2)** for non-trivial remote
  commands. Every quoting nightmare on this system has had this same
  root cause.

---

## 10. Quick links to deep dives

| If you need to…                            | Read                                              |
|--------------------------------------------|---------------------------------------------------|
| Launch Stage 3 (real money)                | `docs/stage3_runbook.md`                          |
| Recover from a backtester isolation guard  | `docs/backtester_vm_runbook.md`                   |
| Understand the freeze policy               | `docs/FREEZE_v2.1.md`                             |
| See current weekly freeze log              | `docs/freeze_log_week1.md`                        |
| Audit findings from May 25 deep scan       | `docs/findings_log_2026-05-25.md`                 |
| Audit changes log from May 25              | `docs/changes_done_2026-05-25.md`                 |
| Architecture overview                      | `docs/ARCHITECTURE.md`                            |

If you find a command in this document that fails, the document is
wrong — file a fix-up in the same commit as your actual work.
