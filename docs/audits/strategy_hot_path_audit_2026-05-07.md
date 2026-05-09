# Strategy Hot-Path Audit — 2026-05-07

Done as part of Phase 1a. Read-only audit of all 6 active strategies for
per-bar performance bottlenecks. The trigger was: the overnight battery's
first variant (10 symbols × 30 days × 5min, 12,975 bars, 6 strategies)
took ~50 minutes — an order of magnitude slower than expected.

## Methodology

For each strategy's `generate_signal(data, symbol)`, looked for:

1. `data.copy()` — full DataFrame copy on every call.
2. `.rolling(...)` / `.ewm(...)` over the FULL history when only the last
   bar's value is consumed.
3. Python `for i in range(len(df))` with `.iloc[i]` writes — the most
   expensive Pandas anti-pattern.
4. Duplicate work — the same indicator computed twice in the same call.

## Findings

| Strategy | `df.copy()` | Full-history rolling | Python for-loop iloc | Duplicate work | Severity |
|---|---|---|---|---|---|
| mean_reversion          | YES | 4 (`mean`, `std`, `bb_upper`, `bb_lower`) | NO | NO  | HIGH |
| supertrend_follow       | YES | 3 (ATR×2, ADX) | **YES (1 → len(df))** | **ATR computed twice**  | CRITICAL |
| moving_average_crossover| YES | 2 SMAs | NO | NO  | MEDIUM |
| opening_range_breakout  | YES | 2 (`vol_avg`, ATR) | NO | NO  | MEDIUM |
| vwap_bounce             | YES | `vol_avg` + cumulative VWAP | NO | NO  | MEDIUM |
| rsi_momentum            | YES | RSI via full-series `ewm` | NO | NO  | MEDIUM |
| xgboost_classifier      | (uses pre-computed features) | NO | NO | NO | LOW |

### Detail: `mean_reversion.py` (HIGH)

Lines 69-77:

```python
df = data.copy()                                         # ⚠ full copy
df["rolling_mean"] = df["close"].rolling(20).mean()      # ⚠ full series
df["rolling_std"]  = df["close"].rolling(20).std()       # ⚠ full series
df["z_score"]      = (df["close"] - df["rolling_mean"]) / df["rolling_std"].replace(0, np.nan)
df["bb_upper"]     = df["rolling_mean"] + 2 * df["rolling_std"]
df["bb_lower"]     = df["rolling_mean"] - 2 * df["rolling_std"]
df["bb_width"]     = (df["bb_upper"] - df["bb_lower"]) / df["rolling_mean"]
```

Only 3 cells are used downstream: `z[-1]`, `z[-2]`, `rolling_mean[-1]`,
`bb_width[-1]`. Every other cell is thrown away.

Refactor (Phase 1b):
```python
closes = data["close"].iloc[-self.lookback_period - 1:]   # 21-22 values
mean = closes.mean()
std  = closes.std(ddof=1)                                 # pandas default
z    = (closes.iloc[-1] - mean) / std if std > 0 else np.nan
# Compute z_prev from the previous-bar window of the same length:
prev_window = data["close"].iloc[-self.lookback_period - 2:-1]
z_prev = (prev_window.iloc[-1] - prev_window.mean()) /
         (prev_window.std(ddof=1) or np.nan)
# bb_width is derived from std/mean — already have those.
bb_width = (4 * std) / mean if mean > 0 else np.nan
```

Drops from O(N) per call to O(lookback) per call. For 12,975 bars at
lookback=20, this is ~12,975 × 22 = 285K ops vs current 12,975 × 12,975 =
168M ops (Pandas rolling allocates a sliding window over the FULL series
even though we only consume the last value).

### Detail: `supertrend_follow.py` (CRITICAL)

Lines 75-101:

```python
def _compute_supertrend(self, df) -> tuple:
    atr = self._compute_atr(df, self.period)              # full series
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + self.multiplier * atr
    lower = hl2 - self.multiplier * atr

    direction = pd.Series(1, index=df.index)
    st = pd.Series(np.nan, index=df.index)

    for i in range(1, len(df)):                           # ⚠ Python loop
        if df["close"].iloc[i] > upper.iloc[i - 1]:       # ⚠ chained iloc
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
        # ... more iloc reads + writes ...
```

Then:
```python
df["adx"] = self._compute_adx(df)                         # full series
atr_val = float(self._compute_atr(df, self.period).iloc[-1])  # ⚠ ATR AGAIN
```

Two issues:
1. The Python for-loop iterates the FULL history every single call. With
   12,975 bars per symbol and 6 strategies running per bar, that's
   12,975 × 12,975 = 168M iloc operations per symbol per backtest variant.
   This is BY FAR the dominant cost.
2. ATR is computed twice in one call (once in `_compute_supertrend`,
   once at line 117 just to get the latest value).

Refactor (Phase 1c):
- Cache the supertrend state on `self`: `(direction, lower, upper, last_idx)`.
  On each call, advance state from `last_idx + 1 → len(df) - 1` only.
  For live trading that's exactly 1 bar of work; for backtest, still O(N)
  total but no longer per-bar.
- Compute ATR once and pass through.

### Detail: `rsi_momentum.py` (MEDIUM)

`_compute_rsi` uses `series.diff()` + `.ewm()` over the full series. Same
issue as mean_reversion but cheaper because RSI math is simpler.

### Detail: others (MEDIUM)

`moving_average_crossover`, `opening_range_breakout`, `vwap_bounce` all
do `data.copy()` + a couple of full-series rolling calls. Same pattern,
same fix: read tail-slice, compute scalars.

## Estimated speedup

| Refactor | Per-call cost (12k-bar window) | Battery time |
|---|---|---|
| Current                    | O(N) across 7-10 indicators | ~50 min/variant |
| Phase 1b (mean_reversion)  | O(lookback) for that strategy | ~40 min/variant |
| Phase 1c (supertrend cache)| O(1) per bar after warmup | ~15 min/variant |
| Both above + tail-slice on others | All strategies O(lookback) | **~5-8 min/variant** |

Conservative 5-10x speedup. For LIVE trading the win is smaller per-cycle
(~50ms saved per cycle?) but adds up over the day. The bigger win is
backtest iteration speed.

## Risk of the refactor

HIGH — strategy logic is load-bearing. Each refactor MUST:

1. Be paired with a snapshot test (`generate_signal(historical_df) == old_result`).
2. Run the existing strategy unit tests + behavioural tests as a regression gate.
3. Be merged one strategy at a time, not in a single bulk PR.

## Recommendation for tomorrow's run

DO NOT refactor live before market open. The audit is the deliverable
for tonight. Refactor work is queued as Phase 1b/c — to be done in a
follow-up session with paired snapshot tests.

The 50-min battery variant is acceptable as a one-time overnight run.
Once we have a winning config from tonight's battery, the refactor will
make iterative parameter tuning cheap.

## Action items

- [x] Audit all 6 strategies — DONE (this doc).
- [ ] Phase 1b: refactor `mean_reversion.generate_signal` with snapshot test.
- [ ] Phase 1c: refactor `supertrend_follow._compute_supertrend` with cached state.
- [ ] Phase 1c (other): tail-slice the remaining 3 strategies.
- [ ] Re-run battery on the refactored code; confirm metrics match within
      Rs 1 / 1 trade (snapshot test gate).
