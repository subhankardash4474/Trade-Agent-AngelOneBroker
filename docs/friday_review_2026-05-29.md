# Friday review — 2026-05-29

**Status:** Draft prepared 2026-05-28 (market holiday). Final
verdicts/decisions get appended Friday morning after slot #3 of the
backtester queue completes.

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

**Do NOT promote V4 to live yet.** The 4-strategy ensemble's best
variant still bleeds money over 60d on the live universe. Promoting
V4 buys us a 30% PnL improvement on a strategy that is fundamentally
unprofitable -- we'd be sailing slightly slower onto the same rocks.

**Instead:**

1. **Keep capital paused** (continue freeze-v2.1's protective stance
   that has kept losses at zero since 2026-05-27, see EOD report
   `docs/eod_report_2026-05-27.md`).
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
| PF < 0.95 on 232 stocks | Slot-1 V15 was small-universe noise | Defer retrain indefinitely. Look for alpha elsewhere (regime classifier, entry-lag, position sizing). |

---

## 8. Backlog / queued items as of Friday morning

| Item | Priority | Blocker | Estimated effort |
|------|----------|---------|------------------|
| Bug K fix (move slice before cache save) + unit test | HIGH | None | ~30 min code + 1h test/CI |
| Re-queue a real holdout-30d batch after Bug K fix | HIGH | Bug K fix lands first | One overnight run |
| XGBoost retrain (slot-3 candidate) | TBD by §7 matrix | Friday morning V15 transfer result | ~2 days per `findings_log_2026-05-27.md §5.9` |
| V18 anomaly RCA | LOW | None | ~1h code-read + ~1h test |
| Trader VM trades.csv cleanup | DONE | -- | Verified clean 2026-05-28 11:55 IST, no work needed |
| Bug J permanent fix | DONE | -- | Landed `31703bc` 2026-05-28 |
| Entry-lag forensic (Hypothesis H3 from sprint) | OPEN | Entry-lag never measured | sprint Day 3-4 |

---

## 9. Cross-references

* `docs/findings_log_2026-05-27.md` -- the operational log (§1-§9
  + executive summary).
  * §5 = forensic audit of the broken XGBoost pkl (provides context
    for the V15 interpretation in §3 here).
  * §7 = perf sprint (provides context for the slot #1+#2 throughput
    jump).
  * §8 = battery queue trim rationale.
  * §9 = Bug K disclosure (referenced from §4 here).
* `docs/eod_report_2026-05-27.md` -- the trader-VM EOD report
  showing the protective freeze-v2.1 stance produced zero trades
  and zero losses on the diagnostic-sprint Day 1.
* `docs/diagnosis_sprint_2026-05-27.md` -- the 10-hypothesis,
  5-day Option-A sprint plan. The Friday review is the H1+H2+H3
  read-out checkpoint.
* `data/battery_queue.yaml` -- current queue. 3 jobs, ~36h total.
* `tools/cloud/bootstrap_backtester.sh` -- Bug J permanent fix
  landed today.
* `tests/unit/test_bootstrap_backtester_perms.py` -- 7 Bug J
  regression tests.
