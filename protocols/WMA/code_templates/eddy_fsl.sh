#!/usr/bin/env bash
set -euo pipefail

# FSL eddy template
# Hard rules:
# - acqp/index must be derived from real JSON/meta; no guessing.
# - if missing required fields, abort/skip with explicit reason.

export FSLDIR=/opt/fsl
# shellcheck disable=SC1091
source "$FSLDIR/etc/fslconf/fsl.sh"

usage() {
  cat <<'EOF'
Usage:
  bash eddy_fsl.sh <dwi_nii> <bval> <bvec> <json> <mask_nii> [outdir]

Environment overrides:
  DWI, BVAL, BVEC, JSON, MASK, OUTDIR, EDDY_CMD, DRY_RUN
EOF
}

DWI="${DWI:-${1:-}}"
BVAL="${BVAL:-${2:-}}"
BVEC="${BVEC:-${3:-}}"
JSON="${JSON:-${4:-}}"
MASK="${MASK:-${5:-}}"
OUTDIR="${OUTDIR:-${6:-eddy}}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$DWI" || -z "$BVAL" || -z "$BVEC" || -z "$JSON" || -z "$MASK" ]]; then
  usage >&2
  exit 1
fi

mkdir -p "$OUTDIR"

for f in "$DWI" "$BVAL" "$BVEC" "$JSON" "$MASK"; do
  [[ -f "$f" ]] || { echo "[ERROR] Missing file: $f"; exit 1; }
done

run_cmd() {
  echo "[CMD] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

if [[ -z "${EDDY_CMD:-}" ]]; then
  if command -v eddy_cuda11.0 >/dev/null 2>&1; then
    EDDY_CMD="eddy_cuda11.0"
  elif command -v eddy >/dev/null 2>&1; then
    EDDY_CMD="eddy"
  else
    echo "[ERROR] No eddy found (eddy_cuda11.0 / eddy)."
    exit 1
  fi
fi

echo "[INFO] Using: $EDDY_CMD"

run_cmd python3 "$(dirname "$0")/make_acqp_from_bids_json.py" --json "$JSON" --out "$OUTDIR/acqp.txt"

NVOL=$(fslnvols "$DWI")
run_cmd python3 "$(dirname "$0")/make_index_txt.py" --nvol "$NVOL" --row 1 --out "$OUTDIR/index.txt"

if [[ "$EDDY_CMD" == "eddy" ]]; then
  if [[ -z "${OMP_NUM_THREADS:-}" ]]; then
    TOTAL_CORES=$(nproc)
    OMP_NUM_THREADS=$(( TOTAL_CORES * 80 / 100 ))
    [[ "$OMP_NUM_THREADS" -lt 1 ]] && OMP_NUM_THREADS=1
    export OMP_NUM_THREADS
  fi
  echo "[INFO] OMP_NUM_THREADS=$OMP_NUM_THREADS"
fi

run_cmd "$EDDY_CMD" \
  --imain="$DWI" \
  --mask="$MASK" \
  --acqp="$OUTDIR/acqp.txt" \
  --index="$OUTDIR/index.txt" \
  --bvecs="$BVEC" \
  --bvals="$BVAL" \
  --out="$OUTDIR/eddy_corrected"

echo "[OK] eddy finished: $OUTDIR"
