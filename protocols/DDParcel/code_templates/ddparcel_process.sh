#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ddparcel_process.sh \
    --sub-id <id> \
    --dwi-nhdr <path> \
    [--dwi-nifti <path> --bval <path> --bvec <path>] \
    --mask-nii <path> \
    --output-dir <dir> \
    [--atlas <path>]
USAGE
}

SUB_ID=""
DWI_NHDR=""
DWI_NII=""
BVAL=""
BVEC=""
MASK_NII=""
OUTPUT_DIR=""
ATLAS_T2="/opt/DDParcel/100HCP-population-mean-T2-1mm.nii.gz"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sub-id)
      SUB_ID="$2"; shift 2 ;;
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

if [[ -z "$SUB_ID" || -z "$MASK_NII" || -z "$OUTPUT_DIR" ]]; then
  usage
  exit 1
fi

source /opt/conda/etc/profile.d/conda.sh
conda activate wma

if [[ -z "${OMP_NUM_THREADS:-}" || ! "$OMP_NUM_THREADS" =~ ^[0-9]+$ ]]; then
  export OMP_NUM_THREADS=4
fi

NIFTI_WRITE="$(command -v nifti_write.py || true)"
if [[ -z "$NIFTI_WRITE" ]]; then
  NIFTI_WRITE="/opt/conda/envs/wma/bin/nifti_write.py"
fi
if [[ ! -f "$NIFTI_WRITE" ]]; then
  echo "Error: nifti_write.py not found at $NIFTI_WRITE"
  exit 1
fi

SLICER="/opt/Slicer-5.2.2-linux-amd64/Slicer"
DWIToDTIEstimation="$SLICER --launch /opt/Slicer-5.2.2-linux-amd64/NA-MIC/Extensions-31382/SlicerDMRI/lib/Slicer-5.2/cli-modules/DWIToDTIEstimation"
DiffusionTensorScalarMeasurements="$SLICER --launch /opt/Slicer-5.2.2-linux-amd64/NA-MIC/Extensions-31382/SlicerDMRI/lib/Slicer-5.2/cli-modules/DiffusionTensorScalarMeasurements"
BRAINSFit="$SLICER --launch /opt/Slicer-5.2.2-linux-amd64/lib/Slicer-5.2/cli-modules/BRAINSFit"
ResampleScalarVectorDWIVolume="$SLICER --launch /opt/Slicer-5.2.2-linux-amd64/lib/Slicer-5.2/cli-modules/ResampleScalarVectorDWIVolume"
BRAINSTransformConvert="$SLICER --launch /opt/Slicer-5.2.2-linux-amd64/lib/Slicer-5.2/cli-modules/BRAINSTransformConvert"

mkdir -p "$OUTPUT_DIR"

if [[ -z "$DWI_NHDR" ]]; then
  if [[ -n "$DWI_NII" && -n "$BVAL" && -n "$BVEC" ]]; then
    DWI_NHDR="$OUTPUT_DIR/${SUB_ID}_dwi.nhdr"
  else
    echo "Error: --dwi-nhdr is required, or provide --dwi-nifti/--bval/--bvec for conversion."
    exit 1
  fi
fi

if [[ ! -f "$DWI_NHDR" ]]; then
  if [[ -z "$DWI_NII" || -z "$BVAL" || -z "$BVEC" ]]; then
    echo "Error: DWI NHDR not found and conversion inputs missing."
    exit 1
  fi
  CONVERT_SCRIPT="$(dirname "$0")/convert_to_nhdr.sh"
  if [[ ! -x "$CONVERT_SCRIPT" ]]; then
    echo "Error: convert_to_nhdr.sh not found or not executable at $CONVERT_SCRIPT"
    exit 1
  fi
  bash "$CONVERT_SCRIPT" --dwi-nifti "$DWI_NII" --bval "$BVAL" --bvec "$BVEC" --out-nhdr "$DWI_NHDR"
fi

NRRD_DTI="$OUTPUT_DIR/${SUB_ID}-dti.nhdr"
NRRD_B0="$OUTPUT_DIR/${SUB_ID}-b0.nhdr"
if [[ ! -f "$NRRD_B0" ]]; then
  $DWIToDTIEstimation --enumeration LS "$DWI_NHDR" "$NRRD_DTI" "$NRRD_B0"
