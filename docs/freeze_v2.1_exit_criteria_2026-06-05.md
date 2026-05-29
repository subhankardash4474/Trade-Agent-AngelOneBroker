# Freeze v2.1 — Exit Criteria, Pre-Committed 2026-05-29

**Author:** trading agent + operator joint commitment.
**Status:** ACTIVE. This document is the operating contract for all
trading-agent work between 2026-05-29 and the 2026-06-05 / 2026-06-08
decision points. It supersedes any prior implicit "let's see what
the next battery says" framing.

**Why this document exists.** The 2026-05-29 project review identified
an honest risk: the diagnostic sprint has produced three independent
negative signals (no edge in any 232-stock variant; no transfer of
the one Nifty-50 winner; AUC=0.49 on a clean 271k-sample retrain),
and without pre-committed go/no-go thresholds the team will keep
running "one more battery" on an unprofitable engine until the
operator's time and capital are exhausted. The contract below is
the prevention mechanism. It is signed in writing on 2026-05-29 and
the decisions on 2026-06-05 / 2026-06-08 are made AGAINST these
thresholds, not against new framings invented on the day.

---

## 0. The three pre-committed thresholds

These are the only thresholds that gate the next-week decisions.
Re-derivation, re-framing, or "but on this slice it looks better"
arguments do NOT override them. The threshold values were chosen on
2026-05-29 with the data the team had on 2026-05-29 — they remain
the gates regardless of what next-week data presents.

### 0.1 Threshold 1 — H3-prime entry-lag forensic (deliverable Wed 2026-06-03)

**Measurement.** Histogram of `broker_fill_ts - strategy_emit_ts`
across the last 30 trading days of `logs/trader_log_*.log` and
`logs/orders_*.csv`. Median, p90, p99 reported. Reference baseline
is the backtester's ideal-fill assumption (`fill_ts == bar_close_ts`).

**Outcomes.**

| Median lag | Verdict | Lead conclusion |
|---|---|---|
| < 30 s | Live execution healthy | Strategies themselves are the bottleneck. PERF-01/02/14 fixes will NOT save us. Move directly to Threshold 3. |
| 30 s – 120 s | Meaningfully degraded but recoverable | PERF-01/02/14 plausibly worth deploying. Pilot a single re-run window (paper, allow_shorts:false, xgb still off) with a hard-rupee 10-trade kill floor of -₹500 net. |
| > 120 s | Confirmed -- this is the bug | Deploy PERF-01/02/14 (audit-only path with explicit unfreeze acknowledgement, see §1). Re-run a 5-trading-day paper window before considering live. |

### 0.2 Threshold 2 — Slot #4 focus run, V15 with retrained pkl (deliverable Sat 2026-05-30 morning)

**Measurement.** Slot #4 `post_retrain_xgb_focus_60d` (5 variants
including V15 = mean_reversion + xgboost_classifier only). PF, PnL,
WR%, MaxDD per variant.

**Reference prediction (per AUC=0.49 implication).** V15 PF lands in
0.85–0.95 range; PnL net-negative; WR% remains the highest of the
5 (`xgboost` distribution is mildly skewed but no directional edge).

**Outcomes.**

| V15 PF | Verdict | Lead conclusion |
|---|---|---|
| ≥ 1.05 | SURPRISING | Means the new (AUC=0.49) pkl unlocked something the broken pkl actively destroyed. Investigate. Do **NOT** ship to live solely on this. |
| 0.90 ≤ PF < 1.05 | As predicted | Weak evidence of net-zero model contribution. Confirms the diagnostic-sprint H2 conclusion (XGBoost on TA features carries no edge). |
| PF < 0.90 | Model dead weight | Permanently retire `xgboost_classifier` from `strategies.active`. Replace its 1.0 weight with explicit zero in config. Remove the strategy file from `packages/strategies/__init__.py` import so the canonical design intent becomes "ensemble of 4" not "ensemble of 5 with one disabled." |

