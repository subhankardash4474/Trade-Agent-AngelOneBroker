# Cloud Pod Architecture — Decision Record

**Date**: 2026-05-09
**Status**: Approved direction. Phase 1 (logical split, on laptop) queued for after current backtest battery completes.
**Supersedes**: `restructure_plan.md` Phase E (single-package split). Phase E target layout is REPLACED by the package layout below; Phase D (`scripts/` reorg) still applies as a precursor.

---

## Decision

The system will be split into **3 deployable units (pods)** with a shared core library:

| Pod | Purpose | Schedule | Resource profile |
|---|---|---|---|
| `trader` | Live trading: ingests ticks, runs strategies, places orders | 09:00–15:45 IST trading days | Always-on, small VM, low-latency |
| `research` | Backtesting + diagnostics + model training; emits proposals | 24×7 background | Big CPU, spot-priceable |
| `ui` | Read-only dashboard | On-demand | Tiny VM, off most of the time |

**Email is NOT a pod.** Resend / SES is already a managed cloud service; wrapping it in a microservice adds latency, secrets management, deploy artifacts, and a new failure mode for zero benefit. `monitoring/alerts.py` stays as a library imported by `trader` and `research`.

**Decision rationale (why 3, not 4):**

A pod earns its existence by either (a) being on the live decision-latency path and needing isolation, or (b) having a fundamentally different scaling profile. Email has neither — it is a stateless function call to an external SaaS.

---

## Architecture

```
                  +---------------------------------------------+
                  |          SHARED CLOUD INFRA                 |
                  |  RDS Postgres | S3 | Secrets | CloudWatch   |
                  +-----+--------------+--------------+---------+
                        |              |              |
              +---------+              |              +---------+
              |                        |                        |
        +-----+-----+          +-------+------+         +-------+----+
        |   POD 1   |          |   POD 2      |         |   POD 3    |
        |  trader   |          |  research    |         |    ui      |
        |           |          |              |         |            |
        |  09-15:45 |          |  24x7        |         | on-demand  |
        |  + alerts |          |  battery     |         | dashboard  |
        |           |          |  + diag      |         |            |
        |           |          |  + train     |         |            |
        |           |          |              |         |            |
        | t3.small  |          | c5.large     |         | t3.micro   |
        | ~$15/mo   |          | spot ~$25/mo |         | ~$5/mo     |
        +-----+-----+          +-------+------+         +-----+------+
              |                        |                       |
              +------------------------+-----------------------+
                                       |
                              Resend SDK (inline,
                              not its own pod)
```

### Data flow

```
                    AngelOne API
                         |
                         v
    [trader] --> writes trades --> RDS Postgres <-- reads trades [research]
        |                                                |
        |-> Resend (alerts)                              |-> writes diagnostics, backtest
                                                         |   results, proposals --> S3
                                                         |
                                            laptop <-- pulls --[ tools/sync_from_cloud.py ]
                                                                       |
                                                                       v
                                                            logs/cloud_sync/* (gitignored)
                                                                       |
                                                                       v
                                                              Cursor reads & advises
```

---

## Repository layout (target after Phase 1)

