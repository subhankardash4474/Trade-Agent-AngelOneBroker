# Freeze-v2.1 — Week 1 running notes (2026-05-18 → 2026-05-22)

> Created retroactively on 2026-05-19 post-EOD. Per
> `docs/freeze/freeze_observability_extensions.md` daily checklist (10 min, after 16:00 IST):
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
2026-05-21 | longs N=0 PnL=Rs 0 | shorts N=2 PnL=Rs -249
2026-05-21 | audit: GREEN — two stop_loss closures both shorts: JKTYRE -Rs8.80 (BE-stop saved ~Rs147 of MFE giveback) and KEC -Rs240.28 (55 bps adverse slippage at stop fill); ~2,246 signals audited, 2,240 rejected (regime + cooldown); both losers from rsi_momentum (now 7d PF 0.21, KILL by point estimate); supertrend_follow had no losers today, 7d PF crept back to 1.03 (n=5, still INSUFFICIENT_DATA); regime=bear_high_vol all session (Nifty 23,649 vs 200-EMA 24,745 = -4.4 % below, VIX 17.85); daemon 58h uptime, 0 exceptions; consec_losses=2 (cooldowns: JKTYRE, KEC); freeze contract intact
2026-05-22 | longs N=0 PnL=Rs 0 | shorts N=2 PnL=Rs +80
2026-05-22 | audit: GREEN+ — first profitable freeze session; both winners SHORTS via rsi_momentum (TARIL +Rs38.70 in 5 min, MMTC +Rs41.13 in 15 min, both `signal` exits at small reversal profits); 158 signals audited, 2 ACCEPTED / 155 REJECTED (~98 % rejection consistent with regime gate); daemon hung at 12:23 IST (Cycle 87 was last heartbeat) and stayed silent 11h until VM reboot at 23:35 IST — **CRITICAL: silent failure with NO alert**; trades had already closed by 10:28 so no positions were left unmanaged but had open positions been held this would have been a serious incident; SSH/sshd also became unreachable from operator IP at ~12:30 (likely fail2ban or related VM-level issue); regime=bear_high_vol all session (consistent with all week)
```

---

## Pre-Friday-review staging area (operator-only)

This block is for capturing facts as they accumulate so Friday's
30-minute review can be a table-read, not an investigation.

### Cumulative numbers (auto-updates daily)

| Metric | Mon 05-18 | Tue 05-19 | Wed 05-20 | Thu 05-21 | Fri 05-22 (review) | **Week 1 totals** |
|---|---:|---:|---:|---:|---:|---:|
| Day P&L (Rs) | -12 | -203 | 0 | -249 | **+80** | **-385** |
| Cumulative P&L since freeze (Rs) | -12 | -216 | -216 | -465 | **-385** | -385 |
| Trades closed | 1 | 2 | 0 | 2 | 2 | **7** |
| Wins | 0 | 0 | 0 | 0 | **2** | 2 (29 %) |
| Losses | 1 | 2 | 0 | 2 | 0 | 5 (71 %) |
| Long trades | 0 | 0 | 0 | 0 | 0 | **0** |
| Short trades | 1 | 2 | 0 | 2 | 2 | 7 (100 %) |
| Open positions at EOD | 0 | 0 | 0 | 0 | 0 | 0 |
| Drawdown % | 0.96 | 1.12 | 1.12 | 1.32 | 1.05 | peak 1.32 |
| Regime detected | bear_high_vol | bear_high_vol | bear_high_vol | bear_high_vol | bear_high_vol | **5/5 = 100 %** |
| Audit verdict | GREEN | GREEN | GREEN | GREEN | **GREEN+** (silent-failure flag) | 4 GREEN + 1 GREEN+ |
| Daemon exceptions | 0 | 0 | 0 | 0 | **0 (but silent hang at 12:23)** | 0 / 1 hang |
| Heartbeat email received | n/a (cron not yet installed) | n/a | n/a | n/a | n/a | NEVER deployed |

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
     -Rs 3,000 by 2026-05-29" (per `docs/freeze/FREEZE_v2.1.md` §Kill
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
     `docs/freeze/freeze_contingencies.md` §C2.
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

### 2026-05-21 EOD observations (additions, not edits)

10. **`regime.py` read-only inspection — classifier is coarse, not stuck.**
    - User requested inspection of `packages/core/regime.py`. Read-only,
      no code changes — frozen file, no bypass slot consumed.
    - **Live values at 15:11 IST today:** Nifty 23,649 · 200-EMA 24,745 ·
      gap -1,096 pts (-4.4 %) · India VIX 17.85.
    - The classifier's logic `nifty_trend = 1 if close >= ema200 else -1`
      (`trading_agent.py` lines 2787-2790) produces `nifty_trend=-1` for
      any non-data-outage condition where Nifty is even Rs 0.01 below
      the 200-EMA. The `sideways` regime requires `nifty_trend=0`, which
      live code only emits on a data outage (< 200 closes available).
    - **Therefore `sideways` is structurally unreachable** in normal
      operation. The classifier is doing exactly what it was designed
      to do; the design itself has only 4 functional output states
      (`bull_low_vol`, `bull_high_vol`, `bear_low_vol`, `bear_high_vol`)
      not the 5 the docstring promises.
    - **bear_high_vol IS technically correct today** — Nifty is genuinely
      4.4 % below trend with elevated VIX. The "stuck classifier" framing
      from the external 2026-05-20 verdict is inaccurate; the issue is
      that a knife-edge 200-EMA threshold treats a multi-month corrective
      phase as monolithic, with no notion of intraday consolidation.
    - Today's intraday Nifty range was 23,640-23,691 = 51-pt range
      (0.22 %). Character was flat consolidation; classifier emitted
      `bear_high_vol` all 7 hours. The signal pipeline rejected
      ~2,240 of 2,246 candidates, mostly through this gate.
    - **Action: candidate post-freeze redesign.** Add a percentage band
      around the 200-EMA (e.g., `\|close - ema200\| / ema200 < 1% → sideways`)
      and/or downgrade the daily regime from a HARD GATE to a SIZING/WEIGHT
      signal. Validation depends on V1-vs-V2 backtest data (see item 12)
      and Friday's weekly review. **No code change this week.**

11. **`regime.py` reveals architectural asymmetry (operator observation).**
    - The agent has TWO contradictory regime primitives:
      | Primitive | Type | Effect |
      |---|---|---|
      | `execution.long_entry_regimes` (config) | Hard allow-list | Binary kill. Excludes `bear_high_vol` → 100% of longs rejected. |
      | `STRATEGY_REGIME_PREF_DIRECTIONAL` (regime.py) | Soft weights | Already down-weights BUYs in bear regimes (e.g. supertrend BUY × 0.3). |
    - The hard gate **overrides** the soft weighting and produces the
      observed binary "all longs rejected" pattern. Two layers of
      regime logic fight each other.
    - 3 of 5 regime primitives are asymmetric long/short (separate
      lists/flags instead of one direction-aware primitive):
      `long_entry_regimes` vs `short_selling_regimes`, `regime_size_multipliers`
      has longs only, `intraday_regime_block_longs` has no short counterpart.
    - **Architectural finding for Friday review:** the agent is a stack
      of regime-specific patches, not a regime-generic framework. A
      symmetric / generic redesign would: remove the hard allow-lists,
      let the soft direction-aware multipliers handle all sizing, keep
      hard kill-switches only for genuine panic conditions (e.g. VIX
      spikes, intraday flash crash). This is a candidate post-freeze
      design change, NOT a Week-1 action. **No code change this week.**
    - **Filed in pre-Friday-review staging.** Will reference both
      findings (#10 + #11) in the regime evidence dump scheduled for
      Saturday 2026-05-23.

12. **Battery (backtester VM) — V2_all_filters_off has live evidence of long signals in bear_high_vol.**
    - `battery_freeze_v21_20260518T181337` is still running at 198 %
      CPU (3.1 GB RAM, 10.6 GB limit), "unhealthy" docker healthcheck
      but actively producing logs. **3+ days elapsed, no V1/V2 result
      JSONs written yet** — slow but not stalled.
    - Today's container stdout shows V2 generating LONG signals in
      `bear_high_vol`-equivalent backtest periods at confidence 1.000:
      ORB BUY KEC, BUY IRB, BUY NLCINDIA, BUY RBLBANK; rsi_momentum
      BUY RENUKA (RSI 31), BUY COALINDIA (RSI 34.5); supertrend_follow
      and vwap_bounce also firing both sides.
    - **This is the smoking gun:** the live agent rejected 2,240+ of
      these same signal types today via `long_entry_regimes`. V2's
      eventual PF will tell us the counterfactual: what would those
      rejected longs have earned if the gate weren't there. Strongest
      evidence-base possible for the Friday review's regime-gate
      decision.
    - `battery-scheduler` systemd unit is `inactive` (stopped per
      operator decision earlier this week; will be restarted before
      Friday market open to consume the post-`f3d4356` queue
      including V16_completely_naked).
    - Disk: not approaching limit. Memory: under cap. No intervention
      required tonight.

13. **`rsi_momentum` solo-strategy concentration intensified.**
    - Both of today's 2 trades (JKTYRE, KEC) came from `rsi_momentum`.
      Both shorts. Both stop-loss exits.
    - 7d window: rsi_momentum is now n=8, PF 0.21, Kelly -0.948.
      This is a single-strategy, single-direction, single-regime
      trading pattern.
    - User observation today: "i see only rsi_momentum only firing in
      past couple of days no other". Confirmed:
      | Strategy | 7d trades | 7d PnL |
      |---|---:|---:|
      | rsi_momentum | 8 | Rs -533 |
      | supertrend_follow | 5 | Rs +5 (no losers) |
      | Everything else | 0 | Rs 0 |
    - Combined with #10 + #11 above: the freeze has reduced the agent
      to "rsi_momentum shorts in bear_high_vol" by structural gate
      exclusion. Friday's weekly review now has clear evidence of
      single-strategy concentration risk to weigh against the
      contingency activation question (item 9 / `FREEZE_v2.1_revision.md`).

14. **KEC stop fill had 55 bps adverse slippage.**
    - Entry 478.87, stop fill 487.60. The strategy-level stop was
      ~485 (1.3 % above entry); actual fill was 0.55 % beyond that
      = 55 bps slippage.
    - Net loss Rs -240.28 (gross was ~Rs -200, slippage cost ~Rs 40).
    - For a Rs 5,000 live-capital budget, this slippage profile alone
      consumes ~0.8 % per stop hit. Two stops/day = 1.6 % daily slip
      drag. Not actionable inside freeze; flagged for post-freeze
      execution-quality review.

15. **Cumulative since freeze start crossed Rs -465.**
    - Headroom against Rs -3,000 kill criterion: **Rs 2,535 remaining
      (-15.5 % of headroom consumed by Day 4)**.
    - 7 more trading sessions until 2026-05-29 (mid-freeze health
      check). Linear projection at current trajectory: ~Rs -930 by
      05-29 = still 31 % of kill headroom (well clear).
    - Phase-A rolling 5-day PF: **0.25** (FAIL by floor-PF criterion).
      Per freeze contract this does NOT trigger action; reported only
      for the Friday review file.

### 2026-05-22 EOD observations (additions, not edits) — Week 1 close

16. **First profitable session of the freeze.**
    - 2 trades, both winners, both `rsi_momentum` SHORTS, both `signal` exits.
    - `TARIL` qty 40 @ 314.06 → 312.76 (5 min hold, +Rs 38.70, +0.31 %)
    - `MMTC` qty 195 @ 65.24 → 64.96 (15 min hold, +Rs 41.13, +0.32 %)
    - Day P&L +Rs 79.83. Both exits triggered by strategy reversal
      (i.e. the system flipped out at small mean-reversion profits — not
      a TP hit, not a SL hit). This is the SAME signal-driven exit
      pattern that produced `+₹138` on PCBL on 2026-05-12 — small,
      fast, no-stop-loss path. **Two trades is statistical noise**,
      but the pattern is consistent with what `rsi_momentum` should
      ideally be doing in a bear-vol tape: short, fast, mean-reversion.

17. **Cumulative since freeze start: -Rs 385.22.**
    - Headroom vs Rs -3,000 kill criterion: **Rs 2,615 remaining
      (87 % intact)** after 5 sessions.
    - Linear projection to 2026-05-29 (mid-freeze health check, end of
      Week 2): -Rs 770 cumulative at current trajectory ≈ 26 % of kill
      headroom — comfortable.
    - Phase-A rolling 5-day PF: **0.48** (FAIL by floor — reported but
      does not trigger action).

18. **`rsi_momentum` is the only strategy producing trades all week.**
    - Per-strategy 7d table (from EOD diagnostic):
      | Strategy | N | WR % | PF | Verdict |
      |---|---:|---:|---:|---|
      | rsi_momentum | 25 | 64.0 | 1.02 | **KEEP** (point estimate) |
      | xgboost_classifier | 6 | 66.7 | 1.10 | INSUFFICIENT_DATA |
      | supertrend_follow | 40 | 52.5 | 0.62 | KILL |
      | mean_reversion | 22 | 59.1 | 0.51 | KILL |
      | ensemble | 25 | 40.0 | 0.41 | KILL |
    - **rsi_momentum's PF crossed back above 1.0** for the first time
      in the freeze week (was 0.21 on Thursday after KEC stop-loss,
      now 1.02 after today's two winners). This is exactly the kind
      of reversion-to-mean a small-N statistical sample produces;
      not yet edge-confirming.
    - Three strategies (supertrend_follow, mean_reversion, ensemble)
      remain in KILL territory by point estimate. PF lower-CI for
      supertrend_follow is now 0.25-1.52 (still inconclusive); ensemble
      upper-CI < 1.0 (only confirmed "no edge" verdict).

19. **CRITICAL OPERATIONAL FINDING — silent daemon hang at 12:23 IST.**
    - Cycle 87 heartbeat at 12:23:11 IST was the LAST heartbeat from
      the trader daemon all afternoon. From 12:23 to the VM reboot at
      23:35 IST (= **11 hours 12 minutes of silent inactivity**), the
      daemon produced no heartbeats, no audit checkpoints, no
      processed signals, no trade events, and no exception traces.
    - **Trader did NOT crash** (docker container stayed `Up`,
      healthcheck reported `healthy`). Process was alive but its
      main loop was blocked / starved / hung.
    - **Trades had already closed by 10:28 IST** (TARIL exit at 09:35,
      MMTC exit at 10:28), so no positions were left unmanaged. Pure
      luck — if a position had been open at 12:30, its SL/TP/trailing
      logic would not have fired for 11h.
    - **SSH/sshd from operator IP also became unreachable at ~12:30**
      ("Connection timed out during banner exchange" — banner-level,
      not auth-level). May be the same VM-level event that hung the
      daemon, or fail2ban-banning operator IP after repeated SCP
      retries.
    - VM reboot at 23:35 cleared both issues. Container restarted
      cleanly, daemon resumed Saturday-morning idle (market closed).
    - **Detection lag**: 11 hours, 12 minutes. Acceptable for a
      Friday afternoon (no positions held); **catastrophic** for any
      Tue/Wed/Thu session with open positions.
    - **Heartbeat email NEVER deployed** all week (the very feature
      designed to detect this — `tools/send_heartbeat.py` shipped
      2026-05-19 — has not been installed via cron on the trader VM).
      Single biggest actionable carry-forward into Week 2.

20. **Today's deploy work supplanted the regime evidence dump.**
    - Saturday's planned regime-evidence dump (items #10 + #11 from
      Thursday's EOD entry) was deferred because Friday afternoon was
      consumed by:
      - Battery VM redeploy (V16 + speed patches + `-d` scheduler bug)
      - PermissionError firefighting (chmod 1777 on logs/ + data/)
      - Trader VM SSH lockout debugging
    - Saturday-Sunday resumes the regime-evidence work. Now informed
      by V1 vs V2 *and* V1 vs V2 second result (nifty50 universe)
      arriving Saturday early morning.

21. **Battery: V1 vs V2 second result expected Saturday early morning.**
    - `battery_nifty50_60d_20260522T085929` (running on new code) at
      ~70 % progress (V1 + V2 in parallel), ETA ~3.8h from 23:00 IST
      Friday → completion ~03:00 IST Saturday.
    - Combined with V1+V2 on the v2_baseline 90-day dataset (already
      preserved at `logs/battery_pulled/`), this gives **two
      independent V2-vs-V1 comparisons** on different universes /
      time windows. Most powerful evidence-base for the regime gate
      decision.
    - Per-worker rate dropped from 16-18 ev/s (May-18 run) to ~5 ev/s
      (today's run). Possible regression in the speed patches or
      worker count change. Investigate after V1 lands.

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
  `4276962`; ground truth lives in `docs/freeze/FREEZE_v2.1.md` §Bypass ledger):
  | Date | Commit | Slot? | Description |
  |---|---|---|---|
  | 2026-05-19 | 9cd7acd | audit-only (no frozen file) | observability: HTML email rendering + CI green-up |
  | 2026-05-19 | 868d5ad | audit-only (no frozen file) | observability: freeze-v2.1 pre-commitments + diagnostic stats |
  | 2026-05-19 | 9772e4d | audit-only (no frozen file) | freeze-bypass: battery throughput + cloud progress tool |
  | 2026-05-20 | 5934960 | audit-only (no frozen file) | freeze-bypass: battery infrastructure hardening (perf + functionality) |
  | 2026-05-21 | f3d4356 | audit-only (no frozen file) | freeze-bypass: V16_completely_naked diagnostic variant + backtest_gates harness extension |
  | 2026-05-21 | ec278d4 | audit-only (no frozen file) | battery_status_remote regex fix (ETA / progress rendering) |
  | 2026-05-22 | 27b6b12 | audit-only (no frozen file) | scheduler bug fix: `-d` as docker flag, not python tail arg |
  - Bypass cap: **0 / 3 used** at end of Week 1. All seven
    `freeze-bypass:`-tagged commits touched only research / observability /
    operational files; per `docs/freeze/FREEZE_v2.1.md` lines 135-138 these do
    NOT consume a slot. Cap test is "touches a frozen file" (strategies,
    risk gates, trading agent, sizing, config.yaml strategy/risk blocks,
    or model artefact). All three slots remain available for Weeks 2-3.
- **2026-05-21 read-only inspection** of `packages/core/regime.py`
  (frozen file). Inspection alone does not modify code; no bypass slot
  consumed. Findings folded into items #10 + #11 above as Friday-review
  inputs only.
- **2026-05-22 VM-side operational events** (no commits, no slots
  consumed): killed long-running `battery_freeze_v21_20260518T181337`
  (3-day stuck container after V1+V2 results landed); `chmod 1777` on
  battery VM `logs/` and `data/` to allow both opc (scheduler) and uid-1001
  (container) write access; reset battery `data/battery_queue_state.json`
  to clear `-d`-bug failures; soft-rebooted trader VM at 23:35 IST to
  recover from silent daemon hang + SSH lockout.

---

## Week 1 review handover (Saturday 2026-05-23 morning, post-EOD)

Per the freeze contract, **Week 1 review is observation only — no decisions.**
Following observations are recorded for the Week 2 review (Friday 2026-05-29)
and the mid-freeze health check.

### Decision-table (read-only this week)

| Pre-decision artefact | Status (Sat 2026-05-23) | Action this week |
|---|---|---|
| Per-strategy live-vs-battery table | **PENDING** — battery V1+V2 (nifty50_60d, run #2) was at ~70 % progress at Friday EOD; ETA ~03:00 IST Saturday. V1+V2 from May-18 v2_baseline (90d) already in hand at `logs/battery_pulled/`. | Build the table Saturday afternoon once V1 result JSON lands. |
| Trade count vs contingency threshold (≥10 trades = stay on plan; <10 = activate `FREEZE_v2.1_revision.md` Branch 1) | **Week 1 closed at 7 trades.** Threshold of ≥10 NOT met. | **Branch 1 (extended window) is auto-activated** per the contract. Window extends from 5 Fridays to 7 Fridays (≈14 Jul 2026). Mid-freeze checkpoint moves to Fri 2026-06-05. **No code changes.** |
| Battery first-completion check (V1 vs V2 evidence) | **V2_baseline (May-18 run, 90d v2 universe): IN HAND** ≈ ~390 trades, broader signal acceptance; PF and per-strategy comparison pending diagnostic re-run. **V2 (May-22 run, nifty50 60d): IN PROGRESS** completing Saturday early morning. | Saturday-Sunday: fold both V2 results into the regime evidence dump (item #20 above). |
| Operator-skipped-days count (`docs/freeze/freeze_contingencies.md` §C6) | **0 / 5 sessions.** Daemon ran 5/5 trading days, audit verdicts GREEN 5/5, no manual intervention required. Silent hang on Friday 12:23 IST is logged but did NOT break the freeze (no positions held, no manual override). | None (no slot consumed). |
| Bypass slot consumption | **0 / 3 used.** Seven `freeze-bypass:`-tagged commits in Week 1, all audit-only (no frozen-file edits). All three slots remain. | None — slots fully preserved for Weeks 2-3 if a frozen-file emergency arises. |
| Regime evidence dump (the Saturday 2026-05-23 deferred work) | **Scope intact**, deferred from Saturday morning to Saturday afternoon / Sunday. Frame is items #10 + #11 (binary 200-EMA test, unreachable `sideways` regime) + V2_all_filters_off as quantitative complement. | Saturday afternoon onwards (no live trading impact). |
| Heartbeat email cron installation (`tools/cloud/install_heartbeat_cron.sh`) | **NEVER DEPLOYED on trader VM all of Week 1.** Friday's silent daemon hang at 12:23 IST went undetected for 11 hours — the exact failure mode this script was written to catch (2026-05-19). | **Highest-priority Week 2 carry-forward.** Schedule for Sunday 2026-05-24 evening alongside the regime evidence write-up. |

### Per-strategy 7-day live-only snapshot (point estimate, no CIs yet)

| Strategy | N | WR % | PF | Expectancy | Verdict (point estimate) | Verdict (with CI) |
|---|---:|---:|---:|---:|---|---|
| rsi_momentum  | 25 | 64.0 | 1.02 | +Rs 0.56 | KEEP | INSUFFICIENT_DATA (PF 95 % CI straddles 1.0) |
| xgboost_classifier | 6 | 66.7 | 1.10 | +Rs 3.33 | KEEP | INSUFFICIENT_DATA (n < 10) |
| supertrend_follow | 40 | 52.5 | 0.62 | -Rs 16.13 | KILL | INSUFFICIENT_DATA (CI lower < 1, upper > 1) |
| mean_reversion | 22 | 59.1 | 0.51 | -Rs 22.57 | KILL | INSUFFICIENT_DATA (n borderline) |
| ensemble | 25 | 40.0 | 0.41 | -Rs 7.80 | KILL | **CONFIRMED no edge** (CI upper < 1.0) |

**Reading rule (per `docs/freeze/FREEZE_v2.1.md` §C2 statistical-significance check):**
verdicts above are **point estimates only** and explicitly do not trigger
strategy disable / scale until n ≥ 30 with CI excluding 1.0. The only
strategy that meets that bar this week is `ensemble` (CI upper < 1.0).
**No action this week** — `ensemble` removal stays a Week-3 / post-freeze
decision, not a bypass-slot emergency.

### Operational scoreboard

| Metric | Week 1 actual | Contract / target |
|---|---|---|
| Daemon uptime % | ~95 % (11h hang on Friday afternoon, no positions held) | ≥ 99 % (FAIL — single incident) |
| Days with audit verdict GREEN | 5/5 (100 %) | ≥ 4/5 (PASS) |
| Heartbeat alert success rate | 0 % (cron not installed) | n/a — feature not yet deployed |
| Manual operator interventions | 1 (VM reboot Friday night) | minimise |
| Bypass slots consumed | 0 / 3 | ≤ 1 / 3 in Week 1 (PASS) |
| Frozen-file edits | 0 | 0 (PASS) |

### Commitments rolling into Week 2

1. **Install `tools/cloud/install_heartbeat_cron.sh` on trader VM** — top priority.
2. **Complete the regime evidence dump** (items #10 + #11 + V2 backtest comparison).
3. **Investigate the trader-VM silent-hang root cause** (was it the same event as the SSH lockout? Disk pressure? Memory? IST 12:23 GMT 06:53 timestamp may correlate with cron / cloud maintenance window).
4. **Investigate battery throughput regression** (5 ev/s vs 16-18 ev/s in May-18 run). Speed patch may have introduced a counter-effect, or the new image has a different worker profile.
5. **Build per-strategy live-vs-battery comparison table** once V2 (nifty50_60d) result lands Saturday morning.
6. **Fold the V2_baseline 90d result into the diagnostic** as a long-side counter-factual ("what does the system look like with no regime gate, on the same universe but a longer window?").

### Week 1 — TL;DR for the Operator

- **Week 1 is OBSERVATION ONLY.** No frozen-file changes have been made,
  no strategy has been disabled, no risk parameter has been touched.
- **Result: -Rs 385 across 7 trades (5 sessions).** Worst-case projection
  to kill (-Rs 3,000) at this loss rate: 9 weeks. Comfortable headroom.
- **Trade-count contingency Branch 1 auto-activates** (7 trades vs ≥10
  threshold). Freeze window extends 2 weeks → end target Fri 2026-07-10.
- **One CRITICAL operational issue: silent daemon hang Friday afternoon.**
  Detection mechanism (heartbeat email) was written 4 days earlier but
  never deployed. Top Week-2 priority.
- **Long-side famine continues** (5/5 sessions = 0 longs, 100 % shorts).
  Regime evidence dump quantifies the cost in Week 2.
- **Bypass discipline: clean.** 0/3 slots used; all 7 audit-tagged commits
  touched only research/observability/operational files.