### 0.3 Threshold 3 — Wind-down kill criterion (decision Fri 2026-06-08)

**Pre-condition state on 2026-06-08:**

* Best backtest variant on the 232-stock production universe is PF 0.84 (V4, net negative).
* ML hypothesis returned AUC 0.49 on a 271k-sample clean retrain.
* Live capital pause is the only thing keeping cumulative deployment cost from exceeding -₹2,500.
* All 3 freeze slots are consumed.

**Wind-down condition (BOTH must be true).**

1. No PERF fix produces a paper-mode 5-day window with PF ≥ 1.20.
   *Equivalently: no 5-day window with rolling PnL beating cost burn.*
2. No new H3 / H1 finding identifies a single named bug whose
   remediation could plausibly move PF above 1.0.

**If both true on 2026-06-08:** declare experiment concluded. Specifically:

* Capital paused remains.
* No further engineering investment past the post-mortem.
* The Phase-5 audit remediation is preserved as the canonical
  "production-grade hardening" reference.
* A final post-mortem document is written: what worked, what didn't,
  what the data ultimately said, what's salvageable for a v3 if it
  ever happens.

**This is not a prediction.** It is a pre-committed condition. If
the data on 2026-06-08 satisfies both clauses, wind-down is the
decision regardless of any psychological reluctance to take it.

---

## 1. The Friday 2026-06-05 decision tree

On 2026-06-05, the operator picks **exactly one** of the following
three options. The choice is constrained by the Threshold-1 +
Threshold-2 readouts.

### 1.A — Wind-down

**Triggers.** Threshold 1 returns < 30 s median lag *AND* Threshold 2
returns V15 PF < 0.90 *AND* the H1 per-regime PnL slice does not
identify a single regime where the engine is profitable in
isolation.

**What it means.** Take the engineering work as a portfolio piece,
take the audit work as a learning asset, recover the operator's
time. This is the recommended option absent surprise data.

**Assistant's honest view (2026-05-29):** This is the most likely
outcome consistent with the current data. AUC=0.49 + cross-universe
non-transfer + V4 PF=0.84 + top-features being calendar/VIX is a
coherent picture that points here.

### 1.B — Single-knob deploy

**Triggers.** Threshold 1 returns 30–120 s median lag (i.e. the lag
bug was meaningful) *AND* a defensible-edge case can be made on
post-PERF behaviour.

**Constraints (NON-NEGOTIABLE if option B is chosen).**

* PERF-01 + V4 trend_filter_pct=3% only. No other knobs touched.
* Universe restricted to Nifty 50 (50 stocks, NOT 232).
* `max_concurrent_positions` capped at 5.
* Hard rupee kill floor of -₹500 cumulative; on breach, agent
  auto-pauses, alerts ops, requires manual unpause.
* Paper mode for the first 5 trading days. Live trading (real
  capital) requires a second explicit unfreeze decision after the
  paper window.
* Daily readout: PnL, fill-lag p50/p90, kill-floor distance.

**Assistant's honest view (2026-05-29):** This is "give the engine
one more empirical test with the lag bug fixed." Defensible only if
H3-prime returns > 30 s. The hard rupee kill floor is the
difference between this and "let's run another battery."

### 1.C — Architectural pivot

**Triggers.** Threshold 1 + Threshold 2 leave open the possibility
that the chosen *horizon* (5-min bars, 15-min directional return)
or chosen *features* (TA only) are wrong, but the *infrastructure*
(broker integration, alert/dedup, persistence, audit checkpoints,
phase-5 hardening) is sound.

**What it means.** Higher timeframe (15-min or 1-hour bars), feature
universe pivot to event-driven (earnings proximity, sector rotation,
rate-decision proximity, news flow), and explicitly NOT an intraday
MIS engine. This is essentially a new project sharing infrastructure
with the old.

