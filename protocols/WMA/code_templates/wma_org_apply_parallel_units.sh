#!/usr/bin/env bash
set -euo pipefail

# Subject/session-level WMA batch runner.
# The units file must contain one unit_root per line, for example:
#   sub2211001
#   sub2211002
#   sub-105/ses-01
#
# Required environment variables:
#   PROCESSED_ROOT   e.g. /benchmark_runs/task2/SMU_processed
#   UNITS_FILE       one unit_root per line
#
# Optional environment variables:
#   MAX_JOBS         default 8
#   WMA_THREADS      default 4
#   DRY_RUN          default 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCESSED_ROOT="${PROCESSED_ROOT:-}"
UNITS_FILE="${UNITS_FILE:-}"
MAX_JOBS="${MAX_JOBS:-8}"
WMA_THREADS="${WMA_THREADS:-4}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$PROCESSED_ROOT" || -z "$UNITS_FILE" ]]; then
  echo "[ERROR] PROCESSED_ROOT and UNITS_FILE are required." >&2
  exit 1
fi

[[ -f "$UNITS_FILE" ]] || { echo "[ERROR] Missing units file: $UNITS_FILE" >&2; exit 1; }

run_one() {
  local unit_root="$1"
  local unit_id="${unit_root//\//_}"
  local dwi_dir="$PROCESSED_ROOT/$unit_root/dwi"
  local vtk_file="$dwi_dir/tractography/${unit_id}_dwi_tractography.vtk"
  local wma_dir="$dwi_dir/WMA"

  if [[ ! -f "$vtk_file" ]]; then
    vtk_file="$(find "$dwi_dir/tractography" -maxdepth 1 -type f -name '*.vtk' | sort | head -n 1)"
  fi
  [[ -n "$vtk_file" ]] || { echo "[$unit_id] ERROR missing tractography vtk"; return 1; }

  echo "[$unit_id] starting WMA"
  if [[ "$DRY_RUN" == "1" ]]; then
    VTK_FILE="$vtk_file" WMA_DIR="$wma_dir" WMA_THREADS="$WMA_THREADS" DRY_RUN=1 \
      bash "$SCRIPT_DIR/wma_org_apply.sh"
  else
    xvfb-run -a env \
      VTK_FILE="$vtk_file" \
      WMA_DIR="$wma_dir" \
      WMA_THREADS="$WMA_THREADS" \
      DRY_RUN=0 \
      bash "$SCRIPT_DIR/wma_org_apply.sh"
  fi
}

export SCRIPT_DIR PROCESSED_ROOT WMA_THREADS DRY_RUN
export -f run_one

active=0
while IFS= read -r unit_root || [[ -n "$unit_root" ]]; do
  [[ -z "$unit_root" || "$unit_root" =~ ^# ]] && continue
  run_one "$unit_root" &
  ((active += 1))
  if (( active >= MAX_JOBS )); then
    wait -n
    ((active -= 1))
  fi
done < "$UNITS_FILE"

wait
echo "[OK] WMA batch finished."
