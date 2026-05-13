#!/usr/bin/env bash
# Ampere A1 Free-Tier capacity watcher
# =============================================================================
# OCI Always-Free includes 4 OCPU + 24 GB RAM of VM.Standard.A1.Flex, but
# capacity in ap-mumbai-1 / ap-hyderabad-1 is frequently exhausted. This
# script polls `oci compute instance launch` every N minutes until OCI
# yields capacity, then exits with the new instance's OCID + public IP.
#
# One-time setup: see docs/ampere_capacity_watcher_setup.md
#
# Usage:
#   bash tools/cloud/ampere_capacity_watcher.sh                  # defaults
#   bash tools/cloud/ampere_capacity_watcher.sh --interval 5     # 5 min cadence
#   bash tools/cloud/ampere_capacity_watcher.sh --max-hours 48   # 48 h timeout
#   bash tools/cloud/ampere_capacity_watcher.sh --dry-run        # validate; no launch
#
# Recommended invocation: run from the trader VM (always-on) under tmux:
#   ssh ubuntu@<trader-ip>
#   tmux new -s ampere
#   nohup bash tools/cloud/ampere_capacity_watcher.sh > ~/ampere_watcher.log 2>&1 &
#   tmux detach   (Ctrl-b d)
# Then check progress later with:
#   tail -f ~/ampere_watcher.log
# =============================================================================

set -euo pipefail

CONFIG_FILE="${AMPERE_WATCHER_CONFIG:-$HOME/.ampere_watcher.env}"
INTERVAL_MIN=10
MAX_HOURS=48
DRY_RUN=0

print_help() {
    sed -n '2,28p' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval)   INTERVAL_MIN="$2"; shift 2;;
        --max-hours)  MAX_HOURS="$2";    shift 2;;
        --dry-run)    DRY_RUN=1;         shift;;
        --config)     CONFIG_FILE="$2";  shift 2;;
        -h|--help)    print_help; exit 0;;
        *) echo "unknown arg: $1"; print_help; exit 2;;
    esac
done

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "config not found: $CONFIG_FILE"
    echo "see docs/ampere_capacity_watcher_setup.md for setup instructions"
    exit 2
fi

# Source the config in a subshell-like way -- we explicitly list the vars we
# expect rather than slurping everything into the global namespace.
# shellcheck disable=SC1090
source "$CONFIG_FILE"

required_vars=(COMPARTMENT_OCID SUBNET_OCID IMAGE_OCID
               AVAILABILITY_DOMAIN SSH_PUBLIC_KEY_PATH DISPLAY_NAME)
for v in "${required_vars[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        echo "missing required config var: $v"
        echo "see docs/ampere_capacity_watcher_setup.md"
        exit 2
    fi
done

OCPUS="${OCPUS:-2}"
MEMORY_GB="${MEMORY_GB:-12}"

if ! command -v oci >/dev/null 2>&1; then
    echo "ABORT: 'oci' CLI not on PATH. Install with: pip install oci-cli"
    exit 2
fi

if [[ ! -f "$SSH_PUBLIC_KEY_PATH" ]]; then
    echo "ABORT: SSH_PUBLIC_KEY_PATH points at non-existent file: $SSH_PUBLIC_KEY_PATH"
    exit 2
fi

LOG_FILE="${LOG_FILE:-$HOME/ampere_watcher.log}"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    local msg="$*"
    printf '[%s] %s\n' "$(date '+%F %T %Z')" "$msg" | tee -a "$LOG_FILE"
}

