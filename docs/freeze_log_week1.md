# Freeze-v2.1 — Week 1 running notes (2026-05-18 → 2026-05-22)

> Created retroactively on 2026-05-19 post-EOD. Per
> `docs/freeze_observability_extensions.md` daily checklist (10 min, after 16:00 IST):
> append **two** lines per trading session — one trade-flow line, one
> audit-verdict line. No commentary. No decisions until Friday's
> weekly review.
>
> Friday 2026-05-22 review will reference this file directly.

---

## Daily entries

```
2026-05-18 | longs N=0 PnL=Rs 0 | shorts N=1 PnL=Rs -12
2026-05-18 | audit: GREEN — first session of freeze; one stop_loss (CDSL); daemon healthy, no exceptions, regime=bear_high_vol all session
2026-05-19 | longs N=0 PnL=Rs 0 | shorts N=2 PnL=Rs -203
2026-05-19 | audit: GREEN — two stop_loss (VOLTAS -190, SWIGGY -13); daemon healthy; 10,901 BUY signals gated by long_regime:bear_high_vol; regime detector pegged bear_high_vol all session
```

---

## Pre-Friday-review staging area (operator-only)

This block is for capturing facts as they accumulate so Friday's
30-minute review can be a table-read, not an investigation.

### Cumulative numbers (auto-updates daily)

| Metric | Mon 05-18 | Tue 05-19 | Wed 05-20 | Thu 05-21 | Fri 05-22 (review) |
|---|---:|---:|---:|---:|---:|
| Day P&L (Rs) | -12 | -203 | — | — | — |
| Cumulative P&L since freeze (Rs) | -12 | -216 | — | — | — |
| Trades closed | 1 | 2 | — | — | — |
| Cumulative trades since freeze | 1 | 3 | — | — | — |
| Long trades | 0 | 0 | — | — | — |
| Short trades | 1 | 2 | — | — | — |
| Open positions at EOD | 0 | 0 | — | — | — |
| Drawdown % | 0.96 | 1.12 | — | — | — |
| Regime detected | bear_high_vol | bear_high_vol | — | — | — |
| Audit verdict | GREEN | GREEN | — | — | — |
| Daemon exceptions | 0 | 0 | — | — | — |
| Heartbeat email received | n/a (cron not yet installed) | n/a | — | — | — |

### Observed-but-flagged (not actioned)

These are facts the daily checklist surfaced. They are **not decisions
to act on** during Week 1 (5-day data is statistical noise per the
freeze contract). They are inputs for Friday and beyond.

1. **Long-side famine (high signal, zero fires).**
   - 5/5 sessions since freeze start: regime detector emitted
     `bear_high_vol` for the entire session.
   - Today's signal_audit: 10,901 BUY signals from `xgboost_classifier`
     rejected with `long_regime:bear_high_vol`; 0 longs accepted.
   - 10-day diagnostic (`logs/diagnostics/profit_diagnostic_20260519_155644.md`):
     **25 of 25 closed trades are SHORTS**. Long-side has produced
     zero trades for the entire 10-day window (since 2026-05-09).
   - Decision deferred to Friday weekly review per freeze contract.
     Question for Friday: is `bear_high_vol` genuinely the regime
     (= the market is bearish-volatile) or is the detector mis-tuned
     (= a `nifty_trend` / `india_vix` threshold issue)?
2. **EOD diagnostic auto-verdict: FAIL (rolling 5-day window).**
   - `logs/diagnostics/eod_2026-05-19.md` reports rolling PF 0.32
     against floor 1.00.
   - **Freeze contract is explicit: the daily EOD verdict does NOT
     trigger action.** The kill criterion is "cumulative P&L <
     -Rs 3,000 by 2026-05-29" (per `docs/FREEZE_v2.1.md` §Kill
     criterion); cumulative is currently -Rs 216, well clear.
   - Decision deferred.
3. **`supertrend_follow` flagged KILL by per-strategy verdict.**
   - 16 trades in 5d (18 in 10d), PF 0.36–0.40, PnL Rs -829 to -889,
     Kelly -0.58 to -0.68.
   - PF lower-95-CI is [0.08, 1.12] — bootstrap CI straddles 1.0,
     so the verdict per `freeze_contingencies.md` §C2 is
     "inconclusive (CI straddles 1.0)", NOT a confident kill.
   - PF excluding the max-PnL trade is 0.26 (vs 0.36 with it). The
     "one lucky trade" filter says this is NOT one-lucky-trade.
   - **No action.** A `supertrend_follow` kill would be a
     contract-changing freeze breach. If we revisit at Friday, the
     options are: (a) weight-zero in ensemble (= behaviour-preserving
     bypass under `FREEZE_v2.1.md` §4), (b) wait for Week 2
     mid-freeze health check (Fri 2026-05-29), (c) honour the freeze
     in full and decide June 8.
4. **Per-supersector concentration in Financials.**
   - 10d window: 9 of 25 trades in Financials. PnL Rs -694.
   - Existing supersector cap from the 2026-05-14 fix did NOT block
     entries (cap is on concurrent open positions, not on cumulative
     daily entries). Friday discussion: should the cap also gate
     by daily-entry-count, or is the existing same-time cap
     sufficient? Defer.
5. **Battery (backtester VM) has produced zero variant rankings.**
   - `battery_freeze_v21_20260518T181337` (ad-hoc, started Sun
     23:46 IST) has been running 16+ hours with 0 of 15 variants
     complete. V1/V2 workers are alive and writing.
   - Speed patches (quiet-logger + queue reorder) landed today as
     commit `9772e4d`, but apply only to the NEXT container, not
     the running one.
   - Operator decision pending: kill Sunday's run and let the
     scheduler launch the fast `nifty50_60d` job from the
     reordered queue, OR let Sunday's run finish (could be 24-48+
     more hours). `docs/backtester_vm_runbook.md` §3 covers the
     zero-trade-battery escalation if it persists past Wed morning.

### What did NOT happen (no-ops worth noting)

- No daemon crash on either VM.
- No broker authentication error.
- No orphaned SL-M orders.
- No drawdown-tier escalation (still NORMAL all week).
- No circuit-breaker trip (`consecutive_losses` at 2, threshold not
  breached).
- No commit to `config.yaml`, `trading_agent.py`, or any strategy
  file since freeze start (= contract held).
- `freeze-bypass:` ledger:
  | Date | Commit | Type | Description |
  |---|---|---|---|
  | 2026-05-19 | 9772e4d | behaviour-preserving | battery quiet-logger + cloud progress tool + queue reorder |
  - Bypass cap: **1 / 3 used** in this freeze cycle.

---

## Friday review handover (to be completed by Operator on 2026-05-22)

This block sits empty until Friday EOD. At Friday review:

1. Confirm cumulative numbers in the table above are correct.
2. Build the per-strategy verdict table per
   `docs/freeze_observability_extensions.md` §Weekly review.
3. **DO NOT make decisions** (Week 1 rule).
4. Note any operational issues that DID require action.
5. Pass the file forward to `docs/freeze_log_week2.md` for the next
   week's running notes.

| Pre-decision artefact | Status (Fri 2026-05-22) |
|---|---|
| Per-strategy live-vs-battery table | pending |
| Trade count vs contingency threshold (≥10 trades = stay on plan; <10 = activate `FREEZE_v2.1_revision.md`) | pending — Tue evening count = 3, on track for ~7–10 by Friday |
| Battery first-completion check | pending |
| Operator-skipped-days count (per `docs/freeze_contingencies.md` §C6) | 0 so far |
