#!/usr/bin/env bash
# =============================================================================
# XGBoost retrain runner — backtester VM
# =============================================================================
# Phase 2 + Phase 3 of the §5.9 retrain runbook (`docs/findings_log_2026-05-27.md`).
# Designed to run on the backtester VM after slot-3 of the trimmed queue
# finishes (V19 ETA ~17:00 IST tonight 2026-05-29).
#
# Sequence:
#   1. Sanity: no battery_* container is currently running. Refuse if there
#      is one — we'd be racing the workers for the model file.
#   2. Stop battery-scheduler.service so it doesn't pick up new jobs from
#      the queue mid-retrain.
#   3. Build symbols-file from data/v2_universe_232.txt (same 232-stock
#      universe the slot-#3 v2_holdout_30d battery used).
#   4. docker run prepare_dataset.py (~10 min on Ampere 2 OCPU; downloads
#      232 × 60d × 5m bars from yfinance + features + labels + P1 #7 split).
#   5. docker run train_xgboost.py (~1-2 min; isotonic-calibrated booster
#      with F-22 chronological-tail validation).
#   6. Capture key metrics (AUC, Brier, label balance, BUY/SELL prediction
#      distribution) from training log.
#   7. Backup the existing models/xgboost_model.pkl → pre_retrain copy with
#      a timestamp.
#   8. Atomically replace models/xgboost_model.pkl with the new one.
#   9. Print a GO/NO-GO summary the operator can act on.
#
# This script DOES NOT:
#   - Restart the battery scheduler (operator decision after reviewing GO/NO-GO).
#   - Queue any new battery jobs (queue edits land in `data/battery_queue.yaml`
#     via git, then a `sudo systemctl restart battery-scheduler.service`).
#   - Touch the trader VM. xgboost_classifier is already disabled in
#     `strategies.active` live; the trader's pkl is unused until the next
#     freeze-bypass slot is consumed.
#
# Hard-stop conditions (printed to stderr, non-zero exit):
#   * AUC < 0.55                        — model couldn't find enough signal
#   * BUY/SELL prediction split > 85/15 — re-introduced the broken-pkl bias
#   * Label balance > 85/15 in dataset  — yfinance returned a degenerate
#                                         window (e.g. one-sided drift)
#
# When the hard-stop fires, the existing pkl is NOT replaced. The freshly
# trained pkl is left at /opt/trading-agent/models/xgboost_model_retrain_*.pkl
# for forensic inspection but the live symlink stays on the old (broken) one.
#
# Usage:
#   sudo bash tools/cloud/run_retrain_on_backtester.sh
#
# Estimated wall-clock: ~12-15 min total (10 min prepare + 2 min train +
# overhead).
# =============================================================================
set -euo pipefail

REPO=/opt/trading-agent
TS=$(date -u +%Y%m%dT%H%MZ)
# Log to /home/opc/retrain_logs not $REPO/logs because daemon-user owns
# the latter under Bug J's three-way ownership split — see findings_log
# §1.5). HARDCODED to /home/opc (not $HOME) because this script may be
# invoked via `sudo bash ...` from the wait wrapper, in which case $HOME
# would become /root and the logs would land in the wrong tree. /home/opc
# is the operator's natural look-up location and is opc-owned.
OPC_HOME=/home/opc
LOG_DIR="$OPC_HOME/retrain_logs"
mkdir -p "$LOG_DIR"
chown opc:opc "$LOG_DIR" 2>/dev/null || true
RUN_LOG="$LOG_DIR/retrain_${TS}.log"
PKL_OUT="$REPO/models/xgboost_model_retrain_${TS}.pkl"
PKL_LIVE="$REPO/models/xgboost_model.pkl"

exec > >(tee -a "$RUN_LOG") 2>&1

banner() { echo; echo "============================================================"; echo " $1"; echo "============================================================"; echo; }

banner "XGBoost retrain — $TS"
echo "Repo : $REPO"
echo "Log  : $RUN_LOG"
echo "Out  : $PKL_OUT"

