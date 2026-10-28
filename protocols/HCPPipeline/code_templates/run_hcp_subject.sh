#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_hcp_subject.sh \
    --subject <id> \
    [--data-root <dir>] \
    [--study-root <dir>] \
    [--hcppipedir <dir>] \
    [--python-bin <path>] \
    [--gpu true|false] \
    [--force-rerun 0|1]

Runs the HCP-style structural + diffusion preprocessing workflow inside the container:
  PreFreeSurfer -> FreeSurfer -> DiffPreproc -> PostEddy

Defaults:
  --data-root   /workspace/docker_data/data
  --study-root  /workspace/docker_data/processed/HCPpipeline_Agent_results
  --hcppipedir  /workspace/HCPpipelines
  --python-bin  /opt/conda/envs/wma/bin/python3.9
  --gpu         true
  --force-rerun 0
USAGE
}

SUBJECT=""
DATA_ROOT="/workspace/docker_data/data"
STUDY_ROOT="/workspace/docker_data/processed/HCPpipeline_Agent_results"
HCPPIPEDIR="/workspace/HCPpipelines"
PYTHON_BIN="/opt/conda/envs/wma/bin/python3.9"
USE_GPU="true"
FORCE_RERUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject)
      SUBJECT="$2"; shift 2 ;;
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
      echo "Unknown argument: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [[ -z "$SUBJECT" ]]; then
  usage
  exit 1
fi

if [[ ! "$FORCE_RERUN" =~ ^[01]$ ]]; then
  echo "Error: --force-rerun must be 0 or 1" >&2
  exit 1
fi

export TERM=xterm
export HCPPIPEDIR
export HCPPIPEDIR_Global="${HCPPIPEDIR}/global/scripts"
export HCPPIPEDIR_Config="${HCPPIPEDIR}/global/config"
export HCPPIPEDIR_Templates="${HCPPIPEDIR}/global/templates"
export FSLDIR="${FSLDIR:-/opt/fsl}"
export FREESURFER_HOME="${FREESURFER_HOME:-/opt/freesurfer}"
export CARET7DIR="${CARET7DIR:-/opt/workbench/bin_linux64}"
export PATH="${CARET7DIR}:$PATH"
export FSLOUTPUTTYPE=NIFTI_GZ
export OMP_NUM_THREADS=1
export ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export FSLPARALLEL=0
export FSL_SKIP_PYTHON_VERSION_CHECK=1

set +e
set +u
set +o pipefail
source "${FSLDIR}/etc/fslconf/fsl.sh" >/dev/null 2>&1
source "${FREESURFER_HOME}/SetUpFreeSurfer.sh" >/dev/null 2>&1
set -euo pipefail

STUDY_DIR="${STUDY_ROOT}/${SUBJECT}"
LOG_DIR="${STUDY_ROOT}/logs"
STAGE_LOG_DIR="${LOG_DIR}/stages/${SUBJECT}"
MAIN_STATUS_PREFIX="${LOG_DIR}/run_${SUBJECT}_full"
MAIN_LOG="${MAIN_STATUS_PREFIX}.log"

mkdir -p "${STUDY_DIR}" "${LOG_DIR}" "${STAGE_LOG_DIR}"

if [[ "${FORCE_RERUN}" -eq 1 ]]; then
  : > "${MAIN_LOG}"
fi

exec > >(tee -a "${MAIN_LOG}") 2>&1

log() {
  echo "[$(date -Is)] $*"
}

die() {
  log "FATAL: $*"
  exit 1
}

cleanup() {
  local rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    log "Pipeline exited successfully with exit code 0"
  else
    log "Pipeline failed with exit code ${rc}"
  fi
}
trap cleanup EXIT

assert_file() {
  local path="$1"
  [[ -f "${path}" ]] || die "Missing required file: ${path}"
}

