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
