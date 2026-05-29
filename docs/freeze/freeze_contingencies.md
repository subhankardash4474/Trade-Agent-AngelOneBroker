# Freeze-v2.1 Contingencies — pre-decided responses

> **Status:** PRE-WRITTEN 2026-05-19. Each section pre-commits to a
> response for a scenario the external verdict flagged as plausible
> during the 2026-05-18 → 2026-06-08 window.
>
> **Promise:** when a scenario hits, the operator follows THIS document.
> No improvising at 02:00 IST. The verdict's recurring theme — operator
> drift, bypass abuse, tweak spiral — is defeated by pre-commitment.

---

## Index — match the scenario to the section

| # | Scenario | Section |
|--:|---|---|
| 1 | Daemon crash / VM reclaim / WS disconnect | §C1 |
| 2 | Statistical artefacts that look like signal | §C2 |
| 3 | Battery vs live disagree fundamentally | §C3 |
| 4 | Frozen-model calibration drift | §C4 |
| 5 | Freeze passes "too well" (over-fitting risk) | §C5 |
| 6 | Operator drift / bypass abuse / decision fatigue | §C6 |
| 7 | Indian market calendar anomalies | §C7 |
| 8 | Black swan / regime shock contamination | §C8 |
| 9 | Backtester isolation guard fires | →`backtester_vm_runbook.md` §1 |
| 10 | Capital-add temptation | →`FREEZE_v2.1.md` §Capital-add lock |
| 11 | Trade count too low for original gates | →`FREEZE_v2.1_revision.md` |

---

## C1. Silent operational failures (HIGH probability)

The trader VM is a 1-OCPU instance running for 21 continuous days. The
verdict flagged daemon crashes that the watcher misses, alert pipeline
failures, disk fill, OCI free-tier reclaim, WS reconnect loops.

### C1.a Daemon down for > 5 minutes during market hours

**Trigger:** `logs/health.json` is older than 90 seconds AND it is
between 09:15–15:30 IST on a trading day. The heartbeat email
(`tools/send_heartbeat.py`) did not arrive at the expected time, OR
`docker compose ps` shows the container as restarting/exited.

**Wrong move:** assume "it'll restart on its own."

**Right sequence:**

1. SSH in: `ssh ubuntu@<trader_ip>`.
2. `cd ~/trading-agent && docker compose logs --tail=200 trader`.
3. If a stack trace is present, capture it to `logs/incidents/`.
4. Restart: `docker compose restart trader`.
5. Verify health: `curl -s http://localhost:8080/health || true`.
6. If it crashes again within 60 s: stop the container, file an
   incident note in `docs/incidents/YYYY-MM-DD_daemon_crash.md`, and
   leave it stopped until you root-cause. **Do not loop-restart a
   crashing daemon during market hours** — the partial state
   contaminates the experiment.
7. Counted as a freeze observation (uptime % impact), NOT a freeze
   bypass (the fix is operational).

### C1.b Alert pipeline silently broken

**Trigger:** you didn't receive the 09:10 IST heartbeat AND
`logs/diagnostics/eod_YYYY-MM-DD.md` exists locally (the daemon was up
yesterday). The trader is alive but alerts aren't reaching you.

**Right sequence:**

1. `cat ~/trading-agent/logs/alerts/*.log | tail -100` — look for
   recent SMTP / Resend errors.
2. Manually trigger a test alert from the host:
   ```bash
   cd ~/trading-agent && python -c \
     "from packages.monitoring.alerts import AlertManager; \
      import yaml; \
      cfg = yaml.safe_load(open('config.yaml')); \
      AlertManager(cfg).send('TEST: heartbeat verify', 'manual check', level='info')"
   ```
3. If the test alert arrives, the daemon's alerter is the issue —
   restart the daemon.
4. If the test alert doesn't arrive, the credential / SMTP / Resend
   path is broken — check `.env`, key rotation, rate limits.
5. Counted as operational, not a bypass.

### C1.c Disk filling on trader VM

**Trigger:** weekly `df -h` shows root volume > 75 % full. With
`signal_audit_*.csv` (~1 MB / market day), `trades.csv`, audit
checkpoints (~16 × 5 KB / day), and daemon logs, growth is real over
21 days.

**Right sequence:**

1. `du -sh ~/trading-agent/{logs,data,models}` to confirm what's
   growing.
2. **Do not delete `data/trading_agent.db`** — it is the canonical
   trade history. The verdict's §3 (statistical-significance) depends
   on it.
3. Compress old per-day audit logs: `gzip logs/diagnostics/eod_2026-05-*.md`
   leaving only the current week uncompressed.
4. Rotate daemon logs: `truncate -s 0 logs/agent.log` AFTER copying
   the last 24 h to `logs/agent_archive_YYYY-MM-DD.log`.
5. Counted as operational.

### C1.d OCI free-tier reclaim of trader VM

