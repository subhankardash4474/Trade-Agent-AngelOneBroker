# V27 Mode A retune sweep — V28 / V29 / V30 / V31 / V32 / V33 + attribution

> Filed 2026-06-01 ~16:00 IST, **EXTENDED ~17:30 IST with Phase 10 concentration sweep + V32 attribution**. This document is the comparison table + verdict for the V28-V33 retune burn.

## TL;DR (UPDATED POST-PHASE-11)

**V32 (max_concurrent=6) remains the best Mode A variant after charter §3.6 sector cap was tested in Phase 11.** V32 PASSES the charter §3.10 PF gate (1.36 > 1.20) and Max DD gate (-7.80% < 25.0%). FAILS the CAGR-vs-benchmark gate (-6.14pp vs +2.0pp required). Per-symbol attribution shows V32 has **genuine individual-stock-picking edge (68% of P&L)** — NOT closet-indexing.

**V34 = V32 + charter §3.6 sector cap (max 3 per sector) is WORSE than V32** — sector cap forced sub-optimal trades inside the energy bucket (COALINDIA flipped from +5% contributor in V32 to **-19% loser** in V34). The sector cap reduced CAGR more than concentration risk.

| Variant | CAGR | PF | Max DD | Trades | Charter §3.10 gates |
|---|---:|---:|---:|---:|---|
| NIFTYBEES (benchmark) | +8.98% | — | -15.22% | 1 | — |
| **V27 first-cut** | +1.25% | 1.10 | -10.24% | 314 | A2/A3 (fails PF + CAGR-gap) |
| V27-no-benchmark | +1.02% | 1.08 | -10.26% | 312 | A2/A3 (worse) |
| **V28** (entry_n=100) | +0.13% | 1.01 | -12.07% | 294 | A2/A3 (worse) |
| **V29** (chandelier=2.5) | **-1.46%** | **0.88** | -11.33% | 371 | **A3 (NET LOSS)** |
| **V30** (max_concurrent=8) | +1.87% | 1.19 | -8.55% | 239 | A2 (PF -0.01 short) |
| V31 (all three combined) | -0.32% | 0.96 | -11.07% | 265 | A3 (NET LOSS) |
| **V32** (max_concurrent=6) | **+2.84%** | **1.36** | **-7.80%** | 180 | **PF + DD PASS, CAGR-gap -6.14pp FAIL** |
| V33 (max_concurrent=4) | +2.32% | 1.36 | -7.84% | 130 | PF + DD PASS, CAGR-gap -6.66pp FAIL |
| **V34** (max_c=6 + sector_cap=3) | +1.93% | 1.24 | -7.80% | 183 | PF + DD PASS but WORSE than V32 |

Charter §3.10 gates for Mode A `swing_cash_v27`:

| Gate | Threshold | Best (V30) | Pass? |
|---|---:|---:|---|
| `pf_min` | 1.20 | 1.19 | **NEAR-MISS** (-0.01) |
| `cagr_vs_niftybees_min_pct` | +2.0pp | **-7.11pp** | **FAIL** |
| `maxdd_max_pct` | 25.0% | 8.55% | PASS (way under) |

## Per-variant diagnosis

### V28 — entry_n: 55 → 100 (longer breakout window)
**Hypothesis:** Longer breakout = fewer false signals = only the strongest trends fire.

**Result:** WORSE. CAGR dropped from +1.25% to +0.13%. PF dropped from 1.10 to 1.01 (basically random). Max DD worsened from -10.24% to -12.07%. Trade count dropped from 314 to 294 (-6%) — meaning the longer window did reduce signal frequency, but the SURVIVING trades did not have meaningfully better edge. Almost no signal got pruned that wouldn't have been pruned by V27's other filters (volume + ADX + regime).

**Diagnostic read:** Longer breakout windows on daily bars don't add information beyond what's already in the 55-bar baseline + the regime/volume/ADX gates. V27's filter stack is doing the work; the breakout window itself isn't the bottleneck.

### V29 — chandelier_mult: 3.0 → 2.5 (tighter trailing stop)
**Hypothesis:** Tighter trail = faster loss-cutting = better win/loss asymmetry.

