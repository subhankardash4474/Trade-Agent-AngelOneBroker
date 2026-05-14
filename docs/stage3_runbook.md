# Stage 3 Go-Live Runbook

**Scope**: First real-money production run. 5 large-cap NSE names,
Rs 5,000 capital, single 09:30-12:30 IST session, hard rupee floor at
-Rs 500. This document is the operational checklist; the strategic
context lives in `docs/e2e_broker_test_plan.md`.

**Target day**: Monday, 18 May 2026 (first trading day after this prep).

---

## TL;DR launch command (on the trader VM)

```bash
cd /opt/trading-agent
./tools/cloud/stage3_launch.sh --dry-run     # T-1 hour
./tools/cloud/stage3_launch.sh               # T-5 min, after removing EMERGENCY_STOP
```

The launch script handles: pre-flight, paper-daemon teardown, broker
auth smoke test, DB sanity, pre-flight snapshot, and the actual
`docker compose` invocation with all Stage 3 flags.

---

## 1. The day before (Sun evening)

### 1.1 Verify the deploy on the VM is current

```bash
ssh -i $HOME\.ssh\oci_trader_key ubuntu@80.225.251.79
cd /opt/trading-agent
git log -1 --format='%h %s'
# Expected: latest commit (Stage 3 prep) hash
ls config_overlays/stage3.yaml docker-compose.stage3.yml tools/cloud/stage3_launch.sh
# Expected: all 3 exist
```

If any are missing, re-pull and rebuild:
```bash
git pull
docker compose build trader
```

### 1.2 Arm the kill-switch (file should exist all night)

```bash
touch /opt/trading-agent/EMERGENCY_STOP
ls -la /opt/trading-agent/EMERGENCY_STOP
```

### 1.3 Manually sanity-check the broker portal

- Log into AngelOne web (smartapi.angelbroking.com or Angel One mobile)
- Confirm: balance >= Rs 5,500 (Rs 5k for Stage 3 + Rs 500 buffer for
  charges; we ran Stage 2.1 with Rs 1,000 and the live fills consumed
  the float -- top up if needed)
- Confirm: no open positions, no pending orders
- Bookmark the **"Square off all"** button on the positions page
- Practice clicking through: login -> Positions -> "Square off all" -> Confirm.
  Should take < 30 seconds.

### 1.4 Set redundant phone alarms

- 09:25 IST -- pre-flight + EMERGENCY_STOP removal
- 09:30 IST -- session begins
- 12:25 IST -- "5 min to cutoff"
- 12:30 IST -- "session should be self-stopping now; verify"
- 12:35 IST -- "run post-session reconciliation"

---

## 2. Monday morning (target: 08:55 IST onward)

### 2.1 Pre-flight DRY RUN (08:55 - 09:10 IST)

```bash
ssh -i $HOME\.ssh\oci_trader_key ubuntu@80.225.251.79
cd /opt/trading-agent
./tools/cloud/stage3_launch.sh --dry-run
```

The dry-run does everything except the actual `docker compose up`:
- Confirms overlay file parses
- Tears down paper daemon (this is real! paper trading stops here.
  If you want to keep paper running today, abort and reconfigure.)
- Confirms `.env` has all 5 AngelOne vars
- Runs the read-only auth smoke test (`tools/test_angelone_auth.py`)
- Confirms DB has 0 open positions
- Snapshots DB + logs to `archive/stage3_<timestamp>/`

**Expected output**: `DRY-RUN GREEN -- all pre-flight checks passed.`

If anything is **BLOCKING**, ABORT. Do not edit configs to make a check
pass; debug instead.

### 2.2 Final manual checks (09:10 - 09:25 IST)

| Check | How | Expected |
|---|---|---|
| Broker still authenticates | Re-read dry-run output | "broker auth smoke test passed" |
| Cash >= Rs 5,000 | AngelOne web -> Funds | YES |
| No open positions | `docker exec ... sqlite3 ... 'SELECT * FROM open_positions'` | empty |
| EMERGENCY_STOP present | `ls /opt/trading-agent/EMERGENCY_STOP` | file exists |
| IP whitelist still valid | smartapi.angelbroking.com -> My Apps -> Edit App | "Primary Static IP" == 80.225.251.79 |
| It's actually Monday | `date` | Mon |
| Markets are open | NSE holiday calendar | not a holiday |

