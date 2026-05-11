# deploy/

Deployment artefacts. The trading agent is a **modular monolith** that runs
as a single OS process on a single VM (or in a single container). Splitting
it into microservices/pods is rejected by design — see
[`docs/cloud_pod_architecture.md`](../docs/cloud_pod_architecture.md) for the
latency-budget reasoning (the trader pod stays a single process; the
*research* and *UI* pods are separate processes on separate VMs).

## Layout (as of Phase 1 MVC, 2026-05-11)

Docker artefacts have been moved to the **repo root** to follow the standard
Docker convention (`docker build .` works from the repo root, no `cd` dance
needed):

```
<repo-root>/
├── Dockerfile               # multi-arch (amd64+arm64), Python 3.11-slim,
│                            # non-root user, tini PID 1, healthcheck
├── docker-compose.yml       # local-paths layout, restart=unless-stopped,
│                            # volume-mounts for data/logs/models/config
├── .dockerignore            # excludes secrets, models, logs, data, tests, docs
├── .env.example             # local-dev (corp-network friendly default)
├── .env.production.example  # cloud-flavoured (forces SSL verification on)
└── tools/cloud/             # ops scripts for cloud deployment
    ├── oci_bootstrap.sh     # one-shot installer for fresh OCI/Ubuntu/AL2023 VM
    ├── trader.service       # optional systemd unit (boot-time start)
    ├── deploy.sh            # SSH-push deploy (git pull on box, rebuild, restart)
    └── healthcheck_cron.sh  # dead-man's switch with Resend email alerts
```

Detailed playbook: [`docs/cloud_mvc_runbook.md`](../docs/cloud_mvc_runbook.md).

## Local docker run (smoke test)

```bash
# from project root
docker compose build trader
docker compose up trader
# Watch ~2 min for paper-mode heartbeats and "Outside market hours -- sleeping"
# Ctrl+C, then:
docker compose down
```

## Cloud deployment plan

The MVC target is **Oracle Cloud Infrastructure free tier** (Ampere A1 ARM
instance in Mumbai region). See
[`docs/cloud_mvc_runbook.md`](../docs/cloud_mvc_runbook.md) for the 5-day
sprint plan and fallback ladder (OCI → DigitalOcean → AWS).

| Tier | Concern | MVC choice |
| --- | --- | --- |
| Compute | host the daemon | OCI VM.Standard.A1.Flex, 1 OCPU + 6 GB (free) |
| Storage (state) | DB, models, market_data | Local bind-mounts on the boot volume (50 GB free) |
| Storage (logs) | live, audit, postmortem, journal | Local rotating files; nightly rclone → OCI Object Storage |
| Secrets | broker credentials | `.env` chmod 600 owned by `trader` user |
| Logs | external sink | journald (via Docker logging driver) + Resend EOD emails |
| Alerting | dead-man's switch | `tools/cloud/healthcheck_cron.sh` → Resend |
| Watchdog | restart on crash | docker's `restart=unless-stopped` + optional systemd `trader.service` |

Phased migration outlined in
[`docs/cloud_mvc_runbook.md`](../docs/cloud_mvc_runbook.md) and
[`docs/cloud_pod_architecture.md`](../docs/cloud_pod_architecture.md).

## Why not Kubernetes?

We considered it. Rejected because:

1. **Single-tenant economics**. Running a 32-instance fleet for a ₹100k
   account doesn't recover the orchestration overhead.
2. **Latency budget**. AngelOne ticks → strategy → ensemble → exit has to
   clear in <2 s. K8s service hops add 50-200 ms each. Can't afford 4-6 of
   those per cycle.
3. **State**. Trading state is intrinsically stateful (open positions,
   trailing stops, equity curve). Splitting state across pods means inventing
   a coordination layer we don't need.

If we ever scale to 5+ portfolios or multi-broker, revisit.
