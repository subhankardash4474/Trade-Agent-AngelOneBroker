#!/usr/bin/env bash
# =============================================================================
# OCI Trading Agent -- one-shot VM bootstrap
# =============================================================================
# Idempotent installer for a fresh Oracle Linux 9 / Ubuntu 22.04 / Amazon
# Linux 2023 instance. Run ONCE as the default user (opc / ubuntu / ec2-user)
# right after first SSH login.
#
# What it does:
#   1.  Detects distro and uses the right package manager
#   2.  Installs Docker engine + compose plugin + git + python3 + utilities
#   3.  Creates a non-login `trader` user owning /opt/trading-agent
#   4.  Clones the repo from GitHub (read-only HTTPS) into that user's home
#   5.  Opens firewall for nothing inbound except SSH (everything outbound)
#   6.  Enables docker so it auto-starts on reboot
#   7.  Prints next-step instructions for the operator
#
# It does NOT:
#   - Push secrets (you'll scp .env in manually -- chmod 600 -- after this)
#   - Push the trained XGBoost model (you'll scp models/xgboost_model.pkl in)
#   - Start the trading container (you do `docker compose up -d` after .env
#     is in place)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<user>/<repo>/main/tools/cloud/oci_bootstrap.sh -o /tmp/bootstrap.sh
#   chmod +x /tmp/bootstrap.sh
#   /tmp/bootstrap.sh https://github.com/<user>/<repo>.git
#
# Required env / args:
#   $1 = git clone URL (HTTPS), e.g. https://github.com/you/trading-agent.git
#
# Optional env:
#   TRADER_HOME  default /opt/trading-agent
#   GIT_BRANCH   default main
# =============================================================================
set -euo pipefail

# ---- args ------------------------------------------------------------------
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <git_clone_url> [branch]"
    echo "Example: $0 https://github.com/you/trading-agent.git main"
    exit 1
fi
GIT_URL="$1"
GIT_BRANCH="${2:-main}"
TRADER_HOME="${TRADER_HOME:-/opt/trading-agent}"
LOG_FILE="/tmp/oci_bootstrap_$(date +%Y%m%dT%H%M%S).log"

# ---- logging helpers -------------------------------------------------------
log()  { printf "[bootstrap] %s\n" "$*" | tee -a "$LOG_FILE"; }
fail() { printf "[bootstrap][FATAL] %s\n" "$*" | tee -a "$LOG_FILE" >&2; exit 1; }

log "============================================================"
log " OCI Trading Agent bootstrap"
log " Started:  $(date -Iseconds)"
log " Host:     $(hostname)"
log " Log file: $LOG_FILE"
log " Repo:     $GIT_URL ($GIT_BRANCH)"
log " Target:   $TRADER_HOME"
log "============================================================"

# ---- 1. Detect distro / package manager ------------------------------------
if   [ -f /etc/oracle-release ]; then DISTRO=oracle
elif [ -f /etc/almalinux-release ]; then DISTRO=alma
elif [ -f /etc/rocky-release ]; then DISTRO=rocky
elif [ -f /etc/redhat-release ]; then DISTRO=rhel
elif grep -qi ubuntu /etc/os-release 2>/dev/null; then DISTRO=ubuntu
elif grep -qi debian /etc/os-release 2>/dev/null; then DISTRO=debian
elif grep -qi 'amazon linux' /etc/os-release 2>/dev/null; then DISTRO=amzn
else fail "Unsupported distro. Expected Oracle Linux / Ubuntu / Debian / Amazon Linux."
fi
log "Detected distro: $DISTRO"

if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
    PKG_MGR="apt-get"
    DEFAULT_USER="ubuntu"
else
    PKG_MGR="dnf"
    DEFAULT_USER="opc"  # OCI / Oracle Linux default; AL2023 = ec2-user (handled below)
fi
[ "$DISTRO" = "amzn" ] && DEFAULT_USER="ec2-user"

# ---- 2. System packages ----------------------------------------------------
log "[2/7] Installing base packages..."
if [ "$PKG_MGR" = "apt-get" ]; then
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl gnupg lsb-release git python3 python3-pip \
        ufw htop vim tmux jq cron sqlite3 rsync
else
    sudo dnf install -y \
        ca-certificates curl git python3 python3-pip \
        firewalld htop vim tmux jq cronie sqlite rsync
fi

