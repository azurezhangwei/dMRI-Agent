#!/usr/bin/env bash
set -euo pipefail

PURGE=0
YES=0

usage() {
  cat <<'EOF'
Usage: dmri-agent-uninstall [options]

Options:
  --purge     Also remove user configuration, memory, managed Protocols, and logs.
  --yes       Do not ask for confirmation when --purge is used.
  -h, --help  Show this help message.

An in-place source checkout is never deleted. A managed checkout is deleted only
when it contains the installer marker created by install.sh.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

SCRIPT_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT_PATH" ]]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
  LINK_TARGET="$(readlink "$SCRIPT_PATH")"
  if [[ "$LINK_TARGET" == /* ]]; then
    SCRIPT_PATH="$LINK_TARGET"
  else
    SCRIPT_PATH="$SCRIPT_DIR/$LINK_TARGET"
  fi
done
APP_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
MARKER="$APP_DIR/.dmri-agent-install"
[[ -f "$MARKER" ]] || {
  printf 'Installation marker not found: %s\n' "$MARKER" >&2
  exit 1
}

read_marker() {
  local key="$1"
  sed -n "s/^${key}=//p" "$MARKER" | head -n 1
}

MODE="$(read_marker mode)"
RECORDED_APP_DIR="$(read_marker app_dir)"
BIN_DIR="$(read_marker bin_dir)"
[[ -n "$BIN_DIR" ]] || BIN_DIR="$HOME/.local/bin"
RECORDED_STATE_HOME="$(read_marker state_home)"
RECORDED_CONFIG_HOME="$(read_marker config_home)"
RECORDED_PROTOCOLS_HOME="$(read_marker protocols_home)"
RECORDED_WORKSPACE="$(read_marker workspace)"
RECORDED_INSTALL_JSON="$(read_marker install_json)"
RECORDED_SLICER_REGISTRY="$(read_marker slicer_registry)"
RECORDED_SLICER_RC="$(read_marker slicer_rc)"
SLICER_REGISTERED="$(read_marker slicer_registered)"
case "$SLICER_REGISTERED" in
  0|1) ;;
  *)
    printf 'Install marker has invalid or missing Slicer registration state.\n' >&2
    printf 'Refusing to remove the application while a Slicer startup hook may remain.\n' >&2
    exit 1
    ;;
esac
RECORDED_APP_DIR="$(cd "$RECORDED_APP_DIR" 2>/dev/null && pwd -P)" || {
  printf 'Recorded install path no longer exists: %s\n' "$RECORDED_APP_DIR" >&2
  exit 1
}
[[ "$RECORDED_APP_DIR" == "$APP_DIR" ]] || {
  printf 'Install marker path mismatch: %s != %s\n' "$RECORDED_APP_DIR" "$APP_DIR" >&2
  exit 1
}
if [[ "$MODE" == "managed" && \
      ( -z "$APP_DIR" || "$APP_DIR" == "/" || "$APP_DIR" == "$HOME" || -L "$APP_DIR" ) ]]; then
  printf 'Refusing to uninstall unsafe managed path: %s\n' "$APP_DIR" >&2
  exit 1
fi

remove_link() {
  local target="$1"
  if [[ -L "$target" ]]; then
    local resolved
    resolved="$(readlink "$target")"
    if [[ "$resolved" != /* ]]; then
      resolved="$(dirname "$target")/$resolved"
    fi
    resolved="$(cd "$(dirname "$resolved")" 2>/dev/null && pwd -P)/$(basename "$resolved")"
    case "$resolved" in
      "$APP_DIR"/*) rm "$target" ;;
    esac
  fi
}

CONFIG_HOME="${RECORDED_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/dmri-agent}"
INSTALL_JSON="${RECORDED_INSTALL_JSON:-$CONFIG_HOME/install.json}"
SLICER_REGISTRY="${RECORDED_SLICER_REGISTRY:-$CONFIG_HOME/slicer-startup-registration.json}"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ "$MODE" == "managed" && -z "$PYTHON_BIN" ]]; then
  printf 'Python is required to validate the managed layout before deletion.\n' >&2
  exit 1
fi
CURRENT_WORKSPACE="$RECORDED_WORKSPACE"
if [[ -n "$PYTHON_BIN" ]]; then
  CURRENT_WORKSPACE="$("$PYTHON_BIN" - "$APP_DIR" "$MARKER" "$INSTALL_JSON" <<'PY'
import json
import os
from pathlib import Path
import sys

app_value, marker_value, metadata_value = sys.argv[1:]
app = os.path.realpath(app_value)


def validate(label: str, raw: str) -> None:
    if not raw:
        return
    if not os.path.isabs(raw):
        raise SystemExit(f"Protected {label} path must be absolute")
    candidate = os.path.realpath(os.path.expanduser(raw))
    try:
        common = os.path.commonpath((app, candidate))
    except ValueError:
        return
    if common == app:
        raise SystemExit(
            f"Protected {label} path must be outside the application directory: {candidate}"
        )


values = {}
for line in Path(marker_value).read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
for key in (
    "bin_dir",
    "config_home",
    "state_home",
    "protocols_home",
    "workspace",
    "install_json",
    "slicer_registry",
    "slicer_rc",
):
    validate(f"recorded {key}", values.get(key, ""))

metadata = Path(metadata_value)
workspace = ""
if metadata.is_file():
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Existing install metadata is invalid: {exc}")
    workspace = payload.get("workspace")
    if workspace is not None and not isinstance(workspace, str):
        raise SystemExit("Existing install metadata has an invalid workspace")
    validate("metadata workspace", workspace or "")
print(workspace or values.get("workspace", ""))
PY
  )"
fi
if [[ "$PURGE" == "1" && "$YES" != "1" ]]; then
  printf 'Purge dMRI-Agent configuration, memory, managed Protocols, and state? [y/N] '
  if ! read -r answer; then answer=""; fi
  case "$answer" in y|Y|yes|YES) ;; *) printf 'Purge cancelled.\n'; PURGE=0 ;; esac
fi

STATE_HOME="${RECORDED_STATE_HOME:-${DMRI_STATE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/dmri-agent}}"
PROTOCOLS_HOME="${RECORDED_PROTOCOLS_HOME:-${DMRI_PROTOCOLS_HOME:-$STATE_HOME/protocols}}"

if [[ -n "$PYTHON_BIN" && -f "$APP_DIR/scripts/installer_support.py" ]]; then
  "$PYTHON_BIN" "$APP_DIR/scripts/installer_support.py" assert-idle --state-home "$STATE_HOME"
elif [[ -f "$STATE_HOME/runtime/service.json" ]]; then
  printf 'Cannot validate the backend state. Close Slicer and restore Python before uninstalling.\n' >&2
  exit 1
fi

if [[ "$PURGE" == "1" ]]; then
  if [[ -z "$PYTHON_BIN" || ! -f "$APP_DIR/scripts/installer_support.py" ]]; then
    printf 'Python and installer support are required to validate purge paths.\n' >&2
    exit 1
  fi
  purge_args=(validate-purge
    --target "$CONFIG_HOME" --target "$STATE_HOME"
    --target "$PROTOCOLS_HOME" --target "$APP_DIR")
  for workspace in "$RECORDED_WORKSPACE" "$CURRENT_WORKSPACE"; do
    if [[ -n "$workspace" ]]; then
      purge_args+=(--workspace "$workspace")
    fi
  done
  "$PYTHON_BIN" "$APP_DIR/scripts/installer_support.py" "${purge_args[@]}"
fi

if [[ "$SLICER_REGISTERED" == "1" && \
      ! -f "$APP_DIR/scripts/slicer_rc_registration.py" ]]; then
  printf 'Slicer registration manager is missing from the installation.\n' >&2
  exit 1
fi
if [[ "$SLICER_REGISTERED" == "1" && -z "$PYTHON_BIN" ]]; then
  printf 'Python is required to remove the installed Slicer registration.\n' >&2
  exit 1
fi
if [[ "$SLICER_REGISTERED" == "1" && ! -f "$SLICER_REGISTRY" && \
      -z "$RECORDED_SLICER_RC" ]]; then
  printf 'Slicer registration record is missing and the install marker has no recovery path.\n' >&2
  printf 'Refusing to remove the application while its Slicer startup hook may remain.\n' >&2
  exit 1
fi

if [[ "$SLICER_REGISTERED" == "1" && -f "$SLICER_REGISTRY" ]]; then
  [[ -n "$PYTHON_BIN" ]] || {
    printf 'Python is required to remove the installed Slicer registration.\n' >&2
    exit 1
  }
  "$PYTHON_BIN" "$APP_DIR/scripts/slicer_rc_registration.py" unregister-record \
    --registry "$SLICER_REGISTRY" --owner "$APP_DIR"
  rm -f "$SLICER_REGISTRY"
elif [[ "$SLICER_REGISTERED" == "1" && -n "$RECORDED_SLICER_RC" && \
       -f "$APP_DIR/scripts/slicer_rc_registration.py" && -n "$PYTHON_BIN" ]]; then
  # A previous interrupted uninstall may already have removed the private
  # registry.  Removing the recorded, matching owner block is idempotent and
  # makes the operation safely retryable.
  "$PYTHON_BIN" "$APP_DIR/scripts/slicer_rc_registration.py" unregister \
    --rc "$RECORDED_SLICER_RC" --owner "$APP_DIR"
fi
if [[ -n "$PYTHON_BIN" && -f "$APP_DIR/scripts/installer_support.py" ]]; then
  "$PYTHON_BIN" "$APP_DIR/scripts/installer_support.py" remove-metadata \
    --path "$INSTALL_JSON" --app-dir "$APP_DIR"
fi

remove_link "$BIN_DIR/dmri-agent"
remove_link "$BIN_DIR/dmri-agent-service"
remove_link "$BIN_DIR/dmri-agent-web"
remove_link "$BIN_DIR/dmri-agent-slicer"
remove_link "$BIN_DIR/dmri-agent-check"
remove_link "$BIN_DIR/dmri-agent-doctor"
remove_link "$BIN_DIR/dmri-agent-uninstall"

if [[ "$MODE" == "managed" ]]; then
  rm -rf "$APP_DIR"
else
  rm -f "$MARKER"
  printf 'Kept in-place source checkout: %s\n' "$APP_DIR"
fi

if [[ "$PURGE" == "1" ]]; then
  rm -rf "$CONFIG_HOME"
  if [[ "$STATE_HOME" != "$APP_DIR" ]]; then
    rm -rf "$STATE_HOME"
  fi
  if [[ "$PROTOCOLS_HOME" != "$STATE_HOME" && "$PROTOCOLS_HOME" != "$APP_DIR" ]]; then
    rm -rf "$PROTOCOLS_HOME"
  fi
fi

printf 'dMRI-Agent command links were removed.\n'
if [[ "$PURGE" == "1" && -n "$CURRENT_WORKSPACE" ]]; then
  printf 'User workspace was preserved: %s\n' "$CURRENT_WORKSPACE"
fi