attempt_launch() {
    # Returns 0 on instance up; nonzero on any failure. stdout=API response.
    # Metadata is built via python3.json.dumps so the SSH public key's
    # trailing newline (and any future special chars) never break the
    # JSON contract. Naively splicing $(cat pubkey) leaves the newline
    # in and OCI fails the call with "Parameter 'metadata' must be in
    # JSON format." (observed 2026-05-13 on attempt #1).
    local metadata_json
    metadata_json=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    pubkey = f.read().strip()
print(json.dumps({'ssh_authorized_keys': pubkey}))
" "$SSH_PUBLIC_KEY_PATH")
    # Also suppress the cosmetic "OCI_API_KEY label" warning so it
    # doesn't make every failure log look scarier than it is.
    export SUPPRESS_LABEL_WARNING=True
    set +e
    oci compute instance launch \
        --availability-domain "$AVAILABILITY_DOMAIN" \
        --compartment-id     "$COMPARTMENT_OCID" \
        --shape              VM.Standard.A1.Flex \
        --shape-config       "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}" \
        --image-id           "$IMAGE_OCID" \
        --subnet-id          "$SUBNET_OCID" \
        --display-name       "$DISPLAY_NAME" \
        --metadata           "$metadata_json" \
        --assign-public-ip   true \
        --wait-for-state     RUNNING \
        2>&1
    local rc=$?
    set -e
    return $rc
}

if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: config validated. Would launch A1.Flex ${OCPUS}/${MEMORY_GB}GB"
    log "  AD=$AVAILABILITY_DOMAIN"
    log "  compartment=$COMPARTMENT_OCID"
    log "  subnet=$SUBNET_OCID"
    log "  image=$IMAGE_OCID"
    log "  display_name=$DISPLAY_NAME"
    log "  ssh_key=$SSH_PUBLIC_KEY_PATH"
    exit 0
fi

start_ts=$(date +%s)
deadline_ts=$((start_ts + MAX_HOURS * 3600))
attempt=0

log "============================================================"
log "Ampere watcher started. Polling every ${INTERVAL_MIN} min."
log "Will exit on success, on non-capacity error, or after ${MAX_HOURS} h."
log "Shape: A1.Flex ${OCPUS} OCPU / ${MEMORY_GB} GB"
log "Region/AD: $AVAILABILITY_DOMAIN"
log "============================================================"

while [[ $(date +%s) -lt $deadline_ts ]]; do
    attempt=$((attempt + 1))
    elapsed_min=$(( ($(date +%s) - start_ts) / 60 ))
    log "attempt #${attempt} (t+${elapsed_min}m) ..."

    if launch_output=$(attempt_launch); then
        log "SUCCESS on attempt #${attempt}"
        # Parse the JSON returned by `oci compute instance launch`. We tolerate
        # parser failures gracefully -- the operator can always look up the
        # instance from the OCI console.
        instance_ocid=$(printf '%s' "$launch_output" \
            | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['data']['id'])" \
            2>/dev/null || true)
        log "instance OCID: ${instance_ocid:-<parse-failed>}"

        if [[ -n "$instance_ocid" ]]; then
            pub_ip=$(oci compute instance list-vnics --instance-id "$instance_ocid" 2>/dev/null \
                | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['data'][0].get('public-ip',''))" \
                2>/dev/null || true)
            log "public IP:     ${pub_ip:-<lookup-failed>}"
        fi

        log "============================================================"
        log "Backtester VM is up."
        log "Next step: Stage 1 bootstrap (see docs/backtester_vm_runbook.md)."
        log "============================================================"
        exit 0
    fi

    # Detect "out of capacity" from the launch output to decide whether to
    # retry. Anything else (auth, OCID typo, quota exceeded) is a permanent
    # config problem and should bail loudly rather than waste a 48h polling
    # window logging the same error.
    if echo "$launch_output" | grep -qiE "out of host capacity|out of capacity|capacity.* not available"; then
        log "no capacity yet -- sleeping ${INTERVAL_MIN}m"
        sleep $((INTERVAL_MIN * 60))
        continue
    fi

    log "ABORT: non-capacity error on attempt #${attempt}"
    log "--- launch_output ---"
    log "$launch_output"
    log "---------------------"
    log "this is most likely a config problem (bad OCID, auth, quota)."
    log "fix it, then restart the watcher."
    exit 1
done

log "TIMEOUT: ${MAX_HOURS} h elapsed (${attempt} attempts) without capacity."
log "either bump --max-hours or try a different region (ap-hyderabad-1)."
exit 3
