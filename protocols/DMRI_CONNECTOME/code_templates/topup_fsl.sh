#!/usr/bin/env bash
set -euo pipefail

# FSL topup template
# Preconditions:
# - paired reverse PE series exist (e.g., dir-AP and dir-PA)
# - each JSON has PhaseEncodingDirection + TotalReadoutTime

export FSLDIR=/opt/fsl
# shellcheck disable=SC1091
source "$FSLDIR/etc/fslconf/fsl.sh"

usage() {
  cat <<'EOF'
Usage:
  bash topup_fsl.sh <ap_dwi> <ap_json> <pa_dwi> <pa_json> [outdir]

Environment overrides:
  AP_DWI, AP_JSON, PA_DWI, PA_JSON, OUTDIR, DRY_RUN
EOF
}

AP_DWI="${AP_DWI:-${1:-}}"
AP_JSON="${AP_JSON:-${2:-}}"
PA_DWI="${PA_DWI:-${3:-}}"
PA_JSON="${PA_JSON:-${4:-}}"
OUTDIR="${OUTDIR:-${5:-topup}}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$AP_DWI" || -z "$AP_JSON" || -z "$PA_DWI" || -z "$PA_JSON" ]]; then
  usage >&2
  exit 1
fi

for f in "$AP_DWI" "$AP_JSON" "$PA_DWI" "$PA_JSON"; do
  [[ -f "$f" ]] || { echo "[ERROR] Missing file: $f"; exit 1; }
done

mkdir -p "$OUTDIR"

run_cmd() {
  echo "[CMD] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@"
}

run_cmd fslroi "$AP_DWI" "$OUTDIR/b0_AP" 0 1
run_cmd fslroi "$PA_DWI" "$OUTDIR/b0_PA" 0 1
run_cmd fslmerge -t "$OUTDIR/b0_AP_PA" "$OUTDIR/b0_AP" "$OUTDIR/b0_PA"

run_cmd python3 "$(dirname "$0")/make_acqp_from_bids_json.py" \
  --json "$AP_JSON" "$PA_JSON" \
  --out "$OUTDIR/acqp.txt"

run_cmd topup \
  --imain="$OUTDIR/b0_AP_PA" \
  --datain="$OUTDIR/acqp.txt" \
  --config="$FSLDIR/etc/flirtsch/b02b0.cnf" \
  --out="$OUTDIR/topup_results" \
  --iout="$OUTDIR/topup_corrected_b0"

echo "[OK] topup finished: $OUTDIR"
