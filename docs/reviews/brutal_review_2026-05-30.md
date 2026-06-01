# Brutal review — 2026-05-30

**Status:** First-pass adversarial review run at 2026-05-30 ~00:48 IST,
the night after the Friday `friday_review_2026-05-29.md` landed and the
freeze-v2.1 exit ladder was pre-committed. Read-only desk note; no
config / code / DB / log mutations were performed. Produced by the
`.cursor/skills/brutal-review/` persona at the operator's invocation
("play adviser and tear it apart").

> **CHG-charges note (added 2026-06-01, historical-record footnote).**
> Every PF number in this review (V20 PF 0.41, V22 PF 0.28, V24 PF 0.21,
> V25 PF 0.23, V15 PF 0.77, etc.) was measured under the pre-CHG
> Zerodha-calibrated charges model. On 2026-06-01 the model was
> corrected to AngelOne's actual rates — see
> [`../findings/findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md)
> and per-variant adjustments in
> [`../findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md).
> The v3 delivery battery is the largest mover: V20 PF 0.41 → 0.21,
> V25 PF 0.23 → 0.05 (with 35 winners flipping to losers). The
> "evidence-complete RED" verdict of this review is **strengthened**
> by the correction — every "near 1.0" data point moves further from
> 1.0. The original numbers below stay as the as-of-2026-05-30
> historical record; corrected numbers are the authoritative set for
> any analysis dated 2026-06-01 or later.

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
* [`freeze_v2.1_exit_criteria_2026-06-05.md`](../freeze/freeze_v2.1_exit_criteria_2026-06-05.md) —
  the operating contract for 2026-05-29 → 2026-06-08. T1 / T2 / T3
  thresholds, the three Friday options, audit-only reclassification.
* [`wind_down_criteria_2026-06-05.md`](../freeze/wind_down_criteria_2026-06-05.md) —
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

---

## Session @ 11:07 IST

**BRUTAL REVIEW — 2026-05-30 (Session 2)**
Window reviewed: 2026-05-29 16:01 IST → 2026-05-30 10:10 IST
(the 18 hours since session 1; markets closed today, Saturday).
Persona: Expert algo trader + adviser. Verdict is unsentimental.

This session does NOT re-derive the 13-day window — session 1 above
already did that and the numbers haven't moved (no live trades since
2026-05-26). This session re-checks **what changed since 01:24 IST**
and is therefore tightly scoped to: (a) the v3 swing battery that
finished at 10:35 IST and the forensic verdict written at 10:42 IST,
(b) the local-laptop daemon that was started at 09:38 IST today and
is still running, (c) which of session 1's six findings the day's 13
commits actually closed.

---

### Verdict (one line)

**RED, escalating.** The v3 swing pivot (the project's single named
T3 hypothesis) cleared the bug-vs-edge test honestly and produced no
edge — but the verdict was computed on the **7–11% of signals that
survived `allow_shorts:false`**, so the wind-down trigger is about to
fire on a half-tested strategy; meanwhile the trader VM shipped two
new live bugs in the same morning the freeze surface was supposed to
be untouchable.

---

### Bottom-line numbers (independently derived, not from checkpoint)

Sources for the new numbers below:
`logs/backtests/battery_v3_swing_a5_180d_eff_20260530T050422/results/V*.json`,
`logs/daemon_2026-05-30.log`,
`data/self_sufficiency.json`, `logs/health.json`,
`docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md`,
`git log --since="2026-05-30 01:00"`.

| Metric | Value | Source / note |
|---|---|---|
| Live P&L delta since session 1 | **₹0** | Markets closed; daemon idle. Cumulative still -₹1,212.26 per checkpoint. |
| Avg win / avg loss / R / WR (unchanged) | ₹68 / -₹128 / 0.53 / 36.7% | `logs/trades.csv` rows 2-32 — no new rows since 2026-05-26. |
| **v3 swing battery — best variant** | V20 PF **0.41** / WR 20.0% / -₹1,137 | `results/V20_swing_pullback_only.json:62-66`, 55 trades over ~408 daily bars × 30 Nifty-30 stocks. |
| v3 swing battery — worst variant | V24 PF 0.21 / WR 10.9% / -₹1,499 | `results/V24_swing_combined_tight.json`. Tightening produced WORSE WR than V23 (loose), per `v3_phase_a5_forensic_2026-05-30.md:79-93`. Signature of rules fitting noise. |
| **v3 fill model verified honest** | `backtest.fill_mode: next_bar_open` in every V20-V24 config; engine path at `packages/research/backtest_ensemble.py:682` dispatches | `logs/backtests/.../configs/V20_swing_pullback_only.yaml:246`, `results/V20_*.json:16-19`. The "no edge" verdict is NOT an artefact of close-fill bias. |
| **v3 shorts-blocked rate (V20)** | **2,623 / 2,831 = 92.7%** of raw signals stripped by `risk.allow_shorts:false` | `results/V20_swing_pullback_only.json:80` `"shorts_blocked": 2623`. The 55 executed trades = the surviving **7.3%**. |
| v3 shorts-blocked rate (V22 combined) | **2,627 / 2,962 = 88.7%** stripped | `results/V22_swing_combined.json` gate_stats. PF=0.28 read on 11.3% of the strategy's natural signal set. |
| v3 shorts-blocked rate (V21 breakout-only) | 0 / 119 — all 0 | V21's `breakout_20d` rule is inherently long-side; not affected by the veto. PF 0.23 is a clean read of that rule alone. |
| **Trader runtime-persist** | **BROKEN** — `AttributeError("'TradingAgent' object has no attribute '_strategy_state'")` | `logs/daemon_2026-05-30.log:~1955` (2 hits this morning); direct evidence for session-1 Finding 4 hypothesis "writer path is dead". |
| **xgboost zombie signals** | **30 hits** in today's daemon log for **AAPL / MSFT** (US tickers not in any prod universe) | `logs/daemon_2026-05-30.log:641-...`, starting 09:38:49 IST — 36 minutes AFTER commit `c9d3936` ("retire xgboost_classifier"). |
| Supervisor restart loop today | **21 cycles** by 10:10 IST | `Select-String -Pattern "Agent exited cleanly\|Agent self-exited" \| Count`. Session-1 Finding 6 not closed; worse on a Saturday. |
| WebSocket reconnect spam today | dozens of `Reconnection failed: down` | `logs/daemon_2026-05-30.log` tail — broker WS being hammered on a market-closed Saturday. |
| self_sufficiency.json drift | **16 days stale**, `cumulative_realised_inr: 0.0`, `last_update: 2026-05-14` | `data/self_sufficiency.json`. Session-1 Finding 4 still wide open. |
| health.json drift | cash 100,000 vs checkpoint 120,990; ts 2026-05-29 18:32 | `logs/health.json:12`. Third disagreeing source for "current cash". |
| Commits since session 1 | **13** | `git log --since="2026-05-30 01:00"`. 7 v3 charter commits, 1 T1 verdict commit, 5 docs commits. **Zero commits addressing session-1 Findings 3, 4, 5, or 6.** |