### 2.3 GO (09:25 IST)

```bash
# 1. Remove the kill-switch (THIS IS THE COMMIT POINT)
rm /opt/trading-agent/EMERGENCY_STOP

# 2. Launch
cd /opt/trading-agent
./tools/cloud/stage3_launch.sh
```

The launch script will:
1. Re-run the full pre-flight (so the dry-run + actual launch are
   guaranteed in sync).
2. Confirm EMERGENCY_STOP is no longer present.
3. Bring up `trader-stage3` container with all the right flags.
4. Tail 60 seconds of logs so you see the boot banner.

**Look for in the boot banner**:
- `Mode: LIVE (--live, REAL MONEY)`
- `[OVERLAY] applied config_overlays/stage3.yaml`
- `[E2E] --max-loss-rs: Rs 500.00`
- `[E2E] --single-shot: one round-trip per symbol per day`
- `[E2E] --live: real-money orders -- pre-flight checks MUST be green`

If any of these lines is missing or says PAPER, ABORT and post-mortem.

### 2.4 First-trade verification (09:30 - 09:35 IST)

The market opens at 09:15 but Stage 3's window starts at 09:30 (set
in the overlay). Between 09:30 and the first signal:

- Open a separate terminal and tail the daemon: `docker compose -f docker-compose.yml -f docker-compose.stage3.yml logs -f trader`
- Watch for: `[SIGNAL]` -> `[ORDER PLACED]` -> `[FILL]` lines
- Cross-check the first fill against AngelOne web (Order Book / Trade Book)
  within 60 seconds.
- If the order shows in our logs but NOT on the broker, that's a P0
  bug -- hit the kill-switch (`touch /opt/trading-agent/EMERGENCY_STOP`)
  and investigate.

---

## 3. Mid-session monitoring (09:30 - 12:30 IST)

### 3.1 Things to watch (passive)

```bash
# Live daemon logs
docker compose -f docker-compose.yml -f docker-compose.stage3.yml logs -f trader

# Trade-book diff (run every 30 min)
docker exec trader-stage3 python tools/reconcile_trade_book.py --date $(date +%Y-%m-%d)

# Current P&L
docker exec trader-stage3 sqlite3 data/trading_agent.db \
    "SELECT SUM(realized_pnl) FROM trades WHERE date(exit_time)=date('now', 'localtime')"
```

### 3.2 Kill-switch escalation ladder

| Symptom | Action |
|---|---|
| One trade goes against plan | Watch -- single trade is in budget |
| Realised P&L < -Rs 200 | Verify max-loss-rs gate is armed; consider manual flatten |
| Realised P&L < -Rs 400 | TOUCH `EMERGENCY_STOP`. Daemon will refuse new entries; existing positions get managed by SL/TP. |
| Realised P&L hits -Rs 500 | Daemon trips its own breaker (auto, no action needed). Verify. |
| Daemon log shows tracebacks | TOUCH `EMERGENCY_STOP`. Stop the container if positions are open and broker SL/TP is reliable. |
| Broker shows a fill that's NOT in our DB | TOUCH `EMERGENCY_STOP`. Manually flatten via AngelOne web ("Square off all"). |
| You feel uncertain for any reason | TOUCH `EMERGENCY_STOP`. We can resume tomorrow. |

The whole point of Stage 3's small capital is that *every escalation
above is cheap*. Treat the rupee-floor and the kill-switch as muscle
memory you're training -- the cost of a false alarm is negligible
compared to the cost of missing a real one.

### 3.3 Hard kill (nuclear option)

If the daemon won't respond to `EMERGENCY_STOP`:

```bash
# Stop the container -- daemon is killed; existing positions
# remain with the broker. Manage them via AngelOne web.
docker compose -f docker-compose.yml -f docker-compose.stage3.yml down

# Then flatten manually on AngelOne web:
# Positions tab -> "Square off all" -> Confirm.
```

---

## 4. End of session (12:30 IST and after)

### 4.1 The 12:30 IST cutoff

