# Restructure plan

Started 2026-05-09. Phases 0/A/B/C/F are complete and live on `main`; D and E
are queued. This doc is the single source of truth for what's been done and
what's pending.

> **2026-05-09 update**: Phase E (single-package split of `trading_agent.py`)
> has been **superseded** by the 3-pod architecture decided in
> [`docs/cloud_pod_architecture.md`](cloud_pod_architecture.md). The new target
> layout is `packages/{trader,research,ui,core,strategies}` rather than a flat
> `trading_agent/` package. Phase D (`scripts/` reorg) below still applies as a
> precursor since it cleans up `tools/` before the bigger code move.

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

### Phase E (SUPERSEDED) — see Phase 1 (pod split) below

The original Phase E target was a flat `trading_agent/` Python package. This
has been replaced by a **3-pod architecture** (one pod = one Python package
under `packages/`). See [`docs/cloud_pod_architecture.md`](cloud_pod_architecture.md)
for the full design, rationale, and migration timeline.

The internal slicing of `trading_agent.py` (into `agent.py`, `cycle.py`,
`exits.py`, `safety.py`, `carryover.py`, `continuity.py`, `alerts.py`,
`health.py`) is preserved as the *internal* module layout of the new
`packages/trader/` package — the slicing work is the same, only the parent
namespace changes.

### Phase 1 — Logical pod split (`packages/{trader,research,ui,core,strategies}`)

**Status: PREP COMPLETE; EXECUTE queued for ~19:30 IST 2026-05-09 (after battery).**

#### Prep (committed, safe to do during battery)

- `packages/__init__.py`, `packages/{trader,research,ui}/__init__.py` — empty pod skeletons
- `tools/_phase1_move.py` — automated mover with `--dry-run` / `--execute` / `--rollback`
- `tests/unit/test_pod_boundaries.py` — 10 tests, skip-marked until execute lands

Run `python tools/_phase1_move.py --dry-run` to preview the exact plan at any time.

#### Move plan (preserves all import paths via `packages/` on `sys.path`)

| Source | Destination | Why |
|---|---|---|
| `core/` | `packages/core/` | Shared library (DB, charges, portfolio, secrets, ensemble, regime) |
| `strategies/` | `packages/strategies/` | Shared library |
| `brokers/` | `packages/brokers/` | Shared library (will move under core in a later phase) |
| `monitoring/` | `packages/monitoring/` | Shared library (alerts; dashboard goes to ui later) |
| `training/` | `packages/training/` | Shared library (model retraining) |
| `backtest.py` | `packages/research/backtest.py` | POD 2 module |
| `backtest_ensemble.py` | `packages/research/backtest_ensemble.py` | POD 2 module |
| `analyze_day.py` | `packages/research/analyze_day.py` | POD 2 module |
| `tools/overnight_backtest_battery.py` | `packages/research/battery.py` | POD 2 module |
| `tools/profit_diagnostic.py` | `packages/research/diagnostic.py` | POD 2 module |

`trading_agent.py` stays at root in Phase 1; its split into `packages/trader/{agent,cycle,exits,...}.py` is Phase 1.5 (multi-session refactor).

#### Import-path discipline

The directory moves **preserve names**. `from core.X import Y` keeps working
unchanged because `packages/` joins `sys.path` via:

- A new project-root `conftest.py` (for pytest)
- A 4-line prelude prepended to `run_daemon.py`, `stop_daemon.py`, `main.py`

Only **7 import statements across 3 files** require rewriting — the
research-pod files that picked up new names (`backtest_ensemble` → `research.backtest_ensemble` etc.).
Confirmed by latest dry-run: `main.py:99`, `tools/overnight_backtest_battery.py:51`, `tests/integration/test_validation_tools.py × 5`.

#### Execute checklist (when battery completes)

1. Verify last variant landed cleanly via `.\tools\battery_status.ps1`.
2. Stop battery process: `Stop-Process -Id <PID> -Force`.
3. Stop daemon if running: `Stop-Process -Name python -Force` (it's idle on weekend, but be safe).
4. `git status --short` — confirm working tree clean (or only the auto-generated battery yamls; we'll commit those first).
5. `python tools/_phase1_move.py --dry-run` — final preview, sanity check.
6. `python tools/_phase1_move.py --execute` — performs all moves + sys.path bootstrap + import rewrites.
7. `python -c "import core, strategies, brokers; print('imports OK')"` — smoke check.
8. `python -m pytest tests/unit -q` — must stay 388 pass + 10 newly active boundary tests = 398 pass.
9. `python -m pytest tests/integration -q` — sanity.
10. `git add -A && git commit -m "Phase 1: pod split (packages/ layout)"`.
11. `git push origin main`.

If any step fails: `python tools/_phase1_move.py --rollback` returns to HEAD; investigate; retry.

#### Why this is structured as 3 pods, not 4

| Pod | Why it earns its own deploy |
|---|---|
| `trader` | Live decision loop, latency-sensitive, always-on, isolated failure domain |
| `research` | 24x7 backtesting + diagnostics + training; spot-priceable; long-running CPU |
| `ui` | Read-only dashboard; on-demand; different scaling profile |

**Email is NOT a pod** -- it's a stateless SDK call to a managed service. Stays as
`packages/monitoring/alerts.py` (or `packages/core/alerts.py` post-1.5), imported
by `trader` and `research`.

Full architecture rationale + cost model + cloud migration roadmap:
[`docs/cloud_pod_architecture.md`](cloud_pod_architecture.md).

The local-laptop sync mechanism (so Cursor keeps seeing cloud-research findings)
is stubbed at `tools/sync_from_cloud.py` -- works against a local mirror today,
swaps to S3 in Phase 2 with no caller changes.