---

### Top suspicions, ranked by ₹ impact

#### 1. The v3 "no edge" verdict is a verdict on the LONG-ONLY 7–11% of signals — the wind-down trigger is about to fire on half-tested evidence

**Evidence.** `results/V20_swing_pullback_only.json` gate_stats:

```
"total_signals":   2831,
"shorts_blocked":  2623,   // 92.7% of raw signals vetoed
"executed":          55,
```

`results/V22_swing_combined.json` gate_stats:
`shorts_blocked: 2627 / 2962 = 88.7%`. `results/V21_*.json`: 0 / 119
(Rule 2 is long-side by construction, unaffected). The forensic doc's
verdict, "the trend-pullback-with-RSI-cooled setup does not have edge
on this universe at this horizon"
(`docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md:117-121`), is
overstated by one degree. The correct verdict is **"the long-only arm
of trend-pullback does not have edge"**. The short arm — 88-93% of
the strategy's natural signal set — has not been tested.

**Business interpretation.** This is the same finding as session 1
Finding 1, but now the v3 forensic *reproduces* it inside a clean
backtest. The v3 swing was supposed to be the named, measurable T3
hypothesis whose failure triggers wind-down (`wind_down_criteria_2026-06-05.md`,
charter §6.5). Firing the wind-down on the long-only sub-set of v3's
signals would close the project on a strawman. The most expensive
mistake this project can still make is to call it dead before testing
the other half of the strategy.

**Estimated ₹ impact / day.** Not directly bleeding right now (capital
paused), but: if the short side of trend_pullback has edge and we
shut down without testing it, the opportunity cost is **the entire
remaining option-value of the project**. Bounded above by the
₹120k notional × ~10-15% annual swing-strategy expectation if real
= **₹12-18k/year** of foregone PnL on a wrong-shutdown.

**Recommended action.** Before 2026-06-05 verdict meeting, add one
variant: **V25_swing_combined_shorts** — V22 settings, but with
`risk.allow_shorts: true` and a hard daily-loss floor at ₹500 (≈ 0.4%
of ₹120k capital). One battery slot, ≤2h. If V25 also shows PF<1.0
across all directions, the wind-down verdict is honest. If V25 shows
PF≥1.0, the wind-down verdict is wrong and the right move is a v3.1
that keeps the strategy but lifts the long-only veto under a tight
risk perimeter.

#### 2. Two new live bugs landed on the supposedly-frozen trader VM in the same morning the v3 charter promised "museum mode"

**Evidence — bug A (xgboost zombie).** `logs/daemon_2026-05-30.log:641-670`:

```
2026-05-30 09:38:49.617 | INFO | strategies.xgboost_classifier:generate_signal:339 -
    [xgboost_classifier] BUY AAPL buffered (1/2) | prob_up=0.800
2026-05-30 09:38:49.624 | INFO | strategies.xgboost_classifier:generate_signal:354 -
    [xgboost_classifier] BUY AAPL | prob_up=0.800 | stability=2/2
2026-05-30 09:38:49.630 | INFO | strategies.xgboost_classifier:generate_signal:378 -
    [xgboost_classifier] SELL AAPL buffered (1/2) | prob_down=0.900
```

30 total AAPL/MSFT hits since 09:38:49 IST today. Neither symbol
appears in `data/v2_universe_232.txt`, `data/v3_universe_top30.json`,
`config.yaml`, or any `tests/fixtures/*` file. Commit `c9d3936`
("T1 verdict applied: V15 PF=0.77 < 0.90 -> retire xgboost_classifier")
landed at 09:02:07 IST — **36 minutes before** the classifier started
firing BUYs on US tickers in this morning's daemon.

**Evidence — bug B (runtime-persist AttributeError).** Same log,
~10:10 IST:

```
2026-05-30 10:10:45.804 | WARNING | trading_agent:_persist_runtime_state:2343 -
    [RUNTIME-PERSIST] save failed: AttributeError("'TradingAgent' object has no attribute '_strategy_state'")
```

Twice within 1 ms (two near-simultaneous persist attempts both
crashed). This is the **direct evidence** for session 1 Finding 4's
"writer path is dead" hypothesis. The agent cannot persist its
strategy_state across restarts — every restart starts from zero, and
the `self_sufficiency.json` file will never advance.

