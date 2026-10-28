#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  convert_to_nhdr.sh \
    --dwi-nifti <path> \
    --bval <path> \
    --bvec <path> \
    --out-nhdr <path>

Converts DWI NIfTI + bval/bvec into NHDR using Slicer DWIConvert.
USAGE
}

DWI_NII=""
BVAL=""
BVEC=""
OUT_NHDR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dwi-nifti)
      DWI_NII="$2"; shift 2 ;;
    --bval)
      BVAL="$2"; shift 2 ;;
    --bvec)
      BVEC="$2"; shift 2 ;;
    --out-nhdr)
      OUT_NHDR="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$DWI_NII" || -z "$BVAL" || -z "$BVEC" || -z "$OUT_NHDR" ]]; then
  usage
  exit 1
fi

SLICER="/opt/Slicer-5.2.2-linux-amd64/Slicer"
DWICONVERT="$SLICER --launch DWIConvert"

if [[ ! -x "$SLICER" ]]; then
  echo "Error: Slicer not found at $SLICER"
  exit 1
fi

OUT_NHDR_DIR="$(dirname "$OUT_NHDR")"
OUT_NHDR_NAME="$(basename "$OUT_NHDR")"
if [[ "$OUT_NHDR_NAME" != *.nhdr && "$OUT_NHDR_NAME" != *.nrrd ]]; then
  OUT_NHDR_NAME="${OUT_NHDR_NAME}.nhdr"
fi
mkdir -p "$OUT_NHDR_DIR"

$DWICONVERT \
  --conversionMode FSLToNrrd \
  --fslNIFTIFile "$DWI_NII" \
  --outputVolume "$OUT_NHDR_NAME" \
  --outputDirectory "$OUT_NHDR_DIR" \
  --inputBVectors "$BVEC" \
  --inputBValues "$BVAL" \
  --allowLossyConversion
