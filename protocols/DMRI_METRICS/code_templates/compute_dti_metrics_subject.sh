#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  compute_dti_metrics_subject.sh \
    --subject <id> \
    --dwi <dwi.nii.gz> \
    --bval <dwi.bval> \
    --bvec <dwi.bvec> \
    --mask <mask.nii.gz> \
    --out-dir <subject-output-dir>
USAGE
}

SUBJECT=""
DWI=""
BVAL=""
BVEC=""
MASK=""
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject) SUBJECT="$2"; shift 2 ;;
    --dwi) DWI="$2"; shift 2 ;;
    --bval) BVAL="$2"; shift 2 ;;
    --bvec) BVEC="$2"; shift 2 ;;
    --mask) MASK="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -n "$SUBJECT" && -n "$DWI" && -n "$BVAL" && -n "$BVEC" && -n "$MASK" && -n "$OUT_DIR" ]] || {
  usage >&2
  exit 1
}

export FSLDIR="${FSLDIR:-/opt/fsl}"
if [[ -f "$FSLDIR/etc/fslconf/fsl.sh" ]]; then
  # shellcheck disable=SC1090
  source "$FSLDIR/etc/fslconf/fsl.sh"
fi
export PATH="$FSLDIR/bin:$FSLDIR/share/fsl/bin:$PATH"
export FSLOUTPUTTYPE=NIFTI_GZ
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"

command -v dtifit >/dev/null 2>&1 || { echo "Missing dtifit" >&2; exit 127; }
command -v fslmaths >/dev/null 2>&1 || { echo "Missing fslmaths" >&2; exit 127; }

mkdir -p "$OUT_DIR/logs"
PREFIX="$OUT_DIR/${SUBJECT}_dtifit"
LOG="$OUT_DIR/logs/${SUBJECT}_dtifit.log"

dtifit \
  --data="$DWI" \
  --out="$PREFIX" \
  --mask="$MASK" \
  --bvecs="$BVEC" \
  --bvals="$BVAL" \
  --sse \
  >"$LOG" 2>&1

cp -f "${PREFIX}_L1.nii.gz" "${PREFIX}_AD.nii.gz"
fslmaths "${PREFIX}_L2.nii.gz" -add "${PREFIX}_L3.nii.gz" -div 2 "${PREFIX}_RD.nii.gz"

for suffix in FA MD AD RD L1 L2 L3 S0 V1 sse; do
  [[ -s "${PREFIX}_${suffix}.nii.gz" ]] || {
    echo "Missing output: ${PREFIX}_${suffix}.nii.gz" >&2
    exit 2
  }
done
