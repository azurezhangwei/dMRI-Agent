#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'USAGE'
Usage: export_aparc_aseg.sh --subject <id> --processed-root <dir> [--out <path>] [--fs-home <dir>] [--license <path>]
USAGE
}

subject=""
processed_root=""
output=""
fs_home="/opt/freesurfer"
fs_license="/opt/freesurfer/license.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject)
      subject="$2"
      shift 2
      ;;
    --processed-root)
      processed_root="$2"
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

if [[ -z "$subject" || -z "$processed_root" ]]; then
  echo "[error] --subject and --processed-root are required" >&2
  usage
  exit 2
fi

subjects_dir="$processed_root/$subject/anat/FreeSurfer"
subject_dir="$subjects_dir/$subject"

export FREESURFER_HOME="$fs_home"
export FS_LICENSE="$fs_license"
export SUBJECTS_DIR="$subjects_dir"
export PATH="$FREESURFER_HOME/bin:$PATH"

source "$FREESURFER_HOME/SetUpFreeSurfer.sh"

if [[ -z "$output" ]]; then
  output="$processed_root/$subject/registration/aparc+aseg.nii.gz"
fi

mkdir -p "$(dirname "$output")"

mri_convert "$subject_dir/mri/aparc+aseg.mgz" "$output"

echo "[OK] Exported aparc+aseg to $output"