# ── Step 1: refuse if any battery_* container is running ─────────────────
banner "Step 1: pre-flight container check"
RUNNING=$(sudo docker ps --format '{{.Names}}' | grep -E '^battery_' || true)
if [ -n "$RUNNING" ]; then
    echo "[FATAL] battery container(s) still running:" >&2
    echo "$RUNNING" >&2
    echo "" >&2
    echo "Wait for slot-3 (V19) to finish before retraining." >&2
    echo "Status: tools/battery_status_remote.ps1 from your laptop." >&2
    exit 10
fi
echo "OK: no battery_* containers running."

# ── Step 2: stop the scheduler so it doesn't queue new jobs mid-retrain ──
banner "Step 2: stop battery-scheduler.service"
if sudo systemctl is-active --quiet battery-scheduler.service; then
    echo "Scheduler active — stopping..."
    sudo systemctl stop battery-scheduler.service
    echo "Scheduler stopped."
else
    echo "Scheduler already inactive."
fi

# ── Step 3: ensure symbols file is present (must be in repo from a fresh pull) ─
banner "Step 3: verify v2 universe symbols file"
SYMS_FILE="$REPO/data/v2_universe_232.txt"
if [ ! -f "$SYMS_FILE" ]; then
    echo "[FATAL] symbols file not found: $SYMS_FILE" >&2
    echo "Pull the latest main from origin first:" >&2
    echo "  cd $REPO && sudo -u opc git pull origin main" >&2
    exit 11
fi
N=$(wc -l < "$SYMS_FILE")
echo "Symbols file: $SYMS_FILE ($N symbols)"
if [ "$N" -lt 200 ] || [ "$N" -gt 250 ]; then
    echo "[FATAL] expected 230±10 symbols, got $N" >&2
    exit 12
fi

# ── Step 4: prepare_dataset on full 232-stock universe ───────────────────
banner "Step 4: prepare_dataset.py (60d × 5m × 232 stocks)"

# The trading-agent:latest image has a USER directive baked in
# (uid=1001 trader, see `sudo docker run --rm trading-agent:latest id`).
# /opt/trading-agent/data is owned by opc:opc with mode 0775 → trader has
# read+execute but NOT write. The first iteration of this script tried to
# do `--output data/retrain_<TS>` which failed at os.makedirs with
# PermissionError (2026-05-29 12:05 UTC, see /home/opc/retrain_logs/
# retrain_20260529T1205Z.log). Pre-create the output dir with chown
# 1001:1001 so the container (uid 1001) can write into it. Keeps the
# `data/retrain_<TS>/` path stable and is defensive against future
# ownership drift in /opt/trading-agent/data/.
DATASET_DIR="$REPO/data/retrain_${TS}"
sudo mkdir -p "$DATASET_DIR"
sudo chown 1001:1001 "$DATASET_DIR"

sudo docker run --rm \
    -v "$REPO":/app \
    -v "$REPO/data":/app/data \
    -w /app \
    -e PYTHONPATH=/app \
    trading-agent:latest \
    python -m packages.training.prepare_dataset \
        --symbols-file data/v2_universe_232.txt \
        --interval 5m \
        --period 60d \
        --horizon 3 \
        --threshold-pct 0.3 \
        --output data/retrain_${TS}

TRAIN_CSV="$DATASET_DIR/train_dataset.csv"
TEST_CSV="$DATASET_DIR/test_dataset.csv"
if [ ! -f "$TRAIN_CSV" ] || [ ! -f "$TEST_CSV" ]; then
    echo "[FATAL] prepare_dataset did not produce train/test CSVs" >&2
    exit 13
