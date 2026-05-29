# freeze-v2.1 Revision — Branch 1 Contingency (battery-primary, live-parity)

> **Status:** PRE-WRITTEN 2026-05-19 00:55 IST, under calm conditions, in
> response to the 2026-05-18 external verdict that flagged
> "**low-trade-count failure is the most probable Phase A outcome**."
>
> **This document is NOT active.** It activates only when triggered by
> the data, per the rules in §Trigger. Sitting in `main` unmerged-as-policy
> so future-me cannot rationalise around it on a panicked Friday afternoon.

---

## Why this exists

The original `FREEZE_v2.1.md` exit gates were drafted on the evening of
2026-05-18, looking back at a 22-trade window where `supertrend_follow`
was generating **77 % of trade volume**. Killing `supertrend_follow` was
the right call, but the **100-trade gate was implicitly calibrated to a
system that no longer exists**.

### Naive trade-rate projection under freeze-v2.1

| Component | Trades/day | Note |
|---|---:|---|
| Pre-freeze baseline (22 trades / 7 days) | 3.1 | with supertrend |
| Remove `supertrend_follow` (was 17 of 22) | −2.4 | |
| Re-enable longs in 2 more regimes | +1.5 | optimistic |
| `scanner.top_n` 200 → 300 | +0.5 | breadth |
| **Realistic projection** | **2 – 4** | |
| **Best case** | **6** | |

| Scenario | Trades/day | 21-day total | Hits 100? | Hits 30/strategy? |
|---|---:|---:|---|---|
| Pessimistic | 2 | 30 | No | No (1 strat at ~15) |
| Realistic | 3 | 45 | No | No |
| Optimistic | 4 | 60 | No | Maybe 1 strat at 30 |
| Best case | 6 | 90 | Borderline | Maybe 2 strats at 30 |

The 100-trade gate from `FREEZE_v2.1.md` requires ~7 trades/day — **above
every defensible projection**. Plan for the gate being mathematically
unreachable in 21 days from the start.

### Reframing — battery is the better instrument

The Nifty 500 × 90-day battery produces **~200+ trades per variant per
run**. Per-strategy that's 30–80 decisive trades from a single battery
run, vs. 5–15 from live in the same window. Under freeze-v2.1 the
**battery is structurally the better statistical instrument**. The
original gates treat live as the primary evidence; this revision
inverts that — without compromising freeze discipline, because no
frozen file is touched.

---

## Trigger — when does this revision activate

This revision merges into `FREEZE_v2.1.md` (i.e. becomes active) when
**any one** of the following fires:

| Checkpoint | Threshold | Action |
|---|---|---|
| **End of Week 1 (Fri 2026-05-22 EOD)** | < 15 closed trades since freeze start | Merge revision immediately; revert is impossible. |
| **End of Week 2 (Fri 2026-05-29 EOD)** | < 40 closed trades since freeze start | Merge revision; original gates abandoned for this freeze cycle. |
| **End of Week 3 (Fri 2026-06-05 EOD)** | < 70 closed trades since freeze start | Merge revision; June-8 decision uses revised gates. |

Decision is mechanical, not interpretive. Counts come from
`SELECT COUNT(*) FROM trades WHERE exit_time >= '2026-05-18'` against
`data/trading_agent.db`.

If none of the triggers fire, the original `FREEZE_v2.1.md` gates
apply and this document expires unused on 2026-06-08.

---

## Revised exit criteria (active only when triggered)

Lift the freeze when **all four** are met:

1. **≥ 3 battery runs** on Nifty 500 × 90 d × 5-min showing
   **PF lower-95-CI > 1.0** for at least one strategy. CIs are
   bootstrap-derived from per-trade PnL; report the run id, the
   universe, the window, and the per-strategy CI band.
2. **Live ≥ 40 paper trades** total on the frozen config (relaxed
   from 100). Across whichever strategies cross **N ≥ 20** decisive
   trades, **live PF must be within ±20 % of battery PF** for the
   same strategy on the comparable battery window.
3. **No live operational regressions** during the freeze:
   daemon uptime ≥ 95 % of market minutes, zero orphan SL-M orders,
   zero crash loops, zero "alerts didn't fire" incidents.
4. **Tooling parity** unchanged from original — `tools/audit_checkpoint.py`
   and the EOD diagnostic emit per-strategy reports without manual SQL.

### Why these thresholds, specifically

- **Battery PF lower-CI > 1.0 (not point-PF):** the verdict's "small-sample
  trap" warning. A 22-trade live window with PF 1.20 (May 12 reading)
  decayed to PF 0.36 (May 18 reading). A 200-trade battery run with a
  lower-CI that excludes 1.0 is genuinely incompatible with "no edge."
