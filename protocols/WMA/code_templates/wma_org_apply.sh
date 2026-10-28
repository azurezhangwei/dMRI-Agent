#!/usr/bin/env bash
set -euo pipefail

# WMA + ORG atlas apply template (headless).
# For headless execution, wrap with `xvfb-run -a` unless the current shell
# already has a working X server.

usage() {
  cat <<'EOF'
Usage:
  bash wma_org_apply.sh <vtk_file> [wma_dir]

Environment overrides:
  VTK_FILE, WMA_DIR, ORG_ATLASES, SLICER_PATH, FIBER_TRACT_MEASUREMENTS,
  WMA_THREADS, DRY_RUN
EOF
}

ORG_ATLASES="${ORG_ATLASES:-/opt/ORG}"
SLICER_PATH="${SLICER_PATH:-/opt/Slicer-5.2.2-linux-amd64/Slicer}"
FIBER_TRACT_MEASUREMENTS="${FIBER_TRACT_MEASUREMENTS:-/opt/Slicer-5.2.2-linux-amd64/NA-MIC/Extensions-31382/SlicerDMRI/lib/Slicer-5.2/cli-modules/FiberTractMeasurements}"
VTK_FILE="${VTK_FILE:-${1:-}}"
WMA_DIR="${WMA_DIR:-${2:-./WMA}}"
WMA_THREADS="${WMA_THREADS:-4}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$VTK_FILE" ]]; then
  usage >&2
  exit 1
fi

[[ "$DRY_RUN" == "1" || -f "$VTK_FILE" ]] || { echo "Error: VTK not found: $VTK_FILE"; exit 1; }
mkdir -p "$WMA_DIR"

run_cmd() {
  echo "[CMD] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

run_cmd wm_apply_ORG_atlas_to_subject.sh \
  -i "$VTK_FILE" \
  -o "$WMA_DIR" \
  -a "$ORG_ATLASES" \
  -s "$SLICER_PATH" \
  -d 1 \
  -m "$FIBER_TRACT_MEASUREMENTS" \
  -n "$WMA_THREADS"

echo "[OK] Finished. Output: $WMA_DIR"