**Result:** MUCH WORSE. **CAGR went NET NEGATIVE: -1.46%.** PF 0.88 (the strategy now LOSES money before charges). Trade count rose from 314 to 371 (+18%) confirming the whipsaw concern — tighter trail gets hit more often by normal volatility, then the strategy re-enters on the next breakout, then gets stopped out again. Total charges rose from ~₹14.5k to ~₹17.1k.

**Diagnostic read:** On Indian equity at this volatility level, 2.5x ATR trail is INSIDE the normal noise range. The 3.0x V27 default is at or near the optimal trade-off between giving trends room to breathe and cutting losses. Tightening it is destructive.

### V30 — max_concurrent: 12 → 8 (fewer slots, more concentration)
**Hypothesis:** Fewer concurrent positions → more capital per trade → bigger wins on the trades that work.

**Result:** **THE FIRST POSITIVE RETUNE.** All three primary metrics improve:
- CAGR: +1.25% → **+1.87%** (+0.62pp)
- PF: 1.10 → **1.19** (+0.09; one cent below the charter PF gate)
- Max DD: -10.24% → **-8.55%** (1.69pp improvement)

Per-trade P&L diagnostic:
- V27 avg trade P&L: (53624 − 49660) / 314 = **₹12.6 / trade**
- V30 avg trade P&L: (48405 − 40790) / 239 = **₹31.9 / trade** (~2.5× better)

**Diagnostic read:** When the risk-parity allocator has fewer slots, it concentrates into the highest-conviction (lowest-vol-weighted) signals. On 2022-2026 Indian equity, the lowest-vol signals are the broad ETFs (NIFTYBEES, JUNIORBEES, BANKBEES) — which by construction track the market. **V30's edge may largely come from being a worse-disguised version of NIFTYBEES buy-and-hold.** Worth confirming with a per-symbol P&L attribution.

### V31 — all three combined (entry_n=100 + chand=2.5 + maxc=8)
**Hypothesis:** Stack the retunes.

**Result:** REGRESSES. CAGR -0.32%, PF 0.96 (losing). V28's and V29's negative contributions wash out V30's positive. **Additive retune hypothesis fails.**

### V32 — max_concurrent: 8 → 6 (more concentration; Phase 10 sweep)
**Hypothesis:** If concentration is the only positive axis (V30), push further.

**Result:** **NEW BEST VARIANT.** Crosses both PF and DD charter gates.
- CAGR +1.87% (V30) → **+2.84%** (+0.97pp vs V30; +1.59pp vs V27)
- PF 1.19 (V30) → **1.36** (+0.17 vs V30; **PASSES charter §3.10 gate of 1.20**)
- Max DD -8.55% (V30) → **-7.80%** (best yet)
- Per-trade P&L: V27 ₹12.6 → V30 ₹31.9 → **V32 ₹66.4** (5.3× V27)
- Trades: 180 (vs V27 314, V30 239)

### V33 — max_concurrent: 6 → 4 (most concentrated)
**Hypothesis:** Continue the concentration sweep — does it peak at 6 or go further?

**Result:** **SLIGHT REGRESSION from V32. Local CAGR peak is V32 (max_c=6).**
- CAGR +2.84% (V32) → **+2.32%** (regression of 0.52pp)
- PF 1.36 (V32) → 1.36 (identical; PASSES charter gate)
- Per-trade P&L: V32 ₹66.4 → **V33 ₹74.7** (better per trade)
- Trades: 180 → **130** (-28%; even fewer signals fit)

**Diagnostic read:** Concentration helps up to max_c=6; below that, the strategy is trade-starved (fewer signals get to fire when slots stay occupied). V32 is the local optimum.

## Concentration sweep curve

| max_concurrent | CAGR | PF | Per-trade P&L |
|---:|---:|---:|---:|
| 12 (V27 baseline) | +1.25% | 1.10 | ₹12.6 |
| 8 (V30) | +1.87% | 1.19 | ₹31.9 |
| **6 (V32)** | **+2.84%** | **1.36** | ₹66.4 |
| 4 (V33) | +2.32% | 1.36 | ₹74.7 |

