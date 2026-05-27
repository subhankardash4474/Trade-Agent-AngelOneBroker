# Diagnostic Sprint — 2026-05-27 → 2026-05-31 (5 days)

**Author:** Investigative agent + operator
**Trigger:** Advisor memo received 2026-05-27 morning (May-27 review,
day 9 of Freeze v2.1) recommending a 5-day targeted diagnostic sprint
to reduce ambiguity ahead of the 2026-05-29 Friday review and the
2026-06-08 freeze-end + candidate-deploy decision.

**Schedule:** Option A — Friday is **review-only** (no new sprint work
that day). Sprint work distributed Wed / Thu / Sat / Sun.

**Constraint:** Every sprint action must be observability-only or
in-place edit of an already-slotted config value. No new freeze-bypass
slots may be consumed.

---

## Sprint summary table

| Hyp | Title | Day | Status | Evidence link |
|---|---|---|---|---|
| H1 | Regime classifier inputs are stale or wrong-scale → bear_high_vol bias | 1 (Wed) | **DEPLOYED** (observability live) | commit `e1df9e8`; `logs/trading_agent_2026-05-27.log` `[REGIME-INPUT]` lines |
| H2 | Long-side famine is xgboost-model bias, not signal availability | 1 + 2 | OPEN | will use H1 logs + per-strategy signal CSV |
| H3 | V4 threshold-3% on Nifty 50 is the right candidate | 2 (Thu) | **PARTIALLY CONFIRMED** | `findings_log_2026-05-25.md §2`; awaiting xgboost-active re-run on backtester |
| H4 | Short side has structurally negative edge regardless of tuning | 0 | **CONFIRMED** | `findings_log_2026-05-25.md §6` (V1 90d × 228: longs +₹556 / shorts −₹379) |
| H5 | Bug H — xgboost silently OFF in battery | 0 | **CONFIRMED + FIXED** | commit `73c26bf`; `findings_log_2026-05-25.md §16` |
| H6 | Bug I — trader VM divergence from main for 2 weeks | 0 | **CONFIRMED + FIXED** | `findings_log_2026-05-25.md §17`; operator rebuild 2026-05-26 14:37 IST; closure verdict `findings_log_2026-05-27.md §3` |
| H7 | First long-only session 2026-05-27 produces different signal mix than all-short Week 2 | 1 + 2 | OPEN | will use today's `signal_audit_2026-05-27.csv` + `trading_agent_2026-05-27.log` |
| H8 | xgboost long-side calibration may have drifted | 4 (Sat) | OPEN | requires historical calibration export + per-strategy 30d rolling WR |
| H9 | Intraday regime overlay has been live since 2026-05-17 but never observed | 1 (Wed) | **DEPLOYED** (observability live) | commit `e1df9e8`; `[REGIME-INTRADAY-INPUT]` confirmed at 11:05:22 IST |
| H10 | V4's PF 1.35 on Nifty 50 may not transfer to the live 228-stock universe | 4 (Sat) | OPEN | requires `nifty500_v1_long_only_60d` battery variant added to queue |

**Status legend:**
- **CONFIRMED** = hypothesis settled by existing data; no further work
  needed to answer it.
- **CONFIRMED + FIXED** = settled AND remediation already landed.
- **PARTIALLY CONFIRMED** = direction confirmed but one critical
  validation still in flight.
- **DEPLOYED** = the experiment needed to answer this hypothesis is
  live; the answer materialises as data accumulates.
- **OPEN** = needs action this sprint.

---

## Day 1 — Wednesday 2026-05-27 (TODAY)

**Deployed:**

* Commit `e1df9e8` — `feat(regime): per-cycle [REGIME-INPUT] observability
  log (diag-sprint H1)`. Two `logger.info()` calls in
  `packages/core/regime.py`:
  * `[REGIME-INPUT] nifty_trend=<int> india_vix=<float> high_vol=<bool> -> regime=<label>`
  * `[REGIME-INTRADAY-INPUT] nifty_intraday_pct=<float> vix_intraday_delta=<float> -> regime=<label>`
