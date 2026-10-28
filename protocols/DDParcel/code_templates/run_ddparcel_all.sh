#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_ddparcel_all.sh \
    --sub-id <id> \
    --dwi-root <processed_subject_dwi_dir> \
    [--dwi-nhdr <path>] \
    [--dwi-nifti <path> --bval <path> --bvec <path>] \
    [--mask-nii <path>] \
    [--output-dir <dir>] \
    [--atlas <path>]

Notes:
  - This is the one-shot wrapper. It resolves common paths under the subject's
    processed DWI directory and then calls ddparcel_process.sh once.
  - It never scans outside --dwi-root.
USAGE
}

SUB_ID=""
DWI_ROOT=""
DWI_NHDR=""
DWI_NII=""
BVAL=""
BVEC=""
MASK_NII=""
OUTPUT_DIR=""
ATLAS_T2=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sub-id)
      SUB_ID="$2"; shift 2 ;;
    --dwi-root)
      DWI_ROOT="$2"; shift 2 ;;
    --dwi-nhdr)
      DWI_NHDR="$2"; shift 2 ;;
    --dwi-nifti)
      DWI_NII="$2"; shift 2 ;;
    --bval)
      BVAL="$2"; shift 2 ;;
    --bvec)
      BVEC="$2"; shift 2 ;;
    --mask-nii)
      MASK_NII="$2"; shift 2 ;;
    --output-dir)
      OUTPUT_DIR="$2"; shift 2 ;;
    --atlas)
      ATLAS_T2="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$SUB_ID" || -z "$DWI_ROOT" ]]; then
  usage
  exit 1
fi

if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$DWI_ROOT/ddparcel"
fi

mkdir -p "$OUTPUT_DIR"
LOG_FILE="$OUTPUT_DIR/ddparcel_all.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[INFO] DDParcel one-shot start"
echo "[INFO] sub-id: $SUB_ID"
echo "[INFO] dwi-root: $DWI_ROOT"
echo "[INFO] output-dir: $OUTPUT_DIR"

if [[ -z "$MASK_NII" ]]; then
  CAND_MASK="$DWI_ROOT/${SUB_ID}_dwi_mask.nii.gz"
  if [[ -f "$CAND_MASK" ]]; then
    MASK_NII="$CAND_MASK"
  else
    echo "Error: --mask-nii not provided and default mask not found: $CAND_MASK"
    exit 1
  fi
fi

if [[ -z "$DWI_NHDR" ]]; then
  CAND_NHDR="$DWI_ROOT/${SUB_ID}_dwi.nhdr"
  if [[ -f "$CAND_NHDR" ]]; then
    DWI_NHDR="$CAND_NHDR"
  fi
fi

if [[ -z "$DWI_NHDR" ]]; then
  if [[ -z "$DWI_NII" ]]; then
    CAND_NII="$DWI_ROOT/eddy/eddy_corrected.nii.gz"
    CAND_BVAL="$DWI_ROOT/eddy/eddy_corrected.bval"
    CAND_BVEC="$DWI_ROOT/eddy/eddy_corrected.bvec"
    if [[ -f "$CAND_NII" && -f "$CAND_BVAL" && -f "$CAND_BVEC" ]]; then
      DWI_NII="$CAND_NII"
      BVAL="$CAND_BVAL"
      BVEC="$CAND_BVEC"
    fi
  fi

  if [[ -z "$DWI_NII" || -z "$BVAL" || -z "$BVEC" ]]; then
    echo "Error: No NHDR found and conversion inputs missing."
    echo "Provide --dwi-nhdr, or --dwi-nifti/--bval/--bvec, or ensure eddy outputs exist."
    exit 1
  fi

  DWI_NHDR="$DWI_ROOT/${SUB_ID}_dwi.nhdr"
fi

SCRIPT_DIR="$(dirname "$0")"
PROCESS_SCRIPT="$SCRIPT_DIR/ddparcel_process.sh"
if [[ ! -x "$PROCESS_SCRIPT" ]]; then
  echo "Error: ddparcel_process.sh not found or not executable at $PROCESS_SCRIPT"
  exit 1
fi

CMD=(bash "$PROCESS_SCRIPT" --sub-id "$SUB_ID" --dwi-nhdr "$DWI_NHDR" --mask-nii "$MASK_NII" --output-dir "$OUTPUT_DIR")
if [[ -n "$DWI_NII" && -n "$BVAL" && -n "$BVEC" ]]; then
  CMD+=(--dwi-nifti "$DWI_NII" --bval "$BVAL" --bvec "$BVEC")
fi
if [[ -n "$ATLAS_T2" ]]; then
  CMD+=(--atlas "$ATLAS_T2")
fi

echo "[INFO] Running: ${CMD[*]}"
"${CMD[@]}"

echo "[OK] DDParcel one-shot finished."
