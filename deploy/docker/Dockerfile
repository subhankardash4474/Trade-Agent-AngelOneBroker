# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────
# Trading Agent — production image
#
# Base: python:3.12-slim (3.14 isn't on Docker Hub yet at time of writing).
# Multi-stage to keep the runtime layer small and avoid shipping build tools.
# ─────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder

# Build deps for any wheels that need to compile (xgboost, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


FROM python:3.12-slim AS runtime

# Copy installed wheels from the builder.
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata

# Runtime libs only (libgomp for xgboost)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy code (mind .dockerignore!)
COPY . .

# Mount-able volumes for runtime state. Logs and DB MUST persist outside
# the container, otherwise we lose history every restart.
VOLUME ["/app/logs", "/app/data", "/app/models"]

# Healthcheck: the agent writes logs/health.json every heartbeat. If it's
# stale > 5 min, the container is unhealthy.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python tools/health_check.py || exit 1

# Default: run the resilient daemon. Override with `docker run ... <other-cmd>`.
ENTRYPOINT ["python"]
CMD ["run_daemon.py"]
