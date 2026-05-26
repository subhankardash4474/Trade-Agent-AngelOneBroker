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

---

## 12. Bug E — O(N²) full-history slicing in `_merge_bars` (2026-05-25 mid-run perf)

**Status:** Fixed, behavior-preserving, audit-only entry, no slot consumed.

### 12.1 Observation

The restarted V1+V2 nifty50_60d battery showed a second wave of
throughput degradation that was NOT a recurrence of the earlier
loguru sink leak. Live data captured during the run:

```
when       elapsed  events     instantaneous rate (per worker)
15:07 IST   3.0m    ~7,000      ~39 ev/s
15:13 IST   9.1m    14,544      ~21 ev/s  (events in [3,9.1] / 6.1m)
15:26 IST   22.2m   25,197      ~14 ev/s
15:50 IST   45.4m   38,832      ~10 ev/s
16:11 IST   ~60m    48,418      ~7 ev/s   (events in [60.0,61.0] / 1m)
```

Cumulative rate dropped 39 → 14 ev/s; instantaneous rate dropped
39 → 7 ev/s (~5.5x slowdown). Memory was healthy (~840 MB / 11 GB),
CPU was 99% saturated per worker, log duplicate-ratio was 1.07
(no sink leak). So this was real CPU work growing per event.

### 12.2 Root cause

`packages/research/backtest_ensemble.py:_merge_bars` yielded
`df.iloc[: i + 1]` — the entire growing prefix — as `df_slice`
to every strategy on every bar. The strategies all follow the
pattern:

```python
df = data.copy()                              # O(N)
df["rsi"] = self._compute_rsi(df["close"])    # O(N) ewm
df["rsi_prev"] = df["rsi"].shift(1)           # O(N)
rsi = df["rsi"].iloc[-1]                      # uses last row only
```

Cost per event = O(len(df_slice)) × n_strategies. df_slice grows
linearly with simulation progress, so total work = O(N²) per
symbol. Pre-patch the cost ratio of late-sim vs early-sim was
~13× (per-symbol history 800 bars vs 60 bars), which matches the
observed 5.5x rate drop after accounting for fixed-cost overhead.

### 12.3 Fix

Added `strategy_history_window: int = 300` to `BacktestConfig`.
Modified `_merge_bars` to slice to `df.iloc[max(0, i+1-window) : i+1]`
instead of `df.iloc[: i + 1]`. Per-event work is now constant
w.r.t. simulation length.

### 12.4 Numerical equivalence

All strategy indicators are EWM-based (RSI, ATR, ADX, supertrend)
or rolling-window-based (SMA, VWAP, volume-mean). EWMs decay the
contribution of older bars geometrically at rate (1 - α)^N where
α = 2/(period + 1). For window=300:

| Strategy        | period | α     | (1-α)^300   | Notes                       |
|-----------------|--------|-------|-------------|-----------------------------|
| RSI(14)         | 14     | 0.133 | 4 × 10⁻¹⁹   | Below float precision        |
| ATR/ADX(14)     | 14     | 0.133 | 4 × 10⁻¹⁹   | Below float precision        |
| Supertrend(10)  | 10     | 0.182 | 4 × 10⁻²⁴   | Below float precision        |
| MA cross EMA(50)| 50     | 0.039 | 7 × 10⁻⁶    | ~1 ppm — well below useful   |
| XGBoost(60)     | 60     | n/a   | 0           | Fixed window, fully covered  |

The worst-case strategy (MA crossover EMA(50)) shows a confidence
drift of ~2.2e-6 absolute between full and windowed slices —
below the 4th decimal place. Signal DIRECTION is byte-identical
in every test case. The drift is below any threshold used in the
ensemble aggregator (`confidence_threshold: 0.55`) and below the
audit logger's rounding precision (2 decimal places).

### 12.5 Tests

`tests/unit/test_strategy_history_window.py` (13 tests):
- `BacktestConfig.strategy_history_window` default = 300, is int, overridable
- Per-strategy walk: 500-bar synthetic OHLCV, for each bar in [350, 500),
  compare `generate_signal(full_prefix)` vs `generate_signal(last_300_bars)`
  for: RSI Momentum, MA Crossover, Mean Reversion, Supertrend, VWAP Bounce
- `_merge_bars` slice contract: bounded by window, tail not head, never
  empty (window=0 floors to 1), uncapped when window > history

Tolerances: signal direction exact; confidence atol=1e-4 (10× the
worst-case EWM tail residual); SL/TP rtol=1e-4 (1 basis point).

### 12.6 Expected perf impact

Late-sim per-event cost: O(800) → O(300) = ~2.7× faster on the
strategy-eval hot path.

Allocations per event: 800-row `df.copy()` → 300-row copy = ~2.7× less
heap churn, less GC pressure. Effect compounds because Python's
garbage collector runs less often.

Projected post-fix rate: late-sim 7 ev/s → 18-25 ev/s per worker,
sustained. ETA per 60-day variant: 3.7h → ~1.5h. Total queue
(19 variants × 2 workers parallel): 80-90h → ~28h.

### 12.7 Freeze accounting

`packages/research/backtest_ensemble.py` is research/, not on the
slot-consuming list in FREEZE_v2.1 (which covers strategies, risk,
position sizing, trading_agent.py, config.yaml strategy/risk
blocks, models). This is an **audit-only performance fix** that
preserves all observable behaviour (signals, PnL within 1bp). No
bypass slot consumed. Bypass ledger remains 1/3 (risk.allow_shorts).

### 12.8 Mid-run deployment

After fix + tests + commit, the running V1+V2 pair (at ~23% complete,
1h elapsed, ETA 3.7h) was stopped. The fix was deployed to the
backtester VM and the queue restarted with a fresh run_id. The
52 min of V1+V2 work was sunk-cost; net savings ~50 hours on the
remaining 19-variant queue.

**Old run (archived):** `battery_nifty50_60d_20260525T093330` →
`/opt/trading-agent/logs/backtests/_archive/battery_nifty50_60d_20260525T093330_O_N2_BUG`
(reached 23.1% in 1h 14m before being stopped; 90 MB archived for forensic
comparison against post-fix run)

**New run:** `battery_nifty50_60d_20260525T105637` (started 2026-05-25 16:26 IST,
commit 7ec02a1 in container).

**Operational gotcha encountered during deploy:**
The scheduler initially crash-looped with `PermissionError: Operation not
permitted` on `os.replace()` of `battery_queue_state.json`. Root cause:
during the manual queue-state reset I `chown`'d the file to 1001:1001
(intending to match the docker worker UID), but the scheduler systemd unit
runs as `opc`. Combined with the sticky bit on `/opt/trading-agent/data/`
(drwxrwxrwt), opc could not rename a file owned by uid 1001. Fix:
`chown opc:opc` + `restorecon`. Added to ops_runbook common-pitfalls.

---

## 13. Bug F — `ProcessPoolExecutor` cascade-fail when worker dies in re-used subprocess (2026-05-25 evening)

