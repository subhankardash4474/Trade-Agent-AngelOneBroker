# Overnight Engineering Session — 2026-05-07 (16:00 -> 21:00)

Carried out as part of the "Phases 1, 2, 3" overnight plan. Daemon was
killed at 16:00; this session is purely offline backtesting + code +
tests.

## Findings from today's live trading
- **MFE leakage = 84%**: Rs +270 banked, but post-mortem flagged
  Rs +1,367 left on the table.
  - MEESHO peak +Rs 276 -> exited +Rs 71 (74% giveback). Existing
    0.3% price-trail never caught it because the bar closes drifted
    back without ever crossing the trail line.
  - CROMPTON carryover gapped against us on open, lost Rs 166 on a
    stale yesterday-ATR SL.
- **Strategy mix**:
  - supertrend_follow +Rs 138 (5T, 60% WR) — workhorse.
  - xgboost_classifier +Rs 122 (4T, 75% WR).
  - rsi_momentum +Rs 62 (2T).
  - mean_reversion -Rs 52 (4T) — only red strategy.
- **Trend filter on mean_reversion is working**: blocked NLCINDIA SHORTs
  multiple times in the battery logs.

## Code shipped tonight (active for tomorrow's 09:00 auto-restart)

1. **Peak-giveback exit** (`core/risk_manager.py: TrailingStop`).
   Arms at 1.5R, exits when current_R has fallen 35% from peak_R.
   Independent of the existing 0.3% price-trail.
   - MEESHO simulation: would have exited around +Rs 180 instead of +Rs 71.
   - Config: `risk.peak_giveback_enabled / peak_giveback_arm_rr / peak_giveback_pct`.

2. **Carryover SL -> break-even** (`trading_agent.py: _maybe_recompute_carryover_sl`).
   First market-open cycle each day, any position whose entry_time was
   on a prior session has its SL tightened to MAX(current_sl, entry_price)
   for LONGs (or MIN for SHORTs).
   - CROMPTON simulation: would be a break-even exit instead of -Rs 166.
   - Config: `risk.carryover_sl_to_breakeven`.

3. **Per-strategy circuit breaker** (`trading_agent.py:
   _update_strategy_breaker_state` / `_strategy_is_suspended`).
   3 consecutive losses OR -1% of capital from a SINGLE strategy =>
   suspend that strategy for the rest of the day. Other strategies
   keep trading normally.
   - Config: `risk.strategy_max_consec_losses / strategy_daily_loss_pct`.

4. **Window cap** (`trading_agent.py: _pre_trade_safety_checks`).
   Max 4 entries per rolling 5-min window. Prevents correlated cluster
   risk like today's 13:45-13:46 IFCI+ICICIPRULI burst.
   - Config: `risk.max_opens_per_window / opens_window_minutes`.

5. **Health JSON heartbeat** (`trading_agent.py: _write_health_json`).
   Atomic write to `logs/health.json` every heartbeat.
   `tools/health_check.py` is a watchdog probe with 4 exit codes:
     - 0 = healthy
     - 1 = file missing
     - 2 = stale heartbeat (>10 min by default)
     - 3 = PnL below floor
   Designed for cron / Windows Scheduled Task / shell-loop watchdogs.

6. **Daily-loss kill-switch verification**: 11 new tests cover the
   exact threshold, latching across wins, day-rollover reset,
   drawdown halt independence, custom 2% limit.

## Test results
+60 new tests this session, full suite: **634 passed**.
- 8 alert retry + spool (last session, included for completeness)
- 11 peak-giveback (incl. MEESHO scenario simulation)
- 8 health.json + health_check
- 11 kill-switch verification
- 8 strategy breaker
- 6 window cap
- 8 carryover SL recompute (incl. CROMPTON scenario)

## Strategy hot-path audit
See `logs/strategy_hot_path_audit_2026-05-07.md` for detail.
TL;DR: every strategy does `data.copy()` + full-history rolling per
bar. `supertrend_follow` has a Python `for i in range(len(df))` loop
with chained `.iloc[i]` reads/writes — by FAR the dominant cost.
Estimated 5-10x backtest speedup once refactored, but Phase 1b/c
work for a follow-up session (needs paired snapshot tests; high
regression risk).

## Backtest battery (in flight)
9 config variants x 9 mid-cap symbols x 30 days x 5min, started 16:14 IST.
- Symbols (1 dropped due to no yfinance data): IFCI, CROMPTON, NLCINDIA,
  ZYDUSWELL, POLICYBZR, HDFCLIFE, TATAMOTORS + 2 more from initial 10.
- 12,975 bars total.
- Variants: baseline / xgb-trend-on-5pct / xgb-trend-on-15pct /
  mean_rev TP-60 / TP-100 / Z-1.8 / Z-2.0 / no-mean_reversion / xgb-only.
- First variant ETA ~50 min, total 5-6 hrs.
- Output: `logs/backtests/20260507T161436/`.

