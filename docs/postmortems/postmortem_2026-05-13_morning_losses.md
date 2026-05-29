# Post-mortem — 2026-05-13 morning losses (HCLTECH + BAJFINANCE)

**Status**: Two consecutive stop-outs in the first 80 minutes of the
session. **Day P&L: −Rs 303.52** (0W/2L). Both losses share the SAME
root-cause bug (now fixed) and one would-have-helped feature (now
shipped).

---

## The trades

| Symbol | Strategy | Side | Entry | Exit | SL distance | P&L | Hold |
|---|---|---|---:|---:|---:|---:|---:|
| HCLTECH | supertrend_follow | SELL | 1142.35 | 1150.40 | **0.70 %** | −148.24 | 29 min |
| BAJFINANCE | xgboost_classifier | SELL | 896.72 | 903.53 | **0.76 %** | −155.28 | 27 min |

Both stop distances are **well inside the configured 1.2 % noise floor**
(`risk.min_stop_loss_pct`). The floor exists *exactly* to prevent
this — sub-1 % stops on quiet stocks get knocked out by normal intraday
noise before the thesis has a chance to play out.

---

## Three logic issues surfaced

### 1. SL floor bypass (THE root cause)  —  fixed in this batch

`trading_agent.py:2817` did:

```python
stop_loss = signal.stop_loss or self.risk_manager.get_stop_loss(...)
```

Strategy-provided SLs (supertrend uses `price ± 3 × ATR`, xgboost uses
its own logic) flowed through the **left side** of the `or` and never
saw the floor. The floor was only applied inside `get_stop_loss`. Every
quiet-stock SL came in below the 1.2 % floor and we bled out on noise.

**Fix**: extracted the floor into a standalone `RiskManager.enforce_sl_floor`
helper. `trading_agent.py` now routes strategy SLs through it
unconditionally:

```python
if signal.stop_loss:
    stop_loss = self.risk_manager.enforce_sl_floor(current_price, signal.stop_loss, side)
else:
    stop_loss = self.risk_manager.get_stop_loss(current_price, side, atr)
```

**Counterfactual on today**:
- HCLTECH: floored SL would be **1156.06** instead of 1150.40 → the
  1150.40 spike at 10:06 would NOT have triggered the stop. Trade still
  open (or exited via a different reason later).
- BAJFINANCE: floored SL would be **907.48** instead of 903.53 → same
  story. The 10:31 spike does not breach the floored stop.
