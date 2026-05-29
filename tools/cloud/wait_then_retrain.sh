#!/usr/bin/env bash
# =============================================================================
# Wait-then-retrain wrapper — backtester VM
# =============================================================================
# Polls for "no battery_* container is currently running" once every 60
# seconds, then invokes tools/cloud/run_retrain_on_backtester.sh.
#
# Designed to be launched while slot-#3 (V19) is still in flight, so the
# retrain auto-fires the moment V19 exits without the operator needing
# to babysit the SSH session.
#
# Idempotent: if no battery container is running at launch time, the
# poll loop exits on the first tick and the retrain starts immediately.
#
# Logs everything to /opt/trading-agent/logs/wait_then_retrain_<TS>.log.
# Sample usage from the operator's laptop (Windows PowerShell):
#
#   ssh -i $env:USERPROFILE\.ssh\oci_trader_key opc@$env:BACKTESTER_VM_HOST `
#       "cd /opt/trading-agent && nohup bash tools/cloud/wait_then_retrain.sh `
#        > logs/wait_then_retrain_<TS>.log 2>&1 &"
#
# Or, in bash:
#
#   ssh opc@$BACKTESTER_VM_HOST \
#       "cd /opt/trading-agent && nohup bash tools/cloud/wait_then_retrain.sh \
#        > logs/wait_then_retrain_$(date -u +%Y%m%dT%H%MZ).log 2>&1 &"
#
# Watch progress without holding the connection open:
#
#   ssh opc@$BACKTESTER_VM_HOST 'tail -f /opt/trading-agent/logs/retrain_*.log'
# =============================================================================
set -euo pipefail

REPO=/opt/trading-agent
TS=$(date -u +%Y%m%dT%H%MZ)
LOG="$REPO/logs/wait_then_retrain_${TS}.log"

mkdir -p "$REPO/logs"
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo " wait_then_retrain.sh started at $TS"
echo " polling every 60s for no battery_* containers..."
echo "============================================================"

POLL_INTERVAL=60
WAITED_S=0
MAX_WAIT_S=$((6 * 3600))   # hard cap 6h — safety net if a container hangs

while true; do
    RUNNING=$(sudo docker ps --format '{{.Names}}' | grep -E '^battery_' || true)
    if [ -z "$RUNNING" ]; then
        echo "[$(date -u +%H:%M:%SZ)] no battery_* container active — proceeding to retrain."
        break
    fi
    echo "[$(date -u +%H:%M:%SZ)] still waiting; active: $(echo "$RUNNING" | tr '\n' ' ')"
    sleep "$POLL_INTERVAL"
    WAITED_S=$((WAITED_S + POLL_INTERVAL))
    if [ "$WAITED_S" -ge "$MAX_WAIT_S" ]; then
        echo "[FATAL] waited $WAITED_S seconds (>${MAX_WAIT_S}s cap); aborting." >&2
        echo "        Investigate hung battery container manually." >&2
        exit 30
    fi
done

echo
echo "Triggering: bash $REPO/tools/cloud/run_retrain_on_backtester.sh"
echo
exec sudo bash "$REPO/tools/cloud/run_retrain_on_backtester.sh"