**Trigger:** trader VM unreachable, OCI console confirms terminated.
This is **catastrophic for the freeze window** — the experiment data
is gone unless snapshotted.

**Pre-commitment (DO NOW):** the `data/trading_agent.db` and
`logs/diagnostics/` must be backed up to object storage **once per
trading day, at 16:30 IST**. Schedule via cron on the VM itself, not
manually. The backup target should be a tenant outside the at-risk
free-tier instance.

**Recovery sequence (if it happens despite snapshots):**

1. Provision replacement VM via OCI console.
2. Bootstrap with `tools/cloud/bootstrap_trader.sh <new_ip>` (or
   equivalent — confirm script exists; if not, create it analogous to
   `bootstrap_backtester.sh`).
3. Restore `data/trading_agent.db` from latest snapshot.
4. Restart daemon.
5. Mark all market minutes between reclaim and restore as
   `CONTAMINATED` in `logs/contaminated_days.csv`.
6. Reclaim of the trader VM **automatically extends the freeze by the
   number of trading days lost**. Original 2026-06-08 decision date
   slides accordingly.

---

## C2. Statistical artefacts that look like signal (MEDIUM probability)

The verdict flagged five distortion patterns. The unified response:
extend the EOD diagnostic to compute the metrics that expose each
pattern, then read them weekly. **The diagnostic extension is allowed
under §What is NOT frozen (observability).**