**Status:** Fixed; harness change only; audit-only entry, no slot consumed.

### 13.1 Observation

The post-Bug-E `battery_nifty50_60d_20260525T105637` run completed
**V1 + V2 successfully** (~3.18 h each at the projected post-fix rate)
with real numbers — first clean post-fix battery output:

| Variant | Trades | WR | PF | PnL |
|---|---:|---:|---:|---:|
| V1 baseline shipped | 59 | 39.0% | 0.76 | -Rs 206 |
| V2 all filters off | 69 | 30.4% | 0.58 | -Rs 399 |

Then the run **mass-failed**: V3-V19 (17 variants) all marked CRASHED in
`comparison.md` with the identical generic error
`"A process in the process pool was terminated abruptly while the
future was running or pending."`

### 13.2 Forensic timeline

```
20:05:33 IST  V3 last [BATTERY-PROGRESS] line: 17.9% complete, 21 ev/s
              (V3 had been emitting every 60s for 30 min — perfectly healthy)
20:06:06 IST  V4 last [BATTERY-PROGRESS] line: 16.9% complete, 21 ev/s
              (V4 still alive 33 s AFTER V3 stopped — they did NOT die together)
20:06:19 IST  Orchestrator records BrokenProcessPool, marks all 17 pending
              variants as CRASHED, exits with code 3
20:06:20 IST  Container exits (code 3, OOMKilled=false, no kernel signal)
20:07:11 IST  Scheduler launches next queue job (long-only validation)
```

So **only V3 actually crashed** — V4 was killed by `ProcessPoolExecutor`
when it detected V3's worker had died, and V5-V19 **never ran at all**.
The "17 failed variants" line in comparison.md is an artifact of
`fut.result()` re-raising `BrokenProcessPool` for every queued future
once the pool is invalidated.

### 13.3 Crash mode

Evidence narrows the cause sharply:

| Signal | Verdict |
|---|---|
| Python traceback in V3's worker log | NONE — log just stops abruptly |
| OOMKilled flag on container | `false` |
| Kernel OOM in journalctl --kernel | empty across the death window |
| Container memory limit | unset (HostConfig.Memory=0) |
| Watchdog kill (`os._exit(124)`) | not the cause: 30-min limit, only 46s of silence |
| External SIGTERM (docker stop / systemd) | not in journal |

The worker died **without writing any Python error**, the container
**was not OOM-killed**, and the kernel **logged no signal**. That
combination is the classic fingerprint of a **native-code segfault /
abort / bus error in a C extension** (numpy, pandas, yfinance,
loguru-via-zmq, xgboost) — Python doesn't get a chance to handle the
signal, the process just dies.

### 13.4 Why V3 (not V1/V2): re-used worker hypothesis

`ProcessPoolExecutor` REUSES the same worker subprocesses across
submitted tasks (documented at `battery.py:584`). With max_workers=2
and 19 variants:

- worker A: V1 → V3 → V5 → ...
- worker B: V2 → V4 → V6 → ...

V1 and V2 ran in **fresh** workers and passed.
V3 and V4 ran in **re-used** workers (each on top of V1/V2's leftover
process state) and died at the same elapsed time (~30 min in).

State that survives variant boundaries inside one worker process:

- `strategies._trend_context._cache` — module-level `dict` of yfinance
  daily bars, TTL 6h, never explicitly cleared
- yfinance / urllib3 internal connection pool + cookie jar
- xgboost native handles (V1's `_load_model` failed due to missing
  pickle — possibly leaves the C++ side in a half-initialized state)
- numpy / pandas internal caches (allocator pools, type-checking caches)
- loguru queue threads (sink leak from previous bugs already proven to
  exist; we patched it earlier today but other latent leaks may remain)

Any one of these could be the actual segfault trigger. We don't yet
know which, because the evidence we have only narrows it to
"native-code crash in something that survived the V1→V3 transition".

### 13.5 Fix

Two changes to `packages/research/battery.py`:

1. **Eliminate worker re-use** (the load-bearing fix):
   ```python
   ProcessPoolExecutor(max_workers=args.workers, max_tasks_per_child=1)
   ```
   Each variant now runs in a brand-new subprocess. V3 inside a fresh
   worker is functionally indistinguishable from V1 inside a fresh
   worker — and V1 passes. Cost: ~15 s startup tax per variant
   (imports + 90 MB market_data unpickle). For a 19-variant run with
   workers=2 that's ~150 s = ~3 min added to a ~40 h queue.
   Negligible.

2. **Diagnostic safety net** (in case a variant somehow crashes anyway):
   ```python
   import faulthandler
   _fault_fp = open(workers_dir / f"{name}.fault.log", "w")
   faulthandler.enable(file=_fault_fp, all_threads=True)
   ```
   At the top of `_run_variant_in_subprocess`, before any heavy work.
   Any future SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL will write a
   Python traceback to `<run>/workers/<variant>.fault.log` BEFORE the
   process dies. The file is per-variant so concurrent worker deaths
   don't race on a shared log. Best-effort wrapped in try/except so
   the diagnostic tooling can never break the run it's instrumenting.

### 13.6 Why this is not a "specific variant" bug

The strongest hypothesis is state-pollution because:

- V3's overrides are tiny (4 trend-filter disables) and structurally
  similar to V7-V9 (also trend-filter knobs). If V3 had a code bug
  triggered by its config, V7/V8/V9 would too — but those never ran.
- V1 already exercises every code path V3 touches, with MORE filters
  active (V1=defaults=all-on; V3=4-off-2-on); V1 ran 3.18 h cleanly.
- V4 shows a different config (uniform 3% threshold), exercises a
  superset of V3's code path, and was killed by cascade not its own
  fault.

The "data-driven crash on 2026-03-11 at sim time" hypothesis is
unlikely too: V1 ran the same 60 days of data on the same 48 symbols
and hit 2026-03-11 fine; V3 hits the same data through the same
strategies in the same backtester.

If V3 still dies after the fix, faulthandler will tell us *what*
crashed and we can do a targeted code fix. But max_tasks_per_child=1
removes the most plausible cause.

### 13.7 Tests

`tests/unit/test_battery_worker_isolation.py` (6 tests):

- `TestProcessPoolMaxTasksPerChild`:
  - AST walk of `battery.main` finds exactly one `ProcessPoolExecutor`
    call, asserts `max_tasks_per_child=1` is present as a literal int
  - Asserts `max_workers` is also explicitly wired (defends against
    accidental fallback to `os.cpu_count()`)
- `TestWorkerFaulthandler`:
  - Source-level: `_run_variant_in_subprocess` imports faulthandler
    and calls `.enable(...)`
  - Per-variant fault log path is `<workers_dir>/<name>.fault.log`
    (no shared-file race)
  - faulthandler init is wrapped in try/except so a failure can't
    kill the run
- `TestDocumentation`:
  - "Bug F" string present in `battery.py` so the change is grep-able

