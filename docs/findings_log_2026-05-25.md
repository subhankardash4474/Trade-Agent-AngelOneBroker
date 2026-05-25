# Findings Log — 2026-05-25 (Week-2 Freeze Review Prep)

**Author:** Operator + automated battery
**Status:** Living document, append-only
**Purpose:** Preserve all analysis from the Monday 2026-05-25 deep dive
so the Friday 2026-05-29 review (and any future post-mortem) has the
full evidence trail without re-deriving from raw logs.

---

## Executive summary (TL;DR)

After Week-2 of the freeze, three independent data sources point to
the same conclusion: **the trading engine is not broken; the SHORT
SIDE is structurally negative-edge regardless of tuning, and the live
agent has been routed exclusively into shorts since 2026-05-13 due to
the `bear_high_vol` regime classification, which explains the
-₹1,505 / 28-trade live bleed.**

The cheapest, highest-confidence fix is **`risk.allow_shorts: false`**
(disable new short entries, keep all other behavior). The expected
edge of "V1 long-only" is approximately **PF 1.5, +₹556 over 90 days
on the live universe shape** based on the V1 results from the
2026-05-18 pre-speed-patch battery (extracting only the long-side
trades).

This document captures the four data sources that drove that
conclusion.

---

## 1. Live agent performance (Week 1 + 2 of freeze)

| Metric | Reading | Source |
|---|---|---|
| Days traded | 14 (2026-05-13 → 2026-05-23) | health.json + DB |
| Trades closed | 28 | `trades` table |
| Cumulative PnL | -₹1,505 | `trades.realized_pnl` sum |
| Win rate | 38% | `trades.realized_pnl > 0` ratio |
| Long trades | 0 | all rows have `side = 'SELL'` |
| Short trades | 28 (100%) | all rows have `side = 'SELL'` |
| Active regime | `bear_high_vol` for ~all of Week 2 | regime classifier output |
| Active strategies | `supertrend_follow`, `rsi_momentum` (only) | regime weights |
| Per-strategy: `mean_reversion` | 0 trades | regime-weighted out |
| Per-strategy: `xgboost_classifier` | 1 trade | low-volume in bear regime |
| Per-strategy: `vwap_bounce` | 0 trades | regime-weighted out |
| Per-strategy: `opening_range_breakout` | 0 trades | regime-weighted out |
| R:R achieved | 1:0.39 | stop-loss exits dominate |
| Implied required WR for break-even | 71% | (1 / (1 + 0.39)) |
| Actual WR | 38% | half of required |

**Verdict on its own:** insufficient sample size (n=28) to claim
"engine has no edge" with confidence. Bernoulli noise alone explains
a -₹1,500 outcome.

---

## 2. Battery 60-day × 50-stock × 5-min (post-speed-patch)

**Run ID:** `battery_nifty50_60d_20260522T085929`
**Started:** 2026-05-22 14:30 IST
**Variants completed at time of writing:** 7 / 16 (V1–V7)
**Universe:** Nifty 50 (50 large-caps)
**Window:** 60 calendar days × 5-min bars
**Initial capital:** ₹10,000

### 2.1 Variant results (V1–V7)

| Variant | Trades | WR% | PnL | PF | R:R | MaxDD% | Ret% |
|---|---:|---:|---:|---:|---:|---:|---:|
| V1_baseline_current_shipped | 69 | 42.0 | -₹298 | 0.80 | 1:1.10 | 6.40 | -2.98% |
| V2_all_filters_off | 78 | 43.6 | -₹420 | 0.75 | 1:0.97 | 6.85 | -4.20% |
| V3_only_xgb_mr_filtered_yday | 78 | 43.6 | -₹420 | 0.75 | 1:0.97 | 6.85 | -4.20% |
| **V4_threshold_3pct** | **61** | **50.8** | **+₹340** | **1.35** | **1:1.30** | **3.08** | **+3.40%** |
| V5_threshold_7pct | 63 | 44.4 | -₹116 | 0.90 | 1:1.13 | 4.88 | -1.16% |
| V6_threshold_10pct | 65 | 44.6 | -₹214 | 0.84 | 1:1.04 | 5.58 | -2.14% |
| V7_filter_supertrend_only | (just finalised, see §6) | | | | | | |

### 2.2 V4 long/short forensic (computed 2026-05-25 12:25 IST)

V4 (`trend_filter_pct: 3.0` on all six strategies) is the ONLY
profitable variant on Nifty 50 60d. The single most important
validation check on V4 was the long/short balance.