* Commit `8e1e926` — `fix(config): commit allow_shorts:false to git
  (slot-1 durability fix)`. See `findings_log_2026-05-27.md §2` for
  the regression timeline.
* Both pulled onto trader VM at 11:02 IST; image rebuilt + container
  recreated at 11:04 IST; first `[REGIME-INTRADAY-INPUT]` log line
  captured at 11:05:22 IST.

**Hypotheses advanced today:**

* **H1 DEPLOYED.** First 24h of logs will accumulate before review on
  Thursday. Expected sample size at 09:30 IST tomorrow:
  ~`231 stocks × 1 cycle / 60s × ~18 trading hours = ~250 000` per-symbol
  signal emissions, but `classify_regime` is called once per cycle (not
  once per symbol), so expected `[REGIME-INPUT]` count: **~1,080**
  (one per cycle, 60-sec poll, 18h × 60 = 1,080). Same expected count
  for `[REGIME-INTRADAY-INPUT]`.
* **H9 DEPLOYED.** Paired with H1.
* **H7 OPEN — start collecting.** Today's `signal_audit_2026-05-27.csv`
  will already show the first long-only signal mix. Pending: write a
  one-shot analyzer for `tools/diagnose_signal_mix.py` (queued for
  Day 2).

**Not done today on purpose:**

* H3 validation — backtester is running it; landing time is Thu 2026-05-28
  ~15:30 IST per the backtester-status probe. Trying to accelerate by
  killing the current run would lose the audit-only V1+V2 results we're
  about to get.
* H8 calibration — needs the H1 logs to settle first so we can correlate.
* H10 universe-transfer — needs queue space; backtester is fully
  consumed by the validation run.

---

## Day 2 — Thursday 2026-05-28

**Planned:**

1. **Pull 24h of `[REGIME-INPUT]` logs** off the trader VM at 09:30 IST.
   Run an aggregator: distribution over labels, distribution over
   inputs (`india_vix` histogram, `nifty_trend` count of ±1/0), how
   often `high_vol=True`. Answer for H1: does the classifier label
   match the labels a human reader would assign from the same inputs?
   Expected: yes (the logic is rules-based) — but the *inputs* may be
   stale, which is the actual concern. Cross-check `india_vix` against
   the live VIX from a third-party source (NSE indices feed).
2. **Pull 24h of `[REGIME-INTRADAY-INPUT]` logs**. Same treatment.
   Specific check: does `vix_intraday_delta` ever fire? The advisor's
   memo §intraday-overlay note suggests the field was wired but never
   observed.
3. **H3 validation result.** Read the backtester's `validation_run.md`
   when it lands (~15:30 IST). Decision: does V4 PF 1.35 hold with
   xgboost active? Three outcomes:
   * PF stays ≥ 1.30 → H3 fully confirmed; V4 is the preferred
     candidate. Carry into Friday review.
   * PF drops to 1.0–1.30 → H3 weakened; V4 still viable but the
     advisor's "V1 long-only" candidate moves to front.
   * PF drops below 1.0 → V4 candidate killed; fallback to V1 long-only
     (the slot-1 already-shipped path).
4. **H7 first-pass analysis.** With 2 sessions of long-only data, count:
   * Per-strategy signals emitted (LONG/SHORT/HOLD breakdown).
   * Per-strategy "would-have-fired" SHORT count under the *old*
     `allow_shorts: true` regime (extractable from logs because the
     daemon logs all candidate signals before the risk-manager
     filter).
   * Compare to the same 2-session window last week.
   Expected: short candidates remain ~unchanged in *count* but are
   *filtered* by `allow_shorts: false`; long candidates may rise
   slightly as ensemble weights re-balance.

**Not planned for Day 2:**

