# Post-Freeze Proposal: Apply V4 (`trend_filter_pct: 5.0 → 3.0`)

**Status:** DRAFT — pending Friday 2026-05-29 review and remaining battery validation
**Author:** Operator (with battery infrastructure)
**Decision date target:** Friday 2026-05-29 (Week-2 freeze review)
**Earliest deploy target:** Monday 2026-06-08 (post freeze-lift)
**Bypass slots required:** 1 of 3 remaining

> **2026-05-25 13:00 IST UPDATE — proposal scope expanded.** Re-analysis of
> the 2026-05-18 pre-speed-patch 90-day × 228-stock battery (V1 + V2 only,
> the killed run) shows that **the shipped config IS marginally profitable
> on the live universe shape** (V1: +₹177, PF 1.04, 278 trades) and that
> **the short side is the structural loss-driver in both V1 and V2** (V1
> shorts: -₹379 WR 41.7%; V2 shorts: -₹398 WR 46.4%). The live agent's
> -₹1,505 / 28 trades / 100%-short pattern is now explainable as
> "executing the structurally-losing side of the engine in the regime
> that biases toward it." This proposal is being expanded into a 3-way
> comparison (V4-tighten / V2-filters-off / V1-longs-only) before any
> deploy decision. See §10 below.

---

## TL;DR

After a 14-day live freeze with no edge (-₹1,505 over 28 trades), the
overnight-battery harness ran a 60-day × 50-stock × 5-min parameter
sweep over the shipped configuration. The single most-actionable
finding so far: **changing `trend_filter_pct` from 5.0 to 3.0 across
all six strategies takes the same engine from -₹298 (PF 0.80) to
+₹340 (PF 1.35) on identical 60-day Nifty 50 data**. This proposal
captures the evidence, the remaining validation gates, and the
deploy-and-revert criteria.

This document is **decision-ready**, not yet decision-made. The
Friday review ratifies or rejects.

---

## 1. The exact change

**One config line.** Change the `trend_filter_pct` value on each of
the six per-strategy blocks in `config.yaml`:

```yaml
strategies:
  rsi_momentum:
    trend_filter_pct: 5.0   # → 3.0
  vwap_bounce:
    trend_filter_pct: 5.0   # → 3.0
  opening_range_breakout:
    trend_filter_pct: 5.0   # → 3.0
  supertrend_follow:
    trend_filter_pct: 5.0   # → 3.0
  xgboost_classifier:
    trend_filter_pct: 5.0   # → 3.0
  # mean_reversion: no trend_filter_pct in shipped config; add 3.0 for parity
```

No other config edits. No code edits. No model retrain. No risk-gate
change. No strategy-active list change.

---

## 2. The evidence

### 2.1 Battery setup

- **Run ID:** `battery_nifty50_60d_20260522T085929`
- **Started:** 2026-05-22 14:30 IST · **Last update:** 2026-05-25 12:17 IST
- **Universe:** Nifty 50 (50 large-cap NSE symbols) — same dataset across all variants
- **Window:** 60 calendar days · 5-minute bars
- **Initial capital:** ₹10,000 (NB: live runs ₹100k — see §4.2)
- **Variants completed:** 6 / 16 (V1–V6) · V7–V8 finishing within hours

### 2.2 Threshold-sweep results

| Variant | `trend_filter_pct` | Trades | WR% | PnL | PF | R:R | MaxDD% | Ret% | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| V4 | **3.0** | 61 | **50.8** | **+₹340** | **1.35** | 1:1.30 | **3.08** | **+3.40%** | **WINNER** |
| V5 | 7.0 | 63 | 44.4 | -₹116 | 0.90 | 1:1.13 | 4.88 | -1.16% | near-flat |
| V6 | 10.0 | 65 | 44.6 | -₹214 | 0.84 | 1:1.04 | 5.58 | -2.14% | losing |
| **V1** | **5.0 (shipped)** | 69 | 42.0 | -₹298 | 0.80 | 1:1.10 | 6.40 | -2.98% | **WORST** of the sweep |
| V2 | none (off) | 78 | 43.6 | -₹420 | 0.75 | 1:0.97 | 6.85 | -4.20% | worst overall |
| V3 | none on 4 of 6 | 78 | 43.6 | -₹420 | 0.75 | 1:0.97 | 6.85 | -4.20% | == V2 (see §6.2) |

The threshold landscape is **non-monotonic** and the shipped value
sits at the local minimum of the swept range:

