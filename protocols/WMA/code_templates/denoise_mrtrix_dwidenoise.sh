#!/usr/bin/env bash
set -euo pipefail

# MRtrix MP-PCA denoise template.
# Prefer passing inputs explicitly, for example:
#   bash denoise_mrtrix_dwidenoise.sh \
#     /path/to/sub_dwi.nii.gz \
#     /path/to/sub_dwi.bval \
#     /path/to/sub_dwi.bvec \
#     /path/to/sub_dwi_mask.nii.gz \
#     /path/to/denoise

usage() {
  cat <<'EOF'
Usage:
  bash denoise_mrtrix_dwidenoise.sh <dwi_nii> <bval> <bvec> <mask_nii> [outdir]

Environment overrides:
  DWI_NII, BVAL, BVEC, MASK, OUTDIR, DRY_RUN
EOF
}

DWI_NII="${DWI_NII:-${1:-}}"
BVAL="${BVAL:-${2:-}}"
BVEC="${BVEC:-${3:-}}"
MASK="${MASK:-${4:-}}"
OUTDIR="${OUTDIR:-${5:-denoise}}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$DWI_NII" || -z "$BVAL" || -z "$BVEC" || -z "$MASK" ]]; then
  usage >&2
  exit 1
fi

mkdir -p "${OUTDIR}"

for f in "${DWI_NII}" "${BVAL}" "${BVEC}" "${MASK}"; do
  [[ -f "$f" ]] || { echo "[ERROR] Missing file: $f"; exit 1; }
done

run_cmd() {
  echo "[CMD] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

echo "[INFO] Starting dwidenoise (MP-PCA)..."

run_cmd mrconvert "${DWI_NII}" \
  -fslgrad "${BVEC}" "${BVAL}" \
  "${OUTDIR}/dwi.mif" \
  -force

run_cmd dwidenoise "${OUTDIR}/dwi.mif" \
  "${OUTDIR}/dwi_den.mif" \
  -noise "${OUTDIR}/dwi_noise.mif" \
  -mask "${MASK}" \
  -force

run_cmd mrcalc "${OUTDIR}/dwi.mif" "${OUTDIR}/dwi_den.mif" \
  -subtract "${OUTDIR}/dwi_den_residual.mif" \
  -force

run_cmd mrconvert "${OUTDIR}/dwi_den.mif" \
  "${OUTDIR}/dwi_den.nii.gz" \
  -export_grad_fsl "${OUTDIR}/dwi_den.bvec" "${OUTDIR}/dwi_den.bval" \
  -force

echo "[OK] Denoising finished. Outputs in: ${OUTDIR}"