**Business interpretation.** The freeze contract
(`docs/freeze/FREEZE_v2.1.md`, expires 2026-06-05) and the v3 charter
(`freeze_v3.0_charter_2026-05-30.md` §6.1, "museum mode") explicitly
forbid trader-VM changes during the freeze window. The "retire
xgboost_classifier" change at 09:02 was meant to be a backtest-side
T1 verdict — but in this morning's local daemon it has either (a) not
deactivated the strategy at all (still in `strategies.active`), or
(b) deactivated only the V15 backtest variant and left the live config
untouched. Either way, the operator cannot rely on the freeze
discipline to mean "live behaviour is locked". And the runtime-persist
break would silently corrupt the wind-down meeting's "cumulative
realised" reading if the trader VM ever restarts.

**Estimated ₹ impact / day.** If this were the actual trader VM
(this morning's daemon is the local laptop, so the production impact
is bounded), a zombie classifier firing BUYs on tickers not in
`v3_universe_top30` could (a) place an order against a symbol the
broker doesn't recognise → rejected, no loss, or (b) place an order
against a symbol the broker DOES recognise → uncontrolled loss
potential bounded only by `max_position_size_pct: 15.0`
(`config.yaml:319`) = ≈₹18k of notional risk per position. **P0
regardless of impact**, because the failure class is "strategy fires
on symbols that were never validated".

**Recommended action.** Two things, both today:

1. **Kill the local laptop daemon now** — it has nothing useful to do
   on a Saturday and it's polluting the production log with AAPL
   signals that future EOD scripts will have to filter.
2. **Run `code-bug-review`** on the trader-side
   `strategies.xgboost_classifier` and `trading_agent._persist_runtime_state`.
   File findings under `docs/bug_found_2026-05-30/`. The two bugs need
   formal write-ups before the 2026-06-05 verdict.

#### 3. Three of session 1's six findings are still open 10 hours later — every commit today went to v3, none to observability

**Evidence.** `git log --since="2026-05-30 01:00" --pretty=oneline`
shows 13 commits since session 1: 7 are v3 charter/phase commits
(A1-A5), 1 is the T1 verdict, 5 are doc reorg / cross-reference
sweeps. None mention:

* Session 1 Finding 3: checkpoint acceptance-rate gate. The latest
  checkpoint (`logs/audit/2026-05-29/checkpoint_1601.md:4`) still says
  `Verdict: GREEN` despite cumulative -₹1,212 and 3 sessions of zero
  trades.
* Session 1 Finding 5: conftest isolation against test→prod CSV/log
  leakage. `tests/conftest.py` was not touched today.
* Session 1 Finding 6: supervisor restart loop. Today's log shows
  **21 restart cycles** by 10:10 IST — the same loop, now firing on a
  market-closed Saturday.

**Business interpretation.** The v3 push is excellent engineering
discipline (mechanical charter compliance, pre-committed verdict tree,
honest forensic doc). But it is happening to the exclusion of the
observability fixes that the same operator agreed to at 01:24 IST.
The trade-off is real: if the v3 sweep proves the project's hypothesis
dead, the observability fixes were never going to matter. **But if v3
proves anything *short* of dead (Finding 1 above suggests it might),
the operator will need acceptance-rate alerting and conftest isolation
in the next live run — and they'll be writing them under pressure
between 2026-06-05 (verdict) and 2026-06-08 (wind-down trigger
window).**

**Estimated ₹ impact / day.** Zero direct, but: the supervisor loop
costs ~₹10/day of cloud CPU and 100% of forensic readability for any
15:15-15:55 window. The acceptance-rate gate would have caught the
"engine inert in bear" mode 3 days earlier than the operator did
manually. The conftest isolation is the kind of thing that costs zero
until the day it costs a P0 reconciliation incident.

**Recommended action.** Pick ONE of these and ship before the next
market open (2026-06-02 Monday):

* Cheapest: the supervisor `--no-restart-after-self-exit` flag.
  5 lines in `run_daemon.py`. ~10 min.
* Highest leverage: the acceptance-rate `<5%` ⇒ YELLOW checkpoint
  rule. ~20 min in the audit generator.
* Most-needed: the `conftest.py` monkey-patch isolating the prod CSV
  + logger paths. ~30 min, fixes Bug O properly.

#### 4. The Saturday daemon is hammering AngelOne's websocket with reconnect attempts on a closed market

**Evidence.** `logs/daemon_2026-05-30.log` tail:

```
2026-05-30 10:10:45.727 | ERROR | core.websocket_client:_reconnect_loop:779 - Reconnection failed: down
2026-05-30 10:10:45.727 | INFO  | core.websocket_client:_reconnect_loop:765 - Reconnecting WebSocket in 32s...
... (repeats with 60s, 60s, 60s, 60s, 60s, 2s backoff cycles)
```

Combined with 21 supervisor-induced daemon restarts in the same morning,
the local laptop is generating ~30-50 connection attempts per hour to
AngelOne's websocket endpoint **while the broker is closed for the
weekend**. The reconnect-loop backoff caps at 60s but the supervisor
restart resets it every ~75s, so the backoff never compounds.

**Business interpretation.** AngelOne does not advertise a rate limit
on websocket connection attempts but Indian retail brokers
*do* track these and *do* throttle abusive clients. A weekend pattern
of dozens of failed reconnects can quietly mark the client_id as
suspicious; the consequence next live session is a delayed connection
or a silent throttle. Either of those becomes a P0 if it happens on a
trade-decision cycle.

**Estimated ₹ impact / day.** Likely zero, possibly catastrophic.
Cannot quantify without AngelOne's internal throttle policy.

**Recommended action.** Either (a) kill the local daemon (it has no
purpose on a Saturday), or (b) extend the
`core.websocket_client._reconnect_loop` to detect `state ==
idle_off_hours` and short-circuit the reconnect entirely. ~10 lines.

#### 5. Source-of-truth drift now has FOUR disagreeing numbers for "are we paying for ourselves"

**Evidence.**

