#!/bin/bash
# =============================================================================
# Install the battery-scheduler systemd unit on the backtester VM.
# =============================================================================
# Run AFTER bootstrap_backtester.sh has built the image and laid down the
# repo at /opt/trading-agent. This script is idempotent.
#
# What it does:
#   1. Copies the canonical queue file into place if absent.
#   2. Installs battery-scheduler.service into /etc/systemd/system/.
#   3. Reloads systemd, enables the unit (auto-start on boot), but does
#      NOT immediately start it -- if a battery container is already
#      running, the scheduler would just wait, which is fine, but
#      operator should explicitly opt in to "start it now".
#   4. Prints next-step instructions.
#
# Run ON the VM (not from laptop):
#   cd /opt/trading-agent
#   sudo bash tools/cloud/install_battery_scheduler.sh
# =============================================================================
set -euo pipefail

TRADER_HOME="${TRADER_HOME:-/opt/trading-agent}"
UNIT_NAME="battery-scheduler.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
QUEUE_PATH="${TRADER_HOME}/data/battery_queue.yaml"
EXAMPLE_PATH="${TRADER_HOME}/tests/fixtures/battery_queue_example.yaml"
SRC_UNIT_PATH="${TRADER_HOME}/tools/cloud/battery-scheduler.service"

log() { printf '[install_scheduler] %s\n' "$*"; }
fail() { printf '[install_scheduler][FATAL] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "must run as root (use sudo)."
[ -f "$SRC_UNIT_PATH" ] || fail "unit file not found at $SRC_UNIT_PATH (run bootstrap_backtester.sh first?)"
[ -f "${TRADER_HOME}/tools/run_battery_queue.py" ] || fail "tools/run_battery_queue.py missing in $TRADER_HOME"
[ -f "$EXAMPLE_PATH" ] || fail "example queue missing at $EXAMPLE_PATH"

# ── 1. Ensure PyYAML is available for the host python3 ──────────────
log "[1/5] Verifying PyYAML on host python3 (the scheduler dep)..."
if ! /usr/bin/python3 -c "import yaml" 2>/dev/null; then
    log "  PyYAML not present; installing via pip..."
    /usr/bin/python3 -m pip install --quiet pyyaml
fi
/usr/bin/python3 -c "import yaml; print('  PyYAML', yaml.__version__)"

# ── 2. Place the queue file if missing ──────────────────────────────
log "[2/5] Queue file..."
if [ ! -f "$QUEUE_PATH" ]; then
    install -o opc -g opc -m 0644 "$EXAMPLE_PATH" "$QUEUE_PATH"
    log "  installed default queue at $QUEUE_PATH"
else
    log "  queue already present at $QUEUE_PATH (leaving as-is)"
fi

# ── 3. Install the systemd unit ────────────────────────────────────
log "[3/5] Installing $UNIT_PATH..."
install -m 0644 "$SRC_UNIT_PATH" "$UNIT_PATH"
systemctl daemon-reload

# ── 4. Enable (auto-start on boot) but don't start yet ─────────────
log "[4/5] Enabling unit (auto-start on boot)..."
systemctl enable "$UNIT_NAME" >/dev/null

# ── 5. Verify the scheduler can at least parse the queue ───────────
log "[5/5] Dry-run sanity check..."
sudo -u opc /usr/bin/python3 "${TRADER_HOME}/tools/run_battery_queue.py" \
    --queue "$QUEUE_PATH" \
    --state "${TRADER_HOME}/data/battery_queue_state.json" \
    --dry-run \
    --no-wait-pre-existing 2>&1 | head -30 || \
    fail "dry-run failed; refusing to enable. Fix the queue file first."

log ""
log "==================================================="
log " Installed. The unit is ENABLED (boot-auto-start) but NOT yet STARTED."
log ""
log "  To start it now (will wait for any running battery first):"
log "    sudo systemctl start $UNIT_NAME"
log ""
log "  Status / logs:"
log "    sudo systemctl status $UNIT_NAME"
log "    sudo journalctl -u $UNIT_NAME -f"
log ""
log "  To edit the queue:"
log "    sudo nano $QUEUE_PATH"
log "    sudo systemctl restart $UNIT_NAME"
log ""
log "  To force re-run of a completed job:"
log "    jq 'del(.jobs[\"<job_name>\"])' ${TRADER_HOME}/data/battery_queue_state.json | sudo tee ${TRADER_HOME}/data/battery_queue_state.json"
log "==================================================="