assert_nifti_readable() {
  local path="$1"
  assert_file "${path}"
  if [[ "${path}" == *.nii.gz ]]; then
    gzip -t "${path}" || die "Invalid gzip stream, possible incomplete download: ${path}"
  fi
  fslstats "${path}" -R >/dev/null 2>&1 || die "Cannot read NIfTI voxel data, possible truncated file: ${path}"
}

assert_cmd() {
  local cmd="$1"
  command -v "${cmd}" >/dev/null 2>&1 || die "Missing required command: ${cmd}"
}

run_logged() {
  local stage="$1"
  shift
  local stage_log="${STAGE_LOG_DIR}/${stage}.log"
  : > "${stage_log}"
  log "START ${stage}"
  set +e
  "$@" 2>&1 | tee -a "${stage_log}"
  local rc=${PIPESTATUS[0]}
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    log "FAIL ${stage} rc=${rc}"
    return "${rc}"
  fi
  log "END ${stage}"
  return 0
}

write_stage_artifacts() {
  local stage="$1"
  shift
  local report="${STAGE_LOG_DIR}/${stage}_artifacts.tsv"
  : > "${report}"
  while [[ "$#" -gt 0 ]]; do
    printf "%s\t%s\n" "$1" "$2" >> "${report}"
    shift 2
  done
  log "Artifact handoff for ${stage}: ${report}"
}

prepare_subject_workspace() {
  local src="${DATA_ROOT}/${SUBJECT}/unprocessed"
  local dst="${STUDY_DIR}/unprocessed"
  [[ -d "${src}" ]] || die "Missing input directory: ${src}"
  if [[ ! -e "${dst}" ]]; then
    ln -s "${src}" "${dst}"
  fi
}

T1W_IMG="${STUDY_DIR}/unprocessed/3T/T1w_MPR1/${SUBJECT}_3T_T1w_MPR1.nii.gz"
T2W_IMG="${STUDY_DIR}/unprocessed/3T/T2w_SPC1/${SUBJECT}_3T_T2w_SPC1.nii.gz"
FMAP_MAG="${STUDY_DIR}/unprocessed/3T/T1w_MPR1/${SUBJECT}_3T_FieldMap_Magnitude.nii.gz"
FMAP_PHASE="${STUDY_DIR}/unprocessed/3T/T1w_MPR1/${SUBJECT}_3T_FieldMap_Phase.nii.gz"

POSDATA="${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir95_RL.nii.gz@${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir96_RL.nii.gz@${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir97_RL.nii.gz"
NEGDATA="${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir95_LR.nii.gz@${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir96_LR.nii.gz@${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir97_LR.nii.gz"

FS_SESSION_DIR="${STUDY_DIR}/T1w"
FS_SUBJECT_DIR="${FS_SESSION_DIR}/${SUBJECT}"
T1W_RESTORE="${FS_SESSION_DIR}/T1w_acpc_dc_restore.nii.gz"
T1W_RESTORE_BASE="${FS_SESSION_DIR}/T1w_acpc_dc_restore"
T1W_RESTORE_BRAIN="${FS_SESSION_DIR}/T1w_acpc_dc_restore_brain.nii.gz"
T1W_RESTORE_BRAIN_BASE="${FS_SESSION_DIR}/T1w_acpc_dc_restore_brain"
T2W_RESTORE="${FS_SESSION_DIR}/T2w_acpc_dc_restore.nii.gz"
T1W_ACPC_BASE="${FS_SESSION_DIR}/T1w_acpc_dc"
BIASFIELD_BASE="${FS_SESSION_DIR}/BiasField_acpc_dc"
FS_LOG="${FS_SUBJECT_DIR}/scripts/recon-all.log"
EYE_DAT="${FS_SUBJECT_DIR}/mri/transforms/eye.dat"
DIFF_DIR="${STUDY_DIR}/Diffusion"
DIFF_T1W_DIR="${FS_SESSION_DIR}/Diffusion"

