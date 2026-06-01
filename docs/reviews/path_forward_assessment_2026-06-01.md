# Path-Forward Assessment — Post-v2.1 Wind-Down

**Date:** 2026-06-01 (Mon)
**Author:** trading agent (adviser persona) + operator joint decision
**Status:** DRAFT — operator decision: **fight on with proper architecture**
**Context:** Filed in continuation of [`brutal_review_2026-06-01.md`](brutal_review_2026-06-01.md)
Session 1–3 and [`strategy_reference_review_2026-06-01.md`](strategy_reference_review_2026-06-01.md).
Companion document: [`strategy_charter_v4_2026-06-01.md`](strategy_charter_v4_2026-06-01.md)
which is the technical charter that this decision-paper points at.

---

## 0. TL;DR

The operator has chosen, against the adviser's default recommendation, to
**continue building the algo through iterative experimentation rather
than wind the project down into a passive index portfolio**. This
document records that choice, the cost of the choice, the four
parallel research tracks that operationalise it, the mode-flag
architecture that lets the tracks run side-by-side without interfering
with each other, and the pre-committed kill-criteria that prevent
"fight to the end" from turning into "lose to the end".

The doc is **decision-recorded, not decision-made-better.** The adviser
voice's default at ₹120k capital remains: NIFTYBEES + GOLDBEES is the
honest answer. The operator's counter-position — that the project has
value as a portfolio-engineering exercise and as capital-growth
optionality independent of near-term P&L — is **defensible**, and is
the one being executed.

---

## 1. The decision

> *"I don't want to stop. I want to fight till the very end with
> multiple hit and trial. I believe we can figure out something."*
> — operator, 2026-06-01 12:09 IST

This is recorded verbatim because the rest of the document is
conditional on this stance. It is also recorded so that if, in 6
or 18 months, the experimentation has not produced edge, the
operator can re-read this and decide consciously whether the next
fight is still worth fighting — instead of drifting into it.

### 1.1 What the operator is choosing

| Choice | Adviser's default | Operator's actual |
|---|---|---|
| Path on Friday wind-down | A: Wind down + passive NIFTYBEES | B+: keep building, restructured + multi-mode |
| Strategy families to test | One (cross-asset trend) | Four (cross-asset trend + F&O paper + cointegration pairs + discretionary swing) |
| Capital deployed live | ₹0 (paper only) until ₹3-5L | Paper-only until each mode's `paper_to_live_threshold` met |
| Time horizon | 3-5 years to build capital | Continuous research, indefinite |
| Cost burn acceptance | Stop the ₹2,500/mo burn | Accept the burn as research budget |

### 1.2 What this choice is NOT

To prevent ex-post drift:

- It is **NOT** a rejection of the V25/V26 evidence. V25 PF 0.04 / V26
 PF 0.01 at AngelOne rates stands; v2.1 still winds down on Friday
 per [`wind_down_criteria_2026-06-05.md`](../freeze/wind_down_criteria_2026-06-05.md).
- It is **NOT** a license to revive frozen-list strategies. The
 `swing_combined_shorts`, `rsi_momentum_intraday`, ensemble, and
 XGBoost paths are dead per v3 charter §1 findings 1-4. New work
 starts from new hypotheses.
- It is **NOT** an unfreeze of `packages/strategies/` or risk-gate
 thresholds in the current code. Mode A (V27 cross-asset trend) is
 a NEW module that lands AFTER the Friday wind-down, in a new
 strategy file (`packages/strategies/swing_cross_asset_trend.py` or
 equivalent), not as edits to frozen files.
- It is **NOT** authorisation to live-trade F&O. F&O work is
 paper-mode and backtest-only until capital + evidence both clear
 thresholds documented in §5.

---

## 2. The cost of fighting (honest accounting)

Before the operator commits, the cost of "fight till the end" is
written down in numbers, not feelings. This is the section the
operator should re-read in 6 months.

### 2.1 Direct cost burn

| Line item | Per month | Per year |
|---|---:|---:|
| AngelOne SmartAPI subscription | ₹500 | ₹6,000 |
| Cloud VM (current daemon host) | ₹1,500 | ₹18,000 |
| Backtester VM (on-demand) | ₹500 | ₹6,000 |
| **Total burn** | **₹2,500** | **₹30,000** |

