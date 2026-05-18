# freeze-v2.1 — Phase A Lockdown (2026-05-18)

> **Intent**: lock the trading agent's *trading behaviour* for **3 weeks** while
> a high-volume battery validates it on a separate VM. No new strategies, no
> new gates, no new sizing tweaks. Only **bug fixes** and **observability**
> are allowed past this line.

---

## Why this freeze, and why now

Over the 7-day Phase A paper window (2026-05-12 → 2026-05-18) the agent
realized **−Rs 1,132** across 17 closed trades, with the system's own EOD
diagnostic flagging `supertrend_follow` as responsible for **~79 %** of the
loss (`PF 0.35`, `WR 35.3 %`, `Kelly −0.642`, PnL −Rs 896).

The remaining strategies have **too few trades for a verdict** (1–3 closes
each). We can't make any of those calls until each strategy has at least
~30 decisive trades — which is what this freeze is designed to produce.

The temptation right now is to add more code. We have ~50 ideas in the
backlog. Adding any of them muddies the statistical signal we'll get from
the next 3 weeks. **Freeze first, measure second.**

---

## What is frozen

The freeze applies to:

1. **Strategy code** — `packages/strategies/*.py`. No changes to entry / exit
   logic, indicators, or hyperparameters.
2. **Ensemble + voting logic** — `packages/strategies/ensemble.py`.
3. **Risk-manager rules** — `packages/core/risk_manager.py`. No new gates,
   no new caps, no threshold tweaks.
4. **Sizing logic** — `packages/core/position_sizer.py` / kelly multipliers.
5. **`config.yaml`** strategy + risk blocks — see "Frozen settings" below.
6. **ML model artifact** — `models/xgboost_model.pkl` (the calibrated
   isotonic model trained 2026-05-14). No retrains during the freeze.

## What is NOT frozen (allowed mid-freeze)

- **Critical bug fixes** with the bar being "the daemon would otherwise
  crash, leak orders, or compromise broker funds." Every such patch must
  be paired with a regression test and called out in `changes_done_*.md`.
- **Observability** — additional logging, metrics, audit fields, alert
  routing. Anything that improves our ability to *measure* without
  changing what the agent *does*.
- **Backtester / battery infra** — scripts, runners, parsers, dashboards
  on the new backtester VM. That VM exists specifically to do work
  *outside* the frozen trader.
- **Operational tooling** — `tools/cloud/*`, deploy scripts, log pullers.

## Frozen settings (config.yaml at freeze time)

| Setting | Value at freeze | Why |
|---------|-----------------|-----|
| `strategies.weights.supertrend_follow` | **0.0** | KILL verdict; ~79 % of 7-day loss |
| `strategies.weights.xgboost_classifier` | 1.0 | parity start; calibrated 2026-05-14 |
| `strategies.weights.lstm_price_model` | 1.8 | unchanged |
| `strategies.weights.opening_range_breakout` | 1.3 | unchanged |
| `strategies.weights.vwap_bounce` | 1.2 | unchanged |
| `strategies.weights.moving_average_crossover` | 1.0 | unchanged |
| `strategies.weights.rsi_momentum` | 1.0 | unchanged |
| `strategies.weights.mean_reversion` | 0.8 | unchanged |
| `execution.long_entry_regimes` | bull_low_vol, bull_high_vol, sideways, bear_low_vol | widened from bull-only so longs get evidence |
| `execution.short_selling_regimes` | bear_high_vol, bear_low_vol, sideways | unchanged |
| `scanner.top_n` | **300** | widened 200 → 300 for breadth |
| `risk.max_positions_per_strategy` | 4 | new cap from changes_done_2026-05-14 |
| `risk.max_positions_per_supersector` | 3 | new cap |
| `risk.consecutive_loss_limit` | 3 | unchanged |
| `risk.daily_loss_limit_pct` | 2.0 % | unchanged |

All other risk gates (cooldowns, blackouts, intraday-regime overlay,
opening-range lockout, etc.) **stay on**. The freeze is *more* discipline,
not less.

## Exit criteria (when does the freeze lift)

Lift the freeze when **all four** are met:

1. **≥ 100 closed paper trades** on the frozen config (currently 17 of 100).
2. **Per-strategy verdict** — each non-killed strategy has ≥ 30 decisive
   trades, with a 7-day rolling profit factor reported in EOD diagnostics.
3. **Battery agreement** — the backtester VM has produced ≥ 3 independent
   battery runs on the frozen config across the Nifty 500 universe, and
   the live paper agent's per-strategy stats are within **±15 %** of the
   battery's expectation for at least 3 strategies.
4. **Tooling parity** — `tools/audit_checkpoint.py` and the EOD diagnostic
   produce strategy-level reports without manual SQL.

Target date: **2026-06-08** (3 weeks from freeze). Slipping is fine as
long as the gates above are met before any unfreeze.

## Operator commitments during the freeze

- **No config edits to frozen rows** above. If you spot something obvious,
  open a `freeze-violation-candidate-*.md` note in `docs/` and we discuss
  before merging.
- **Daily EOD review** of `logs/diagnostics/eod_YYYY-MM-DD.md` (or the
  audit checkpoint). Annotate any surprise behaviour in the same file.
- **Weekly battery review** of the latest backtester output and a
  paper-vs-battery delta table.

## Tag

This commit is tagged `freeze-v2.1`. Any future commit that touches a
frozen file MUST include `freeze-bypass: <one-line justification>` in
the commit message body. Otherwise the change should wait until the
freeze lifts.

---

*Authors: Trading Agent dev (Subhanda) + Claude.*
*Freeze starts: 2026-05-18, evening session. Tag: `freeze-v2.1`.*
