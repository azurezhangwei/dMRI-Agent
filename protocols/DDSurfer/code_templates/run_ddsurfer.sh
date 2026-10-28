#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  run_ddsurfer.sh --ddsurfer-root <repository> [--python <interpreter>]
    [--stage pipeline|postprocess] -- <DDSurfer arguments>

Environment defaults: DDSURFER_ROOT, PYTHON_BIN (otherwise python3).
Backend arguments are passed unchanged. Use '-- --help' for backend help.
Example:
  run_ddsurfer.sh --ddsurfer-root /opt/DDSurfer -- \
    --subject sub-001 --raw-input-root /data --output-root /outputs --device cpu
USAGE
}

ddsurfer_root=${DDSURFER_ROOT:-}
python_bin=${PYTHON_BIN:-python3}
stage=pipeline

while (($#)); do
  case "$1" in
    --ddsurfer-root|--python|--stage)
      if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
        printf 'Missing value for %s\n' "$1" >&2
        exit 2
      fi
      case "$1" in
        --ddsurfer-root) ddsurfer_root=$2 ;;
        --python) python_bin=$2 ;;
        --stage) stage=$2 ;;
      esac
      shift 2
      ;;
    --) shift; break ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown wrapper argument: %s (use -- before backend arguments)\n' "$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$ddsurfer_root" || $# -eq 0 ]]; then
  usage >&2
  exit 2
fi
if [[ ! -f "$ddsurfer_root/run_ddsurfer_pipeline.py" ]]; then
  printf 'DDSurfer source not available in this runtime: %s\n' "$ddsurfer_root" >&2
  exit 2
fi
ddsurfer_root=$(cd "$ddsurfer_root" && pwd)
case "$stage" in
  pipeline) entrypoint="$ddsurfer_root/run_ddsurfer_pipeline.py" ;;
  postprocess) entrypoint="$ddsurfer_root/postprocessing/pipeline.py" ;;
  *) printf 'Unsupported stage: %s\n' "$stage" >&2; exit 2 ;;
esac
if [[ ! -f "$entrypoint" ]]; then
  printf 'DDSurfer entry point missing: %s\n' "$entrypoint" >&2
  exit 2
fi
if ! command -v "$python_bin" >/dev/null 2>&1; then
  printf 'Python interpreter not available: %s\n' "$python_bin" >&2
  exit 2
fi

export PYTHON_BIN="$python_bin"
exec "$python_bin" "$entrypoint" "$@"
