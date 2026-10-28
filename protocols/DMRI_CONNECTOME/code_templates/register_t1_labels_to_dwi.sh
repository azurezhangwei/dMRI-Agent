#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'USAGE'
Usage: register_t1_labels_to_dwi.sh \
  --fixed-b0 <mean_b0.nii.gz> \
  --moving-t1 <T1w_or_brain.nii.gz> \
  --moving-labels <aparc+aseg.nii.gz> \
  --out-dir <registration_dir>
USAGE
}

fixed_b0=""
moving_t1=""
moving_labels=""
out_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fixed-b0)
      fixed_b0="$2"
      shift 2
      ;;
    --moving-t1)
      moving_t1="$2"
      shift 2
      ;;
    --moving-labels)
      moving_labels="$2"
      shift 2
      ;;
    --out-dir)
      out_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[error] Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$fixed_b0" || -z "$moving_t1" || -z "$moving_labels" || -z "$out_dir" ]]; then
  echo "[error] --fixed-b0, --moving-t1, --moving-labels, and --out-dir are required" >&2
  usage
  exit 2
fi

mkdir -p "$out_dir"

prefix="$out_dir/T1w_to_dwi_"
t1_in_dwi="$out_dir/T1w_in_dwi.nii.gz"
labels_in_dwi="$out_dir/aparc+aseg_in_dwi.nii.gz"

antsRegistration \
  --dimensionality 3 \
  --float 0 \
  --output ["$prefix","$t1_in_dwi"] \
  --interpolation Linear \
  --winsorize-image-intensities [0.005,0.995] \
  --use-histogram-matching 1 \
  --initial-moving-transform ["$fixed_b0","$moving_t1",1] \
  --transform Rigid[0.1] \
  --metric MI["$fixed_b0","$moving_t1",1,32,Regular,0.25] \
  --convergence [1000x500x250x0,1e-6,10] \
  --shrink-factors 8x4x2x1 \
  --smoothing-sigmas 3x2x1x0vox \
  --transform Affine[0.1] \
  --metric MI["$fixed_b0","$moving_t1",1,32,Regular,0.25] \
  --convergence [1000x500x250x0,1e-6,10] \
  --shrink-factors 8x4x2x1 \
  --smoothing-sigmas 3x2x1x0vox

antsApplyTransforms \
  -d 3 \
  -i "$moving_labels" \
  -r "$fixed_b0" \
  -o "$labels_in_dwi" \
  -n NearestNeighbor \
  -t "${prefix}0GenericAffine.mat"

echo "[OK] T1w registered to diffusion space: $t1_in_dwi"
echo "[OK] Labels projected to diffusion space: $labels_in_dwi"