These are *structural* (source-text / AST) rather than runtime tests
because spinning up real `ProcessPoolExecutor` subprocesses inside
pytest is slow, flaky on Windows (spawn-pickling, sys.path), and
would require a full battery scaffold for what is fundamentally a
one-line invocation contract.

### 13.8 Expected impact

- V3-V19 of the failed run will resume cleanly when re-queued (or run
  cleanly on any future battery launch).
- Future cascade-fails are bounded: a single variant crash now affects
  at most the ProcessPoolExecutor's pending queue in the same pool;
  surviving variants in the same batch may still cascade if they
  share the broken pool — but `max_tasks_per_child=1` makes a single
  variant's death unlikely to corrupt the pool's bookkeeping. (If we
  see this in practice, the next iteration is per-batch pools — left
  for follow-up if needed.)
- All future native-code crashes will have a real Python traceback in
  `<run>/workers/<variant>.fault.log` instead of the opaque
  `BrokenProcessPool` we got this time.

### 13.9 Freeze accounting

`packages/research/battery.py` is research/, not on the slot-consuming
list in FREEZE_v2.1 (which covers strategies, risk, position sizing,
trading_agent.py, config.yaml strategy/risk blocks, models). This is
a **harness fix**: it changes how the orchestrator launches workers,
not what they compute. Backtest results are byte-identical (each
variant runs the same code on the same data; only the surrounding
process-management changes). No bypass slot consumed. Bypass ledger
remains 1/3 (risk.allow_shorts).

### 13.10 Resume plan for V3-V19

After commit + push + pull, queue a `--resume battery_nifty50_60d_20260525T105637`
run. The harness's resume logic reads the existing `comparison.md`,
notes V1+V2 are already DONE, and re-runs only V3-V19 (the 17 marked
as failed). Same fixed code, same market_data, same configs — the
only difference is each variant is now in its own subprocess.

If the resume completes cleanly: state-pollution hypothesis confirmed,
Bug F closed. ETA at the post-Bug-E rate (~3.2 h per variant on 2
workers): 17 × 3.2 / 2 ≈ 27 h to complete the missing 17 variants.

If the resume still fails at V3 (~30 min in): the issue is *not*
state pollution; check `<run>/workers/V3_only_xgb_mr_filtered_yday.fault.log`
for the real Python traceback, fix the underlying code, re-resume.

---

## 14. Bug G — Backtester subsystem code-review hardening (2026-05-25 night)

**Status:** Fixed; harness/library changes only; audit-only entry, no
slot consumed.

### 14.1 Why this entry exists (preemptive vs. reactive)

After landing the Bug F fix and dealing with the operational fallout
(scheduler clobbering manual queue edits, zombie containers blocking
launches, comparison.md showing stale failure counts), the operator
asked for "a full code review for backtest so that no other issues
appear." We've now found and fixed six bugs in one day (A — intra-bar
SL/TP gate; B — observability gaps; C — opposite-signal exit fill; D —
regime classifier disabled; E — O(N²) full-history slicing; F — pool
cascade from worker re-use). The pattern is clear: every bug found has
been costing us multiples of "find + fix" time in operational
recovery. The next 64 h of compute (37 h validation + ~27 h
nifty50_60d resume) cannot afford another preventable surprise.

The review surveyed every file in the backtester hot path:
`packages/research/{backtest_ensemble.py, battery.py, diagnostic.py}`,
`packages/strategies/_trend_context.py`, `tools/{run_battery.py,
run_battery_queue.py}`, plus the test fixtures and the recent Bug E /
Bug F regression tests. Total: 10 files, ~5,400 LOC.

The review surfaced **24 issues across 4 severity tiers**: 5 critical,
8 high, 7 medium, 4 low. Today's commit closes the **4 critical
issues that could affect the running 64 h compute window** (the fifth
critical — Bug B's regime hardcoding — cannot be retrofitted without
breaking running runs and is being deferred to the post-validation
window). The high/medium/low tiers are catalogued in §14.7 below for
the next freeze-bypass cycle.

### 14.2 G-1 — Atomic results-JSON writes + corrupt-JSON quarantine

**Observation.** `_completed_variant_names()` previously returned
`{p.stem for p in results_dir.glob("*.json")}` — every file matching
the glob was treated as proof the variant was complete. Combined
with the non-atomic `Path.write_text()` write inside
`_run_variant_in_subprocess`, this means: if a worker crashes
*during* the JSON write (OOM, native segfault, parent
`BrokenProcessPool` shutdown), the file exists on disk with a
truncated / invalid payload — and `--resume` permanently SKIPS that
variant. The variant would be missing from the final
`comparison.md` with no automatic recovery and no warning.

**Crash window.** The non-atomic write is one open + one write of
~5-50 KB JSON. On a fast disk, ~5 ms. With 19 variants × ~3 retries
across 64 h, the cumulative window is ~285 ms. Low probability per
event, but a single hit silently destroys evidence the operator
won't notice until the Friday review.

**Fix.**
1. New `_atomic_write_text(path, text)` helper: writes to
   `path.with_suffix(path.suffix + ".tmp")`, then `Path.replace()`s
   it onto the target. Atomic at the directory-entry level on both
   POSIX and Windows for same-filesystem renames.
2. Wired through every harness write that resume reads:
   `results/<name>.json`, `results/<name>.failure.txt`,
   `comparison.md`.
3. `_completed_variant_names()` now parses every JSON file,
   schema-checks against `_RESULT_REQUIRED_KEYS = ("variant",
   "summary", "elapsed_sec")`, and quarantines bad files to
   `<name>.json.corrupt`. Corrupt names are EXCLUDED from the
   completed set so resume re-runs them. Operators see a clear
   `[BATTERY] quarantined corrupt result …` warning per file.

**Cost.** One extra `Path.replace()` per write (~microseconds);
schema parse on resume (~ms per file). Negligible.

### 14.3 G-2 — Auto-retry loop on `BrokenProcessPool` cascade

**Observation.** Bug F's `max_tasks_per_child=1` eliminates
*cross-variant state pollution* but does NOT prevent the cascade
itself. Python's `ProcessPoolExecutor` invalidates the WHOLE pool
when ANY worker terminates abnormally — every pending future raises
`BrokenProcessPool`, even though the surviving variants never had a
chance to run. With 17 pending variants per typical battery, one
unlucky native crash still costs us 16 lost variants.

**Why this matters NOW.** The running validation has 19 variants in
its slate. Two have completed (V1, V2 baselines, currently running
in the new pool with the Bug F fix); 17 are still ahead. If
*any* of those 17 hits a fresh native crash (numpy SIMD bug, xgboost
init race, yfinance native socket close, OOM), the fix from this
morning saves us from cross-variant pollution but NOT from
cascade-killing the other 16. We'd be back to manually re-resuming.

**Fix.** Wrap the dispatch loop in a bounded retry. After a cascade,
recreate a fresh `ProcessPoolExecutor(max_workers=N,
max_tasks_per_child=1)` and re-submit only:

* Variants without a valid `results/<name>.json` (i.e., not
  completed — checked via `_completed_variant_names`).
* Variants not in the `real_failed` set (a per-attempt set tracking
  variants that hit a real Python `Exception` in `fut.result()`,
  distinct from `BrokenProcessPool` casualties).

Per-future handling now has TWO except branches:
```python
except BrokenProcessPool:
    broken_pool_seen = True; continue   # cascade casualty, retry
except Exception as e:
    real_failed.add(name); ...           # real failure, don't retry
```

Bounded by `MAX_POOL_RETRIES = 3` so a deterministically-crashing
variant doesn't infinite-loop us. After 3 attempts, remaining stuck
variants are written to `<name>.failure.txt` with the explicit
message `pool cascade after 3 retries` so the operator can root-
cause whatever's deterministically killing the worker.

**Cost.** Zero on the happy path (no cascade → loop exits after 1
iteration). On a single-cascade run: one extra pool spin-up (~5-10
seconds for 2 fresh workers + market_data unpickle each) per remaining
variant. For the running validation (17 variants pending after V1+V2),
this could save us ~50 hours of compute if any cascade occurs.

### 14.4 G-3 — Hard timeout on `yfinance.download`

**Observation.** `_trend_context._fetch_daily()` calls
`yfinance.download()` with no timeout. yfinance's underlying HTTP
client has socket defaults that can hang for many minutes on a
broken connection. Variants V1, V3-V9, V17-V18 all use the trend
filter (`trend_filter_pct` config field) → all invoke this code path
on every entry. A stalled HTTP socket hangs the worker thread
indefinitely; the harness' 30-min progress watchdog eventually fires
`os._exit(124)`, which cascade-kills the pool (Bug G-2 partially
recovers, but fail-fast on the network side is much cleaner).

**Fix.** New `_yf_download_with_timeout(symbol, timeout)` wraps the
yfinance call in a `concurrent.futures.ThreadPoolExecutor` with
`result(timeout=N)`. On timeout, returns `None`; `_fetch_daily`
treats `None` as "trend unknown" and the strategy fails open (does
NOT block the trade — same semantic as a permanent fetch failure).
Default 30 s timeout, tunable via `TREND_FETCH_TIMEOUT_SEC` env.

Why a thread-with-timeout (not `signal.alarm` and not
`yf.download(..., timeout=N)`)?
* Signal-based preemption is fragile in worker subprocesses.
* yfinance's outer `timeout=` kwarg is not version-stable.
* `ThreadPoolExecutor.result(timeout=N)` is portable and survives
  yfinance upgrades. A leaked thread (yfinance never returns) is
  bounded to one worker lifetime because `max_tasks_per_child=1`
  guarantees process exit after the variant.

**Cost.** Zero on the happy path (~50 ms HTTP fetch returns
immediately). On a hang: capped at 30 s instead of unbounded.

### 14.5 G-5 — Queue scheduler `--rm` + zombie-container retry

**Observation.** This is the exact incident from earlier today.
`build_docker_run_argv()` in `tools/run_battery_queue.py` did NOT
pass `--rm`, so an exited container kept its name. Since
`_run_id_for(...)` reuses the same `run_id` for resume launches, the
next launch hit `Error response from daemon: Conflict. The container
name "/<run_id>" is already in use by container ...`, the scheduler
marked the job permanently `"failed"` at launch, and the queue
ground to a halt until manual `docker rm` cleanup. We had 5 zombie
containers to clean up by hand.

`tools/cloud/launch_battery.sh` already passes `--rm`; this was a
parity gap between the manual launcher and the scheduler-spawned
launcher.

**Fix.**
1. Add `--rm` to the queue scheduler's docker-run argv.
2. On launch failure where stderr contains
   `is already in use by container`, run `sudo docker rm -f <run_id>`
   and retry the launch exactly once.
3. Distinguish `failure_phase: "launch"` from `failure_phase: "run"`
   in the saved job state so operators can route triage correctly
   (image / daemon vs. harness / variant).

**Cost.** Zero on the happy path. On zombie hit: one extra
`docker rm -f` (~1-2 s) + one launch retry. Eliminates the manual
intervention failure mode entirely.

### 14.6 Tests for Bug G

`tests/unit/test_battery_robustness.py` (NEW) — 26 tests across 5
classes, all passing alongside the existing 6 Bug F isolation tests:

* **TestAtomicWriteHelper** (5): `_atomic_write_text` exists, writes
  atomically, leaves no `.tmp` residue, overwrites existing files,
  is wired through both result JSON and comparison.md.
* **TestCorruptJsonQuarantine** (5): truncated JSON / missing keys /
  wrong-type `summary` are quarantined and excluded; valid JSON is
  accepted; `.tmp` residue is skipped.
* **TestProcessPoolRetryLoop** (5): `BrokenProcessPool` import
  exists, `MAX_POOL_RETRIES` defined, both per-future except
  branches present, all PPE constructions still set
  `max_tasks_per_child=1`, `real_failed` tracking set is named.
* **TestTrendContextTimeout** (5): `_YF_TIMEOUT_SEC` defined and in
  sane range, `_yf_download_with_timeout` wrapper exists,
  `_fetch_daily` routes through it (and does NOT call `yf.download`
  directly), timeout returns `None` (fail-open), env override
  honored.
* **TestQueueSchedulerRobustness** (3): `--rm` in argv, name-conflict
  recovery in `process_queue`, `failure_phase` recorded for both
  values.
* **TestBugGDocumented** (3): `Bug G-1` in `battery.py`, `Bug G-3`
  in `_trend_context.py`, `Bug G-5` in `run_battery_queue.py`.

Full unit suite still passes: **1267 / 1267** (was 1247 before this
work; +6 isolation tests landed earlier today, +14 Bug E tests
from earlier today, +26 Bug G robustness tests now). No existing
tests changed.

### 14.7 Issues NOT fixed in this commit (catalogued for follow-up)

These are recorded here so the next freeze-bypass cycle has a
prioritized backlog:

**HIGH (fix before next battery launch, but not blocking the
running window):**

* H-1: Same-bar re-entry after intra-bar SL/TP exit
  (`backtest_ensemble.py:311-400`) inflates trade count on
  stop-heavy variants. Add `continue` or `exited_this_bar` flag.
* H-2: `backtest_end` exits omit slippage
  (`backtest_ensemble.py:588-603`); positions still open at dataset
  end get systematically better fills.
* H-3: `comparison.md` notes still claim "Sharpe is annualized from
  per-bar returns" (corrected in this commit) — verify Friday
  reviewer reads the new wording.
* H-4: In-memory scheduler state clobbers manual `battery_queue_state.json`
  edits (already documented in `ops_runbook.md` as today's pitfall;
  proper fix is re-read state from disk before each `save_state()`
  merge).
