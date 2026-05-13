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

# UID/GID are parameterised so the image's `trader` user can be made to
# match the host's deploying user, which avoids the volume-mount
# permission collision we hit on 2026-05-13:
#   * host /opt/trading-agent was owned by UID 998 (host's `trader` user)
#   * image's `trader` was hard-coded to UID 1001
#   * after `chown -R trader:trader` on the host, the container couldn't
#     write its own log files because container-UID 1001 != host-UID 998
# Default (1001) preserves the legacy behaviour for fresh local builds.
# Cloud builds set TRADER_UID/TRADER_GID in `.env` to match the host
# user, so volume mounts are read/writable from both sides without
# manual chown.
ARG TRADER_UID=1001
ARG TRADER_GID=1001

RUN groupadd --system --gid ${TRADER_GID} trader \
    && useradd  --system --uid ${TRADER_UID} --gid ${TRADER_GID} \
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