prefreesurfer_complete() {
  [[ -f "${T1W_RESTORE}" && -f "${T1W_RESTORE_BRAIN}" && -f "${T2W_RESTORE}" ]]
}

freesurfer_complete() {
  [[ -f "${FS_LOG}" ]] && grep -q "finished without error" "${FS_LOG}" &&
    [[ -f "${EYE_DAT}" ]] &&
    [[ -f "${FS_SUBJECT_DIR}/mri/brainmask.mgz" ]] &&
    [[ -f "${FS_SUBJECT_DIR}/surf/lh.orig" ]] &&
    [[ -f "${FS_SUBJECT_DIR}/surf/rh.orig" ]]
}

diffpreproc_complete() {
  [[ -f "${DIFF_DIR}/eddy/eddy_unwarped_images.nii.gz" && -f "${DIFF_DIR}/data/data.nii.gz" ]]
}

posteddy_complete() {
  [[ -f "${DIFF_DIR}/reg/EPItoT1w.dat" && -f "${DIFF_T1W_DIR}/data.nii.gz" ]]
}

validate_prefreesurfer() {
  assert_file "${T1W_RESTORE}"
  assert_file "${T1W_RESTORE_BRAIN}"
  assert_file "${T2W_RESTORE}"
}

validate_freesurfer() {
  freesurfer_complete || die "FreeSurfer did not complete successfully"
  assert_file "${EYE_DAT}"
  assert_file "${FS_SUBJECT_DIR}/mri/brainmask.mgz"
  assert_file "${FS_SUBJECT_DIR}/surf/lh.orig"
  assert_file "${FS_SUBJECT_DIR}/surf/rh.orig"
}

validate_diffpreproc() {
  assert_file "${DIFF_DIR}/eddy/eddy_unwarped_images.nii.gz"
  assert_file "${DIFF_DIR}/data/data.nii.gz"
  assert_file "${DIFF_DIR}/nodif_brain_mask.nii.gz"
}

validate_posteddy() {
  assert_file "${DIFF_DIR}/reg/EPItoT1w.dat"
  assert_file "${DIFF_T1W_DIR}/data.nii.gz"
  assert_file "${DIFF_T1W_DIR}/nodif_brain_mask.nii.gz"
}

preflight() {
  assert_file "${PYTHON_BIN}"
  assert_cmd wb_command
  assert_cmd fslmaths
  assert_cmd fslstats
  assert_cmd flirt
  assert_cmd mri_convert
  assert_cmd bbregister
  assert_cmd mri_surf2surf
  assert_cmd mris_convert
  assert_cmd mris_autodet_gwstats

  [[ -d "${HCPPIPEDIR}" ]] || die "Missing HCPPIPEDIR: ${HCPPIPEDIR}"
  [[ -n "${CARET7DIR}" ]] || die "CARET7DIR is empty"

  assert_nifti_readable "${T1W_IMG}"
  assert_nifti_readable "${T2W_IMG}"
  assert_nifti_readable "${FMAP_MAG}"
  assert_nifti_readable "${FMAP_PHASE}"

  local d
  for d in \
    "${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir95_RL.nii.gz" \
    "${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir96_RL.nii.gz" \
    "${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir97_RL.nii.gz" \
    "${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir95_LR.nii.gz" \
    "${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir96_LR.nii.gz" \
    "${STUDY_DIR}/unprocessed/3T/Diffusion/${SUBJECT}_3T_DWI_dir97_LR.nii.gz"; do
    assert_nifti_readable "${d}"
  done
}

ensure_tkregister() {
  if [[ ! -x "${FREESURFER_HOME}/bin/tkregister" && -x "${FREESURFER_HOME}/bin/tkregister2" ]]; then
    ln -sf "${FREESURFER_HOME}/bin/tkregister2" "${FREESURFER_HOME}/bin/tkregister"
  fi
}