```
trading-agent/
|-- packages/
|   |-- core/                # shared library (DB, charges, portfolio, secrets, alerts, brokers)
|   |   |-- __init__.py
|   |   |-- database.py
|   |   |-- portfolio.py
|   |   |-- charges.py
|   |   |-- risk_manager.py
|   |   |-- secrets.py
|   |   |-- alerts.py
|   |   |-- brokers/
|   |   |-- regime.py
|   |   |-- features.py
|   |   |-- ensemble.py
|   |   `-- data_handler.py
|   |
|   |-- strategies/          # shared library (all strategy implementations)
|   |   `-- __init__.py
|   |
|   |-- trader/              # POD 1
|   |   |-- __init__.py
|   |   |-- __main__.py      # entry: python -m trader
|   |   |-- agent.py         # TradingAgent class core
|   |   |-- cycle.py         # _trading_cycle, _process_signal
|   |   |-- exits.py         # exit paths, _fast_exits_sleep
|   |   |-- safety.py        # preflight, kill switches, breaker, window cap
|   |   |-- carryover.py     # carryover SL, profit lock, EOD square-off
|   |   |-- continuity.py    # state restore, daily resets
|   |   `-- health.py        # heartbeat, health.json
|   |
|   |-- research/            # POD 2
|   |   |-- __init__.py
|   |   |-- __main__.py      # entry: python -m research
|   |   |-- scheduler.py     # cron-like loop (battery nightly, diag at 16:00 IST, train weekly)
|   |   |-- battery.py       # was tools/overnight_backtest_battery.py
|   |   |-- diagnostic.py    # was tools/profit_diagnostic.py
|   |   |-- backtest_ensemble.py
|   |   |-- backtest.py
|   |   |-- training.py      # XGBoost retraining
|   |   `-- proposals.py     # emits config patch suggestions to S3 (or local mirror)
|   |
|   `-- ui/                  # POD 3
|       |-- __init__.py
|       |-- __main__.py      # entry: python -m ui (FastAPI app)
|       |-- routes.py
|       |-- views/
|       `-- static/
|
|-- deploy/
|   |-- docker/
|   |   |-- trader.Dockerfile
|   |   |-- research.Dockerfile
|   |   |-- ui.Dockerfile
|   |   |-- docker-compose.yml         # local 3-pod test
|   |   `-- docker-compose.prod.yml    # single-VM cloud (Phase 2)
|   |-- ecs/                           # Phase 3 task definitions
|   `-- terraform/                     # Phase 3 IaC (RDS, S3, ECS)
|
|-- scripts/                # local-only CLIs (not deployed in any pod)
|   |-- live/               # ops scripts wrapping pod 1 (run, stop, close, replay)
|   |-- ops/                # diagnostic/inspection one-offs
|   |-- analysis/           # post-trade analysis
|   `-- broker/             # AngelOne smoke / login helpers
|
|-- tools/
|   |-- sync_from_cloud.py  # mirrors S3 -> logs/cloud_sync/ for offline analysis
|   `-- ...
|
|-- tests/
|-- docs/
`-- pyproject.toml          # workspace; each package has its own pyproject too
```

### Why packages, not microservices, in Phase 1

In Phase 1 each "pod" is just a Python module entry point (`python -m trader`). Same machine, same DB. The split only enforces **import boundaries** — `research` cannot accidentally import `trader.exits` because pyproject.toml + tests block it.

This gives 90% of the architectural benefit (clean separation, test isolation, easier reasoning) at **0% of the cloud cost**, and Phase 2 (containerise) becomes trivial because the boundaries already exist.

---

## Phased migration

| Phase | Trigger | Scope | Cost (USD/mo) |
|---|---|---|---:|
| **1. Logical pods** | After current backtest battery completes (~tonight) | `git mv` into `packages/`. Same laptop, same SQLite. Each "pod" = a Python module. Phase D (`scripts/` reorg) folded into this. | $0 |
| **2. Containerise + single-VM cloud** | After 5 consecutive profitable trading days | `Dockerfile` per pod, deploy via `docker-compose` to ONE EC2/ECS host. SQLite -> RDS Postgres. Local files -> S3. Secrets -> AWS Secrets Manager. | ~$30 |
| **3. Multi-pod cloud** | After 1 profitable month | Each pod on its own ECS Fargate task or EC2. CI/CD via GitHub Actions. Spot instances for `research`. CloudWatch dashboards. | ~$100 (~Rs 8,500) |
| **4. Optimise** | After 6 months of stable operation | Savings plans, autoscaling, reserved capacity. | ~$50 (~Rs 4,000) |

**Hard rule: zero cloud spend until live trading is profitable for 5+ consecutive days.** The laptop is the runway.

---

## Local-laptop sync (the "bring data back to me" mechanism)

The cloud `research` pod will discover patterns at scale the laptop never could, but Cursor (the AI in the IDE) only sees what's on the laptop's filesystem. We bridge that gap with a sync script.

### Design

`tools/sync_from_cloud.py` — pluggable source, uniform local destination:

| Source URI | Mode | When |
|---|---|---|
| `file:///path/to/local/mirror` | **dev / Phase 1** | While research pod is on the same laptop, it writes to a local "fake S3" folder; sync just copies. |
| `s3://trading-agent-data/` | **prod / Phase 2+** | Real S3 bucket, IAM-authenticated. |

What the cloud pod writes:

```
s3://trading-agent-data/
  diagnostics/2026-05-09.md           # daily profit_diagnostic output
  diagnostics/2026-05-09.json         # machine-readable
  backtests/RUN_ID/comparison.md
  backtests/RUN_ID/results/*.json
  proposals/2026-05-09.yaml           # suggested config patches
  trades-export/2026-05-09.csv        # full trade ledger snapshot
```

What the laptop ends up with after `python tools/sync_from_cloud.py --since 7d`:

```
logs/cloud_sync/                      # gitignored
  diagnostics/...
  backtests/...
  proposals/...
  trades-export/...
```

Cursor reads from `logs/cloud_sync/` exactly as it reads from `logs/diagnostics/` today. Same workflow, deeper data.

### "Proposals" — the killer feature

The `research` pod can emit *suggested config patches* (YAML diffs) into `proposals/`:

```yaml
# logs/cloud_sync/proposals/2026-05-09.yaml
date: 2026-05-09
generated_by: research/diagnostic.py
based_on:
  trades_analyzed: 412
  period_days: 30
verdict_changes:
  - strategy: supertrend_follow
    old_verdict: SCALE
    new_verdict: WATCH
    reason: "PF dropped 1.56 -> 0.92 over last 7d; 8 of 12 trades were stops"
proposed_patches:
  - path: strategies.active
    op: remove
    value: supertrend_follow
    confidence: medium
  - path: ensemble.confidence_threshold
    op: replace
    old: 0.55
    new: 0.62
    confidence: high
estimated_impact:
  pnl_delta_rs_per_day: +85
  expectancy_change: +Rs 4.2/trade
```

Cursor reads, sanity-checks, and you approve before any patch is applied to live. Closes the human-in-the-loop on autonomous self-improvement.

---

## Open questions for later

1. **Schema evolution** — once we move to Postgres, who owns migrations? `alembic` in `packages/core/migrations/`?
2. **Tick storage** — keeping every tick from AngelOne in Postgres will balloon costs. Cold tier to S3 after 30 days?
3. **UI auth** — Phase 3 dashboard will need at least basic auth. Probably AWS Cognito (free tier).
4. **Disaster recovery** — RDS automated backups + S3 versioning. Need to spec RTO/RPO.
5. **Will the live agent ever talk to research synchronously?** (e.g. "should I take this trade?" → research gives Bayesian update.) Adds latency. Probably no.

These are deferred to Phase 3 design time.

---

## Cross-references

- [`docs/restructure_plan.md`](restructure_plan.md) — execution detail for Phase D and the now-superseded Phase E
- [`deploy/README.md`](../deploy/README.md) — current Docker scaffolding (will evolve into per-pod Dockerfiles)
- `tools/sync_from_cloud.py` — the local sync stub
- `tools/profit_diagnostic.py` — what the cloud `research` pod will run on a schedule
