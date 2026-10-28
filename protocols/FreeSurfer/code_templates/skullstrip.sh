#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'USAGE'
Usage: skullstrip.sh --subject <id> --t1 <T1w_path> [--subjects-dir <dir>] [--fs-home <dir>] [--license <path>]
USAGE
}

subject=""
t1=""
subjects_dir=""
fs_home="/opt/freesurfer"
fs_license="/opt/freesurfer/license.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subject)
      subject="$2"
      shift 2
      ;;
    --t1)
      t1="$2"
      shift 2
      ;;
    --subjects-dir)
      subjects_dir="$2"
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

if [[ -z "$subject" || -z "$t1" ]]; then
  echo "[error] --subject and --t1 are required" >&2
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

recon-all -s "$subject" -i "$t1" -skullstrip

echo "[OK] skullstrip completed. SUBJECTS_DIR=$SUBJECTS_DIR subject=$subject"