install_wrappers() {
  ensure_tkregister

  cat > /tmp/mri_surf2surf <<'EOF_MRI_SURF2SURF'
#!/usr/bin/env bash
set -euo pipefail
hemi=""
newargs=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hemi)
      hemi="$2"
      newargs+=( "$1" "$2" )
      shift 2
      ;;
    --tval)
      tval="$2"
      if [[ "$tval" == "white.preaparc.rawavg" && -n "$hemi" ]]; then
        newargs+=( "$1" "../surf/${hemi}.white.preaparc.rawavg" )
      else
        newargs+=( "$1" "$2" )
      fi
      shift 2
      ;;
    *)
      newargs+=( "$1" )
      shift
      ;;
  esac
done
exec /opt/freesurfer/bin/mri_surf2surf "${newargs[@]}"
EOF_MRI_SURF2SURF
  chmod +x /tmp/mri_surf2surf

  cat > /tmp/fslmaths <<EOF_FSLMATHS
#!/usr/bin/env bash
set -euo pipefail
export FSLOUTPUTTYPE="\${FSLOUTPUTTYPE:-NIFTI_GZ}"
export OMP_NUM_THREADS="\${OMP_NUM_THREADS:-1}"
export ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS="\${ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS:-1}"
export OPENBLAS_NUM_THREADS="\${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="\${MKL_NUM_THREADS:-1}"
if [[ "\$#" -eq 4 && "\$2" == "-thr" && "\$3" == "0" ]]; then
  in="\$1"
  out="\$4"
  "${PYTHON_BIN}" - <<'PY' "\$in" "\$out"
import sys
import numpy as np
import nibabel as nib
inp = sys.argv[1]
outp = sys.argv[2]
img = nib.load(inp)
data = img.get_fdata()
data[data < 0] = 0
out = nib.Nifti1Image(data.astype(np.float32), img.affine, img.header)
nib.save(out, outp)
PY
  exit 0
fi
exec /opt/fsl/bin/fslmaths "\$@"
EOF_FSLMATHS
  chmod +x /tmp/fslmaths

  cat > /tmp/mris_convert <<'EOF_MRIS_CONVERT'
#!/usr/bin/env bash
set -euo pipefail
hemi=""
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --volume)
      args+=( "$1" "$2" "$3" )
      hemi="$3"
      shift 3
      ;;
    *)
      args+=( "$1" )
      shift
      ;;
  esac
done
if [[ -n "$hemi" ]]; then
  ensure_link() {
    local base="$1"
    local target="${hemi}.${base}"
    if [[ -L "$target" && ! -e "$target" ]]; then
      if [[ -f "${target}.rawavg" ]]; then
        ln -sf "${target}.rawavg" "$target"
      elif [[ -f "${target}.preaparc" ]]; then
        ln -sf "${target}.preaparc" "$target"
      fi
    fi
    if [[ ! -e "$target" ]]; then
      if [[ -f "${target}.rawavg" ]]; then
        ln -sf "${target}.rawavg" "$target"
      elif [[ -f "${target}.preaparc" ]]; then
        ln -sf "${target}.preaparc" "$target"
      fi
    fi
  }
  ensure_link white
  ensure_link pial
fi
exec /opt/freesurfer/bin/mris_convert "${args[@]}"
EOF_MRIS_CONVERT
  chmod +x /tmp/mris_convert

  export PATH="/tmp:${PATH}"
}

repair_fs_links() {
  local surf_dir="${FS_SUBJECT_DIR}/surf"
  [[ -d "${surf_dir}" ]] || return 0
  cd "${surf_dir}"

  fix_link() {
    local hemi="$1"
    local base="$2"
    local f="${hemi}.${base}"
    if [[ -L "${f}" && ! -e "${f}" ]]; then
      if [[ -f "${f}.rawavg" ]]; then
        ln -sf "${f}.rawavg" "${f}"
      elif [[ -f "${f}.preaparc" ]]; then
        ln -sf "${f}.preaparc" "${f}"
      fi
    fi
    if [[ ! -e "${f}" ]]; then
      if [[ -f "${f}.rawavg" ]]; then
        ln -sf "${f}.rawavg" "${f}"
      elif [[ -f "${f}.preaparc" ]]; then
        ln -sf "${f}.preaparc" "${f}"
      fi
    fi
  }

  fix_link lh white
  fix_link rh white
  fix_link lh pial
  fix_link rh pial
}