* Any code change to strategy / risk / ensemble. H1+H9 are the only
  active sprint patches.
* Any deploy to trader VM beyond what landed today.

---

## Day 3 — Friday 2026-05-29 (REVIEW DAY)

**No new sprint work.** Consumes the data assembled Wed + Thu.

**Review meeting deliverables** (template, to be filled in by Friday):

1. **Verdict on each hypothesis** (CONFIRMED / REFUTED / NEEDS-MORE-DATA).
2. **Candidate identification** for June 8 deploy:
   * Option A: V1-long-only (slot-1 already shipped; keep as-is).
   * Option B: V4 threshold-3% (requires slot-2 consumption *if* slots
     2–3 are reclassified to audit-only first).
   * Option C: V2 filters-off (untested at parameter-level on
     post-bug-H data; not ready for Friday).
   * Option D: extend freeze 7 days; postpone decision.
3. **Kill criteria** for the chosen candidate (K1–K5 framework per
   `docs/post_freeze_v4_proposal.md` §5.3, if it still exists in the
   repo; otherwise from the advisor memo §rec 6 verbatim).
4. **Slot 2 + 3 reclassification decision.** Per advisor memo §honest
   concern 2: if the slot-2 and slot-3 sweeps were genuinely
   behaviour-neutral audits, reclassify them to `audit-only` so the
   bypass count drops back to 1/3 (the slot-1 flag) before the freeze
   ends.

---

## Day 4 — Saturday 2026-05-30

**Planned (post-Friday-decision):**