fi
# Docker writes output as uid 1001 (trader) inside the container, which
# lands as 1001:1001-owned files on the host bind-mount. The python
# sub-shells below run with whatever uid invoked this script (root if
# under sudo, opc if direct); both can read 0644 trader-owned files
# fine, but chown back to opc anyway so the operator can `rm -rf` the
# dataset_dir without sudo when cleaning up.
sudo chown -R opc:opc "$DATASET_DIR"
TRAIN_ROWS=$(($(wc -l < "$TRAIN_CSV") - 1))
TEST_ROWS=$(($(wc -l < "$TEST_CSV") - 1))
echo "Train: $TRAIN_ROWS rows  | Test: $TEST_ROWS rows"

# Hard-stop A: label balance must be sane.
# We can't use the host python here — Oracle Linux 8 ships /usr/bin/python3
# without pandas. The trading-agent:latest image has the full ML stack
# baked in, so we route the sanity checks through a thin docker run too.
# The previous version of this script (commit 6385ee6) tried the host
# python and crashed at `ModuleNotFoundError: No module named 'pandas'`
# (see /home/opc/retrain_logs/retrain_20260529T1217Z.log) — which the
# fail-closed `LABEL_RC != 0` branch correctly interpreted as "abort,
# don't replace pkl", but it surfaced as a misleading "label balance >
# 85/15" message. Fixed now.
docker_py() {
    sudo docker run --rm \
        -v "$REPO":/app \
        -v "$REPO/data":/app/data \
        -v "$REPO/models":/app/models \
        -w /app \
        -e PYTHONPATH=/app \
        trading-agent:latest \
        python "$@"
}

