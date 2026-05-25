#!/usr/bin/env bash
# install_watchdog_cron.sh
# ========================
# Install / refresh the cron entry that runs the daemon-liveness watchdog
# from tools/watchdog_check.py every 5 minutes during the trading window.
#
# Why a separate cron from the heartbeat
# --------------------------------------
# * Heartbeat is a daily 09:10 IST email confirming everything looks fine.
# * Watchdog is an intra-day silent-hang detector: 5-minute polling of
#   logs/health.json mtime, with state-machine deduplication so a 4-hour
#   hang produces ~5 alerts (every 1 hour escalation), not 48.
#
# This was added 2026-05-25 in direct response to the 11-hour daemon
# silent hang on 2026-05-22 (12:23 IST -> 23:35 IST), which the daily
# heartbeat couldn't detect because by the time the next 09:10 fired
# the hang had already cleared overnight via container restart.
#
# Idempotent: re-running replaces the existing line.
# Respects VM timezone for the trading-window check (handled inside
# watchdog_check.py, not here -- the cron just fires every 5 min).
#
# Two run modes (mirrors install_heartbeat_cron.sh):
#   1) Host-python   (default): cron runs `python3 tools/watchdog_check.py`.
#                    Requires pytz + yaml + project deps on the host.
#   2) Docker-exec   (--container NAME): cron runs
#                    `sudo docker exec NAME python tools/watchdog_check.py`.
#                    Use on the trader VM where credentials + deps live
#                    inside the container.
#
# Usage on the trader VM (Docker mode):
#   bash tools/cloud/install_watchdog_cron.sh --container trader
#
# After install, verify with:
#   crontab -l | grep watchdog
#
# Manual test (forces a check; does NOT send unless stale):
#   sudo docker exec trader python tools/watchdog_check.py
#   tail -1 /opt/trading-agent/logs/watchdog_cron.log
#   cat /opt/trading-agent/logs/watchdog_state.json

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${REPO_DIR}/logs/watchdog_cron.log"
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
        exit 1
    fi
fi

if [ ! -f "${REPO_DIR}/tools/watchdog_check.py" ]; then
    echo "ERROR: ${REPO_DIR}/tools/watchdog_check.py not found." >&2
    exit 1
fi

# Same log-file pre-create dance as the heartbeat installer
# (see install_heartbeat_cron.sh comments for the full rationale --
# cron silently fails on `>>` redirect if the file doesn't exist or
# isn't writable by the cron user, which is what bit us on 2026-05-25).
LOG_FILE_DIR="$(dirname "${LOG_FILE}")"
if [ ! -d "${LOG_FILE_DIR}" ]; then
    mkdir -p "${LOG_FILE_DIR}" 2>/dev/null \
        || ${SUDO_BIN} -n mkdir -p "${LOG_FILE_DIR}" 2>/dev/null \
        || { echo "ERROR: cannot create ${LOG_FILE_DIR}" >&2; exit 1; }
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

# Schedule: every 5 minutes, all hours (the watchdog itself silences
# off-hours alerting via _is_trading_window). We deliberately don't
# constrain the cron to 9-16 IST because that would miss recovery
# events (e.g. daemon restarted at 17:00 after a hang -- we want to
# clear the STALE state so the next morning's check is clean).
SCHEDULE="*/5 * * * *"

MARKER="# watchdog-cron (managed by install_watchdog_cron.sh)"
if [ -n "${CONTAINER}" ]; then
    CMD_LINE="${SUDO_BIN} -n docker exec ${CONTAINER} python tools/watchdog_check.py >> ${LOG_FILE} 2>&1"
    MODE_NOTE="docker-exec into container '${CONTAINER}'"
else
    CMD_LINE="cd ${REPO_DIR} && ${PYTHON_BIN} tools/watchdog_check.py >> ${LOG_FILE} 2>&1"
    MODE_NOTE="host python (${PYTHON_BIN})"
fi

echo "Installing watchdog cron"
echo "  Mode:     ${MODE_NOTE}"
echo "  Schedule: ${SCHEDULE}  (every 5 min)"
echo "  Repo:     ${REPO_DIR}"
echo "  Log:      ${LOG_FILE}"
echo

CURRENT="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf "%s\n" "${CURRENT}" \
            | grep -vF "${MARKER}" \
            | grep -vF "tools/watchdog_check.py" \
            || true)"

mkdir -p "$(dirname "${LOG_FILE}")"

{
    if [ -n "${FILTERED}" ]; then
        printf "%s\n" "${FILTERED}"
    fi
    echo "${MARKER}"
    echo "${SCHEDULE} ${CMD_LINE}"
} | crontab -

echo "Installed. Current watchdog-related crontab lines:"
crontab -l | grep -E "watchdog|watchdog_check" || echo "  (none found -- install failed?)"

# Run the watchdog once immediately so the operator sees:
#   1. the cron-mode invocation works
#   2. the initial state file gets created (no spurious 'recovered' on
#      first real cron tick)
echo
echo "Smoke test (one-shot invocation -- alerts only if currently stale):"
echo "------------------------------------------------------------"
if [ -n "${CONTAINER}" ]; then
    ${SUDO_BIN} -n docker exec "${CONTAINER}" python tools/watchdog_check.py || true
else
    ( cd "${REPO_DIR}" && "${PYTHON_BIN}" tools/watchdog_check.py ) || true
fi
echo "------------------------------------------------------------"
echo
echo "Done. Watchdog will run every 5 min from now on."
echo "Tail the log with: tail -f ${LOG_FILE}"