1. **H8 — xgboost calibration drift analysis.**
   * Export the model's per-class probability histogram from the most
     recent 30 days of inference (the daemon logs `[XGB-PREDICT]`
     lines on every prediction; extract from
     `logs/trading_agent_2026-05-*.log`).
   * Compare to the training-time validation histogram (from the
     model's metadata or a re-run on the validation fold).
   * Decision question: has the LONG/SHORT probability distribution
     drifted? In particular, has the conditional `P(LONG | bear_regime)`
     collapsed (matching the 0/3 May-26 long stop-outs) or remained
     calibrated?
2. **H10 — universe-transfer validation.**
   * Queue a new battery variant
     `V20_nifty500_v1_long_only_60d` (or repurpose an existing slot
     in the queue). Universe: live ~228-stock list, 60-day window,
     long-only entries (mirrors slot-1 on the live config).
   * Launch overnight (cost: ~6h on the new Tier-D backtester).
   * Data ready by Sun morning.
3. **Bug J permanent fix.** Edit `tools/cloud/bootstrap_backtester.sh`
   per `findings_log_2026-05-27.md §1.5`. Audit-only commit.

---

## Day 5 — Sunday 2026-05-31

**Planned:**

1. **H10 data integration.** Read the
   `V20_nifty500_v1_long_only_60d` output. Decision-affecting question:
   does V1-long-only on the 228-stock universe show PF > 1.0?
   * Yes (PF ≥ 1.3): V1-long-only is the lowest-risk, lowest-cost
     deploy. Carry into Phase A candidate decision.
   * Marginal (1.0 ≤ PF < 1.3): hold to V4 candidate if V4 with
     xgboost held.
   * Negative (PF < 1.0): the slot-1 already-shipped candidate is
     itself questionable. Major flag.
2. **June 8 deploy plan write-up.** Produce
   `docs/phase_a_deploy_plan_2026-06-08.md` with:
   * Chosen candidate.
   * Pre-committed kill criteria (K1–K5).
   * Deploy mechanics (single-config-edit vs slot consumption).
   * Rollback procedure (the exact `git revert <hash>` for each
     potential change).
3. **Sprint synthesis** — append a `## Sprint outcome` section to this
   file with hypothesis-by-hypothesis verdicts.

---

## Hypothesis detail (long form)

### H1 — Regime classifier inputs are stale or wrong-scale

**Claim:** The trader VM has been classifying `bear_high_vol` for most
of Week 2 (2026-05-13 → 2026-05-23, 28 trades, 100% short). The
classifier reads `india_vix` and `nifty_trend` from
`trading_agent._market_context`. If either input is stale (cached too
long) or on the wrong scale (VIX in basis points vs percent, or Nifty
1y EMA instead of 200-day EMA), the regime label could be wrong, and
the strategy weights would route into the wrong side of the book.

**Why suspected:** The 5/25 90d × 228 battery showed the **same
strategy mix** generates +₹556 on the LONG side and −₹379 on the
SHORT side. The live engine has 100% routed into the LOSING side. The
only branching point between long and short routing in the strategy
layer is the regime classification + ensemble weights for that regime.
If the regime label is wrong, the ensemble weights pick the wrong
strategies.

**Test (now live):** Every `classify_regime` call now emits
`[REGIME-INPUT] nifty_trend=<int> india_vix=<float> high_vol=<bool> -> regime=<label>`.
On Thu we cross-check the logged `india_vix` against a third-party
source. We also count regime-label flip-flops vs steady-state to detect
"flapping" (frequent label changes during a single session, which
would indicate the inputs are themselves noisy).

**Status as of 11:05 IST today:** DEPLOYED. First sample line in the
trader log:
```
2026-05-27 11:05:22 | INFO | [REGIME-INTRADAY-INPUT] nifty_intraday_pct=0.178 vix_intraday_delta=-0.455 -> regime=neutral
```

The daily `[REGIME-INPUT]` will fire on the next regime-eval pass
(cached behind the per-cycle market-context refresh).

---

### H2 — Long-side famine is xgboost-model bias, not signal availability

**Claim:** xgboost is the only strategy that produced LONGs (3 of them,
on 2026-05-26). The other 5 strategies emitted exclusively HOLD or
SHORT in long-only-conducive setups. The famine is therefore not a
"the data didn't show longs" problem; it is a "the *models* didn't
emit longs" problem.

**Why this matters for the candidate decision:** If the long-side
edge in the V1 90d × 228 battery (+₹556 over 122 trades) was driven
mostly by mean-reversion BUYs and rsi-momentum BUYs, but the live
engine emits ~zero such signals (because the regime weights suppress
them in bear_high_vol), the V1 long-only candidate is *not* going to
produce that edge live until the regime label changes.

**Test (Day 2):** Cross-tabulate signal_audit CSV by (strategy, side,
regime) for the May 2026 window. The 90d battery has the same
breakdown (`results/battery_nifty500_90d/V1_baseline_current_shipped/per_trade.csv`).
If live SHORT/LONG mix on bear_high_vol is dramatically different from
backtest, the difference identifies the gap.

**Status:** OPEN. To be analysed Thu.

---

### H3 — V4 threshold-3% on Nifty 50 is the right candidate

**Status: PARTIALLY CONFIRMED.** From `findings_log_2026-05-25.md §2`:

> V4 (`trend_filter_pct: 3.0` on all six strategies) is the ONLY
> profitable variant on Nifty 50 60d. PF 1.35, balanced long/short
> (59% long / 41% short), both sides positive.

**Remaining question (the load-bearing one):** Does V4 hold with
xgboost active (Bug H fix)? The backtester is currently re-running
V1–V19 with xgboost enabled via the bind-mount fix. V4 is the 4th in
the queue. Expected landing: Thursday 2026-05-28 ~15:30 IST per the
backtester-status probe steady-state of 16 ev/s.

**Decision-affecting outcomes** (advisor memo §rec 2):

* PF ≥ 1.30 → H3 fully confirmed. V4 is the candidate.
* 1.0 ≤ PF < 1.30 → H3 weakened. V4 still viable but V1-long-only
  becomes the lower-risk alternative.
* PF < 1.0 → H3 refuted. V4 candidate killed.

---

### H4 — Short side has structurally negative edge regardless of tuning

**Status: CONFIRMED** by the 90d × 228 stock battery
(`findings_log_2026-05-25.md §6`). V1 SHORT side: −₹379 on 156 trades
(−₹2.43 avg). V2 (filters off) SHORT side: −₹398 on 183 trades
(−₹2.18 avg). The number of trades changes by ~17%; the average outcome
per trade stays in the same statistical neighbourhood. This is the
empirical basis for slot-1 (`risk.allow_shorts: false`).

**No further action required.** Decision-affecting verdict baked into
the slot-1 deploy.

---

### H5 — Bug H: xgboost was silently OFF in battery

**Status: CONFIRMED + FIXED.**

* Confirmed: `findings_log_2026-05-25.md §16`. The battery containers
  did not have `models/` bind-mounted; `xgboost_classifier.is_healthy()`
  returned False on every variant, silently dropping xgboost from the
  ensemble.
* Fixed: commit `73c26bf` added `-v ${TRADER_HOME}/models:/app/models:ro`
  to `tools/run_battery_queue.py::build_docker_run_argv` and to
  `tools/cloud/launch_battery.sh`, plus the regression test in
  `tests/unit/test_battery_queue_scheduler.py`.
* Re-validation: V1+V2 of the Nifty-50-60d run are being re-executed
  with xgboost active. Output lands Thu ~15:30 IST (H3 above).

**No further action required.**

---

### H6 — Bug I: trader VM divergence from main for ~2 weeks

**Status: CONFIRMED + FIXED.**

* Confirmed: `findings_log_2026-05-25.md §17`. Trader was at
  `868d5ad` (2026-05-19) with 5 modified-tracked files + several
  untracked production artifacts.
* Fixed: operator manual rebuild on 2026-05-26 14:37 IST. Trader HEAD
  advanced to `73c26bf` (and now `e1df9e8` after today's pull).
* Closure verdict: `findings_log_2026-05-27.md §3`. The 5 hot-fixes
  are operationally real but **strategy-neutral**. The live trade
  record from May 13 → May 25 is therefore valid evidence about
  freeze-v2.1's strategy behaviour. (Answers the advisor memo's
  "concrete question" §3.)

**No further action required.**

---

### H7 — First long-only session 2026-05-27 produces different signal mix

**Claim:** With `allow_shorts: false`, the risk manager will reject
all SHORT-side entries. The downstream effect on the ensemble should
be a per-cycle shift in the strategy-vote distribution: SHORT-tilted
strategies (e.g. supertrend_follow in a bear regime) will have their
votes filtered post-aggregation, freeing slot space for strategies
that *do* emit LONG candidates.

**Test (Day 2):** Parse `signal_audit_2026-05-27.csv` and
`trading_agent_2026-05-27.log`. Count:
* Per-strategy candidates per cycle (LONG/SHORT/HOLD breakdown).
* Filter pass rate (candidate → confirmed-by-ensemble → confirmed-by-
  risk-manager). The risk-manager filter on SHORT is the key new gate.
* Compare to 2026-05-26 (last day with `allow_shorts: true`) and
  2026-05-22 (Friday last week — most recent comparable long-side
  base rate).

**Status:** OPEN.

---

### H8 — xgboost long-side calibration may have drifted

**Claim:** The model's only 3 LONG trades produced 3 stop-outs (0/3
WR) on 2026-05-26 vs a backtested base rate of 52.8% (V4) or ~56% (V2
90d battery). A 0/3 outcome from a 52.8% base rate has prior
probability ~10.5%. Not refutational on its own, but worth checking.

