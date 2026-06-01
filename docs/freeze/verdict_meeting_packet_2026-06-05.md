# Verdict-Meeting Packet — 2026-06-05

> **Filed:** 2026-06-01 12:00 IST. Read this aloud on Friday in the order
> the sections appear.
>
> **Purpose:** consolidate the v2.1 / v3 wind-down vs activation decision
> evidence chain produced between 2026-05-29 (criteria pre-commit) and
> 2026-06-01 (CHG calibration + V25/V26 true re-sim). This is **not** a
> position statement; it is the evidence packet against which the
> pre-committed mechanical criteria are applied.
>
> **Verdict-meeting failure mode to watch:** if the agent (or anyone
> reading) finds themselves constructing reasons to NOT pick wind-down
> when the data points there, re-read
> [`wind_down_criteria_2026-06-05.md` §5](wind_down_criteria_2026-06-05.md).

---

## 0. One-page TL;DR

| Question | Pre-committed threshold | Measured | Verdict |
|---|---|---|---|
| **T1** — Is `xgboost_classifier` net-positive at slot-#4 rates? | V15 PF ≥ 0.90 = keep · < 0.90 = retire | V15 PF 0.944 at Zerodha rates → **0.386 at AngelOne rates** (per [`charges_pf_adjustment`](../findings/charges_pf_adjustment_2026-06-01.md)); regardless of rate set, T1 is in the "retire" band under AngelOne and the "weak" band under Zerodha | **RETIRE xgboost_classifier** (pre-committed) |
| **T2** — Is the H3 entry-lag forensic actionable? | Median lag < 30 s = healthy · 30-120 s = pilot PERF · > 120 s = deploy PERF under new freeze | **Deliverable not produced** (operator did not run the H3 forensic between 2026-05-29 and 2026-06-01) | **Defer to T3** (which is reached on T2 silence) |
| **T3** — Is the experiment concluded? | If by 2026-06-08 **both** (a) no PERF-paper window achieves rolling PF ≥ 1.20 over 5 days, and (b) no H3 / H1 finding names a single bug whose fix could move PF above 1.0 — then declare experiment concluded | (a) No PERF paper window was run. (b) The CHG sweep retroactively re-prices the entire backtest suite; the strongest v3 candidate (V25 with shorts) re-prices from PF 0.23 to **PF 0.04** and MaxDD 30% → **76.6%**. The capital-cap-loosening V26 re-prices to **PF 0.01 / MaxDD 82.3%**. No single bug exists whose fix moves either above 1.0 | **TRIGGERED → wind-down** |
| **NEW evidence (post-2026-05-29 pre-commit)** | n/a | CHG-01..CHG-05 in `packages/core/charges.py` corrects a Zerodha-vs-AngelOne calibration error that the live trader has carried since 2026-05-08 (24 trading days). Every historical PF in the verdict-meeting packet is optimistic by ~50-80% under the corrected charges. | Reinforces wind-down |

**Mechanical reading:** T1 retire, T3 trigger, no defender for the "defer to V26 / v3.1" objection. **Wind down v2.1; activate v3 Phase A only if the charter §1 finding #4 economic case can be re-stated at AngelOne rates and still hold up — it cannot.**

---

## 1. The pre-committed wind-down criteria (read first)

Cross-ref: [`wind_down_criteria_2026-06-05.md`](wind_down_criteria_2026-06-05.md).

Reproduced inline for the meeting:

> **T3 — Wind-down trigger.** If by 2026-06-08 **both** of the following are true:
>   * (a) No PERF-fix paper window achieves rolling **PF ≥ 1.20 over 5 trading days**.
>   * (b) No H3 / H1 finding names a single bug whose fix could plausibly move PF above 1.0.
>
> Then: **declare the experiment concluded.** Final post-mortem. No further engineering investment.

The packet below establishes both (a) and (b).

---

## 2. The headline number set — V25 and V26 at AngelOne rates