(Source: `data/self_sufficiency.json` plus operator's own figures.
Verify against actual cloud bills if these are stale.)

### 2.2 Opportunity cost

Capital not in NIFTYBEES while the algo project is running:

| Capital | NIFTYBEES @ 10% CAGR | NIFTYBEES @ 12% CAGR |
|---:|---:|---:|
| ₹120,000 | ₹12,000 / yr | ₹14,400 / yr |
| ₹300,000 | ₹30,000 / yr | ₹36,000 / yr |
| ₹500,000 | ₹50,000 / yr | ₹60,000 / yr |

### 2.3 Combined annual cost

| Capital | Burn | Opportunity cost @ 10% | Total cost to "fight" | Algo CAGR needed to break even |
|---:|---:|---:|---:|---:|
| ₹120,000 | ₹30,000 | ₹12,000 | **₹42,000** | **+35%** |
| ₹300,000 | ₹30,000 | ₹30,000 | **₹60,000** | **+20%** |
| ₹500,000 | ₹30,000 | ₹50,000 | **₹80,000** | **+16%** |
| ₹1,000,000 | ₹30,000 | ₹100,000 | **₹130,000** | **+13%** |

**The honest expectation for any retail-accessible systematic
strategy is 3-8% CAGR.** Every row of this table says the project
is operationally underwater unless the operator treats the burn
+ opportunity cost as **education + capital-growth-optionality
budget**, not income.

The operator's stance accepts this framing. The ₹42,000/year at
₹120k capital is the price of the research lab. Whether the lab
ever produces income is the open question; the lab existing is
the decision.

### 2.4 The non-monetary cost

| Item | Cost |
|---|---|
| Operator's hours/week on the project | ~10-20 hours |
| Cognitive overhead of running a live system | Real, hard to measure |
| Risk of bad-day errors (DB-corruption, accidental live order with paper-mode flag off) | Bounded but non-zero |
| Risk of project becoming an identity ("I'm an algo trader") that resists wind-down evidence | **The single largest hidden cost** |

§9 below has the pre-committed discipline list for the last one.

---

## 3. The four parallel tracks

The fight-till-the-end strategy is operationalised as **four parallel
research tracks**, each with its own hypothesis, kill-criteria, and
capital-deployment trigger. The mode-flag architecture in §4 lets
them coexist in one codebase without cross-contamination.

### Track A — Cross-asset trend following (cash equity)

| Field | Value |
|---|---|
| **Module slug** | `swing_cash` (mode) / `cross_asset_trend_v27` (variant) |
| **Hypothesis** | Diversified cross-asset trend on 50-80 instruments at daily timeframe under AngelOne CNC costs produces PF ≥ 1.2 over a 5-year backtest and beats NIFTYBEES buy-and-hold by ≥ 2% CAGR. |
| **What's new vs v3 swing** | Adds ETFs (GOLDBEES, SILVERBEES, BANKBEES, JUNIORBEES, NIFTYIETF) for genuine asset-class diversification; expands stock universe to Nifty 100/200; replaces the two simple rules with proper CTA-style signal (Donchian + volatility-targeted sizing + risk parity). |
| **Status** | Spec'd in [`strategy_charter_v4_2026-06-01.md`](strategy_charter_v4_2026-06-01.md) §3. Builds in **backtester first**, paper-mode second, live last. |
| **Backtester support** | EXISTS. `packages/research/backtest_ensemble.py` already supports daily-bar trading on equity universes with AngelOne charges (post-CHG-01..05). Extensions needed: ETF universe loader, Donchian signal module, vol-targeted sizer, NIFTYBEES benchmark in comparison report. ~1 week of code. |
| **Estimated dev effort to first backtest** | 1-2 weeks part-time |
| **Estimated dev effort to paper-trading** | 2-3 weeks after first backtest |
| **Kill criteria (hard, pre-committed)** | First backtest: PF < 1.2 on 5-year window OR CAGR < NIFTYBEES + 2%. Paper-trading: drawdown > 8% in any 30-day window OR 90-day cumulative net < 0%. |
| **Capital trigger to live** | ₹3 lakh + 6 months profitable paper. |