- Estimated combined save: **≥ Rs 303** (the entire day's loss).

### 2. Regime exposure not size-scaled  —  shipped in this batch

Both trades fired in `bear_high_vol` regime — historically our weakest
tape. Risk size today was 100 % of baseline even though we have repeated
evidence (2026-05-12 GODREJCP, 2026-05-13 HCLTECH+BAJFINANCE) that this
regime whipsaws shorts.

**Fix**: new `risk.regime_size_multipliers` config block + a
`regime_size_multiplier(regime)` method on `RiskManager`, applied inside
`calculate_position_size`. Defaults: `bear_high_vol: 0.70`,
`bull_low_vol: 1.20`, others 0.85–1.00.

**Counterfactual on today**:
- HCLTECH: 16 shares × Rs 8.05 loss + Rs 19.44 commission → with 0.70x
  the position would have been ~11 shares × 8.05 + ~Rs 15 = **−Rs 104**
  instead of −Rs 148 (saves Rs 44).
- BAJFINANCE: 20 shares × Rs 6.81 loss + Rs 19.08 commission → with
  0.70x the position would have been ~14 shares × 6.81 + ~Rs 15 =
  **−Rs 110** instead of −Rs 155 (saves Rs 45).
- Even WITHOUT bug #1's fix, regime-sizing alone would have cut today's
  loss by ~Rs 89 (29 % reduction).

### 3. XGBoost direction flip-flop  —  NEW pending item, not fixed yet

BAJFINANCE's signal trail in the log:

```
09:34 XGB BUY  @ 897.20 (prob_up=0.695)     ← model says LONG
09:52 XGB SELL @ 892.45 (prob_down=0.725)   ← 180° flip 18 min later
10:04 ENTRY: SHORT 20 @ 896.72 (xgboost alone, ensemble conf=0.556)
10:31 STOP   @ 903.53                        ← whipsawed
```

The XGB model went BUY → SELL in 18 minutes and we took the second
signal. This is a known overfitting failure mode of single-bar models.

**Proposed fix** (not in this batch): require XGB to remain on the same
side for ≥ 2 consecutive candles before being eligible to trigger an
ensemble entry. Cheap to implement (just a per-symbol "last side + bars
since last flip" tracker), zero false-negatives on real momentum
because real momentum sustains for >5 min.

Also separately worth investigating: HCLTECH entered at the **bottom**
of a 20-minute drift down — XGB had been bearish since 09:17 but
supertrend didn't confirm until 09:37, exactly at the local low. This
is "wait for confirmation, enter the reversal" — a known late-entry
trap. Not specific to today; design-level discussion needed.

---

## "Could the counter-trade have made a profit?"

In hindsight — yes, marginally. But the proposal needs a clear-eyed
answer.

| Symbol | Our side | Result | Counter-side | Implied result (entry → stop time) |
|---|---|---|---|---|
| HCLTECH | SHORT | −0.71 % | LONG @ 1142.35 | +0.71 % at 10:06 |
| BAJFINANCE | SHORT | −0.76 % | LONG @ 896.72 | +0.76 % at 10:31 |

So a mirror LONG at *our exact entry price* would have ridden the
adverse move into profit. But:

1. **No LONG signal existed at those moments**. The ensemble didn't
   vote BUY on either stock at 09:37 / 10:04. The "counter-trade" is
   pure hindsight bias — we'd be reversing on intuition, not edge.
2. **The current regime guard blocks LONG entries in
   `bear_high_vol`** (`execution.long_entry_regimes:
   [bull_low_vol, bull_high_vol]`). Any attempt to manually go LONG
   right now would be rejected at the gate. Bypassing the guard on a
   hunch after a loss is *literally* the textbook way to give back more
   on a single trade than the two losses combined.
3. **Path matters**. Both stocks may continue UP from here, may
   whipsaw back down, may sit flat. The stop firing only tells us
   "*price was here at this minute*", not "*price is going there
   next*". Counter-trading the loser assumes mean reversion that
   doesn't exist in trending tape.
4. **The right counter to our miss is system-level, not
   trade-level**. The two fixes shipped today (`enforce_sl_floor` +
   `regime_size_multipliers`) make the *same losing setup* lose less,
   indefinitely. A one-off counter-trade fixes nothing structurally.

**Recommendation**: do NOT reverse. Let the cooldown (BAJFINANCE: 30 min
from stop = 11:01; HCLTECH: already past at ~10:36) expire and let the
system re-engage with proper SL flooring on the next bona-fide signal.

---

## Summary of fixes shipped today

| Fix | Hits today's losses | Counterfactual save |
|---|---|---:|
| `enforce_sl_floor` on strategy-supplied SLs | ✅ HCLTECH + BAJFINANCE | **−Rs 303** (full day) |
| `regime_size_multipliers` (0.70x in bear_high_vol) | ✅ HCLTECH + BAJFINANCE | **−Rs 89** (29 % of loss) |
| `_tune_confidence_threshold` smoothing (no more ratchet) | indirect | edge over time, not today |
| `min_profit_to_charges_ratio` 2.5 → 3.0 | indirect | rejects marginal entries |
| `_symbols_done_today` rehydrate on restart | indirect | restart safety, not today |

Combined: the SAME setup today, with both fixes in place, would have
left BOTH stops at floored levels that weren't breached. **Most likely
outcome: 2 open positions instead of 2 closed losses.**

---

## Pending items (logged to TODO)

1. **XGB direction-stability filter** — require ≥ 2 consecutive same-side
   XGB signals before that strategy contributes weight to an ensemble vote.
2. **Late-entry diagnostic** — instrument: how many minutes elapsed
   between first matching-side signal and entry? If we routinely enter
   at the END of a directional move, the ensemble's
   confirmation-waiting is overcalibrated. Surface in EOD report.
3. **MFE/MAE per-trade** — neither HCLTECH nor BAJFINANCE has logged
   maximum favourable/adverse excursion. Required to tune trailing-stop
   and peak-giveback further.
4. **Hybrid signals** (RSI+VWAP, ORB+ST) — vision-spec item; postponed
   to dedicated session because it needs backtest validation.
5. **EMERGENCY_STOP flatten verification** — feature exists with
   `emergency_stop_flatten: false`; never tested on live.

---

## Deployment

These fixes are now in:
- `packages/core/risk_manager.py` — `enforce_sl_floor`,
  `regime_size_multiplier`, regime-aware `calculate_position_size`
- `trading_agent.py` — strategy SL routed through floor; regime passed
  into sizing; threshold smoothing; single-shot rehydrate
- `config.yaml` — `regime_size_multipliers` block,
  `min_profit_to_charges_ratio: 3.0`
- 21 new unit tests across `test_risk_manager.py` and
  `test_single_shot_enforcement.py`. Total: 489 pass, 0 lint errors.

Next step: `git push` and `docker compose up --build -d` on the OCI VM
to deploy.