fi

NRRD_FA="$OUTPUT_DIR/${SUB_ID}-dti-FractionalAnisotropy.nhdr"
NRRD_TRACE="$OUTPUT_DIR/${SUB_ID}-dti-Trace.nhdr"
NRRD_MINEIG="$OUTPUT_DIR/${SUB_ID}-dti-MinEigenvalue.nhdr"
NRRD_MIDEIG="$OUTPUT_DIR/${SUB_ID}-dti-MidEigenvalue.nhdr"

NII_FA="$OUTPUT_DIR/${SUB_ID}-dti-FractionalAnisotropy.nii.gz"
NII_TRACE="$OUTPUT_DIR/${SUB_ID}-dti-Trace.nii.gz"
NII_MINEIG="$OUTPUT_DIR/${SUB_ID}-dti-MinEigenvalue.nii.gz"
NII_MIDEIG="$OUTPUT_DIR/${SUB_ID}-dti-MidEigenvalue.nii.gz"

if [[ ! -f "$NII_MIDEIG" ]]; then
  $DiffusionTensorScalarMeasurements --enumeration FractionalAnisotropy "$NRRD_DTI" "$NRRD_FA"
  $DiffusionTensorScalarMeasurements --enumeration Trace "$NRRD_DTI" "$NRRD_TRACE"
  $DiffusionTensorScalarMeasurements --enumeration MinEigenvalue "$NRRD_DTI" "$NRRD_MINEIG"
  $DiffusionTensorScalarMeasurements --enumeration MidEigenvalue "$NRRD_DTI" "$NRRD_MIDEIG"

  python "$NIFTI_WRITE" -i "$NRRD_FA" -p "${NRRD_FA%.nhdr}"
  python "$NIFTI_WRITE" -i "$NRRD_TRACE" -p "${NRRD_TRACE%.nhdr}"
  python "$NIFTI_WRITE" -i "$NRRD_MINEIG" -p "${NRRD_MINEIG%.nhdr}"
  python "$NIFTI_WRITE" -i "$NRRD_MIDEIG" -p "${NRRD_MIDEIG%.nhdr}"
fi

TFM="$OUTPUT_DIR/${SUB_ID}-b0ToAtlasT2.tfm"
TFM_INV_TFM="$OUTPUT_DIR/${SUB_ID}-b0ToAtlasT2_Inverse.tfm"
TFM_INV_H5="$OUTPUT_DIR/${SUB_ID}-b0ToAtlasT2_Inverse.h5"
if [[ ! -f "$TFM" ]]; then
  $BRAINSFit --fixedVolume "$ATLAS_T2" --movingVolume "$NRRD_B0" \
    --linearTransform "$TFM" --useRigid --useAffine
fi

if [[ ! -f "$TFM_INV_TFM" && -f "$TFM_INV_H5" ]]; then
  if head -n 1 "$TFM_INV_H5" | grep -q "#Insight Transform File"; then
    cp "$TFM_INV_H5" "$TFM_INV_TFM"
  else
    if head -c 4 "$TFM_INV_H5" | grep -q "HDF"; then
      $BRAINSTransformConvert --inputTransform "$TFM_INV_H5" --outputTransform "$TFM_INV_TFM" || true
    fi
  fi
fi

if [[ ! -f "$TFM_INV_TFM" ]]; then
  "$SLICER" --launch PythonSlicer "$(dirname "$0")/invert_affine.py" "$TFM" "$TFM_INV_TFM"
fi

TFM_INV="$TFM_INV_TFM"
if [[ ! -f "$TFM_INV" ]]; then
  echo "Error: inverse transform not found. Expected $TFM_INV_TFM"
  exit 1
fi