| Source | Cumulative realised | Cash | Last update |
|---|---|---|---|
| `data/self_sufficiency.json` | **0.0** | (n/a) | 2026-05-14 13:45 (16d stale) |
| `logs/health.json:9-12` | (n/a) | **100,000.0** | 2026-05-29 18:32 (idle_off_hours) |
| `logs/audit/2026-05-29/checkpoint_1601.json:120` | **-1,212.26** | **120,990.17** | 2026-05-29 16:01 |
| `logs/diagnostics/eod_2026-05-29.md:2` | (per-strategy: 3 trades) | (n/a) | 2026-05-29 18:33 |

Session 1 already flagged the first three. The fourth (eod_2026-05-29.md
reporting only 3 trades when CSV has 7 post-5/21) confirms the DB
analytics-pipeline blindspot is still active.

**Business interpretation.** The wind-down decision on 2026-06-08 will
hinge on `cumulative_realised vs cost-burn`. Today there are FOUR
candidate sources and they disagree by **₹1,212 on realised** and
**₹20,990 on cash**. If a fresh restart of the trader VM reads
`self_sufficiency.json` as the canonical state, the verdict meeting
sees `cumulative = 0`, which is **the most optimistic reading
available**. Session-2 evidence (bug B above: `_persist_runtime_state`
throwing AttributeError) makes this scenario more plausible, not less:
the persistence file isn't getting rewritten because the writer
crashes.

**Estimated ₹ impact / day.** Zero direct, but the wind-down decision
itself is the highest-stakes decision the project will make. Reading
the wrong number = wrong decision. Bounded above by the same
₹12-18k/year option-value as Finding 1.

**Recommended action.** Before the 2026-06-05 verdict meeting, add ONE
assertion to the EOD pipeline:

```python
assert self_sufficiency["cumulative_realised_inr"] == checkpoint["cumulative_realised_inr"], \
    f"DRIFT: self_sufficiency={...}, checkpoint={...}"
```

Fail loud, do not silently fall back. ~15 min.

---

### Things the daemon is telling itself that are not true

* **Commit `c9d3936` declares "retire xgboost_classifier", but xgboost
  is still firing BUY/SELL signals 36 min later in the same daemon
  process.** The retirement was at the V15-variant level
  (`logs/backtests/...V15...`) and did not change the live config's
  `strategies.active` list. The narrative says "retired"; the live log
  says "still scoring AAPL/MSFT every cycle".
* **`v3_phase_a5_forensic_2026-05-30.md` says "trend-pullback does not
  have edge"**, but the gate_stats show the verdict was computed on
  the **long-only 7.3% (V20) / 11.3% (V22)** of the strategy's natural
  signal set. Correct framing: "trend-pullback's long-only arm does
  not have edge on Nifty-30 daily."
* **`comparison.md` headers say "Days: 600"**
  (`logs/backtests/battery_v3_swing_a5_180d_eff_20260530T050422/comparison.md:7`)
  but the run-id is `..._180d_eff_...`, the charter scopes 180 days,
  and `log.txt:2` confirms `--days 600`. The "180d_eff" suffix and
  the `Days: 600` cell can't both be right. (Not material to the
  verdict — 600 days is a richer evidence base than 180, so if
  anything the "no edge" reading is stronger than the charter spec
  would suggest.)
* **Checkpoint `1601.md:4` still says GREEN** despite the prior brutal
  review documenting 3 sessions of zero trades and Finding 1
  recommending the GREEN/YELLOW/RED gate be rewired. No change today.

---

### Things that look fine

* **V3 charter discipline is exemplary.** Pre-committed verdict tree
  (`freeze_v3.0_charter_2026-05-30.md §6.5`), mechanical application
  ("SURPRISE branch"), single-day Phase A1→A5 execution with 7 commits,
  no scope drift. This is the right way to operate. The forensic doc
  even refuses to recommend next steps ("Sleep on it. Decide tomorrow.")
  per charter §10.5 R1.
* **V3 fill_mode is honest.** Confirmed `BacktestConfig.fill_mode:
  "next_bar_open"` in every V20-V24 result file; engine path at
  `packages/research/backtest_ensemble.py:682` dispatches correctly;
  `no_next_bar` gate counter exists (`results/V22_*.json:gate_stats`)
  and reported 3 dropped signals out of 2962 — bounded, transparent.
  Phase A2-1 was the largest gap and it landed clean.
* **Bug K (slice-after-cache-save) is fully closed** with three regression
  test classes — verified during this morning's gap analysis
  (`docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md:174-184`).
  No new evidence to add or contest.
* **DELIVERY charges path** is the correct STT-on-both-legs + DP-per-SELL
  model (`packages/core/charges.py:150-218`); the only docstring drift
  is the "per day" misnomer in the charter, scoped for a footnote.

---

### What I refused to conclude (insufficient evidence)

* **Whether `trend_pullback` with `allow_shorts: true` is profitable.**
  Cannot be answered without a V25 re-run. **This is the single most
  important open question for the 2026-06-05 verdict.**
* **Origin of the AAPL/MSFT xgboost signals.** Could be (a) a stale
  paper-mode universe in this local laptop's `config.yaml`, (b) a
  test fixture's symbol list leaking into the prod data_handler, or
  (c) Yahoo Finance returning a fallback set when the requested
  Nifty-30 symbols 404. Needs `code-bug-review` to trace from
  `data_handler.download_historical_for_*` → `xgboost_classifier.generate_signal`.
* **Whether the trader-VM `runtime_state.json` writer has been broken
  since 2026-05-14.** The mtime suggests yes, the AttributeError
  caught this morning suggests yes, but I cannot say *when* the bug
  was introduced without a git-blame on `_persist_runtime_state` and a
  diff against the last successful write. Out of brutal-review scope;
  belongs in `code-bug-review`.
