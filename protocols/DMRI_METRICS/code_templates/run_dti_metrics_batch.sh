#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_dti_metrics_batch.sh \
    --dataset-root <dir> \
    --processed-root <dir> \
    --qc-root <dir> \
    [--max-jobs 6]
USAGE
}

DATASET_ROOT=""
PROCESSED_ROOT=""
QC_ROOT=""
MAX_JOBS=6

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset-root) DATASET_ROOT="$2"; shift 2 ;;
    --processed-root) PROCESSED_ROOT="$2"; shift 2 ;;
    --qc-root) QC_ROOT="$2"; shift 2 ;;
    --max-jobs) MAX_JOBS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -n "$DATASET_ROOT" && -n "$PROCESSED_ROOT" && -n "$QC_ROOT" ]] || {
  usage >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_SCRIPT="${SCRIPT_DIR}/check_dti_inputs.py"
SUBJECT_SCRIPT="${SCRIPT_DIR}/compute_dti_metrics_subject.sh"
QC_SCRIPT="${SCRIPT_DIR}/qc_dti_metrics.py"

mkdir -p "$PROCESSED_ROOT" "$QC_ROOT"
INPUT_QC="${QC_ROOT}/input_qc.tsv"
PROCESSING_UNITS="${QC_ROOT}/processing_units.tsv"
RUN_LOG_DIR="${QC_ROOT}/logs"
mkdir -p "$RUN_LOG_DIR"

python3 "$CHECK_SCRIPT" --dataset-root "$DATASET_ROOT" --out-tsv "$INPUT_QC"
cp -f "$INPUT_QC" "$PROCESSING_UNITS"

tail -n +2 "$INPUT_QC" | awk -F'\t' '$2=="ok"{print $1"\t"$4"\t"$5"\t"$6"\t"$7}' \
  | xargs -r -P "$MAX_JOBS" -n 5 bash -c '
      set -euo pipefail
      subject="$1"; dwi="$2"; bval="$3"; bvec="$4"; mask="$5"
      out_dir="'"$PROCESSED_ROOT"'/${subject}/dti_metrics"
      if ! bash "'"$SUBJECT_SCRIPT"'" \
        --subject "$subject" \
        --dwi "$dwi" \
        --bval "$bval" \
        --bvec "$bvec" \
        --mask "$mask" \
        --out-dir "$out_dir" \
        >"'"$RUN_LOG_DIR"'/${subject}.stdout.log" 2>"'"$RUN_LOG_DIR"'/${subject}.stderr.log"; then
          echo "${subject}" >>"'"$QC_ROOT"'/failed_subjects.txt"
      fi
    ' _

python3 "$QC_SCRIPT" \
  --input-qc "$INPUT_QC" \
  --processed-root "$PROCESSED_ROOT" \
  --out-tsv "${QC_ROOT}/dti_metrics_qc.tsv"

{
  echo "# DTI Metrics Summary"
  echo
  echo "- dataset_root: ${DATASET_ROOT}"
  echo "- processed_root: ${PROCESSED_ROOT}"
  echo "- qc_root: ${QC_ROOT}"
  echo "- max_jobs: ${MAX_JOBS}"
  echo
  echo "## Input QC"
  awk -F'\t' 'NR>1{count[$2]++} END{for (k in count) print "- " k ": " count[k]}' "$INPUT_QC"
  echo
  echo "## Metrics QC"
  awk -F'\t' 'NR==1{for(i=1;i<=NF;i++) if($i=="status") s=i} NR>1{count[$s]++} END{for (k in count) print "- " k ": " count[k]}' "${QC_ROOT}/dti_metrics_qc.tsv"
} >"${QC_ROOT}/summary.md"
