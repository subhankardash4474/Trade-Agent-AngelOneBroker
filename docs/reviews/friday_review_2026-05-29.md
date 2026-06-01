# Friday review — 2026-05-29

**Status:** Draft prepared 2026-05-28; verdicts appended
2026-05-29 14:08 IST after slot #3 V15 result landed (V15
result is the decisive cell). Section 10 below is the
landed-data verdict; everything above stays as the original
draft for traceability.

> **CHG-charges note (added 2026-06-01, historical-record footnote).**
> Every PF / WR / PnL number cited in this review was measured against
> the pre-CHG Zerodha-calibrated charges model. On 2026-06-01 the model
> was corrected to AngelOne's actual rates (CHG-01..CHG-05) — see
> [`../findings/findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md)
> and the per-variant adjustment in
> [`../findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md).
> The corrected numbers tighten every v2.1 variant PF in this review
> (e.g. V1 baseline shipped: pre-CHG PF 0.78 → post-CHG PF 0.32; V15:
> pre-CHG 0.94 → post-CHG 0.39). **No variant flips from PF < 1 to PF ≥ 1
> under the correction; the directional verdict of this review is
> strengthened, not changed.** The original numbers are preserved below
> as the as-of-2026-05-29 historical record.

**Audience:** operator + advisor reviewing whether the freeze-v2.1
diagnostic sprint has produced enough evidence to (a) promote a new
live config, (b) consume slot-3 of the bypass ledger on a retrained
model, and (c) restart capital deployment after the 2026-05-26 famine
break.

---

## TL;DR

1. **No variant in 240 backtests of the freeze-v2.1 4-strategy
   ensemble is profitable on the live 232-stock universe.** Best
   (V4, threshold tightened from 5% to 3%) is PF 0.84, -₹489 over
   60d, MaxDD 7.99%. Better than baseline (V1: PF 0.78, -₹693) but
   still net negative.

2. **V15_mr_xgb_only is the ONLY profitable variant in the entire
   suite** (PF 1.02, +₹10 over 60d on 50 stocks, MaxDD 1.92%). The
   recipe: drop everything except mean-reversion + the *broken* XGBoost
   classifier. This is counter-intuitive given §5's "XGB is broken"
   verdict and **reframes the slot-3 retrain priority from QUEUED to
   HIGH** -- but only if V15 stays positive on the 232-stock universe
   (data lands Friday morning from slot #3 of the backtester queue).

3. **V16_completely_naked confirms the gates earn their keep.**
   Without any filter: -40.48% return, MaxDD 40.57%, 1647 trades over
   60d on 50 stocks. The current filter stack is NOT the source of the
   live losses -- it is actively preventing much larger ones.

4. **Slot #3 of the queue is NOT a holdout p-hack guard** (it was
   intended to be one). `findings_log_2026-05-27.md` §9 documents
   **Bug K**: the `--holdout-window-days` flag is silently ignored in
   the parallel-worker path -- the cache is saved before the slice
   runs, workers reload pre-slice data. Slot #3 is reframed as a
   "wider variant sweep on 232 stocks" (which is still useful for
   cross-universe transfer and for testing V15 on the big universe).
   A real walk-forward / p-hack test requires fixing Bug K first
   (queued for post-Friday week, ~30 min work).

5. **V18 anomaly: V18_long_only_threshold_3pct produces identical
   trades to V2_all_filters_off on the 232-stock universe (266 trades,
   -₹981) but to V4_threshold_3pct on the 50-stock universe (27 trades,
   -₹44).** The 3% threshold appears to disappear on the bigger
   universe -- a separate config-override merge bug, decision-affecting
   for any "tighten threshold" candidate. Investigation queued.

---

## 1. Evidence base

### 1.1 Three completed backtest jobs

| Slot | Run ID | Universe | Days | Variants | Status |
|------|--------|----------|------|----------|--------|
| #1 | `battery_nifty50_60d_20260527T065700` | 50 stocks (Nifty50) | 60 | 19 | DONE Wed evening |
| #2 | `battery_nifty500_v4_long_only_validation_60d_20260527T142630` | 232 stocks (v4 universe) | 60 | 6 | DONE Thu early morning |
| #3 | `battery_v2_holdout_30d_20260528T011921` | 232 stocks | 60 (advertised as 30-holdout; see §4 Bug K) | 19 | IN-PROGRESS, V3 at ~47% as of Thu 12:11 IST, ETA Sat ~18:00 IST |

Total: 44 variant×universe combinations completed, plus 17 more from
slot #3 by Friday morning (V1-V14 confirmed; V15+ pending V3 finish).

### 1.2 Cross-universe consistency (V1 + V2 on 50 vs 232 stocks)

| Variant | Slot #1 (50 stocks) PnL / PF / MaxDD | Slot #2 (232 stocks) PnL / PF / MaxDD |
|---------|----:|----:|
| V1_baseline_current_shipped | -₹67 / 0.76 / 1.42% | -₹693 / 0.78 / 8.77% |
| V2_all_filters_off | -₹128 / 0.58 / 1.68% | -₹981 / 0.69 / 11.21% |
| V4_threshold_3pct | -₹44 / 0.85 / 1.11% | -₹489 / 0.84 / 7.99% |
| V17_long_only_shipped | -₹67 / 0.76 / 1.42% | -₹693 / 0.78 / 8.77% |
| V18_long_only_threshold_3pct | -₹44 / 0.85 / 1.11% | **-₹981 / 0.69 / 11.21%** ← anomaly |
| V19_long_only_filters_off | -₹128 / 0.58 / 1.68% | -₹981 / 0.69 / 11.21% |

