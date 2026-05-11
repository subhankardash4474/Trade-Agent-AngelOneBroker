# =============================================================================
# Trading Agent -- Production container image
# =============================================================================
# Multi-stage build:
#   1. builder  -- installs Python deps with build-time tools available
#   2. runtime  -- minimal final image, non-root user, only runtime deps
#
# Targets both amd64 (laptop/AWS x86) and arm64 (OCI Ampere A1, Apple Silicon)
# Build:  docker build -t trading-agent:latest .
# Run:    docker compose up -d
# =============================================================================

# ----- Stage 1: builder ------------------------------------------------------
FROM --platform=$TARGETPLATFORM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for xgboost / pandas / scipy fallback compilation (rarely hit on
# arm64 since wheels exist, but kept as a safety net for fresh interpreters).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libffi-dev \
        libssl-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN pip install --prefix=/install -r requirements.txt

# ----- Stage 2: runtime ------------------------------------------------------
FROM --platform=$TARGETPLATFORM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages \
    TZ=Asia/Kolkata \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        tzdata \
        curl \
    && ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN groupadd --system --gid 1001 trader \
    && useradd  --system --uid 1001 --gid 1001 \
                --home-dir /home/trader --create-home \
                --shell /usr/sbin/nologin trader \
    && mkdir -p /app /app/data /app/logs /app/models \
    && chown -R trader:trader /app

WORKDIR /app

COPY --chown=trader:trader run_daemon.py trading_agent.py main.py stop_daemon.py conftest.py ./
COPY --chown=trader:trader config.yaml ./
COPY --chown=trader:trader packages/  ./packages/
COPY --chown=trader:trader tools/     ./tools/

# Drop privileges -- container never runs as root.
USER trader

# tini handles PID 1 zombie reaping + clean SIGTERM forwarding to Python.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python /app/tools/health_check.py --max-age-seconds 600 --quiet || exit 1

# Default = paper mode, 60s polling, market-hours-only. Override at runtime via
# `command:` in docker-compose.yml or `docker run ... <args>`.
CMD ["python", "run_daemon.py", "--paper", "--interval", "60"]
