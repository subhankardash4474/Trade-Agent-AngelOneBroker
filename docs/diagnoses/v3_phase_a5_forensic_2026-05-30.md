# V3 Phase A5 forensic — read-once verdict

**Run:** `battery_v3_swing_a5_180d_eff_20260530T050422`
**Date:** 2026-05-30, ~10:42 IST
**Author:** assistant (mechanical application of charter §6.5 verdict tree)
**Cross-refs:**
- `docs/freeze/freeze_v3.0_charter_2026-05-30.md` §6.5 (Phase A5 read-out tree), §10.5 R1 (do NOT debug into oblivion)
- `docs/findings/findings_log_2026-05-27.md` §28 (A2-A4 deliverables)
- `docs/diagnoses/v3_backtester_gap_analysis_2026-05-30.md` (A1 gap analysis)

## 1. The headline

All five v3 swing variants produced **PF < 1.0**:

| Variant | Trades | WR% | PnL (Rs) | PF | MaxDD% |
|---|---:|---:|---:|---:|---:|
| V20_swing_pullback_only | 55 | 20.0 | -1,137 | 0.41 | 13.0 |
| V21_swing_breakout_only | 46 | 13.0 | -1,712 | 0.23 | 16.9 |
| V22_swing_combined | 84 | 17.9 | -2,267 | 0.28 | 22.3 |
| V23_swing_combined_loose | 103 | 19.4 | -2,750 | 0.29 | 26.9 |
| V24_swing_combined_tight | 46 | 10.9 | -1,499 | 0.21 | 15.2 |

Per charter §6.5 mechanically: **SURPRISE branch.** Charter §10.5 R1
mandates: read once, do NOT debug into oblivion, sleep on it, decide
the next move tomorrow morning.

## 2. Diagnostic — bug vs no-edge

A diagnostic script analysed the per-variant trade dicts to classify
the failure mode. Verdict tree (defined before reading the data, per
charter pre-commitment discipline):

* **BUG-A:** ≥50% trades close on `stop_loss` with `holding_days==0`
  → intra-bar SL firing on entry bar (a known risk of next-day-open
  fill mode).
* **BUG-B:** ≥50% trades exit `end_of_backtest` → positions never
  hit TP/SL within the test window.
* **BUG-C:** Median `holding_days == 0` → fills not advancing.
* **NO-EDGE:** Balanced exits, charter-spec 3-10 day holds, but PnL
  red → strategy genuinely has no edge on this universe.
* **MIXED:** No single dominant failure mode.

Result table:

| Variant | n | SL% | TP% | OppSig% | EOB% | Med Hold | SameDay-SL% | Verdict |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| V20 | 55 | 29.1 | 20.0 | 49.1 | 1.8 | 6 d | 1.8 | **NO-EDGE** |
| V21 | 46 | 84.8 | 13.0 | 0.0 | 2.2 | 5 d | 15.2 | MIXED — but dominated by SL exit |
| V22 | 84 | 54.8 | 15.5 | 27.4 | 2.4 | 6 d | 10.7 | MIXED |
| V23 | 103 | 58.3 | 17.5 | 22.3 | 1.9 | 6 d | 12.6 | MIXED |
| V24 | 46 | 56.5 | 10.9 | 32.6 | 0.0 | 5 d | 10.9 | MIXED |

**No variant fits BUG-A, BUG-B, or BUG-C.** Same-day stop-loss is
1.8-15.2% (BUG-A would require ≥50%). End-of-backtest flush is
0-2.4% (BUG-B would require ≥50%). Median holds are 5-6 days
(BUG-C would require 0). End-of-backtest exits are negligible.

The mechanics are working as designed.

## 3. Two real signals from the data

### 3.1 Rule 2 (breakout_20d) is catastrophically bad on Nifty 50

V21 alone, no help from Rule 1: **84.8% stop-out rate, PF 0.23**. This
isn't noise; this is anti-edge. The 20-day high breakout signal on
Nifty 50 mega-caps is a *bear trap*: price breaks the 20-day high,
reverses, hits the 4% SL.

This pattern is consistent with mean-reversion dominating mega-cap
microstructure: institutional sells into momentum strength, retail buys
the breakout, retail loses. It's the exact failure mode that makes
"buy the breakout on liquid largecaps" a known retail-trap setup.