**Hypothesis:** The model's `P(LONG)` distribution has *shifted right*
(model is over-confident on longs in the current regime), or the
features feeding the model are in a different statistical regime than
the training set (covariate shift). Either would produce confident-
but-wrong LONG signals.

**Test (Day 4):** Re-run the model's training-validation histogram on
the current production data. Compare empirical CDF of `P(LONG)`
predictions in production vs validation.

**Status:** OPEN. Deferred to Sat to keep the small evidence base
intact (we need more than 3 LONG trades to draw any signal).

---

### H9 — Intraday regime overlay has been live since 2026-05-17 but never observed

**Claim:** `classify_intraday_regime` (lines 226–291 of
`packages/core/regime.py`) was added during the P2 logic-edges
audit on 2026-05-17 (commit `a3145c8`). The function is called by
the trading agent on every cycle to produce a per-cycle overlay on
the daily regime. But until commit `e1df9e8` today, the function
emitted no observability — its output was used by downstream
risk-manager logic but never logged. The advisor memo flagged the
audit-only nature of P2's commit as exactly the kind of bypass-abuse
pattern the contract was designed to discourage.

**Test (now live):** `[REGIME-INTRADAY-INPUT]` log line on every
call. First sample today at 11:05:22 IST showed
`nifty_intraday_pct=0.178 vix_intraday_delta=-0.455 -> regime=neutral`,
which is a sensible reading (small up move, VIX cooling) and matches
the day's tape.

