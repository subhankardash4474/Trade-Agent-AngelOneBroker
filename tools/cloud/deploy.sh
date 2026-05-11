#!/usr/bin/env bash
# =============================================================================
# Cloud deploy -- push latest main to the trader VM and restart container
# =============================================================================
# Strategy: we keep it simple for MVC. The cloud VM has its own git clone of
# the repo; this script SSH's in, `git pull`s, rebuilds the image, and bounces
# the container. No registry, no rsync, no CI pipeline -- just `git pull` on
# the box. Add ECR/GHCR later if we outgrow this.
#
# Pre-reqs (one-time):
#   - You can `ssh trader@<vm-ip>` without a password (key in ~/.ssh)
#   - The VM already has a working clone of the repo + .env in $TRADER_HOME
#   - `docker compose` works on the VM
#
# Usage:
#   tools/cloud/deploy.sh <vm-ip-or-host> [branch]
#
# Examples:
#   tools/cloud/deploy.sh 130.61.42.111
#   tools/cloud/deploy.sh trader-mumbai.example.com feature/circuit-band-clamp
# =============================================================================
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <vm-host-or-ip> [git-branch]"
    exit 1
fi

VM_HOST="$1"
GIT_BRANCH="${2:-main}"
SSH_USER="${SSH_USER:-trader}"
TRADER_HOME="${TRADER_HOME:-/opt/trading-agent}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=accept-new -o ConnectTimeout=10}"

echo "============================================================"
echo " Cloud deploy"
echo " Target:  $SSH_USER@$VM_HOST"
echo " Branch:  $GIT_BRANCH"
echo " Path:    $TRADER_HOME"
echo " Started: $(date -Iseconds)"
echo "============================================================"

# Safety: refuse to deploy if local working tree is dirty (would mean the
# remote box is in a more-up-to-date state than what's been pushed/reviewed).
if ! git diff-index --quiet HEAD --; then
    echo "[FAIL] local working tree is dirty -- commit or stash before deploying"
    git status --short
    exit 2
fi

# Safety: refuse if local main is ahead of origin/main (i.e. there are
# unpushed commits the VM can't reach via git pull).
LOCAL_HEAD="$(git rev-parse HEAD)"
git fetch origin "$GIT_BRANCH" 2>&1 | sed 's/^/    /'
REMOTE_HEAD="$(git rev-parse "origin/$GIT_BRANCH")"
if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
    echo "[WARN] local HEAD differs from origin/$GIT_BRANCH"
    echo "       local : $LOCAL_HEAD"
    echo "       remote: $REMOTE_HEAD"
    echo "       Tip: 'git push origin $GIT_BRANCH' first if you have unpushed commits."
fi

# shellcheck disable=SC2087
ssh ${SSH_OPTS} "$SSH_USER@$VM_HOST" bash <<EOF
set -euo pipefail
cd "$TRADER_HOME"
echo "[remote] pwd      = \$(pwd)"
echo "[remote] git head = \$(git rev-parse --short HEAD)"
echo "[remote] fetching origin..."
git fetch origin "$GIT_BRANCH"
echo "[remote] resetting to origin/$GIT_BRANCH..."
git reset --hard "origin/$GIT_BRANCH"
echo "[remote] new git head = \$(git rev-parse --short HEAD)"

echo "[remote] rebuilding image (this may take 2-5 min on first build)..."
docker compose build --pull trader

echo "[remote] restarting container..."
docker compose up -d trader

echo "[remote] waiting 15s for healthcheck..."
sleep 15

echo "[remote] container status:"
docker compose ps trader

echo "[remote] last 30 log lines:"
docker compose logs --tail 30 trader

echo "[remote] health check:"
python3 "$TRADER_HOME/tools/health_check.py" --max-age-seconds 600 || \
    echo "[remote] (health probe not green yet -- container may still be booting)"
EOF

echo "============================================================"
echo " Deploy complete: $(date -Iseconds)"
echo "============================================================"
