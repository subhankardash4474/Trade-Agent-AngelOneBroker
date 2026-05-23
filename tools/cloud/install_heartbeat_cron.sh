#!/usr/bin/env bash
# install_heartbeat_cron.sh
# =========================
# Install / refresh the cron entry that runs the daily heartbeat email
# from tools/send_heartbeat.py.
#
# Idempotent: re-running replaces the existing line.
# Respects VM timezone: if the VM is on UTC, the cron line schedules
# 03:40 UTC == 09:10 IST. If on IST already, schedules 09:10.
#
# Two run modes:
#   1) Host-python   (default): cron runs `python3 tools/send_heartbeat.py`.
#                    Requires pytz + yaml + project deps on the host.
#   2) Docker-exec   (--container NAME): cron runs
#                    `sudo docker exec NAME python tools/send_heartbeat.py`.
#                    Use this on the trader VM where credentials
#                    (RESEND_API_KEY) and dependencies live inside the
#                    container -- not on the host.
#
# Usage on the trader VM (Docker mode):
#   bash tools/cloud/install_heartbeat_cron.sh --container trader
#
# After install, verify with:
#   crontab -l | grep heartbeat
#
# Manual test (forces a send) -- container mode:
#   sudo docker exec trader python tools/send_heartbeat.py --force-send
# Host mode:
#   cd ~/trading-agent && python tools/send_heartbeat.py --force-send

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${REPO_DIR}/logs/heartbeat_cron.log"
CONTAINER=""
SUDO_BIN="${SUDO_BIN:-sudo}"

while [ $# -gt 0 ]; do
    case "$1" in
        --container)
            CONTAINER="$2"; shift 2;;
        --container=*)
            CONTAINER="${1#--container=}"; shift;;
        -h|--help)
            sed -n '1,30p' "$0"; exit 0;;
        *)
            echo "ERROR: unknown argument: $1" >&2; exit 2;;
    esac
done

if [ -n "${CONTAINER}" ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: --container ${CONTAINER} given but docker not on PATH." >&2
        exit 1
    fi
    if ! ${SUDO_BIN} -n docker inspect "${CONTAINER}" >/dev/null 2>&1; then
        echo "ERROR: docker container '${CONTAINER}' not found / no sudo access." >&2
        echo "       Try: ${SUDO_BIN} docker ps   to confirm." >&2
        exit 1
    fi
fi

if [ ! -f "${REPO_DIR}/tools/send_heartbeat.py" ]; then
    echo "ERROR: ${REPO_DIR}/tools/send_heartbeat.py not found." >&2
    echo "       Set REPO_DIR=/path/to/repo and re-run if the auto-detect failed." >&2
    exit 1
fi

# Determine the schedule line based on the VM's timezone.
TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || echo "Etc/UTC")"
case "${TZ_NAME}" in
    *Kolkata*|*Calcutta*|Asia/Kolkata)
        # IST host: just use 09:10 local time, Mon-Fri.
        SCHEDULE="10 9 * * 1-5"
        ZONE_NOTE="(IST host -- runs 09:10 local)"
        ;;
    *)
        # Default to UTC host: 09:10 IST == 03:40 UTC.
        SCHEDULE="40 3 * * 1-5"
        ZONE_NOTE="(non-IST host -- runs 03:40 UTC == 09:10 IST)"
        ;;
esac

# A unique marker comment so this script can find / replace its own
# line on subsequent runs without disturbing other cron entries.
MARKER="# heartbeat-cron (managed by install_heartbeat_cron.sh)"
if [ -n "${CONTAINER}" ]; then
    CMD_LINE="${SUDO_BIN} -n docker exec ${CONTAINER} python tools/send_heartbeat.py >> ${LOG_FILE} 2>&1"
    MODE_NOTE="docker-exec into container '${CONTAINER}'"
else
    CMD_LINE="cd ${REPO_DIR} && ${PYTHON_BIN} tools/send_heartbeat.py >> ${LOG_FILE} 2>&1"
    MODE_NOTE="host python (${PYTHON_BIN})"
fi

echo "Installing heartbeat cron ${ZONE_NOTE}"
echo "  Mode:     ${MODE_NOTE}"
echo "  Schedule: ${SCHEDULE}"
echo "  Repo:     ${REPO_DIR}"
echo "  Log:      ${LOG_FILE}"
echo

# Read current crontab, strip any prior heartbeat lines + marker, then
# append the fresh entry.
CURRENT="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf "%s\n" "${CURRENT}" \
            | grep -vF "${MARKER}" \
            | grep -vF "tools/send_heartbeat.py" \
            || true)"

mkdir -p "$(dirname "${LOG_FILE}")"

{
    if [ -n "${FILTERED}" ]; then
        printf "%s\n" "${FILTERED}"
    fi
    echo "${MARKER}"
    echo "${SCHEDULE} ${CMD_LINE}"
} | crontab -

echo "Installed. Current heartbeat-related crontab lines:"
crontab -l | grep -E "heartbeat|send_heartbeat" || echo "  (none found -- install failed?)"

# Smoke test in dry-run mode so the operator can see the body the
# system would have sent. Does NOT actually send during install.
echo
echo "Dry-run preview of tomorrow's heartbeat body:"
echo "------------------------------------------------------------"
if [ -n "${CONTAINER}" ]; then
    ${SUDO_BIN} -n docker exec "${CONTAINER}" python tools/send_heartbeat.py --dry-run || true
else
    ( cd "${REPO_DIR}" && "${PYTHON_BIN}" tools/send_heartbeat.py --dry-run ) || true
fi
echo "------------------------------------------------------------"
echo
echo "Done. Heartbeat will fire at the scheduled time on next weekday."
