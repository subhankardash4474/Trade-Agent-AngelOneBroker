#!/usr/bin/env bash
# =============================================================================
# Dead-man's-switch cron wrapper
# =============================================================================
# Designed to run every 5 min via cron on the cloud VM. Calls
# tools/health_check.py inside the running container and, if the daemon's
# heartbeat is stale or the container is down, emits a Resend email and
# logs to a dedicated rotating log.
#
# Why inside the container: health.json lives at /app/logs/health.json
# inside the container == ./logs/health.json on the host (volume-mounted),
# so we could also check from the host. Running inside the container has
# the side benefit of also alerting if the container itself has crashed
# (because docker exec will fail).
#
# Cron entry (edit with `crontab -e` as user trader):
#   */5 * * * * /opt/trading-agent/tools/cloud/healthcheck_cron.sh \
#                 >> /opt/trading-agent/logs/cron_healthcheck.log 2>&1
#
# Env required at script invocation time:
#   RESEND_API_KEY   -- copy from .env so cron can read it (see install
#                       step in cloud_mvc_runbook.md)
#   ALERT_RECIPIENT  -- ditto
# =============================================================================
set -uo pipefail  # -e off: we WANT to handle failures and email them

TRADER_HOME="${TRADER_HOME:-/opt/trading-agent}"
CONTAINER="${CONTAINER:-trader}"
MAX_AGE_S="${MAX_AGE_S:-600}"           # alert if heartbeat older than 10 min
PNL_FLOOR="${PNL_FLOOR:--3000}"         # alert if daily PnL drops below -Rs 3000
TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
HOST="$(hostname)"

# Load .env so we get RESEND_API_KEY / ALERT_RECIPIENT in cron context
if [ -f "$TRADER_HOME/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$TRADER_HOME/.env" 2>/dev/null || true
    set +a
fi

send_alert() {
    local subject="$1"
    local body="$2"
    if [ -z "${RESEND_API_KEY:-}" ] || [ -z "${ALERT_RECIPIENT:-}" ]; then
        echo "[$TS] [healthcheck-cron] cannot email -- RESEND_API_KEY or ALERT_RECIPIENT missing"
        return 0
    fi
    curl -fsSL "https://api.resend.com/emails" \
        -H "Authorization: Bearer ${RESEND_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "$(cat <<JSON
{
  "from": "${ALERT_SENDER:-Trading Agent <onboarding@resend.dev>}",
  "to":   ["${ALERT_RECIPIENT}"],
  "subject": "${subject}",
  "text": "${body}"
}
JSON
)" >/dev/null 2>&1 || echo "[$TS] [healthcheck-cron] resend POST failed"
}

# 1. Is the container even running?
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    msg="Container '$CONTAINER' is DOWN on $HOST at $TS. \\
docker ps output: \\
$(docker ps --filter "name=$CONTAINER" --format 'table {{.Names}}\t{{.Status}}\t{{.CreatedAt}}')"
    send_alert "[trader] container DOWN on $HOST" "$msg"
    echo "[$TS] [healthcheck-cron] FAIL: container down on $HOST"
    exit 11
fi

# 2. Heartbeat freshness + PnL floor
OUTPUT="$(docker exec "$CONTAINER" python /app/tools/health_check.py \
            --max-age-seconds "$MAX_AGE_S" --pnl-floor "$PNL_FLOOR" 2>&1)"
RC=$?

case "$RC" in
    0)
        echo "[$TS] [healthcheck-cron] OK: $OUTPUT"
        ;;
    1)
        msg="health.json missing inside container on $HOST. Daemon may have never\\
started, or never reached its first heartbeat. Output:\\
$OUTPUT"
        send_alert "[trader] health.json MISSING on $HOST" "$msg"
        echo "[$TS] [healthcheck-cron] FAIL: file missing"
        ;;
    2)
        msg="Daemon heartbeat is STALE on $HOST (>${MAX_AGE_S}s old).\\
Container is up but Python loop is wedged. Output:\\
$OUTPUT"
        send_alert "[trader] heartbeat STALE on $HOST" "$msg"
        echo "[$TS] [healthcheck-cron] FAIL: stale heartbeat"
        ;;
    3)
        msg="Daily P&L below floor (${PNL_FLOOR} Rs) on $HOST.\\
Output:\\
$OUTPUT"
        send_alert "[trader] PnL FLOOR breached on $HOST" "$msg"
        echo "[$TS] [healthcheck-cron] ALERT: pnl floor"
        ;;
    *)
        msg="Unknown health_check.py exit code $RC on $HOST. Output:\\
$OUTPUT"
        send_alert "[trader] health probe UNKNOWN ($RC) on $HOST" "$msg"
        echo "[$TS] [healthcheck-cron] FAIL: unknown rc=$RC"
        ;;
esac
exit "$RC"