* **Whether `allow_shorts:false` was a charter-level decision or a
  risk-policy-veto that the v3 sweep inherited by accident.** The
  veto is documented at `config.yaml:319` as "Higher-level kill switch
  on the SHORT side (2026-05-25)" with no expiry, but the v3 swing
  charter does not explicitly say "long-only". The default behaviour
  may not match the charter's intent. Needs an operator decision before
  V25 is queued.

---

### Next 24h checklist (operator actions, ranked)

1. **(P0, ~10 min)** Kill the local laptop daemon NOW. It is on a
   Saturday, emitting xgboost BUY signals for AAPL/MSFT, hammering
   AngelOne's websocket, and producing zero useful output. (Closes the
   ongoing damage from Findings 2 + 4.)
2. **(P0, ~2 h)** Queue **V25_swing_combined_shorts** = V22 + `risk.allow_shorts: true` + hard daily-loss floor ₹500. Run before the
   verdict meeting. If V25 ≥ PF 1.0 the wind-down trigger is wrong;
   if V25 < PF 1.0 the wind-down trigger is honest. **Without this,
   the verdict is on half the data.** (Closes Finding 1.)
3. **(P0, ~30 min)** Confirm xgboost is actually deactivated in the
   live config — open `config.yaml`, check `strategies.active`. If
   `xgboost_classifier` is still in the list, that's the bug (the T1
   verdict commit `c9d3936` only changed backtest variants). Either
   way, file a `code-bug-review` write-up of the AAPL/MSFT signal
   origin into `docs/bug_found_2026-05-30/`. (Closes Finding 2 bug A.)
4. **(P0, ~30 min)** Fix `_persist_runtime_state` — the strategy_state
   attribute reference is broken. Grep for `_strategy_state` vs
   `strategy_state` vs `_strategies_state` and reconcile. Add a unit
   test that asserts persistence succeeds on a freshly-constructed
   `TradingAgent`. (Closes Finding 2 bug B.)
5. **(P1, ~15 min)** Add the EOD assertion: `self_sufficiency.cumulative ==
   checkpoint.cumulative ± 0.01`. Fail loud. (Closes Finding 5.)
6. **(P1, ~10 min)** Ship at least ONE of session 1's three unfixed
   observability items — the supervisor `--no-restart-after-self-exit`
   flag is the cheapest. (Partially closes Finding 3.)
7. **(P2, post-decision)** Resolve the "long-only veto vs charter
   intent" question (last bullet of "refused to conclude"). Either
   document `allow_shorts:false` as a permanent project-level
   constraint, or scope a v3.1 that explicitly tests both directions.

---

### One-paragraph summary for the 2026-06-05 verdict meeting

The v3 swing pivot executed end-to-end in a single day with charter
discipline I'd happily ship to a real desk. The forensic verdict ("no
edge, sleep on it") is honest *for what it tested* — but it tested the
**long-only 7-11% of the strategy's natural signal set**. Triggering
the wind-down on this evidence without first running a shorts-allowed
variant (≤2h of work) is the most expensive call this project can
still make. In parallel: two live bugs landed on the supposedly-frozen
trader VM in the same morning (xgboost zombie firing for US tickers
36 min after the retirement commit; runtime-persist throwing
AttributeError every cycle), and the GREEN/YELLOW/RED gate, the
acceptance-rate alarm, the conftest isolation, and the supervisor
restart loop — all four cheap observability fixes the operator agreed
to at 01:24 IST — are still untouched 10 hours later. **Verdict
remains RED, escalating to RED-WITH-NEW-BUGS.**

---

### Cross-references (delta only — session 1's full list still applies)

* `docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md` — the morning's
  honest "SURPRISE branch" verdict; this session contests one degree
  of the framing (long-only sub-sample).
* `docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md` — Phase A1
  deliverable, confirms `next_bar_open` fill model is implemented.
* `docs/freeze/freeze_v3.0_charter_2026-05-30.md` — the new operating
  contract whose §10.5 R1 / §6.5 the morning's forensic correctly
  honoured.
* `logs/backtests/battery_v3_swing_a5_180d_eff_20260530T050422/results/*.json`
  — the gate_stats this session re-reads to surface the shorts-blocked
  rate the forensic doc didn't quote.
* `logs/daemon_2026-05-30.log` — the local laptop daemon log where
  the xgboost-zombie and runtime-persist bugs surfaced this morning.
* `git log --since="2026-05-30 01:00"` — the 13-commit delta since
  session 1.

---

## Session @ 14:47 IST

**BRUTAL REVIEW — 2026-05-30 (Session 3)**
Scope: review of the new V25 finding and the three closure commits
landed between Session 2 (11:07 IST) and now.
Persona: Expert algo trader + adviser. Verdict is unsentimental.

This is a **scoped** brutal review — not a full sweep — focused on the
operator's question: "is the V25 finding honest enough that the
2026-06-05 wind-down trigger can fire on it without re-opening the
'incomplete data' objection?" Session 2's mandate produced five P0/P1
items; this session checks what landed and whether the V25 verdict
that drove the V25 commit is robust.

**New artefacts under review:**

| Commit | Time IST | What it claims to close |
|---|---|---|
| `7b4723d` | 14:11 | Session 2 §2 Bugs A (xgboost zombie) + B (`_persist_runtime_state` AttributeError) |
| `f50e9ee` | 14:12 | Session 2 §1 — adds V25_swing_combined_shorts variant |
| `2837b45` | 14:13 | Session 2 §3 — supervisor restart loop (Session 1 Finding 6) |
| `6f13234` | 14:14 | docs: brutal-review Session 2 + findings_log §30 audit closure |
| `f1670d4` | 14:45 | V25 verdict: PF 0.23 → wind-down trigger on complete data |

Five session-2 P0/P1 items: closed = 4 (§1 V25, §2 bugs A+B, §3
supervisor, §5 source-of-truth divergence partially via findings
log). Open = 1 (the EOD self_sufficiency==checkpoint assertion was
not landed; see Things-That-Look-Suspect §3 below).