ensure_white_preaparc() {
  [[ -f "${FS_SUBJECT_DIR}/mri/rawavg.cmdc.mgz" ]] || return 0
  cd "${FS_SUBJECT_DIR}/mri"
  if [[ ! -f ../surf/lh.white.preaparc.rawavg ]]; then
    mri_surf2surf --s "${SUBJECT}" --hemi lh --sval-xyz white.preaparc --surfreg white.preaparc \
      --reg transforms/rawavg2conf.dat rawavg.cmdc.mgz --tval-xyz rawavg.cmdc.mgz --tval white.preaparc.rawavg
  fi
  if [[ ! -f ../surf/rh.white.preaparc.rawavg ]]; then
    mri_surf2surf --s "${SUBJECT}" --hemi rh --sval-xyz white.preaparc --surfreg white.preaparc \
      --reg transforms/rawavg2conf.dat rawavg.cmdc.mgz --tval-xyz rawavg.cmdc.mgz --tval white.preaparc.rawavg
  fi
}

ensure_autodet_stats() {
  [[ -f "${FS_SUBJECT_DIR}/mri/brain.finalsurfs.mgz" ]] || return 0
  cd "${FS_SUBJECT_DIR}/mri"
  [[ -f ../surf/autodet.gw.stats.lh.dat ]] || \
    mris_autodet_gwstats --o ../surf/autodet.gw.stats.lh.dat --i brain.finalsurfs.mgz --wm wm.mgz --surf ../surf/lh.orig
  [[ -f ../surf/autodet.gw.stats.rh.dat ]] || \
    mris_autodet_gwstats --o ../surf/autodet.gw.stats.rh.dat --i brain.finalsurfs.mgz --wm wm.mgz --surf ../surf/rh.orig
}

ensure_brainmask_fs() {
  if [[ ! -f "${FS_SESSION_DIR}/brainmask_fs.nii.gz" && -f "${FS_SUBJECT_DIR}/mri/brainmask.mgz" ]]; then
    mri_convert "${FS_SUBJECT_DIR}/mri/brainmask.mgz" "${FS_SESSION_DIR}/brainmask_fs.nii.gz"
  fi
  if [[ -f "${FS_SESSION_DIR}/brainmask_fs.nii.gz" ]]; then
    ln -sf "${FS_SESSION_DIR}/brainmask_fs.nii.gz" "${FS_SESSION_DIR}/brainmask_fs"
  fi
}

ensure_white_deformed() {
  local surf_dir="${FS_SUBJECT_DIR}/surf"
  [[ -d "${surf_dir}" ]] || return 0
  if [[ -f "${surf_dir}/lh.white.rawavg" ]]; then
    ln -sf lh.white.rawavg "${surf_dir}/lh.white"
    ln -sf rh.white.rawavg "${surf_dir}/rh.white"
    ln -sf lh.white "${surf_dir}/lh.white.deformed"
    ln -sf rh.white "${surf_dir}/rh.white.deformed"
  fi
}

repair_freesurfer_outputs() {
  install_wrappers
  repair_fs_links
  ensure_white_preaparc
  ensure_autodet_stats
  ensure_brainmask_fs
  ensure_white_deformed
}

compute_existing_session() {
  if [[ -f "${FS_SUBJECT_DIR}/mri/orig.mgz" ]]; then
    printf "TRUE"
  else
    printf "FALSE"
  fi
}