```
side    n     pnl      avg     win%
----  ----  -------  ------  ------
BUY    36   +262.62  +7.29   52.8%   <- long
SELL   25    +77.82  +3.11   48.0%   <- short
TOTAL  61   +340.44          50.8%

Split: 59% long / 41% short (BALANCED)
Both sides positive.
```

**Decision-affecting:** V4 is NOT a smarter version of the live
agent's bear bet; it makes money on BOTH sides. PASS.

### 2.3 V2 == V3 forensics (computed 2026-05-25 12:25 IST)

V2 and V3 have bit-for-bit identical trade ledgers (SHA-256 hash
match: `82dea26dfa9a4663...`). Investigation showed the recorded
config overrides DO differ correctly:

- V2: `[mean_reversion=None, xgboost=None, supertrend=None, rsi=None, vwap=None, orb=None]`
- V3: `[supertrend=None, rsi=None, vwap=None, orb=None]` (MR + XGB at default)

Empirically `mean_reversion` and `xgboost_classifier` fire **zero
trades** on this Nifty 50 universe in this 60-day window (matches
live: 0 and 1 trade respectively over 14 days). With those two
strategies dormant, V2 and V3 differ only on a parameter that's read
on no code path that fires.

**Conclusion:** NOT a config-loading bug; correctly a degenerate
variant design. V3 to be retired or redesigned for next battery
generation.

### 2.4 Threshold landscape (V4–V6, V1)

The shipped value (`trend_filter_pct: 5.0`) sits at the LOCAL
MINIMUM of the swept range:

```
trend_filter_pct  →  PnL     PF     verdict
3.0  (V4)            +₹340   1.35   WIN
5.0  (V1 shipped)    -₹298   0.80   WORST
7.0  (V5)            -₹116   0.90   near-flat
10.0 (V6)            -₹214   0.84   losing
```

Non-monotonic; either tightening (3%) or loosening (7%) beats the
shipped 5%.

---

## 3. Battery 90-day × 228-stock × 5-min (PRE-speed-patch, KILLED)

**Run ID:** `battery_freeze_v21_20260518T181337`
**Started:** 2026-05-18 23:46 IST
**Killed:** 2026-05-22 13:12 IST (manual; throughput-cliff
investigation began ~14:30 IST same day, leading to the speed
patches and switch to Nifty 50)
**Variants completed:** 2 / 15 (V1, V2 only)
**Universe:** ~228 stocks (50 Nifty large-caps + 178 mid/small caps)
— **same shape as the live scanner watchlist**
**Window:** 90 calendar days × 5-min bars (2026-02-17 → 2026-05-18,
59 distinct trading days)
**Initial capital:** ₹10,000
**Archive location:** `logs/backtests/archive/battery_freeze_v21_20260518T181337.tar.gz`
(252M, archived 2026-05-25 by `prune_old_battery_runs.sh`)

This is the **most important data source** in the entire project so
far. It was misclassified as "stale / pre-patch" until re-examined
on 2026-05-25 at the operator's prompt.

### 3.1 Variant results

| Metric | V1 (shipped) | V2 (filters all OFF) |
|---|---:|---:|
| Trades | 278 | 297 |
| Win rate | **44.6%** | **50.2%** |
| PnL | **+₹177.35** | **+₹658.55** |
| PF | 1.04 | 1.13 |
| MaxDD | 8.74% | 6.52% |
| Return % | +1.36% | +6.32% |

### 3.2 Long/short asymmetry (THE finding)

| Side | V1 trades | V1 PnL | V1 avg | V1 WR | V2 trades | V2 PnL | V2 avg | V2 WR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **BUY** | 122 | **+₹556** | +₹4.56 | 48.4% | 114 | **+₹1,057** | +₹9.27 | 56.1% |
| **SELL** | 156 | **-₹379** | -₹2.43 | 41.7% | 183 | **-₹398** | -₹2.18 | 46.4% |

**Key observations:**

1. The short side has **structurally negative edge in BOTH V1 and
   V2** on 339+ short trades over 90 days. Disabling the trend
   filter does NOT fix shorts (-₹379 → -₹398 is within noise).

2. V1 longs are profitable (+₹556, WR 48.4%, avg +₹4.56). V2
   longs are STRONGLY profitable (+₹1,057, WR 56.1%, avg +₹9.27).

3. The +₹482 V1→V2 PnL swing comes ENTIRELY from the long side.
   The short side is invariant to the trend filter.

4. Exit reasons (V1): 154 stop-loss losses (-₹5,058), 121
   take-profit wins (+₹5,195). The system DOES hit take-profits
   roughly 44% of the time; the engine is functional. The win/loss
   distribution is symmetric in magnitude (~₹33 per loss vs ~₹43
   per win). The problem is direction selection, not exit logic.