---

### Verdict (one line)

**The V25 finding is honest.** The wind-down trigger CAN now fire on
2026-06-05 without re-opening the long-only-veto objection — but with
three named caveats below that the operator's own write-up did not
surface and which the verdict meeting deserves to see.

Status moves from **RED-WITH-NEW-BUGS** (session 2, 11:07) to
**RED, evidence-complete** (now). Verdict colour unchanged; the
adverb changes from "escalating" to "complete-enough".

---

### Bottom-line numbers (independently re-derived from V25 raw JSON)

Source: `logs/backtests/battery_v3_swing_a5_v25_shorts_20260530T090709/results/V25_swing_combined_shorts.json`.

| Metric | Claimed (commit `f1670d4`) | Re-derived | Match? |
|---|---|---|---|
| Trades | 189 | `summary.trades = 189` (`:63`) | ✅ |
| Win rate | 27.5% | `summary.win_rate = 27.5` (`:66`) | ✅ |
| PnL | -₹3,764 | `summary.pnl = -3763.63` (`:67`) | ✅ |
| Profit Factor | 0.23 | `summary.profit_factor = 0.23` (`:68`) | ✅ |
| R:R | 1:0.61 | `summary.rr = 0.61` (`:69`) | ✅ |
| Sharpe (annualised) | -5.53 | `summary.sharpe = -5.53` (`:71`) | ✅ |
| Max drawdown | 37.84% | `summary.max_dd_pct = 37.84` (`:72`) | ✅ |
| Charges drag | -₹3,408 | `summary.charges = 3408.11` (`:74`) | ✅ |
| Shorts blocked | 0 | `gate_stats.shorts_blocked = 0` (`:85`) | ✅ |
| Executed / Total signals | 189 / 2,636 = 7.2% | `executed=189`, `total_signals=2636` (`:77,84`) | ✅ |
| Override resolution | `allow_shorts: True` wins | `overrides[]:55-58` shows `allow_shorts:true` is the LAST entry; later overrides win in the loader | ✅ (also pinned by `test_v25_resolves_allow_shorts_true`) |

All headline numbers match the commit message and the forensic
§8 update. The V25 result is internally consistent with the artefact
on disk.

---

### Top suspicions, ranked by ₹ impact

#### 1. The backtester does NOT apply the live drawdown halt — every V20-V25 PF reading lets the strategy bleed past `drawdown_halt_pct: 20.0`

**Evidence.** V25's MaxDD is 37.84% (`results/V25_swing_combined_shorts.json:72`).
`config.yaml:319` block has `drawdown_halt_pct: 20.0` and
`max_drawdown_pct: 10.0` (daily). A grep of
`packages/research/backtest_ensemble.py` for `drawdown_halt|max_drawdown_pct`
returns **4 hits, all of them reporting metrics (lines 199, 1147, 1203,
1225) — none of them gating execution**. The halt config keys exist on
the dataclass; nothing in the event loop consults them.

```
packages/research/backtest_ensemble.py:1147
            r.max_drawdown_pct = (
                mdd_val / final_peak * 100.0 if final_peak else 0.0
            )
```

This is the **only** consumer. The backtester reports drawdown — it
does not stop the strategy when drawdown exceeds 20%.

**Business interpretation.** V25 ran to -37.84% drawdown in backtest;
the live engine would have halted at -20% (potentially earlier if the
daily 10% gate fires first). So V25's PnL of -₹3,764 over the full
600-day window is an over-statement of the loss the live engine would
have realised — the real live loss would have been bounded by the halt
threshold, then the strategy would have been *suspended*, not deleted.

This does **not** rescue the verdict — PF 0.23 over the surviving
sub-window would still be < 1.0, and the strategy would still be a
losing strategy that the operator should not run. **But it does mean
every PF figure across V20-V25 is computed without the most important
protective gate the live engine actually has.** Any future v3.1
backtest will have the same issue. If at some point a candidate
strategy lands at PF 0.95-1.05, the backtester is mis-reporting the
live outcome by *more* than the wind-down margin.

**Estimated ₹ impact / day.** Zero in the wind-down context (V25
verdict not threatened). Bounded above by **the misclassification
risk on a future borderline strategy** — if such a candidate emerges
in v3.1 or later, this gap could either falsely accept a strategy
(backtest looks decent because losses kept compounding into a recovery
phase) or falsely reject one (backtest looks bad because losses ran
past the live halt). Likely a one-line `if r.max_drawdown_pct >
drawdown_halt_pct: break` in the event loop. ~30 min fix.

**Recommended action.** **Not blocking the 2026-06-05 wind-down
decision.** File as a v3.1-prerequisite if any future strategy push
happens. Document in `docs/findings/findings_log_2026-05-27.md` so
the gap is on the record for the verdict meeting.

#### 2. 71.8% of V25 signals dropped at the 5-position cap — V25 tested "what does V22+shorts look like with this position cap" and not "is there short edge"

**Evidence.** `V25 gate_stats: max_positions_reached = 1,893 / 2,636 = 71.8%`.
Only 7.2% of generated signals actually opened a position. The
forensic §8.3 acknowledges this and asserts "Even at higher caps the
additional fills would be marginal-quality shorts further diluting
the book" — but that assertion is **uncited**. There is no V25-with-
higher-cap data point to support it.

The asymmetric-short caveat compounds this: trend_pullback's SELL has
1 gate vs BUY's 5 gates; the strategy should emit SELL roughly 10x as
often as BUY in a directional regime. V25's SELL/BUY trade ratio is
143/46 = 3.1:1 — **much less than the underlying signal ratio**. The
gap (10:1 emission, 3:1 execution) is being absorbed by the position
cap, not by any signal-quality filter.