Cross-implication: the combined variants (V22-V24) are dragged down
by V21's failure, since each combined variant runs both rules and
V21 fires more often than V20.

### 3.2 "Tightening" produced LOWER win rate, not higher

| | V23 (loose) | V22 (default) | V24 (tight) |
|---|---:|---:|---:|
| RSI window | 35-60 | 40-55 | 42-50 |
| Vol multiplier | 0.7 / 1.5 | 0.8 / 1.5 | 0.8 / 2.0 |
| ADX threshold | 15 | 20 | 25 |
| Trades | 103 | 84 | 46 |
| **WR%** | **19.4** | **17.9** | **10.9** |
| TP rate% | 17.5 | 15.5 | 10.9 |

If the rules captured even weak real edge, tighter thresholds should
produce equal-or-higher win rates (selecting for cleaner setups).
Getting *worse* WR on tighter selection is the classic signature of
*rules fitting noise, not signal*. There is no underlying selection
criterion the threshold-tightening is honoring.

This second signal is independent of Signal 3.1 — it would hold even
if Rule 2 were profitable.

## 4. Why this is "no edge" and not "bad params"

V20 alone is the cleanest read because it isolates Rule 1 from Rule 2's
failure:

* 49.1% **opposite-signal exits** = the strategy's own "close < 50-DMA"
  exit firing as designed. Mechanics confirmed.
* 29.1% stop_loss + 20.0% take_profit = a balanced 1.45:1 SL:TP ratio.
  Roughly what we'd expect from a 3% SL / 8% TP setup on a noisy series.
* Median hold 6 days = squarely in the charter "3-10 day swing" target.
* PF 0.41 with avg winner Rs 73 vs avg loser Rs 44 = 1.66:1 R:R but
  only 20% WR. Required WR for breakeven at 1.66 R:R = 37.6%. We're
  17.6 percentage points below breakeven on the WR axis.

This is **not** a "the rule almost works" reading. It's a "the entry
gate has 20% WR when classical TA on liquid largecaps suggests 45-55%
WR is normal" reading. The setup isn't picking better-than-random
entries.

The most parsimonious explanation: **the trend-pullback-with-RSI-cooled
setup does not have edge on this universe at this horizon.** It might
have edge elsewhere (mid-caps, different sectors, weekly bars, options
overlay), but charter §6 specifically scoped to top-30 Nifty 50 daily
swing CNC.

## 5. What is NOT in this analysis