Per-trade economics keep improving monotonically as concentration tightens; CAGR peaks at 6 because trade frequency drops faster than per-trade P&L grows below that point.

## V32 per-symbol attribution (the "closet-indexing" question)

The critical follow-up: **is V32's edge real, or just a different way to closet-index NIFTYBEES?** If risk-parity loads heavily into broad ETFs and they dominate the P&L, V32 is essentially NIFTYBEES buy-and-hold in disguise.

**Result (180 trades, ₹11,955 total net P&L):**

| Bucket | Symbols | Trades | Net P&L | % of total |
|---|---:|---:|---:|---:|
| **Individual stocks** | 57 | 159 | **₹8,145** | **68.1%** |
| Commodity ETFs (SILVERBEES, GOLDBEES) | 2 | 10 | ₹3,761 | 31.5% |
| Broad ETFs (NIFTYBEES + JUNIORBEES + BANKBEES + NIFTYIETF) | 2 | 5 | ₹424 | **3.5%** |
| Sector ETFs | 2 | 6 | -₹374 | -3.1% |

**Top contributors:** IOC (₹5,183, 43%), SILVERBEES (₹3,289, 28%), ADANIGREEN (₹2,558, 21%), M&M (11%), HAVELLS (11%), HDFCLIFE (9%).

**Top losers:** TATASTEEL (-12%), ADANIENT (-11%), JSWSTEEL (-9%), ADANIPORTS (-8%), PIDILITIND (-7%).

**Closet-indexing hypothesis: REFUTED.** Broad ETFs contribute only **3.5% of net P&L**. V32 has genuine individual-stock-picking edge — IOC alone outweighs all 4 broad ETFs combined by 12×. The strategy actually picks names; it doesn't passively ride beta.

**Observation:** Top winners + top losers both cluster in commodity/energy/Adani-group exposures. Concentration risk is real: a few names dominate both sides of the P&L distribution. A future variant could test sector caps to reduce this concentration noise.

## Charter §3.10 verdict against the V32 candidate (UPDATED — V32 supersedes V30)

Per the swing_cash_v27 kill_criteria backtest gates (charter §3.10 / Phase 7 manifest):

| Charter gate | V32 actual | Verdict |
|---|---:|---|
| `pf_min ≥ 1.20` | **1.36** | **PASS** (+0.16) |
| `cagr_vs_niftybees_min_pct ≥ +2.0pp` | **-6.14pp** | **FAIL** (by 8.14pp) |
| `maxdd_max_pct ≤ 25.0%` | 7.80% | PASS (way under) |

**V32 passes 2 of 3 charter gates.** PF gate clears with comfortable margin. Max DD gate clears with massive margin (V32 is 1/3 of the threshold). **The CAGR-vs-benchmark gate is still binding and still fails.**

**Risk-adjusted comparison** (CAGR ÷ |Max DD| as crude Calmar proxy):

| Strategy | CAGR | Max DD | Calmar-like ratio |
|---|---:|---:|---:|
| NIFTYBEES buy-and-hold | +8.98% | -15.22% | **0.59** |
| V32 Mode A | +2.84% | -7.80% | **0.36** |

V32's risk-adjusted return is still lower than NIFTYBEES, but the gap narrows significantly (0.36 vs 0.59 ratio, vs raw CAGR ratio of 0.32). NIFTYBEES has nearly 2× the drawdown of V32.

## Implications (UPDATED)

1. **The Donchian-55/20 + vol-target + risk-parity Mode A spec has a real but small edge** when configured with `max_concurrent=6`. V32 produces PF 1.36 + Max DD -7.80% on genuine individual-stock picks (68% of P&L). It is NOT closet-indexing.

2. **The strategy still underperforms NIFTYBEES on raw CAGR** by 6.14pp. The 2022-2026 window was a strong-beta market and NIFTYBEES was a generous baseline. V32's risk-adjusted profile (lower drawdown) closes some of the gap but not all.