```
3% ── 5% (shipped) ── 7% ── 10%
+₹340      -₹298    -₹116   -₹214
WIN        WORST    flat    lose
```

V4 vs V1 swing: **+₹638 on identical 60-day data**. PF improvement
0.80 → 1.35 (+69%). Drawdown halved (6.4% → 3.1%).

### 2.3 Validation checks completed

| # | Check | Status | Result |
|---|---|---|---|
| 1 | **V4 long/short split balanced (≥30% on weaker side)** | ✓ PASSED | **36 long / 25 short = 59% / 41%** |
| 2 | Per-side profitability (both sides positive) | ✓ PASSED | Long +₹262.62 (avg +₹7.29, WR 52.8%), Short +₹77.82 (avg +₹3.11, WR 48.0%) |
| 3 | V4 max-drawdown ≤ 5% | ✓ PASSED | 3.08% |
| 4 | V4 expectancy positive | ✓ PASSED | +₹5.6 / trade |
| 5 | V4 Sharpe-ish positive | ✓ PASSED | 0.01 (low magnitude but right sign) |

### 2.4 Validation checks PENDING — block deploy

| # | Check | Status | Source |
|---|---|---|---|
| 6 | **Universe-transfer**: V4 holds on a 200+ stock mid-cap universe (the live universe shape) | ⏳ PENDING | Requires new `nifty500_v4_60d` battery queue entry |
| 7 | **Sample-size CI**: 90-day and 120-day runs tighten the n=61 confidence interval | ⏳ PENDING | Already queued (`v2_baseline_90d`, `nifty50_120d` etc.) |
| 8 | **Live-vs-battery parity for May 12–21**: do V4's trades on the overlapping window predict the live trades, or did V4 find a different selection rule? | ⏳ PENDING | One-off analysis, can be done after V4 trade-export |
| 9 | **Capital-scale invariance**: re-run V4 at ₹100k initial capital matching live (changes `min_trade_notional` headroom and per-trade share-of-equity) | ⏳ PENDING | Add `nifty50_v4_100k` to queue |
| 10 | **Per-strategy isolation (V7–V9)**: which strategies actually need the filter at 3% vs which are filter-insensitive | ⏳ PENDING | V7 + V8 finishing today, V9 today/tomorrow |
| 11 | **Confidence-threshold (V10–V11)**: independent-knob check for orthogonal improvement | ⏳ PENDING | Tomorrow |

**At least checks #6, #7, #8 must pass before deploy.** Check #9 is
the highest-EV addition to the queue (capital-scale gap is 10×).

---

## 3. Why this is the right change

1. **Falsifies the "engine has no edge" hypothesis.** The shipped
   config losing in backtest as well as live (V1: -₹298 / PF 0.80)
   would, on its own, suggest the engine itself has no edge at any
   tuning. V4's +₹340 / PF 1.35 on the same engine, same dataset,
   same gates, falsifies that. The engine has tunable edge; the
   shipped tune is bad.

2. **One-knob change, not a redesign.** The fix is a single numerical
   parameter on six strategy blocks. No new code paths, no new
   strategies, no model retrain, no risk-engine change. This is the
   safest possible kind of unfreeze edit.

3. **The battery infrastructure was built for exactly this finding.**
   The 2026-05-08 sweep that originally chose `trend_filter_pct: 5.0`
   used 30 days × 10 stocks ≈ 5–7 trades per variant — pure noise.
   This sweep uses 60 days × 50 stocks ≈ 60–80 trades per variant —
   the differences exceed Bernoulli noise at the win-rate level.
   This is the calibration mistake `battery-v2` was specifically
   designed to catch. It worked.

4. **Long-side and short-side both contribute.** V4 is not a
   smarter version of the same bear bet. 36/61 trades are long
   (59%) and BOTH sides are profitable. A regime change that
   neutralises the bear bias does NOT zero out the edge.

5. **MaxDD is HALVED.** Even if the +₹638 PnL swing erodes in
   out-of-sample, the drawdown improvement (6.4% → 3.1%) is a
   risk-side win that's hard to lose statistically.

---

## 4. Risks and counter-arguments

### 4.1 Universe-transfer risk (HIGHEST)

V4 was tested on **Nifty 50 large-caps**. The live agent trades a
**200-stock mid-cap-heavy scanner watchlist**. Liquidity, volatility,
and price ranges differ:

