# Wind-Down Criteria — Pre-Committed 2026-05-29

**Status:** Pre-committed BEFORE slot #4 finishes. Locked.

**Pre-commit timestamp:** 2026-05-29 ~19:50 IST.

> **CHG note (added 2026-06-01, post-pre-commit, doc-only):** the V15 PF
> threshold in §1.T1 below was measured against the **pre-CHG charges
> model** (Zerodha-calibrated `packages/core/charges.py`). On 2026-06-01,
> the charges model was corrected to AngelOne's actual rates
> (CHG-01..CHG-05, see [`../findings/findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md)),
> which makes every backtest PF tighten. The V15 PF threshold of 0.90
> stays in force as written — re-derivation would violate the pre-commit
> — but for context the V15 variant in the v2_holdout_30d battery went
> from PF 0.944 to PF 0.386 under the corrected charges. The directional
> conclusion of T1 (xgboost_classifier retired) is **strengthened**, not
> changed, by CHG. Full per-variant adjustment in
> [`../findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md).
> No new freeze slot consumed; charges.py is not on the frozen file list.

**Why this doc exists.** Once the focus run produces a number, every
threshold written afterwards will be unconsciously calibrated to
that number. Pre-commit prevents result-driven goalpost-moving — the
failure pattern that consumed all 3 freeze slots.

This is the operational kill-criteria sheet. It is short on purpose.
The longer operating contract lives in
[`freeze_v2.1_exit_criteria_2026-06-05.md`](freeze_v2.1_exit_criteria_2026-06-05.md);
this doc is what gets read aloud at the 2026-06-05 verdict meeting
and applied mechanically.

---

## 1. Pre-committed thresholds for the 2026-06-05 verdict meeting

### T1 — Slot #4 focus run, V15 with retrained pkl

Lands Saturday 2026-05-30 ~05:00–08:00 IST.

| V15 PF | Verdict |
|---|---|
| **≥ 1.05** | Surprise — investigate before any action |
| **0.90 – 1.05** | As predicted; weak evidence of net-zero model contribution |
| **< 0.90** | Permanently retire `xgboost_classifier` from `strategies.active` |

### T2 — H3 entry-lag forensic, median `broker_fill_ts − strategy_emit_ts`

Deliverable Wed 2026-06-03.

| Median lag | Verdict |
|---|---|
| **< 30 s** | Execution healthy; PERF fixes not the bottleneck → Threshold T3 wind-down branch |
| **30 – 120 s** | Meaningful but recoverable; pilot a paper window with PERF deployed |
| **> 120 s** | Confirmed; deploy PERF-01/02/14 under a fresh freeze contract |

### T3 — Wind-down trigger

If by 2026-06-08 **BOTH** of the following are true:

* **(a)** No PERF-fix paper window achieves rolling **PF ≥ 1.20 over 5 trading days**.
* **(b)** No H3 / H1 finding names a single bug whose fix could plausibly move PF above 1.0.

Then: **declare the experiment concluded.** Final post-mortem.
No further engineering investment.

---

## 2. What does NOT trigger wind-down

* A single bad day.
* A single bad week.
* A slot-#4 PF in the 0.90 – 1.05 band on its own.

---

## 3. What this commits us to

* **No 4th freeze slot.** The contract caps at 3; we are at 3.
* **No deploy** of V4 or any variant to live without a NEW freeze charter.
* **No further retrain attempts** with different hyperparameters "just to see."
* **No architectural pivot** (v3 timeframe-shift) before the wind-down decision is rendered.

---

## 4. The "what NOT to do" list (anti-temptation)

The strongest pull between now and 2026-06-05 is to do something
that feels productive. Each of these is rejected in advance:

| Tempting move | Why not |
|---|---|
| Retrain XGBoost with different hyperparameters / longer history / different feature set | AUC 0.49 with all 7 known pipeline bugs fixed is **not** a hyperparameter problem. New features are a v3 question, not a v2.1 question. |
| Run more battery variants (V20, V21, …) to find an outlier | The 17 variants already tested cover the meaningful parameter space. More variants = p-hacking. |
| Promote V4 to live "with tight stops" / "with a small position cap" | V4 PF is 0.84, -4.8% / 60d. A position cap turns a small loss into a small loss. |
| Open a 4th freeze slot for "just one critical fix" | The contract caps at 3 specifically to prevent this. Slot 4 = unfreeze decision. |
| Begin "v3 architectural pivot" (higher TF, event-driven) this weekend | The right eventual move IF wind-down doesn't fire. Doing it now is escape-from-data behaviour. |
| Watch the slot #4 progress every hour tonight | The data lands when it lands. Watching faster does not improve it. |

---

## 5. Failure mode to watch for at the 2026-06-05 verdict meeting

If at the verdict meeting I (the agent) find myself constructing
reasons to not pick option **A (wind-down)** when the data points
there, **that's the bypass-abuse failure mode reasserting itself.**

Re-read this doc. Pick A.

---

## 6. The single sentence

> Tonight: write this. Saturday: read slot #4 and start H3. Next Friday: apply the criteria mechanically.

That is the entire plan. Everything else is noise until the data lands.
