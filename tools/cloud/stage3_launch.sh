#!/usr/bin/env bash
# =============================================================================
# Stage 3 launch -- controlled 5-stock live basket (single session)
# =============================================================================
# Runs ON the trader VM (80.225.251.79). Sequence:
#
#   1. ABORT if anything stale -- existing daemon, EMERGENCY_STOP, open
#      positions, missing creds, missing overlay.
#   2. Tear down the paper-trading daemon (docker compose down).
#   3. Snapshot DB + logs to a Stage 3 archive folder.
#   4. Smoke-test AngelOne auth (read-only). Aborts if broker rejects.
#   5. Launch the Stage 3 daemon (docker compose up -d w/ stage3 override).
#   6. Tail the first 60s of logs so the operator sees the boot banner
#      and can confirm the mode line says LIVE.
#
# This script DELIBERATELY does NOT remove the EMERGENCY_STOP file -- the
# operator removes it manually right before 09:30 IST as the final go-gate.
# If this script ran end-to-end without manual intervention, a bug in the
# pre-flight could push us live before we're ready.
#
# Usage (on the VM):
#   cd /opt/trading-agent
#   ./tools/cloud/stage3_launch.sh
#
# Dry-run (validate everything, don't actually launch):
#   ./tools/cloud/stage3_launch.sh --dry-run
# =============================================================================

set -euo pipefail

DRY_RUN="false"
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="true"
fi

ROOT="/opt/trading-agent"
cd "$ROOT"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
    echo
    echo -e "${BOLD}=== $1 ===${NC}"
}

fail() {
    echo -e "${RED}BLOCKING: $1${NC}" >&2
    exit 1
}

warn() {
    echo -e "${YELLOW}WARN: $1${NC}"
}

ok() {
    echo -e "${GREEN}OK${NC}: $1"
}

# -----------------------------------------------------------------------------
# 1. Pre-flight
# -----------------------------------------------------------------------------
banner "Stage 3 pre-flight"

# 1.1 -- date/time
NOW_IST=$(TZ='Asia/Kolkata' date '+%Y-%m-%d %H:%M:%S %Z')
echo "current time     : $NOW_IST"
DOW=$(TZ='Asia/Kolkata' date '+%u')   # 1=Mon, 7=Sun
if [[ "$DOW" -ge 6 ]]; then
    warn "today is a weekend -- NSE is closed. Continuing for dry-run / staging."
fi

# 1.2 -- overlay file exists and parses
OVERLAY="config_overlays/stage3.yaml"
[[ -f "$OVERLAY" ]] || fail "overlay file missing: $OVERLAY"
python3 -c "import yaml; yaml.safe_load(open('$OVERLAY'))" \
    || fail "overlay file is not valid YAML"
ok "overlay file present and parses: $OVERLAY"

# 1.3 -- no daemon already running
RUNNING=$(docker ps --filter 'name=trader' --format '{{.Names}} ({{.Status}})' || true)
if [[ -n "$RUNNING" ]]; then
    echo "existing daemon containers:"
    echo "$RUNNING" | sed 's/^/    /'
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "would tear down paper daemon now (dry-run: skipped)"
    else
        echo "tearing down paper daemon..."
        docker compose down --remove-orphans
        ok "paper daemon stopped"
    fi
else
    ok "no daemon currently running"
fi

# 1.4 -- EMERGENCY_STOP file present (kill-switch ARMED)
# Note: at LAUNCH time the file must be REMOVED so the daemon can start.
# But pre-flight time is BEFORE launch -- we want the file to exist as
# evidence the kill-switch is set up and the operator knows where to find
# it. Operator removes it right before pressing GO.
if [[ -f EMERGENCY_STOP ]]; then
    ok "EMERGENCY_STOP file exists (kill-switch armed)"
    echo "    REMINDER: remove this file IMMEDIATELY before launch:"
    echo "        rm $ROOT/EMERGENCY_STOP"
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "dry-run: file will NOT be removed. Real launch needs manual removal."
    fi
else
    warn "EMERGENCY_STOP file is NOT present."
    echo "    For Stage 3, we recommend ARMING it before launch so the"
    echo "    operator has a one-keystroke kill (touch EMERGENCY_STOP)."
    echo "    Creating an empty one now:"
    if [[ "$DRY_RUN" != "true" ]]; then
        touch EMERGENCY_STOP
        ok "EMERGENCY_STOP file created (armed)"
    fi
fi

# 1.5 -- credentials in .env
[[ -f .env ]] || fail ".env file missing"
REQUIRED_VARS=(ANGELONE_API_KEY ANGELONE_API_SECRET ANGELONE_CLIENT_ID
               ANGELONE_PASSWORD ANGELONE_TOTP_SECRET)