NII_FA_REG="$OUTPUT_DIR/${SUB_ID}-dti-FractionalAnisotropy-Reg.nii.gz"
NII_TRACE_REG="$OUTPUT_DIR/${SUB_ID}-dti-Trace-Reg.nii.gz"
NII_MINEIG_REG="$OUTPUT_DIR/${SUB_ID}-dti-MinEigenvalue-Reg.nii.gz"
NII_MIDEIG_REG="$OUTPUT_DIR/${SUB_ID}-dti-MidEigenvalue-Reg.nii.gz"
NII_MASK_REG="$OUTPUT_DIR/${SUB_ID}-mask-Reg.nii.gz"
if [[ ! -f "$NII_MASK_REG" ]]; then
  $ResampleScalarVectorDWIVolume -i linear "$NII_FA" --Reference "$ATLAS_T2" --transformationFile "$TFM" "$NII_FA_REG"
  $ResampleScalarVectorDWIVolume -i linear "$NII_TRACE" --Reference "$ATLAS_T2" --transformationFile "$TFM" "$NII_TRACE_REG"
  $ResampleScalarVectorDWIVolume -i linear "$NII_MINEIG" --Reference "$ATLAS_T2" --transformationFile "$TFM" "$NII_MINEIG_REG"
  $ResampleScalarVectorDWIVolume -i linear "$NII_MIDEIG" --Reference "$ATLAS_T2" --transformationFile "$TFM" "$NII_MIDEIG_REG"
  $ResampleScalarVectorDWIVolume -i nn "$MASK_NII" --Reference "$ATLAS_T2" --transformationFile "$TFM" "$NII_MASK_REG"
fi

NII_FA_REG_NORM="$OUTPUT_DIR/${SUB_ID}-dti-FractionalAnisotropy-Reg-NormMasked.nii.gz"
NII_TRACE_REG_NORM="$OUTPUT_DIR/${SUB_ID}-dti-Trace-Reg-NormMasked.nii.gz"
NII_MINEIG_REG_NORM="$OUTPUT_DIR/${SUB_ID}-dti-MinEigenvalue-Reg-NormMasked.nii.gz"
NII_MIDEIG_REG_NORM="$OUTPUT_DIR/${SUB_ID}-dti-MidEigenvalue-Reg-NormMasked.nii.gz"

if [[ ! -f "$NII_MIDEIG_REG_NORM" ]]; then
  python /opt/DDParcel/normalize.py --input "$NII_FA_REG" --mask "$NII_MASK_REG" --output "$NII_FA_REG_NORM" --flip 1
  python /opt/DDParcel/normalize.py --input "$NII_TRACE_REG" --mask "$NII_MASK_REG" --output "$NII_TRACE_REG_NORM" --flip 1
  python /opt/DDParcel/normalize.py --input "$NII_MINEIG_REG" --mask "$NII_MASK_REG" --output "$NII_MINEIG_REG_NORM" --flip 1
  python /opt/DDParcel/normalize.py --input "$NII_MIDEIG_REG" --mask "$NII_MASK_REG" --output "$NII_MIDEIG_REG_NORM" --flip 1
fi

MGZ_WMPARC_REG="$OUTPUT_DIR/${SUB_ID}-DDSurfer-wmparc-Reg.mgz"
if [[ ! -f "$MGZ_WMPARC_REG" ]]; then
  python /opt/DDParcel/DDSurfer_Pred.py --in_dir "$OUTPUT_DIR" --out_dir "$OUTPUT_DIR" --weights_dir /opt/DDParcel/weights
fi
if [[ ! -f "$MGZ_WMPARC_REG" ]]; then
  DDSURFER_FALLBACK="$OUTPUT_DIR/$(basename "$OUTPUT_DIR")-DDSurfer-wmparc-Reg.mgz"
  if [[ -f "$DDSURFER_FALLBACK" ]]; then
    cp "$DDSURFER_FALLBACK" "$MGZ_WMPARC_REG"
  fi
fi

NII_WMPARC="$OUTPUT_DIR/${SUB_ID}-DDSurfer-wmparc.nii.gz"
if [[ ! -f "$NII_WMPARC" ]]; then
  $ResampleScalarVectorDWIVolume --Reference "$MASK_NII" --transformationFile "$TFM_INV" --interpolation nn \
    "$MGZ_WMPARC_REG" "$NII_WMPARC"
fi

echo "[OK] DDParcel outputs in: $OUTPUT_DIR"
echo "[OK] Parcellation: $NII_WMPARC"