Reading rules:
* Trades scale ~5x across universes (50 → 232 stocks). PnL also scales
  ~5-10x, MaxDD ~7x. Direction (negative) and rank order (V4 best)
  are stable across universes -- good cross-universe transfer.
* V17 = V1 and V19 = V2 on BOTH universes -- confirms the
  long-only filter is a no-op on the freeze-v2.1 strategy stack
  (because `allow_shorts: false` is already live, see §2 of
  findings_log_2026-05-27.md).
* V18 = V4 on 50 stocks but V18 ≠ V4 on 232 stocks. The 3% threshold
  fires on the 50-stock universe (27 trades) but the same variant on
  232 stocks runs as if `threshold=5%` (266 trades, matching V2).
  See §6 V18 anomaly.

---

## 2. Variant ranking — top 5 from each slot

### 2.1 Slot #1 (50 stocks, 60d, 19 variants)

By PF, descending:

| Rank | Variant | Trades | WR% | PnL | PF | MaxDD% | Ret% |
|-----:|---------|------:|----:|----:|----:|------:|-----:|
| 1 | **V15_mr_xgb_only** | 56 | 50.0 | **+₹10** | **1.02** | 1.92 | **+0.10%** |
| 2 | V10_confidence_060 | 26 | 42.3 | -₹29 | 0.88 | 1.07 | -0.29% |
| 3= | V4_threshold_3pct | 27 | 40.7 | -₹44 | 0.85 | 1.11 | -0.44% |
| 3= | V18_long_only_threshold_3pct | 27 | 40.7 | -₹44 | 0.85 | 1.11 | -0.44% |
| 5 (cluster) | V1=V5=V11=V12=V13=V14=V17 | 29 | 41.4 | -₹67 | 0.76 | 1.42 | -0.67% |
| ... | (V2 / V3 / V6 / V7 / V8 / V9 / V19 in -₹106 to -₹128 range) | | | | | | |
| 19 | **V16_completely_naked** | **1647** | 29.8 | **-₹4055** | **0.34** | **40.57** | **-40.48%** |

**Reading:**
* The freeze-v2.1 4-strategy ensemble is *intrinsically losing on this
  universe & window* even at its best (V10/V4 both PF < 0.9). The
  ensemble's edge is, charitably, marginal.
* V15 is an outlier: it ditches 4 of the 5 strategies (rsi, vwap,
  orb, supertrend) and runs ONLY `mean_reversion + xgboost_classifier`
  with reduced filters. It has the highest WR (50%) and the only
  positive PF/PnL/Sharpe in the entire 19-variant suite. Discussed
  in §3 below.
* Confidence threshold dialled DOWN from 0.7 to 0.6 (V10) outperforms
  the shipped 0.7 (V1) by ₹38. Trade count drops slightly (29 -> 26)
  because the regime-classifier-trusted signals get more weight while
  some marginal cross-signal trades drop out -- net PF improves.
* V11 (confidence 0.5) does WORSE than V10 (0.6) -- so lower isn't
  monotonically better; there's a sweet spot near 0.6.
* V16 (no filters at all) loses 40% of capital in 60d. The
  current filter stack is preventing a much larger loss than the
  one we're currently seeing.

### 2.2 Slot #2 (232 stocks, 60d, 6 variants)

By PF, descending:

| Rank | Variant | Trades | WR% | PnL | PF | MaxDD% | Ret% |
|-----:|---------|------:|----:|----:|----:|------:|-----:|
| 1 | **V4_threshold_3pct** | 229 | 38.4 | **-₹489** | **0.84** | 7.99 | -4.80% |
| 2= | V1_baseline_current_shipped | 235 | 36.2 | -₹693 | 0.78 | 8.77 | -6.68% |
| 2= | V17_long_only_shipped | 235 | 36.2 | -₹693 | 0.78 | 8.77 | -6.68% |
| 4 (cluster) | V2 = V18 = V19 | 266 | 34.6 | -₹981 | 0.69 | 11.21 | -9.58% |

**Reading:**
* V4 wins on 232 stocks by ₹204 (29% better PnL than baseline). Same
  rank as on 50 stocks -- cross-universe consistent.
* V4 still loses -4.80% of capital over 60d. **This is not a "promote
  to live" candidate** without additional changes; it is only "the
  least-bad of bad options" within the current strategy stack.
* The V18 anomaly is visible here -- V18 should be ranked alongside V4
  (both are "threshold 3%", just long-only-explicitly vs
  whatever-the-regime-allows) but lands with V2 instead.

### 2.3 Slot #3 (232 stocks, advertised "holdout-30d", actually 60d)

* V1, V2 done -- byte-identical to slot #2's V1, V2 (Bug K
  confirmation, §4 below).
* V3 in progress, ETA Thu ~14:00 IST.
* V4-V19 follow. ETA full completion Sat ~18:00 IST.
* **Friday morning will have at minimum V1-V10 on the bigger universe.**
  Critical decision-affecting variants from §2.1:
  * V10 (confidence 0.6) -- does it transfer to 232 stocks?
  * V15 (mr_xgb_only) -- does the only-profitable variant survive
    the bigger universe? **This is the V15 retrain go/no-go gate.**
  * V16 (completely_naked) -- does the catastrophic outcome scale?

