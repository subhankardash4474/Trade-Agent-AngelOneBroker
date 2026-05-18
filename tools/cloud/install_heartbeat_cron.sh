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
# Usage on the trader VM:
#   bash tools/cloud/install_heartbeat_cron.sh
#
# After install, verify with:
#   crontab -l | grep heartbeat
#
# Manual test (forces a send):
#   cd ~/trading-agent && python tools/send_heartbeat.py --force-send

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${REPO_DIR}/logs/heartbeat_cron.log"

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
CMD_LINE="cd ${REPO_DIR} && ${PYTHON_BIN} tools/send_heartbeat.py >> ${LOG_FILE} 2>&1"

echo "Installing heartbeat cron ${ZONE_NOTE}"
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
( cd "${REPO_DIR}" && "${PYTHON_BIN}" tools/send_heartbeat.py --dry-run ) || true
echo "------------------------------------------------------------"
echo
echo "Done. Heartbeat will fire at the scheduled time on next weekday."