**Status: DEPLOYED.** Same as H1.

---

### H10 — V4's PF 1.35 on Nifty 50 may not transfer to the 228-stock universe

**Claim:** V4 was tested on Nifty 50 (large caps only). The live agent
trades a 228-stock universe weighted toward mid-caps. The same
trend-filter threshold applied to mid-caps may produce a different
filter-pass rate and a different long/short balance.

**Test (Day 4):** Queue `V20_nifty500_v1_long_only_60d` overnight.
Universe: live 228-stock list. Long-only entries. 60-day window.
Compare resulting PF + per-side breakdown to V1 from the existing
90d × 228 run (re-baselined for the 60d window).

**Status:** OPEN. Deferred to Sat because the backtester is fully
consumed by the H3 validation run until ~Thu 15:30 IST.

---

## Constraint enforcement (freeze policy)

Every sprint patch is one of:

1. **Observability only.** Adds `logger.info(...)` calls; does not
   change any branch decision, return value, or persisted state. The
   `FREEZE_v2.1.md` "What is NOT frozen" section explicitly permits
   this: "Observability — additional logging, metrics, audit fields,
   alert routing."
2. **In-place edit of an already-slotted config value.** E.g. flipping
   `risk.allow_shorts` from `true` to `false`. The *addition* of the
   key consumed the slot on 2026-05-26; the *value flip* (or future
   value flips) are config edits, not new bypasses.
3. **Operator-tool / bootstrap-script fixes.** E.g. Bug J permanent
   fix queued for Day 4. These are not in the frozen surface
   (`packages/strategies/`, `packages/core/risk_manager.py`,
   `packages/core/position_sizer.py`, `config.yaml` strategy + risk
   blocks, `models/xgboost_model.pkl`).

**No sprint action consumes a freeze-bypass slot.** Current slot
ledger remains `slot-1 LIVE+DURABLE; slot-2 LIVE (audit-only candidate);
slot-3 LIVE (audit-only candidate)`. The advisor's recommendation to
reclassify slots 2 + 3 to audit-only is a *Friday review* decision,
not a sprint-day decision.

---

## Cross-references

* `docs/findings_log_2026-05-25.md` — sections 1–17 (the evidence base
  for H3, H4, H5, H6).
* `docs/findings_log_2026-05-27.md` — sections 1–4 (today's findings;
  this sprint doc is referenced from §4 there).
* `docs/FREEZE_v2.1.md` — slot ledger + frozen-surface definition.
* `docs/changes_done_2026-05-27.md` — the formal audit-fix sweep (38
  items); orthogonal to this sprint.
* `docs/findings_2026-05-27.md` — F-01..F-108 audit findings catalogue;
  also orthogonal.

---

## Sprint outcome (to be filled Sun 2026-05-31)

_TBD — append at end of sprint._