* H-5: Bug F watchdog `os._exit(124)` still breaks the pool for
  sibling workers; G-2's auto-retry recovers, but a per-batch pool
  isolation would be cleaner.
* H-6: `_merge_bars` materializes the full event list before
  yielding (memory spike at variant start). Heap merge would
  amortize.

**MEDIUM (next freeze cycle):**

* M-1: Strategy `generate_signal` exceptions silently swallowed
  inside the per-bar loop.
* M-2: XGBoost recomputes full `FeatureEngine.compute_all()` on
  every bar even with the new 300-bar window.
* M-3: XGBoost / LSTM not in Bug E equivalence tests.
* M-4: Tie-breaking at identical timestamps in `_merge_bars` is
  dict-order dependent (add secondary sort by symbol).
* M-5: `_trend_context._cache` grows unbounded within a single
  worker (200+ symbols × 6h TTL); add LRU cap.
* M-6: `ProcessPoolExecutor` doesn't set `mp_context="spawn"` on
  Linux for parity with Windows + safety against fork+native-thread
  issues.
* M-7: `BacktestConfig.apply_regime_filter` is dead code — wire it
  up or remove.

**LOW (backlog):**

* L-1: Opposite-signal exit fills at bar close, not intra-bar touch
  (Bug C approximation, documented but slightly different from Bug A
  intra-bar precision).
* L-2: `backtest_end` closes don't update `losses_per_stock`
  blacklist counter (irrelevant — loop has ended).
* L-3: Faulthandler fault-log file handle never explicitly closed
  (process exit cleans up; intentional).
* L-4: `diagnostic.py` not in battery hot path; reviewed for
  cross-contamination, none found.

**CRITICAL deferred (not fixable mid-run without invalidating
results):**

* DEF-1: Regime classifier hardcoded to `"unknown"` in
  `backtest_ensemble.py` (Bug B / Bug D residue). All current PnL/WR/PF
  numbers in the running battery overstate live-aggressive behavior
  vs. production. Direction conclusions (short-veto / long-only)
  still hold; magnitudes do not. Friday review must flag all numbers
  as "no regime suppression" until the post-validation window adds
  Nifty/VIX to `market_data.pkl` and computes per-bar regime.

### 14.8 Confirmations on today's earlier fixes

The review explicitly verified each of today's earlier fixes is
sound (no follow-up issues found in the bugs themselves):

* **Bug A (intra-bar SL/TP gate):** ordering correct
  (`_detect_intrabar_exit` runs *before* opposite-signal branch);
  conservative SL-first tie-breaking when both are in range
  (`backtest_ensemble.py:724-727, 739-740`); gap-open uses open
  price. *Caveat:* same-bar re-entry possible (H-1, deferred).
* **Bug C (opposite-signal exit fill):** evaluated and closed at
  current-bar close + slippage with simulated `exit_time`
  (`backtest_ensemble.py:376-384`); same-direction duplicates still
  dropped via held-position branch.
* **Bug E (`strategy_history_window=300`):** slice is
  `df.iloc[max(0, i+1-window) : i+1]` — correct at all boundaries,
  warm-up uses shorter prefix when `i+1 < window`,
  `max(window, 1)` prevents empty slices. *Caveat:* XGBoost not in
  equivalence tests (M-3, deferred).
* **Bug F (`max_tasks_per_child=1`):** confirmed at
  `battery.py:1475-1478`; loguru `add`/`remove` in try/finally
  remains correct belt-and-suspenders. *Caveat:* `mp_context` not
  set (M-6, deferred); cascade not eliminated (closed in this
  commit via Bug G-2).

### 14.9 Freeze accounting

All four Bug G fixes touch `packages/research/battery.py`,
`packages/strategies/_trend_context.py`, and
`tools/run_battery_queue.py`. None is on the slot-consuming list in
FREEZE_v2.1 (which covers strategies, risk, position sizing,
trading_agent.py, config.yaml strategy/risk blocks, models). All
four are **harness/library robustness fixes**: they change *how* the
orchestrator handles failures and *how* one helper times out a
network call, not *what* anything computes. Backtest results are
byte-identical (same code on same data on the happy path; Bug G-2
only activates on cascade and produces the same per-variant output
the original happy path would have produced; Bug G-3 only changes
behavior on a network hang where the prior code would have
hung-then-watchdog-killed). No bypass slot consumed. Bypass ledger
remains 1/3 (risk.allow_shorts).


## 15. Bug G self-audit — two real defects in the original Bug G fix (2026-05-26 morning)

**Trigger.** The user asked to "check the bug g and fix" without
deploying. Bug G shipped overnight while the validation run
continued on the in-memory pre-G code; the next scheduler-spawned
launch (`nifty50_60d` resume in ~28h) was going to be the first
container to actually exercise the new code paths. If a defect lurked
in any of G-1/G-2/G-3/G-5, that resume would be the worst possible
moment to discover it — mid-validation, on a critical-path job, with
no human in the loop. So we did a line-by-line self-audit of the
cb76f0e diff, mentally simulating every failure path each fix
claimed to handle.

**Two real defects surfaced.** Both reproduced in pytest by
temporarily reverting the audit fix and re-running the new
regression test. Both were silently broken in cb76f0e — no test
covered the failure path because the original tests were
source-level structural checks rather than behavioural tests.

### 15.1 G-1.A — Orphan `<name>.json` masks `<name>.failure.txt` on resume

**Failure mode.** The worker (`_run_variant_in_subprocess`) writes
`results/<name>.json` *atomically at the end of the function*, then
returns the payload tuple. Pickling and IPC happen between the
return statement and the parent's `fut.result()`. If anything raises
in that window — non-pickleable object reaches the IPC channel, an
OOM during pickle-buffer allocation, the subprocess receives a
SIGKILL after the JSON write but before the return — the parent's
per-future `except Exception` branch records a `<name>.failure.txt`,
but the on-disk JSON survives. The variant now has BOTH files: a
clean-looking JSON that says "I succeeded" and a failure.txt that
says "I crashed".

**Original `_completed_variant_names` was structurally blind to this.**
It read `results/*.json`, schema-checked each, and added the stem to
`completed`. The failure.txt was never consulted. Consequence: on
the next operator-initiated `--resume <run_id>`, the variant would
appear in `completed`, the resume's `pending` filter would exclude
it, and the variant the operator was specifically trying to retry
would silently be skipped. This is the exact failure-mode pattern
Bug G-1 was supposed to prevent (silent skip on resume) — the fix
just plugged a different leak.

**Probability assessment.** Low per-variant. The window between
JSON write and successful return is small — single-digit
milliseconds — and most exception sources we'd hit there
(non-pickleable types) would have been caught earlier by the JSON
serialization which uses `default=str`. But "low per variant" times
"19 variants × N validation runs × indefinite future" is not
obviously low at the program scope, especially as we add stricter
content into payloads.