---

## 3. The V15 question — the only positive variant uses the "broken" XGBoost

### 3.1 What V15 actually configures

From `packages/research/battery.py` variant registry (verbatim
description):

> **V15_mr_xgb_only:** Disable rsi_momentum, vwap_bounce,
> opening_range_breakout, supertrend_follow. Keep ONLY
> mean_reversion + xgboost_classifier. Loosen filters
> (no_supertrend_align, no_rsi_align). Tests the hypothesis that the
> mean-reversion + XGBoost combo is the ensemble's profitable core
> and the other strategies are net drag.

V15 result on slot #1 (50 stocks, 60d):
* 56 trades over 60d = ~0.9 trades/day
* Win rate 50.0%
* PnL +₹10 on ₹10,000 capital = +0.10% return
* PF 1.02, Sharpe 0.13
* MaxDD 1.92% -- tightest among all 19 variants except V10

### 3.2 What this means for §5 (XGBoost broken model verdict)

`findings_log_2026-05-27.md §5` confirmed the in-production XGBoost
.pkl predicts ~95% SELL / ~5% BUY on validation data, was trained
during the 2026-05-14 panic patch with 4 known training-pipeline
bugs (P1#8, F-24, C-23, F-22) -- none of which were applied to the
.pkl on disk. **That .pkl is the same one V15 used in slot #1.**
V15 was profitable WITH the broken pkl.

Two interpretations, mutually compatible:

**Interpretation A: The model isn't the dominant source of losses.**
The losses come from the OTHER 4 strategies (rsi, vwap, orb,
supertrend) and/or the regime classifier mis-firing. The "broken"
XGBoost gates correctly because it predicts SELL most of the time
in the validation window (which was a bear/sideways window) -- and
SELLs are filtered out by `allow_shorts: false` (slot-1, live). So
the model contributes ~zero live signals but its INFREQUENT
mean-reversion BUYs land profitably. The XGB model's "bias" toward
SELL is currently muted by allow_shorts and is therefore not toxic.

**Interpretation B: V15's profit is small-universe noise.** 56
trades over 60d is a modest sample. PF 1.02 is barely positive.
On the 232-stock universe (slot #3 by Friday), V15 might revert to
PF < 1.

**Friday morning verdict gate:** check V15 on slot #3.
* If V15 PF > 1.0 on 232 stocks -> **interpretation A confirmed**.
  Retrain becomes high-priority because a CORRECTLY-trained XGBoost
  on a non-bear window would shift the model's BUY/SELL distribution
  back toward balanced -- and V15 would benefit. **Recommend
  consuming slot-3 of the bypass ledger on retrain.**
* If V15 PF < 0.8 on 232 stocks -> **interpretation B confirmed**.
  V15's slot-#1 profit was noise. Retrain is *not* unblocked by this
  evidence. Stay deferred. Look elsewhere for the alpha.
* If 0.8 < V15 PF < 1.0 on 232 stocks -> ambiguous; recommend a
  second confirmatory run (e.g. V15 on a different 60d window, or
  V15 on a different universe) before consuming a slot.

---

## 4. Bug K disclosure — slot #3 is NOT a holdout

Full details in `findings_log_2026-05-27.md` §9. Short version:

* The flag `--holdout-window-days 30` causes
  `packages/research/battery.py:1305-1334` to slice the in-memory
  market_data to the last 30 days. **But the slice runs AFTER the
  cache is written (line 525).** Worker subprocesses then reload
  market_data from the cache file and never see the slice.
* Slot #3's V1+V2 came out byte-identical to slot #2's V1+V2 (same
  235 / 266 trade counts, same PnL, same PF, same MaxDD), confirming
  the slice didn't propagate.
* **Consequence for this review:** no true train/holdout split
  evidence exists. The Friday verdict on V4 (or V10 or V15) being
  "real edge" cannot be confirmed by walk-forward at this review.

### 4.1 What still works

Slot #3 is still useful as a **wider variant sweep on the 232-stock
universe.** It fills in V3, V5-V16 on the big universe (which slot
#2 didn't cover, only V1+V2+V4+V17+V18+V19). The Friday review's
V15 transfer test (above) is still answerable from slot #3 data.

### 4.2 Fix plan

Move the slice block to BEFORE `_save_market_data_cache()` in
`packages/research/battery.py`. Add a unit test that asserts
workers see the sliced data (test will fail on current code, pass
after the reorder). ~30 min work, queued for post-Friday week.

After Bug K fix lands, re-queue a real holdout run for the next
weekend. Until then, *every* live-promotion decision has zero
walk-forward backing -- which is a limit on this review's
confidence.

---

## 5. Live-config candidate -- the verdict

### 5.1 Candidate: V4_threshold_3pct

**Configuration:**
* All 4 currently-live strategies enabled (rsi_momentum, vwap_bounce,
  opening_range_breakout, supertrend_follow).
* `xgboost_classifier` stays disabled (slot-2 still live as today).
* `allow_shorts: false` stays live (slot-1).
* `risk.entry_filters.min_movement_pct: 0.05 -> 0.03`. That's the
  ONLY live config change vs current.

**Evidence for:**
* Highest PF (0.84) and lowest MaxDD (7.99%) among the 6 variants
  tested on the 232-stock universe.
* Rank-stable across universes: also #2 on slot #1 (50 stocks).
* Single-knob change: easy to ship, easy to revert.

**Evidence against:**
* PF still < 1.0. -₹489 over 60d on 232 stocks (-4.80% return). On
  capital scaled to ₹1L, that's -₹4,890 / 60d -- a real-money loss.
* Promoting V4 to live commits us to ANOTHER round of capital decay.
  Better to fix the underlying alpha problem first.

### 5.2 Recommended decision

**[VERDICT, 2026-05-29 14:08 IST]** Do NOT promote V4 to live.
The 4-strategy ensemble's best variant still bleeds money over
60d on the live universe (V4 confirmed at -₹489 / PF 0.84
across slots #2 and #3). Promoting V4 buys us a 30% PnL
improvement on a strategy that is fundamentally unprofitable --
we'd be sailing slightly slower onto the same rocks.

**Instead:**

1. **Keep capital paused** (continue freeze-v2.1's protective stance
   that has kept losses at zero since 2026-05-27, see EOD report
   `docs/eod/eod_report_2026-05-27.md`).
2. **Use slot-3 of the bypass ledger ONLY if V15 transfers** (PF > 1.0
   on 232 stocks per slot #3 Friday morning data). The retrain
   runbook in `findings_log_2026-05-27.md §5.9` is ready to execute
   the moment V15 transfer is confirmed.
3. **Fix Bug K and re-run a real holdout** before any future
   live-promotion decision. Without walk-forward evidence, the V4
   ranking could be an artifact of the specific 60d window.
4. **Investigate V18 anomaly** as a side-task during the post-Friday
   week. Even if V4 isn't promoted, V18 being broken means any
   "tighten threshold" experiment we run on the 232-stock universe
   is currently mis-tagged.

---

## 6. V18 anomaly — separate config-merge bug

### 6.1 Observation

| Variant | Slot #1 (50 stocks) | Slot #2 (232 stocks) | Slot #3 (232 stocks, pending) |
|---------|---------------------|----------------------|-------------------------------|
| V4_threshold_3pct | 27 trades, -₹44, PF 0.85 | 229 trades, -₹489, PF 0.84 | pending |
| V18_long_only_threshold_3pct | **27 trades, -₹44, PF 0.85** (= V4) | **266 trades, -₹981, PF 0.69** (= V2) | pending |
| V2_all_filters_off | 30 trades, -₹128, PF 0.58 | 266 trades, -₹981, PF 0.69 | pending |

On slot #1, V18 = V4 (correct: 3% threshold applied). On slot #2,
V18 = V2 (3% threshold appears to have NO effect).

V18 is defined as "V4 + force long_only + force xgb off". On the
232-stock universe, the long_only and xgb-off overrides clearly
land (otherwise V18 would have ~5x more trades from short signals).
But the threshold-3pct override doesn't. That's a specific override
that's missing.

### 6.2 Root cause hypothesis (unverified)

Two possibilities:

* **(a) Variant-merge order bug.** V18 defines its overrides in a
  list; if the long_only override is applied AFTER the threshold
  override (instead of merging), it might wipe `entry_filters` back
  to the default. Need to read `packages/research/battery.py` variant
  definitions.
* **(b) Universe-specific override.** Some override is gated on
  `len(universe) <= 50` and silently disabled on bigger universes.
  Less likely but worth checking.

Investigation queued for post-Friday week (low-priority -- doesn't
affect the V4 candidate since V4 itself is consistent across
universes).

### 6.3 Friday-morning sanity check

When slot #3's V18 result lands, if it matches V2 again (266 trades,
-₹981), confirmation that the bug is universe-specific not random.
If it matches V4 instead (~229 trades), the slot-#2 V18 anomaly was
a one-off, possibly a config snapshot issue.

---

## 7. Slot-3 retrain decision matrix

| Slot #3 V15 result | Interpretation | Recommended action |
|--------------------|----------------|--------------------|
| PF > 1.05 on 232 stocks | Strong positive transfer | **Consume bypass slot-3, execute retrain runbook (`findings_log_2026-05-27.md §5.9`) next sprint week.** |
| 1.00 < PF < 1.05 on 232 stocks | Weak positive transfer, possibly noise | Hold; queue one more confirmatory run (e.g. V15 on a 90d window, or V15 with `--days 30` start-of-month boundary). |
| 0.95 < PF < 1.00 on 232 stocks | Ambiguous, slot-1 likely small-universe noise | Defer retrain. Investigate WHY slot-1 was positive (window-specific artifact?). |
| **PF < 0.95 on 232 stocks** | **[LANDED, V15 PF=0.94]** Slot-1 V15 was small-universe noise | **DEFER retrain indefinitely. Look for alpha elsewhere (regime classifier, entry-lag, position sizing).** See §10.2. |

---

## 8. Backlog / queued items as of Friday morning

| Item | Priority | Blocker | Estimated effort |
|------|----------|---------|------------------|
| Bug K fix (move slice before cache save) + unit test | HIGH | None | ~30 min code + 1h test/CI |
| Re-queue a real holdout-30d batch after Bug K fix | HIGH | Bug K fix lands first | One overnight run |
| **XGBoost retrain -- PRE-FLIGHT (steps A-E)** | **DEFERRED INDEFINITELY (§7 = NO-GO)** | -- | Was gated on V15 transferring; V15 PF=0.94 on slot #3 = no-go. Pre-flight stays in the runbook (`findings_log_2026-05-27.md §5.10`) for re-activation if H1/H3 forensics later argue retrain is the next move. |
| **XGBoost retrain -- TRAINING (steps 2-5 of §5.9)** | **DEFERRED INDEFINITELY (§7 = NO-GO)** | -- | Same reasoning as PRE-FLIGHT row. |
| Hypothesis H3 forensic (entry-lag, never measured) | **HIGH (next sprint week)** | None | ~3-4 days. Concrete deliverable: histogram of `(broker_fill_ts - strategy_emit_ts)` from the last 30d of trader logs vs the backtester ideal-fill model. |
| Hypothesis H1 diagnostic (regime classifier mis-fire) | **HIGH (next sprint week)** | Slot-3 must finish (V18+V19 still in flight as of 14:08 IST) | ~1-2 days. Per-regime PnL slice for V1+V10+V15 from slot-3 data; tests whether regime tagging is the root of the cross-universe PF degrade. |
| V18 anomaly RCA | LOW | None | ~1h code-read + ~1h test |
| Trader VM trades.csv cleanup | DONE | -- | Verified clean 2026-05-28 11:55 IST, no work needed |
| Bug J permanent fix | DONE | -- | Landed `31703bc` 2026-05-28 |
| Entry-lag forensic (Hypothesis H3 from sprint) | OPEN | Entry-lag never measured | sprint Day 3-4 |

### 8.1 Retrain pre-flight sequence (Friday morning runbook)

If §7 matrix says GO, execute in this order (do NOT skip pre-flight --
the broken pkl came from a panic patch that skipped it):

```
[ ] A. Code-read prepare_dataset.py. Verify P1 #7 cross-symbol
       calendar-leak fix is present.                     (20 min)
[ ] B. Code-read train_xgboost.py. Verify C-23 out-of-sample
       calibration is present.                           (20 min)
[ ] C. Pick training + holdout windows. Document choice
       in findings_log_2026-05-27.md §5.10.              (30 min)
[ ] D. Write tests/unit/test_training_pipeline_preflight.py
       (5 bug-fix assertions). Run -> all green.         (30 min)
[ ] E. Run prepare_dataset.py on a 10-stock slice. Assert
       label distribution not extreme (no >85% one-sided). (15 min)
[ ] 2. Stop battery scheduler. docker run prepare_dataset
       on full universe. docker run train_xgboost.       (~18h)
[ ] 3. Held-out backtest validation. AUC > 0.60, Brier
       improvement, calibration plot near-diagonal.      (~30 min)
[ ] 4. Bench-test new pkl through V1+V4 on Nifty 50 60d
       and 232-stock 60d. Compare vs job#1/job#2 numbers. (~6h)
[ ] 5. Write FREEZE_v2.1.md slot-3 entry. Replace
       models/xgboost_model.pkl on BOTH VMs. Re-enable
       xgboost_classifier in strategies.active. Restart
       trader container. Verify [BUY/SELL] log balance.  (~15 min)
[ ] 6. Resume backtester queue (battery-scheduler.service).
```

Hard stop at step 3 if any of {AUC <= 0.60, calibration plot
miscalibrated, BUY/SELL distribution > 85% one-sided}. Means the
training-bug fixes weren't sufficient -- log as a 6th finding
and defer further retrains until the root cause is understood.

---

## 9. Cross-references

* `docs/findings/findings_log_2026-05-27.md` -- the operational log (§1-§9
  + executive summary).
  * §5 = forensic audit of the broken XGBoost pkl (provides context
    for the V15 interpretation in §3 here).
  * §7 = perf sprint (provides context for the slot #1+#2 throughput
    jump).
  * §8 = battery queue trim rationale.
  * §9 = Bug K disclosure (referenced from §4 here).
* `docs/eod/eod_report_2026-05-27.md` -- the trader-VM EOD report
  showing the protective freeze-v2.1 stance produced zero trades
  and zero losses on the diagnostic-sprint Day 1.
* `docs/diagnoses/diagnosis_sprint_2026-05-27.md` -- the 10-hypothesis,
  5-day Option-A sprint plan. The Friday review is the H1+H2+H3
  read-out checkpoint.
* `data/battery_queue.yaml` -- current queue. 3 jobs, ~36h total.
* `tools/cloud/bootstrap_backtester.sh` -- Bug J permanent fix
  landed today.
* `tests/unit/test_bootstrap_backtester_perms.py` -- 7 Bug J
  regression tests.

---

## 10. Friday-morning verdict (data-landed)

**Status:** appended 2026-05-29 14:08 IST. Slot #3's V15 result
landed at 10:26 IST today. V18 still in flight (64.1% as of this
write); V19 just started. Verdict below is locked because V15
is the single decisive cell from the §7 matrix; V18/V19 are
informational only and do not change the action. Final
slot-#3 wrap is expected ~17:00 IST tonight; if V18 / V19
deliver any unexpected positive variant, we'll re-open this
section. Otherwise §10 is the closing call for the diagnostic
sprint's H1+H2 read-out.

### 10.1 Headline verdict — DEFER retrain, KEEP capital paused

| Question | Answer | Source |
|----------|--------|--------|
| Does V15 transfer profitably to the 232-stock universe? | **NO** | Slot #3, 14:08 IST |
| Does any variant in 257 backtests turn profitable? | **NO** | Slots #1+#2+#3, all 17/19 done in #3 |
| Promote V4_threshold_3pct to live? | **NO** | §5.2, unchanged |
| Consume bypass slot-3 on XGBoost retrain? | **NO** -- §7 matrix DEFER branch | §10.2 below |
| Keep capital paused under freeze-v2.1? | **YES** | §5.2 + §10.2 |

### 10.2 V15 transfer test — the decisive cell

Slot #1 (50 stocks, 60d): **PF 1.02, +₹10, 56 trades, WR 50.0%, MaxDD 1.92%.**
Slot #3 (232 stocks, 60d): **PF 0.94, -₹326, 444 trades, WR 47.3%, MaxDD 8.8%.**

Per §7 matrix `PF < 0.95 on 232 stocks` row:

> Slot-1 V15 was small-universe noise. Defer retrain
> indefinitely. Look for alpha elsewhere (regime classifier,
> entry-lag, position sizing).

**Reading:**
* PnL flipped sign across universes: +₹10 → -₹326. Trade
  count scaled 56 → 444 (~8× on a 5× universe size, so the
  MR strategy fires more aggressively on the bigger
  universe; the extra trades land net-negative).
* WR is still the highest of any variant in slot #3 (47.3%
  vs the next-best V4 at 38.4%) -- but PF 0.94 means the
  losers, while individually less frequent, are bigger than
  the winners. This is consistent with mean-reversion entries
  that get caught in trending moves.
* Cross-universe rank stability HOLDS (V15 is still the best
  variant in slot #3 by PF), but **best-in-class is no longer
  profitable** on the production universe. That's the new
  evidence.

**What this kills:**
* The "XGBoost retrain unblocks the alpha" hypothesis from
  the diagnostic sprint's H2. The current XGB pkl is broken
  (forensic audit §5 of `findings_log_2026-05-27.md`), but
  even the variant that ONLY uses MR + that broken XGB doesn't
  transfer profitably to the live universe. Retraining XGB
  might still help -- but on the strength of slot-#3 data
  alone, retrain is **not** the highest-leverage next move.

**What stays alive (now top of the next-sprint backlog):**
* Entry-lag forensic (Hypothesis H3 from
  `docs/diagnoses/diagnosis_sprint_2026-05-27.md`). Live trades may be
  systematically late vs the backtester's ideal-fill model;
  if so, the backtester's PnL is an *upper bound* on what
  live can deliver. Worth measuring before any new strategy
  hypothesis.
* Regime-classifier mis-firing (H1). The cross-universe
  PF degradation (slot-1 V10/V15 around PF 0.9-1.0 vs slot-3
  V10/V15 at PF 0.79-0.94) suggests the regime classifier
  may be tagging winning windows differently across
  universe sizes. Diagnostic: log per-regime PnL across
  V1+V10+V15 from slot-3.

### 10.3 Slot-#3 ranking (17 of 19 variants done)

| Rank | Variant | Trades | WR% | PnL | PF | MaxDD% | Ret% | Cross-universe stable? |
|-----:|---------|------:|----:|----:|----:|------:|-----:|:--|
| 1 | V15_mr_xgb_only | 444 | 47.3 | -₹326 | **0.94** | 8.80 | -3.23% | NO -- profit lost on transfer |
| 2 | V5_threshold_7pct | 253 | 37.2 | -₹451 | 0.86 | 7.73 | -4.26% | n/a (was not in slot #1) |
| 3 | V4_threshold_3pct | 229 | 38.4 | -₹489 | 0.84 | 7.99 | -4.80% | YES -- still rank 1 in slot #2 |
| 4 | V6_threshold_10pct | 257 | 35.8 | -₹626 | 0.81 | 8.16 | -6.01% | n/a |
| 5 | V10_confidence_060 | 225 | 36.0 | -₹636 | 0.79 | 8.29 | -6.11% | NO -- rank 2 in slot #1 |
| 6= | V1=V12=V13=V14 (cluster) | 235 | 36.2 | -₹693 | 0.78 | 8.77 | -6.68% | YES |
| 7 | V11_confidence_050 | 235 | 36.2 | -₹694 | 0.78 | 8.78 | -6.69% | n/a |
| 8 | V17_long_only_shipped | 236 | 36.0 | -₹711 | 0.78 | 8.63 | -6.86% | YES (≈V1, expected) |
| 9= | V8=V9 | 263 | 33.5 | -₹809 | 0.74 | 9.75 | -7.85% | n/a |
| 10= | V2=V3=V7 | 266 | 34.6 | -₹981 | 0.69 | 11.21 | -9.58% | YES (≈V2 cluster, expected) |
| 17 | V16_completely_naked | **4890** | 32.6 | **-₹6771** | **0.39** | **67.65** | **-67.64%** | YES (catastrophic on both) |
| -- | V18 | in flight | -- | -- | -- | -- | -- | pending §10.4 |
| -- | V19 | in flight | -- | -- | -- | -- | -- | pending §10.4 |

**Reading new entries (V5/V6/V11/V12/V13/V14 — first time on the
big universe):**
* V5 (threshold 7%) edges out V4 (threshold 3%) by ₹38. Tighter
  AND looser thresholds both beat the shipped 5% (V1) -- the
  shipped value is a local minimum. Worth re-running V5 on a
  different 60d window to check it's not window-specific.
* V11 (confidence 0.5) is essentially identical to V1 -- so
  loosening the confidence floor below 0.7 doesn't help on
  the 232-stock universe. Slot-1's V10 advantage (confidence
  0.6) doesn't transfer.
* V12/V13/V14 are all ≡ V1. Confirms peak-giveback,
  window-cap-8, and opening-lockout-off are no-ops on the
  current strategy stack.
* V16 (no filters at all) loses 67.64% over 60d. The current
  filter stack is preventing -₹6,000 of additional loss vs
  what a naked stack would do; that's still real protective
  value, even though we're losing money WITH the filters.

### 10.4 Pending finalisations

| Item | Expected by | Effect on §10.1 verdict |
|------|------|------|
| ~~V18 result lands~~ | **DONE 15:25 IST 2026-05-29** | V18 = 229 trades, PF 0.85, -₹473 — **almost identical to V4** (229 trades, PF 0.84, -₹489). The slot-#2 V18=V2 anomaly was a one-off (stale config snapshot at slot-#2 startup, NOT a universe-specific override-merge bug). §6.2 hypothesis (b) ruled out; §6 RCA priority drops to LOW. |
| ~~V19 result lands~~ | **DONE 17:34 IST 2026-05-29** | V19 = 266 trades, PF 0.69, -₹981 — **byte-identical to V2 / V3 / V7** as predicted by symmetry (long-only ≡ all-filters-off when `allow_shorts: false` is already live). Confirmation only; no impact on §10.1. |

### 10.5 Retrain LANDED with operator override — 2026-05-29 18:05 IST

User: "Train the XGB with latest data so that the next battery run
will be with proper setup. And we have actually good or bad data
by end of the next week maybe."

This **overrode the §10.1 "defer retrain indefinitely" call** on the
practical argument that a properly-trained baseline pkl is strictly
better than the broken pipeline pkl AND the next battery gives
end-of-next-week real evidence — backtester-only, no freeze slot.

**Pre-flight A-E** ran locally in ~30 min (see findings_log §5.10.1).
All 7 known training-pipeline fixes pinned in
`tests/unit/test_training_pipeline_preflight.py` (33 tests; full
unit **1,713/1,713**).

**Phase 2 training** ran on backtester VM at 17:59 IST:

| Metric | Value | Verdict |
|---|---|---|
| Total samples | 271,979 | ✅ Healthy |
| Train/Test rows | 217,544 / 54,435 (P1 #7 timestamp split @ 2026-05-12 09:35 IST) | ✅ |
| Label balance | UP 49.9% / DOWN 50.1% | ✅ Far from broken-pkl 95/5 |
| Best iteration | 30 / 500 (early-stop) | ✅ F-22 carve fired |
| **Raw test AUC** | **0.4705** | ❌ ~Random; no edge at model layer |
| **Calibrated AUC** | 0.4908 (raw_eval 0.5166) | C-23 collapse safety fired → ships raw booster |
| **Prediction distribution** | **BUY 32.0% / SELL 68.0%** | ⚠️ Mild SELL bias; **nowhere near broken 95/5** |
| Top features | dow_sin, tod_cos, india_vix, dow_cos, tod_sin | Session-time + VIX dominate (no real signal pattern) |

**Hard-stop fired**: `AUC=0.4908 < 0.55` → script refused the swap.
**Operator override**: backtester pkl manually swapped (NOT trader);
broken pkl backed up at
`models/xgboost_model_pre_override_20260529T1233Z.pkl`. Hash-verified
(see findings_log §5.10.2). Hard-stop in the script unchanged — the
override is a one-time deliberate action with full audit trail.

**Updated decision (overrides §10.1):**

* **Backtester pkl**: swapped to retrained (160 KB, 32/68 prediction
  distribution, AUC 0.47).
* **Trader pkl**: untouched. `xgboost_classifier` remains disabled in
  `strategies.active` live. **No capital exposure to the new pkl.**
* **Slot #4 queued**: `post_retrain_xgb_focus_60d` (V1 + V3 + V10 +
  V11 + V15 on 232 stocks × 60d, ETA ~12h). Provides apples-to-apples
  comparison vs slot-3 with only the pkl changed.
* **Slot #5 deferred**: `post_retrain_v2_holdout_30d` (~36h) is left
  commented-out, gated on focus result. Activate iff focus V15
  PF ≥ 0.95.
* **No promotion to live**: even if focus V15 PF > 1.0, do NOT
  consume bypass slot 3 of 3 yet — first run H3 entry-lag forensic
  to confirm whether broker fill timing (not the model) is the
  primary loss driver.

**What the AUC=0.49 result means for §10.1:**

The retrain was not a magic bullet, as predicted by §10.1. The 271k
samples on the proper pipeline (all 7 known fixes confirmed firing)
still produce no edge at the model layer. This **strengthens** the
"defer XGBoost-deploy indefinitely" call: H3 (entry-lag forensic)
and H1 (regime classifier diagnostic) are even more clearly the
right next moves. The focus battery exists only to confirm the
predicted "no PF improvement at strategy layer" outcome (which would
fully isolate model layer from execution layer as loss drivers).

**Wall-clock ahead:**

| Item | Expected by |
|---|---|
| Slot #4 focus result | Saturday 2026-05-30 ~05-08 IST |
| H3 entry-lag forensic | Sunday 2026-05-31 → Tuesday 2026-06-02 |
| H1 regime classifier | Tuesday 2026-06-02 → Wednesday 2026-06-03 |
| Slot #5 holdout (if enabled) | Wednesday early morning |
| **Friday 2026-06-05 verdict** | Synthesis: focus PF + H3 histogram + H1 per-regime PnL |

### 10.5 Recommended next moves

In priority order, with rough effort estimates. The capital
remains paused throughout; this is research / forensic
queue.

1. **(today, 30 min)** Append the V15 verdict to
   `findings_log_2026-05-27.md` so the operational log carries
   the same conclusion as this review. New §10 mirroring §10
   here.
2. **(today, ~5 min)** Stop the model-retrain pre-flight todo
   from being on the active queue. The `model_retrain` todo
   moves from PENDING-(gated-on-V15) → DEFERRED-INDEFINITELY
   per §10.2.
3. **(post-Friday week, ~2h)** Bug K fix: move the
   holdout-slice block to BEFORE `_save_market_data_cache` in
   `packages/research/battery.py`; add the unit test from
   `findings_log_2026-05-27.md §9`. Re-queue an actual
   holdout-30d batch for the following weekend. This is the
   only way to get walk-forward evidence.
4. **(next sprint week, 3-4 days)** Hypothesis H3 forensic
   from the diagnostic sprint -- measure live entry lag.
   Concrete deliverable: histogram of `(broker_fill_ts -
   strategy_emit_ts)` from the last 30d of trader logs vs the
   backtester's ideal-fill model.
5. **(next sprint week, 1-2 days)** Hypothesis H1 -- per-regime
   PnL slice for V1+V10+V15 from slot-3 data. Tests whether
   regime classifier mis-firing (cross-universe instability)
   is the underlying cause of the cross-universe PF degrade.
6. **(low priority, ~1h)** V18 anomaly RCA from §6 once V18's
   slot-#3 cell lands. Not decision-affecting (since no V*
   variant is being promoted) but cheap to close.

### 10.6 What this means for the diagnostic sprint

The Friday checkpoint of the 5-day Option-A sprint
(`docs/diagnoses/diagnosis_sprint_2026-05-27.md`) was meant to read out
H1+H2+H3. With the V15 verdict:

* **H2 (XGBoost broken model is the cause of live losses):**
  partially refuted. The *current* broken pkl IS broken
  (validated forensic in §5), but retraining it is not
  sufficient to turn the system profitable on the 232-stock
  universe -- because the only variant that even hinted at
  using XGB profitably (V15 in slot #1) didn't transfer.
  Retrain stays in the backlog as a "would help if combined
  with H1/H3 fixes" rather than a standalone unblock.
* **H1 (regime classifier mis-firing):** strengthened by
  cross-universe instability data. Top of the next-sprint
  backlog (§10.5 item 5).
* **H3 (entry-lag inflating live losses):** unchanged --
  still un-measured. Top-priority forensic for the next
  sprint week (§10.5 item 4).

Capital remains paused under freeze-v2.1. The sprint produced
clean evidence on H2 (negative result, but conclusive) and a
strong steer toward H1+H3 as the next-sprint focus. That's a
successful sprint outcome even though the headline answer is
"nothing to ship today."

---

## 11. Project review 2026-05-29 19:10 IST + freeze-exit pre-commitment

The operator delivered a thorough adversarial review at 19:10 IST
that synthesised the diagnostic-sprint data into a brutal-but-correct
three-part finding (no-edge in any 232-stock variant; single Nifty-50
winner doesn't transfer; AUC=0.49 on clean retrain) and asked for
in-writing pre-commitment to kill thresholds before any next-week
work begins.

The pre-commitment landed in:

**[`docs/freeze/freeze_v2.1_exit_criteria_2026-06-05.md`](../freeze/freeze_v2.1_exit_criteria_2026-06-05.md)**

That document is the operating contract from 2026-05-29 forward.
Three pre-committed thresholds:

| Threshold | When | What |
|---|---|---|
| **T1** | Wed 2026-06-03 | H3-prime entry-lag forensic. Median `broker_fill_ts - strategy_emit_ts`: < 30 s → wind-down candidate; 30–120 s → single-knob pilot under hard kill floor; > 120 s → deploy PERF-01/02/14 + 5-day paper window. |
| **T2** | Sat 2026-05-30 morning | Slot #4 focus run V15 PF: ≥ 1.05 surprising (do **NOT** ship); 0.90–1.05 net-zero model; < 0.90 retire `xgboost_classifier` permanently. |
| **T3** | Fri 2026-06-08 | Wind-down kill criterion. If no PERF fix produces a 5-day paper window with PF ≥ 1.20 AND no H3/H1 finding identifies a single-bug remediation that could move PF above 1.0 → wind-down. |

Friday 2026-06-05 decision is constrained to exactly three options
(see exit-criteria doc §1):

* **1.A** Wind-down (recommended absent surprise data).
* **1.B** Single-knob deploy: PERF-01 + V4-thresh-3% on Nifty 50
  only, max-concurrent-positions=5, hard rupee kill floor -₹500,
  paper-only for first 5 days. Live trading requires a second
  explicit unfreeze decision.
* **1.C** Architectural pivot: higher TF, event-driven features,
  formal v2.1 close-out + new v3 charter.

The implicit fourth option ("extend the freeze, run more battery
variants, hope for surprise edge") is explicitly ruled out. See
exit-criteria doc §1.D.

Bug O (test → prod `trades.csv` leak) and the audit-only
reclassification refinement also landed in this review window. Full
RCA in `docs/findings/findings_log_2026-05-27.md` §24.

The review's data-side conclusions are accepted. This file is
preserved as the snapshot of what we believed and how we framed it
on Friday morning before the review; the exit-criteria document is
what we operate under for the next 10 days.

