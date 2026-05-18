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

## Bypass discipline (added 2026-05-19, post-verdict)

**Cap: 3 `freeze-bypass:` commits per freeze cycle.** The 4th bypass
is not allowed — instead, the freeze is declared over and an explicit
unfreeze decision (with documentation in `changes_done_*.md`) must
happen. This cap exists because the verdict-flagged failure mode is
**bypass abuse**: first bypass genuinely necessary, second "basically
the same kind of thing", by bypass five the contract is meaningless.

A running bypass ledger lives at the bottom of this document
(§Bypass ledger). Every bypass commit appends one row.

Two distinct kinds of bypass exist:

1. **Behaviour-preserving bypass** — fixes a bug, restores intended
   behaviour, adds observability. Counts against the cap but is
   normally accepted on merit.
2. **Contract change bypass** — alters the freeze contract itself,
   e.g. activating `FREEZE_v2.1_revision.md`. Counts against the cap
   and requires a separate one-line entry in `changes_done_*.md`.

If a bypass is purely operational (deploy script, CI fix, dependency
update, etc.) and touches **no frozen file**, it does NOT count
against the cap. The cap is for bypasses that interact with frozen
behaviour or the contract.

## Kill criterion (added 2026-05-19, post-verdict)

> **If cumulative Phase A realised PnL is below −Rs 3,000 by Friday
> 2026-05-29 (end of Week 2), halt the freeze.**

"Halt" means:

1. Daemon stops opening new positions (set `live_mode: false` if not
   already, raise `risk.max_open_positions: 0`).
2. Open positions are managed to exit on their own SL/TP/intraday-time
   stop — no forced flatten unless an open position is itself blowing
   up.
3. Write `docs/halt_phase_a_2026-MM-DD.md` documenting the cumulative
   PnL, the per-strategy attribution, the contaminated-day exclusions
   (if any), and the decision.
4. Move to Branch C (postmortem) per §"Forward-looking verdict /
   Branch C" — write `docs/postmortem_phase_a.md` from the template
   before any further action.

Why this number, specifically:

- Phase A started 2026-05-12 at PnL 0 and was at −Rs 1,132 on
  2026-05-18 across 17 closed trades.
- At the realistic projection of 3 trades/day × 21 trading days = 63
  trades, a worst-case continuation of the Week 0 expectancy
  (−Rs 51/trade) would put us at roughly −Rs 3,200.
- −Rs 3,000 is approximately 3 % of the paper capital base
  (~Rs 100k), which is the threshold below which the system has
  exhausted any plausible noise budget and is in the "the
  configuration genuinely has no edge" region.
- This is a **pre-committed** halt threshold. The point of pinning
  it now, while calm, is that future-me on a bad Friday afternoon
  cannot move the goalpost.

The capital base referenced is the **paper** capital. Real-money
deployment is governed separately (Stage 2.1 has not happened).

## Disagreement rules — battery vs live (added 2026-05-19)

The exit-criteria gate "battery agreement on ≥3 strategies" assumes
the battery is a fair reference. If battery and live disagree, the
direction of the disagreement determines the interpretation:

| Pattern | Likely cause | Action |
|---|---|---|
| Battery PF > live PF by > 30 % | **Code parity broken** — backtest and live engine compute different things. Or: live operational issues eating edge (slippage, late fills, missed exits). | Treat as a bug. Find the parity break before drawing any edge conclusion from either dataset. Do NOT trust live as "the real number." |
| Battery PF < live PF by > 30 % | Live window hit a favorable regime that the 90-day battery averaged across. **Live looks good for the wrong reason.** | Trust the longer instrument (battery). Do not declare edge from live alone. |
| Adjacent battery runs (same config, sliding window) disagree by > 30 % | **Battery itself is regime-fragile.** Point estimate is meaningless. | Demand bootstrap CIs from battery output. The lower-CI, not the point PF, is what gates Phase A. |
| Battery and live agree within ±20 % | Working as designed. | Proceed against exit criteria. |

A weekly variance row in `logs/drift/weekly_variance.csv` (one row
per Friday) makes this trend visible across the freeze window.
Schema: `week_ending, strategy, live_n, live_pf, battery_pf, delta_pct, within_band`.

## Capital-add lock (added 2026-05-19)

The paper capital base **stays at Rs 100,000 for the entire freeze
cycle**, regardless of P&L. Adding capital because "things are going
well" is a freeze violation — Kelly scales with capital, position
sizes change, slippage profile changes. The statistical signal you
were collecting gets contaminated by a scale change you can't
disentangle.

If real-money deployment becomes possible at the June-8 decision
gate, that is a separate Stage 2.1 decision with its own contract.
It does not happen mid-freeze.

## Contingency activation (added 2026-05-19)

If the trade-count trajectory says the 100-trade gate is mathematically
unreachable, the activation of `docs/FREEZE_v2.1_revision.md`
(Branch 1: battery-primary, live-parity) is **pre-authorised** by the
trigger rules in that file. The activation is a mechanical SQL count
+ a single PR replacing §Exit criteria. It is NOT a freeze break.

## Operator commitments — extended (added 2026-05-19)

In addition to the §Operator commitments above:

1. **Daily 10-min EOD review** of `logs/diagnostics/eod_YYYY-MM-DD.md`.
   Two-line append to `docs/freeze_log_weekN.md` per day:
   long/short trade counts and PnL today; any audit checkpoint that
   went RED or AMBER.
2. **No watching the diagnostic during market hours.** It biases the
   next decision. Open it only after 16:00 IST.
3. **No reading external algo-trading content during the freeze.**
   Ideas during a freeze are noise.
4. **Friday weekly review** — 30 minutes, write the per-strategy
   verdict table to `docs/freeze_weekN_review.md`. No commentary,
   no decisions, just numbers on a page.
5. **AI assistant boundary** — every session that touches the trader
   begins with `FREEZE_v2.1.md` in context. Most assistants respect
   the contract when made visible.

## Bypass ledger

Format: `YYYY-MM-DD | commit-sha | one-line justification`.
Three slots max — adding a 4th means an explicit unfreeze decision.

```
(empty as of 2026-05-19; first bypass appends here)
```

---

*Authors: Trading Agent dev (Subhanda) + Claude.*
*Freeze starts: 2026-05-18, evening session. Tag: `freeze-v2.1`.*
*Last revised: 2026-05-19 (post-verdict additions; no frozen behaviour touched).*