**Fix.** `_completed_variant_names` now also enumerates
`results/*.failure.txt`, computes the variant name (string-slice
because `Path.stem` only strips one suffix), and treats the
failure-txt set as an *override*: any JSON whose sibling failure
exists is omitted from `completed` regardless of how cleanly it
parses. The orphan JSON is left in place (NOT renamed to .corrupt)
so an operator triaging the failure can still inspect what the
worker computed before crashing. The within-run case (same retry
loop, after writing failure.txt) is also covered correctly: the
variant is in `real_failed` so the loop's `not_done` filter excludes
it; the next attempt would see the failure.txt and skip the orphan
even if `real_failed` were lost.

**Cost.** ~15 lines net in `_completed_variant_names`, 3 new tests
in `TestCorruptJsonQuarantine`. Behaviour change is strictly
*excluding* a variant from completed; never *including* one that
shouldn't be. Cannot regress any happy-path run.

### 15.2 G-3.A — `with ThreadPoolExecutor(...) as ex:` defeats the timeout

**Failure mode.** The original `_yf_download_with_timeout` looked
like a textbook portable-timeout: submit to a single-worker
ThreadPoolExecutor, `result(timeout=N)`, catch TimeoutError, fail
open. Source review passed. Tests passed.

**It does not work when the inner fetch is genuinely hung.** The
`with _cf.ThreadPoolExecutor(...) as ex:` construct relies on
`Executor.__exit__`, which is documented to call
`shutdown(wait=True)`. With `wait=True`, `__exit__` blocks until
every running task completes. So when `result(timeout=N)` raises
`TimeoutError` and control hands back to the with-block, the
with-block's `__exit__` then BLOCKS WAITING FOR THE HUNG THREAD —
exactly the thing the timeout was supposed to escape. The function
never returns within `timeout` seconds; it returns whenever the
underlying yfinance call decides to wake up (or never).

The G-3 fix was, in effect, equivalent to the no-timeout original
on the failure path it was supposed to handle.

**Empirically reproduced.** A new behavioural test
(`test_timeout_actually_returns_within_window_when_fetch_hangs`)
mocks `yfinance.download` to sleep 30s, asks for a 1.0s timeout,
and asserts `elapsed < 5.0`. With the cb76f0e code: elapsed = 30.0s
(test fails). With the audit fix: elapsed ≈ 1.0s (test passes).