| Pattern | Metric the EOD diagnostic must produce |
|---|---|
| One huge trade distorts PF | **PF excluding the max-PnL trade per strategy** |
| Sector concentration | **Per-supersector PF and trade count** |
| Time-of-day clustering | **Entry-time histogram per strategy** |
| Survivorship in scanner | **Audit `core/stock_scanner.py` once for look-ahead** |
| Win/loss streak Markov | **Bootstrap PF lower-95-CI** (so streaks don't drive verdict) |

**Decision rule:**

- If a strategy's PF is > 1.0 but PF-excluding-max-trade < 0.8, the
  strategy's "edge" is one lucky trade. Treat verdict as INSUFFICIENT,
  not KEEP/SCALE.
- If per-supersector PF varies by > 50 % across supersectors with
  N ≥ 5 each, the edge is sector-dependent. Document but do not
  unfreeze.
- If 70 %+ of winning trades fire in a 90-minute time window, the
  "edge" is a time-of-day artefact. Document, watch for it during the
  freeze, decide at June 8 whether to add a time-of-day filter
  post-freeze.

All four metrics are added to the EOD diagnostic in this same
commit (see `changes_done_2026-05-19.md`).

---

## C3. Battery vs live disagree fundamentally (MEDIUM probability)

See `FREEZE_v2.1.md` §Disagreement (added 2026-05-19). The
disagreement-handling rules are pinned there, not here, because they
modify the exit criteria interpretation. This file just points there.

The single additional commitment:

- **`logs/drift/weekly_variance.csv` is produced every Friday EOD**,
  one row per strategy. Schema in `freeze_observability_extensions.md`
  §Weekly variance row.
- Three consecutive Fridays of > 30 % divergence in the same direction
  means the code-parity bug hypothesis (battery PF > live PF) or the
  battery-fragility hypothesis (adjacent battery runs disagree) is
  active. Open an investigation, tag commit `freeze-bypass:` if any
  frozen file needs to change.

---

## C4. Frozen-model calibration drift (LOW-MEDIUM probability)

The XGBoost model artefact (`models/xgboost_model.pkl`) was calibrated
on data through ~2026-05-12. By 2026-06-08 the most recent 27 days of
data have not been seen by the model. If regime shifted, the model's
predicted probabilities decalibrate.

**Pre-commitment:**

- Log predicted probability per XGBoost trade and the realised
  outcome. The trade record schema already has `predicted_proba`
  (verify in `data/trading_agent.db` — if missing, add as
  observability). All freeze-safe.
- At week 2 and week 3, run a calibration check (a tools script
  TBD — out of scope for tonight, will ship later in the freeze):
  for each predicted-probability bucket (0.50–0.55, 0.55–0.60, …),
  compute realised win-rate. If predicted vs realised diverges by
  > 10 percentage points in any bucket with N ≥ 5, the model is
  stale.

**Decision rule:**

- If calibration is healthy AND xgboost strategy PF < 1.0: the "no
  edge" conclusion for xgboost is real.
- If calibration is decalibrated AND xgboost strategy PF < 1.0: the
  conclusion is provisional. The model needs a retrain post-freeze
  before any verdict.
- If calibration is decalibrated AND xgboost PF > 1.0: the strategy
  appears to work despite stale calibration, which is statistical
  noise. Treat verdict as INSUFFICIENT.

**Crucially:** decalibration is NOT a reason to retrain mid-freeze.
The model freeze is part of the experiment contract.

---

## C5. Freeze passes "too well" (LOW probability, DANGEROUS)

**Trigger:** by 2026-06-08, portfolio PF is > 2.0 across ≥ 100 trades
with strong Kelly across multiple strategies. The instinct will be to
declare victory and push to live with real money.

**Pre-commitment — what "passes too well" actually means:**

1. The freeze passing on a single window means **eligible for Stage 2.1
   ONLY**, not eligible for full live deployment.
2. Stage 2.1 is **Rs 2,000–5,000 of real money, 10 trades, parity test
   only**. The success criterion is "live fills agree with paper fills
   within ±0.15 % slippage" — NOT "we made profit."
3. After Stage 2.1, a new freeze window of 2 weeks at the Stage-2.1
   capital level is required before any scale-up.
4. **No capital scale-up to Rs 30k+ on the strength of a single 21-day
   PF estimate.** The verdict explicitly flagged this as the
   genuinely-dangerous outcome.

The reason the strong-pass result is dangerous: a 3-week window with
PF 2.0 on 100 trades has ~30 % probability of being statistical noise
overlaid on a friendly regime. The bootstrap lower-CI is what matters;
report it.

---

## C6. Operator drift / decision fatigue / bypass abuse (HIGH probability)

The verdict flagged this as "probably the single most underrated
risk." Three failure modes:

1. Daily review skipped after Day 9.
2. Friday review becomes ritual (table filled, not interpreted).
3. `freeze-bypass:` first justified, second "basically the same",
   by bypass five contract is meaningless.

**Pre-commitments:**

1. **Calendar reminders.** Set up daily 16:15 IST and weekly Friday
   17:00 IST reminders for the EOD review and Friday weekly review.
   These are meetings with yourself.
2. **Bypass cap.** Already documented in `FREEZE_v2.1.md` §Bypass —
   3 total per freeze cycle.
3. **AI assistant boundary.** Every chat session that touches the
   trader begins with `FREEZE_v2.1.md` in context. The assistant
   respects the contract when made visible.
4. **Ritual-vs-decision check.** End of week 2 (Fri 2026-05-29): the
   operator answers in writing "what did I learn this week?" If the
   answer is "nothing new" two weeks in a row, the freeze is producing
   ritual without decision-making. That itself is a decision point —
   consider activating Branch 1 (revision) earlier than the trigger
   threshold suggests.

---

## C7. Indian market calendar anomalies (LOW probability, predictable)

NSE / BSE holidays and special-event days bias the data. The
21-day window has known gotchas worth pre-decoding:

| Date | Event | Impact |
|---|---|---|
| (check NSE calendar) | Holidays | reduces trade count |
| Last Thursday of month | F&O expiry | stocks misbehave; momentum strategies misfire |
| Quarterly results dates | Earnings | gap moves blow out SLs on overnight |
| F&O ban list inclusion | Stock-specific volatility | scanner may pick up these symbols |

**Pre-commitment:**

- Maintain `logs/calendar_events.csv` with one row per anomaly date.
  Schema: `date, event_type, expected_impact, notes`.
- When reviewing weekly P&L, attribute bad days to calendar events
  FIRST before concluding "strategy is broken."
- Do NOT add a calendar filter to the strategy code during the freeze
  — that's a behaviour change. Document the impact, decide post-freeze.

---

## C8. Black swan / regime shock (LOW probability, but pre-decidable)

**Trigger:** any day with India VIX > 25 OR NIFTY move > 2.5 %. Real
shock events (RBI surprise, geopolitical, circuit-breakers) happen.

**Pre-commitment — the contamination rule:**

- Any day matching the trigger above is marked `CONTAMINATED` in
  `logs/contaminated_days.csv`.
- The EOD diagnostic computes BOTH inclusive and exclusive PFs.
- Phase A exit-criteria comparison uses **exclusive** PF for the
  edge claim, **inclusive** PF for the risk-tolerance check.

This is statistically defensible (you're not cherry-picking; the
exclusion criterion is pre-stated) and prevents an unrelated market
event from killing the experiment OR fooling it into looking
profitable.

The trader's existing circuit breakers (daily 3 % loss, max VIX 25,
drawdown 20 %) handle the *operational* response. The contamination
flag handles the *statistical* response.

---

## Cross-references

- `docs/FREEZE_v2.1.md` — the contract itself (frozen settings, exit
  criteria, kill criterion, bypass cap, capital-add lock,
  disagreement rules, operator commitments)
- `docs/FREEZE_v2.1_revision.md` — Branch 1 contingency (battery-primary
  if trade count is too low)
- `docs/backtester_vm_runbook.md` — backtester operational scenarios
- `docs/postmortem_phase_a_template.md` — Branch C (freeze fails)
  template; required to fill before any "let me try one more thing"
- `docs/freeze_observability_extensions.md` — what the daily / weekly
  / EOD diagnostic must produce, including the contamination handling

---

*Author: Trading Agent dev (Subhanda) + Claude.*
*Drafted: 2026-05-19. All pre-commitments made in cold blood, before
the failure modes activate.*