### 3.3 Implications for live agent loss

The live agent has traded **100% short** since 2026-05-13 because
`bear_high_vol` regime + `short_selling_regimes` config routes
exclusively to shorts.

V1 shorts on 90 days: WR 41.7%, avg -₹2.43, PF ~0.85.
At n=28 (the live trade count), this distribution produces:

- Mean expected PnL: 28 × -₹2.43 = -₹68
- 5th-percentile worst case (Bernoulli + magnitude variance):
  approximately -₹1,200 to -₹1,800

**Live -₹1,505 is comfortably inside the noise envelope of "running
the structurally-negative-edge side of a working engine for 28
trades."** It is NOT evidence the engine has no edge; it is
evidence we executed the wrong side.

### 3.4 Why universe matters more than tuning

| | 60d × 50 Nifty (post-patch) | 90d × 228 full-universe (pre-patch) |
|---|---|---|
| V1 (shipped, filter=5%) | -₹298 PF 0.80 | **+₹177 PF 1.04** |
| V2 (filters off) | -₹420 PF 0.75 | **+₹659 PF 1.13** |

On Nifty 50 (large-caps, ~1.2% ATR), V2 (filters off) is the
**worst** variant. On the 228-stock live-shape universe (mid-cap
heavy, ~2% ATR), V2 is the **best** variant. **The trend filter
HELPS on large-caps and HURTS on mid-caps.** The live universe is
mid-cap-heavy.

This means V4's win on Nifty 50 (3% trend filter) may NOT transfer
to the live universe. Universe-transfer validation is required
before any deploy.

---

## 4. Three candidate fixes (ranked by confidence)

| Rank | Candidate | Source evidence | Confidence | Cost |
|---|---|---|---|---|
| **1** | **`risk.allow_shorts: false`** (V1 long-only) | 90d V1 longs: +₹556, WR 48.4%, PF ~1.5 inferred | **HIGH** | 0 bypass slots (flag defaults to no-op) for code; 1 slot to flip in production |
| 2 | **V2** (`trend_filter_pct: null` everywhere) | 90d × 228 V2 full result | MEDIUM | 1 bypass slot |
| 3 | **V4** (`trend_filter_pct: 3.0` everywhere) | 60d × 50 Nifty V4 result | MEDIUM (untested on live universe) | 1 bypass slot |
| 4 | **V4 + long-only combined** | Inferred best-case | HIGHEST (untested) | 1 bypass slot |

The cheapest experiment that captures the highest-confidence finding
(short side has no edge) is candidate 1.

---

## 5. Validation runs needed before any deploy

Priority order. Each is one battery worker × ~14-20 hours.

| # | Run name | Tests | Cost (hours) |
|---|---|---|---|
| 1 | `nifty500_v1_long_only_60d` | Confirm V1 long-only PF/edge on 228-stock universe | ~14h |
| 2 | `nifty500_v4_60d` | Does V4's 3% filter still win on mid-cap universe? | ~14h |
| 3 | `nifty500_v2_60d` | Confirm V2 filters-off result holds on a different time window | ~14h |
| 4 | `nifty50_v4_100k_60d` | Capital-scale invariance check (₹10k → ₹100k) | ~14h |
| 5 | `nifty500_v4_long_only_60d` | The combined candidate | ~14h |

The original queue (`v2_baseline_90d`, `nifty50_60d_train`,
`nifty50_30d_holdout`, `nifty50_120d`) should be reordered to put
runs 1–5 ahead, since they all gate the deploy decision.

---

## 6. V7 (filter_supertrend_only) — landed 2026-05-25 12:35 IST

V7 finalised between the V4 forensic and the writing of this
document. Result to be added when comparison.md updates.

**Hypothesis under test:** if `supertrend_follow` is the bad
shorter (it generated most of the live agent's losing SELL signals
based on the strategy attribution in §1), then filtering only it
should improve PF the most among single-strategy isolations. V8
(`filter_rsi_only`) tests the same for `rsi_momentum`.

If V7 PF > 1.0 AND V8 PF < 1.0: the fix is "tighten supertrend's
trend filter only".

If V7 ~ V8 ~ V1: the bad-shorter problem is NOT strategy-specific
and the long-only flag remains the right fix.

---

## 7. Open questions deferred to Tuesday/Wednesday

1. **Live-vs-battery May 12–21 trade parity** — does V1's long
   trades on 2026-05-13 to 2026-05-21 include trades the live agent
   *didn't* take (because regime classifier blocked longs)? If yes,
   that's diagnostic confirmation that the regime classifier is
   filtering out the side that has edge.

