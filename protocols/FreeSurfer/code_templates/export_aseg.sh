#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'USAGE'
Usage: export_aseg.sh --subject <id> [--subjects-dir <dir>] [--out <path>] [--fs-home <dir>] [--license <path>]
USAGE
}

subject=""
subjects_dir=""
output=""
fs_home="/opt/freesurfer"
fs_license="/opt/freesurfer/license.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject)
      subject="$2"
      shift 2
      ;;
    --subjects-dir)
      subjects_dir="$2"
      shift 2
      ;;
    --out)
      output="$2"
      shift 2
      ;;
    --fs-home)
      fs_home="$2"
      shift 2
      ;;
    --license)
      fs_license="$2"
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

if [[ -z "$subject" ]]; then
  echo "[error] --subject is required" >&2
  usage
  exit 2
fi

if [[ -z "$subjects_dir" ]]; then
  subjects_dir="/workspace/docker_data/processed/${subject}/FreeSurfer"
fi

export FREESURFER_HOME="$fs_home"
export FS_LICENSE="$fs_license"
export SUBJECTS_DIR="$subjects_dir"
export PATH="$FREESURFER_HOME/bin:$PATH"

source "$FREESURFER_HOME/SetUpFreeSurfer.sh"

aseg_mgz="$SUBJECTS_DIR/$subject/mri/aseg.mgz"
if [[ -z "$output" ]]; then
  output="$SUBJECTS_DIR/$subject/mri/aseg.nii.gz"
fi

mri_convert "$aseg_mgz" "$output"

echo "[OK] aseg exported to $output"
