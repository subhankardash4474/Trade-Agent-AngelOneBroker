#!/usr/bin/env bash
# install_prune_cron.sh
# =====================
# Install / refresh a daily cron entry on the backtester VM that runs
# `prune_old_battery_runs.sh`. Companion to install_heartbeat_cron.sh
# and install_watchdog_cron.sh on the trader VM.
#
# Cadence: 02:00 UTC == 07:30 IST daily. Runs BEFORE the 09:00 IST
# trading-day kickoff so any morning capacity check sees the freshly
# pruned state.
#
# Usage:
#   bash tools/cloud/install_prune_cron.sh
#
# After install:
#   crontab -l | grep prune
#   tail -f /opt/trading-agent/logs/prune_cron.log

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
LOG_FILE="${REPO_DIR}/logs/prune_cron.log"
SUDO_BIN="${SUDO_BIN:-sudo}"

if [ ! -f "${REPO_DIR}/tools/cloud/prune_old_battery_runs.sh" ]; then
    echo "ERROR: ${REPO_DIR}/tools/cloud/prune_old_battery_runs.sh not found." >&2
    exit 1
fi

# Pre-create log file with the same care as the heartbeat cron --
# cron's >> redirect fails silently on permission issues.
LOG_DIR="$(dirname "${LOG_FILE}")"
if [ ! -d "${LOG_DIR}" ]; then
    mkdir -p "${LOG_DIR}" 2>/dev/null \
        || ${SUDO_BIN} -n mkdir -p "${LOG_DIR}" 2>/dev/null \
        || { echo "ERROR: cannot create ${LOG_DIR}" >&2; exit 1; }
fi
if [ ! -e "${LOG_FILE}" ]; then
    touch "${LOG_FILE}" 2>/dev/null \
        || ${SUDO_BIN} -n touch "${LOG_FILE}" 2>/dev/null \
        || { echo "ERROR: cannot create ${LOG_FILE}" >&2; exit 1; }
fi
if [ ! -w "${LOG_FILE}" ]; then
    ${SUDO_BIN} -n chown "$(id -un):$(id -gn)" "${LOG_FILE}" 2>/dev/null || true
    ${SUDO_BIN} -n chmod 664 "${LOG_FILE}" 2>/dev/null || true
fi

SCHEDULE="0 2 * * *"
MARKER="# prune-cron (managed by install_prune_cron.sh)"
CMD_LINE="bash ${REPO_DIR}/tools/cloud/prune_old_battery_runs.sh --age-days 7 --keep 2 >> ${LOG_FILE} 2>&1"

echo "Installing prune cron"
echo "  Schedule: ${SCHEDULE}  (02:00 UTC == 07:30 IST daily)"
echo "  Repo:     ${REPO_DIR}"
echo "  Log:      ${LOG_FILE}"
echo "  Args:     --age-days 7 --keep 2"
echo

CURRENT="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf "%s\n" "${CURRENT}" \
            | grep -vF "${MARKER}" \
            | grep -vF "prune_old_battery_runs.sh" \
            || true)"

{
    if [ -n "${FILTERED}" ]; then
        printf "%s\n" "${FILTERED}"
    fi
    echo "${MARKER}"
    echo "${SCHEDULE} ${CMD_LINE}"
} | crontab -

echo "Installed. Current prune-related crontab lines:"
crontab -l | grep -E "prune|prune_old_battery" || echo "  (none found -- install failed?)"

echo
echo "Smoke test (one-shot dry-run):"
echo "------------------------------------------------------------"
bash "${REPO_DIR}/tools/cloud/prune_old_battery_runs.sh" --dry-run --age-days 7 --keep 2 || true
echo "------------------------------------------------------------"
echo
echo "Done. Next scheduled run: tomorrow 02:00 UTC (07:30 IST)."