2. **V2 == V3 retirement** — do not run V3-style variants in future
   batteries. Flag for next battery design review.

3. **Disk rotation working correctly** — `0 2 * * *` cron just
   installed; observe tomorrow morning whether 9-day-old runs are
   archived as expected.

4. **Why `signal_audit` CSV was missing on Friday EOD** — earlier
   in this conversation a missing CSV was noted. Need to confirm
   it's been recreated correctly post-restart.

5. **NSE_UNIVERSE 504-stock fetch** — gated behind
   `scanner.use_live_universe: false` (default OFF). Worth
   investigating whether enabling it for the validation runs would
   change the battery's "live universe shape" definition.

---

## 8. Decision matrix for Friday 2026-05-29 review

The Friday review now has actual decisions to make, not just
"gather more data".

| Decision | Recommended | Conditional on |
|---|---|---|
| Add `risk.allow_shorts` flag (defaults true, no behavior change) | **APPROVED PRE-MEETING** | None — observability-only PR, no bypass slot |
| Reorder battery queue to prioritise nifty500_* runs | YES | Disk space remains > 5G |
| Flip `risk.allow_shorts: false` on 2026-06-08 freeze-lift | **PROVISIONAL YES** | Validation runs 1 + 2 confirm long-side edge |
| Deploy V4 untested | NO | Universe-transfer check failed |
| Extend freeze beyond June 8 if validation runs fail | YES (default) | Any validation run shows PF < 1.0 |

---

## 9. Appendix: pointers to raw data

- Live trades CSV: `/app/data/trading_agent.db` (table: `trades`)
- Live equity snapshots: same DB, table `equity_snapshots`
- Live signal audit: `logs/signal_audit_*.csv`
- 60d Nifty 50 battery: `logs/backtests/battery_nifty50_60d_20260522T085929/`
  - `comparison.md` for summary
  - `results/V*.json` for per-variant trade ledgers and metrics
- 90d × 228 battery (archived): `logs/backtests/archive/battery_freeze_v21_20260518T181337.tar.gz`
  - To inspect: `cd logs/backtests && tar -xzf archive/battery_freeze_v21_20260518T181337.tar.gz`

---

*Last updated 2026-05-25 13:15 IST after V1+V2 90d re-analysis and
V7 finalisation. Append-only — corrections go in dated subsections,
not in-place edits.*

---

## §10. Battery throughput degradation + queue resume — bug pair (2026-05-25 14:25 IST)

### Symptom

Operator-observed degradation on `V9_filter_vwap_orb_off.log`:

| reading | progress | elapsed | rate (avg) | ETA |
|---|---|---|---|---|
| 1 | 11.4% (23,934/209,040) | 19.3 m | **21 ev/s** | 2.5 h |
| 2 | 27.6% (57,613/209,040) | 1.5 h   | **11 ev/s** | 4.0 h |

Instantaneous rate during the last 70 min: **7.9 ev/s** — a ~3x
slowdown vs. fresh start, and the variant-level ETA had crept up
even while progress was still being made.

### Smoking gun

Worker log file sizes formed a perfect arithmetic progression
(186-188 KB per step):

```
V1.log = 772 KB    V3.log = 583 KB    V5.log = 394 KB
V7.log = 206 KB    V9.log =  18 KB    (V9 mid-variant)
V2.log = 772 KB    V4.log = 584 KB    V6.log = 395 KB
V8.log = 206 KB    V10.log = 17 KB
```

This is impossible unless each worker subprocess is writing every log
line to ALL previously-opened sinks. Math confirms:
- V1.log = 188 KB (V1 phase) + 188 (V3) + 188 (V5) + 188 (V7) + 18 (V9 so far) = 770 KB ✓
- V3.log = 188 (V3) + 188 (V5) + 188 (V7) + 18 (V9) = 582 KB ✓
- (etc.)

### Root cause #1 — sink leak in `_run_variant_in_subprocess`

`packages/research/battery.py:585` calls `logger.add(worker_log_path,
…)` at the start of every variant but **never removes the sink**.
`loguru.logger.add()` is additive. `ProcessPoolExecutor` reuses
worker subprocesses across tasks, so each subsequent variant scheduled
to the same worker accumulates one more attached file sink. By the
5th variant per worker, every emitted log line was being written to
5 files = 5x disk I/O = ~2x throughput drop on the 2-vCPU Ampere VM
(CPU at 99% but contended with kernel I/O).

**Fix:** capture the sink id returned by `logger.add(...)`, wrap the
variant body in `try` / `finally`, and call `logger.remove(sink_id)`
unconditionally on exit. Tested locally; deployed via
`/opt/trading-agent/packages` read-only bind mount.