prepare_fresh_fs_dir_if_needed() {
  local existing_session
  existing_session="$(compute_existing_session)"
  mkdir -p "${FS_SESSION_DIR}"
  if [[ "${existing_session}" == "FALSE" && -d "${FS_SUBJECT_DIR}" ]]; then
    local ts
    ts="$(date +%Y%m%d_%H%M%S)"
    mv "${FS_SUBJECT_DIR}" "${FS_SESSION_DIR}/${SUBJECT}_backup_${ts}"
  fi
  if [[ "${existing_session}" == "FALSE" ]]; then
    rm -rf "${FS_SUBJECT_DIR}"
  else
    mkdir -p "${FS_SUBJECT_DIR}"
  fi
}

repair_diffusion_t1w_geometry() {
  local base="${DIFF_T1W_DIR}"
  local data="${base}/data.nii.gz"
  [[ -f "${data}" ]] || return 0
  local mask
  for mask in nodif_brain_mask_temp fov_mask; do
    local target="${base}/${mask}.nii.gz"
    [[ -f "${target}" ]] || continue
    if ! "${PYTHON_BIN}" - <<'PY' "${data}" "${target}"
import sys
import numpy as np
import nibabel as nib
ref = nib.load(sys.argv[1])
img = nib.load(sys.argv[2])
same_shape = ref.shape[:3] == img.shape[:3]
same_affine = np.allclose(ref.affine, img.affine, atol=1e-4)
raise SystemExit(0 if same_shape and same_affine else 1)
PY
    then
      local backup="${target}.bak.$(date +%s)"
      log "Realigning ${mask}.nii.gz to data.nii.gz"
      mv "${target}" "${backup}"
      flirt -in "${backup}" -ref "${data}" -applyxfm -usesqform \
        -interp nearestneighbour -out "${target}"
    fi
  done
}

run_diffusion_to_structural_repair() {
  [[ -d "${DIFF_DIR}/reg" ]] || return 1
  [[ -f "${DIFF_DIR}/data.nii.gz" ]] || return 1
  repair_freesurfer_outputs
  repair_diffusion_t1w_geometry
  run_logged diffusion_to_structural_repair \
    "${HCPPIPEDIR}/DiffusionPreprocessing/scripts/DiffusionToStructural.sh" \
    --t1folder="${FS_SESSION_DIR}" \
    --session="${SUBJECT}" \
    --workingdir="${DIFF_DIR}/reg" \
    --datadiffdir="${DIFF_DIR}/data" \
    --t1="${T1W_ACPC_BASE}" \
    --t1restore="${T1W_RESTORE_BASE}" \
    --t1restorebrain="${T1W_RESTORE_BRAIN_BASE}" \
    --biasfield="${BIASFIELD_BASE}" \
    --brainmask="${FS_SESSION_DIR}/brainmask_fs" \
    --datadiffT1wdir="${DIFF_T1W_DIR}" \
    --regoutput="${DIFF_DIR}/reg/Scout2T1w" \
    --QAimage="${DIFF_DIR}/reg/T1wMulEPI" \
    --dof=6 \
    --gdflag=0 \
    --diffresol=1.25 \
    --t1w-cross2long-xfm=
}