> Source: `logs/backtests/battery_chg_recompute_20260601T114500/comparison.md`
> (battery run completed 2026-06-01 11:50:16 IST, using
> `packages/core/charges.py` commit `e277e21` — AngelOne calibration).

| Variant | PF | Trades | WR% | MaxDD% | Ret% | Sharpe | Provenance |
|---|---:|---:|---:|---:|---:|---:|---|
| **V25-Zerodha** (historical baseline) | 0.23 | 189 | ~5 | ~30 | -41 | (recorded) | `logs/backtests/battery_v3_swing_a5_v25_shorts_20260530T090709/comparison.md` |
| V25-AngelOne post-hoc | 0.05 | 189 | — | — | — | — | `docs/findings/charges_pf_adjustment_2026-06-01.md` (paper re-pricing of the May 30 trades) |
| **V25-AngelOne true re-sim** | **0.04** | 190 | **2.1** | **76.6** | **-76.6** | -8.3 | `logs/backtests/battery_chg_recompute_20260601T114500/comparison.md` |
| **V26-AngelOne true re-sim** | **0.01** | 195 | **1.0** | **82.3** | **-82.3** | -8.2 | same |

**Plain-English reading:**

- V25 was the single best v3 swing variant — `trend_pullback` + `breakout_20d` with shorts allowed, the explicit test of "what if we let the strategy take the side that was vetoed in v3 Phase A". Its 2026-05-30 Zerodha-rate reading was PF 0.23, which charter §1 finding #4 used to project ₹250-700/month seed-phase income at AngelOne (the calibration error that CHG corrects).
- At the broker's actual rate card, **V25 bleeds 76.6% of capital over the 600-day window** — three out of every four rupees the operator deploys at the strategy disappear into the cost structure. The "₹250-700/month income" projection becomes **₹0/month with strong negative tail risk**.
- V26 was added to the catalogue specifically to close Session 3's "position-cap dilution" objection (the assertion that V25's PF 0.23 was an artefact of the 5-position live cap and would relax at a 15-position cap). **V26 is worse, not better.** PF 0.01 < V25's 0.04, MaxDD 82.3% > 76.6%. The position-cap thesis is decisively refuted: more positions = more losing trades × AngelOne's per-trade cost floor.

**This number set satisfies T3(b):** no single bug exists whose fix could move either V25 or V26 above PF 1.0. The cost structure is the cost structure; the strategy edge is missing.

---

## 3. Why the rate set changed — CHG-01..CHG-05

