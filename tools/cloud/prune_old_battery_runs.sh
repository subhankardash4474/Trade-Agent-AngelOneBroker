#!/usr/bin/env bash
# prune_old_battery_runs.sh
# =========================
# Reclaim disk on the backtester VM by tar+gzipping completed
# battery runs older than N days. Idempotent: re-running is safe;
# already-archived runs are skipped.
#
# Why this exists
# ---------------
# Each `battery_*` run directory under `logs/backtests/` accumulates
# 200KB per worker log + per-variant result JSONs + a market_data.pkl
# cache. After 3-4 runs the directory is in the 500MB-1GB territory
# and the 30G OCI volume gets uncomfortably full. The Friday 2026-05-22
# run ended at 60% disk usage on the dedicated backtester VM, with 4
# more queued runs ahead -- without rotation we would have run out
# of disk before the queue drained.
#
# What gets archived
# ------------------
# A run is eligible for archival when:
#
#   1. Its directory mtime is older than --age-days days (default 7),
#      AND
#   2. The directory is *not* the most recent run (alphabetic sort by
#      run_id timestamp suffix), AND
#   3. The directory is *not* currently the target of a running
#      docker container named battery_<run_id>.
#
# Eligible runs are tar.gz'd into archive/<run_id>.tar.gz inside the
# same logs/backtests directory; the original directory is then
# removed. Compression typically reduces 500MB to ~80MB.
#
# Restoring an archive
# --------------------
# To inspect an archived run:
#
#   cd logs/backtests
#   tar -xzf archive/<run_id>.tar.gz
#   # original directory is restored at logs/backtests/<run_id>
#
# After inspection, delete the extracted directory (the archive
# remains as the canonical copy).
#
# Usage
# -----
#   bash tools/cloud/prune_old_battery_runs.sh                # default: 7 days
#   bash tools/cloud/prune_old_battery_runs.sh --age-days 3   # tighter
#   bash tools/cloud/prune_old_battery_runs.sh --dry-run      # print only
#   bash tools/cloud/prune_old_battery_runs.sh --keep 2       # keep N most recent

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
BACKTESTS_DIR="${REPO_DIR}/logs/backtests"
ARCHIVE_DIR="${BACKTESTS_DIR}/archive"
AGE_DAYS=7
KEEP_RECENT=1
DRY_RUN=0
SUDO_BIN="${SUDO_BIN:-sudo}"

while [ $# -gt 0 ]; do
    case "$1" in
        --age-days)   AGE_DAYS="$2"; shift 2;;
        --age-days=*) AGE_DAYS="${1#--age-days=}"; shift;;
        --keep)       KEEP_RECENT="$2"; shift 2;;
        --keep=*)     KEEP_RECENT="${1#--keep=}"; shift;;
        --dry-run)    DRY_RUN=1; shift;;
        -h|--help)
            sed -n '1,55p' "$0"; exit 0;;
        *)
            echo "ERROR: unknown argument: $1" >&2; exit 2;;
    esac
done

if [ ! -d "${BACKTESTS_DIR}" ]; then
    echo "INFO: ${BACKTESTS_DIR} does not exist; nothing to prune."
    exit 0
fi

mkdir -p "${ARCHIVE_DIR}"

# 1. List all run directories (one per line), most recent first.
RUNS=()
while IFS= read -r d; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    # Skip the archive subdirectory itself.
    [ "$name" = "archive" ] && continue
    # Skip dotfiles.
    case "$name" in .*) continue ;; esac
    RUNS+=("$name")
done < <(find "${BACKTESTS_DIR}" -mindepth 1 -maxdepth 1 -type d | sort -r)