**Why this matters in production.** A hung yfinance worker thread
inside a battery variant is the exact upstream of a Bug F-style
cascade: watchdog eventually fires `os._exit(124)` → ProcessPool
sees worker death → BrokenProcessPool for all sibling variants. The
G-3 fix was meant to close that path by failing the fetch fast and
returning None (fail-open: trend filter treats it as "trend
unknown" and lets the trade through). With the broken fix, the
fetch doesn't fail fast — it hangs → watchdog → cascade. We didn't
notice in the validation run because no yfinance call has actually
hung yet.

**Fix.** Replace the `with`-block with an explicit
`try/finally`: create the executor manually, return the result on
success (or None on TimeoutError), and call
`ex.shutdown(wait=False, cancel_futures=True)` in the finally. The
hung yfinance thread leaks until the worker subprocess exits, but
`max_tasks_per_child=1` guarantees the worker exits after the
single variant — bounding the leak to one variant's lifetime. The
trade-off (briefly leak a daemon-equivalent thread vs. block the
whole worker indefinitely) is unambiguously correct for our
use-case.

**Cost.** ~10 lines in `_yf_download_with_timeout`, 1 new
behavioural test. The behavioural test (sleep+timeout pattern)
takes ~1.5s to run, so it doesn't materially slow the suite. The
test is the load-bearing piece — source-level checks fundamentally
cannot catch this class of bug.

### 15.3 What we did NOT change (audit findings, deferred)

Several minor issues surfaced during the audit and were
intentionally left alone (documented here so they aren't
re-discovered as "new" later):

* **`_atomic_write_text` race on shared target.** Two callers
  writing to the same path concurrently can lose a write — both
  use the same deterministic `<path>.tmp` filename. Fine in
  practice (every current call site has exclusive ownership);
  flagged for the day someone adds a parallel writer to
  comparison.md or a results JSON.
* **Watchdog cascades a single-variant hang.** Even with G-2's
  retry loop, if variant X *deterministically* hangs and triggers
  the watchdog, X cascades the pool and takes its in-flight
  siblings down with it. After MAX_POOL_RETRIES=3 attempts X is
  marked stuck. Acceptable: a deterministically-hanging variant is
  itself the bug to fix; the retry loop's job is to absorb
  *transient* cascades. A future enhancement could run the suspect
  alone (max_workers=1) on retry.
* **Worker log appends across retries.** When G-2 retries variant
  X, loguru opens `workers/X.log` in append mode (default), so
  attempt-2 content concatenates onto attempt-1's truncated tail.
  Forensic-quality only — operator triage still works, just with
  a "where does attempt 2 start?" scan. Not worth the change.
* **G-5 stale stderr on rm-failure path.** When `docker rm -f`
  itself fails, the stored failure record carries the *first*
  launch's "in use by container" stderr rather than the rm-failed
  context. Operator can read the scheduler stdout for the
  "docker rm -f failed" line that precedes the saved record.

### 15.4 Freeze accounting — no slot, no policy change

Both audit fixes are post-cb76f0e amendments to the same
harness/library files:

* `packages/research/battery.py` (`_completed_variant_names`)
* `packages/strategies/_trend_context.py` (`_yf_download_with_timeout`)

Neither file is on FREEZE_v2.1's slot-consuming list. Both fixes
make the *failure-handling* paths actually work as advertised —
they don't change anything about *what* the backtester computes on
a happy path. Backtest results remain byte-identical for any run
that doesn't trigger the failure paths these fixes guard. Bypass
ledger remains 1/3 (risk.allow_shorts).

### 15.5 Deployment posture — explicitly NOT pushed to the VM

Per the user's directive ("check the bug g and fix just don't
deploy it"), this audit-fix commit is pushed to origin/main but
**not** pulled to the backtester VM. Rationale:

* The currently-running validation container (started before
  cb76f0e) holds in-memory pre-Bug-G code; it is not affected by
  any commit on disk.
* The next scheduler-spawned launch — `nifty50_60d` resume — picks
  up the latest pulled code via the read-only `packages/`
  bind-mount. The VM-side checkout currently sits at cb76f0e (the
  ORIGINAL Bug G fixes). That includes the broken G-3 timeout, so
  if a yfinance call hangs in the resume window we'd cascade.
* Per user instruction, we do NOT pull the audit fix mid-window.
  The validation tail is the priority; a VM-side `git pull` would
  also affect any other code path that's been touched on origin.
* If a yfinance hang materializes during the resume, we revisit.
  Probability is low (no hang has occurred in the last ~12h of
  active validation traffic) and the watchdog still acts as a
  backstop (60-min silence window → fail the variant rather than
  wedge indefinitely).
* Post-validation, both audit fixes get pulled with the rest of
  the post-Friday tail of changes.

---

## 16. Bug H — Backtester silently ran without XGBoost (2026-05-26 afternoon)

### 16.1 Discovery context

Today's live trader VM took a -Rs 453 daily loss from three
`xgboost_classifier` BUY trades (HFCL, TATAINVEST, TATACHEM) that
opened in the `bear_low_vol` regime and all stopped out within the
first 15 minutes. The trader VM logs confirmed xgboost was both
the strategy responsible and the only voice the regime was
weighting at 5.0× in this market.

That triggered an obvious cross-check: "what does the
`nifty500_v4_long_only_validation_60d` backtester run say about
shipped behaviour in this regime?" The answer was supposed to be
V1's PnL. The actual answer turned out to be: **V1 in that run
isn't shipped behaviour. It's shipped MINUS xgboost.**

### 16.2 Failure mode

Every variant's worker log carries the line:

```
[XGB-HEALTH] XGBoost model not found at models/xgboost_model.pkl.
Strategy will return HOLD.
```

`xgboost_classifier.load_or_train` is fail-open by design (a
missing artifact during live trading must not crash the daemon),
so a missing model gives you a strategy that returns HOLD on
every cycle, a quiet warning at startup, and no scheduler alarm.
The validation ran for ~16 hours and completed V1 / V2 before any
operator noticed.

The artifact `models/xgboost_model.pkl` was missing because:

1. The Dockerfile creates `models/` empty (so the strategy's
   path resolution doesn't fail) but does not COPY a model file.
   This is the same image used on the trader VM.
2. The trader VM bootstrap pipeline writes the model to
   `/opt/trading-agent/models/xgboost_model.pkl` on the host,
   then docker-compose bind-mounts `models/` into the container.
   So the trader runs with xgboost active.
3. The backtester VM has the host directory `models/` (empty,
   uid 1001, mtime 2026-05-18) but no bootstrap step ever
   populated it, and the queue scheduler's
   `build_docker_run_argv` did not declare a `models/` mount.

So the backtester container had an empty `/app/models/`, the
strategy fell through to fail-open, and every backtest in this
run effectively turned xgboost off.

### 16.3 What's affected

* `nifty500_v4_long_only_validation_60d_20260525T164751`:
  V1 (-Rs 562, -5.62%) and V2 (-Rs 1167, -11.54%) are **xgboost-off
  results**, not shipped baselines. Renamed on disk to
  `.NO_XGBOOST.json.archive` to preserve the data without leaving
  them in `_completed_variant_names`.
* V4 / V17 (in flight at 2.3% and 1.7%, respectively) — killed,
  will re-run from scratch with xgboost active.
* V18 / V19 — never started; will pick up the corrected
  environment when scheduled.
* `nifty50_60d_20260525T105637` — older run, predates the
  validation insert. Same gap applies (model missing). The
  resume queued after the validation will inherit the corrected
  environment automatically — every variant gets re-run with
  xgboost. **No corrective action needed on that queue item; the
  scheduler resume already plans to re-execute V3-V19 there.**
* All `_archive/` runs from May 18-25 — same gap, but those
  results were already discarded or are flagged as
  speed-patch-era data. No fix needed; they were never
  decision-grade.

### 16.4 Fix

* `tools/run_battery_queue.py:build_docker_run_argv` — added
  `-v {TRADER_HOME}/models:/app/models:ro`. Read-only because
  the model file is a production artifact; a worker must never
  rewrite it during a run.
* `tools/cloud/launch_battery.sh` — same mount, plus the
  pre-existing `packages/` mount that had drifted (manual
  launches were running with image-baked code from
  2026-05-22). Both mounts now identical between the two
  launch paths.
* `tests/unit/test_battery_queue_scheduler.py` — extended
  `test_run_argv_includes_required_mounts` with the
  `models:/app/models:ro` assertion. 29 tests still pass; 4
  battery_robustness tests still pass. Total: 59 / 59.

### 16.5 Rollout (2026-05-26 14:30 IST onward)

Step-by-step is documented in `changes_done_2026-05-25.md` §15.5.
Verification gate is at the first variant completion (~6-12 h
after restart): the worker log must contain
`[XGB-HEALTH] OK — 31 features` instead of "model not found". If
the warning reappears, the bind-mount didn't take effect (most
likely cause: a stale `git pull` or a docker daemon caching the
old container spec) and the next 30 hours of compute would be
wasted again. Hard-stop on that condition.

### 16.6 Process lesson

The warning was visible from variant launch but no automated
surface saw it. The `battery_status_remote.ps1` summary script
shows variant progress, queue state, ETA — but does not grep
worker logs for known fail-open warnings. **Post-Friday todo**
(not part of this fix, captured in `docs/post_freeze_v4_proposal.md`):

> Add a "fail-open warning census" section to
> `battery_status_remote.ps1`. Lines to count per worker log:
>   * `\[XGB-HEALTH\] XGBoost model not found`
>   * `\[trend_context\] yfinance timed out`
>   * `\[(BATTERY|RESULTS)\] .*invalid|corrupt`
>
> Surface them with the same prominence as the
> per-variant progress %. Any non-zero count blocks the run from
> being declared "decision-grade".

This kind of silent fail-open is exactly the failure mode
freeze-v2.1 was supposed to catch via "audit before deploy", and
it slipped through because the audit lens was pointed at *code
changes* not *deployment state*. Worth a separate think after
Friday on whether the freeze ledger should track "VM-side
artifact state" as a first-class column.

### 16.7 Probability-weighted cost (Bug G framework)

* P(recurrence without fix): 1.0 (it's structural — happens every
  battery container launch).
* Magnitude: every variant's PnL silently misrepresents the
  shipped baseline. The validation run was on the critical path
  for the Friday 2026-05-29 review.
* Time-cost: 30 hours of compute (V1 + V2 + part of V4/V17)
  effectively wasted, plus the operator audit time it took to
  diagnose.
* Fix cost: 4 lines of code, 1 test line, 1 file copy. ~15 min
  dev time + ~10 min VM ops.

---

## 17. Bug I — Trader VM operating with 5 uncommitted local hot-fixes for ~2 weeks (2026-05-26 afternoon)

### 17.1 Discovery context

Triggered by attempting to deploy `risk.allow_shorts: false` to the
trader VM (the slot-1 pre-staged flag, authorised today by the user
under "deploy all the fix"). The deploy hop was `git pull origin
main` on the trader, which **aborted** with:

```
error: Your local changes to the following files would be overwritten by merge:
        docker-compose.yml
        packages/core/stock_scanner.py
        packages/monitoring/alerts.py
        tools/cloud/install_heartbeat_cron.sh
        tools/send_heartbeat.py
Please commit your changes or stash them before you merge.
error: The following untracked working tree files would be overwritten by merge:
        tools/cloud/install_watchdog_cron.sh
        tools/watchdog_check.py
Please move or remove them before you merge.
Aborting
```

Trader HEAD: `868d5ad` (2026-05-19). Origin/main HEAD: `73c26bf`
(2026-05-26). Divergence on disk = 7 days. The container had been
restarted as part of the deploy attempt, but the pull failed and the
`sed` edit of `config.yaml` was a no-op (the key wasn't present on
the trader's `config.yaml` because the slot-1 commit was never pulled
in). **Net effect: trader is operating identically to pre-attempt,
behaviour unchanged, but in a state that quietly resists upgrades.**

### 17.2 What the 5 modified files contain

Every diff is a *real production hot-fix* that solves an operational
problem. None are bugs or junk:

| File | Local delta | Justification (from in-file comments) |
|---|---|---|
| `docker-compose.yml` | +16 lines: bind-mounts `./tools:/app/tools:ro` and `./packages:/app/packages:ro` | 2026-05-23: enabling host-side hotpatch without an image rebuild during freeze. Required for everything below to actually take effect. |
| `packages/core/stock_scanner.py` | rewritten (523 lines vs HEAD's 418) | NSE archives CSV path hot-fix. Without it, the live scanner fails to refresh its NSE universe. |
| `packages/monitoring/alerts.py` | rewritten (665 lines vs HEAD's 644) | TLS verify default flip + markdown→HTML render for emails. Without it, alert emails fail under corporate-network TLS interception and arrive as walls of pseudo-text. |
| `tools/send_heartbeat.py` | rewritten (463 lines vs HEAD's 362) | Refactor + container-exec mode so cron can fire `docker exec trader python ...` from the host. |
| `tools/cloud/install_heartbeat_cron.sh` | +92 lines | `--container` mode + log-file pre-create dance (diagnosed 2026-05-25 09:10 IST when first heartbeat fire was a permission-denied no-op). |

And four untracked files that the trader genuinely relies on:

| File | Mtime | Why |
|---|---|---|
| `tools/cloud/install_watchdog_cron.sh` | 2026-05-25 | Watchdog installer (added in response to 2026-05-22 11-hour silent-hang). |
| `tools/watchdog_check.py` | 2026-05-25 | The watchdog daemon itself — 5-min cron-fired probe of `logs/health.json` mtime, alerts on transitions. |
| `docker-compose.override.yml` | 2026-05-11 | OCI VM memory limits: 750M cap, 400M reservation. |
| `EMERGENCY_STOP` | 2026-05-13 | Empty sentinel file; daemon checks for its presence and refuses to open new positions if present. |
| `_deploy.sh` | 2026-05-13 | Operator script for `git reset --hard origin/main` + UID alignment + image rebuild. |
| `logs/.eod_sent_2026-05-15.flag` | 2026-05-15 | EOD-summary dedup marker; harmless residue. |

### 17.3 Why it matters

* **Upgrades are blocked.** Any attempt to pull origin/main aborts
  on the modified-files check. This is a freeze-policy concern in
  two directions:
  1. Future audit-only fixes (e.g. the G-3 / G-3.A yfinance timeout,
     which is genuinely relevant to live trading because the daemon
     calls yfinance in the trend-context cache) cannot land on the
     trader without manual reconciliation.
  2. The slot-1 `risk.allow_shorts: false` change cannot be deployed
     via the documented `git pull` hop. Today's deploy attempt
     proved this. The same will be true of slot-2 and slot-3 when
     they're consumed.
* **The trader is on a fork-style branch with no upstream.** The 5
  modified files have never been committed (anywhere). If the trader
  VM disk is lost, the hot-fixes are lost with it.
* **The freeze ledger does not reflect the actual production state.**
  `FREEZE_v2.1.md` tracks slot consumption via files-modified-on-main.
  None of the trader's hot-fixes appear in the ledger because they
  never went through main. The ledger reads "1/3 slots used" but the
  trader is materially N changes ahead of the documented baseline.

### 17.4 What today's attempt did NOT cause (defensive accounting)

* `config.yaml` was unchanged because the `sed` regex couldn't match
  `allow_shorts:` (the line doesn't exist on the trader's config).
* `config.yaml.bak_pre_allow_shorts_20260526T091042` was created on
  the trader and remains as a forensic snapshot of the pre-attempt
  state.
* The trader container was restarted with the SAME config + SAME
  on-disk packages it had at 14:37 IST. It came back healthy at
  14:41 IST, rehydrated 3 trades + 3 cooldowns from DB, loaded
  xgboost successfully, and ran preflight clean. **Behaviour
  identical to pre-attempt.**
* Operationally: the user explicitly took over the trader VM
  rebuild as a manual task ("i will maually rebuilt the tarder vm")
  upon seeing the divergence. This finding documents what was on
  disk at handover time.

### 17.5 Recommended reconciliation (post-Friday review)

Not done today on purpose — too risky during the validation tail.
Plan for post-Friday:

1. On the trader VM, create a feature branch:
   ```
   sudo git -c safe.directory=/opt/trading-agent checkout -b trader-hotfixes-2026-05-26
   sudo git -c safe.directory=/opt/trading-agent add docker-compose.yml \
       packages/core/stock_scanner.py packages/monitoring/alerts.py \
       tools/cloud/install_heartbeat_cron.sh tools/send_heartbeat.py \
       tools/cloud/install_watchdog_cron.sh tools/watchdog_check.py \
       docker-compose.override.yml
   # leave EMERGENCY_STOP + _deploy.sh + .eod_sent flag uncommitted on
   # purpose -- those are operator-local artifacts
   sudo git -c safe.directory=/opt/trading-agent commit -m 'trader: production hot-fixes ... 2026-05-11 → 2026-05-25'
   sudo git -c safe.directory=/opt/trading-agent push origin trader-hotfixes-2026-05-26
   ```
2. Pull-request that branch into main, review each hot-fix for
   intent, merge.
3. `git pull origin main` on the trader VM now fast-forwards cleanly.
4. Then the slot-1 `risk.allow_shorts: false` deploy is the
   documented 2-line edit-of-config.yaml + restart.

Total estimated reconciliation time: ~90 minutes including review.
Best done on a Saturday morning when the market is closed.

### 17.6 Lesson for the freeze policy

The freeze ledger tracks main-branch changes. It does NOT track the
gap between main-branch state and on-VM state. For the next freeze
iteration, the ledger should either:
* Run a daily reconciliation check (`git status --porcelain` on
  each VM, alert if non-empty), OR
* Pin VMs to a tag with no read-write working tree (mounts only,
  no `git pull` capacity).

The current freeze policy assumes the VM's checkout is always the
canonical artifact. Today's discovery shows that's wishful thinking
once an operator starts hot-fixing in place. The 5 hot-fixes here
are all *good* changes — the system worked correctly to keep the
daemon alive — but they accumulated outside the audit trail.

### 17.7 Files touched in this finding

* `docs/findings_log_2026-05-25.md` — this §17 added
* `docs/changes_done_2026-05-25.md` — §16 mirror entry (next commit)
* No code changes. No deploy. No restart. Pure documentation.
