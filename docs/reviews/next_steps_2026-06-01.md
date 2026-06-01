# Next Steps Plan — post-Phase 16 (2026-06-01)

**Author:** Codegen (Phase 16 wrap-up, 17:45 IST)
**Status:** DRAFT — operator review required before any execution
**Replaces verbal "next steps" floated mid-session.**

## 0. Context recap (one paragraph)

Phase 13–15 produced Engine B (`packages/research/swing_backtester.py`) plus
6 swing-cash strategies (V35-V40) and identified a new Profile A:
**70% NIFTYBEES + 30% V38(weekly_entry_n=25, weekly_exit_m=12)**. All
research work is freeze-safe (zero trader-runtime imports). Phase 16
audited the result against `repo-conventions`, relocated 4 misplaced
sweep-param JSONs, and identified deferred drifts. The operator's
mid-session question — _"can we end the v2.1 freeze early?"_ — was
declined per `docs/freeze/freeze_v2.1_exit_criteria_2026-06-05.md` (the
06-05 verdict meeting is contractual and bypassing it would not save
calendar time anyway because 06-08 is a fixed Mode A deploy date).

This document lays out the *actual* path between today (2026-06-01) and
the 06-05 verdict / 06-08 deploy, plus the deferred Phase 17 cleanup.

---

## 1. Calendar (immutable)

| Date | Event | Source |
|---|---|---|
| 2026-06-01 (today) | Phase 13/14/15 complete; Phase 16 cleanup committed | this doc |
| 2026-06-02 to 06-04 | T-3, T-2, T-1: freeze-safe dev work (this doc §3) | `freeze_v2.1_exit_criteria_2026-06-05.md` |
| 2026-06-05 (Fri) | Verdict meeting: review v2.1 paper data + decide Mode A profile | `wind_down_criteria_2026-06-05.md` |
| 2026-06-08 (Mon) | Mode A deploy on trader VM (paper mode initially) | charter §6 + freeze contract |

**Cannot accelerate:** weekend markets are closed, and Friday's verdict
needs a full week of post-26-05 paper data (last config change was
2026-05-26).

## 2. What we know about the trader VM (re-confirmed Phase 16)

| Property | Value |
|---|---|
| IP | `80.225.251.79` |
| SSH | `ubuntu@80.225.251.79 -i ~/.ssh/oci_trader_key` |
| Repo path on VM | `/opt/trading-agent` |
| Container | `trader` (via `docker compose up -d trader`) |
| Daemon | `python run_daemon.py --paper --interval 60` |
| Live strategies | `rsi_momentum`, `vwap_bounce`, `opening_range_breakout`, `supertrend_follow` |
| Audit cadence | hourly checkpoint inside daemon @ top of each IST hour 09:00–16:00 |
| Crons | heartbeat 09:10 IST, watchdog every 5 min |
| Logs | `/opt/trading-agent/logs/` → pulled via `tools/cloud/pull_logs.ps1` |

**Key fact for next-steps planning:** the daemon does NOT have a dynamic
strategy loader — strategies must be added to `trading_agent.py:_load_registry()`
AND to `config.yaml:strategies.active`. The freeze contract counts
`trading_agent.py` and `config.yaml` strategy/risk blocks as frozen
surfaces. ANY V38 deploy requires a freeze-bypass slot.

## 3. T-3 / T-2 / T-1 work plan (freeze-safe)

The freeze contract permits the following **without** consuming bypass
slots, because none touch frozen behavior:

### 3.1 Pre-stage the 2026-06-05 verdict packet

Create `docs/freeze/verdict_meeting_packet_2026-06-05.md` (file already
exists per directory audit — needs Phase 15 data appended).

Contents to add:
- Paper-mode P&L summary from 2026-05-26 onwards
- Phase 15 Profile A recommendation with the new numbers
- Phase 16 cleanup acknowledgement
- One-page decision matrix: profile A / A-Plus / A-Defense / hold

**Owner:** codegen + operator review
**Effort:** ~1h
**Freeze impact:** zero

### 3.2 Pull live daemon status from the trader VM

The hourly audit-checkpoint job has been running on the VM. Pull and
inspect:

```powershell
.\tools\cloud\pull_logs.ps1
```

Then run the `trading-audit` skill to summarize the latest checkpoint(s).
Specifically check:
- Daemon health: GREEN/YELLOW/RED verdicts
- Trades since 05-26 (config change date)
- Per-strategy 7-day P&L
- Any open positions left from Friday

**Owner:** operator runs SCP from laptop; codegen runs the audit skill on pulled artifacts
**Effort:** ~30min total
**Freeze impact:** zero

### 3.3 Build the Mode A dispatcher wiring (for 06-08 deploy)

This is the actual code work needed to ship V38 to paper mode on 06-08.
Three pieces:

#### (a) PaperBroker stub
File: `packages/brokers/paper_broker.py` (CHECK if exists — `packages/brokers/`
already houses live brokers)
Purpose: accept Engine B orders, fill at next-bar open, write to a separate
paper trade book so we don't pollute the intraday `trades` DB.

#### (b) ModeDispatcher wiring
File: `packages/trader/mode_dispatcher.py` (EXISTS but not wired into
`trading_agent.py`)
Purpose: route the V38 swing strategy alongside the existing intraday
strategies, but on a different cadence (V38 is daily-bar, not minute-bar).

