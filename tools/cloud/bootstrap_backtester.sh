#!/usr/bin/env bash
# =============================================================================
# Backtester VM bootstrap (freeze-v2.1 companion)
# =============================================================================
# Stands up a fresh OCI VM as a *backtester* role -- runs `tools/run_battery.py`
# inside Docker, isolated from the live trader VM, no broker credentials.
#
# Idempotent: re-runs safely. Detects distro (Oracle Linux / Ubuntu / Amazon
# Linux), installs Docker + git + Python, clones the repo, builds the
# `trading-agent:latest` image, and runs a one-shot smoke test that the
# battery harness loads.
#
# This is the *backtester* counterpart to `oci_bootstrap.sh`. The key
# differences vs the trader bootstrap:
#   * No `trader` system user is created (this VM never runs a daemon as a
#     service; batteries are launched as one-shot docker run jobs).
#   * No .env scaffolding (the BACKTESTER_MODE assertion in battery.py
#     refuses to start if broker creds are present).
#   * Boot volume is sized for cached market_data.pkl (~1 GB) plus result
#     tarballs.
#
# Usage (from the laptop, NOT on the VM):
#   tools/cloud/bootstrap_backtester.sh \
#       <BACKTESTER_IP> \
#       <git_clone_url> \
#       [branch]
#
# Example:
#   tools/cloud/bootstrap_backtester.sh 132.45.67.89 \
#       https://github.com/subhanda/trading-agent.git \
#       freeze-v2.1
#
# Required: ssh key at $HOME/.ssh/oci_trader_key (same key as trader VM)
#           OR -i passed via $SSH_KEY env.
# =============================================================================
set -euo pipefail

if [ "$#" -lt 2 ]; then
    cat <<EOF >&2
Usage: $0 <backtester_ip> <git_clone_url> [branch]

Required arguments:
  backtester_ip    Public IP of the backtester VM (e.g. 132.45.67.89)
  git_clone_url    HTTPS clone URL of the trading-agent repo

Optional:
  branch           Git branch / tag to deploy (default: freeze-v2.1)

Optional env:
  SSH_USER         SSH user (default: opc on Oracle Linux, ubuntu on Ubuntu).
                   If unsure, try opc first.
  SSH_KEY          Path to private key (default: \$HOME/.ssh/oci_trader_key).
EOF
    exit 1
fi

BACKTESTER_IP="$1"
GIT_URL="$2"
GIT_BRANCH="${3:-freeze-v2.1}"
SSH_USER="${SSH_USER:-opc}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oci_trader_key}"
TRADER_HOME="/opt/trading-agent"

if [ ! -f "$SSH_KEY" ]; then
    echo "[bootstrap_backtester][FATAL] SSH key not found: $SSH_KEY" >&2
    exit 2
fi

SSH_OPTS=(
    -i "$SSH_KEY"
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=accept-new
    -o BatchMode=yes
)

remote() {
    # Run a single shell snippet on the backtester VM. We deliberately use
    # `bash -lc` so .bashrc-style PATH additions (rvm-style stuff is rare on
    # OCI base images but cheap insurance) are honoured.
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${BACKTESTER_IP}" "bash -lc \"$1\""
}

echo "============================================================"
echo " Backtester VM bootstrap"
echo " Host    : ${SSH_USER}@${BACKTESTER_IP}"
echo " Repo    : ${GIT_URL} (${GIT_BRANCH})"
echo " Target  : ${TRADER_HOME}"
echo " SSH key : ${SSH_KEY}"
echo "============================================================"

echo "[1/6] Ping VM..."
remote "echo ok && uname -srm && cat /etc/os-release | head -3"

echo "[2/6] Install base packages + Docker (idempotent)..."
remote "
    set -euo pipefail
    if [ -f /etc/oracle-release ] || [ -f /etc/redhat-release ]; then
        sudo dnf install -y git python3 python3-pip jq tmux rsync >/dev/null
        if ! command -v docker >/dev/null 2>&1; then
            sudo dnf -y install dnf-utils
            sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
            sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || \
            sudo dnf install -y docker docker-compose-plugin
        fi
    else
        sudo apt-get update -y >/dev/null
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-pip jq tmux rsync ca-certificates curl gnupg >/dev/null
        if ! command -v docker >/dev/null 2>&1; then
            sudo install -m 0755 -d /etc/apt/keyrings
            DIST=\$(. /etc/os-release && echo \$ID)
            curl -fsSL https://download.docker.com/linux/\${DIST}/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/\${DIST} \$(. /etc/os-release && echo \$VERSION_CODENAME) stable\" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
            sudo apt-get update -y >/dev/null
            sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        fi
    fi
    sudo systemctl enable docker >/dev/null 2>&1 || true
    sudo systemctl start docker
    sudo usermod -aG docker \$USER >/dev/null 2>&1 || true
    docker --version
"

echo "[3/6] Allocate 2 GB swap (defensive; harmless on Ampere)..."
remote "
    set -euo pipefail
    if [ ! -f /swapfile ]; then
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile >/dev/null
        sudo swapon /swapfile
        grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
        echo 'swap allocated'
    else
        echo 'swap already exists'
    fi
    free -h | head -3
"

echo "[4/6] Clone or refresh repo at ${TRADER_HOME}..."
remote "
    set -euo pipefail
    sudo mkdir -p ${TRADER_HOME}
    sudo chown \$USER:\$USER ${TRADER_HOME}
    if [ ! -d ${TRADER_HOME}/.git ]; then
        git clone --branch ${GIT_BRANCH} --depth 50 ${GIT_URL} ${TRADER_HOME}
    else
        git -C ${TRADER_HOME} fetch origin
        git -C ${TRADER_HOME} checkout ${GIT_BRANCH}
        git -C ${TRADER_HOME} reset --hard origin/${GIT_BRANCH} || git -C ${TRADER_HOME} reset --hard ${GIT_BRANCH}
    fi
    mkdir -p ${TRADER_HOME}/logs/backtests ${TRADER_HOME}/data
    ls -la ${TRADER_HOME} | head -6
"

echo "[5/6] Build the trading-agent image (this is the long step)..."
remote "
    set -euo pipefail
    cd ${TRADER_HOME}
    # Use 'docker compose build trader' if compose file present; fall
    # back to direct docker build otherwise.
    if [ -f docker-compose.yml ] || [ -f compose.yml ]; then
        sudo docker compose build trader
    else
        sudo docker build -t trading-agent:latest .
    fi
    sudo docker images trading-agent:latest --format '{{.Repository}}:{{.Tag}}  {{.Size}}'
"

echo "[6/6] Smoke-test: battery --help inside the freshly built image..."
remote "
    set -euo pipefail
    sudo docker run --rm \
        -e BACKTESTER_MODE=1 \
        trading-agent:latest \
        python tools/run_battery.py --help | head -20
"

echo "============================================================"
echo " Bootstrap complete. Next steps from your laptop:"
echo ""
echo "   # 1. Set the backtester IP for the launcher + pull scripts:"
echo "   \$env:BACKTESTER_VM_HOST = '${BACKTESTER_IP}'"
echo ""
echo "   # 2. Kick off a battery run:"
echo "   bash tools/cloud/launch_battery.sh --days 90 --workers 2 \\"
echo "       --universe-file tests/fixtures/battery_v2_universe.json \\"
echo "       --run-id battery_freeze_v21_\$(date +%Y%m%dT%H%M%S)"
echo ""
echo "   # 3. Pull results when it's done:"
echo "   .\\tools\\cloud\\pull_battery_results.ps1 -RunId <run_id>"
echo "============================================================"