**Business interpretation.** V25 *as designed* answers the exact
question Session 2 asked ("does adding shorts to V22 turn it
profitable?" — answer: no). But V25 does **not** answer the closely
related question "is the short side of trend_pullback profitable when
not capital-constrained?" The position-cap dilution means the V25 PF
of 0.23 is a verdict on a *capital-throttled subset* of the short
signal set, not on the short signal set itself.

For the **wind-down decision specifically** this does not matter —
the live engine WILL be capital-constrained, and at the live
`max_positions: 5` value V25 reproduces the live behaviour faithfully.
For any **v3.1 follow-up that proposes a higher position cap**, V25 is
not informative about what would happen.

**Estimated ₹ impact / day.** Zero in the wind-down context. The
operator's forensic §8.3 framing is correct in practice; the
adversarial point is just that the supporting evidence isn't on disk.

**Recommended action.** **Optional pre-verdict insurance run:** a
V26_swing_combined_shorts_loose_cap = V25 with `risk.max_positions:
15`. If V26 PF is still < 1.0, the operator's §8.3 assertion is
confirmed; if V26 PF ≥ 1.0, the wind-down verdict is on shaky ground
and v3.1 is owed a proper short-side strategy. Cost: one battery slot
(~25 min); deferrable. If skipped, the verdict meeting should note
this as a known sub-question that was not tested.

#### 3. The "source-of-truth divergence" item from Session 2 §5 was NOT closed by today's commits

**Evidence.** Session 2 §5 recommended a one-line EOD assertion:
`self_sufficiency.cumulative == checkpoint.cumulative ± 0.01`. Today's
commits (5 of them, all 14:11–14:45) touched: `strategies` (Bug A),
`trading_agent._persist_runtime_state` (Bug B),
`packages/research/battery.py` (V25 variant),
`tools/run_daemon_resilient.ps1` (supervisor fix), and
`docs/reviews/brutal_review_2026-05-30.md` + `docs/findings/...`
(audit closure). **No commit modified the EOD pipeline.**

`data/self_sufficiency.json` is still stale (mtime 2026-05-14, content
`cumulative_realised_inr: 0.0`). The 2026-06-05 verdict meeting will
still read three different cash figures from three different files
unless one of them is explicitly chosen as canonical.

**Business interpretation.** Bug B's fix (preserve the disk snapshot
when in-memory state is incomplete) *reduces* the risk of a stale
`self_sufficiency.json` getting silently clobbered with an empty
state. So the worst-case scenario from Session 1 Finding 4 is less
likely than it was at 01:24 IST. But the **positive** assertion that
the three sources agree is still absent. If, between now and 2026-06-05,
the trader VM restarts and reads `self_sufficiency.json` as canonical,
the operator sees `cumulative=0` and over-counts the project's
profitability by ₹1,212.

**Estimated ₹ impact / day.** Zero direct; bounded by the wrong
decision risk at the verdict meeting. Worth 15 min between now and
the meeting.

**Recommended action.** Add the assertion before 2026-06-04 EOD.
Trivial.

---

### Things that look fine

This section is **larger than usual on purpose** — the operator
shipped a lot in 3 hours and most of it is correct.

* **V25 variant is config-correct.** Override list (`results/V25_*.json:3-58`)
  ends with `["risk.allow_shorts", true]`. Loader applies overrides in
  order, later wins. Behaviour pinned by
  `test_v25_resolves_allow_shorts_true`. The base-config `allow_shorts:
  false` is correctly overridden by the V25-specific true. No bug.
* **V25 verdict-tree pre-commit was honoured mechanically.** The
  commit `f50e9ee` documented the two-branch verdict tree in the
  variant docstring BEFORE the run; commit `f1670d4` shows the V25
  PF=0.23 falls in the first branch ("forensic verdict honest, wind-down
  on complete-enough data"). Charter §10.5 R1 ("do NOT debug into
  oblivion") explicitly NOT triggered: no parameter sweep, no "let me
  also try V26 with wider SL", no retry on a different universe. This
  is exemplary discipline.
* **Forensic §8 (the V25 update) self-flags the asymmetric-short
  caveat.** §8.5 names what V25 rules out AND what it doesn't, including
  "A truly symmetric short strategy (`trend_pullback_short` with mirror
  gates) … V25 doesn't prove it doesn't [have edge]". The operator's
  framing is one degree more honest than the commit-message headline.
  The brutal review's primary objection from session 2 is closed; the
  remaining v3.1 hypothesis is parked correctly.
* **Bug A fix (xgboost zombie) is defence-in-depth.** Module-level
  `DEPRECATED_STRATEGIES = {"xgboost_classifier"}` with a CRITICAL log
  when a stale config tries to load a retired name. The right
  semantics: "config can be stale, denylist cannot". To revive: must
  remove from the set in the same commit that overturns the verdict.
  6 tests, including `test_load_strategies_denylist_logs_critical`.
* **Bug B fix (`_persist_runtime_state`) chose the right failure
  semantics.** The commit explicitly rejects `getattr(..., {})` (which
  would clobber the disk snapshot) in favour of skip-and-CRITICAL-log
  (which preserves the snapshot). This is the correct choice for a
  persistence layer protecting protective runtime state — silent
  clobber would be worse than the swallow. 3 tests covering both the
  skip path and the success path.
* **Supervisor fix is targeted.** `tools/run_daemon_resilient.ps1`
  diff: only the post-exit branch changed. `if $exitCode -eq 0` logs
  `[SUPERVISOR-CLEAN-EXIT]` and `exit 0`. Non-zero still cooldowns and
  retries — transient broker / OOM failures still self-heal. The
  opt-out env var `SUPERVISOR_RESTART_ON_CLEAN_EXIT=1` is documented
  in the supervisor's own log so an operator restoring legacy
  behaviour can grep-find the knob. 4 structural tests pin the
  PowerShell source against future regressions.
