#!/usr/bin/env bash
# Inline deploy script: executed on the OCI VM as the trader user.
# Pull latest main, rebuild image, restart container, verify.
# Stays in this repo because the only caller is the deploy hop in
# `tools/cloud/pull_logs.ps1` (Windows / no bash) and the equivalent
# ad-hoc PowerShell flow used during 2026-05-13 redeploy.
set -euo pipefail

cd /opt/trading-agent

echo "[remote] BEFORE: $(git log -1 --format='%h %s' HEAD)"
git fetch origin main
git reset --hard origin/main
echo "[remote] AFTER:  $(git log -1 --format='%h %s' HEAD)"

# UID/GID alignment (2026-05-13 fix). The Dockerfile now accepts
# TRADER_UID/TRADER_GID build args. We sync the .env on this VM so the
# container's `trader` user has the same UID/GID as the host user that
# owns the bind-mounted directories. Without this, volume mounts hit
# 'PermissionError' on the daemon's log file -- which is exactly what
# happened on the 2026-05-13 redeploy until we manually `chown -R 1001`.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
ENV_FILE="/opt/trading-agent/.env"
if [[ -f "$ENV_FILE" ]]; then
    # Replace existing TRADER_UID/GID lines or append if missing.
    if grep -q '^TRADER_UID=' "$ENV_FILE"; then
        sed -i "s/^TRADER_UID=.*/TRADER_UID=$HOST_UID/" "$ENV_FILE"
    else
        echo "TRADER_UID=$HOST_UID" >> "$ENV_FILE"
    fi
    if grep -q '^TRADER_GID=' "$ENV_FILE"; then
        sed -i "s/^TRADER_GID=.*/TRADER_GID=$HOST_GID/" "$ENV_FILE"
    else
        echo "TRADER_GID=$HOST_GID" >> "$ENV_FILE"
    fi
    echo "[remote] aligned TRADER_UID=$HOST_UID TRADER_GID=$HOST_GID in .env"
else
    echo "[remote] WARN: .env not found at $ENV_FILE; container UID will default to 1001"
fi

echo "[remote] rebuilding image..."
docker compose build trader

echo "[remote] restarting container..."
docker compose up -d trader

echo "[remote] sleeping 10s for daemon boot..."
sleep 10

echo "[remote] container status:"
docker compose ps trader

echo "[remote] last 20 daemon log lines:"
docker compose logs --tail 20 trader
