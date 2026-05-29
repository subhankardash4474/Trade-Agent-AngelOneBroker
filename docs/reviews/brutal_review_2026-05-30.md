# Brutal review — 2026-05-30

**Status:** First-pass adversarial review run at 2026-05-30 ~00:48 IST,
the night after the Friday `friday_review_2026-05-29.md` landed and the
freeze-v2.1 exit ladder was pre-committed. Read-only desk note; no
config / code / DB / log mutations were performed. Produced by the
`.cursor/skills/brutal-review/` persona at the operator's invocation
("play adviser and tear it apart").

**Window reviewed:** 2026-05-12 → 2026-05-29 (13 trading days, last
real trade 2026-05-26).

**Audience:** operator + adviser preparing for the slot-#4 readout
(Sat 2026-05-30 ~05–08 IST), the H3-prime forensic (Wed 2026-06-03),
the Fri 2026-06-05 option-A/B/C decision, and the wind-down
trigger on 2026-06-08.

**Persona contract:** unsentimental, evidence-or-silence, business
logic first, rank findings by ₹ impact. See
`.cursor/skills/brutal-review/SKILL.md` for the full rules of
engagement and the mandated output structure.

**Companion docs (read these first if you have time for one only):**

* [`friday_review_2026-05-29.md`](friday_review_2026-05-29.md) —
  the operator's own Friday review; §10 is the diagnostic-sprint
  read-out and §11 is the freeze-exit pre-commitment summary.
* [`freeze_v2.1_exit_criteria_2026-06-05.md`](freeze_v2.1_exit_criteria_2026-06-05.md) —
  the operating contract for 2026-05-29 → 2026-06-08. T1 / T2 / T3
  thresholds, the three Friday options, audit-only reclassification.
* [`wind_down_criteria_2026-06-05.md`](wind_down_criteria_2026-06-05.md) —
  the short, locked, paste-and-apply operational sheet for the
  2026-06-05 verdict meeting.

This brutal review is **complementary to**, not a replacement for,
the Friday review. The Friday review documents what the team
believes; this review verifies that belief against raw evidence
(trades.csv, signal-audit CSVs including rejected rows, daemon /
trading-agent logs, postmortems, EOD diagnostics, the audit
checkpoint stream) and flags where the data does not support the
current framing.

---

## Verdict (one line)

**RED** — engine has been structurally non-trading for 3 sessions,
the only live trades it managed in week 2 entered an average of 31
minutes late at the local high and stopped out, and the
operator-authored exit contract is the only thing standing between
the project and continued capital decay.

---

## Bottom-line numbers (independently derived, not from checkpoint)

Sources: `logs/trades.csv` rows 2–32 (real entries only; ZZTEST rows
visible in `logs/trades_pre_bug_o_purge_2026-05-29.csv` excluded),
`logs/signal_audit_2026-05-{27,28,29}.csv`,
`logs/diagnostics/eod_2026-05-29.md`, `logs/postmortem/2026-05-26.md`,
`logs/audit/2026-05-29/checkpoint_1601.json`.

| Metric | Value | Source / note |
|---|---|---|
| Realised P&L (5/12 → 5/26, 30 closed trades) | **-₹1,765** gross sum of `pnl` column | `logs/trades.csv` rows 2-32. Cumulative since deployment, per the daemon's own ledger, is **-₹1,212.26** (`checkpoint_1601.json:120`) after netting earlier recoveries. |
| Win count / loss count / WR | 11W / 19L / **36.7%** | Same source. |
| Avg win / avg loss / R-multiple | ₹68 / ₹-128 / **0.53** | Same source. Sub-1.5 R with sub-55% WR ⇒ negative-EV before fees. |
| Avg MFE on the last 3 closed trades (5/26) | ₹+136 vs realised ₹-151 ⇒ leakage **≈ ₹287 / trade** | `logs/postmortem/2026-05-26.md:6` (`Avg MFE capture: -131.5%`). |
| Slippage actual vs assumed | **Insufficient evidence** | `data/slippage_log.csv` is 262 bytes, last touched 2026-05-13. No live slippage measurement since the freeze started. |
| Max drawdown in window | ~1.6% (halt at 20%) | `logs/trading_agent_2026-05-29.log:24951` EOD alert. DD is small only because capital is paused. |
| Cycles / signals scored / signals accepted / trades placed, 5/27 | many cycles / 30 signals / **0 accepted** / 0 trades | `logs/signal_audit_2026-05-27.csv` — all 30 rows REJECTED. |
| Same, 5/28 | many cycles / 6 signals / **0 accepted** / 0 trades | `logs/signal_audit_2026-05-28.csv` — all 6 rows REJECTED. |
| Same, 5/29 | 8–10 cycles / 42 signals / **0 accepted** / 0 trades | `logs/signal_audit_2026-05-29.csv` — all 42 rows REJECTED; checkpoint_1601 confirms `closed_trades_today=0`. |
| DB vs CSV reconciliation | `data/trading_agent.db` mtime is **2026-05-19 15:43**, yet `logs/trades.csv` carries entries through 2026-05-26. Per-strategy verdict in `eod_2026-05-29.md` shows only the 5/26 xgboost triplet (`Trades analyzed: 3`). | DB-backed analytics appear to be missing rsi_momentum / supertrend_follow rows from 5/21 and 5/22. CSV is the trader's primary persistence, so this is an analytics blindspot rather than a money loss. |