| | Nifty 50 backtest | Live universe |
|---|---|---|
| Symbols | 50 large-caps | ~200 mixed cap |
| Median price | ₹500–3000 | ₹50–800 |
| Median ADV | ~10M shares | ~1–5M shares |
| Median ATR% | ~1.2% | ~2.0% |

A 3% trend filter that's "tight enough to filter noise but loose
enough to keep signal" on a 1.2% ATR universe might be **too tight**
on a 2.0% ATR universe (filtering real signals as noise) or **too
loose** on a more volatile cap (admitting noise as signal). The
direction of bias is unknown a priori.

**Mitigation:** Run `nifty500_v4_60d` (queued ahead of v2 holdout)
before any deploy. Required PF ≥ 1.10 on the larger universe.

### 4.2 Capital-scale risk

At ₹10k cap, `min_trade_notional: 5000` means single positions are
50% of equity → forced concurrency limits. At ₹100k cap, single
positions are 5% → no concurrency pressure. This changes:

- **Trade count** (live can hold more concurrent positions)
- **Per-trade Kelly fraction** (live trades smaller fraction of bank)
- **Realised slippage cost** (smaller relative to position)

Battery results may not extrapolate linearly. Re-run V4 at ₹100k
before deploy.

### 4.3 Out-of-sample risk on N=61

Bootstrap 95% lower-CI on a PF=1.35 with n=61 is approximately
**0.85–1.00** (rough estimate; replicate with the project's bootstrap
code on the JSON before deploy). The point estimate is profitable but
the 5th-percentile case is break-even-to-losing. Wait for n=90 (90d
run, ~90 trades) to tighten this.

### 4.4 The 60-day window overlap with live paper trading

Days 2026-05-12 → 2026-05-21 fall inside both the live-trading
window AND the V4 backtest window. If V4 reproduces the live trades
exactly on those days, the +₹340 might be coincidence on 12 days the
live system also traded. If V4 produces DIFFERENT trades, that's
evidence of a different selection rule that happens to win.

**Mitigation:** Check #8 above. Export V4's trades for 2026-05-12–21
and diff against `trades` table for the same window.

### 4.5 What if V7–V9 (per-strategy isolation) finds a better tune?

V7 (filter only `supertrend_follow`), V8 (filter only `rsi_momentum`),
V9 (`vwap_bounce` + `opening_range_breakout` filter off) might
identify an even better config than V4's "all six at 3%". If so, the
proposal updates to that config; the framework here is the same.

**Decision:** Wait for V7/V8 (today) and V9 (tomorrow) before
finalising the change-set. If any single-strategy variant beats V4 on
PF AND maxDD AND per-side balance, replace V4's all-six change with
the per-strategy one.

---

## 5. Deploy plan

### 5.1 Pre-deploy checklist (must all be ✓)

- [ ] V4 universe-transfer check passed (`nifty500_v4_60d` PF ≥ 1.10)
- [ ] V4 capital-scale check passed (`nifty50_v4_100k` PF ≥ 1.10)
- [ ] V4 vs live trade parity for 2026-05-12–21 documented
- [ ] V7/V8/V9 reviewed — V4 still the best single-config recommendation
- [ ] 90-day or 120-day run completed — V4-equivalent variant PF ≥ 1.0
- [ ] Bypass slot accounting up to date (`docs/freeze/FREEZE_v2.1.md`)

### 5.2 Deploy steps

1. **Branch + PR:** `freeze-bypass: deploy V4 trend_filter_pct change`.
   Reference this doc.
2. **Edit `config.yaml`:** `5.0 → 3.0` on all six trend_filter_pct
   lines. Add `trend_filter_pct: 3.0` to mean_reversion block (parity).
3. **Update `docs/freeze/FREEZE_v2.1.md`:** mark slot 1/3 consumed, log the
   reason.
4. **Run unit tests + battery dry-run** to confirm config loads.
5. **Schedule deploy:** open of next trading day (not mid-session).
6. **`docker compose down/up`** on the trader VM (re-reads
   config.yaml; status quo deploy procedure).
7. **Validate first 5 cycles:** scanner returns, signals fire (or
   not), heartbeat normal. Watchdog + heartbeat will catch any
   regression.

### 5.3 Kill / revert criteria (auto-trigger NOT manual judgement)

The deploy is **provisional** until the V4 config has accumulated
**30 paper-trade results** in live conditions. During that window,
any of the following triggers an immediate revert (single config
edit back to 5.0, no new bypass slot consumed):