### Track B — F&O paper-mode (backtest + simulated live)

| Field | Value |
|---|---|
| **Module slug** | `swing_fno_paper` and `intraday_fno_paper` (two sub-modes) |
| **Hypothesis** | TWO sub-hypotheses, tested independently: (i) **Futures swing on Nifty/Bank Nifty futures with weekly-monthly hold under STT futures rates** produces PF ≥ 1.3. (ii) **Options-selling defined-risk spreads** (iron condors, credit spreads) on Nifty weekly options produce PF ≥ 1.2 with max-loss capped at spread width. |
| **Status** | **Backtester does NOT currently support F&O.** Spec'd in [`strategy_charter_v4_2026-06-01.md`](strategy_charter_v4_2026-06-01.md) §4 with required engine extensions enumerated. |
| **Backtester support** | DOES NOT EXIST. Required additions: (a) F&O bhav copy loader (NSE EOD), (b) options pricing (Black-Scholes for theoreticals or NSE-published IV for actuals), (c) expiry-day handling, (d) lot-size + tick-size constraints, (e) SPAN+exposure margin model, (f) per-leg STT/STT-on-exercise asymmetry, (g) brokerage F&O rate table. ~4-6 weeks of dev. |
| **Estimated dev effort** | 4-6 weeks for backtester support; +2 weeks to wire into paper-mode |
| **Kill criteria (hard, pre-committed)** | If the backtester extension itself isn't producing internally-consistent option premiums within 1.5σ of NSE-published EOD premia on a 10-day spot-check, the engine is unreliable and F&O backtests are uninterpretable — STOP and fix or abandon. After engine validation: PF < 1.2 on 3-year backtest = kill the variant. |
| **Capital trigger to live** | ₹5 lakh + 6 months profitable paper + Track A also profitable. F&O at retail capital ALONE is the single fastest way to ruin in Indian markets (per `strategy_reference_review_2026-06-01.md`, options-buying retail loss rate ~85%, F&O retail loss rate per SEBI 2024 ~89%). The capital trigger is intentionally aggressive. |

**Important honesty:** At ₹120k capital the operator **cannot live-trade
even one lot of Nifty futures** (lot = 75 × spot ~₹25,000 = ₹18.7 lakh
notional, SPAN+exposure margin ~₹2.8 lakh). Options buying is the
only F&O activity possible at ₹120k, and it is the
**lowest-expected-value F&O activity**. F&O at this capital is
**backtest+paper learning only**. The mode flag will REFUSE to enable
`fno_live` unless `capital_inr >= 500000` (see §4.4 capital gates).

### Track C — Cointegration pairs research (low-priority)

| Field | Value |
|---|---|
| **Module slug** | `pairs_cointegration_research` |
| **Hypothesis** | Mean-reverting cointegrated pairs among Nifty 200 stocks (selected by Augmented Dickey-Fuller test + half-life < 30 days) produce a PF > 1.1 with z-score-based entry at ±2.0σ and exit at 0 or stop at ±3.5σ. |
| **Status** | Lowest priority of the four tracks. Listed for completeness because the operator explicitly asked about institutional stat-arb. The published edge is largely decayed since ~2010 per `strategy_reference_review_2026-06-01.md` §1.2; this track exists so the operator has a self-verified "no, cointegration doesn't work for me either" result and can stop wondering. |
| **Backtester support** | PARTIAL. The daily-bar engine handles individual symbols; pairs require a coupling layer (concurrent long-short positions on two symbols with paired entry/exit). ~1-2 weeks of dev. |
| **Estimated dev effort** | 1-2 weeks |
| **Kill criteria** | First backtest: if PF < 1.1 on top-20 pairs over 5 years, **declare research closed** and re-read `strategy_reference_review_2026-06-01.md` §1.2. |
| **Capital trigger to live** | N/A — research-only. If profitable in backtest, it joins Track A's queue for paper, not its own. |

### Track D — Discretionary swing trading (parallel, non-algo)