3. **The two binding constraints for Mode A passing the charter:**
   - The +2.0pp CAGR-vs-benchmark gate is the hard ceiling. Cannot be passed without either a richer signal (something orthogonal to NIFTYBEES beta) OR a cheaper cost regime OR a longer time window that includes a non-bull-market regime.
   - Concentration risk: V32's P&L is dominated by ~10 names. A bad turn in 2-3 of them (commodity bust, Adani-group event) would meaningfully swing performance.

4. **The concentration sweep peaked at max_c=6.** Below that (V33 max_c=4), per-trade economics keep improving but total trades drop faster, so CAGR regresses. Above that (V30 max_c=8), per-trade economics weaken faster than trade frequency rises.

5. **Charter §3.10 currently REQUIRES the CAGR-vs-benchmark gate to pass.** If the operator wants to deploy V32 anyway, that requires either:
   - A formal charter amendment loosening the CAGR-vs-benchmark threshold (e.g. accept underperformance if Max DD and PF pass — a CHANGE in policy)
   - Acceptance that V32 is a "diversification" play that runs alongside an explicit NIFTYBEES core allocation (no charter change needed; reframes the strategy's purpose)

## Recommended next steps (UPDATED post-V32 attribution)

Pick one:

**(a) Accept V32 as best Mode A candidate; document gates carefully.** V32 passes 2 of 3 charter gates and is NOT closet-indexing. Cost: ~15 min to write a "V32 candidate accepted" addendum. Then either propose a charter amendment to the CAGR gate, or accept the gate as binding and deploy V32 only as a diversifier alongside a NIFTYBEES core.

**(b) Explore signal-richness improvements.** V32's edge is concentration around the EXISTING signal stack. A richer signal (volume-weighted breakout? cross-sectional momentum rank? sector-rotation filter?) could potentially close more of the 6pp CAGR gap. ~1-2 days dev per new signal.

**(c) Test sector-cap (charter §3.6 says 3 per sector but V27 standalone does NOT enforce it).** V32's top-10 contributors cluster in commodity/energy/Adani; a sector cap would reduce concentration risk + may improve CAGR by forcing diversification. ~45 min dev + 1 backtest.

**(d) Concede A3 on Mode A.** Charter §3.10 CAGR-gate is binding and V32 still fails by 6.14pp. All major parameter axes tested. Mark Mode A as A3, pivot v4 to a different hypothesis. Clean cutover.

**(e) Park until Friday verdict.** 8 Mode A data points in the verdict-meeting packet (V27 + V27-no-bm + V28 through V33 + attribution). Plenty to inform the decision. Resume Monday.

## Phase 11 — V34 sector cap test (charter §3.6)

**Hypothesis:** V32's top-10 contributors clustered in commodity / energy / Adani-group exposures (per Phase 10 attribution). Enforcing the charter §3.6 "max 3 per sector" rule should reduce concentration risk; if individual stock selection is genuinely good, V34's CAGR shouldn't drop much.

**Implementation:** Built `packages/core/instruments/sector_classifier.py` with sector assignments for all 75 universe instruments (Adani family in its own bucket because V32 concentration risk). Added `--sector-cap` flag to standalone backtester. Cap enforced at entry-execution time (after risk-parity allocation, before order placement).

**Result (V34 = V32 params + sector_cap=3):**

| Metric | V32 | V34 | Δ |
|---|---:|---:|---:|
| CAGR | +2.84% | **+1.93%** | **-0.91pp** |
| PF | 1.36 | 1.24 | -0.12 (still ≥ 1.20 gate) |
| Max DD | -7.80% | -7.80% | flat |
| Trades | 180 | 183 | +3 |
| Total net P&L | ₹11,955 | ₹7,926 | **-34%** |

**V34 attribution (vs V32 attribution):**

| Bucket | V32 P&L | V32 % | V34 P&L | V34 % |
|---|---:|---:|---:|---:|
| Individual stocks | ₹8,145 | 68% | ₹4,315 | 54% |
| Commodity ETFs | ₹3,761 | 31% | ₹3,605 | 45% |
| Broad ETFs | ₹424 | 4% | ₹364 | 5% |
| Sector ETFs | -₹374 | -3% | -₹357 | -4% |

**Key observations:**
1. Individual stock P&L DROPPED by 47% (₹8,145 → ₹4,315) — the sector cap prevented the best-performing individual-stock sectors (energy, mainly IOC) from loading further.
2. **COALINDIA flipped from +5% contributor in V32 to -19% LOSER in V34** — sector cap forced the strategy to hold COALINDIA when it would otherwise have rotated to a better energy signal.
3. Commodity ETFs (SILVERBEES, GOLDBEES) were unaffected because they're in separate buckets.

**Verdict on the sector cap:** The cap reduced concentration risk but at the cost of CAGR. V32's edge legitimately comes partly from concentration into the best-firing sectors at the right times. The sector cap is a "safety vs. return" trade-off — V34 is acceptable if the operator weighs sector-blow-up risk highly; otherwise V32 is the better Mode A candidate.

**Implication for Phase 1 paper-mode decision:** The charter §3.6 sector cap rule (as currently written) materially harms V32. Either:
  (a) Modify charter §3.6 to allow max 4-5 per sector (V34-style cap)
  (b) Modify charter §3.6 to make the cap a soft warning (count + log, don't enforce)
  (c) Keep charter §3.6 as-is (max 3) and accept the V34 -0.91pp CAGR cost
  (d) Drop the sector-cap requirement entirely (operator policy choice)

## Artefacts

| Result dir | Variant |
|---|---|
| `logs/backtests/v27_firstcut_2026_06_01/` | V27 baseline (entry_n=55, chand=3.0, maxc=12) |
| `logs/backtests/v27_no_benchmark_2026_06_01/` | V27 with NIFTYBEES + JUNIORBEES + BANKBEES + NIFTYIETF excluded from signal candidates |
| `logs/backtests/v27_v28_entry100_2026_06_01/` | V28: entry_n=100 |
| `logs/backtests/v27_v29_chand25_2026_06_01/` | V29: chandelier_mult=2.5 |
| `logs/backtests/v27_v30_maxc8_2026_06_01/` | V30: max_concurrent=8 |
| `logs/backtests/v27_v31_combined_2026_06_01/` | V31: entry_n=100 + chand=2.5 + maxc=8 |
| `logs/backtests/v27_v32_maxc6_2026_06_01/` | **V32: max_concurrent=6 (BEST)** |
| `logs/backtests/v27_v33_maxc4_2026_06_01/` | V33: max_concurrent=4 |
| `logs/backtests/v27_v34_maxc6_sec3_2026_06_01/` | V34: max_c=6 + sector_cap=3 |
| `tools/_v32_attribution_2026_06_01.py` | Per-symbol P&L attribution tool |
| `logs/v32_attribution_2026-06-01.log` | V32 attribution output (closet-indexing REFUTED) |
| `logs/v34_attribution_2026-06-01.log` | V34 attribution output (sector cap reduces individual-stock P&L) |
| `packages/core/instruments/sector_classifier.py` | Sector assignments for all 75 V4 universe instruments |
| `tests/unit/test_sector_classifier_2026_06_01.py` | 9 pin tests for the sector classifier |

Each result dir contains `comparison.md`, `results.json`, `manifest.json`, `equity_curve.csv`, `trades.csv`.

CLI flag additions live in `tools/v27_backtest_2026_06_01.py` (`--entry-n`, `--exit-m`, `--chandelier-mult`, `--max-concurrent`). Reproducible.

## Commits

| Commit | Phase | Description |
|---|---|---|
| `7d693cd` | 8.A | V27-no-benchmark sensitivity (self-cannibalization REFUTED) |
| `d572332` | 8.B | BacktestConfig.sizer extension (engine sizer plug-point) |
| `b381012` | 8.C | ModeDispatcher skeleton (charter §2) |
| `c7afba2` | 8.* | Phase 8 writeup |
| `ae00241` | 9 | V28-V31 retune burn + initial findings doc |
| (next) | 10 | V32-V33 concentration sweep + attribution + updated findings |