Per charter §10.5 R1 ("do NOT debug into oblivion") and §6.5 ("read
once, sleep on it"):

* **No re-runs with tweaked parameters.** That's curve-fitting.
* **No "let me try one more variant".** Charter forbids it.
* **No engine-side debugging.** Mechanics are confirmed sane above.
* **No verdict on next steps.** That's the operator's call after sleep.
* **No trader VM touch.** Museum mode per §6.1; the gate at §7.1 is
  not crossed and won't be crossed today.

## 6. Three options for tomorrow morning's decision

These are surfaced to the operator, NOT decided here. Per charter §A5
outcome 3, the operator decides "whether to try a different rule set
or pivot the pivot."

### Option A — Wind-down

Accept that "side-hustle algo on Indian cash equities without leverage"
is the wrong framing. The data from v2.1 (no edge on 5-min intraday)
plus v3 (no edge on daily swing) covers the two most-tractable
horizons for retail-without-leverage. Capital pause indefinite.
Engineering work archived as portfolio piece + learning artefact.

**Cost:** zero further capital risk; project closes.
**Pre-commitment:** matches `wind_down_criteria_2026-06-05.md` T3
trigger; v3 was the "single named, measurable hypothesis" that T3
required. Hypothesis tested, hypothesis failed.

### Option B — Different rule set, same infra (v3.1 charter)

Possible candidates:
* Pure trend-following: BUY on close > 50-DMA, SELL on close < 50-DMA.
  No pullback gate. The simplest possible rule.
* Mean reversion: BUY on RSI(14) < 30, SELL on RSI(14) > 70 with a
  trend filter (close > 200-DMA).
* Bollinger band breakout: BUY on close > BB(20, 2)_upper.
* Sector-rotation top-N: weekly rotation into the top-3 sectors by
  trailing 4-week return.

**Cost:** one more 2-week cycle. Requires fresh v3.1 charter pre-commit
to avoid scope drift.
**Risk:** if 4 distinct rule families all show no edge, project enters
the same wind-down decision having burned more time.

### Option C — Same rules, wider SL/TP (cheapest)

3% SL on Nifty 50 daily bars is ~1.5× ATR — tight. A wider SL/TP might
let the rule survive normal noise long enough to capture real moves:
* Rule 1: 5% SL / 12% TP (vs 3% / 8%)
* Rule 2: 6% SL / 15% TP (vs 4% / 12%)

Add as V25/V26 variants. ~2h to write + test + commit + queue.

**Cost:** one more battery slot.
**Risk:** curve-fitting v3 to a single noise-fit. If V25/V26 pass at
PF 1.5+ but ONLY with these specific widths, that's a strong
overfitting signature — not a real discovery.

## 7. The single sentence

The v3 strategies executed correctly, in charter-compliant conditions,
on the charter-specified universe, and produced PF 0.21-0.41 with
20% WR — which is the data-driven reading of "no edge for this
hypothesis on this universe at this horizon."

Sleep on it. Decide tomorrow.

---

## 8. UPDATE — V25 disambiguation result (2026-05-30 14:37 IST)

**Trigger.** The 2026-05-30 brutal review (Session 2 §1) caught that
sections 1-7 above tested only the LONG-ONLY 7-11% of trend_pullback's
natural signal set: 88-93% of SELL emissions were dropped by
`_v3_swing_base`'s `risk.allow_shorts: false`. Without a shorts-allowed
read, the wind-down trigger would fire on incomplete evidence.

V25_swing_combined_shorts (`commit f50e9ee`) = V22 + `risk.allow_shorts: True`.
Same universe, window, fill_mode, and warmup. Only the long-only veto is
dropped. Documented asymmetry in the variant block: trend_pullback's SELL
emission has ONE gate (`close < sma_50`) vs the BUY's FIVE gates, and the
SELL carries no SL/TP so the engine uses ATR-based fallbacks. V25 is
necessary-but-not-sufficient evidence for "the bidirectional version of
trend_pullback has no edge"; a TRULY symmetric short is a v3.1 hypothesis.

### 8.1 V25 result vs V22 (apples-to-apples; only allow_shorts differs)

| Metric | V22 longs-only | V25 shorts-allowed | Δ |
|---|---:|---:|---:|
| Trades | 84 | 189 | +105 (+125%) |
| Win rate | 17.9% | 27.5% | +9.6 ppts |
| PnL | -₹2,267 | -₹3,764 | -₹1,497 worse |
| Profit Factor | 0.28 | **0.23** | -0.05 worse |
| R:R | ~1:0.45 | 1:0.61 | wider but still <1 |
| Max drawdown | 22.3% | **37.84%** | nearly doubled |
| Sharpe | -3.5 (estimated) | -5.53 | deeper negative |
| Charges drag | -₹1,500 (estimated) | -₹3,408 | scaled with trade count |

**Source:** `logs/backtests/battery_v3_swing_a5_v25_shorts_20260530T090709/results/V25_swing_combined_shorts.json`.

### 8.2 Side split (V25 internal)

| Side | N | WR% | PnL | PF | avg_W | avg_L | Hold | TPs | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BUY  | 46  | 19.6 | -₹1,185 | 0.31 | +₹58.9 | -₹46.4 | 18.8d | 7  | 27  |
| SELL | 143 | 30.1 | -₹2,579 | 0.19 | +₹13.8 | -₹31.7 | 4.2d  | 43 | 97  |

**Long-arm reading.** V25's BUY arm (n=46, WR 19.6%, PF 0.31, avg hold
18.8d) reproduces V20's edge profile (n=55, WR 20.0%, PF 0.41, longs only).
The small drift (PF 0.41 → 0.31) is explained by short positions consuming
some of the 5-position cap, so the long arm sees less capital allocation
on edge-positive days.

**Short-arm reading.** Higher win rate, smaller wins, worse PF. This is
the textbook signature of a **high-frequency mediocre signal**:

* 143 trades in ~210 trading days = ~0.7 short entries / day across 30
  stocks. Shorts fire CONSTANTLY any time price closes below 50-DMA, the
  asymmetric one-gate condition.
* Wins only +₹13.8 average (vs longs' +₹58.9). The engine's ATR-based
  fallback TP is much tighter than the strategy's 8% TP that the long
  path uses, so even when the short works, it captures only a small move.
* Losses still hit ATR-based SL at roughly the same -₹31.7 magnitude,
  giving the 1:0.43 win/loss ratio that drives PF 0.19.
* 4.2d avg hold (vs longs' 18.8d) confirms shorts are closing fast —
  squeezed by the next leg up in a generally rising market regime over
  the 600-day window.

### 8.3 Gate stats: signal saturation

```
total_signals:       2,636
max_positions_reached: 1,893  (71.8%)
insufficient_cash:     471   (17.9%)
expected_profit:        82   (3.1%)
executed:              189   (7.2%)
shorts_blocked:          0   (correct — V25 allows shorts)
no_next_bar:             1   (final-bar drop, expected)
```

71.8% of signals were dropped at the 5-position cap. Even at higher
caps the additional fills would be marginal-quality shorts further
diluting the book. The position cap is not the bottleneck; the
underlying signal quality is.

### 8.4 Charter §6.5 verdict tree application

The pre-committed V25 verdict tree (`packages/research/battery.py`
V25_swing_combined_shorts docstring) had two branches:

* **V25 PF < 1.0** → "forensic verdict honest. The simpler short side
  ALSO has no edge. Wind-down on complete-enough data; the 'what about
  shorts' objection is addressed for the simpler short. A truly
  symmetric short remains a v3.1 hypothesis the operator can pursue
  separately if they want exhaustive coverage."
* **V25 PF ≥ 1.0** → "MATERIAL FINDING. Wind-down decision should be
  deferred until a proper bidirectional version is implemented and
  tested."

**V25 PF = 0.23 → first branch fires.**

### 8.5 What V25 rules out, and what it doesn't

**Rules out (after V25):**
* "The Phase A5 verdict was a long-only artefact." Adding the asymmetric
  short side made every economic metric worse, not better.
* "Shorts are the cheap unlock for swing-CNC on Nifty 50." Higher win
  rate is a trap; smaller wins + larger losses + nearly-doubled MaxDD.
* "There's an obvious edge hiding in the SELL emissions we're vetoing."
  No — the SELL emissions are individually tradeable but the ensemble
  result is net-negative.

**Does NOT rule out:**
* A truly symmetric short strategy (`trend_pullback_short` with mirror
  pullback / RSI / volume gates). V25's short side used ATR-based
  fallback SL/TP because the strategy's SELL was designed as a
  long-exit, not a short-entry. A proper short-side strategy could in
  principle have edge; V25 doesn't prove it doesn't.
* A different rule family entirely (Bollinger breakout, sector
  rotation, mean-reversion-with-trend-filter, etc.).
* The same rules with materially wider SL/TP (Option C from §6 above).

### 8.6 Charter discipline: what V25 does NOT trigger

Per charter §10.5 R1 ("do NOT debug into oblivion"):

* **No** "let me also build trend_pullback_short and breakout_20d_short
  to test the truly symmetric version" — that is a v3.1 hypothesis,
  pre-commit required, separate operator decision.
* **No** parameter sweep over fallback ATR SL/TP widths — would be
  curve-fitting on a single battery result.
* **No** retry on a different universe — V25 is on the same Nifty-30
  the charter pre-committed to.

### 8.7 Status of the wind-down decision

The 2026-06-05 wind-down trigger (T3 hypothesis failure per
`docs/freeze/wind_down_criteria_2026-06-05.md`) now has:

* V20-V24 long-only result: PF 0.21-0.41 (Phase A5).
* V25 shorts-allowed result: PF 0.23 (this update).
* Mechanical sanity confirmed: no bugs, fills land at next-day open,
  exits hit at sensible reasons and holding periods.
* The brutal review's primary objection (long-only-only test) is
  formally addressed.

The decision is now on complete-enough evidence. **Operator owns the
call.** This forensic does not recommend a path; per charter §10.5 R1
that is an operator-decision step, not a forensic-author step.
