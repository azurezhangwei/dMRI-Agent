#!/usr/bin/env bash
set -euo pipefail

# UKFTractography template.
# Run with prepared NHDR + uint8 mask, or pass EDDY_DWI/BVAL/BVEC/MASK_NII and
# let the script prepare them inside the output directory.

usage() {
  cat <<'EOF'
Usage:
  bash ukf_tractography.sh [dwi_nhdr] [mask_nrrd] [output_vtk]

Preferred environment variables:
  DWI_NHDR, MASK_NRRD, OUTPUT_VTK

Optional preparation inputs:
  EDDY_DWI, BVAL, BVEC, MASK_NII

Other optional environment variables:
  UKF_NUM_THREADS, SEEDS_PER_VOXEL, STOPPING_THRESHOLD, STOPPING_FA,
  SEEDING_THRESHOLD, DRY_RUN
EOF
}

export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/opt/Slicer-5.2.2-linux-amd64/lib/Slicer-5.2"
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/opt/Slicer-5.2.2-linux-amd64/NA-MIC/Extensions-31382/UKFTractography/lib/Slicer-5.2/qt-loadable-modules"
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/opt/Slicer-5.2.2-linux-amd64/lib"
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/opt/Slicer-5.2.2-linux-amd64/lib/Teem-1.12.0"

UKFTractography=/opt/Slicer-5.2.2-linux-amd64/NA-MIC/Extensions-31382/UKFTractography/lib/Slicer-5.2/cli-modules/UKFTractography
NHDR_WRITE=/opt/conversion/conversion/nhdr_write.py

DWI_NHDR="${DWI_NHDR:-${1:-}}"
MASK_NRRD="${MASK_NRRD:-${2:-}}"
OUTPUT_VTK="${OUTPUT_VTK:-${3:-}}"
EDDY_DWI="${EDDY_DWI:-}"
BVAL="${BVAL:-}"
BVEC="${BVEC:-}"
MASK_NII="${MASK_NII:-}"
UKF_NUM_THREADS="${UKF_NUM_THREADS:-4}"
SEEDS_PER_VOXEL="${SEEDS_PER_VOXEL:-1}"
STOPPING_THRESHOLD="${STOPPING_THRESHOLD:-0.06}"
STOPPING_FA="${STOPPING_FA:-0.08}"
SEEDING_THRESHOLD="${SEEDING_THRESHOLD:-0.1}"
DRY_RUN="${DRY_RUN:-0}"

run_cmd() {
  echo "[CMD] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

prepare_nhdr() {
  local workdir="$1"
  local dwi_local="$workdir/$(basename "$EDDY_DWI")"
  local bval_local="$workdir/$(basename "$BVAL")"
  local bvec_local="$workdir/$(basename "$BVEC")"
  local nhdr_local="$workdir/$(basename "${EDDY_DWI%.nii.gz}.nhdr")"
  ln -sf "$EDDY_DWI" "$dwi_local"
  ln -sf "$BVAL" "$bval_local"
  ln -sf "$BVEC" "$bvec_local"
  run_cmd python3 "$NHDR_WRITE" --nifti "$dwi_local" --bval "$bval_local" --bvec "$bvec_local" --nhdr "$nhdr_local"
  DWI_NHDR="$nhdr_local"
}

prepare_mask_nrrd() {
  local workdir="$1"
  local mask_local="$workdir/$(basename "${MASK_NII%.nii.gz}_uint8.nrrd")"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[CMD] python3 - <<PY ... create uint8 nrrd: $mask_local"
    MASK_NRRD="$mask_local"
    return 0
  fi
  python3 - "$MASK_NII" "$mask_local" <<'PY'
import sys
import nibabel as nib
import nrrd
import numpy as np

src, dst = sys.argv[1], sys.argv[2]
img = nib.load(src)
data = (img.get_fdata() > 0).astype(np.uint8)
header = {
    "type": "uint8",
    "dimension": 3,
    "sizes": list(data.shape),
    "space directions": img.affine[:3, :3].tolist(),
    "space origin": img.affine[:3, 3].tolist(),
    "space": "right-anterior-superior",
}
nrrd.write(dst, data, header)
print(dst)
PY
  MASK_NRRD="$mask_local"
}

if [[ -z "$OUTPUT_VTK" ]]; then
  usage >&2
  echo "[ERROR] OUTPUT_VTK is required." >&2
  exit 1
fi

WORKDIR="$(dirname "$OUTPUT_VTK")"
mkdir -p "$WORKDIR"
WORKDIR="$(cd "$WORKDIR" && pwd)"

if [[ -z "$DWI_NHDR" ]]; then
  [[ -n "$EDDY_DWI" && -n "$BVAL" && -n "$BVEC" ]] || {
    usage >&2
    echo "[ERROR] Provide DWI_NHDR or EDDY_DWI/BVAL/BVEC." >&2
    exit 1
  }
  prepare_nhdr "$WORKDIR"
fi

if [[ -z "$MASK_NRRD" ]]; then
  [[ -n "$MASK_NII" ]] || {
    usage >&2
    echo "[ERROR] Provide MASK_NRRD or MASK_NII." >&2
    exit 1
  }
  prepare_mask_nrrd "$WORKDIR"
fi

[[ "$DRY_RUN" == "1" || -f "$DWI_NHDR" ]] || { echo "Error: DWI nhdr not found: $DWI_NHDR"; exit 1; }
[[ "$DRY_RUN" == "1" || -f "$MASK_NRRD" ]] || { echo "Error: mask not found: $MASK_NRRD"; exit 1; }

if [[ ! -f "$OUTPUT_VTK" || "$DRY_RUN" == "1" ]]; then
  echo "[INFO] Running UKFTractography..."
  run_cmd "$UKFTractography" \
    --dwiFile "$DWI_NHDR" \
    --maskFile "$MASK_NRRD" \
    --seedsFile "$MASK_NRRD" \
    --numTensor 2 \
    --seedsPerVoxel "$SEEDS_PER_VOXEL" \
    --stoppingThreshold "$STOPPING_THRESHOLD" \
    --stoppingFA "$STOPPING_FA" \
    --seedingThreshold "$SEEDING_THRESHOLD" \
    --tracts "$OUTPUT_VTK" \
    --recordFA \
    --recordTrace \
    --recordTensors \
    --numThreads "$UKF_NUM_THREADS"
  echo "[OK] Finished: $OUTPUT_VTK"
else
  echo "[SKIP] Output exists: $OUTPUT_VTK"
fi