---

## Top suspicions, ranked by ₹ impact

### 1. Strategy stack is one-sided in a bear regime; `allow_shorts:false` makes the live engine inert

**Evidence.** `logs/signal_audit_2026-05-29.csv` — every row is `SELL`,
regime tagged `bear_low_vol` or `bear_high_vol`. 16/42 rejected for
`opening_lockout`, **26/42 rejected for `allow_shorts:false`**.
Identical pattern on 5/28 (6/6) and 5/27 (20/30 by `allow_shorts:false`
after the opening lockout clears). `config.yaml:319` confirms
`allow_shorts: false`:

```yaml
risk:
  # ── Risk-policy short veto (2026-05-25) ───────────────────────────
  # Higher-level kill switch on the SHORT side. When `false`, every
  # ...
  allow_shorts: false
```

**Business interpretation.** In a bear tape the live strategies
(`rsi_momentum`, `supertrend_follow`) produce 100% SELL; the long-only
veto then strips 100% of them. The agent is not "protecting capital" —
it is **structurally absent from the market** until either (a) regime
flips bull, or (b) `mean_reversion` fires a BUY (which is also
suspended on the trader VM per the runtime-state restore at
`logs/daemon_2026-05-29.log:82`, showing the previous session's
runtime state had `suspended=['mean_reversion']`). The 232-stock
backtests show 229–266 trades over 60d because the backtest window
straddled bull periods; **the live tape over 2026-05-12 → 2026-05-29
has not.** This is the simplest explanation for "no edge transfers" in
`friday_review_2026-05-29.md §10.2` — there is no edge to transfer
when the engine fires zero entries.

**Estimated ₹ impact / day.** Not directly costing money (no trades =
no losses) but it is the cause of the "no edge" verdict in the Friday
review, which was reached on a 13-day window where 3 of the last 4
sessions placed zero trades. The backtest / live mismatch is being
interpreted as "no edge" when it is partly "no exposure".

