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
2026-05-20 | longs N=0 PnL=Rs 0 | shorts N=0 PnL=Rs 0
2026-05-20 | audit: GREEN — zero-trade session; 575 BUY signals all rejected (467 long_regime:bear_high_vol, 110 opening_lockout); late-session XGB shift (SELL/BUY ratio 0.0 -> 0.8 between 11:06 and 16:00) produced 3 SELL ensembles (MRPL, DELHIVERY, UPL) after 15:00, all blocked by intraday-exit gate; daemon 34h uptime, 0 exceptions
```

---

## Pre-Friday-review staging area (operator-only)

This block is for capturing facts as they accumulate so Friday's
30-minute review can be a table-read, not an investigation.

### Cumulative numbers (auto-updates daily)

| Metric | Mon 05-18 | Tue 05-19 | Wed 05-20 | Thu 05-21 | Fri 05-22 (review) |
|---|---:|---:|---:|---:|---:|
| Day P&L (Rs) | -12 | -203 | 0 | — | — |
| Cumulative P&L since freeze (Rs) | -12 | -216 | -216 | — | — |
| Trades closed | 1 | 2 | 0 | — | — |
| Cumulative trades since freeze | 1 | 3 | 3 | — | — |
| Long trades | 0 | 0 | 0 | — | — |
| Short trades | 1 | 2 | 0 | — | — |
| Open positions at EOD | 0 | 0 | 0 | — | — |
| Drawdown % | 0.96 | 1.12 | 1.12 | — | — |
| Regime detected | bear_high_vol | bear_high_vol | bear_high_vol | — | — |
| Audit verdict | GREEN | GREEN | GREEN | — | — |
| Daemon exceptions | 0 | 0 | 0 | — | — |
| Heartbeat email received | n/a (cron not yet installed) | n/a | n/a | — | — |

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

### 2026-05-20 EOD observations (additions, not edits)

6. **`supertrend_follow` bootstrap CI tightened past 1.0.**
   - 2026-05-19 EOD diagnostic: PF lower-95-CI [0.08, 1.12] →
     "inconclusive (CI straddles 1.0)".
   - 2026-05-20 EOD diagnostic (`logs/diagnostics/eod_2026-05-20.md`):
     n=13, PF 0.27, **upper-95-CI = 0.93 < 1.0 → "no edge"**.
   - Statistically, supertrend_follow now meets the freeze
     contingency's strict-kill criterion (upper-CI < 1.0), not just
     the soft-flag criterion. **No action this week** — Friday
     review decides whether to weight-zero it (= bypass slot 1 of 3,
     since `config.yaml` strategy block is frozen) or wait for
     Week 2 mid-freeze health check (Fri 2026-05-29) per
     `docs/freeze_contingencies.md` §C2.
7. **Long-side famine continued.**
   - 5/5 sessions since freeze start: regime detector pegged
     `bear_high_vol` for the entire session. **All 19 trades in
     the 7d window are shorts.** Long-side has produced zero
     trades since 2026-05-09 (11 trading days).
   - Today's signal_audit: 575 BUY signals rejected with
     `long_regime:bear_high_vol` (467) or `opening_lockout` (110).
     Zero longs accepted.
   - Notably new today: XGB classifier shifted from BUY-only
     (2308 BUY / 0 SELL / 805 HOLD at 11:06) to mixed
     (122 BUY / 102 SELL / 721 HOLD at 16:00). 3 SELL ensembles
     produced (MRPL conf 0.629, DELHIVERY conf 0.775, UPL
     conf 0.664) after 15:00 IST — all blocked by intraday-exit
     gate. **The pattern of "model finally sees shorts but only
     in the dead hour" is the new shape to watch for Thursday.**
   - Friday question carried forward (was item 1, restated):
     is the regime detector mis-tuned, or is the market genuinely
     bear-vol all session every session?
8. **Sector concentration in Financials grew vs 2026-05-19.**
   - 7d window: 7 of 19 trades (37 %) in Financials supersector.
     Financials PnL Rs -605 = **51 % of the 7d losses**.
   - Same pattern flagged on 2026-05-19 (9/25 trades, Rs -694 in
     10d). Trend is intensifying — the few new trades in the
     freeze window have concentrated further in the same
     supersector.
   - Operator question for Friday: should the existing
     `max_per_supersector` cap (concurrent open positions) be
     extended to also cap cumulative daily entries per supersector?
     Would be a `config.yaml` change → 1 bypass slot.
9. **Zero-trade session (today) is the cleanest signal yet.**
   - 0 closed trades on a session that produced 577 signals
     (1.7× day-2's signal rate). The risk gates and the
     intraday-exit timing are doing exactly what the freeze
     contract designed them to do.
   - Day's contribution to cumulative P&L: ₹0. Headroom against
     the −Rs 3,000 kill criterion is unchanged at ~Rs 2,400.

### What did NOT happen (no-ops worth noting)

- No daemon crash on either VM.
- No broker authentication error.
- No orphaned SL-M orders.
- No drawdown-tier escalation (still NORMAL all week).
- No circuit-breaker trip (`consecutive_losses` at 2, threshold not
  breached).
- No commit to `config.yaml`, `trading_agent.py`, or any strategy
  file since freeze start (= contract held).
- `freeze-bypass:` ledger (reconciled 2026-05-20 11:00 IST per commit
  `4276962`; ground truth lives in `docs/FREEZE_v2.1.md` §Bypass ledger):
  | Date | Commit | Slot? | Description |
  |---|---|---|---|
  | 2026-05-19 | 9cd7acd | audit-only (no frozen file) | observability: HTML email rendering + CI green-up |
  | 2026-05-19 | 868d5ad | audit-only (no frozen file) | observability: freeze-v2.1 pre-commitments + diagnostic stats |
  | 2026-05-19 | 9772e4d | audit-only (no frozen file) | freeze-bypass: battery throughput + cloud progress tool |
  | 2026-05-20 | 5934960 | audit-only (no frozen file) | freeze-bypass: battery infrastructure hardening (perf + functionality) |
  - Bypass cap: **0 / 3 used** in this freeze cycle. All four
    `freeze-bypass:`-tagged commits touched only research / observability /
    operational files; per `docs/FREEZE_v2.1.md` lines 135-138 these do
    NOT consume a slot. Cap test is "touches a frozen file" (strategies,
    risk gates, trading agent, sizing, config.yaml strategy/risk blocks,
    or model artefact). All three slots remain available.

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
| Trade count vs contingency threshold (≥10 trades = stay on plan; <10 = activate `FREEZE_v2.1_revision.md`) | pending — Wed evening count still = 3 (zero-trade Wed session); on track for ~5–7 by Friday, **below the ≥10 threshold → contingency Branch 1 likely activates** |
| Battery first-completion check | pending |
| Operator-skipped-days count (per `docs/freeze_contingencies.md` §C6) | 0 so far |
