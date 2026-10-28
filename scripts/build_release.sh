#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/dist"
ALLOW_DIRTY=0

usage() {
  cat <<'EOF'
Usage: scripts/build_release.sh [options]

Options:
  --output <dir>  Release output directory. Default: ./dist
  --allow-dirty   Allow tracked release files to differ from HEAD.
  -h, --help      Show this help message.
EOF
}

fail() {
  printf '[release] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      [[ $# -ge 2 && -n "${2:-}" ]] || fail "Missing value for --output"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
done

cd "$ROOT_DIR"
command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "Python 3 is required"
[[ -f VERSION ]] || fail "VERSION is missing"

VERSION="$(tr -d '[:space:]' < VERSION)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || \
  fail "VERSION is not semantic: $VERSION"
PACKAGE_NAME="dmri-agent-$VERSION"

INCLUDE_PATHS=(
  LICENSE
  THIRD_PARTY_NOTICES.md
  README.md
  VERSION
  backend
  config
  install.sh
  install.ps1
  uninstall.sh
  uninstall.ps1
  protocols
  slicer_extension
  scripts/launchers
  scripts/native_install.py
  scripts/installer_support.py
  scripts/runtime_bootstrap.sh
  scripts/slicer_registration.py
  scripts/slicer_rc_registration.py
  scripts/validate_protocol_package.py
)

if [[ "$ALLOW_DIRTY" == "0" ]]; then
  git diff --quiet -- "${INCLUDE_PATHS[@]}" || \
    fail "Tracked release files differ from HEAD; commit them or use --allow-dirty"
  git diff --cached --quiet -- "${INCLUDE_PATHS[@]}" || \
    fail "Staged release files differ from HEAD; commit them or use --allow-dirty"
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dmri-agent-release.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
STAGE_ROOT="$TMP_DIR/$PACKAGE_NAME"
mkdir -p "$STAGE_ROOT" "$OUTPUT_DIR"

is_excluded() {
  case "$1" in
    .gitattributes|.gitignore|pyproject.toml|backend/requirements.in)
      return 0
      ;;
    benchmark/*|docker/slicer/*|.env|web/.env.local|*/node_modules/*|*/.next/*|*/dist/*|*__pycache__/*|*.pyc|*.pyo|*.tsbuildinfo|.dmri-agent-install)
      return 0
      ;;
  esac
  return 1
}

FILE_COUNT=0
while IFS= read -r -d '' source_path; do
  is_excluded "$source_path" && continue
  [[ -f "$source_path" ]] || continue
  [[ ! -L "$source_path" ]] || fail "Symlinks are not allowed in releases: $source_path"
  mkdir -p "$STAGE_ROOT/$(dirname "$source_path")"
  cp -p "$source_path" "$STAGE_ROOT/$source_path"
  FILE_COUNT=$((FILE_COUNT + 1))
done < <(git ls-files -z -- "${INCLUDE_PATHS[@]}")

# Validate the same runtime contract as both native installers. Developer tests
# and research records stay in the repository, outside the user distribution.
python3 - "$ROOT_DIR" "$STAGE_ROOT" <<'CHECK'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts.installer_support import MANDATORY_COPY_PATHS
stage = Path(sys.argv[2])
required = [Path(p) for p in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "VERSION")]
for relative in [*required, *MANDATORY_COPY_PATHS]:
    path = stage / relative
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"Required release file is missing or unsafe: {relative}")
CHECK

[[ "$FILE_COUNT" -gt 100 ]] || fail "Release file set is unexpectedly small: $FILE_COUNT"

if find "$STAGE_ROOT" -type f \( \
  -name '.env' -o -name '*.pem' -o -name '*.key' -o -name '*.p12' -o \
  -name '*.dcm' -o -name '*.nii' -o -name '*.nii.gz' -o -name '*.nrrd' -o \
  -name '*.nhdr' -o -name '*.vtk' -o -name '*.vtp' -o -name '*.tck' -o \
  -name '*.trk' -o -name '*.pt' -o -name '*.pth' -o -name '*.ckpt' \
\) -print -quit | grep -q .; then
  fail "Release contains a forbidden credential, medical-data, or model file"
fi

if grep -RIE \
  '(ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)' \
  "$STAGE_ROOT" >/dev/null 2>&1; then
  fail "Release contains a likely credential"
fi

SOURCE_REVISION="$(git rev-parse HEAD)"
SOURCE_EPOCH="$(git show -s --format=%ct HEAD)"

python3 - "$STAGE_ROOT" "$VERSION" "$SOURCE_REVISION" "$SOURCE_EPOCH" \
  "$OUTPUT_DIR" "$PACKAGE_NAME" "$ALLOW_DIRTY" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import zipfile

stage = Path(sys.argv[1])
version = sys.argv[2]
revision = sys.argv[3]
epoch = int(sys.argv[4])
output = Path(sys.argv[5]).resolve()
package_name = sys.argv[6]

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

files = []
for path in sorted(p for p in stage.rglob("*") if p.is_file()):
    rel = path.relative_to(stage).as_posix()
    files.append({"path": rel, "size": path.stat().st_size, "sha256": digest(path)})

manifest = {
    "schema_version": 1,
    "name": "dMRI-Agent",
    "version": version,
    "source_revision": revision,
    "source_date_epoch": epoch,
    "build_mode": "working_tree_candidate" if sys.argv[7] == "1" else "committed_release",
    "distribution": "unified",
    "frontends": ["slicer"],
    "install_commands": {
        "macos": "bash ./install.sh",
        "linux": "bash ./install.sh",
        "windows": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1",
    },
    "file_count": len(files),
    "files": files,
}
(stage / "RELEASE_MANIFEST.json").write_text(
    json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
)

archive_paths = sorted([stage, *stage.rglob("*")], key=lambda item: item.as_posix())
zip_epoch = max(epoch, 315532800)
zip_time = time.gmtime(zip_epoch)[:6]
zip_path = output / f"{package_name}.zip"
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in archive_paths:
        arcname = path.relative_to(stage.parent).as_posix()
        if path.is_dir():
            arcname += "/"
            payload = b""
            mode = stat.S_IFDIR | 0o755
        else:
            payload = path.read_bytes()
            mode = stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)
        info = zipfile.ZipInfo(arcname, date_time=zip_time)
        info.create_system = 3
        info.external_attr = mode << 16
        archive.writestr(info, payload)

checksums = output / "SHA256SUMS"
checksums.write_text(
    "".join(f"{digest(path)}  {path.name}\n" for path in (zip_path,)),
    encoding="ascii",
)
PY

printf '[release] Built %s from %s tracked files\n' "$PACKAGE_NAME" "$FILE_COUNT"
printf '[release] Output: %s\n' "$(cd "$OUTPUT_DIR" && pwd)"
cat "$OUTPUT_DIR/SHA256SUMS"
