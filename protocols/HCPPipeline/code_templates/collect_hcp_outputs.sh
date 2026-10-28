#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  collect_hcp_outputs.sh \
    --subject <id> \
    [--study-root <dir>] \
    [--output-dir <dir>]

Copies key HCP outputs for one subject into a compact export directory.
This is optional; the main pipeline already writes results into --study-root.
USAGE
}

SUBJECT=""
STUDY_ROOT="/workspace/docker_data/processed/HCPpipeline_Agent_results"
OUTPUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject)
      SUBJECT="$2"; shift 2 ;;
    --study-root)
      STUDY_ROOT="$2"; shift 2 ;;
    --output-dir)
      OUTPUT_DIR="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [[ -z "${SUBJECT}" ]]; then
  usage
  exit 1
fi

if [[ -z "${OUTPUT_DIR}" ]]; then
  OUTPUT_DIR="${STUDY_ROOT}/output/${SUBJECT}"
fi

SRC="${STUDY_ROOT}/${SUBJECT}"
LOG_DIR="${STUDY_ROOT}/logs"
[[ -d "${SRC}" ]] || { echo "Missing subject output directory: ${SRC}" >&2; exit 1; }

mkdir -p "${OUTPUT_DIR}/T1w" "${OUTPUT_DIR}/Diffusion" "${OUTPUT_DIR}/logs"

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -e "${src}" ]]; then
    cp -a "${src}" "${dst}"
  fi
}

copy_if_exists "${SRC}/T1w/T1w_acpc_dc_restore.nii.gz" "${OUTPUT_DIR}/T1w/"
copy_if_exists "${SRC}/T1w/T1w_acpc_dc_restore_brain.nii.gz" "${OUTPUT_DIR}/T1w/"
copy_if_exists "${SRC}/T1w/T2w_acpc_dc_restore.nii.gz" "${OUTPUT_DIR}/T1w/"
copy_if_exists "${SRC}/T1w/brainmask_fs.nii.gz" "${OUTPUT_DIR}/T1w/"
copy_if_exists "${SRC}/T1w/${SUBJECT}/scripts/recon-all.log" "${OUTPUT_DIR}/logs/"

copy_if_exists "${SRC}/Diffusion/eddy/eddy_unwarped_images.nii.gz" "${OUTPUT_DIR}/Diffusion/"
copy_if_exists "${SRC}/Diffusion/data/data.nii.gz" "${OUTPUT_DIR}/Diffusion/"
copy_if_exists "${SRC}/Diffusion/nodif_brain_mask.nii.gz" "${OUTPUT_DIR}/Diffusion/"
copy_if_exists "${SRC}/Diffusion/reg/EPItoT1w.dat" "${OUTPUT_DIR}/Diffusion/"
copy_if_exists "${SRC}/T1w/Diffusion/data.nii.gz" "${OUTPUT_DIR}/Diffusion/"
copy_if_exists "${SRC}/T1w/Diffusion/nodif_brain_mask.nii.gz" "${OUTPUT_DIR}/Diffusion/"

copy_if_exists "${LOG_DIR}/run_${SUBJECT}_full.log" "${OUTPUT_DIR}/logs/"

if [[ -d "${LOG_DIR}/stages/${SUBJECT}" ]]; then
  mkdir -p "${OUTPUT_DIR}/stage_artifacts"
  find "${LOG_DIR}/stages/${SUBJECT}" -maxdepth 1 -type f -name '*_artifacts.tsv' \
    -exec cp -a {} "${OUTPUT_DIR}/stage_artifacts/" \;
fi

echo "Collected HCP outputs into ${OUTPUT_DIR}"
