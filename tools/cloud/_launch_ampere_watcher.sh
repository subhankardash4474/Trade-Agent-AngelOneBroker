#!/usr/bin/env bash
# Small wrapper that establishes PATH + cwd before invoking the watcher,
# so it works under non-login subshells (tmux, nohup, systemd).
# Lives in tools/cloud/ so it ships with the repo and can be scp'd onto
# the watcher host without recreating it inline.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$HOME"
exec /bin/bash /opt/trading-agent/tools/cloud/ampere_capacity_watcher.sh --interval 20 --max-hours 60 >> "$HOME/ampere_watcher.log" 2>&1
