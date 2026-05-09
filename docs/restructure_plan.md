# Restructure plan

Started 2026-05-09. Phases 0/A/B/C/F are complete and live on `main`; D and E
are queued. This doc is the single source of truth for what's been done and
what's pending.

## Completed (committed in git)

| Phase | Commit | Description |
|---|---|---|
| 0 | `3581ca8` | git init + comprehensive `.gitignore` + baseline snapshot |
| A | `ecc1419` | archive 44 stale logs (April + early May one-offs) into `logs/archive/` |
| B | `8dc0468` | split `tests/` into `tests/unit/` + `tests/integration/` (653 tests still pass) |
| C | `1aba692` | `docs/` scaffold: `ARCHITECTURE.md`, journals, audits, postmortems |
| F | `2a11c32` | `deploy/`: Dockerfile, compose, GH Actions; root `pyproject.toml` |

Total: 5 commits, **zero behaviour change**. All tests green. Battery still
running, daemon (idle in market-closed mode) still healthy.

## Pending

### Phase D — `scripts/` reorganisation

**Status: BLOCKED until battery completes** (est. ~19:00-20:00 IST 2026-05-09).

Battery process holds open `tools/overnight_backtest_battery.py` and imports
`backtest_ensemble.py`. Moving either while battery runs would either crash
the process or leave it referencing stale paths.

#### Target layout

```
scripts/
├── live/
│   ├── run_daemon.py             ← was run_daemon.py (root)
│   ├── stop_daemon.py            ← was stop_daemon.py (root)
│   ├── close_position.py         ← was tools/close_position.py
│   ├── replay_failed_alerts.py   ← was tools/replay_failed_alerts.py
│   ├── run_resilient.ps1         ← was tools/run_daemon_resilient.ps1
│   └── install_scheduled_task.ps1 ← was tools/install_scheduled_task.ps1
├── ops/
│   ├── now.py                    ← was tools/_now.py
│   ├── inspect_symbol.py         ← was tools/_inspect_symbol.py
│   ├── state_check.py            ← was tools/_state_check.py
│   ├── topup_paper_cash.py       ← was tools/_topup_paper_cash.py
│   ├── health_check.py           ← was tools/health_check.py
│   ├── preflight.py              ← was tools/_preflight.py
│   └── audit_checkpoint.py       ← was tools/audit_checkpoint.py
├── analysis/
│   ├── trade_postmortem.py       ← was tools/trade_postmortem.py
│   ├── postmortem_aggregate.py   ← was tools/_postmortem_aggregate.py
│   ├── analyze_day.py            ← was analyze_day.py (root)
│   ├── send_eod_summary.py       ← was tools/send_eod_summary.py
│   ├── preview_eod_summary.py    ← was tools/preview_eod_summary.py
│   ├── resend_eod.py             ← was tools/_resend_eod.py
│   ├── resend_postmortem.py      ← was tools/_resend_postmortem.py
│   ├── meesho_postmortem.py      ← was tools/_meesho_postmortem.py
│   ├── backfill_zyduswell.py     ← was tools/_backfill_zyduswell.py
│   └── restore_positions.py      ← was tools/_restore_positions.py
├── backtest/
│   ├── single.py                 ← was backtest.py (root)
│   ├── ensemble.py               ← was backtest_ensemble.py (root)
│   ├── battery.py                ← was tools/overnight_backtest_battery.py
│   ├── cooldown.py               ← was tools/cooldown_backtest.py
│   ├── cooldown_simulation.py    ← was tools/cooldown_simulation.py
│   ├── run_overnight.ps1         ← was tools/run_battery_overnight.ps1
│   └── status.ps1                ← was tools/battery_status.ps1
└── broker/
    ├── angelone_login.py         ← was tools/angelone_login.py
    └── angelone_smoke.py         ← was tools/angelone_smoke.py

# Stays at root (entry-points used daily, easier to remember)
main.py
trading_agent.py
```

The `_*.py` underscore prefix on temporary utility scripts becomes redundant
once they're under `scripts/` (the dir name signals "not core code"), so we
drop the prefix during the move.

#### Cross-references that need updating