**Recommended action.** Before T2 (slot-#4 V15 PF) is treated as
definitive, document the **regime mix of the live window vs the
backtest window**. If live was ~100% bear and backtest was ~60% bear,
the V15 transfer-failure verdict in `friday_review_2026-05-29.md
§10.2` is partly an artefact of `allow_shorts:false × regime=bear`.
The wind-down decision on 2026-06-08 should be made knowing this.
Either lift the short veto under tighter controls or accept that the
engine is a long-only-bull strategy and stop running it during
structural bears.

### 2. Live entry lag is 22–86 *minutes*, not the seconds the freeze contract assumes

**Evidence.** `logs/postmortem/2026-05-26.md:6,18,30,42`:

```
Avg MFE capture: -131.5%
Entry lag: median 31.5 min, max 86.7 min, [LATE-ENTRY] flags: 3/3 (threshold 5 min)
```

* HFCL: first signal 09:21, entered 09:52 (31.5 min, 4 signals seen /
  3 rejected first).
* TATAINVEST: first signal 09:26, entered 09:48 (22 min, 4 prior
  rejects).
* TATACHEM: first signal 09:22, entered 10:48 (**86.7 min, 17 prior
  rejects**).

**Business interpretation.** The strategy emits the signal close to
the move, but `opening_lockout_minutes: 15` (`config.yaml:455`) plus
the threshold and rr filters reject the early emissions. The strategy
does **not** queue the signal for re-firing post-lockout, so by the
time conditions re-trigger, the move is exhausted. All 3 entries on
5/26 landed at or above the local MFE high (capture −260% / +0.7% /
−136%) and stopped out — classic "buy the breakout that already
happened" failure. **This is the dominant loss driver for the trades
that actually fired**, not the model and not the strategy mix.

**Estimated ₹ impact / day.** On the 3 trades from 5/26 alone, leakage
MFE → realised was **₹+814** (`logs/postmortem/2026-05-26.md:7`
"Money on table"). If 3 trades/day is the steady state (it isn't right
now, but it is the V4-on-232-stocks expectation), this is roughly
**₹250–800 / trading day** that an on-time entry would have either
captured or avoided losing.

**Recommended action.** The freeze-v2.1 T1 forensic
(`freeze_v2.1_exit_criteria_2026-06-05.md` §0.1, deliverable
2026-06-03) is sized for `broker_fill_ts − strategy_emit_ts` in
**seconds**, with bucket boundaries 30 s / 120 s. The 5/26 postmortem
already shows the signal → fill lag is in **minutes** dominated by
*internal rejection cycles*, not broker. As scoped today, T1 will
likely measure ~1–5 s of broker round-trip and trigger the wind-down
branch — which would be the wrong call. Re-scope T1 to bucket **both**:

1. `last_signal_ts → fill_ts` (current scope, broker round-trip), and
2. `first_signal_ts → fill_ts` (the postmortem's "entry lag" definition,
   which captures the rejection-cycle dwell time).

Otherwise the team risks winding down a project whose actual
diagnosable bug is "we reject early signals and don't re-queue them".

### 3. Three of last four sessions had zero trades but the audit checkpoint says GREEN

**Evidence.** `logs/audit/2026-05-29/checkpoint_1556.md:3-4` declares
**Verdict: GREEN** because "Errors: 0 · Warnings: 1". The same
checkpoint shows `cycles=10`, `avg directional votes=4.3`, `ensemble
acts=11` — yet `trades_today=0` and Day P&L `₹+0.00`. Three sessions
running, zero trades fired, zero acceptance, and the operational
health channel reads GREEN.

```json
// logs/audit/2026-05-29/checkpoint_1601.json (excerpt)
"trades": {
  "closed_count": 0,
  ...
},
"day_pnl": {
  "closed_trades_today": 0,
  ...
  "win_rate": 0.0,
```

**Business interpretation.** The GREEN/YELLOW/RED gate is wired to
error-counts and uptime, not to **strategy effectiveness**. A daemon
that scores 42 signals and rejects every single one is not "healthy" —
it is the canary for a regime/strategy mismatch (Finding 1). The
current gate cannot detect this mode because no exception is raised.

**Estimated ₹ impact / day.** Zero direct, but the gate's silence is
*what allowed* the operator to spend 3 days believing "no losses = OK"
rather than "no activity = engine is hung in a long-only veto loop".
Cost ≈ 3 days of decision delay × cost-burn (`₹125/trading-day` per
checkpoint `self_sufficiency` block) = **~₹375 of unrecovered cost
burn**, plus the opportunity cost of running the H3 forensic 3 days
late.

**Recommended action.** Add a checkpoint rule:
`accepted_signals_today / scored_signals_today < 5%` ⇒ YELLOW;
`< 1%` for 3 consecutive sessions ⇒ RED, *regardless* of error count.
RED also auto-fires the brutal-review skill — which is the point of
the gate. ~5 lines of code in the checkpoint generator.

### 4. Stale persisted state — `self_sufficiency.json` and `health.json` disagree with the checkpoint

**Evidence.**

```json
// data/self_sufficiency.json
{
  "deployed_on": "2026-05-14",
  "cumulative_realised_inr": 0.0,
  "last_update": "2026-05-14T13:45:13.504290+05:30"
}
```

vs `logs/audit/2026-05-29/checkpoint_1601.json:120`:
`"cumulative_realised_inr": -1212.26`, and `logs/health.json:12`
saying `"cash": 100000.0, "state": "idle_off_hours"` while the same
checkpoint says `cash ₹120,990`. Three different sources, three
different numbers.

**Business interpretation.** `self_sufficiency.json` has not been
written for **15 days** of live operation, despite the daemon
producing fresh figures every hour. Either (a) the writer path is
dead, (b) the file is no longer the source of truth and the codepath
that *thinks* it's writing it is silently failing, or (c) the
persisted file is the trader-VM file and the checkpoint reads
in-memory — meaning a restart will reset cumulative-realised to 0 and
the self-sufficiency tracking starts over silently.

**Estimated ₹ impact / day.** Not direct money, but a wind-down
decision on 2026-06-08 will hinge on `cumulative realised vs
cost-burn`; if the persisted file is the post-restart source of truth,
the verdict could be made against `cumulative=0` and miss the
**−₹1,212** of actual deployment cost. Insufficient evidence to
quantify precisely without reading the persistence writer.

**Recommended action.** Before the 2026-06-05 verdict meeting, add one
assertion to the EOD: `self_sufficiency.json.cumulative_realised_inr
== checkpoint.cumulative_realised_inr` (within ±1 paisa). Fail loud if
they diverge.

### 5. Bug O is not actually fixed — only the trades.csv leak was cleaned, not the leak path

**Evidence.** `logs/trades_pre_bug_o_purge_2026-05-29.csv:33-36` shows
4 `ZZTEST/ZZTEST2` `manual_test` rows at 18:47 and 18:58, both AFTER
the supposed Bug O purge at 18:47 (the operator ran the test suite a
second time, the test wrote to prod CSV again):

```
ZZTEST,SELL,100.0,95.5,10,2026-05-29T18:47:36.824810+05:30,...,manual_test,1.05
ZZTEST2,SELL,100.0,95.5,10,2026-05-29T18:47:36.895892+05:30,...,manual_test,1.05
ZZTEST,SELL,100.0,95.5,10,2026-05-29T18:58:07.715280+05:30,...,manual_test,1.05
ZZTEST2,SELL,100.0,95.5,10,2026-05-29T18:58:07.779616+05:30,...,manual_test,1.05
```

The current `logs/trades.csv` has these stripped (a second purge ran),
but `logs/daemon_2026-05-29.log:82-200` is **still polluted with
pytest output**:

```
[RUNTIME-PERSIST] CORRUPT snapshot at C:\...\Temp\pytest-of-subhanda\pytest-137\test_load_malformed_json_retur0\runtime_state.json: ...
[mean_reversion] MEAN-REVERTED for SBIN ...
[STRATEGY-BREAKER] mr suspended for the day (3 consecutive losses, ...)
[rsi_momentum] BUY signal for TEST | RSI=31.8 reversal from oversold
Opened SELL position: 10 x RELIANCE @ 2500.00 (commission: 16.00)
```

**Business interpretation.** Tests can still write into prod paths
because the prod logger and the prod trades-writer don't isolate
per-process. The next operator who reads `daemon_2026-05-29.log` for
"what did the trader do today" will see hundreds of `BUY signal for
TEST | RSI=31.8` entries and either get misled or have to filter them
out manually. The findings_log claim that Bug O is RCA'd hides that
**only the symptom** (4 ZZTEST rows in trades.csv) was deleted, not
the cause. (Note: the polluted `daemon_2026-05-29.log` is the
*operator's local laptop* repo, not the trader-VM file, so the
production impact is bounded — but the leak class is still active.)

**Estimated ₹ impact / day.** Zero direct ₹, but this is the *exact*
class of leak that earlier consumed audit-only baseline-shifting
budget (`freeze_v2.1_exit_criteria_2026-06-05.md` §3, class
"audit-only, baseline-shifting"). A future "is the engine OK?" answer
based on a polluted log is a P0-in-waiting.

**Recommended action.** In `conftest.py`, monkey-patch the prod CSV
writer and the prod logger to a test-isolated path. Assert in CI that
no test run produces a `ZZTEST*` row in any path matching
`logs/trades*.csv`. ~30 min work.

### 6. Daemon supervisor restart loop is firing 20+ times per minute post-market

**Evidence.** `logs/trading_agent_2026-05-29.log` lines 24951–26118
(and continuing) — between 15:21 and 15:53 the daemon prints the same
EOD alert and exits cleanly **~20 times**, each cycle ~75–80 seconds:

```
2026-05-29 15:30:06 | INFO | Agent exited cleanly
2026-05-29 15:30:06 | INFO | Agent self-exited at 15:30:06 IST -- skipping restart loop and sleeping until the next market window.
2026-05-29 15:31:20 | INFO | ALERT: EOD Summary - EOD Report 2026-05-29 | Day PnL: Rs +0.00 | ...
2026-05-29 15:31:37 | INFO | Agent exited cleanly
... (repeats)
```

**Business interpretation.** The supervisor (docker / systemd) is
restarting the daemon and the daemon shuts itself down because the
intraday cutoff (15:15) has passed. The supervisor then restarts it
again. The loop is wasteful CPU and clutters the log with N copies of
"Day PnL: Rs +0.00". Not a strategy bug, but it means the audit
anomaly counter ("warnings: 1") is undercounting — the 20+ shutdowns
are tagged INFO and slip past the GREEN/YELLOW/RED gate.

**Estimated ₹ impact / day.** Zero, but ~₹10 of compute / day if you
care, and it makes the daemon log near-useless for any 15:15–15:55
forensic.

**Recommended action.** Either disable the supervisor's
restart-on-clean-exit between 15:16 and next-market-open, or have the
agent sleep-in-place until next-market-open rather than exit. ~5-line
fix.

---

## Things the daemon is telling itself that are not true

(Places where the checkpoint, EOD, or operator narrative disagrees
with raw evidence. Both sides cited.)

* **Checkpoint says `Verdict: GREEN`, evidence says RED.**
  `logs/audit/2026-05-29/checkpoint_1556.md:4` declares GREEN.
  Independent evidence: zero trades for 3 sessions, zero accepted
  signals, 100% of 42 signals on 5/29 vetoed by `allow_shorts:false`.
  GREEN reflects only error-count; it does not reflect strategy
  viability (Finding 3).
* **EOD profit_diagnostic shows "Trades analyzed: 3"** for last 7d
  (`logs/diagnostics/eod_2026-05-29.md:2`), but `logs/trades.csv` rows
  26-32 list 7 trades after 2026-05-21 (5 of which are within the
  7-day window). The DB-backed analytics are dropping rsi_momentum /
  supertrend_follow rows from the per-strategy verdict and reporting
  only the 5/26 xgboost triplet. The per-strategy verdict table in
  `friday_review_2026-05-29.md` is therefore biased toward "xgboost is
  the only thing that lost" when in fact the supertrend_follow
  stop-outs from 5/13–5/14 (-₹693 in CSV) are the bigger live bleeder.
* **`self_sufficiency.json` says `cumulative=0`, checkpoint says
  `-1212.26`** (Finding 4). The agent is reporting two contradictory
  numbers for "are we paying for ourselves" and the wind-down trigger
  may read the wrong one.
* **Bug O is logged as "audit-only refinement, RCA in findings_log
  §24"** (`friday_review_2026-05-29.md:736-738`), implying
  remediation. The leak path is still active — proof in the same-night
  second leak at 18:58 (Finding 5).

---

## Things that look fine

(Brief, used to prove the sweep was actually done.)

* The pre-committed exit ladder
  (`freeze_v2.1_exit_criteria_2026-06-05.md` +
  `wind_down_criteria_2026-06-05.md`) is structurally honest: dated
  thresholds, explicit option-A wind-down recommendation,
  anti-temptation list. This is the right way to operate. The only
  complaint is the T1 scoping (Finding 2).
* The CSV ↔ pre-purge CSV diff for Bug O is correct: 4 ZZTEST rows
  removed, 30 real rows preserved byte-for-byte. The data-side cleanup
  was done right.
* Risk floors (`max_drawdown_pct: 10.0` daily, 20% portfolio halt) are
  conservative and well below current 1.6% DD. The capital pause is
  doing what it should.
* Backtest framework discipline: V16 (no filters) produced -40% /
  -67% returns and was kept in the variant sweep as a control. That's
  how a research stack should be operated.

---

## What I refused to conclude (insufficient evidence)

* **True broker fill-lag.** No `logs/orders_*.csv` or
  `logs/trader_log_*.log` in the local repo (those live on the trader
  VM). The 5/26 postmortem's "entry lag" is signal-to-fill *including*
  internal rejection time; the broker-only round-trip is unknown until
  T1 is run.
* **DB vs CSV strategy-level reconciliation.** Cannot query SQLite
  without a runtime; the file-mtime evidence + profit_diagnostic's
  "Trades analyzed: 3" strongly suggests rsi_momentum and
  supertrend_follow trades from 5/21+ are missing from the DB, but I
  cannot confirm without
  `sqlite3 data/trading_agent.db "SELECT * FROM trades WHERE entry_time > '2026-05-19'"`.
* **Whether `mean_reversion` is currently suspended on the trader VM.**
  `logs/daemon_2026-05-29.log:82` shows a *restore* of
  `suspended=['mean_reversion']` from a prior runtime state, but that
  may be the local test fixture's state, not the trader VM's. Confirm
  via the trader-VM `runtime_state.json`.
* **AUC=0.49 retrain — whether the backtester pkl swap was actually
  applied.** I see the operator override + audit trail in
  `friday_review_2026-05-29.md §10.5`, but cannot diff the on-disk pkl
  hash without exec access on the backtester VM.

---

## Next 24h checklist (operator actions, ranked)

1. **(P0, ~5 min)** Add a checkpoint rule:
   `accepted_signals_today / scored_signals_today < 5%` flips Verdict
   to YELLOW; `< 1%` for 3 consecutive sessions flips to RED and fires
   brutal-review. This is the highest-leverage observability fix in
   the project right now (closes Finding 3).
2. **(P0, ~15 min, before slot-#4 lands)** Re-scope T1 from
   `broker_fill_ts − strategy_emit_ts` only to **both** that **and**
   `first_signal_ts → fill_ts`. Document in
   `freeze_v2.1_exit_criteria_2026-06-05.md` as a v1.1 amendment so
   the Wed 2026-06-03 verdict reads correctly. Otherwise the
   wind-down will be triggered on a metric that doesn't measure the
   bug (closes Finding 2).
3. **(P0, ~30 min)** Reconcile DB and CSV for trades 2026-05-21
   through 2026-05-26. Pick one as the source of truth for the
   wind-down P&L calculation and document the choice. Today the
   choice is implicit and the two numbers differ.
4. **(P1, ~30 min)** Plug the test → prod leak in `conftest.py`;
   assert no `ZZTEST*` row can land in `logs/trades*.csv` from any
   test run. Bug O is not actually fixed (closes Finding 5).
5. **(P1, ~30 min)** Document the **live regime mix** during the
   trading window 2026-05-12 → 2026-05-29 (rough %: bear_high /
   bear_low / sideways / bull) and place it next to the V15
   transfer-test result. If the live window is ~100% bear and the
   backtest was ~60% bear, the "no edge" verdict needs a footnote
   that says "no edge **on a 13-day single-regime sample**", not "no
   edge" (closes Finding 1).
6. **(P1, ~5 lines of code)** Either delete the supervisor
   restart-on-clean-exit between 15:16 and next-market-open, or have
   the agent sleep-in-place. The 20+ restarts/min loop is making the
   trader log unreadable (closes Finding 6).
7. **(P2, post-decision)** Either kill `allow_shorts:false` under a
   hard daily-loss floor (since the strategies are clearly producing
   SELL signals in bear and the live tape has been almost entirely
   bear), or formally retire `rsi_momentum` / `supertrend_follow` as
   bull-only strategies and stop pretending the engine is
   regime-agnostic. The current configuration is a long-only-bull
   engine wearing a regime-aware-ensemble label.

---

## One-paragraph summary for the 2026-06-05 verdict meeting

The exit ladder is right, the brutal-honest documentation discipline
is exemplary, and the data-side conclusions in
`friday_review_2026-05-29.md §10` are defensible. **But the wind-down
decision on 2026-06-08 is at risk of being made on the wrong sample
and the wrong metric.** Findings 1 (live tape is structurally bear +
long-only veto = no exposure), 2 (entry lag is minutes, not seconds,
and T1's scope misses this), and 4 (cumulative-realised has three
contradictory sources) need to be closed before the verdict, not
after. Findings 3, 5, and 6 are cheap observability fixes whose
absence is the reason this review was needed in the first place.

---

## Cross-references

* `.cursor/skills/brutal-review/SKILL.md` — persona contract, mandated
  evidence sweep tiers, output format.
* `.cursor/skills/trading-audit/SKILL.md` — the *complementary* hourly
  audit skill (trusts the checkpoint, summarises curated state). This
  doc is its adversarial counterpart.
* `friday_review_2026-05-29.md` — the diagnostic-sprint Friday review;
  this doc is the night-after adversarial pass on its evidence base.
* `freeze_v2.1_exit_criteria_2026-06-05.md` — the operating contract
  this review's recommendations target.
* `wind_down_criteria_2026-06-05.md` — the locked verdict-meeting
  sheet.
* `logs/postmortem/2026-05-26.md` — the per-trade postmortem cited in
  Finding 2.
* `logs/audit/2026-05-29/checkpoint_1601.json` — the most recent
  checkpoint cited throughout.