if [ ${#RUNS[@]} -le ${KEEP_RECENT} ]; then
    echo "INFO: ${#RUNS[@]} run(s) found; nothing to prune (keep_recent=${KEEP_RECENT})."
    exit 0
fi

# 2. List currently-running battery containers so we never archive a
#    run whose worker is still writing to it. (Docker names follow
#    the pattern `battery_<run_id>` per launch_battery.sh.)
ACTIVE_RUNS=""
if command -v docker >/dev/null 2>&1; then
    ACTIVE_RUNS="$(${SUDO_BIN} -n docker ps --format '{{.Names}}' 2>/dev/null \
                   | grep '^battery_' \
                   | sed 's/^battery_//' || true)"
fi

CUTOFF_EPOCH=$(date -d "${AGE_DAYS} days ago" +%s)
NOW_EPOCH=$(date +%s)
ARCHIVED_COUNT=0
SKIPPED_COUNT=0
TOTAL_FREED_BYTES=0

echo "Pruning rules:"
echo "  age threshold : >= ${AGE_DAYS} days old"
echo "  keep recent   : skip the ${KEEP_RECENT} most recent run(s)"
echo "  dry-run       : ${DRY_RUN}"
echo "  active runs   : ${ACTIVE_RUNS:-(none)}"
echo "  total runs    : ${#RUNS[@]}"
echo ""

for i in "${!RUNS[@]}"; do
    name="${RUNS[$i]}"
    run_dir="${BACKTESTS_DIR}/${name}"
    archive_path="${ARCHIVE_DIR}/${name}.tar.gz"

    # Rule 1: skip the N most recent (i < KEEP_RECENT means newer).
    if [ "$i" -lt "${KEEP_RECENT}" ]; then
        echo "  KEEP-RECENT  ${name}"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi

    # Rule 2: skip if currently running.
    if [ -n "${ACTIVE_RUNS}" ] && echo "${ACTIVE_RUNS}" | grep -qx "${name}"; then
        echo "  ACTIVE       ${name} (worker still running)"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi

    # Rule 3: skip if already archived AND original was deleted.
    if [ -f "${archive_path}" ]; then
        echo "  ARCHIVED     ${name} -> ${archive_path}"
        # If both archive and original exist, we already archived but
        # didn't delete -- this run aborted mid-prune previously.
        # Re-attempt the cleanup on the original.
        if [ "${DRY_RUN}" -eq 0 ]; then
            ${SUDO_BIN} -n rm -rf "${run_dir}" 2>/dev/null \
                || rm -rf "${run_dir}" 2>/dev/null \
                || echo "    WARN: couldn't remove ${run_dir} (permissions?)"
        fi
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi

    # Rule 4: must be older than threshold.
    mtime=$(stat -c '%Y' "${run_dir}" 2>/dev/null || echo 0)
    if [ "${mtime}" -gt "${CUTOFF_EPOCH}" ]; then
        age_d=$(( (NOW_EPOCH - mtime) / 86400 ))
        echo "  RECENT       ${name} (${age_d}d old, threshold=${AGE_DAYS}d)"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        continue
    fi

    # Eligible -- archive it.
    age_d=$(( (NOW_EPOCH - mtime) / 86400 ))
    size_bytes=$(${SUDO_BIN} -n du -sb "${run_dir}" 2>/dev/null \
                 | awk '{print $1}' || echo 0)
    size_h=$(${SUDO_BIN} -n du -sh "${run_dir}" 2>/dev/null \
             | awk '{print $1}' || echo "?")
    echo "  ARCHIVE      ${name} (${age_d}d old, ${size_h})"

    if [ "${DRY_RUN}" -eq 1 ]; then
        echo "    [DRY-RUN] would tar -czf ${archive_path} ${run_dir} && rm -rf ${run_dir}"
        continue
    fi

    if ${SUDO_BIN} -n tar -czf "${archive_path}" -C "${BACKTESTS_DIR}" "${name}" 2>/dev/null \
        || tar -czf "${archive_path}" -C "${BACKTESTS_DIR}" "${name}" 2>/dev/null; then
        # Only delete original if the archive was actually written.
        if [ -f "${archive_path}" ] && [ -s "${archive_path}" ]; then
            ${SUDO_BIN} -n rm -rf "${run_dir}" 2>/dev/null \
                || rm -rf "${run_dir}" 2>/dev/null \
                || { echo "    WARN: couldn't remove ${run_dir}"; continue; }
            ARCHIVED_COUNT=$((ARCHIVED_COUNT + 1))
            TOTAL_FREED_BYTES=$((TOTAL_FREED_BYTES + size_bytes))
            archive_h=$(${SUDO_BIN} -n du -sh "${archive_path}" 2>/dev/null | awk '{print $1}')
            echo "    OK -> ${archive_path} (${archive_h})"
        else
            echo "    ERROR: tar produced empty/missing archive; original kept"
        fi
    else
        echo "    ERROR: tar failed; original kept"
    fi
done

echo ""
echo "Summary:"
echo "  archived: ${ARCHIVED_COUNT}"
echo "  skipped : ${SKIPPED_COUNT}"
if [ "${TOTAL_FREED_BYTES}" -gt 0 ]; then
    freed_h=$(numfmt --to=iec "${TOTAL_FREED_BYTES}" 2>/dev/null || echo "${TOTAL_FREED_BYTES} bytes")
    echo "  freed   : ~${freed_h}"
fi

# Show post-prune disk state.
echo ""
echo "Post-prune disk:"
df -h "${REPO_DIR}" 2>/dev/null | tail -1