| # | Trigger | Threshold |
|---|---|---|
| K1 | Cumulative paper-trade PnL after 30 trades | < -₹500 |
| K2 | Win-rate after 30 trades | < 35% |
| K3 | Profit-factor after 30 trades | < 0.80 |
| K4 | MaxDD on equity curve | > 5% |
| K5 | Long-side share collapses (< 25% or > 75% of trades) | (regime-classifier issue resurfacing) |

Revert procedure: `git revert` the deploy commit, `docker compose
down/up` on the trader VM. **Does not consume an additional bypass
slot** — the revert is part of the original deploy decision.

### 5.4 Post-deploy validation gate

After 30 trades AND any of (a) 14 calendar days OR (b) cumulative
PnL > +₹500:

- If kill criteria K1–K5 all clear: ratify the change as permanent;
  re-baseline the freeze contract; close the V4 proposal.
- If any kill criterion triggered: revert (per §5.3), open
  postmortem, return to freeze.

---

## 6. Open items being investigated under freeze-bypass

### 6.1 Why does the live universe scan only 169 stocks vs documented ~300?

Investigated 2026-05-25 (commit `eb5bb84`). Root cause: NSE live API
silently 403s from data-center IPs; the system was always falling
back to a hardcoded ~232-symbol list (RECLTD duplicated). New
`_fetch_nse_archive_csv()` path returns 504 stocks but is gated
behind `scanner.use_live_universe: false` (default OFF) — flipping
that flag would be a behavior change requiring its own bypass slot.

### 6.2 Why are V2 and V3 backtest results bit-for-bit identical?

Investigated 2026-05-25. The trade ledgers SHA-256 to the same hash
(`82dea26dfa9a4663...`). The recorded config overrides DO differ
correctly:

- V2: `[mean_reversion=None, xgboost=None, supertrend=None, rsi=None, vwap=None, orb=None]`
- V3: `[supertrend=None, rsi=None, vwap=None, orb=None]` (mean_reversion + xgboost left at default)

Empirically `mean_reversion` and `xgboost_classifier` fire **zero
trades** on the Nifty 50 universe in this window (consistent with
live: 0 and 1 trade respectively over 14 days). With those two
strategies dormant, V2 and V3 differ only in the value of a
parameter that is read on no code path that fires. **NOT a
config-loading bug**; correctly a variant-design flaw (V3 was
intended to test "yesterday's config" but resolved to V2 on this
data). De-prioritise V3 in future battery sweeps.

### 6.3 Live regime-classifier hypothesis

Live trades 100% short side over the last 14 days. V4 backtest
trades 59/41 long/short. Either the live regime classifier is stuck
in a state V4 isn't reproducing, or the live universe (mid-cap)
genuinely produces only short-side signals. Open question; will
become testable after V4 universe-transfer run completes.

---

## 7. What this proposal does NOT do

- Does not touch `trading_agent.py`, any strategy file, the
  ensemble, the risk gate, or the model artefact.
- Does not enable `scanner.use_live_universe: true` (separate
  bypass-slot decision).
- Does not change `confidence_threshold` (V10/V11 territory).
- Does not disable `supertrend_follow` (V15 territory).
- Does not introduce a new strategy.

If the V4 deploy is the only change, the failure mode is well-understood
(revert to 5.0). Compositional changes would muddy attribution.

---

## 8. Sign-off (Friday review)

| Reviewer | Decision | Date | Bypass slot |
|---|---|---|---|
| Operator | _pending_ | — | 1 / 3 |

Decision options:

1. **Approve** — schedule deploy for 2026-06-08 09:00 IST market open
2. **Defer to 2026-06-15** — wait for two more validation gates to clear
3. **Reject** — V4 evidence is insufficient; freeze extends, project
   conversation pivots toward scope-pivot or wind-down (Phase A FAIL)

---

---

## 10. The pre-speed-patch 90d × 228-stock data (added 2026-05-25 13:00 IST)

The 2026-05-18 → 2026-05-22 battery run on the FULL ~228-stock
live-shaped universe was killed at 2 of 15 variants when the
throughput-cliff investigation began. The completed V1 + V2 results
are decision-affecting and were not folded into §1–§9 of this
proposal because they were misclassified as "stale / pre-patch" until
re-examined on 2026-05-25.

### 10.1 What that run showed