Mapped via grep on 2026-05-09; complete list:

**Python imports**
- `trading_agent.py:1369` — `from tools.audit_checkpoint import run_and_save`
  → `from scripts.ops.audit_checkpoint import run_and_save`
- `tests/integration/test_audit_checkpoint.py:28` — `from tools import audit_checkpoint`
  → `from scripts.ops import audit_checkpoint`
- `tools/overnight_backtest_battery.py:51` — `from backtest_ensemble import …`
  → `from scripts.backtest.ensemble import …`

**PowerShell scripts** (hardcoded paths)
- `tools/run_battery_overnight.ps1:39` — `tools\overnight_backtest_battery.py`
  → `scripts\backtest\battery.py`
- `tools/run_battery_overnight.ps1:114,117` — refs to `.\tools\battery_status.ps1`
  → `.\scripts\backtest\status.ps1`
- `tools/run_daemon_resilient.ps1:43` — `Join-Path $ProjectRoot "run_daemon.py"`
  → `Join-Path $ProjectRoot "scripts\live\run_daemon.py"`
- `tools/install_scheduled_task.ps1:29` — `tools\run_daemon_resilient.ps1`
  → `scripts\live\run_resilient.ps1`

**Scheduled task** (registered with the OS — needs re-registration after move)
- Currently runs `tools\run_daemon_resilient.ps1`. Will need to re-run
  `install_scheduled_task.ps1` after the move so the OS task points to the
  new path. **Important: do this before Monday 09:00 IST or the daemon won't
  auto-start for live trading.**

**Documentation** (mostly cosmetic, batch-updateable)
- `README.md` architecture diagram
- `docs/ARCHITECTURE.md` if it references file paths
- `tools/*.py` docstrings with `python tools/X.py` examples

#### Execution order

1. Stop daemon (PID will be different by then; just stop whatever's running).
2. Stop scheduled task temporarily (`Disable-ScheduledTask -TaskName TradingAgentDaemon` — needs admin).
3. Create `scripts/live/`, `scripts/ops/`, `scripts/analysis/`, `scripts/backtest/`, `scripts/broker/` + `__init__.py`s.
4. `git mv` every file per the table above (preserves history).
5. Update the 4 Python import sites (single-line each).
6. Update the 6 PowerShell hardcoded-path lines.
7. Update README + docs cross-refs (find/replace `tools/` → `scripts/<subdir>/`).
8. Run full test suite — must stay at 653 pass.
9. Smoke-launch each PowerShell script to confirm path resolution:
   - `scripts\backtest\status.ps1`
   - `scripts\live\run_resilient.ps1` (start, then immediately kill)
10. Re-run `scripts\live\install_scheduled_task.ps1` (admin) to update the scheduled task path.
11. Commit as `Phase D: scripts/ reorganisation`.
12. Re-enable scheduled task. Verify Monday 09:00 fires correctly.

### Phase E — `trading_agent.py` package split

**Status: DEFERRED — multi-session refactor.**

`trading_agent.py` is **163 KB / 3,395 lines / one class** with 60+ methods.
Splitting requires careful slicing along functional seams without breaking
state ownership. Dry-run plan:

```
trading_agent/
├── __init__.py
├── agent.py          ← TradingAgent class core (constructor, run loop, lifecycle)
├── cycle.py          ← _trading_cycle, _process_signal, signal funnel
├── exits.py          ← _check_position_exits, _fast_exits_sleep, all exit paths
├── safety.py         ← preflight, kill switches, strategy breaker, window cap
├── carryover.py      ← carryover SL recompute, profit lock, EOD square-off
├── continuity.py     ← state restore from DB, _resolve_continuity, daily resets
├── alerts.py         ← _on_trade_closed, EOD summary, post-mortem hooks
└── health.py         ← heartbeat, health.json, audit checkpoint
```

Estimated effort: 8-12 focused hours, with ~50 import sites to update across
the codebase. Tests should remain 100% green at every commit.

Recommendation: tackle this only after **first profitable live trading week**
is in. Until then, the monolithic `trading_agent.py` is fine — splitting it
prematurely just makes git diffs harder during fast-iteration debugging.