Cross-ref: [`findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md)
+ [`changes_done_2026-06-01.md`](../changes/changes_done_2026-06-01.md) Phase 1.

The trader switched brokers from Zerodha to AngelOne on **2026-05-08** (24 trading days before this meeting). `packages/core/charges.py` was not updated at the same time. The strategy backtests and the live PnL ledger have therefore been computing charges at Zerodha rates while the actual broker has been charging AngelOne rates. The five findings:

| ID | Discrepancy | Per-trade impact |
|---|---|---|
| CHG-01 | Intraday brokerage 0.03% → **0.1%** (lower of ₹20 / 0.1%, min ₹5) | ~₹15 extra |
| CHG-02 | Delivery brokerage 0% → **0.1%** | ~₹15 extra |
| CHG-03 | Stamp duty buy: uniform 0.003% → **0.003% intraday / 0.015% delivery** | ~₹0.50 extra for delivery |
| CHG-04 | DP charge ₹13.5 → **₹20** | ~₹6.50 extra per sell-side delivery |
| CHG-05 | No daemon-start logging of active rates → critical observability gap | n/a, observability fix |

Aggregate impact per trade: **~₹20 extra** that the backtester (and the live PnL ledger) was not accounting for. Over 189 V25 trades that compounds into **~₹3,700 unaccounted charges** — paper-arithmetic adjustment. Under true re-sim with compounding it's worse because each unaccounted-for cost shrinks the capital base used to size the next trade. The true re-sim's PF 0.04 vs the post-hoc estimate's 0.05 is the compounding gap.

**For the live PnL ledger:** the operator's cumulative reading of **₹-1,212 since broker switch** is silently optimistic by ~₹20-25 × N live trades. Direction-only correction: actual cumulative loss is closer to **₹-1,500 to ₹-1,700**.

---

## 4. The strategy-reference calibration

Cross-ref: [`strategy_reference_review_2026-06-01.md`](../reviews/strategy_reference_review_2026-06-01.md)
(filed 2026-06-01 ~11:10 IST after the operator surfaced four institutional-quant references for cross-comparison).

Honest retail-trend benchmark: **~3-7% CAGR** (SG Trend Index 2000-2026 net), with 2011-2019 being 8 years flat. The four strategies the operator was reading as "proofs that algo trading at retail can outperform" (Virtu, Renaissance/Medallion, Bridgewater All-Weather, Two Sigma equity stat-arb) are all **structurally unavailable** at the operator's capital base and infrastructure: HFT-class latency, $5-10B leverage capacity, institutional borrow-and-short networks, or 100-Bps-edge-on-1-second-holds market-making — none of which transfer.

The v3 charter §1 finding #4's "5-15% commission drag" advantage rested on charges being computed at Zerodha rates. **At AngelOne rates the advantage evaporates**, and v3 is no longer structurally different from the institutional trend-following benchmarks that already exist — it is the same strategy with worse capacity and a worse cost base. The strategy-reference review's verdict: **even a positive V26 result would only validate the symmetry question, not the broader "is this strategy retail-viable at AngelOne costs" question.** V26 is decisively negative, so even that narrow validation is unavailable.

---

## 5. The brutal-review evidence chain

Cross-ref: [`brutal_review_2026-06-01.md`](../reviews/brutal_review_2026-06-01.md) (both sessions, 09:48 IST and 11:35 IST)
+ [`brutal_review_2026-05-30.md`](../reviews/brutal_review_2026-05-30.md) (three sessions, predecessor).

Key findings carried into this meeting:

| Finding | Status at meeting |
|---|---|
| §1 — Uncommitted CHG diff weakening verdict-meeting framing | **CLOSED** — diff committed in `e277e21` chain; CHG numbers now quoteable. |
| §2 — V26 never run despite 2026-05-30 catalogue addition | **CLOSED** — first execution today, results in §2 of this packet. Position-cap-dilution objection refuted. |
| §3 — Bug O pytest → production-log leak | **DEFERRED** — dev-time noise, post-verdict cleanup. Not verdict-meeting blocking. |
| §4 — Audit checkpoint cadence outage | **SELF-RESOLVED** — 4 hourly checkpoints today (09:00 / 10:01 / 11:00 / 11:36); cadence healthy. |
| §5 — Kill PID 7 (66+ hour stale daemon) | **SELF-RESOLVED** — daemon restarted at 11:34:36 IST; new PID 6 booted with CHG-01..CHG-05 active (verified via `_log_active_rates` startup log line). |
| §6 — DB-CSV blindspot (7 missing rows) | **NOTED** — verdict-meeting reads `logs/trades.csv` directly; DB diagnostic incomplete. Backfill is a post-verdict task. |
| §7 — Trader-VM deploy status | **CONFIRMED untouched** — `tools/cloud/deploy.ps1` not invoked today per freeze charter §6.1. Live trader runs pre-Saturday code. The CHG fix is NOT on the trader VM; live broker orders since 2026-05-08 have been silently more expensive than the daemon's internal PnL ledger believes. |

---

## 6. The "what about deferring?" defenders that have been considered

The pre-committed criteria sheet §4 enumerates anti-temptation defences. Each post-2026-05-29 candidate "defender" is also addressed:

| Defender | Why it's not a defender |
|---|---|
| "V26 might have edge at higher cap; defer" | V26 PF 0.01 < V25 PF 0.04. Closed in §2 above. |
| "v3.1 candidate (symmetric trend_pullback_short) might work; defer" | Strategy-reference review §What-this-means: trend-following crisis alpha works on 20-40 instrument diversified futures baskets, not 30-Nifty-equity baskets. v3.1 would at best be a marginal directional refinement on an already-limited universe. Cross-ref §4 above. |
| "Maybe charges.py rewrite is wrong; revert" | CHG-01..CHG-05 evidence is the AngelOne brokerage page itself, cross-referenced against the AngelOne calculator's worked example (`test_angelone_intraday_example_50_at_1000` in `tests/unit/test_charges_angelone_2026_06_01.py`). The diff also makes the live daemon's `[charges] active rates: broker=AngelOne ...` startup log line agree with the broker statements the operator receives by email — a check the operator can do directly today. |
| "PERF fixes (entry lag) might rescue PF; defer" | T2 was the gate for that and was not produced. The pre-committed criteria sheet explicitly routes T2 silence into T3 trigger. |
| "Maybe yfinance data has drifted between 5/30 and 6/01; the 0.23→0.04 is partly artifact" | The 5/30 V25 fetch and the 6/01 V25 fetch differ by 3 trading days of new data + retroactive split/dividend adjustments. Plausible noise magnitude: ±5-10% on PF. **Observed delta: 82% PF compression.** Off by an order of magnitude. |
| "Wind-down is irreversible; one more week of data won't hurt" | Trader VM is still running pre-Saturday code. Every additional day of live operation accrues silently-optimistic ledger entries and real-broker-charged costs. The operator's net loss is bleeding ~₹20/trade × N trades/day at unaccounted rates. The cost of "one more week" is measurable and growing. |

---

## 7. Recommended verdict — applied mechanically

Per [`wind_down_criteria_2026-06-05.md` §1 T3](wind_down_criteria_2026-06-05.md):

> If by 2026-06-08 **both** (a) no PERF-paper window achieves rolling PF ≥ 1.20 over 5 days, **and** (b) no H3 / H1 finding names a single bug whose fix could plausibly move PF above 1.0 — then declare experiment concluded.

- **(a)** No PERF-paper window was run (the operator's choice — neither right nor wrong, just the state of evidence).
- **(b)** The CHG sweep is itself the strongest possible negative answer to (b): it identifies a 5-defect cluster whose fix moves the strongest variant **from PF 0.23 to PF 0.04**. There is no single bug whose UNDOING this fix would move PF above 1.0 — the fix is correctness, not pessimism.

**Mechanical verdict: T3 TRIGGERS. Wind down v2.1.**

Per [`freeze_v3.0_charter_2026-05-30.md`](freeze_v3.0_charter_2026-05-30.md) §1 finding #4:

> v3 Phase A activation rests on the cost-regime thesis being correct.

The cost-regime thesis as written is incorrect at the operator's broker. Re-stating it at AngelOne rates: ₹0-500/month seed-phase income with PF 0.04 swing-strategy ceiling. **v3 Phase A activation cannot be defended without first re-deriving the economic case at AngelOne rates — and the re-derivation eliminates the case.**

**Recommended sequence post-verdict:**

1. **2026-06-05 EOD** — operator places `logs/STOP` file in trader-VM repo (or invokes `tools/close_position.py` if any open positions). Daemon flushes positions and halts on next cycle.
2. **2026-06-06 (Sat)** — operator decides whether to (a) keep the daemon down indefinitely, (b) run a paper-mode-only daemon for ongoing research, or (c) deploy the CHG fix and resume live only if a NEW edge thesis is identified outside of the current strategy set.
3. **2026-06-08 (Mon)** — final post-mortem document; archive `FREEZE_v2.1.md` as concluded; archive `freeze_v3.0_charter_2026-05-30.md` as "never activated, see verdict packet 2026-06-05".

If, however, the operator does NOT wish to apply the criteria mechanically, the failure mode flagged in `wind_down_criteria_2026-06-05.md` §5 should be re-read aloud at the meeting.

---

## 8. Appendix — the artefact list (what's on disk)

For verdict-meeting cross-reference. Every claim in this packet is rooted in one of these files.

### A. Pre-committed criteria + decision framework
- [`docs/freeze/wind_down_criteria_2026-06-05.md`](wind_down_criteria_2026-06-05.md) — the locked gate sheet (this packet's source of authority).
- [`docs/freeze/freeze_v2.1_exit_criteria_2026-06-05.md`](freeze_v2.1_exit_criteria_2026-06-05.md) — the operating contract through 2026-06-05.
- [`docs/freeze/freeze_v3.0_charter_2026-05-30.md`](freeze_v3.0_charter_2026-05-30.md) — the would-be successor; §1 finding #4 is the v3 economic-case anchor.
- `FREEZE_v2.1.md` (repo root) — frozen file list. `packages/core/charges.py` is NOT enumerated; CHG diff freeze-safe.

### B. Today's CHG ledger
- [`docs/findings/findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md) — CHG-01..CHG-05 + NUM-10.
- [`docs/findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md) + `.csv` — per-variant post-hoc PF adjustment across all 80 backtest variants.
- [`docs/changes/changes_done_2026-06-01.md`](../changes/changes_done_2026-06-01.md) — single-source-of-truth ledger of what landed today.
- `tools/audit/charges_pf_adjustment_2026_06_01.py` — the post-hoc adjustment script.
- `tests/unit/test_charges_angelone_2026_06_01.py` — 22 regression pins on the new rate set.

### C. Today's battery re-sim
- `logs/backtests/battery_chg_recompute_20260601T114500/comparison.md` — V25 + V26 at AngelOne rates (headline numbers in §2 above).
- `logs/backtests/battery_chg_recompute_20260601T114500/results/V25_swing_combined_shorts.json` — per-trade detail for V25.
- `logs/backtests/battery_chg_recompute_20260601T114500/results/V26_swing_combined_shorts_high_cap.json` — per-trade detail for V26.
- `logs/backtests/battery_v3_swing_a5_v25_shorts_20260530T090709/comparison.md` — the V25-Zerodha historical baseline for comparison.

### D. Adversarial review record
- [`docs/reviews/brutal_review_2026-06-01.md`](../reviews/brutal_review_2026-06-01.md) — morning session (09:48 IST) + integration session (11:35 IST).
- [`docs/reviews/brutal_review_2026-05-30.md`](../reviews/brutal_review_2026-05-30.md) — three-session predecessor (00:48 / 11:07 / 14:47 IST).
- [`docs/reviews/strategy_reference_review_2026-06-01.md`](../reviews/strategy_reference_review_2026-06-01.md) — honest expectation calibration vs the four reference strategies the operator surfaced.

### E. Operational state (verdict-week observability)
- `logs/audit/2026-06-01/checkpoint_0900.md` / `_1001.md` / `_1100.md` / `_1136.md` — hourly cadence verified healthy.
- `logs/audit/2026-06-02..2026-06-04/` — to be auto-generated daily; cadence is healthy per the above.
- `logs/health.json` — live daemon state (currently PID 6, paper mode, restarted 11:34:36 IST on CHG code).
- `logs/daemon_2026-06-01.log` — startup line `[charges] active rates: broker=AngelOne ...` confirms `_log_active_rates()` is firing on the local paper daemon.
- `logs/trades.csv` — 7 rows ahead of `data/trading_agent.db.trades` (DB-CSV blindspot from brutal-review §6; reading CSV directly for verdict purposes).

### F. Historical decision docs (with CHG footnotes)
- [`docs/diagnoses/v3_phase_a5_forensic_2026-05-30.md`](../diagnoses/v3_phase_a5_forensic_2026-05-30.md) — the forensic that produced V25's PF 0.23 reading (now footnoted with the AngelOne re-derivation).
- [`docs/reviews/friday_review_2026-05-29.md`](../reviews/friday_review_2026-05-29.md) — the previous Friday's framing (footnoted).

---

> _End of packet. The mechanical reading is wind-down. The criteria
> sheet §5 says re-read §5 if I find myself constructing reasons to not
> pick wind-down. I have not._