* **All 1,765 unit tests pass.** Bug A+B commit message documents zero
  regressions. The session-2 fixes did not break anything.

---

### What I refused to conclude (insufficient evidence)

* **Whether V26 (V25 + higher max_positions) would change the verdict.**
  Not run. The operator's §8.3 assertion ("higher caps would dilute")
  is plausible but uncited. See Suspicion §2.
* **Whether the trader-VM (cloud) currently has the Bug A/B fixes
  deployed.** The fixes are on `main`; the trader VM's deploy
  status is not visible from this local checkout. If the freeze
  contract forbids re-deploys during the freeze window, the fixes are
  *correct on disk but not in production*, which means the next live
  session could still emit the zombie xgboost signals and the
  AttributeError. Operator action: explicit confirmation that either
  (a) the trader-VM was redeployed, or (b) the trader-VM never had
  these bugs because the prod config was already clean.
* **Whether the 03:45 IST timestamps on V25 trade entries
  (`results/V25_*.json:107-108`) reflect a Yahoo-data timezone offset
  or a genuine engine bug.** Trade 1: entry `2025-01-20T03:45:00+05:30`,
  exit `2025-01-21T03:45:00+05:30`. 03:45 IST is well before market open;
  the daily bar should be stamped 09:15 (open) or 15:30 (close). Most
  likely Yahoo Finance returns UTC-aligned 22:15 timestamps for daily
  bars that the data_handler converts to IST → 03:45 IST next day. Not
  material to PF / WR / DD / verdict (those are computed on prices,
  not timestamps), but the timestamps will look wrong to anyone
  reviewing trade-by-trade later. Worth a 1-line clarification in the
  variant block.
* **Whether the BUY-arm PF drift (V20: 0.41 → V25 BUY: 0.31) is
  signal or noise.** N=46 and N=55 at sub-1 PF, no confidence interval
  reported. Operator's "explained by short positions consuming
  position cap" is the most parsimonious explanation. Not material to
  the wind-down.

---

### Next 24h checklist (operator actions, ranked)

1. **(P1, ~15 min, before 2026-06-04 EOD)** Add the EOD assertion
   `self_sufficiency.cumulative_realised_inr ==
   checkpoint.cumulative_realised_inr ± 0.01`. Closes the last open
   Session 2 item. The verdict meeting deserves to read one cumulative
   number, not three.
2. **(P1, ~25 min, OPTIONAL pre-verdict insurance)** Run V26 =
   V25 + `risk.max_positions: 15`. If V26 PF < 1.0, the wind-down
   verdict is confirmed against the position-cap-dilution objection.
   If V26 PF ≥ 1.0, defer the wind-down — the short side may have
   edge that was capital-throttled. Skipping is defensible if the
   operator commits in the verdict-meeting minutes that "V25's
   position-cap dilution was an accepted caveat".
3. **(P1, before 2026-06-04 EOD)** Confirm that the trader-VM has
   either (a) been redeployed with the Bug A+B fixes (denylist +
   persist guard) OR (b) never had those bugs in prod (the bugs were
   local-checkout-only). Either is fine; not knowing which is not.
4. **(P2, ~30 min, post-decision)** Wire the `drawdown_halt_pct` and
   `max_drawdown_pct` config values into the backtester event loop.
   Currently they are read-only metrics; in live they are protective
   gates. Any v3.1 backtest would benefit. Not blocking the wind-down.
5. **(P2, ~1 line)** Footnote on the V25 trade timestamps — either
   convert to 09:15 IST or document the Yahoo 22:15 UTC → 03:45 IST
   conversion in the variant block. Cosmetic.

---

### One-paragraph summary for the 2026-06-05 verdict meeting

V25 is honest. The wind-down trigger can fire on it without re-opening
the long-only-veto objection — the BUY arm of V25 reproduces V20's
edge profile (PF 0.31 vs 0.41) and the SELL arm at PF 0.19 closes
the cheap "what about the obvious short emissions we were vetoing"
question. The operator's forensic §8 correctly self-flags that V25's
short side used the strategy's long-exit signal as a short-entry (the
"asymmetric short" caveat) and parks a properly-symmetric
`trend_pullback_short` as a separate v3.1 hypothesis — that
discipline is exactly right. Three new sub-findings remain for the
verdict meeting record: (a) the backtester does not apply the live
`drawdown_halt_pct: 20.0` gate, so every PF reading lets losses run
past where the live engine would halt; (b) 71.8% of V25's signals were
dropped at `max_positions: 5`, so V25 tested a capital-throttled
subset rather than the underlying signal quality, with the operator's
"higher caps would dilute" assertion plausible but uncited (V26 would
close this in 25 min); (c) the EOD-assertion against
source-of-truth divergence (Session 2 §5) is still unshipped. None of
these threaten the wind-down verdict; all three deserve mention in
the meeting minutes. **Verdict: RED, evidence-complete. Operator is
free to make the 2026-06-05 call.**

---

### Cross-references (delta only — session 1 and 2's full lists still apply)

* `logs/backtests/battery_v3_swing_a5_v25_shorts_20260530T090709/results/V25_swing_combined_shorts.json`
  — the raw artefact this session re-derives from.
* `docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md` §8 — the
  operator's V25 update; this session contests three of its implicit
  framings (drawdown halt, position-cap dilution, source-of-truth
  closure status).
* `commit f1670d4` — V25 verdict commit; numbers all match.
* `commit f50e9ee` — V25 variant + pre-committed verdict tree; clean.
* `commit 7b4723d` — Bug A+B fix; correct failure semantics.
* `commit 2837b45` — supervisor fix; targeted, opt-out documented.
* `packages/research/backtest_ensemble.py:199, 1147, 1203, 1225` — the
  four hits for `drawdown_halt|max_drawdown_pct`, all reporting and
  none gating.