**Constraints.** Cannot be pursued under the existing freeze
contract. Formally close out v2.1 with a final post-mortem and write
a v3 charter before any feature work begins. v3 charter must
articulate: target horizon, target features, target return profile,
and the precise success criterion that will be tested before any
capital deployment.

**Assistant's honest view (2026-05-29):** This is a "yes the
infrastructure is good, but we picked the wrong target" path. It's
defensible but it is NOT a continuation of v2.1 — it's a different
project that happens to inherit the broker integration. Don't
muddy the v2.1 conclusion by labelling pivot work as "more v2.1
investigation."

### 1.D — Implicit fourth option (REJECTED)

The implicit option that has gotten us to 2026-05-29 is:
"extend the freeze, run more battery variants, hope for surprise
edge." This is the option this document exists to **rule out**.
On 2026-06-05, "run more variants" is not on the table. The
operator picks A, B, or C. No fourth choice.

---

## 2. The freeze contract — explicit end date

Per the freeze-v2.1 charter, slot 3 of 3 has been consumed (Phase-5
frozen-file remediation: NUM-04, NUM-05, NUM-08, NUM-09, NUM-12,
NUM-15, OBS-04, OBS-10, OBS-19, CONC-01).

**Effective immediately (2026-05-29):**

* No further behaviour-changing edits to trader-VM-deployable code
  (`packages/strategies/*`, `packages/core/risk_manager.py`,
  `packages/core/portfolio.py`, `packages/core/data_handler.py`,
  `trading_agent.py`, `run_daemon.py`) until the 2026-06-05
  decision lands.
* Bug fixes to that code that surface between now and 2026-06-05
  are documented as Bug-Q / Bug-R / etc. but **NOT deployed**.
  They sit in the queue and are released as part of whichever
  option (A / B / C) gets picked.
* "Audit-only" reclassification (see §3 below) is no longer a
  release valve for behaviour change. From 2026-05-29 forward,
  audit-only means the changed file is NEVER imported by the
  trader-VM-deployable code path.

**Hard end date.** The freeze contract ends, one way or the other,
on **2026-06-08**. Either:
* Wind-down chosen (1.A) and freeze becomes permanent (no further
  behaviour change because the project is concluded), OR
* Single-knob deploy chosen (1.B) which formally closes the freeze
  via explicit unfreeze (slot-4 with the constraints in §1.B), OR
* Architectural pivot chosen (1.C) which closes v2.1 entirely and
  starts a new charter.

There is no fourth path that keeps the freeze indefinitely active.

---

## 3. "Audit-only" reclassification — clarified scope (response to review §3.D)

The 2026-05-29 review correctly flagged that several "audit-only"
entries (Bug E backtester O(N²), Bug F harness cascade-fail, Bug G
hardening, Bug H xgboost-mount, Bug K holdout-flag) **changed what
the data says** even though they did not change trader-VM behaviour.
The cumulative effect was that "what the data says" between Monday
and Friday of week 2 was actively shifting under the freeze.

**New three-way classification, applied retroactively to all open
audit findings:**

| Class | Definition | Examples | Slot consumption |
|---|---|---|---|
| **Trader-behaviour-changing** | Modifies code reachable from `trading_agent.py` or `run_daemon.py` at runtime AND changes which decisions the agent makes (signal, sizing, execution, risk). | NUM-01 (mis_short_margin_pct), STATE-04 (atomic close), Phase-5 frozen-file fixes. | Slot-consuming. |
| **Audit-only, semantically neutral** | Changes a non-trader code path (backtester, research tool, test) AND does NOT change which numbers are correct -- only computes them faster, more clearly, or more cheaply. | PERF-01 (LTP batch endpoint), PERF-02 (per-cycle memo), test-suite refactor. | Audit-only, no slot. |
| **Audit-only, baseline-shifting** | Changes a non-trader code path AND changes which numbers are correct, invalidating prior analytical baselines. | Bug E (backtester O(N²) → fixed), Bug F (harness cascade-fail → unblocked), Bug H (xgb mount missing → all "Bug-H-pre" results obsolete), Bug K (holdout flag silently ignored → slot-3 reframing). | **Audit-only, no slot, BUT requires explicit "baseline reset" notice in findings_log AND a re-run plan for any prior analysis that depended on the old numbers.** |

