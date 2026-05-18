#!/usr/bin/env bash
# =============================================================================
# Launch a battery run on the backtester VM (freeze-v2.1 companion)
# =============================================================================
# SSHes to the backtester VM and starts a one-shot `docker run` invocation
# of `tools/run_battery.py`. The container is detached, named after the
# run-id, mounts /opt/trading-agent/{logs,data} so output survives.
#
# Why detached: a single battery-v2 run takes 8-12 h on Ampere; we don't
# want this shell to hold the connection. Use `tools/cloud/follow_battery.sh`
# (or plain `ssh ... docker logs -f`) to tail.
#
# All extra args are forwarded verbatim to run_battery.py. Sensible defaults
# baked in:
#   * --workers 2  (Ampere 2 OCPU shape; bump on bigger shapes)
#   * --days 90    (battery-v2 default)
#   * --universe-file tests/fixtures/battery_v2_universe.json
#
# Env:
#   BACKTESTER_VM_HOST  (required)  Public IP of the backtester VM
#   BACKTESTER_SSH_USER (optional)  default: opc
#   BACKTESTER_SSH_KEY  (optional)  default: $HOME/.ssh/oci_trader_key
#   BACKTESTER_RUN_ID   (optional)  default: battery_freeze_v21_<utc_ts>
# =============================================================================
set -euo pipefail

if [ -z "${BACKTESTER_VM_HOST:-}" ]; then
    echo "[launch_battery][FATAL] BACKTESTER_VM_HOST is not set." >&2
    echo "  Export it once: export BACKTESTER_VM_HOST=132.45.67.89" >&2
    exit 1
fi

SSH_USER="${BACKTESTER_SSH_USER:-opc}"
SSH_KEY="${BACKTESTER_SSH_KEY:-$HOME/.ssh/oci_trader_key}"
RUN_ID="${BACKTESTER_RUN_ID:-battery_freeze_v21_$(date -u +%Y%m%dT%H%M%S)}"
TRADER_HOME="/opt/trading-agent"

# Defaults: caller-overridable. Anything passed after `--` is forwarded.
DEFAULT_ARGS=(
    --days 90
    --interval 5m
    --workers 2
    --universe-file tests/fixtures/battery_v2_universe.json
)

# Build the final argv by combining defaults with caller args, but caller
# args take precedence: appended last so argparse picks them up.
FINAL_ARGS=("${DEFAULT_ARGS[@]}" "$@" --run-id "$RUN_ID")

if [ ! -f "$SSH_KEY" ]; then
    echo "[launch_battery][FATAL] SSH key not found: $SSH_KEY" >&2
    exit 2
fi

SSH_OPTS=(
    -i "$SSH_KEY"
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=accept-new
    -o BatchMode=yes
)

# Stringify args for the remote shell. printf %q quotes each arg safely so
# values with spaces / glob chars survive the SSH shell hop.
REMOTE_ARGS=""
for a in "${FINAL_ARGS[@]}"; do
    REMOTE_ARGS+=" $(printf '%q' "$a")"
done

echo "============================================================"
echo " Launching battery run"
echo "   Host    : ${SSH_USER}@${BACKTESTER_VM_HOST}"
echo "   Run ID  : ${RUN_ID}"
echo "   Args    : ${FINAL_ARGS[*]}"
echo "============================================================"

# We mount data and logs from the host so result tarballs survive a
# container OOM. We DO NOT mount .env -- the BACKTESTER_MODE=1 assertion
# would refuse to start anyway if broker creds leaked, but extra
# defence-in-depth here is free.
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${BACKTESTER_VM_HOST}" "bash -lc '
    set -euo pipefail
    cd ${TRADER_HOME}

    # Pre-flight: confirm no broker .env got rsynced here. The
    # assertion in battery.main() will catch it too, but failing
    # before docker run avoids a 10s container spin-up on every typo.
    if [ -f .env ] && grep -qE \"^(ANGELONE|SMARTAPI|KITE)_\" .env 2>/dev/null; then
        echo \"[launch_battery][FATAL] backtester VM has broker creds in .env -- refusing.\" >&2
        exit 5
    fi

    sudo docker run -d --rm \
        --name ${RUN_ID} \
        -e BACKTESTER_MODE=1 \
        -v ${TRADER_HOME}/logs:/app/logs \
        -v ${TRADER_HOME}/data:/app/data \
        trading-agent:latest \
        python tools/run_battery.py ${REMOTE_ARGS}

    sudo docker ps --filter name=${RUN_ID} --format \"  {{.ID}}  {{.Status}}  {{.Names}}\"
'"

echo ""
echo "Run started. Useful follow-ups:"
echo ""
echo "  # Tail the live log:"
echo "  ssh -i ${SSH_KEY} ${SSH_USER}@${BACKTESTER_VM_HOST} sudo docker logs -f ${RUN_ID}"
echo ""
echo "  # Check status without tailing:"
echo "  ssh -i ${SSH_KEY} ${SSH_USER}@${BACKTESTER_VM_HOST} sudo docker ps -a --filter name=${RUN_ID}"
echo ""
echo "  # Pull results once it finishes (Windows):"
echo "  .\\tools\\cloud\\pull_battery_results.ps1 -RunId ${RUN_ID}"
echo ""
echo "  # Kill the run if you need to (safe -- battery resumes from per-variant JSONs):"
echo "  ssh -i ${SSH_KEY} ${SSH_USER}@${BACKTESTER_VM_HOST} sudo docker stop ${RUN_ID}"