run_prefreesurfer_stage() {
  if prefreesurfer_complete; then
    log "PreFreeSurferPipeline skip (outputs already exist)"
    validate_prefreesurfer
    write_stage_artifacts prefreesurfer \
      t1w_restore "${T1W_RESTORE}" \
      t1w_restore_brain "${T1W_RESTORE_BRAIN}" \
      t2w_restore "${T2W_RESTORE}"
    return 0
  fi

  run_logged prefreesurfer \
    "${HCPPIPEDIR}/PreFreeSurfer/PreFreeSurferPipeline.sh" \
    --path="${STUDY_ROOT}" \
    --session="${SUBJECT}" \
    --t1="${T1W_IMG}" \
    --t2="${T2W_IMG}" \
    --t1template="${HCPPIPEDIR_Templates}/MNI152_T1_0.7mm.nii.gz" \
    --t1templatebrain="${HCPPIPEDIR_Templates}/MNI152_T1_0.7mm_brain.nii.gz" \
    --t1template2mm="${HCPPIPEDIR_Templates}/MNI152_T1_2mm.nii.gz" \
    --t2template="${HCPPIPEDIR_Templates}/MNI152_T2_0.7mm.nii.gz" \
    --t2templatebrain="${HCPPIPEDIR_Templates}/MNI152_T2_0.7mm_brain.nii.gz" \
    --t2template2mm="${HCPPIPEDIR_Templates}/MNI152_T2_2mm.nii.gz" \
    --templatemask="${HCPPIPEDIR_Templates}/MNI152_T1_0.7mm_brain_mask.nii.gz" \
    --template2mmmask="${HCPPIPEDIR_Templates}/MNI152_T1_2mm_brain_mask_dil.nii.gz" \
    --brainsize=150 \
    --fnirtconfig="${HCPPIPEDIR_Config}/T1_2_MNI152_2mm.cnf" \
    --fmapmag="${FMAP_MAG}" \
    --fmapphase="${FMAP_PHASE}" \
    --fmapcombined=NONE \
    --echodiff=2.46 \
    --SEPhaseNeg=NONE \
    --SEPhasePos=NONE \
    --seechospacing=NONE \
    --seunwarpdir=NONE \
    --t1samplespacing=0.0000074 \
    --t2samplespacing=0.0000021 \
    --unwarpdir=z \
    --gdcoeffs=NONE \
    --avgrdcmethod=SiemensFieldMap \
    --topupconfig=NONE

  validate_prefreesurfer
  write_stage_artifacts prefreesurfer \
    t1w_restore "${T1W_RESTORE}" \
    t1w_restore_brain "${T1W_RESTORE_BRAIN}" \
    t2w_restore "${T2W_RESTORE}"
}

run_freesurfer_stage() {
  repair_freesurfer_outputs

  if ! freesurfer_complete; then
    prepare_fresh_fs_dir_if_needed
    local existing_session
    existing_session="$(compute_existing_session)"

    if ! run_logged freesurfer \
      "${HCPPIPEDIR}/FreeSurfer/FreeSurferPipeline.sh" \
      --session="${SUBJECT}" \
      --session-dir="${FS_SESSION_DIR}" \
      --t1w-image="${T1W_RESTORE}" \
      --t1w-brain="${T1W_RESTORE_BRAIN}" \
      --t2w-image="${T2W_RESTORE}" \
      --existing-session="${existing_session}" \
      --extra-reconall-arg=-all; then
      log "FreeSurfer initial run failed; retrying once with repaired wrappers/state"
      repair_freesurfer_outputs
      existing_session="$(compute_existing_session)"
      run_logged freesurfer_retry \
        "${HCPPIPEDIR}/FreeSurfer/FreeSurferPipeline.sh" \
        --session="${SUBJECT}" \
        --session-dir="${FS_SESSION_DIR}" \
        --t1w-image="${T1W_RESTORE}" \
        --t1w-brain="${T1W_RESTORE_BRAIN}" \
        --t2w-image="${T2W_RESTORE}" \
        --existing-session="${existing_session}" \
        --extra-reconall-arg=-all
    fi
  else
    log "FreeSurferPipeline skip (recon-all already finished)"
  fi

  repair_freesurfer_outputs
  validate_freesurfer
  write_stage_artifacts freesurfer \
    recon_all_log "${FS_LOG}" \
    eye_dat "${EYE_DAT}" \
    brainmask_mgz "${FS_SUBJECT_DIR}/mri/brainmask.mgz" \
    lh_orig "${FS_SUBJECT_DIR}/surf/lh.orig" \
    rh_orig "${FS_SUBJECT_DIR}/surf/rh.orig" \
    t1w_restore "${T1W_RESTORE}" \
    t1w_restore_brain "${T1W_RESTORE_BRAIN}" \
    t2w_restore "${T2W_RESTORE}"
}