The Stage 3 overlay sets `market.trading_hours.end = 12:30`. At 12:30:01:
- `is_market_window()` returns False
- `run_daemon` enters `sleep_until_market()` (idle until next day)
- The daemon does NOT auto-flatten -- existing positions ride to broker
  SL/TP

If there's still an open position at 12:30, you have two choices:
1. **Let SL/TP manage it.** The strategy's SL/TP is whatever the
   strategy emitted at entry. Manual square-off only if the position
   is far from both levels and you'd rather realize now.
2. **Manual flatten.** AngelOne web -> Square off. Then reconcile.

### 4.2 Post-session reconciliation (12:35 - 13:00 IST)

```bash
# 1. Full trade-book diff (SQLite vs broker)
docker exec trader-stage3 python tools/reconcile_trade_book.py \
    --date $(date +%Y-%m-%d) --output logs/stage3_reconcile.json

# 2. Per-trade post-mortem
docker exec trader-stage3 python tools/trade_postmortem.py \
    --date $(date +%Y-%m-%d) > logs/stage3_postmortem.md

# 3. Realised slippage report
docker exec trader-stage3 python -c "
import csv, datetime as dt
today = dt.date.today().isoformat()
with open('data/slippage_log.csv') as f:
    rows = [r for r in csv.DictReader(f) if r['fill_time'].startswith(today)]
print(f'Stage 3 fills: {len(rows)}')
for r in rows:
    print(f'  {r[\"symbol\"]:<10} {r[\"side\"]:<5}  bps={r[\"slippage_bps\"]:>+.1f}')
"

# 4. Pull all artefacts to laptop
exit
# (back on laptop)
.\tools\cloud\pull_logs.ps1
```

### 4.3 Decision: Stage 3 PASS / FAIL

Per `docs/e2e_broker_test_plan.md` section "Pass criteria":

| Criterion | Pass | Fail |
|---|---|---|
| Unreconciled trades | 0 | >=1 |
| Untriggered SL/TP | 0 | >=1 |
| Max-loss kill-switch fired in anger | no (verified in pre-flight only) | yes (=fail unless P0 prevented worse) |
| Slippage vs LTP-at-decision | median <= 10 bps | > 10 bps median |

**Green = Stage 3 PASS.** Schedule Stage 4 (10 stocks, Rs 10k) for
next Monday.

**Any red = Stage 3 FAIL.** Stop, debug, do not advance to Stage 4
until you can run a CLEAN Stage 3 with no red flags.

### 4.4 Tear-down (whichever outcome)

```bash
# Stop Stage 3 container
docker compose -f docker-compose.yml -f docker-compose.stage3.yml down

# Restore paper daemon for the rest of the week
docker compose up -d trader
docker compose logs --tail=20 trader   # confirm "PAPER" in the boot banner
```

### 4.5 Append to journal

Write a 5-line summary to `docs/e2e_broker_test_log.md`:
```markdown
## 2026-05-18 Stage 3 -- 5-stock basket
- Trades: N entries / M exits
- Realised P&L: +/- Rs XYZ (after charges)
- Slippage median: +/- N bps
- Outcome: PASS / FAIL because <reason>
- Next: <Stage 4 scheduled / debug X / etc>
```

---

## 5. Common pitfalls (read before Monday)

| Pitfall | Avoidance |
|---|---|
| Forgetting to remove EMERGENCY_STOP before launch | The launch script REFUSES to start if the file exists. Read its final message. |
| Daemon container says PAPER in boot banner | Means `--live` did not stick (compose override file path typo?). Tear down immediately, do NOT proceed. |
| Auto-restart loops re-firing EOD emails | Fixed 2026-05-13 (3-layer dedup). Confirm: only ONE EOD email should arrive after 12:30. |
| Backtest VM (Ampere) interferes with trader | They're separate VMs. The trader VM IP is the one whitelisted. |
| Paper daemon DB pollution if both run | Stage 3 uses the SAME `data/trading_agent.db`. Stage 3 trades will live alongside paper trades, distinguished by their `mode` column. Post-mortem filters on `mode='live'`. |

---

*Last reviewed: 2026-05-14. Owner: trading-agent ops.*
