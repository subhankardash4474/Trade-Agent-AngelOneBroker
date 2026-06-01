# V27 Mode A retune sweep — V28 / V29 / V30 / V31 results

> Filed 2026-06-01 ~16:00 IST. Phase 9 of the v4 Mode A scaffolding. This document is the comparison table + verdict for the V28-V31 retune burn.

## TL;DR

**V30 (max_concurrent=8) is the sole positive retune** — better than V27 first-cut on CAGR, PF, and Max DD simultaneously. **V28 (longer entry window) and V29 (tighter trail) are both net-negative.** **V31 (all-three combined) regresses** because V28+V29's negatives wash out V30's positive.

| Variant | CAGR | PF | Max DD | Trades | Charter §3.10 gate? |
|---|---:|---:|---:|---:|---|
| NIFTYBEES (benchmark) | +8.98% | — | -15.22% | 1 | — |
| **V27 first-cut** | +1.25% | 1.10 | -10.24% | 314 | A2/A3 (fails PF + CAGR-gap) |
| V27-no-benchmark | +1.02% | 1.08 | -10.26% | 312 | A2/A3 (worse) |
| **V28** (entry_n=100) | +0.13% | 1.01 | -12.07% | 294 | A2/A3 (worse) |
| **V29** (chandelier=2.5) | **-1.46%** | **0.88** | -11.33% | 371 | **A3 (NET LOSS)** |
| **V30** (max_concurrent=8) | **+1.87%** | **1.19** | **-8.55%** | 239 | A2 (PF -0.01 short; CAGR-gap -7.11pp) |
| V31 (all three combined) | -0.32% | 0.96 | -11.07% | 265 | A3 (NET LOSS) |

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

## Charter §3.10 verdict against the V30 candidate

Per the swing_cash_v27 kill_criteria backtest gates (charter §3.10 / Phase 7 manifest):

| Charter gate | V30 actual | Verdict |
|---|---:|---|
| pf_min ≥ 1.20 | 1.19 | **MISS by 0.01** |
| cagr_vs_niftybees ≥ +2.0pp | -7.11pp | **FAIL by 9.11pp** |
| maxdd_max_pct ≤ 25.0% | 8.55% | PASS |

V30 is close on PF but **fails the CAGR-vs-benchmark gate badly**. The CAGR gate is the binding constraint, not the PF gate.

## Implications

1. **The Donchian-55/20 + vol-target + risk-parity Mode A spec is at its ceiling on the 2022-2026 window**, with this 75-instrument universe, at AngelOne CNC charges, on ₹100k capital. The four variants we tested cover the main parameter axes (entry, exit, concentration), and only one (concentration) helps.

2. **The "edge" V30 finds may be concentration-into-benchmark in disguise.** To confirm, run a per-symbol P&L attribution on V30's trades.csv and check what fraction of profits comes from NIFTYBEES/JUNIORBEES/BANKBEES vs individual stocks.

3. **The 7.11pp CAGR gap is not closeable with these parameter axes.** Closing it requires either:
   - A different hypothesis (cross-asset trend on weekly bars; sector rotation; momentum-breadth combo)
   - A different cost regime (no broker can give us cheaper than AngelOne CNC discount)
   - A different capital scale (the per-trade fixed costs dominate at ₹100k)

## Recommended next steps

Pick one:

**(a) Concentration sweep.** Burn ~30 min more compute on V32 (max_c=6) + V33 (max_c=4) to find the local CAGR peak around max_concurrent. If V32 or V33 cross PF 1.20, we have a viable Mode A candidate (still fails CAGR-vs-benchmark, but PF passes).

**(b) Per-symbol attribution.** Spend ~15 min reading V30's trades.csv to confirm/refute the "benchmark in disguise" hypothesis. If confirmed, Mode A is essentially closet-indexing and the operator should know this BEFORE deploying.

**(c) Concede A3 on Mode A.** All four retunes tested; only concentration helps and only marginally. Mark cross-asset-trend on Indian equity at AngelOne CNC + ₹100k as A3, pivot v4 to a different hypothesis. Cleanest path forward.

**(d) Park until Friday verdict.** Mode A's fate may be moot if the v2.1 wind-down verdict goes a particular way. The four data points (V27 + V27-no-bm + V28-V31) are sufficient for the verdict-meeting summary. Resume Monday.

## Artefacts

| Result dir | Variant |
|---|---|
| `logs/backtests/v27_firstcut_2026_06_01/` | V27 baseline (entry_n=55, chand=3.0, maxc=12) |
| `logs/backtests/v27_no_benchmark_2026_06_01/` | V27 with NIFTYBEES + JUNIORBEES + BANKBEES + NIFTYIETF excluded from signal candidates |
| `logs/backtests/v27_v28_entry100_2026_06_01/` | V28: entry_n=100 |
| `logs/backtests/v27_v29_chand25_2026_06_01/` | V29: chandelier_mult=2.5 |
| `logs/backtests/v27_v30_maxc8_2026_06_01/` | **V30: max_concurrent=8 (best so far)** |
| `logs/backtests/v27_v31_combined_2026_06_01/` | V31: entry_n=100 + chand=2.5 + maxc=8 |

Each dir contains `comparison.md`, `results.json`, `manifest.json`, `equity_curve.csv`, `trades.csv`.

CLI flag additions live in `tools/v27_backtest_2026_06_01.py` (`--entry-n`, `--exit-m`, `--chandelier-mult`, `--max-concurrent`). Reproducible.

## Commits

| Commit | Phase | Description |
|---|---|---|
| `7d693cd` | 8.A | V27-no-benchmark sensitivity (self-cannibalization REFUTED) |
| `d572332` | 8.B | BacktestConfig.sizer extension (engine sizer plug-point) |
| `b381012` | 8.C | ModeDispatcher skeleton (charter §2) |
| `c7afba2` | 8.* | Phase 8 writeup |
| (next) | 9 | V28-V31 retune burn + this findings doc |