run_diffpreproc_stage() {
  if diffpreproc_complete; then
    log "Diffusion Preproc skip (eddy outputs already exist)"
    validate_diffpreproc
    write_stage_artifacts diffpreproc \
      eddy_unwarped "${DIFF_DIR}/eddy/eddy_unwarped_images.nii.gz" \
      diffusion_data "${DIFF_DIR}/data/data.nii.gz" \
      nodif_brain_mask "${DIFF_DIR}/nodif_brain_mask.nii.gz"
    return 0
  fi

  run_logged diffpreproc \
    "${HCPPIPEDIR}/DiffusionPreprocessing/DiffPreprocPipeline.sh" \
    --path="${STUDY_ROOT}" \
    --session="${SUBJECT}" \
    --posData="${POSDATA}" \
    --negData="${NEGDATA}" \
    --echospacing-seconds=0.00078 \
    --PEdir=1 \
    --topup-config-file="${FSLDIR}/etc/flirtsch/b02b0_1.cnf" \
    --gdcoeffs=NONE \
    --gpu="${USE_GPU}"

  validate_diffpreproc
  write_stage_artifacts diffpreproc \
    eddy_unwarped "${DIFF_DIR}/eddy/eddy_unwarped_images.nii.gz" \
    diffusion_data "${DIFF_DIR}/data/data.nii.gz" \
    nodif_brain_mask "${DIFF_DIR}/nodif_brain_mask.nii.gz"
}

run_posteddy_stage() {
  if posteddy_complete; then
    log "Diffusion PostEddy skip (EPItoT1w.dat exists)"
    validate_posteddy
    write_stage_artifacts posteddy \
      epito_t1w_dat "${DIFF_DIR}/reg/EPItoT1w.dat" \
      t1w_diffusion_data "${DIFF_T1W_DIR}/data.nii.gz" \
      t1w_nodif_brain_mask "${DIFF_T1W_DIR}/nodif_brain_mask.nii.gz"
    return 0
  fi

  if ! run_logged posteddy \
    "${HCPPIPEDIR}/DiffusionPreprocessing/DiffPreprocPipeline_PostEddy.sh" \
    --path="${STUDY_ROOT}" \
    --session="${SUBJECT}" \
    --dwiname=Diffusion \
    --gdcoeffs=NONE \
    --dof=6 \
    --combine-data-flag=1 \
    --select-best-b0=0; then
    log "Diffusion PostEddy failed; attempting targeted repair"
    if ! run_diffusion_to_structural_repair; then
      log "Targeted DiffusionToStructural repair failed; rerunning PostEddy once after repairs"
      repair_freesurfer_outputs
      repair_diffusion_t1w_geometry
      run_logged posteddy_retry \
        "${HCPPIPEDIR}/DiffusionPreprocessing/DiffPreprocPipeline_PostEddy.sh" \
        --path="${STUDY_ROOT}" \
        --session="${SUBJECT}" \
        --dwiname=Diffusion \
        --gdcoeffs=NONE \
        --dof=6 \
        --combine-data-flag=1 \
        --select-best-b0=0
    fi
  fi

  validate_posteddy
  write_stage_artifacts posteddy \
    epito_t1w_dat "${DIFF_DIR}/reg/EPItoT1w.dat" \
    t1w_diffusion_data "${DIFF_T1W_DIR}/data.nii.gz" \
    t1w_nodif_brain_mask "${DIFF_T1W_DIR}/nodif_brain_mask.nii.gz"
}

prepare_subject_workspace
preflight
run_prefreesurfer_stage
run_freesurfer_stage
run_diffpreproc_stage
run_posteddy_stage
log "HCP pipeline for ${SUBJECT} complete"
