#!/usr/bin/env bash
set -euo pipefail

# Subject/session-level UKF batch runner.
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
#   MAX_JOBS         default 10
#   UKF_NUM_THREADS  default 4
#   DRY_RUN          default 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCESSED_ROOT="${PROCESSED_ROOT:-}"
UNITS_FILE="${UNITS_FILE:-}"
MAX_JOBS="${MAX_JOBS:-10}"
UKF_NUM_THREADS="${UKF_NUM_THREADS:-4}"
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
  local eddy_dwi="$dwi_dir/eddy/eddy_corrected.nii.gz"
  local bval
  local bvec
  local mask_nii
  local output_vtk="$dwi_dir/tractography/${unit_id}_dwi_tractography.vtk"

  bval="$(find "$dwi_dir" -maxdepth 1 \( -type f -o -type l \) -name '*_dwi.bval' | sort | head -n 1)"
  bvec="$(find "$dwi_dir/eddy" -maxdepth 1 \( -type f -o -type l \) -name 'eddy_corrected*.bvec' | sort | head -n 1)"
  if [[ -z "$bvec" ]]; then
    bvec="$(find "$dwi_dir" -maxdepth 1 \( -type f -o -type l \) -name '*_dwi.bvec' | sort | head -n 1)"
  fi
  mask_nii="$(find "$dwi_dir" -maxdepth 1 \( -type f -o -type l \) -name '*_dwi_mask.nii.gz' | sort | head -n 1)"

  if [[ ! -f "$eddy_dwi" || -z "$bval" || -z "$bvec" || -z "$mask_nii" ]]; then
    echo "[$unit_id] ERROR missing inputs"
    return 1
  fi

  echo "[$unit_id] starting UKF"
  EDDY_DWI="$eddy_dwi" \
  BVAL="$bval" \
  BVEC="$bvec" \
  MASK_NII="$mask_nii" \
  OUTPUT_VTK="$output_vtk" \
  UKF_NUM_THREADS="$UKF_NUM_THREADS" \
  DRY_RUN="$DRY_RUN" \
    bash "$SCRIPT_DIR/ukf_tractography.sh"
}

export SCRIPT_DIR PROCESSED_ROOT UKF_NUM_THREADS DRY_RUN
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
echo "[OK] UKF batch finished."