The third class is what the review flagged as a release valve. Going
forward, every Bug-letter that lands in class 3 must include in its
findings_log entry:

1. The list of prior analyses that are now invalidated.
2. The plan for re-running them (or explicit "we accept the
   invalidated baseline because re-running is too expensive").
3. A timestamp of when the baseline was reset.

Bug H, Bug K (already documented this way in retrospect) and
Bug E / Bug F (require retroactive baseline-reset notices) are the
ones that need this treatment now.

---

## 4. What is NOT changing (response to review §3.A)

The retrain operator-override decision on 2026-05-29 produced this
state:

* New pkl deployed to **backtester only**.
* Trader VM untouched.
* `xgboost_classifier` still disabled live (`strategies.active` removal preserved).

The override was specifically for "apples-to-apples slot-3-vs-slot-4
forensic comparison with only the pkl changed." The review
correctly flagged that there's psychological pull to re-frame slot
#4 results as "successful enough" once they land.

**Pre-committed (2026-05-29):**

1. The slot #4 result is forensic, not exploratory. The decision matrix in §0.2 is the only valid response to it.
2. V15 PF ≥ 1.05 is "surprising — investigate"; it is **NOT** "promote to live." Threshold 2 explicitly forbids ship-on-this.
3. The new pkl swap on the backtester is an analytical artefact. It is preserved as `models/xgboost_model_retrain_20260529T1225Z.pkl`. The trader VM continues to use `models/xgboost_model_pre_override_20260529T1233Z.pkl`. Even if option 1.B is chosen on 2026-06-05, V4 (NOT V15) is the deploy variant; V15 is forensic.

---

## 5. The H3-prime entry-lag forensic — concrete deliverable (Wed 2026-06-03)

**Inputs.**

* `logs/trader_log_*.log` for the last 30 trading days (search range: 2026-04-30 → 2026-05-29).
* `logs/orders_*.csv` for the same range, joined to the above by `order_id`.
* `logs/audit/<date>/checkpoint_*.json` for cycle wall-time stats.

**Outputs.**

* `docs/h3_prime_entry_lag_2026-06-03.md` containing:
  * Histogram of `broker_fill_ts - strategy_emit_ts` (median, p90, p99, distribution by hour-of-day).
  * Histogram of `cycle_start_ts - cycle_end_ts` (cycle wall-time).
  * Per-symbol fill-lag scatter (catches wide-spread / illiquid stocks dominating the tail).
  * Cross-reference: did large fill lags correlate with losing trades?
  * Verdict against Threshold 1.

**Tooling.** Net new analysis script in `tools/h3_entry_lag_forensic.py`.
Audit-only (semantically neutral — analysis tool, no trader-code
change). May be implemented this week as a freeze-permitted
audit-only delivery, but no behavioural conclusion is published
until 2026-06-03.

---

## 6. Sign-off

| Role | Action | Timestamp |
|---|---|---|
| Operator | Read project review, raised review §1–§5, asked for in-writing pre-commitment to thresholds. | 2026-05-29 ~19:10 IST |
| Trading agent | Acknowledged review, drafted this document. | 2026-05-29 ~19:30 IST |
| Joint pre-commitment | This document is the operating contract. Decisions on 2026-06-05 / 2026-06-08 are made AGAINST these thresholds, not against new framings invented on the day. | 2026-05-29 |

**Document version:** v1.0 (2026-05-29). Any amendment requires
explicit operator + agent acknowledgement and a version bump. The
versioned diff is preserved in git history as the audit trail.