#### (c) `config.yaml` strategies.modes block
Add:
```yaml
strategies:
  modes:
    swing_cash:
      active: true
      strategies:
        - name: weekly_breakout_v1
          params:
            weekly_entry_n: 25
            weekly_exit_m: 12
            allocation_pct: 30  # 70% NIFTYBEES held passively
```

**Owner:** codegen
**Effort:** ~3-4h coding + 1h freeze-bypass ledger entry
**Freeze impact:** consumes ONE freeze-bypass slot (config.yaml + trading_agent.py
strategy registry) — within the 06-05 verdict scope, accepted as expected

### 3.4 Add V38 to `data/battery_queue.yaml` (deferred drift #6)

So that the backtester VM can validate V38 nightly going forward (not
just on this laptop):

```yaml
jobs:
  - name: v38_weekly_breakout_validation
    days: 365
    interval: 1d
    variants: [V38_n25_m12]  # requires extending battery.py:VARIANTS
    universe-file: data/v4_universe_swing_cash.txt
```

**Catch:** the queue runs `EnsembleBacktester`, not `swing_backtester`.
Either:
- (a) Wrap Engine B in an EnsembleBacktester-compatible variant, OR
- (b) Add a new queue job type that invokes `tools/multi_swing_backtest_2026_06_01.py`

Option (b) is cleaner but requires queue-schema work. **Recommend
deferring to Phase 17 unless operator wants V38 on the backtester VM
before 06-08.**

**Owner:** codegen
**Effort:** ~2-3h (option b)
**Freeze impact:** zero (backtester infrastructure is explicitly NOT frozen)

## 4. Phase 17 cleanup (post-verdict, post-deploy)

The drifts deferred from Phase 16 should be addressed once V38 is in
paper mode and we have signal that Engine B is permanent infrastructure:

| # | Cleanup | Effort | Risk |
|---|---|---|---|
| 1 | Move `StrategySpec` + `OpenPosition` from `packages/research/swing_backtester.py` to `packages/core/strategy_spec.py`; update 7 importers | ~2h | Low — non-runtime change |
| 2 | Rename `tools/multi_swing_backtest_2026_06_01.py` → `tools/run_swing_backtest.py`; update all doc reproducer commands | ~1h | Medium — touches many docs |
| 3 | Update `repo-conventions/SKILL.md` to formalize: (a) `data/sweep_params/<variant>_<tag>_<YYYY-MM-DD>.json` location, (b) Engine B output dir naming `multi_swing_<tag>_YYYY-MM-DD/` (ISO), (c) `tools/audit/` subdir status | ~30min | Zero |
| 4 | Reconcile `packages/strategies/__init__.py:STRATEGY_REGISTRY` with the live `trading_agent.py:_load_registry()` — one or the other should be the source of truth | ~1h | Low |
| 5 | Update `docs/freeze/FREEZE_v2.1.md` to remove stale `position_sizer.py` / `breaker.py` references; replace with actual sizing path (`risk_manager.calculate_position_size`) | ~15min | Zero |
| 6 | Update `docs/backtester_vm_runbook.md` to use `logs/backtests/` instead of stale `logs/battery/` | ~15min | Zero |
| 7 | Run Phase 15 deferred walk-forward holdout (V38_n25_m12 + V40_decile15, OOS 2026-01→05) | ~2h | Zero |

## 5. What MUST happen before 06-08 deploy

Minimum viable Mode A launch:

1. ✅ Profile A selected (assumed: new Phase 15 recommendation 70NB+30V38(n=25,m=12))
2. ⏳ 06-05 verdict meeting confirms profile
3. ⏳ §3.3 dispatcher wiring committed and tested
4. ⏳ Freeze-bypass ledger entry for `trading_agent.py` + `config.yaml` strategy block
5. ⏳ Deploy: `git push` → SSH to trader VM → `git pull` → `docker compose up -d --build trader`
6. ⏳ Verify next-hour audit checkpoint shows V38 module loaded (look for `weekly_breakout_v1` in `per_strategy` section)

If any of 2-4 are missed, **defer the deploy** — the freeze contract is
explicit about "no Friday-night hot pushes."

## 6. Open questions for operator

| # | Question | Default if no answer |
|---|---|---|
| 1 | Accept Phase 16 reorg (4 JSONs moved + doc refs updated)? | Push commit at end of this session |
| 2 | Add V38 queue entry on backtester VM before 06-08, or wait until post-deploy? | **Wait** — backtester runs V1-V26 ensemble cleanly today; adding V38 means schema work |
| 3 | Want walk-forward holdout (Phase 17 §7) done T-3/T-2/T-1, or post-verdict? | **Post-verdict** — gives Friday meeting cleaner data |
| 4 | Phase 17 cleanup priority: pod-boundary fix (#1) or runner rename (#2) first? | **#1 first** (architectural; lower doc-churn risk) |

---

**Bottom line:** The system is in good shape. Phase 16 fixed one
genuine drift (JSON location) and documented the rest. The 06-05 verdict
and 06-08 deploy are on track. Phase 17 is a clean post-deploy cleanup
sprint.