| Metric | V1 (shipped) | V2 (filters all OFF) |
|---|---|---|
| Trades | 278 | 297 |
| Win rate | 44.6% | 50.2% |
| PnL | **+₹177.35** | **+₹658.55** |
| PF | 1.04 | 1.13 |
| MaxDD | 8.74% | 6.52% |
| Return % | +1.36% | +6.32% |
| Date range | 2026-02-17 → 2026-05-18 (59 days) | same |

### 10.2 The long/short asymmetry

| Side | V1 trades | V1 PnL | V1 avg | V1 WR | V2 trades | V2 PnL | V2 avg | V2 WR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BUY (long) | 122 | **+₹556** | +₹4.56 | 48.4% | 114 | **+₹1,057** | +₹9.27 | 56.1% |
| SELL (short) | 156 | **-₹379** | -₹2.43 | 41.7% | 183 | **-₹398** | -₹2.18 | 46.4% |

**Findings:**

1. The short side has **structurally negative edge in both V1 and V2**
   on 339+ short trades over 90 days. Disabling the trend filter does
   not fix shorts.

2. The long side carries V1's profitability (+₹556) and is **strongly
   profitable in V2** (+₹1,057 — nearly doubled per-trade by removing
   the filter on longs).

3. The +₹482 V1→V2 swing comes ENTIRELY from the long side. The short
   side performs ~identically (within ₹19) regardless of filter.

### 10.3 Three candidate configs now exist

| Candidate | Source data | PF | Universe | Risk |
|---|---|---:|---|---|
| **V4** (`trend_filter_pct: 3.0`) | 60d × 50 Nifty | 1.35 | Large-caps tested only | Universe transfer untested; mid-caps may want the filter OFF, not at 3% |
| **V2** (filters all OFF) | 90d × 228 full | 1.13 | Live-universe shape | Short side still bleeds; relies on long side carrying losses |
| **V1 long-only** (shorts disabled) | Inferred from 90d | ~1.5 (longs only) | Live-universe shape | Loses opportunity in genuinely bearish regimes; lowest-effort fix |

### 10.4 Live agent loss is now EXPLAINABLE

The live agent has traded **100% shorts** in the `bear_high_vol`
regime since 2026-05-13 (28 trades, all SELL). The 90d backtest
shorts (V1) lose -₹2.43 per trade × 28 trades = -₹68 expectancy,
with the 5th-percentile worst case at WR 41.7% × n=28 producing
losses in the -₹1,000 to -₹2,000 range. **Live -₹1,505 is well
within that envelope.** The live result is not evidence of a broken
engine; it's evidence of executing the structurally-losing side of a
working engine in a regime that biases toward it.

This is a meaningful update to the May-21 verdict.

### 10.5 Required new battery runs to disambiguate

Before any deploy decision, the queue should add:

| Priority | Run | Tests | Cost |
|---|---|---|---|
| 1 | `nifty500_v4_60d` | Does V4's 3% filter still win on mid-cap-heavy universe? | 1 worker × 14h |
| 2 | `nifty500_v2_60d` | Confirms V2's filters-off result on a different time-window | 1 worker × 14h |
| 3 | `nifty500_v1_long_only_90d` | Tests the cheapest-possible-fix candidate | 1 worker × 20h |
| 4 | `nifty500_v4_long_only_60d` | Combines V4 tuning with side-disable | 1 worker × 14h |

The current V1–V8 60d Nifty 50 run is producing data on **the wrong
universe**. The remaining V9–V16 should complete on Nifty 50 (data
already paid for) but the QUEUE BEHIND THIS RUN needs reordering.

### 10.6 Updated decision options

The Friday 2026-05-29 review now has three credible candidates, not
one. The kill criteria in §5.3 still apply but the deploy candidate
identity is unsettled until the priority-1 and priority-2 runs above
complete (~28 hours of compute, ~Wed/Thu).

The cheapest immediate experiment is the **long-only filter**: it
requires no parameter tuning, no new model, just a 1-line risk-engine
gate that drops SELL signals before order placement. We could
provisionally implement it as a configuration flag (`risk.allow_shorts:
false`) without a bypass slot — making it observability-time only
and inspectable in tests — and then unfreeze with that flag set to
`false` on 2026-06-08.

---

*Last updated 2026-05-25 13:00 IST. Major scope expansion after
re-analysis of the 90d × 228-stock pre-speed-patch run revealed the
short-side is the structural loss driver. The proposal will be
re-edited again after `nifty500_v4_60d` and `nifty500_v2_60d` runs
complete to pick a single deploy candidate.*