for v in "${REQUIRED_VARS[@]}"; do
    if ! grep -qE "^${v}=.+" .env; then
        fail ".env is missing $v (or it's empty)"
    fi
done
ok "all 5 AngelOne credentials present in .env"

# 1.6 -- broker auth smoke test (read-only, no orders)
echo "running AngelOne auth smoke test (read-only)..."
if docker run --rm \
    --env-file .env \
    -v "$ROOT/data:/app/data:ro" \
    -v "$ROOT/config.yaml:/app/config.yaml:ro" \
    -v "$ROOT/config_overlays:/app/config_overlays:ro" \
    trading-agent:latest \
    python tools/test_angelone_auth.py 2>&1 | tail -n 25; then
    ok "broker auth smoke test passed"
else
    fail "broker auth smoke test failed -- check creds + IP whitelist"
fi

# 1.7 -- DB sanity (no open positions, no integrity issues)
echo "running DB sanity check..."
OPEN_POS=$(python3 -c "
import sqlite3
c = sqlite3.connect('data/trading_agent.db')
print(c.execute('SELECT COUNT(*) FROM open_positions').fetchone()[0])
" 2>/dev/null || echo "ERROR")

if [[ "$OPEN_POS" == "ERROR" ]]; then
    fail "cannot read open_positions from DB"
elif [[ "$OPEN_POS" -gt 0 ]]; then
    fail "$OPEN_POS open position(s) in DB -- Stage 3 demands a clean slate. Reconcile + flatten first."
else
    ok "DB has 0 open positions"
fi

# 1.8 -- archive existing logs + DB snapshot (so post-mortem has a baseline)
STAMP=$(date -u +%Y%m%d_%H%M%S)
ARCHIVE_DIR="$ROOT/archive/stage3_$STAMP"
if [[ "$DRY_RUN" != "true" ]]; then
    mkdir -p "$ARCHIVE_DIR"
    cp -a data/trading_agent.db "$ARCHIVE_DIR/trading_agent.pre-stage3.db" 2>/dev/null || true
    cp -a logs/. "$ARCHIVE_DIR/logs.pre-stage3/" 2>/dev/null || true
    ok "pre-launch snapshot at $ARCHIVE_DIR"
else
    warn "dry-run: snapshot skipped"
fi

# -----------------------------------------------------------------------------
# 2. Final go-gate
# -----------------------------------------------------------------------------
banner "Stage 3 pre-flight result"

if [[ "$DRY_RUN" == "true" ]]; then
    echo -e "${GREEN}DRY-RUN GREEN${NC} -- all pre-flight checks passed."
    echo
    echo "To actually launch:"
    echo "  1. Verify it is 09:25-09:29 IST on a trading day."
    echo "  2. Remove the kill-switch: rm EMERGENCY_STOP"
    echo "  3. Run: $0    (no --dry-run)"
    exit 0
fi

if [[ -f EMERGENCY_STOP ]]; then
    fail "EMERGENCY_STOP still present -- daemon will refuse to start. Remove it manually if you really want to go live, then re-run this script."
fi

# -----------------------------------------------------------------------------
# 3. Launch
# -----------------------------------------------------------------------------
banner "Stage 3 launch"

echo "starting Stage 3 daemon (live basket, --max-loss-rs 500, --single-shot)..."
docker compose -f docker-compose.yml -f docker-compose.stage3.yml up -d trader

ok "Stage 3 daemon started"

echo
echo "tailing the first 60s of daemon logs so you can verify the mode line:"
echo "---"
timeout 60 docker compose -f docker-compose.yml -f docker-compose.stage3.yml \
    logs -f trader || true
echo "---"

banner "Stage 3 launch complete"
echo "  Container:   trader-stage3"
echo "  Live logs:   docker compose -f docker-compose.yml -f docker-compose.stage3.yml logs -f trader"
echo "  Kill switch: touch $ROOT/EMERGENCY_STOP   (daemon flattens + halts)"
echo "  Hard stop:   docker compose -f docker-compose.yml -f docker-compose.stage3.yml down"
echo "  Mid-session monitoring: tail -f $ROOT/logs/trading_agent_$(date +%Y-%m-%d).log"
echo
echo "Post-session (after 12:30 IST or kill-switch):"
echo "  python tools/reconcile_trade_book.py --date $(date +%Y-%m-%d)"
echo "  python tools/trade_postmortem.py --date $(date +%Y-%m-%d)"
echo