LABEL_BALANCE=$(docker_py -c "
import pandas as pd
import sys
df = pd.read_csv('data/retrain_${TS}/train_dataset.csv')
total = len(df)
up = int((df['label'] == 1).sum())
down = int((df['label'] == 0).sum())
up_pct = 100.0 * up / total if total else 0
down_pct = 100.0 * down / total if total else 0
sys.stdout.write(f'{up_pct:.1f}/{down_pct:.1f}\n')
if max(up_pct, down_pct) > 85.0:
    sys.exit(1)
")
LABEL_RC=$?
echo "Train label balance UP/DOWN: $LABEL_BALANCE"
if [ $LABEL_RC -ne 0 ]; then
    echo "[FATAL] label balance is >85/15 one-sided — yfinance window is degenerate;" >&2
    echo "        the broken-pkl failure mode (95% one-sided) is NOT acceptable." >&2
    echo "        Existing pkl NOT replaced." >&2
    exit 20
fi

# ── Step 5: train_xgboost ────────────────────────────────────────────────
banner "Step 5: train_xgboost.py (isotonic, calibrated)"
sudo docker run --rm \
    -v "$REPO":/app \
    -v "$REPO/data":/app/data \
    -v "$REPO/models":/app/models \
    -w /app \
    -e PYTHONPATH=/app \
    trading-agent:latest \
    python -m packages.training.train_xgboost \
        --train "data/retrain_${TS}/train_dataset.csv" \
        --test  "data/retrain_${TS}/test_dataset.csv" \
        --output "models/xgboost_model_retrain_${TS}.pkl" \
        --calibration-method isotonic

if [ ! -f "$PKL_OUT" ]; then
    echo "[FATAL] train_xgboost did not produce $PKL_OUT" >&2
    exit 14
fi
# Same chown rationale as for the dataset CSVs: docker wrote the pkl as
# uid 1001 (trader). Step 7's python sub-shell can read it fine; we just
# normalise ownership to opc so cleanup is friction-free.
sudo chown opc:opc "$PKL_OUT"
echo "Model written: $PKL_OUT"

# ── Step 6: extract metrics from the run log (parsed from training output) ─
banner "Step 6: parse training metrics for GO/NO-GO"
# We log everything to RUN_LOG above, so grep the latest run's metrics.
AUC=$(grep -oE 'AUC:[[:space:]]+[0-9.]+' "$RUN_LOG" | tail -1 | awk '{print $2}')
BRIER=$(grep -oE 'Brier:[[:space:]]+[0-9.]+' "$RUN_LOG" | tail -1 | awk '{print $2}')
echo "AUC   : ${AUC:-<missing>}"
echo "Brier : ${BRIER:-<missing>}"

# ── Step 7: BUY/SELL prediction distribution check on test set ───────────
banner "Step 7: prediction distribution check"
PRED_BALANCE=$(docker_py -c "
import pandas as pd
import pickle
import sys
with open('models/xgboost_model_retrain_${TS}.pkl', 'rb') as f:
    model = pickle.load(f)
df = pd.read_csv('data/retrain_${TS}/test_dataset.csv', index_col=0)
feature_cols = [c for c in df.columns if c not in ('label', 'symbol')]
X = df[feature_cols].fillna(0)
proba = model.predict_proba(X)[:, 1]
buy_pct = 100.0 * (proba >= 0.5).mean()
sell_pct = 100.0 - buy_pct
sys.stdout.write(f'BUY={buy_pct:.1f}% SELL={sell_pct:.1f}%\n')
if max(buy_pct, sell_pct) > 85.0:
    sys.exit(1)
")
PRED_RC=$?
echo "Prediction distribution on test set: $PRED_BALANCE"
if [ $PRED_RC -ne 0 ]; then
    echo "[FATAL] prediction distribution is >85/15 one-sided — same failure" >&2
    echo "        mode as the broken 2026-05-14 pkl. Existing pkl NOT replaced." >&2
    exit 21
fi

# ── Step 8: AUC hard-stop ────────────────────────────────────────────────
# Bash float-compare via awk (no docker round-trip needed for a 1-line
# arithmetic check; the host has awk).
if [ -n "${AUC:-}" ]; then
    AUC_OK=$(awk -v a="$AUC" 'BEGIN{print (a + 0 >= 0.55) ? "OK" : "FAIL"}')
    if [ "$AUC_OK" != "OK" ]; then
        echo "[FATAL] AUC=$AUC < 0.55 — model didn't find enough signal." >&2
        echo "        Existing pkl NOT replaced; new pkl preserved at $PKL_OUT" >&2
        echo "        for forensic inspection." >&2
        exit 22
    fi
fi

# ── Step 9: backup + atomic swap ─────────────────────────────────────────
banner "Step 9: backup + atomic swap"
if [ -f "$PKL_LIVE" ]; then
    BACKUP="$REPO/models/xgboost_model_pre_retrain_${TS}.pkl"
    sudo cp -p "$PKL_LIVE" "$BACKUP"
    echo "Backed up old pkl: $BACKUP"
fi
# Atomic replace via mv (same filesystem).
sudo cp -p "$PKL_OUT" "$PKL_LIVE.tmp"
sudo mv "$PKL_LIVE.tmp" "$PKL_LIVE"
echo "Replaced: $PKL_LIVE"
sudo ls -la "$REPO/models/xgboost_model"*.pkl

banner "GO -- retrain complete"
echo "Run timestamp : $TS"
echo "Train rows    : $TRAIN_ROWS"
echo "Test rows     : $TEST_ROWS"
echo "Label balance : $LABEL_BALANCE"
echo "Prediction    : $PRED_BALANCE"
echo "AUC           : ${AUC:-<missing>}"
echo "Brier         : ${BRIER:-<missing>}"
echo
echo "Next steps (operator decision):"
echo "  1. Review $RUN_LOG for top features + per-class precision/recall."
echo "  2. If satisfactory, queue post-retrain battery:"
echo "     - Edit /opt/trading-agent/data/battery_queue.yaml on this VM,"
echo "       OR commit on laptop + push + git pull on this VM."
echo "  3. sudo systemctl start battery-scheduler.service"
echo
echo "Deploy to trader VM:"
echo "  NOT done by this script. xgboost_classifier is currently disabled"
echo "  in strategies.active live; the trader pkl is unused until the next"
echo "  freeze-bypass slot is consumed (gated on next battery V15 verdict)."
