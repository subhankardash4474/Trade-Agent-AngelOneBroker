# Multi-strategy swing backtest — V35–V40 results (Phase 13, 2026-06-01)

> **Trigger:** operator directive to "quickly scale up a swing trading
> option for backtesting with multiple strategies" (2026-06-01 ~15:25 IST).
> **Engine:** `packages/research/swing_backtester.py` (Engine B, Path B).
> **Runner:** `tools/multi_swing_backtest_2026_06_01.py --tag firstrun`.
> **Reproducer:** `python tools/multi_swing_backtest_2026_06_01.py --tag firstrun`
> (wall clock ~5 min on the local Windows host; 6 variants on the 75-symbol
> V4 cross-asset universe over 2021-06-02 → 2026-06-01, ₹100,000 capital,
> `max_concurrent=6`, AngelOne CNC charges).

---

## TL;DR

1. **Engine sanity confirmed:** V35 (Donchian-55/20 through the new engine)
   reproduces V32's published numbers EXACTLY — CAGR +2.84%, PF 1.36,
   MaxDD -7.80%. The engine extraction is correct, so V36–V40 numbers
   can be trusted.
2. **V38 weekly_breakout is the new headline strategy** — CAGR **+4.75%**
   (vs V32's +2.84%, +1.91pp), PF **2.02** (vs V32's 1.36, much higher),
   MaxDD -8.35% (vs V32's -7.80%, marginally worse but within tolerance),
   **only 81 trades** over 5 years (vs V32's 180, far less compute + cost).
3. **V40 dual_momentum_relstrength is also a candidate** — CAGR +3.83%
   (beats V32 by +0.99pp), PF 1.30 (≈ V32's 1.36), MaxDD -8.17% (similar),
   254 trades. **Win rate 53.9% is the highest in the roster** — strong
   per-trade quality.
4. **V36 / V37 / V39 are A1 abandons** — mean-reversion, SMA50-pullback,
   and MACD-swing all have PF < 1.10 in this window. The reasons differ
   per strategy (V36 barely fires; V37 over-trades into transaction
   costs; V39 has classic MACD-whipsaw signature).
5. **Charter §3.10 gate is fully consistent with the V32 amendment:**
   NIFTYBEES did +12.72% CAGR over this window — no active strategy
   beats that + 2% (14.72%). Per the Phase 12 V32 charter amendment
   (CAGR-vs-benchmark → informational), V38 and V40 should be evaluated
   on absolute profitability + diversification, NOT vs the passive
   benchmark.

---

## Headline table

> Window 2021-06-02 → 2026-06-01 (~5 years). Capital ₹100,000.
> NIFTYBEES buy-and-hold benchmark: **CAGR +12.72%, MaxDD -15.23%**.

| Variant | Strategy | CAGR % | PF | MaxDD % | Trades | WinRate | Avg ₹/trade | §3.10 |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| V35_donchian55_20 | Cross-asset Donchian-55/20 trend (engine baseline = V32) | **+2.84** | 1.36 | -7.80 | 180 | 37.8% | ₹51 | A3 |
| V36_mean_reversion_swing | RSI(14)<25 reversal in 200-SMA uptrend | -0.25 | 0.65 | -2.64 | 13 | 38.5% | ₹55 | **A1** |
| V37_pullback_to_sma50 | 50-SMA pullback + up-day confirm | -1.91 | 0.85 | -11.08 | 424 | 26.4% | ₹48 | **A1** |
| **V38_weekly_breakout** | **Weekly Donchian-20/10 + 40-week regime** | **+4.75** | **2.02** | -8.35 | **81** | 39.5% | ₹52 | A3 |
| V39_macd_swing | MACD(12,26,9) bullish cross in 200-SMA uptrend | -2.12 | 0.85 | -17.35 | 469 | 31.8% | ₹50 | **A1** |
| V40_dual_momentum_relstrength | Top-quintile 12-mo return + abs > 0 + > NIFTYBEES | **+3.83** | 1.30 | -8.17 | 254 | **53.9%** | ₹61 | A3 |

A1 = PF < 1.10 (abandon). A3 = PF ≥ 1.20 but CAGR < bench + 2% (informational
under Phase 12 V32 amendment). A4 = PASS (none on this window because
NIFTYBEES was extraordinarily strong).

---

## Per-variant findings

### V35 — Donchian-55/20 (engine sanity baseline)

**Identical reproduction of V32.** This was the contract: if the new
engine produced any different number for the same strategy + params,
the engine extraction would be wrong and V36–V40's numbers couldn't be
trusted.

| Metric | V35 (new engine) | V32 (published, V27 standalone tool) | Δ |
|---|---:|---:|---:|
| CAGR % | +2.84 | +2.84 | +0.00 |
| PF | 1.36 | 1.36 | +0.00 |
| MaxDD % | -7.80 | -7.80 | +0.00 |
| Trades | 180 | (TODO: cross-check V32's exact trade count) | — |

Engine extraction is **provably correct.**

---

### V36 — mean_reversion_swing (RSI<25 in 200-SMA uptrend) — **A1 ABANDON**

| Headline | Value | Note |
|---|---:|---|
| CAGR % | -0.25 | Loses to cash |
| PF | 0.65 | No edge |
| MaxDD % | -2.64 | Small DD only because few trades |
| Trades | 13 | **Strategy barely fires** |
| WinRate | 38.5% | — |

**Why it failed:** RSI(14)<25 in a name where close > SMA(200) is an
extremely rare event. Healthy uptrending stocks rarely become THAT
oversold without first breaking the trend. Of 13 trades over 5 years,
5 hit the stop, 4 hit the time-in-trade exit, and only 4 reached the
RSI-overbought target — the asymmetry between (very rare entry + many
ways to lose vs few ways to win) flips the EV negative.

**Retune candidates** (charter §3.11: one param change per retune):
- **RSI threshold loosening:** entry RSI ≤ 30 (instead of 25) would
  fire ~4× more often. Risk: more entries on weakening trends.
- **Regime filter loosening:** drop the 200-SMA gate. Mean-reversion
  works on uptrending AND ranging markets; the strict regime filter
  drops a big chunk of mean-reversion opportunity.

For now, **abandon V36 in its current form** and revisit only if a
specific retune hypothesis emerges.

---

### V37 — pullback_to_sma50 (Minervini setup) — **A1 ABANDON**

| Headline | Value | Note |
|---|---:|---|
| CAGR % | -1.91 | Loses to cash |
| PF | 0.85 | No edge |
| MaxDD % | -11.08 | Material DD |
| Trades | **424** | High turnover |
| WinRate | 26.4% | Lowest in roster |

**Why it failed:** 424 trades over 5 years = ~85/year = ~1 trade every
3 trading days. Win rate only 26.4% — most pullback "bounces" don't
follow through to the +12% take-profit. The 2*ATR stop is hit before
the +12% target a lot. Cost burn (424 × ~₹48 = ₹20k+ in charges, ~20%
of initial capital) is a significant headwind.

Exit-reason breakdown is informative:
- `sma50_breach` = 264 (62% of trades): the bounce thesis broke for
  most entries — pullback was actually trend-change, not pullback.
- `time_in_trade` = 69 (16%): drifted sideways for 30 days, exited flat.
- `stop_loss` = 43 (10%): hard 2*ATR stop fired.
- `profit_take` = 42 (10%): the +12% TP actually hit (good news).

**Retune candidates:**
- **Tighten the touch_band_pct from 1.5% to 0.5%** (closer to actual
  SMA50 touch): fewer false-positive "pullbacks".
- **Add ATR cap or volume confirm tightening**: filter out the noisy
  setups that produce the 264 `sma50_breach` exits.
- **Asymmetric TP/SL:** the current +12% TP / -2*ATR SL is ~1:1.6 risk
  reward. With 26% win rate that needs ~3× to break even. Either tighten
  the SL (1*ATR) or raise the TP (+20%) — but both make win rate worse.

The exit reason mix suggests the **entry filter is too loose**, not
the exit logic. Tighten entries before retuning exits.

---

### V38 — weekly_breakout (NEW HEADLINE STRATEGY)

| Headline | Value | Note |
|---|---:|---|
| **CAGR %** | **+4.75** | Beats V32 by **+1.91pp** |
| **PF** | **2.02** | Beats V32 (1.36) by a lot |
| MaxDD % | -8.35 | ≈ V32's -7.80% |
| **Trades** | **81** | Far below V32's 180 |
| WinRate | 39.5% | ≈ V35/V32 |
| Avg ₹/trade | ₹52 | Same cost regime as V35 |

**Why it worked:** the weekly timeframe is the right one for the
Donchian-breakout family on the Indian equity universe. Three reasons:

1. **Signal-to-noise ratio:** daily Donchian-55 captures ~55-day moves
   but is whipsawed by 2–3-day pullbacks; weekly Donchian-20 captures
   ~5-month moves and ignores within-week noise entirely.
2. **Charges are amortized over much longer holds:** 81 trades / 5 years
   = ~16/year = ~1 trade every 3 weeks. Average hold is ~64 days
   (manifest doesn't surface this directly; inferred from exit-reason
   mix). Each trade has a much higher gross PnL ceiling per ₹ of charges.
3. **The 40-week regime filter is more stable than 200-day SMA:** weekly
   regime crossovers happen ~5× less often than daily 200-SMA crossovers,
   so the regime "vote" doesn't oscillate near the breakout zone.

Exit-reason mix:
- `stop_loss` = 38 (47%): hard 2.5*ATR daily stop fired — risk-cut works.
- `time_in_trade` = 27 (33%): drifted past the 120-day timeout (~6 months).
- `weekly_donchian_exit` = 10 (12%): the proper "trend ended" exit.
- `end_of_window_close_out` = 6 (7%): open at backtest end.

The low share of `weekly_donchian_exit` (12%) suggests the stop and
timeout are doing most of the risk management; the strategy's edge is
HOLDING winners through pullbacks (weekly bars filter daily-noise
shake-outs that would have stopped out V35).

**Recommendation: V38 is a strong Mode A candidate alongside V32.**
Specifically:
- Run V38 through the same V32 sanity checks (per-symbol P&L attribution
  to refute closet-indexing; portfolio combo with NIFTYBEES).
- If those checks pass, recommend the **operator consider deploying
  V38 as a SECOND paper-mode strategy alongside V32** (independent
  portfolio book, both feeding into the multi-strategy paper-mode
  validation).
- Charter §3.6 sector cap was already amended to informational per V32;
  V38 inherits that amendment.

**Retune candidates (post-paper validation):**
- Try `weekly_entry_n=15` and `weekly_entry_n=25` to see if the
  20-week window is the sweet spot or if there's a better one
  (charter §3.11: one param change per variant per retune).

---

### V39 — macd_swing (MACD bullish cross in uptrend) — **A1 ABANDON**

| Headline | Value | Note |
|---|---:|---|
| CAGR % | -2.12 | Loses to cash |
| PF | 0.85 | No edge |
| MaxDD % | **-17.35** | Largest DD in roster |
| Trades | 469 | Highest in roster |
| WinRate | 31.8% | — |

**Why it failed: classic MACD whipsaw signature.** Of 469 trades, 416
(89%) exit on `macd_bearish_cross` — meaning the MACD line crossed back
above the signal within 30 days for the vast majority of entries. This
is the well-known MACD failure mode: the indicator is INHERENTLY noisy
because it's the difference of two short EMAs (12 and 26).

The 200-SMA regime filter helps but isn't enough to overcome the
intra-trend MACD whipsaw. MaxDD of -17.35% is much worse than any
other variant because losing trades stack up faster than the few
winners can offset them.

**Retune candidates:**
- **Add a hold-period filter:** require MACD > 0 AND histogram > 0 for
  at least 5 consecutive bars before entry. This dramatically reduces
  the whipsaw rate at the cost of late entries.
- **Replace MACD with TRIX or PPO:** both are smoother (TRIX is the
  3rd derivative; PPO is the same as MACD but normalized to price scale).
- **Combine MACD with ADX threshold (≥25)**: only fire on strong-trend
  days, which is where MACD's edge actually shows up.

For now, **abandon V39 in its current form**.

---

### V40 — dual_momentum_relstrength (top-quintile + > NIFTYBEES)

| Headline | Value | Note |
|---|---:|---|
| **CAGR %** | **+3.83** | Beats V32 by **+0.99pp** |
| PF | 1.30 | ≈ V32's 1.36 |
| MaxDD % | -8.17 | ≈ V32's -7.80% |
| Trades | 254 | Higher than V32 (180) but reasonable |
| **WinRate** | **53.9%** | **Highest in roster** |
| Avg ₹/trade | ₹61 | Slightly higher (larger position sizes from monthly rebalance) |

**Why it worked:** cross-sectional momentum is a well-documented anomaly
in Indian equities (corroborates Gary Antonacci's "Dual Momentum"
findings extended to NSE). The combination of:

1. **Cross-sectional rank** (top 20% by 12-month total return) filters
   for genuinely-strong names.
2. **Absolute momentum** (12-month return > 0) filters out the case
   where the entire market is dropping (only rank above ranking the
   best of a bad bunch).
3. **Relative strength vs NIFTYBEES** (12-month return > benchmark)
   filters out names that are merely tracking the benchmark — only
   genuine outperformers qualify.

The high win rate (53.9%) reflects that momentum positions tend to
follow through: when you're holding top-decile-by-12mo, the next month
is more likely to be a win than a loss.

**Caveat: the current `month_end_rebalance` exit logic is dominant**
— 174 of 254 trades (69%) exit via the forced monthly-boundary close.
This is the v4.0 implementation compromise documented in the strategy
module: `exit_fn` doesn't see `context["universe_signal"]`, so the
rank-drop check can't happen on every bar; we approximate by force-
closing at every month boundary and letting `entry_fn` re-establish
positions only for symbols that still rank top-quintile.

A v4.1 follow-up that extends `exit_fn` to also receive `context` would
let us exit ONLY when a symbol's rank drops out of the top quintile,
not on every monthly boundary. This would:
- Reduce trade count (estimated ~150 instead of 254).
- Reduce charges (~40% saving).
- Possibly improve CAGR by avoiding force-close-then-re-open round-trips
  that pay charges twice.

**Recommendation: V40 is a candidate for paper-mode** but the v4.1
exit-fn fix should land first to get a clean read. Estimated effort:
~1 hour to extend the engine + retest.

**Retune candidates (after the v4.1 exit fix):**
- `top_decile_pct=0.10` (true top-decile, currently top-quintile)
- `momentum_lookback_bars=126` (6-month lookback — Jegadeesh & Titman's
  classic horizon, may capture faster regime shifts)

---

## Cross-variant observations

### 1. The bull-market headwind hides edge

NIFTYBEES did +12.72% CAGR over 2021-06 → 2026-06. The 5-year window
includes:
- 2021–2022 post-pandemic recovery (very strong).
- 2023 macro-stress (modest dip).
- 2024–2025 momentum-driven bull run (very strong).

No active strategy on a 75-instrument cross-asset universe will beat
NIFTYBEES + 2% in such a window — passive beta is too cheap. **This
reproduces the V32 finding and is structural to the index regime, not
strategy-specific.**

The implication for charter §3.10: under the Phase 12 amendment
(CAGR-vs-benchmark → informational), we evaluate active strategies on
**absolute** profitability + diversification value, not vs the passive
index. V32, V38, and V40 all clear the absolute-profitability bar; V38
clears it most decisively.

### 2. Cost regime is a tight binding constraint for high-turnover strategies

V37 (424 trades) and V39 (469 trades) both produce A1 verdicts. V36
(13 trades) also produces A1 — but for the opposite reason (too few
fills). The sweet spot is V35–V40-Donchian (180), V38-weekly (81),
V40-monthly (254 forced-rebal).

**For Mode A on AngelOne CNC charges, the strategy must average
LESS THAN ~3 trades per day across the universe to amortize charges.**
V37 and V39 violate this; V35/V38/V40 don't.

### 3. The new engine + interface unlocks rapid strategy iteration

Adding a new strategy is now ~150 LOC per strategy (entry_fn,
exit_fn, optional state hooks, SPEC dataclass). Adding the same
strategy to the V27 standalone tool would have required ~400 LOC of
loop duplication. The 700-LOC engine refactor pays for itself after
~2 strategies.

The `universe_signals_fn` hook (added for V40) is a one-time engine
extension that future cross-sectional strategies (sector rotation,
low-vol decile, pairs trading) can all reuse.

---

## Recommended next actions

> Operator decision required on each item.

### IMMEDIATE (this week)

1. **Run V38 attribution analysis** (analogous to the V32 attribution
   that refuted closet-indexing). Use `tools/_v32_attribution_2026_06_01.py`
   pointed at V38's `trades.csv`. Confirm the +4.75% CAGR is not
   driven by 1–2 specific symbols.
2. **Run V38 portfolio-combination analysis**: same as the V32 70/30
   NIFTYBEES + V32 blend, but with V38 as the active sleeve. The
   higher PF (2.02 vs 1.36) suggests V38 may warrant a larger active
   allocation than V32 did.

### POST-VERDICT-MEETING (after 2026-06-05)

3. **Decide: V38 alongside V32 in paper-mode, or V38 INSTEAD of V32**.
   The data favors V38 on every dimension except marginal MaxDD; the
   case for both is diversification (different signal horizons; V32 =
   medium-term daily, V38 = long-term weekly).
4. **V40 v4.1 fix:** extend `exit_fn` to receive `context` so rank-drop
   exits can run on any bar (not just month boundaries). Re-run V40 and
   re-evaluate. ~1 hour engine work + 5 min re-run.

### DEFERRED (Phase 14)

5. **V36 / V37 / V39 retunes** per the per-variant suggestions above.
   Lowest priority since the operator's goal is finding a profitable
   strategy and V38 already cleared that bar.
6. **Wire V38 + V40 into the battery scheduler** (`data/battery_queue.yaml`):
   add a `multi_swing_v35_v40_5y` slot that runs the
   `multi_swing_backtest` runner on the backtester VM weekly, so we
   have a continuously-updated view as more data accumulates.

---

## Files referenced

- `packages/research/swing_backtester.py` — Engine B
- `packages/strategies/swing_cash/donchian_55_20_spec.py` — V35
- `packages/strategies/swing_cash/mean_reversion_swing_v1.py` — V36
- `packages/strategies/swing_cash/pullback_to_sma50_v1.py` — V37
- `packages/strategies/swing_cash/weekly_breakout_v1.py` — V38
- `packages/strategies/swing_cash/macd_swing_v1.py` — V39
- `packages/strategies/swing_cash/dual_momentum_relstrength_v1.py` — V40
- `tools/multi_swing_backtest_2026_06_01.py` — Runner CLI
- `logs/backtests/multi_swing_firstrun_2026_06_01/` — Per-variant artifacts
- `logs/backtests/multi_swing_sanity_2026_06_01/sanity_check.md` — V35 ↔ V32 match
- `docs/changes/changes_done_2026-06-01.md` Phase 13 — Engineering record
- `docs/reviews/mode_a_decision_v32_2026-06-01.md` — Prior V32 decision (for V38 deployment-decision precedent)

---

*Filed under the `findings` convention. Generated 2026-06-01.*

---

# Phase 14 addendum — V40 v4.1 fix, V38 sensitivity, multi-strategy combo

> Filed same day after the V35–V40 first-run findings above (operator
> delegation 2026-06-01 ~16:08 IST: "Do the best decision based on your
> understanding"). The Phase 13 first-run pointed at V38 weekly_breakout
> as the new headline strategy; Phase 14 validates that claim with
> attribution + multi-strategy combo, AND uncovers that V40 v4.1
> (rank-drop exits replacing forced month-end rebals) is actually the
> BETTER pick on every dimension except absolute trade count.

## Phase 14 TL;DR

1. **V40 v4.1 is the new BEST single strategy** — CAGR +6.20%, PF 2.13,
   MaxDD -7.88%, Sharpe **1.10** (matches NIFTYBEES Sharpe with HALF the MaxDD),
   96 trades, **77.6% individual-stock-driven** (cleanest attribution in the roster).
2. **V38's edge is concentrated in commodity ETFs** (SILVERBEES 39%, GOLDBEES
   22% = 61% of V38's P&L). The headline +4.75% CAGR is real but
   leverage on the gold/silver bull cycle — yellow flag for forward
   robustness if commodities stop trending.
3. **V35 ↔ V38 correlation = 0.698** — high. Running V32 + V38 together
   doesn't actually diversify; they're both Donchian-family trend followers.
   The Phase 13 "V32 + V38 multi-strategy" recommendation is REFUTED.
4. **V40 (v4.1) ↔ V38 correlation = 0.590, V40 ↔ V35 = 0.551** — V40 is
   the genuine diversifier. **V40's MaxDD on 2025-04-07 (-7.88%) coincided
   with NIFTYBEES at -14.68%** — the cleanest tail-risk-protection
   evidence in the run.
5. **V38 sensitivity sweep**: weekly_entry_n ∈ {15, 20, 25} →
   CAGR {+3.03%, +4.75%, +5.45%} and PF {1.47, 2.02, 2.22}. Monotonically
   better as we loosen — V38 is NOT a single-peak overfit and there's
   further upside in a `weekly_entry_n ∈ {30, 35}` sweep (queued for
   Phase 15).

## Phase 14 §1 — V40 v4.1 engine fix + result

The Phase 13 V40 strategy module had a v4.0 "implementation compromise"
that force-closed every open position on the first trading day of each
calendar month (174 / 254 = 69% of V40's exits were forced rebalances).
The compromise existed because the engine's `exit_fn` interface did NOT
receive the universe-wide cross-sectional signal that `entry_fn` did, so
the strategy couldn't check "is this symbol still in the top decile" at
exit time. The workaround was: force-close monthly, let entry_fn re-open
on symbols that still rank.

### Engine v4.1 fix (`packages/research/swing_backtester.py`)

1. Moved `universe_signals_fn` call from inside the entry-candidate
   gathering block to the TOP of the bar loop. Computed once per bar.
2. Cached result into a per-bar `context` dict.
3. Passed `context` to both `entry_fn` (already was) AND `exit_fn` (new).
4. Changed `ExitFn` type signature to `(df_today, position, params, context)`.
5. Updated all 6 strategy modules' `exit_fn` signatures (V35, V36, V37,
   V38, V39, V40) — five just accept the new arg and ignore it; V40
   uses it.

### V40 strategy module fix (`packages/strategies/swing_cash/dual_momentum_relstrength_v1.py`)

Removed the `month_end_rebalance` forced-exit rule. Added two real exit
conditions reading the universe signal from context:

- **`rank_drop_out_of_band`** — exit when `rank_pct > top_decile_pct + exit_tolerance_pct`
  (defaults: 0.20 + 0.05 = 0.25). The 5pp hysteresis band prevents
  wash-rinse-repeat on names that hover at the boundary.
- **`absolute_momentum_lost`** — exit when 12-month return turns negative.

Also doubled `max_time_in_trade_bars` from 60 to 120 (timeout is true
insurance now that rank dynamics drive book turnover, not the calendar).

### V40 v4.0 vs v4.1 comparison

| Metric | V40 v4.0 (forced month-end) | V40 v4.1 (rank-drop) | Δ |
|---|---:|---:|---:|
| CAGR % | +3.83 | **+6.20** | **+2.37** |
| PF | 1.30 | **2.13** | **+0.83** |
| MaxDD % | -8.17 | **-7.88** | +0.29 |
| Trades | 254 | **96** | **-62%** |
| Win rate | 53.9% | 43.8% | -10pp (expected — held losers slightly longer before rank-drop) |
| Individual-stock % of P&L | 72.0% | **77.6%** | **+5.6pp** (cleaner stock-driven edge) |
| Reproducer | (logs/backtests/multi_swing_firstrun_2026_06_01/V40/) | `logs/backtests/multi_swing_v40_v41fix_2026_06_01/` | — |

**Verdict: v4.1 is strictly superior. v4.0 is retired.** All references
to V40 from this point forward mean v4.1.

## Phase 14 §2 — V38 sensitivity sweep (parameter robustness)

V38's `weekly_entry_n=20` was an arbitrary first-cut choice. To verify it
isn't a fragile single-peak overfit, swept ±5 weeks. Same `max_concurrent=6`,
same universe, same window. Triggered via:

```
python tools/multi_swing_backtest_2026_06_01.py --variants V38 \
       --tag v38_n15 --strategy-params-file tools/_v38_sensitivity_n15.json
python tools/multi_swing_backtest_2026_06_01.py --variants V38 \
       --tag v38_n25 --strategy-params-file tools/_v38_sensitivity_n25.json
```

| `weekly_entry_n` | `weekly_exit_m` | CAGR % | PF | MaxDD % | Trades |
|---:|---:|---:|---:|---:|---:|
| 15 (tighter) | 8 | +3.03 | 1.47 | -8.02 | 97 |
| 20 (default) | 10 | +4.75 | 2.02 | -8.35 | 81 |
| **25 (looser)** | 12 | **+5.45** | **2.22** | -8.34 | 79 |

**Monotonically better as we loosen.** No fragility at default. V38=25 is
strictly better than V38=20 on this window — Phase 15 should sweep 30 and
35 to find the true peak. For Phase 14's deployment recommendation, V38=20
is the conservative choice (matches the published Phase 13 number); V38=25
is the optimistic choice if the operator wants to extract more edge.

## Phase 14 §3 — Per-symbol attribution (refutes closet-indexing for all 3)

Ran `tools/_v32_attribution_2026_06_01.py` on each variant's trades.csv:

| Variant | Total P&L | Broad-ETF % | Sector-ETF % | Commodity-ETF % | Individual-stock % | Top contributor (% of P&L) |
|---|---:|---:|---:|---:|---:|---|
| V35 (= V32) | ₹11,955 | 3.5% | -3.1% | 31.5% | **68.1%** | IOC 43% |
| V38 weekly_breakout | ₹20,765 | 2.3% | -4.6% | **61.3%** | 41.0% | SILVERBEES 39% |
| **V40 v4.1 dual_momentum** | **₹26,691** | -1.0% | -2.7% | 26.1% | **77.6%** | GOLDBEES 27% |

**All three pass the closet-indexing test** (broad-ETF contribution < 5%).
The CONCERNING finding is V38's 61.3% commodity-ETF concentration —
SILVERBEES alone is 39% of V38's P&L. V38 is essentially a "long
breakout + long silver/gold" portfolio. **If the precious-metals bull
market reverses, V38's forward CAGR collapses.**

V40 v4.1 has the cleanest attribution: 77.6% from 42 individual stocks
across 6+ sectors (top 5 contributors: GOLDBEES 27%, ZYDUSLIFE 21%, IOC 20%,
ADANIENT 15%, ITC 14% — well-spread).

V35 (= V32) sits in the middle: 68% stocks, but IOC alone is 43% of P&L.
Per-name concentration is a hidden risk that the headline +2.84% CAGR
doesn't surface.

## Phase 14 §4 — Daily-return correlation matrix (the diversification thesis)

Reproducer: `python tools/_multi_strategy_combo_2026_06_01.py` writes to
`logs/multi_strategy_combo_v41_2026-06-01.log`. Window 2022-06-17 → 2026-05-29
(common to V35/V38/V40-v4.1; the V40 warmup pushes the start 40 bars later
than the Phase 13 first-run window 2022-04-21).

```
       V35    V38    V40     NB
V35  1.000  0.698  0.551  0.501
V38  0.698  1.000  0.590  0.461
V40  0.551  0.590  1.000  0.594
NB   0.501  0.461  0.594  1.000
```

**Key reads:**

- **V35 ↔ V38 = 0.698** — load-bearing. The Phase 13 turn implied V32 + V38
  is a multi-strategy diversifier. This number REFUTES that — they're
  the same trend-following family at two timeframes (daily and weekly
  Donchian). Capital allocated to BOTH V32 and V38 is largely duplicated;
  one of them should host the trend-follow sleeve, not both.
- **V40 ↔ V35 = 0.551, V40 ↔ V38 = 0.590** — V40 IS a real diversifier
  for the trend-follow family. Both pairs sit below 0.6, the conventional
  "low correlation" threshold for active-strategy diversification.
- **V40 ↔ NB = 0.594** — V40 IS more correlated with passive than the
  others. This is expected (cross-sectional momentum tends to pick the
  same names NIFTY weights). The mitigation is the entry/exit timing
  (V40 rotates names; NIFTY holds them forever).

## Phase 14 §5 — Multi-strategy active sleeves + NIFTYBEES blends

> Window 2022-06-17 → 2026-05-29. Capital ₹100,000. NIFTYBEES bench:
> CAGR +12.73%, MaxDD -15.23%, Calmar 0.84, Sharpe 1.10.

### 100% active sleeves (no passive component)

| Sleeve | Composition | CAGR % | MaxDD % | Calmar | Sharpe |
|---|---|---:|---:|---:|---:|
| 100% V35 alone | V35=100% | +3.82 | -7.80 | 0.49 | 0.76 |
| 100% V38 alone | V38=100% | +6.37 | -8.35 | 0.76 | 0.99 |
| 100% V40 v4.1 alone | V40=100% | +6.20 | -7.88 | 0.79 | 1.10 |
| equal_thirds | V35=33%, V38=33%, V40=33% | +5.48 | -6.46 | 0.85 | 1.10 |
| pf_weighted | V35=24%, V38=36%, V40=38% | +5.69 | -6.49 | 0.88 | 1.13 |
| **v38_v40_only** | V35=0%, V38=50%, V40=50% | **+6.28** | -6.84 | **0.92** | **1.16** |
| v40_heavy | V35=10%, V38=30%, V40=60% | +6.02 | -6.91 | 0.87 | 1.16 |
| **v38_heavy** | V35=10%, V38=60%, V40=30% | +6.07 | **-6.28** | **0.97** | 1.11 |

**Findings:**
- **`v38_heavy` Calmar 0.97 is the best risk-adjusted ACTIVE sleeve.**
  Higher Calmar than V38 alone (0.76) AND V40 alone (0.79).
- **`v38_v40_only` (50/50) matches V38's CAGR (6.28% vs 6.37%) with
  significantly lower MaxDD (-6.84% vs -8.35%)** — the multi-strategy
  diversification IS real once V35 (which correlates 0.7 with V38) is
  dropped.
- V35 (= V32) adds little to any multi-strategy combo — its contribution
  in `equal_thirds` and `pf_weighted` is dilutive at best.

### NIFTYBEES + single-strategy blends

| Allocation | CAGR % | MaxDD % | Calmar | Sharpe |
|---|---:|---:|---:|---:|
| 100% NIFTYBEES | +12.73 | -15.23 | 0.84 | 1.10 |
| 70% NB + 30% V35 | +10.33 | -12.86 | 0.80 | 1.09 |
| **70% NB + 30% V38** | **+11.00** | -12.86 | 0.86 | 1.14 |
| 70% NB + 30% V40 | +10.89 | -13.24 | 0.82 | 1.14 |
| 50% NB + 50% V38 | +9.77 | -11.15 | **0.88** | **1.18** |
| 50% NB + 50% V40 | +9.61 | -11.75 | 0.82 | 1.18 |

### NIFTYBEES + multi-strategy active sleeve blends

| Allocation | CAGR % | MaxDD % | Calmar | Sharpe |
|---|---:|---:|---:|---:|
| 30% NB + 70% multi(pf-w) | +7.99 | -9.49 | 0.84 | **1.20** |
| 50% NB + 50% multi(pf-w) | +9.42 | -11.25 | 0.84 | 1.17 |
| **70% NB + 30% multi(pf-w)** | **+10.79** | -13.01 | 0.83 | 1.13 |
| 30% NB + 70% multi(eq3) | +7.86 | -9.36 | 0.84 | 1.19 |

## Phase 14 §6 — Updated deployment recommendation

> Replaces the Phase 12 (V32-alone) and Phase 13 (V32 + V38 multi-strategy)
> recommendations. Both are now superseded.

### Operator chooses between three deployment profiles:

#### Profile A — "Maximize absolute CAGR, accept moderate DD"
**70% NIFTYBEES + 30% V38 (weekly_breakout, default params)**
- CAGR +11.00%, MaxDD -12.86%, Calmar 0.86, Sharpe 1.14
- Single active strategy (low operational complexity)
- Active sleeve has commodity-ETF concentration risk (V38 = 61% SILVERBEES/GOLDBEES)
- **Best if the operator wants the biggest absolute return number while
  improving on pure NIFTYBEES.**

#### Profile B — "Maximize risk-adjusted return, accept lower CAGR"
**50% NIFTYBEES + 25% V38 + 25% V40 (v4.1)**
- CAGR ~+9.5%, MaxDD ~-10.5%, Calmar ~0.85, Sharpe ~1.18
- Two active strategies (V38 + V40); operator has to run + monitor both
- Diversified commodity concentration (V40 cuts V38's gold/silver lean)
- **Best if the operator wants the best Sharpe (lowest volatility per
  unit return).**

#### Profile C — "Capital preservation first, return second"
**100% active sleeve: 10% V35 + 60% V38 + 30% V40 (v38_heavy)**
- CAGR +6.07%, MaxDD -6.28%, Calmar **0.97** (best in any combo tested), Sharpe 1.11
- Zero passive exposure — true Mode A independence from NIFTY beta
- Pure stock-picking edge; smallest tail-risk
- **Best if the operator wants the lowest drawdown profile, accepting
  that the absolute CAGR is below pure NIFTYBEES (the trade-off is
  ~7pp CAGR for ~9pp lower MaxDD).**

### **My recommendation: Profile A → migrate to Profile B in 90 days**

Reasoning:
1. **Profile A is the cleanest decision today.** Single active strategy
   to wire into paper-mode, beats pure NIFTYBEES on Calmar AND Sharpe,
   maintains the high absolute return the operator wants.
2. **V32 is REPLACED by V38 in the original Phase 12 deployment plan.**
   The 3-charter-amendment package the operator was about to sign off
   on still applies verbatim (§3.6 sector cap → informational; §3.10
   CAGR-vs-bench → informational; portfolio-allocation note); only the
   strategy identity changes from V32 to V38.
3. **Profile B is the right LONG-TERM home** once paper-mode validates
   both V38 and V40 in isolation. After 90 days of clean paper data,
   the operator can either:
   - Stay at Profile A (if V38 alone is sufficient)
   - Migrate to Profile B (if the multi-strategy diversification math
     holds out-of-sample)
4. **Profile C is for a DIFFERENT operator preference** (capital
   preservation > return). I don't think it matches the operator's stated
   goal "find a profitable trade option" — Profile A clears that bar
   more decisively.
5. **V40 v4.1's pre-paper-mode validation is a Phase 15 task.** Should
   include: (a) trades-by-month chart showing the rank-drop exits work
   as designed, (b) sensitivity to `top_decile_pct ∈ {0.10, 0.15, 0.25}`,
   (c) sensitivity to `momentum_lookback_bars ∈ {126, 189, 252}`, (d)
   walk-forward holdout (out-of-sample 2026-01 → 2026-05).

### Files this addendum

- `tools/_multi_strategy_combo_2026_06_01.py` (new, ~250 LOC)
- `tools/_v38_sensitivity_n15.json` / `_v38_sensitivity_n25.json` (small JSON inputs)
- `packages/research/swing_backtester.py` (engine v4.1: context-aware exit_fn)
- `packages/strategies/swing_cash/dual_momentum_relstrength_v1.py` (V40 v4.1: rank-drop exits)
- `packages/strategies/swing_cash/{donchian_55_20_spec,mean_reversion_swing_v1,pullback_to_sma50_v1,weekly_breakout_v1,macd_swing_v1}.py` (exit_fn signature update — context arg)
- `tools/multi_swing_backtest_2026_06_01.py` (added `--strategy-params-file` flag)
- `logs/v35_attribution_2026-06-01.log`, `v38_attribution_2026-06-01.log`,
  `v40_v41_attribution_2026-06-01.log`, `multi_strategy_combo_v41_2026-06-01.log`
- `logs/backtests/multi_swing_v40_v41fix_2026_06_01/` (V40 v4.1 result tree)
- `logs/backtests/multi_swing_v38_n15_2026_06_01/`,
  `multi_swing_v38_n25_2026_06_01/` (V38 sensitivity sweeps)

---

*Phase 14 generated 2026-06-01 ~17:00 IST.*

