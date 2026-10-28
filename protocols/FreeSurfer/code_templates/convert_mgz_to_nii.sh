#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'USAGE'
Usage: convert_mgz_to_nii.sh --input <mgz> --output <nii.gz> [--fs-home <dir>] [--license <path>]
USAGE
}

input=""
output=""
fs_home="/opt/freesurfer"
fs_license="/opt/freesurfer/license.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)
      input="$2"
      shift 2
      ;;
    --output)
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

if [[ -z "$input" || -z "$output" ]]; then
  echo "[error] --input and --output are required" >&2
  usage
  exit 2
fi

export FREESURFER_HOME="$fs_home"
export FS_LICENSE="$fs_license"
export PATH="$FREESURFER_HOME/bin:$PATH"

source "$FREESURFER_HOME/SetUpFreeSurfer.sh"

mri_convert "$input" "$output"

echo "[OK] Converted $input -> $output"