- **Live ±20 % of battery (not ±15 %):** the original ±15 % gate was
  calibrated to ≥30 trades per strategy. At N=20 the bootstrap CI of
  live PF is wide enough that demanding ±15 % is mathematically
  punitive. ±20 % is the correct band for the data we'll actually have.
- **40 live trades (not 100):** sufficient to detect "code parity is
  broken" (live ≠ battery on the same config) but does NOT pretend to
  establish edge on its own. Edge evidence comes from the battery.
- **Uptime ≥ 95 %:** the verdict's operational-reliability gate. If
  the trader was alive < 95 % of market minutes, neither live data
  nor parity claims are credible.

---

## What this revision does NOT change

- **Strategy code.** Untouched — full lockdown unchanged.
- **Ensemble / risk / sizing / regimes.** Unchanged.
- **`config.yaml` frozen rows.** Unchanged.
- **ML model artefact.** Still frozen (no retrain).
- **Bypass discipline.** Still 3 bypasses max per freeze cycle.

The revision changes **the interpretation of the experiment**, not the
experiment itself.

---

## Honest costs of activating this revision

Worth saying out loud — this is not a free move:

1. **We're claiming the battery is fair.** If the battery's 90-day
   window doesn't include a comparable regime mix to the freeze window,
   the agreement gate fails for the wrong reason. Mitigation: the
   battery-vs-live disagreement rules in `FREEZE_v2.1.md` §Disagreement
   tell us how to interpret a mismatch.

2. **40 trades is genuinely thin.** Per-strategy bootstrap CIs at N=20
   are wide — possibly so wide that "no strategy has lower-CI > 1.0"
   is the default outcome. That answer is still informative (it says
   "this configuration does not produce enough live trades to claim
   edge from live data alone") but it's an answer the operator may
   not want to hear.

3. **We may be over-trusting the battery.** Backtest-vs-live drift is
   a real risk. If the comparator (drift harness in `logs/drift/`)
   hasn't been built and run by the time this revision activates, the
   ±20 % gate is checking against a number we haven't validated. Build
   the drift harness **before** week 2.

4. **The contingency may itself need to extend.** If by Week 3 the
   battery has produced < 3 runs (backtester VM down, queue scheduler
   failed, etc.), even this revision can't close. That's a Branch C
   (postmortem) trigger — see `docs/postmortem_phase_a_template.md`.

---

## What I will write when (if) this activates

The activation procedure is mechanical:

1. On the triggering Friday, compute the trade count via the SQL above.
2. If below threshold: open a PR replacing the `Exit criteria` section
   of `FREEZE_v2.1.md` with §"Revised exit criteria" from this file.
3. Commit message: `freeze-bypass: trigger Branch 1 revision — N trades`
   (the bypass cap allows this; it's a calibration change, not a
   behaviour change, but it does change the contract so it counts).
4. Add a one-line entry to `changes_done_2026-MM-DD.md` for the day:
   "Branch 1 contingency activated — battery-primary gates."
5. **No other change ships in the same commit.** Activation is a
   single, clean action.

After activation, the next operating cycle proceeds against the
revised gates. The original gates are archived in this file for the
audit trail; they do not return.

---

## Sibling commitments (pre-written in the same spirit)

These exist for the same reason — to take decisions in cold blood now
so future-me can't improvise badly later:

- `docs/FREEZE_v2.1.md` §Disagreement — how to read battery-vs-live
  divergence (added 2026-05-19).
- `docs/FREEZE_v2.1.md` §Kill — explicit halt criterion
  (`if cumulative_phase_a_pnl < -X by Y, halt`).
- `docs/FREEZE_v2.1.md` §Bypass — 3 bypasses max; the 4th means the
  freeze is over and an explicit decision is required.
- `docs/backtester_vm_runbook.md` — the response when the backtester
  isolation guard fires (do not retry until rooted).
- `docs/postmortem_phase_a_template.md` — if Branch C fires, this is
  the form the postmortem must take. No skipping the questions.
- `docs/freeze_contingencies.md` — the operational scenarios beyond
  trade-count (silent operational failures, statistical artifacts,
  battery-vs-live disagreement, frozen-model calibration drift,
  black-swan contamination, capital-add temptation).
- `docs/freeze_observability_extensions.md` — daily-checklist and
  weekly-review schemas, plus the heartbeat-email contract.

Read these together. The freeze is not the operator deciding what to
do mid-flight — it's the operator pre-committing to decisions while
calm and executing them mechanically when triggered.

---

*Author: Trading Agent dev (Subhanda) + Claude.*
*Drafted: 2026-05-19 00:55 IST. Trigger window: 2026-05-22 / 05-29 / 06-05.*
*Tag (when active): `freeze-v2.1-rev1`. Until then this file sits dormant.*
