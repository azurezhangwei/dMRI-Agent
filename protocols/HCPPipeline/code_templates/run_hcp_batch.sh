#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_hcp_batch.sh \
    [--subjects-file <path>] \
    [--max-jobs <n>] \
    [--data-root <dir>] \
    [--study-root <dir>] \
    [--hcppipedir <dir>] \
    [--python-bin <path>] \
    [--gpu true|false] \
    [--force-rerun 0|1] \
    [subject1 subject2 ...]

Runs multiple HCP subjects in parallel inside the container by launching
run_hcp_subject.sh in the background.
USAGE
}

SUBJECTS_FILE=""
MAX_JOBS=10
DATA_ROOT="/workspace/docker_data/data"
STUDY_ROOT="/workspace/docker_data/processed/HCPpipeline_Agent_results"
HCPPIPEDIR="/workspace/HCPpipelines"
PYTHON_BIN="/opt/conda/envs/wma/bin/python3.9"
USE_GPU="true"
FORCE_RERUN=0
SUBJECTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subjects-file)
      SUBJECTS_FILE="$2"; shift 2 ;;
    --max-jobs)
      MAX_JOBS="$2"; shift 2 ;;
    --data-root)
      DATA_ROOT="$2"; shift 2 ;;
    --study-root)
      STUDY_ROOT="$2"; shift 2 ;;
    --hcppipedir)
      HCPPIPEDIR="$2"; shift 2 ;;
    --python-bin)
      PYTHON_BIN="$2"; shift 2 ;;
    --gpu)
      USE_GPU="$2"; shift 2 ;;
    --force-rerun)
      FORCE_RERUN="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      SUBJECTS+=( "$1" )
      shift ;;
  esac
done

if [[ -n "${SUBJECTS_FILE}" ]]; then
  [[ -f "${SUBJECTS_FILE}" ]] || { echo "Missing subjects file: ${SUBJECTS_FILE}" >&2; exit 1; }
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    SUBJECTS+=( "${line}" )
  done < "${SUBJECTS_FILE}"
fi

if [[ "${#SUBJECTS[@]}" -eq 0 ]]; then
  usage
  exit 1
fi

if [[ ! "${MAX_JOBS}" =~ ^[0-9]+$ ]] || [[ "${MAX_JOBS}" -lt 1 ]]; then
  echo "Error: --max-jobs must be a positive integer" >&2
  exit 1
fi

if [[ ! "${FORCE_RERUN}" =~ ^[01]$ ]]; then
  echo "Error: --force-rerun must be 0 or 1" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="${SCRIPT_DIR}/run_hcp_subject.sh"
[[ -x "${RUNNER}" ]] || { echo "Runner not executable: ${RUNNER}" >&2; exit 1; }

LOG_DIR="${STUDY_ROOT}/logs"
mkdir -p "${LOG_DIR}"

log() {
  echo "[$(date -Is)] $*"
}

subject_input_ok() {
  local s="$1"
  [[ -d "${DATA_ROOT}/${s}/unprocessed/3T" ]]
}

subject_complete() {
  local s="$1"
  local root="${STUDY_ROOT}/${s}"
  [[ -f "${root}/T1w/T1w_acpc_dc_restore.nii.gz" ]] &&
    [[ -f "${root}/T1w/T1w_acpc_dc_restore_brain.nii.gz" ]] &&
    [[ -f "${root}/T1w/T2w_acpc_dc_restore.nii.gz" ]] &&
    [[ -f "${root}/T1w/${s}/scripts/recon-all.log" ]] &&
    grep -q "finished without error" "${root}/T1w/${s}/scripts/recon-all.log" &&
    [[ -f "${root}/Diffusion/eddy/eddy_unwarped_images.nii.gz" ]] &&
    [[ -f "${root}/Diffusion/data/data.nii.gz" ]] &&
    [[ -f "${root}/Diffusion/nodif_brain_mask.nii.gz" ]] &&
    [[ -f "${root}/Diffusion/reg/EPItoT1w.dat" ]] &&
    [[ -f "${root}/T1w/Diffusion/data.nii.gz" ]] &&
    [[ -f "${root}/T1w/Diffusion/nodif_brain_mask.nii.gz" ]]
}

start_subject() {
  local s="$1"
  local main_log="${LOG_DIR}/run_${s}_full.log"
  LAST_PID=""

  if ! subject_input_ok "${s}"; then
    log "[${s}] missing input tree ${DATA_ROOT}/${s}/unprocessed/3T, skip"
    return 1
  fi

  if subject_complete "${s}" && [[ "${FORCE_RERUN}" -ne 1 ]]; then
    log "[${s}] final outputs already complete, skip"
    return 1
  fi

  : > "${main_log}"

  log "[${s}] launching"
  nohup bash "${RUNNER}" \
    --subject "${s}" \
    --data-root "${DATA_ROOT}" \
    --study-root "${STUDY_ROOT}" \
    --hcppipedir "${HCPPIPEDIR}" \
    --python-bin "${PYTHON_BIN}" \
    --gpu "${USE_GPU}" \
    --force-rerun "${FORCE_RERUN}" \
    >/dev/null 2>&1 &

  LAST_PID=$!
  sleep 2
  return 0
}

idx=0
total=${#SUBJECTS[@]}

while [[ "${idx}" -lt "${total}" ]]; do
  batch=()
  pids=()
  for ((j=0; j<MAX_JOBS && idx<total; j++)); do
    s="${SUBJECTS[$idx]}"
    if start_subject "${s}"; then
      batch+=( "${s}" )
      pids+=( "${LAST_PID}" )
    fi
    idx=$((idx + 1))
  done

  [[ "${#batch[@]}" -gt 0 ]] || continue

  log "waiting for batch: ${batch[*]}"
  for ((k=0; k<${#pids[@]}; k++)); do
    s="${batch[$k]}"
    pid="${pids[$k]}"
    if wait "${pid}"; then
      if subject_complete "${s}"; then
        log "[${s}] complete; final artifacts verified"
      else
        log "[${s}] process exited 0 but final artifacts are incomplete"
        exit 1
      fi
    else
      rc=$?
      log "[${s}] failed with exit code ${rc}; see ${LOG_DIR}/run_${s}_full.log"
      exit "${rc}"
    fi
  done
done

log "all batch subjects finished; successful subjects have verified final artifacts"
