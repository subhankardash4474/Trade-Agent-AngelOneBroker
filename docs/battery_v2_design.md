# Battery-v2 Design

**Status:** Designed, not queued.
**Estimated runtime:** 6-12 hours on a typical laptop, 2-4 hours on a cloud t3.large.
**Recommended trigger:** Sunday afternoon or once the cloud research-pod is wired (per `docs/cloud_pod_architecture.md`).

---

## Why a v2 battery

Battery-v1 (`logs/backtests/20260508T155112`) ran 15 variants on **10 stocks × 30 days**. The result table told us two uncomfortable things:

1. **10 of 15 variants produced identical results** — the universe is too narrow for most knobs (peak-giveback, opening-lockout, window-cap, confidence threshold) to even fire enough times to matter. The toggles are degenerate on this dataset.
2. **Only V9 (`filter_vwap_orb_off`) was profitable** with 5 trades — too thin to act on, but too suggestive to drop.

To make a confident "this knob helps / hurts" decision we need:

- **More signal events**: 200 stocks instead of 10 = ~20x trade count
- **Longer regime exposure**: 90 days instead of 30 = covers at least one full bear/bull/sideways rotation
- **Realistic costs already modeled** (battery-v1 already has `core/charges.py` + slippage + brokerage. Don't change.)

Expected per-variant: 80-300 trades (vs 5-7 in v1). At that sample size, PF differences of 0.2 are statistically distinguishable.

## Universe

- **200 stocks** drawn from the live scanner's NSE-500 watchlist with `min_avg_volume=500_000`, `min_atr_pct=1.0`. This is the **same universe the live agent actually scans**, not a curated large-cap list. Critical: V1 used 10 hand-picked large-caps (RELIANCE, TCS, INFY, HDFCBANK...) which the agent rarely trades anyway because they're capital-inefficient at Rs 2-3k position sizes. V2 must include the mid-caps where >70% of live trades happen.
- **Source list:** Pull the latest `core/stock_scanner.py` output and freeze it as `tests/fixtures/battery_v2_universe.json` so the run is reproducible.

## Time window

- **90 days** ending the prior trading day (last completed session). Re-run weekly to roll the window.
- 5-min bar resolution (matches live agent and v1).

## Variants (planned: 18 — hand-picked, not auto-grid)

The 18 are organised in **4 thematic groups**: edge re-tests (validate v1 findings on a real sample), risk knobs (controls we couldn't test in v1), strategy-mix knobs (what to enable / disable), and TP/SL geometry (the biggest profitability lever once edge is real).

### Group A — Edge re-tests (validate v1 findings)

| ID | Variant | Hypothesis |
|---|---|---|
| **B1** | `baseline_current_shipped` | Reference: exact `config.yaml` as of HEAD. All other variants compared against B1. |
| **B2** | `mr_re_enabled_z_2_0` | Re-enable `mean_reversion` at `entry_z_score=2.0` (stricter than v1's 1.8). Test if MR was killed in error or if the R:R imbalance is structural. |
| **B3** | `vwap_orb_filter_off` | The V9 win. Validate on a real sample whether disabling trend filter on `vwap_bounce` + `opening_range_breakout` actually helps. |
| **B4** | `xgb_thresh_0_70` | Raise XGBoost confidence threshold 0.65 → 0.70. v1 showed XGB has the strongest per-trade PF (4.58) on small N — does tighter selection compound the edge? |

### Group B — Risk knobs that v1 couldn't test (too few trades)

| ID | Variant | Hypothesis |
|---|---|---|
| **B5** | `peak_giveback_off` | Disable peak-giveback exit. v1 V12 was 5 trades — meaningless. With 100s of trades, this should reveal whether peak-giveback locks in profit (good) or exits too early on noise (bad). |
| **B6** | `peak_giveback_50pct` | Loosen peak-giveback threshold 35% → 50%. Hypothesis: 35% is too tight, exiting strong runners early. |
| **B7** | `opening_lockout_off` | Disable 09:15-09:30 lockout. v1 V14 was degenerate; on a wide universe, opening-window edge becomes measurable. |
| **B8** | `window_cap_4` | Tighten max_opens_per_window 6 → 4. Tests whether burst protection helps in correlated-cluster scenarios. |
| **B9** | `daily_loss_kill_2pct` | Tighten daily kill-switch 3% → 2%. Trades off blow-up protection against valid recovery days. |

### Group C — Strategy-mix knobs

| ID | Variant | Hypothesis |
|---|---|---|
| **B10** | `xgb_only` | Run only `xgboost_classifier`. Tests if single-strategy concentration on the highest-PF model beats ensemble noise. |
| **B11** | `rule_based_only` | Disable `xgboost_classifier` (rule-based ensemble only). Tests if the model is actually contributing positive PnL on a wide sample. |
| **B12** | `min_2_strategies_agree` | Raise `min_strategies_agree` 1 → 2. Tests whether confluence helps. |
| **B13** | `ensemble_thresh_0_60` | Tighten `confidence_threshold` 0.55 → 0.60. v1 V10 was degenerate. |

### Group D — TP/SL geometry (the big lever)

| ID | Variant | Hypothesis |
|---|---|---|
| **B14** | `atr_sl_2_5x` | Widen SL multiplier 2.0 → 2.5 ATR. Hypothesis: 2.0 still triggers on noise; 2.5 reduces shakeout exits at the cost of larger losses when wrong. |
| **B15** | `tp_4_pct_max` | Raise `max_tp_pct` 2.5 → 4.0. v1 mean_reversion R:R was 1:0.28 (TP too tight); test if a higher TP ceiling could rescue MR. |
| **B16** | `tp_to_sl_3x` | Raise `max_tp_to_sl_multiple` 2.5 → 3.0. More asymmetric R:R. |
| **B17** | `min_rr_strategy_1_5` | Floor min_rr at 1.5 across the board. Tests if the expected-profit gate is too lenient. |
| **B18** | `combo_winners` | Apply best combo from B1-B17 (decided after the run). The "what's the best config we now know how to build" smoke test. |

(B18 needs to be re-spec'd after B1-B17 finish. The variants doc treats it as a placeholder.)

## Execution plan

```bash
# Once: snapshot the live scanner watchlist
python tools/_freeze_battery_v2_universe.py     # TO BE WRITTEN — ~30 LoC

# Run (auto-resume on interrupt):
python -m research.battery \
    --variants B1,B2,B3,B4,B5,B6,B7,B8,B9,B10,B11,B12,B13,B14,B15,B16,B17 \
    --days 90 \
    --universe-file tests/fixtures/battery_v2_universe.json \
    --resume auto

# Status while running:
.\tools\battery_status.ps1
```

Each variant produces its own `logs/backtests/<run_id>/results/<variant>.json` and the comparison.md is written incrementally — same as v1.

## Why we're NOT queuing it tonight

1. **No urgency.** B1-B17 results inform Tuesday-Wednesday config tweaks at the earliest. Monday's trading already has the v1-derived config.
2. **Heavy local load.** 200 stocks × 90 days × 17 variants = many GB of yfinance pulls. Laptop fans run flat-out for 6-12 hours. The user goes to sleep, the OS may suspend, the run may stall (recoverable via `--resume auto` but irritating).
3. **Better target: cloud research pod.** Per `docs/cloud_pod_architecture.md`, the research pod is designed to run exactly this kind of compute on a t3.large for ~Rs 30-50/run. We're 1-2 weeks from that being live.

When the user is ready: either run locally on a Sunday (laptop plugged in, "stay awake" power profile) or wait for the cloud research pod and run it there. The script is identical — only the host differs.

## Pre-flight checklist (before pressing go)

- [ ] Universe snapshot frozen (`tests/fixtures/battery_v2_universe.json` exists, ~200 symbols)
- [ ] Daemon and prior battery confirmed dead (`.\tools\battery_status.ps1`)
- [ ] Laptop on AC power, sleep disabled (`powercfg /change standby-timeout-ac 0`)
- [ ] At least 5 GB free for cached market data + per-variant results
- [ ] `--resume auto` understood (run can be interrupted and resumed without losing completed variants)

## Success criteria

The run is "useful" if **at least 4 of the 17 variants** produce statistically distinguishable PF results (PF delta vs B1 ≥ 0.15 on N ≥ 50 trades). v1 had 1/15 (V9). v2's universe expansion should bump this materially — if it doesn't, the framework needs revisiting before any further tuning is meaningful.
