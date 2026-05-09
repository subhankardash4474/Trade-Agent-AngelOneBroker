# deploy/

Deployment artefacts. The trading agent is a **modular monolith** that runs as
a single OS process on a single VM (or in a single container). Splitting it
into microservices/pods is rejected by design — see
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for the latency-budget
reasoning.

## Layout

```
deploy/
├── docker/
│   ├── Dockerfile              # multi-stage: builder + slim runtime
│   ├── .dockerignore           # don't ship logs/data/models/.venv
│   └── docker-compose.yml      # single-host, restart=unless-stopped
└── README.md
```

GitHub Actions CI lives at the repo root in `.github/workflows/test.yml`
because that's where Actions looks.

## Local docker run (smoke test)

```bash
# from project root
docker build -f deploy/docker/Dockerfile -t trading-agent:latest .
docker run --rm \
    -v "$(pwd)/logs:/app/logs" \
    -v "$(pwd)/data:/app/data" \
    -v "$(pwd)/models:/app/models" \
    -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
    -v "$(pwd)/.env:/app/.env:ro" \
    trading-agent:latest
```

Or via compose:

```bash
cd deploy/docker
docker compose up -d
docker compose logs -f agent
```

## Cloud deployment plan (when laptop earnings justify it)

| Tier | Concern | Choice |
| --- | --- | --- |
| Compute | host the daemon | AWS EC2 t3.small (2 vCPU, 2GB) — single instance, AZ-resilient via auto-recovery |
| Storage (state) | DB, models, market_data | EBS volume mounted at `/app/data` and `/app/models` |
| Storage (logs) | live, audit, postmortem, journal | EFS or S3 sync on rotation |
| Secrets | broker credentials | AWS Secrets Manager (read at boot via `core/secrets.py`) |
| Logs | external sink | CloudWatch Logs agent on the VM tailing `logs/` |
| Alerting | SNS / Resend | Both — Resend for email (existing), SNS for SMS on critical breaker |
| Watchdog | cron / systemd | systemd unit instead of PowerShell wrapper (Linux migration) |

Phased migration outlined in [`docs/journal/engineering_journal_2026-05-08.md`](../docs/journal/engineering_journal_2026-05-08.md).

## Why not Kubernetes?

We considered it. Rejected because:

1. **Single-tenant economics**. Running a 32-instance fleet for a Rs-100k
   account doesn't recover the orchestration overhead.
2. **Latency budget**. Kite/AngelOne ticks → strategy → ensemble → exit
   has to clear in <2 s. K8s service hops add 50-200 ms each. We can't
   afford 4-6 of those per cycle.
3. **State**. Trading state is intrinsically stateful (open positions,
   trailing stops, equity curve). Splitting state across pods means
   inventing a coordination layer we don't need.

If we ever scale to 5+ portfolios or multi-broker, revisit.