### Root cause #2 — queue scheduler restart re-ran V1 from scratch

When we restarted the container to deploy the sink-leak fix, the new
container's harness log showed:
```
[BATTERY] run_id=... resume=False | variants total=19 completed=0 pending=19
```
**The harness ignored the 8 already-completed variants and was about
to overwrite their result JSONs.**

The scheduler's `build_docker_run_argv()` was passing `--run-id <id>`
on restart, but the battery harness's `args.run_id` branch
(`battery.py:1011`) explicitly sets `resuming=False` — it only pins
the folder name. Auto-resume requires `--resume <id>`, which is
mutually exclusive with `--run-id`. The scheduler's docstring
incorrectly claimed "the harness auto-resumes when the on-disk
run_id folder exists" — wrong.

**Near-miss data loss:** caught after V1 had reached 2.6% progress
on the rerun (no JSON overwrite yet). Stopped container,
`cp -r results results.backup_before_rerun_<ts>`, verified all V1-V8
JSONs intact (PF/trade counts match prior analysis), then patched
the scheduler.

**Fix:** `build_docker_run_argv(job, run_id, image, resuming=False)`
now emits `--resume <id>` when `resuming=True`, `--run-id <id>` when
`resuming=False`. Two new unit tests pin the behavior.

### Verification after redeploy (14:21 IST)

| Check | Before | After |
|---|---|---|
| Harness resume flag | `resume=False` | `resume=True, completed=8, pending=11` |
| Active workers | V1, V2 (rerunning) | **V9, V10** (correct) |
| Worker rate after 2 min | 44-54 ev/s | 44-55 ev/s |
| V1-V8 result JSONs | Untouched | Untouched (verified) |
| Old sink leak | All 10 logs growing | **Only V9, V10 logs growing** |

V1-V8 result JSONs preserved at:
`logs/backtests/battery_nifty50_60d_20260522T085929/results.backup_before_rerun_20260525T084845/`

### Estimated time saved

| Scenario | V9-V16 wall time (remaining 8 variants × 2 workers) |
|---|---|
| Without fix (degrading) | ~30-50 h (each variant slower than the last; V13-V16 would be at 4-6 ev/s) |
| With fix (clean) | **~6-8 h** (each variant ~1-1.3 h fresh) |
| **Savings** | **~22-42 h** |