# ---- 3. Docker engine + compose plugin -------------------------------------
log "[3/7] Installing Docker engine + compose plugin..."
if ! command -v docker >/dev/null 2>&1; then
    if [ "$PKG_MGR" = "apt-get" ]; then
        sudo install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/${DISTRO}/gpg | \
            sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
              https://download.docker.com/linux/${DISTRO} \
              $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
            sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
        sudo apt-get update -y
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                                 docker-buildx-plugin docker-compose-plugin
    else
        # Oracle Linux / Amazon Linux 2023 / RHEL family
        if [ "$DISTRO" = "oracle" ]; then
            sudo dnf -y install dnf-utils
            sudo dnf config-manager --add-repo \
                https://download.docker.com/linux/centos/docker-ce.repo
        elif [ "$DISTRO" = "amzn" ]; then
            # AL2023 ships docker directly
            true
        else
            sudo dnf -y install dnf-utils
            sudo dnf config-manager --add-repo \
                https://download.docker.com/linux/centos/docker-ce.repo
        fi
        sudo dnf install -y docker-ce docker-ce-cli containerd.io \
                            docker-buildx-plugin docker-compose-plugin \
            || sudo dnf install -y docker docker-compose-plugin
    fi
else
    log "    docker already installed -- skipping"
fi

sudo systemctl enable docker
sudo systemctl start  docker

# ---- 4. Create `trader` user ------------------------------------------------
log "[4/7] Creating 'trader' service user..."
if ! id trader >/dev/null 2>&1; then
    sudo useradd --system --create-home --shell /bin/bash \
                 --home-dir /home/trader trader
    sudo usermod -aG docker trader
    log "    user 'trader' created and added to docker group"
else
    log "    user 'trader' already exists -- skipping"
fi

# ---- 5. Clone repo into TRADER_HOME ----------------------------------------
log "[5/7] Cloning $GIT_URL into $TRADER_HOME ..."
sudo mkdir -p "$TRADER_HOME"
sudo chown trader:trader "$TRADER_HOME"
if [ ! -d "$TRADER_HOME/.git" ]; then
    sudo -u trader git clone --branch "$GIT_BRANCH" --depth 1 \
                              "$GIT_URL" "$TRADER_HOME"
else
    log "    repo already cloned -- pulling latest"
    sudo -u trader git -C "$TRADER_HOME" fetch origin
    sudo -u trader git -C "$TRADER_HOME" reset --hard "origin/$GIT_BRANCH"
fi

# Make sure runtime dirs exist and are writable by the in-container UID
# (trader UID inside container = 1001 by Dockerfile design).
sudo mkdir -p "$TRADER_HOME/data" "$TRADER_HOME/logs" "$TRADER_HOME/models"
sudo chown -R 1001:1001 "$TRADER_HOME/data" "$TRADER_HOME/logs" "$TRADER_HOME/models"
sudo chmod 0775 "$TRADER_HOME/data" "$TRADER_HOME/logs" "$TRADER_HOME/models"

# ---- 6. Firewall: deny all inbound except SSH ------------------------------
log "[6/7] Configuring firewall (default-deny inbound except SSH)..."
if [ "$PKG_MGR" = "apt-get" ]; then
    sudo ufw --force reset
    sudo ufw default deny incoming
    sudo ufw default allow outgoing
    sudo ufw allow 22/tcp
    sudo ufw --force enable
    sudo ufw status verbose
else
    sudo systemctl enable firewalld
    sudo systemctl start  firewalld
    sudo firewall-cmd --permanent --add-service=ssh
    sudo firewall-cmd --reload
fi

# ---- 7. Final summary ------------------------------------------------------
PUBLIC_IP="$(curl -fsSL https://checkip.amazonaws.com 2>/dev/null || echo unknown)"
log "[7/7] Bootstrap complete!"
log ""
log "  Public IP (whitelist this in AngelOne SmartAPI portal):"
log "      $PUBLIC_IP"
log ""
log "  Next steps (as the operator, on this VM):"
log "    1. Place secrets:"
log "         sudo -u trader nano $TRADER_HOME/.env"
log "         sudo chmod 600 $TRADER_HOME/.env"
log "         sudo chown trader:trader $TRADER_HOME/.env"
log "       (Use .env.production.example as a template.)"
log ""
log "    2. Place the trained XGBoost model:"
log "         scp models/xgboost_model.pkl  <user>@$PUBLIC_IP:/tmp/"
log "         sudo install -o 1001 -g 1001 -m 0644 /tmp/xgboost_model.pkl \\"
log "                $TRADER_HOME/models/xgboost_model.pkl"
log ""
log "    3. Build & start the container (as trader):"
log "         sudo -u trader -i"
log "         cd $TRADER_HOME"
log "         docker compose build"
log "         docker compose up -d"
log "         docker compose logs -f trader"
log ""
log "    4. Verify health:"
log "         python3 $TRADER_HOME/tools/health_check.py"
log ""
log "  Full log: $LOG_FILE"