## Suggested config diff for tomorrow (decision after battery)
- Peak-giveback: ENABLED (defaults: arm 1.5R, giveback 35%).
- Carryover-SL-to-break-even: ENABLED (no-op when no carryover).
- Strategy breaker: ENABLED (3 consec OR -1%).
- Window cap: 4 / 5min (loose, just a backstop).
- Mean_reversion trend filter: KEEP ON (already in code).
- TP placement / Z-entry / strategy whitelist: WAIT for battery results.

## Files added/modified
- `core/risk_manager.py` — peak-giveback fields on TrailingStop,
  config plumb-through in RiskManager.
- `trading_agent.py` — health JSON, window cap, strategy breaker,
  carryover SL recompute, peak-giveback wiring in _check_position_exits.
- `config.yaml` — 4 new risk.* sections.
- `tools/health_check.py` (new) — watchdog probe.
- `tools/overnight_backtest_battery.py` (new) — battery runner.
- `backtest_ensemble.py` — `run()` accepts pre-fetched market_data.
- 6 new test files in `tests/`.
- `logs/strategy_hot_path_audit_2026-05-07.md` (new).

---

## 2026-05-08 00:55 IST — Battery Results Applied to Production Config

The 9-variant overnight battery (10 mid-caps, 30d, 5min) finished at 23:54
the night before. Results applied to `config.yaml` for tomorrow's run:

### Winners (vs C1 baseline -Rs 69)

| Variant            | Change                                    | PnL     | WR    | PF   | MaxDD |
|--------------------|-------------------------------------------|---------|-------|------|-------|
| C1 baseline        | (current prod config)                     | -Rs 69  | 37.9% | 0.93 | 5.01% |
| **C2 (winner)**    | xgboost trend_filter_pct = 5.0            | +Rs 164 | 45.2% | 1.19 | 2.73% |
| C6                 | mean_reversion entry_z_score 1.6 -> 1.8   | +Rs 107 | 43.3% | 1.12 | 4.25% |
| C7                 | mean_reversion entry_z_score 1.6 -> 2.0   | +Rs 98  | 43.3% | 1.11 | 4.25% |
| C8                 | mean_reversion DISABLED                   | +Rs 71  | 37.0% | 1.08 | 5.50% |

### Losers (do NOT apply)

| Variant       | Change                                  | PnL     | Why it lost                                |
|---------------|-----------------------------------------|---------|--------------------------------------------|
| C3            | xgboost trend filter at 15% (too loose) | -Rs 136 | Loose filter let weak signals through.     |
| C4            | mean_rev TP at 60% reversion            | -Rs 210 | Greedy quick exits, R:R collapsed.         |
| C5            | mean_rev TP at 100% reversion           | +Rs 7   | TP rarely reached -> trailing stops eat win|
| C9            | xgboost-only ensemble                   | -Rs 105 | High WR (46%) but R:R 0.97 — exits too fast|

### Applied

1. `strategies.xgboost_classifier.trend_filter_pct: 5.0`  (was None / off)
2. `strategies.mean_reversion.entry_z_score: 1.8`  (was 1.6)

### Expected combined effect (additive estimate)

C2 (+233 vs baseline) + C6 (+176 vs baseline) acting on disjoint strategies
should yield roughly +Rs 200-300 over 30 days vs baseline (some signal
overlap discounted). On Rs 32k starting capital this scales to ~Rs 700-1000
over 30 days, i.e. **Rs 25-35 per trading day** as the realistic best-case
projection from this battery. NOT Rs 1000/day. See live track record below.

### Live track record (8 days, 81 trades)

| Date  | Trades | PnL      |
|-------|--------|----------|
| 04-27 | 11     | -Rs 124  |
| 04-28 | 14     | -Rs 71   |
| 04-29 | 1      | +Rs 1    |
| 04-30 | 6      | -Rs 129  |
| 05-04 | 4      | -Rs 2    |
| 05-05 | 15     | +Rs 257  |
| 05-06 | 15     | -Rs 321  |
| 05-07 | 15     | +Rs 270  |

Total: -Rs 119 over 8 days. Best day +270, worst day -321.
Win-day rate: 3/8 = 37.5%. Daily PnL std-dev ~Rs 200.

### What was NOT changed

- `mean_reversion.tp_reversion_pct` stays at 0.80 (battery confirms 60 %
  is too greedy and 100 % is too loose).
- `mean_reversion.trend_filter_pct` stays at 5.0 (already on, in code).
- All V2 hardening flags stay ON (peak-giveback, strategy breaker, window
  cap, carryover SL recompute, opening lockout, kill-switches).
- All 6 strategies stay active. C8 says removing MR helps slightly, but
  C6 (tightening MR) helps more. We tighten, not remove.

### Verification
- `python -m pytest tests/ -q` -> 620 passed, 0 failed.
- Strategy class init confirms config loads: XGB.trend_filter_pct=5.0,
  MR.entry_z_score=1.8, MR.tp_reversion_pct=0.8.