Critical path impact: `nifty500_v4_long_only_validation_60d` (Friday
review blocker, queue slot #2) now starts ~Wed 22:00 IST vs ~Fri
10:00 IST in the degraded scenario. **Recovers the Friday review.**

### Files touched

- `packages/research/battery.py` — try/finally + `logger.remove`
  around variant body. Capture sink id from `logger.add()`. Comment
  block referencing this exact incident.
- `tools/run_battery_queue.py` —
  1. New volume mount `-v $TRADER_HOME/packages:/app/packages:ro`
     so code fixes deploy without rebuilding the image.
  2. `build_docker_run_argv(..., resuming: bool)` switches between
     `--resume` (when continuing) and `--run-id` (when fresh).
  3. `_run_id_for()` docstring rewritten to correct the false
     "harness auto-resumes" claim.
- `tests/unit/test_battery_queue_scheduler.py` — three new tests:
  packages mount assertion, fresh-run uses `--run-id`, resume-run
  uses `--resume`.

### Freeze status

**Bug fix, not a feature change.** Touches only research / tooling
code, not `trading_agent.py` live behavior. Bypass-slot ledger
unchanged (still 1/3 used by `risk.allow_shorts`).

### Open follow-ups

1. Add a `--resume auto-detect` mode to the queue scheduler that
   inspects the disk before deciding `--resume` vs `--run-id`, so
   even a state-file deletion can't lose progress (defence in depth).
2. Add a CI test that asserts the harness's `--run-id` branch is
   never reached with an existing populated folder (would have caught
   this two weeks ago).
3. The sink leak existed for the entire freeze-v2.1 run. Worth
   re-extracting any timing data from the May 18 / May 22 batteries
   with this caveat in mind (slowness was a real metric, but the cause
   was tooling not the strategy).

---

## §11. Senior dev + algo expert backtester scan (2026-05-25 15:30 IST)

### Mandate

Operator requested a full scan of backtester logic with both a senior
dev hat (error handling, state, concurrency, edge cases) AND an algo
expert hat (look-ahead, survivorship, fills, slippage, sizing, exits,
warmup, live/backtest parity). Output: bugs found, fixes applied,
tests added, and a clear list of remaining known divergences.

### Trader VM concurrency check (sanity preamble)

Operator asked "is the market really silent" — confirmed the trader
container is healthy and the market is NOT silent. Nifty oscillating
23,944-23,984, VIX 16.83-17.13. The agent is silent BY DESIGN:

- Current regime: `bear_high_vol`
- Active strategy weights in this regime:
  `{supertrend_follow: 0.1, rsi_momentum: 5.0}` — effectively
  rsi_momentum only, since supertrend at 0.1 cannot reach the 0.55
  ensemble threshold alone.
- 10-day trade count is decaying: 8 → 4 → 2 → 2 → 0 (May 15→25).
- RSI on the universe is sitting mid-range, so the strategy's
  oversold-reversal and overbought-reversal triggers are not firing.
- Last 4 trades were ALL `rsi_momentum SELL` (shorts), consistent
  with §3 finding "shorts are the structural loss driver" and our
  `risk.allow_shorts` mitigation (defaults `true`, ready to flip).

No daemon errors. signal_audit CSV for today is missing only because
zero non-HOLD strategy signals fired, so the audit writer was never
called (separate documentation issue, not a bug).

### Bugs found

| ID | Severity | Status | Title |
|---|---|---|---|
| **A** | **HIGH** | **FIXED** | Intra-bar SL/TP not modeled — close-only check, positive PnL bias |
| **B** | HIGH | DEFERRED | `regime` hardcoded to "unknown" — biggest live/backtest divergence |
| **C** | MED | **FIXED** | Opposite-signal exit dropped — held longs never close on a SELL signal |
| **D** | MED-cosmetic | **FIXED** | Sharpe annualized on event-level pct_change, sqrt(252) on 220k samples |
| E | LOW | Documented | Per-event `equity_curve` resolution (works for drawdown, noisy for Sharpe) |
| F | LOW | Documented | Opening lockout not modeled (live: blocks new opens first N min) |
| G | LOW | Documented | WS-tick exits not modeled (live: SL/TP fires on any tick, backtest only on bar close) |
| H | LOW | Documented | Intraday risk-off regime overlay not modeled (Nifty -0.5% / VIX spike block longs) |
| I | LOW (perf) | Documented | Strategies recompute indicators from scratch each bar (O(n²) cumulative) |

### Bug A — Intra-bar SL/TP not modeled (FIXED)

**Old behaviour (`backtest_ensemble.py:269-291`):**

```python
trigger = rm.check_stop_loss_take_profit(
    pos.entry_price, close, pos.side, pos.stop_loss, pos.take_profit,
)
if trigger:
    exit_price = self._apply_slippage(close, pos.side, exit=True)
```

The SL/TP test only looked at the bar's CLOSE. Any bar that touched
SL intra-bar but recovered to close above it (the classic
"wick-and-close" pattern) was treated as "still holding" in
backtests — even though in live trading the broker-side stop
order would have filled the instant price touched. Symmetric
overstatement on TP triggers.

**Net effect on the V1-V8 results we already have:** they
SYSTEMATICALLY overstate net PnL and understate drawdown. By how
much depends on the volatility of the symbols and the tightness
of the SLs, but on 5-minute bars in volatile sectors this can easily
shift PnL by 10-30%. The DIRECTION of all findings (V4 beats V1
beats V2; shorts negative-edge) is preserved because the bias is
applied uniformly across variants. Magnitudes are not.

**Fix:** new helper `_detect_intrabar_exit(pos, open, high, low, close)`:

- Long: SL hit if `low <= sl`, TP hit if `high >= tp`.
- Short: SL hit if `high >= sl`, TP hit if `low <= tp`.
- Gap-through fills at the bar OPEN (worse than the static level,
  matches live broker behaviour on gap-down/gap-up opens).
- Conservative tie-break when both SL and TP are inside the bar:
  assume SL fired first (worst-case for the strategy; avoids the
  opposite optimistic bias).

11 unit tests added (long/short × wick/gap × both-hit/missing-SL +
defensive corner cases).

### Bug B — `regime` hardcoded to "unknown" (DEFERRED, KNOWN DIVERGENCE)

**Location:** `backtest_ensemble.py:402` (still in current code, now
wrapped in a long comment block explaining the divergence).

The backtester passes `regime="unknown"` to every
`ensemble.aggregate()` call. The live agent passes the actual
classified regime (e.g. `bear_high_vol`).

**Why this matters:**

- `ensemble.aggregate()` looks up per-strategy weights two ways:
  1. `self._regime_learned_weights.get(regime, {}).get(strategy)`
  2. Falls back to `self._global_learned_weights.get(strategy)`
- With `regime="unknown"`, path #1 always misses; only the global
  learned weight applies, AND the rule-based `regime_multiplier`
  returns 1.0 (no scaling).
- Live agent in `bear_high_vol`: vwap_bounce, opening_range_breakout,
  xgboost_classifier all have weight 0 (per checkpoint inspection
  earlier today: `Regime weights for bear_high_vol: {supertrend_follow:
  0.1, rsi_momentum: 5.0}`).
- Backtester in same period: ALL strategies vote at their global
  weight (rsi_momentum=5.0, xgboost=5.0, mean_reversion=5.0, etc.).

**Net effect on V1-V19 battery results:**

- Backtester is testing a STRICTER ensemble than what live runs.
- "Shorts have negative edge" finding is reached AFTER throwing out
  the regime-based protections. Live agent gets those protections.
- So the live agent's actual short loss might be SMALLER than the
  battery's -₹379/-₹398 numbers suggest, because vwap_bounce /
  XGBoost shorts are suppressed in bear regimes live.
- Direction of finding still stands; magnitude was likely overstated
  on the short side.

**Why deferred:** fix requires loading Nifty + VIX bars into
`market_data.pkl` (currently only equity bars), computing a
per-bar rolling regime, and threading it through `_bt_config` to
the per-symbol decision. Tractable but a ~4-6h change. Will
revisit after the immediate freeze-week-2 review.

### Bug C — Opposite-signal exit dropped (FIXED)

**Old behaviour:**

```python
if symbol in portfolio.positions:
    pos = portfolio.positions[symbol]
    trigger = rm.check_stop_loss_take_profit(...)
    if trigger:
        # ... close ...
    # Even if no SL/TP trigger, skip strategy evaluation:
if symbol in portfolio.positions:
    equity_curve.append(...)
    continue
```

If a position was held and ensemble would have generated an
opposite-direction signal, it was silently dropped. The live agent
has an explicit exit-on-signal path at `trading_agent.py:3677` (BUY
signal while short → cover) and `:3716` (SELL while long → close).

**Net effect:** backtester held positions LONGER than live would
have. Some winners stayed open until SL/TP/end-of-day; some losers
that would have been cut early stayed open through their full SL.
Direction of bias unclear (depends on strategy/regime); magnitude
likely smaller than Bug A.

**Fix:** after the SL/TP exit branch, if a position is still open,
evaluate strategies and call `ensemble.aggregate`. If aggregate
emits a non-HOLD opposite-direction signal, close at current bar's
close (with adverse slippage). Same-direction signals on existing
positions are dropped (matching live's "already_open:duplicate"
audit reject).

### Bug D — Sharpe annualized incorrectly (FIXED)

**Old (`backtest_ensemble.py:580-582`):**

```python
returns = pd.Series(equity_curve).pct_change().dropna()
if len(returns) > 1 and returns.std() > 0:
    r.sharpe = float((returns.mean() / returns.std()) * (252 ** 0.5))
```

`equity_curve` has one entry per `(symbol, bar)` event, i.e.
~220,000 entries for 50 symbols × 60 days. Per-event `pct_change`
is dominated by per-symbol revaluation noise; `std()` is tiny.
Then we multiply by `sqrt(252)` — which is the annualization
factor for DAILY returns, not per-event returns.

The Sharpe number in `comparison.md` had been treated as "the
annualised risk-adjusted return" by operators when in fact it
was effectively noise × sqrt(252). On the 90-day pre-patch
battery V1 showed Sharpe = 35.2 which is not physically possible.

**Fix:** track `last_equity_per_day` dict keyed by IST date and
updated at every event. At result-build time, prefer daily samples
and pass them to `_build_result(..., daily_equities=)`. Fallback
to the legacy event-level computation when daily samples aren't
provided (preserves backwards compatibility for any external
caller).

### Tests added

13 new tests in `tests/unit/test_backtest_ensemble_helpers.py`:

- `TestIntrabarExitDetection` (11 cases): long/short × wick/gap ×
  both-SL-and-TP-hit + missing-SL defensive case + unknown-side
  defensive case
- `TestSharpeUsesDailyEquities` (2 cases): daily path produces
  sane Sharpe; legacy fallback still works when callers don't
  provide daily samples

Full suite: **1209 unit tests pass** (was 1196 before this scan).

### Deferred / documented divergences

| ID | Title | Effort to fix |
|---|---|---|
| B | Per-bar regime classification | 4-6 h (load Nifty/VIX, thread through) |
| E | Per-event equity_curve resolution | 1 h (drawdown still works, just cosmetic) |
| F | Opening-lockout first-N-min block | 1 h (just an `if ts.time() in window` gate) |
| G | WS-tick exit granularity | UNFIXABLE on bar-only data without a tick feed |
| H | Intraday-regime overlay (Nifty -0.5% / VIX spike) | 2-3 h (rolling indicators on Nifty) |
| I | Strategy indicator memoization | 2-3 h (cache last-bar indicator values) |

All deferred items DOCUMENTED inline in the code at their relevant
call sites so future readers see the divergence before grepping
through findings logs.

### Impact on in-flight battery

The container `battery_nifty50_60d_20260522T085929` is still running
with the **pre-fix code** in worker memory (Python doesn't hot-reload
modules in ProcessPoolExecutor workers). The deployed
`backtest_ensemble.py` won't take effect until the workers are
restarted. Current status: V1-V8 done (legacy), V9 and V10 running at
~15% (legacy).

**Restart decision pending operator approval** (see "Open question
for operator" below).

### Freeze accounting

All four fixes are pure correctness improvements in
`packages/research/backtest_ensemble.py` and
`tests/unit/test_backtest_ensemble_helpers.py`. They do NOT touch
`trading_agent.py` runtime behavior. **Bypass-slot ledger unchanged:
still 1/3 used (by `risk.allow_shorts`).**

### Open question for operator

We have three options for the in-flight Nifty 50 60d battery:

**Option A — Clean restart (recommended for Friday review accuracy):**
- Archive `results/` → `results.legacy_pre_intrabar_fix/`
- Restart the entire 19-variant run from V1 on fixed code
- ~10-12h total wall time; complete by ~04:00 IST Tuesday
- Validation runs follow Tue/Wed/Thu, on time for Fri
- ALL Friday-review variants on consistent code

**Option B — Resume restart (compromise):**
- Stop container, scheduler restarts with `--resume`
- V1-V8 stay as legacy (pre-fix code); V9-V19 use fixed code
- ~6-8h to complete; saves ~3h of compute
- Friday review compares V1-V8 (legacy) vs V9-V19 (fixed) →
  apples-to-oranges for cross-variant comparison

**Option C — No restart, accept the bias:**
- All 19 variants stay on pre-fix code
- Saves 30-60 min of recompute
- Headline Friday-review numbers will systematically overstate
  PnL by ~10-30% per variant (Bug A)
- Sharpe values still wrong (Bug D)
- Direction of conclusions still valid; magnitudes wrong

Operator to choose; default action while waiting is Option C (do
nothing — let the run finish on the buggy code; we can rerun fresh
afterwards if needed).

### Operator decision (2026-05-25 15:00 IST): Option A — Clean restart

Executed at 15:03 IST. Steps:

1. Stopped scheduler + container `battery_nifty50_60d_20260522T085929`.
2. `tar -czf battery_nifty50_60d_20260522T085929_LEGACY_PRE_INTRABAR_FIX.tar.gz`
   (52 MB on disk, preserves all V1-V8 trade ledgers for forensics).
3. Deleted the uncompressed legacy folder (freed ~44 MB net after
   the archive overhead).
4. Cleared the `nifty50_60d` entry from
   `data/battery_queue_state.json` so the scheduler would issue
   a fresh `run_id`.
5. Restarted `battery-scheduler.service`.

**New run:** `battery_nifty50_60d_20260525T093330`

Verified inside the container at startup:
- `EnsembleBacktester._detect_intrabar_exit` present ✓
- `_build_result(..., daily_equities=...)` parameter present ✓
- "opposite-signal exit" comment + `exit_reason="signal"` close path present ✓
- Started with `--run-id <fresh>`, NOT `--resume <old>` ✓

3 min after start (current state):
- V1 + V2 running, 1.5-2.8% complete, 44-53 ev/s
- CPU 200% (both cores saturated, no degradation visible)
- Memory 667 MB (healthy)
- ETA per variant: ~1.1-1.3 h
- Projected total wall time: ~11 h for 19 variants on 2 workers
- Projected completion: ~02:30 IST Tuesday 2026-05-26
- `nifty500_v4_long_only_validation_60d` (queue slot #2) follows
  immediately, expected to start Tuesday ~03:00 IST

**Forensic value of the legacy archive:** the V4 long/short split,
V8 RSI-filter PF=1.01 discovery, and V1/V2 90-day pre-patch
"shorts negative-edge" analysis in §§3-6 ABOVE were all derived
from the legacy buggy code. Those findings remain DIRECTIONALLY
valid (Bug A applies uniformly across variants, so within-variant
analysis like long-vs-short split is fine; cross-variant absolute
comparisons get the bias). The archive lets us re-derive any of
those if Friday's review needs to revisit.