| Field | Value |
|---|---|
| **Module slug** | n/a — operator-manual, no software |
| **Hypothesis** | The same V27 cross-asset trend rules, executed **manually** by the operator (15-30 min/day scan + place orders), produce the same edge as the algo with zero infrastructure cost. |
| **Status** | Available immediately. Operator picks 15 instruments from the V27 universe, runs the Donchian-breakout check manually each evening, places GTT/AMO orders for next-day open. Logs trades in a simple spreadsheet. |
| **Backtester support** | N/A — runs in real markets on real money (or paper, operator's choice). |
| **Estimated dev effort** | 0 |
| **Kill criteria** | 90-day net loss > ₹3,000 OR operator decides the time cost isn't worth it. |
| **Capital trigger to live** | Same as Track A live trigger (₹3L+), OR operator can run it on the existing ₹120k as an explicit "this is a learning trade, not income" budget. |

**Why this track exists:** It is the **falsifier for Track A**. If Track
D (manual, same rules) makes money but Track A (algo, same rules) does
not, the bug is in the agent's execution layer, not in the strategy.
If Track A makes money but Track D does not, the operator's
discretionary discipline is the bottleneck. If neither makes money,
the rules don't have edge and Track A's algo work is wasted compute.
This is a cheap, fast, decisive cross-check.

### Tracks NOT pursued (and why)

| Not pursued | Why |
|---|---|
| **Virtu-style HFT market making** | Structurally retail-impossible (SEBI DMM registration, ₹2-5 Cr capital floor, co-location, microsecond latency). Backtester cannot simulate. See `strategy_reference_review_2026-06-01.md` §1.1. |
| **Renaissance Medallion replica** | Structurally retail-impossible (5,000 simultaneous positions, decades of tick data, 12:1 leverage with prime broker). The cointegration-pairs track (Track C) is the entry-level retail surrogate; if it doesn't work, the Medallion-class hypothesis is closed for this operator. |
| **5-min XGBoost direction prediction** | Already falsified at AUC 0.49 / 271k samples per v3 charter §1 finding 1. Reviving it would be the exact "debug into oblivion" anti-pattern the charter §10.5 R1 was designed to prevent. |
| **5-min intraday ensemble (revive v2.1)** | V20–V25 all PF < 1.0 at AngelOne rates per `findings/charges_pf_adjustment_2026-06-01.md`. v2.1 is being wound down on Friday for cause. |
| **Leverage on negative-EV strategies** | Mathematically guaranteed to accelerate losses. Not a strategy. |

---

## 4. The mode-flag architecture

To run four tracks in parallel without code-conflicts or accidental
live-trade leakage, the agent gains a **mode dispatcher** — a single
config-driven switch that determines which strategy modes are
enabled, in which mode (backtest / paper / live), and with what
capital allocation.

### 4.1 Why this matters

Today, the agent has one mode (`swing_combined_shorts` intraday) and
one runtime (paper). Adding four tracks without a clean dispatcher
would cause:

- Strategies stepping on each other's positions ("Track A wants long
 RELIANCE; Track C wants short RELIANCE in the pair").
- Capital double-allocation (both tracks size to 100% of available
 cash).
- Live-vs-paper accidents (a flag intended to be paper-mode flips
 live because the dispatcher doesn't enforce mode-per-strategy).
- Backtest results unreliable because the dispatcher doesn't isolate
 mode-specific cost models (Track A uses CNC delivery, Track B uses
 F&O rates — they CANNOT share one charges object).

The mode-flag architecture solves all four with one schema change.

### 4.2 The config schema (proposed, draft in `strategy_charter_v4_2026-06-01.md` §5)

```yaml
strategies:
 modes:
 # Currently active modes (v2.1 legacy — will be wound down Friday)
 swing_combined_shorts:
 enabled: false # Friday wind-down
 mode: paper # paper | live | backtest_only
 capital_allocation_pct: 0
 runtime: intraday_mis
 frozen_until: 2026-06-05

 # New post-v3 modes (default disabled until built)
 swing_cash_v27:
 enabled: false
 mode: backtest_only # advance to paper after backtest passes
 capital_allocation_pct: 60 # of total deployed capital
 runtime: swing_cnc
 backtester_variant: cross_asset_trend_v27
 paper_to_live_threshold:
 capital_inr: 300000
 paper_days_profitable: 180

 swing_fno_paper:
 enabled: false
 mode: paper # never auto-promote to live below capital gate
 capital_allocation_pct: 20
 runtime: swing_fno
 backtester_variant: fno_futures_swing_v1
 paper_to_live_threshold:
 capital_inr: 500000
 paper_days_profitable: 180
 track_a_concurrent_profitable: true

 intraday_fno_paper:
 enabled: false
 mode: paper
 capital_allocation_pct: 10
 runtime: intraday_fno
 backtester_variant: fno_options_credit_spreads_v1
 paper_to_live_threshold:
 capital_inr: 500000
 paper_days_profitable: 180

 pairs_cointegration_research:
 enabled: false
 mode: backtest_only
 capital_allocation_pct: 0
 runtime: swing_cnc
 backtester_variant: cointegration_pairs_v1
 paper_to_live_threshold:
 never_auto_promote: true # research-only

 # Total capital_allocation_pct of all `mode in (paper, live)` must
 # sum to ≤ 100; dispatcher enforces this on config load.
```

### 4.3 The dispatcher contract (high-level)

The dispatcher is added to the strategy router and enforces:

| Rule | Enforcement |
|---|---|
| Only modes with `enabled: true` are considered each cycle | hard `assert` at signal-generation time |
| `mode: backtest_only` modes are skipped by the live daemon entirely | router refuses to register backtest_only modes with the live orchestrator |
| `mode: paper` modes route orders to the paper-broker stub even if `broker.live: true` in config | enforced in the order-placement layer with double-check; logs `[MODE-PAPER]` on every paper-routed order |
| Capital allocation per mode is computed once at daemon-startup and not re-derived per-cycle | prevents drift; allocation changes require restart |
| `paper_to_live_threshold` gates a mode's promotion to `mode: live` | promotion requires explicit operator action (config edit + restart); the threshold is documentation, not auto-promote |
| Cost-model is per-mode | Track A uses `CashCNCCharges`; Track B uses `FnoFuturesCharges`; Track D-options uses `FnoOptionsCharges`. Separate classes, separate test suites. |
| Position-uniqueness is enforced per-mode | Track A may hold long RELIANCE; Track C's pair (RELIANCE-ONGC pair) is a SEPARATE position with its own `mode_tag` field in the DB — they do not net |

### 4.4 Capital gates (the "no, you cannot" rules)

The dispatcher REFUSES to enable certain mode combinations until
capital thresholds are met. These are pre-committed gates, not
operator-pleadable:

| Mode | Min capital to `enable: true` in live | Min capital to enable in paper |
|---|---:|---:|
| `swing_cash_v27` | ₹300,000 | ₹0 (always paper-eligible) |
| `swing_fno_paper` | ₹500,000 + Track A live + 6mo profitable | ₹0 (paper-only at all capital levels) |
| `intraday_fno_paper` | ₹500,000 + Track A live + 6mo profitable | ₹0 (paper-only at all capital levels) |
| `pairs_cointegration_research` | **never auto-promote — research-only** | ₹0 |

The dispatcher reads `capital_inr` from `data/self_sufficiency.json`
at startup and refuses to honor `mode: live` if the gate isn't met.
This prevents the operator from impulsively flipping a flag in a bad
mood. The override requires a config-file edit with an explicit
`override_capital_gate: "I accept ruin risk"` value — typing that
string is the friction.

---

## 5. Operating cadence: how the four tracks actually run

The four tracks do not execute simultaneously from day one. The
operator and adviser have a rolling 3-month build-and-test cadence,
each phase has its own focus mode:

### Phase 0 — Wind-down (2026-06-05 to 2026-06-07)

- Friday 06-05: v2.1 verdict meeting; wind-down executed per
 [`wind_down_criteria_2026-06-05.md`](../freeze/wind_down_criteria_2026-06-05.md).
- Saturday-Sunday: agent in maintenance mode. All `swing_combined_shorts`
 disabled. DB snapshot. Final EOD reports archived.

### Phase 1 — Track A backtester build (2026-06-08 to 2026-06-21, ~2 weeks)

- Implement V27 cross-asset trend backtester variant per charter §3.
 Universe loader, Donchian signal, vol-targeted sizer, risk-parity
 allocator, NIFTYBEES benchmark.
- Run V27 over 5-year window on full universe. Inspect kill criteria.
 PF ≥ 1.2 and CAGR-vs-NIFTYBEES ≥ +2% required to advance.

### Phase 2 — Track A paper-mode (2026-06-22 to 2026-09-22, ~3 months)

- If Phase 1 passes, deploy Track A in `mode: paper` on the agent.
 Daemon runs daily at market close, generates orders, logs paper-fills.
- Operator observes daily; weekly journal entry per `daily-log` skill.
 Brutal-review pass every 4 weeks.
- Kill criteria: 30-day drawdown > 8% OR 90-day cumulative net < 0%.

### Phase 3 — F&O backtester build (2026-09 to 2026-11, ~2 months)

- If Phase 2 still alive, build F&O backtester extensions per
 charter §4. This is the largest pure-engineering investment in
 the plan; the operator should re-evaluate at Phase 2 EOQ whether
 it's worth the dev time vs continued Track A paper-extension.
- Test futures swing variant first (simpler). Options spreads
 variant second.

### Phase 4 — Multi-mode paper (2026-12 to 2027-03, ~3 months)

- If F&O backtester clears its own validation (charter §4 kill
 criteria), enable `swing_fno_paper` and `intraday_fno_paper`
 alongside Track A `swing_cash_v27`. All paper.
- Operator tracks per-mode P&L attribution monthly.
- Capital scaling decision (Phase 5 trigger) based on aggregate
 paper performance over the 90 days.

### Phase 5 — Live with capital scale-up

- Trigger: ₹300k+ capital available AND Track A 180 days profitable
 in paper.
- Enable `swing_cash_v27` in `mode: live` per capital gate §4.4.
- F&O remains paper-only until ₹500k AND Track A 180 days live profitable.
- Pairs research stays research-only regardless.

### Phase 6 and beyond — review

Quarterly brutal-review + adviser path-forward-assessment refresh.
Phase 6 is intentionally unspec'd today; the world will look
different in 12 months and the operator should not pre-commit
beyond what the evidence supports.

### Operator workload estimate

| Phase | Hours/week (operator) | Calendar duration |
|---|---:|---|
| Phase 0 | 5 | 1 week |
| Phase 1 | 10-15 | 2 weeks |
| Phase 2 | 3-5 | 3 months |
| Phase 3 | 15-20 | 2 months |
| Phase 4 | 5-10 | 3 months |
| Phase 5+ | 3-5 ongoing | indefinite |

Cumulative through Phase 4: ~300-450 operator-hours. At an
opportunity-cost rate of ₹500/hr (operator's professional rate
proxy), the time investment is ₹150,000-225,000. **This is the
single largest cost line; the cloud/AngelOne burn is small in
comparison.** The operator should account for it as part of the
"fight till the end" decision.

---

## 6. Pre-committed kill criteria across all tracks

Every track has its own kill criteria (§3). In addition, the
**project as a whole** has pre-committed shutdown triggers:

| # | Trigger | What it kills |
|---|---|---|
| **PK1** | 18 calendar months from 2026-06-08 (i.e. by 2027-12-08) with no track ever cleared its paper-to-live capital gate | Entire algo program. Liquidate any paper-tracked variants. Switch all capital to NIFTYBEES. |
| **PK2** | Any single track fires its kill criteria 3 separate times across iterations | That track is permanently closed (no Track A v2, v3, v4…). |
| **PK3** | Cumulative direct cost burn exceeds ₹100,000 with no track in paper-profitable state | Entire algo program. (At ₹30k/yr burn, this is ~3.3 years.) |
| **PK4** | Operator life event (job change, family commitment, health) reduces available hours below 5/week for 90+ days | Pause all live modes; backtest research can continue passively. |
| **PK5** | A bug ships to production that causes a real-money loss > ₹10,000 | Mandatory 30-day live-trading freeze + full code-bug-review + incident-response + postmortem. Capital reduces to ₹0 live until 4 weeks of clean paper. |

**PK1 is the most important.** Without it, "fight till the end"
becomes a euphemism for "never quit". 18 months is generous
relative to the evidence required to validate a daily-timeframe
trend strategy.

---

## 7. Honest expectations — what's likely to actually happen

The adviser's odds-of-outcome calibration, as of 2026-06-01:

| Outcome | Probability (adviser estimate) | What it looks like |
|---|---:|---|
| Track A backtest passes, paper passes, lives, beats NIFTYBEES by ≥ 2% CAGR | **10-15%** | Best case. Operator has a real edge, scales capital, project becomes net-positive after 3-5 years. |
| Track A backtest passes, paper trades flat, never lives | **25%** | Common case. Backtest looks good, real markets disagree. Project becomes an interesting learning exercise but no income. |
| Track A backtest fails (PF < 1.2 or CAGR < NIFTYBEES + 2%) | **35-40%** | Most likely. Cross-asset trend at retail without micro-feature engineering has compressed edge in the 2020s. Track A killed at first gate. |
| Track B F&O backtester completes and surfaces edge | **5-10%** | Low — F&O retail evidence is overwhelmingly negative. Most likely outcome: months of dev for "no, options selling doesn't have retail-accessible edge either". |
| Track C cointegration finds edge | **<5%** | Stat-arb pairs trading is the most-published, most-decayed retail strategy. Almost certainly dead. |
| Track D discretionary makes money operator can't replicate in algo | **15-20%** | Underrated outcome. Operator's discretionary discipline + filter + ability to wait can outperform a mechanised version of the same rules. If this happens, the conclusion is "you don't need the agent — trade manually". |
| Project hits PK1 (18mo timeout) without any track surviving | **40-50%** | The realistic median outcome the operator should plan for. |
| Operator hits PK4 or PK5 (life event / production bug) | **10-15%** | Real risk over 18 months. |

The probabilities don't sum to 100% because the rows are not
mutually exclusive (e.g. Track A backtest passing AND Track D
making money are positively correlated; PK1 fires AND no track
survives are the same event).

**The key takeaway:** the most likely outcome (combining the
Track-failure rows and PK1) is **"~3-12 months of build, then a
sober conclusion that the strategies don't have retail-accessible
edge at this capital scale, and a wind-down to NIFTYBEES with a
better-engineered codebase and meaningful learnings."**

That is **not a failure outcome**. A research lab that proves
its own hypothesis wrong has done its job. The operator should
internalise this NOW so that the wind-down 12 months from now,
if it comes, is a graceful conclusion rather than an emotional
defeat.

---

## 8. What the adviser is explicitly NOT saying

To prevent ex-post misquoting:

1. The adviser is **NOT** saying "this will work". The adviser
 is saying "this is a defensible path IF the operator accepts the
 cost framing in §2 and the kill criteria in §6".
2. The adviser is **NOT** saying "cross-asset trend will produce
 5-7% CAGR". The adviser is saying "if it produces any positive
 edge at retail scale, 5-7% CAGR is the upper end of the realistic
 range; most retail attempts produce 0-3% after costs."
3. The adviser is **NOT** saying "F&O paper-mode is safe". The
 adviser is saying "F&O backtest+paper, with the capital gate
 enforced, has bounded downside and a chance of producing
 educational evidence. F&O live at retail capital is a documented
 negative-expected-value activity per SEBI 2024."
4. The adviser is **NOT** saying "the cointegration track will
 produce edge". The adviser is recommending it ONLY as a
 closed-form falsifier so the operator can stop wondering about
 stat-arb.
5. The adviser **IS** saying that the engineering value of the
 codebase produced by this multi-track work is real and durable
 even if no track produces trading income. The mode-flag
 architecture, the F&O backtester extensions, the risk-parity
 allocator — these are portfolio assets in the
 software-engineering-skills sense.

---

## 9. Anti-temptation discipline (per-operator, pre-committed)

The same list from `freeze_v3.0_charter_2026-05-30.md` §9.1, adapted
to multi-track:

1. **Do not iterate strategy mid-phase.** If Phase 2 paper is running
 Track A v27, do not edit the signal logic until the 90-day kill
 criterion is settled.
2. **Do not add a 5th track mid-project.** The four tracks are
 pre-committed. Adding (e.g.) "intraday cash with ML" because Track
 A is boring is the same anti-pattern as v2.1's "add another
 strategy to the ensemble".
3. **Do not skip the backtest gate to go straight to paper.** The
 backtester is the cheap falsifier. Skipping it forfeits the only
 cheap layer.
4. **Do not skip the paper gate to go straight to live.** Paper is
 the only test of execution + market microstructure interaction.
 Backtest-to-live is the classic blow-up path.
5. **Do not increase capital allocation per the spreadsheet's "we
 could afford it" view.** The capital gates in §4.4 are pre-committed.
6. **Do not enable a NEW mode flag before the previous mode has cleared
 its kill criteria for 90 days.** One thing at a time.
7. **Do not interpret a single profitable month in paper as edge.**
 The 90-day window is the discipline; one-month bumps are noise.
8. **Do not amend this document under emotional load.** If after a
 bad week the operator wants to "rewrite the plan", they wait 7
 days before doing so. Edits made within 7 days of a kill-criterion
 trigger are flagged in `changes-done` with
 `flag: under_emotional_load`.

---

## 10. Open questions for operator decision

These questions the adviser cannot answer alone — operator decides
before Phase 1 begins:

| # | Question | Adviser's lean (non-binding) |
|---|---|---|
| Q1 | Do you accept the cost framing in §2.3 (effectively, ₹42k/year cost at ₹120k capital, framing as "research lab budget" not "income strategy")? | **Operator should answer in writing before Phase 1 starts.** |
| Q2 | Do you accept PK1 (18-month timeout) as pre-committed? | Yes. Without PK1 this project is open-ended which is its own failure mode. |
| Q3 | Does the mode-flag architecture (§4) belong in v3 (one mode at a time, simpler) or v4 (multi-mode from the start)? | v4 from the start. Building it later requires a rewrite. Pay the architecture cost now. |
| Q4 | Track C (cointegration) — include as a build target, or only document as "decided not to test"? | Build, but as the lowest priority. Costs 1-2 weeks. The downside of skipping is the operator wonders for years whether they "missed Renaissance". |
| Q5 | Should Track D (discretionary) start BEFORE Track A backtest is built? | **Yes.** Track D requires zero code. The operator can start it next Monday on the existing V25/V26 universe with Donchian rules and run it in parallel. This generates DATA the adviser can use during Track A's backtest review. |
| Q6 | Is the operator willing to share the dispatcher / mode-flag config schema with the adviser for review BEFORE the first code lands? | Strongly recommended. The schema is the spine; getting it wrong costs months. |
| Q7 | What's the SHA / commit-message convention for post-v3 work? Same as v2.1 (`docs(...)`, `feat(...)`, etc.)? | Yes. No new convention needed. Add `mode:` tag to the body for mode-affecting commits. |

---

## 11. Cross-references

* [`brutal_review_2026-06-01.md`](brutal_review_2026-06-01.md) — Session 1, 2, 3 evidence base
* [`strategy_reference_review_2026-06-01.md`](strategy_reference_review_2026-06-01.md) — Virtu / Renaissance / Trend / Retail strategy critique
* [`strategy_charter_v4_2026-06-01.md`](strategy_charter_v4_2026-06-01.md) — **companion charter; the technical spec**
* [`../freeze/freeze_v3.0_charter_2026-05-30.md`](../freeze/freeze_v3.0_charter_2026-05-30.md) — v3 swing charter (being wound down Friday)
* [`../freeze/wind_down_criteria_2026-06-05.md`](../freeze/wind_down_criteria_2026-06-05.md) — Friday verdict gate
* [`../freeze/verdict_meeting_packet_2026-06-05.md`](../freeze/verdict_meeting_packet_2026-06-05.md) — Friday meeting packet
* [`../findings/findings_log_2026-06-01.md`](../findings/findings_log_2026-06-01.md) — CHG-01..05 / NUM-10 evidence
* [`../findings/charges_pf_adjustment_2026-06-01.md`](../findings/charges_pf_adjustment_2026-06-01.md) — AngelOne PF re-derivation

---

*Last updated: 2026-06-01 12:30 IST. Decision recorded in continuation
of operator's 12:09 IST stated intent. Document is non-amendable
under emotional load (per §9 rule 8) for 7 days after any kill
criterion fires.*
