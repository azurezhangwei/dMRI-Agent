#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'USAGE'
Usage: recon_all.sh --subject <id> --t1 <T1w_path> --processed-root <dir> [--fs-home <dir>] [--license <path>]
USAGE
}

subject=""
t1=""
processed_root=""
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
    --processed-root)
      processed_root="$2"
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

if [[ -z "$subject" || -z "$t1" || -z "$processed_root" ]]; then
  echo "[error] --subject, --t1, and --processed-root are required" >&2
  usage
  exit 2
fi

subjects_dir="$processed_root/$subject/anat/FreeSurfer"
mkdir -p "$subjects_dir"

export FREESURFER_HOME="$fs_home"
export FS_LICENSE="$fs_license"
export SUBJECTS_DIR="$subjects_dir"
export PATH="$FREESURFER_HOME/bin:$PATH"

source "$FREESURFER_HOME/SetUpFreeSurfer.sh"

recon-all -s "$subject" -i "$t1" -all

echo "[OK] recon-all completed"
echo "[OK] SUBJECTS_DIR=$SUBJECTS_DIR"
echo "[OK] Subject=$subject"
