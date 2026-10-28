#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_URL="${DMRI_AGENT_REPOSITORY:-https://github.com/azurezhangwei/dMRI-Agent.git}"
MACOS_DEFAULT_INSTALL=0
if [[ -n "${DMRI_AGENT_INSTALL_DIR:-}" ]]; then
  INSTALL_DIR="$DMRI_AGENT_INSTALL_DIR"
elif [[ -n "${XDG_DATA_HOME:-}" ]]; then
  INSTALL_DIR="$XDG_DATA_HOME/dmri-agent/app"
elif [[ "$(uname -s 2>/dev/null || true)" == "Darwin" ]]; then
  INSTALL_DIR="$HOME/Library/Application Support/dMRI Agent/app"
  MACOS_DEFAULT_INSTALL=1
else
  INSTALL_DIR="$HOME/.local/share/dmri-agent/app"
fi
BIN_DIR="${DMRI_AGENT_BIN_DIR:-$HOME/.local/bin}"
STATE_HOME="${DMRI_STATE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/dmri-agent}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/dmri-agent"
PROTOCOLS_HOME="${DMRI_PROTOCOLS_HOME:-$STATE_HOME/protocols}"
SOURCE_DIR=""
GIT_REF="${DMRI_AGENT_REF:-}"
RUNTIME_DOWNLOADS=1
UPDATE_PATH=1
DEVELOPER_IN_PLACE=0
REGISTER_SLICER=1
SLICER_PATHS=()
BACKEND_PORT="${DMRI_BACKEND_PORT:-8765}"
WORKSPACE=""
WORKSPACE_EXPLICIT=0
DEFAULT_WORKSPACE="${DMRI_AGENT_DEFAULT_WORKSPACE:-$HOME/Documents/dmri-agent-workspace}"
FRESH_MANAGED_INSTALL=0
APP_DIR=""
PYTHON_BIN=""
INSTALL_JSON=""
SLICER_REGISTRY=""
MARKER_PATH=""
SLICER_TRANSACTION=""
SLICER_TRANSACTION_TRACKED=0
SLICER_REGISTRY_BACKUP=""
SLICER_REGISTRY_EXISTED=0
SLICER_REGISTRY_STATE_KNOWN=0
METADATA_BACKUP=""
METADATA_EXISTED=0
METADATA_STATE_KNOWN=0
MARKER_BACKUP=""
MARKER_EXISTED=0
MARKER_STATE_KNOWN=0
ROLLBACK_FAILED=0
INSTALL_COMMITTED=0
INSTALL_STARTED=0
INSTALL_COMPLETED=0
INSTALL_LOCK_FILE=""
MANAGED_STAGE_OWNED=0
MANAGED_STAGE=""
UPGRADE_IN_PROGRESS=0
UPGRADE_JOURNAL=""
UPGRADE_HELPER=""
UPGRADE_BACKUP=""
BOOTSTRAP_DIR=""
BOOTSTRAP_DIR_OWNED=0
RUNTIME_BOOTSTRAP_TMP=""
RUNTIME_BOOTSTRAP_TMP_OWNED=0
RUNTIME_BOOTSTRAP_TMP_PARENT=""
INSTALL_TOKEN=""
NEW_LINKS=()
REMOVED_LINK_PATHS=()
REMOVED_LINK_TARGETS=()

cleanup_owned_temp_dir() {
  local path="$1"
  local expected_parent="$2"
  local basename_prefix="$3"
  local actual_parent resolved_parent leaf
  [[ -n "$path" && -n "$expected_parent" && -d "$path" && ! -L "$path" ]] || return 0
  leaf="$(basename "$path")"
  case "$leaf" in
    "$basename_prefix".*) ;;
    *) return 0 ;;
  esac
  actual_parent="$(cd -P "$(dirname "$path")" 2>/dev/null && pwd)" || return 0
  resolved_parent="$(cd -P "$expected_parent" 2>/dev/null && pwd)" || return 0
  [[ "$actual_parent" == "$resolved_parent" ]] || return 0
  rm -rf "$path"
}

cleanup() {
  local status="$?"
  # Bash 3.2 can report an expansion error from inside a compound command as
  # status 0 to an EXIT trap.  A successful install always reaches the final
  # completion sentinel, so never allow an early abort to be reported as a
  # successful one-command installation.
  if [[ "$status" == "0" && "${INSTALL_STARTED:-0}" == "1" && \
        "${INSTALL_COMPLETED:-0}" != "1" ]]; then
    status=1
    printf '[dMRI-Agent] ERROR: Installation stopped before completion.\n' >&2
  fi
  if [[ "$status" != "0" && "${INSTALL_COMMITTED:-0}" == "1" ]]; then
    printf '[dMRI-Agent] ERROR: Installation was committed, but finalization did not complete.\n' >&2
    printf '[dMRI-Agent] Re-run the same install command to finish recovery; installed files were preserved.\n' >&2
  elif [[ "$status" != "0" ]]; then
    if [[ "${SLICER_TRANSACTION_TRACKED:-0}" == "1" && \
          -n "${SLICER_TRANSACTION:-}" && -f "$SLICER_TRANSACTION" && \
          -n "${APP_DIR:-}" && -f "$APP_DIR/scripts/slicer_rc_registration.py" ]]; then
      if ! "$PYTHON_BIN" "$APP_DIR/scripts/slicer_rc_registration.py" rollback \
        --journal "$SLICER_TRANSACTION" >/dev/null 2>&1; then
        ROLLBACK_FAILED=1
        printf '[dMRI-Agent] ERROR: Slicer startup rollback was blocked; preserving the installed app and recovery records.\n' >&2
        printf '[dMRI-Agent] Recovery journal: %s\n' "$SLICER_TRANSACTION" >&2
      fi
    fi
    if [[ "$ROLLBACK_FAILED" == "0" && "${METADATA_STATE_KNOWN:-0}" == "1" && \
          -n "${INSTALL_JSON:-}" ]]; then
      if [[ "${METADATA_EXISTED:-0}" == "1" && -n "${METADATA_BACKUP:-}" && \
            -f "$METADATA_BACKUP" ]]; then
        cp -p "$METADATA_BACKUP" "$INSTALL_JSON" >/dev/null 2>&1 || true
      elif [[ "${METADATA_EXISTED:-0}" == "0" ]]; then
        "$PYTHON_BIN" "${APP_DIR:-}/scripts/installer_support.py" remove-metadata \
          --path "$INSTALL_JSON" --app-dir "${APP_DIR:-}" >/dev/null 2>&1 || true
      fi
    fi
    if [[ "$ROLLBACK_FAILED" == "0" && "${SLICER_REGISTRY_STATE_KNOWN:-0}" == "1" && \
          -n "${SLICER_REGISTRY:-}" ]]; then
      rm -f "$SLICER_REGISTRY.tmp"
      if [[ "${SLICER_REGISTRY_EXISTED:-0}" == "1" && \
            -n "${SLICER_REGISTRY_BACKUP:-}" && -f "$SLICER_REGISTRY_BACKUP" ]]; then
        cp -p "$SLICER_REGISTRY_BACKUP" "$SLICER_REGISTRY" >/dev/null 2>&1 || true
      elif [[ "${SLICER_REGISTRY_EXISTED:-0}" == "0" ]]; then
        rm -f "$SLICER_REGISTRY"
      fi
    fi
    if [[ "$ROLLBACK_FAILED" == "0" && "${MARKER_STATE_KNOWN:-0}" == "1" && \
          "${FRESH_MANAGED_INSTALL:-0}" != "1" && \
          "${UPGRADE_IN_PROGRESS:-0}" != "1" && -n "${MARKER_PATH:-}" ]]; then
      if [[ "${MARKER_EXISTED:-0}" == "1" && -n "${MARKER_BACKUP:-}" && \
            -f "$MARKER_BACKUP" ]]; then
        cp -p "$MARKER_BACKUP" "$MARKER_PATH" >/dev/null 2>&1 || true
      elif [[ "${MARKER_EXISTED:-0}" == "0" ]]; then
        rm -f "$MARKER_PATH"
      fi
    fi
    if [[ "$ROLLBACK_FAILED" == "0" ]]; then
      for new_link in "${NEW_LINKS[@]:-}"; do
        if [[ -n "$new_link" && -L "$new_link" && \
              "$(readlink "$new_link")" == "${APP_DIR:-}"/* ]]; then
          rm -f "$new_link"
        fi
      done
      local link_index
      for ((link_index=0; link_index<${#REMOVED_LINK_PATHS[@]}; link_index++)); do
        if [[ ! -e "${REMOVED_LINK_PATHS[$link_index]}" && \
              ! -L "${REMOVED_LINK_PATHS[$link_index]}" ]]; then
          ln -s "${REMOVED_LINK_TARGETS[$link_index]}" \
            "${REMOVED_LINK_PATHS[$link_index]}" >/dev/null 2>&1 || true
        fi
      done
    fi
    if [[ "$ROLLBACK_FAILED" == "0" && "${UPGRADE_IN_PROGRESS:-0}" == "1" && \
          -n "${UPGRADE_JOURNAL:-}" && -f "$UPGRADE_JOURNAL" && \
          -n "${UPGRADE_HELPER:-}" && -f "$UPGRADE_HELPER" ]]; then
      if ! "$PYTHON_BIN" "$UPGRADE_HELPER" rollback-upgrade \
        --journal "$UPGRADE_JOURNAL" >/dev/null 2>&1; then
        ROLLBACK_FAILED=1
        printf '[dMRI-Agent] ERROR: Managed upgrade rollback was blocked; preserving both versions and the recovery journal.\n' >&2
        printf '[dMRI-Agent] Recovery journal: %s\n' "$UPGRADE_JOURNAL" >&2
      else
        UPGRADE_IN_PROGRESS=0
      fi
    fi
    if [[ "$ROLLBACK_FAILED" == "0" && "${FRESH_MANAGED_INSTALL:-0}" == "1" && -n "${APP_DIR:-}" && \
          ! -L "$APP_DIR" && -f "$APP_DIR/.dmri-agent-install-pending" && \
          "$(< "$APP_DIR/.dmri-agent-install-pending")" == "${INSTALL_TOKEN:-}" ]]; then
      rm -rf "$APP_DIR"
    fi
  fi
  if [[ "${BOOTSTRAP_DIR_OWNED:-0}" == "1" ]]; then
    cleanup_owned_temp_dir "$BOOTSTRAP_DIR" "${TMPDIR:-/tmp}" "dmri-agent-bootstrap"
    BOOTSTRAP_DIR=""
    BOOTSTRAP_DIR_OWNED=0
  fi
  if [[ "${RUNTIME_BOOTSTRAP_TMP_OWNED:-0}" == "1" ]]; then
    case "$(basename "${RUNTIME_BOOTSTRAP_TMP:-}")" in
      .uv-bootstrap.*)
        cleanup_owned_temp_dir "$RUNTIME_BOOTSTRAP_TMP" "$RUNTIME_BOOTSTRAP_TMP_PARENT" ".uv-bootstrap"
        ;;
    esac
    RUNTIME_BOOTSTRAP_TMP=""
    RUNTIME_BOOTSTRAP_TMP_OWNED=0
    RUNTIME_BOOTSTRAP_TMP_PARENT=""
  fi
  if [[ "${MANAGED_STAGE_OWNED:-0}" == "1" && -n "${MANAGED_STAGE:-}" && \
        -n "${APP_DIR:-}" ]]; then
    cleanup_owned_temp_dir "$MANAGED_STAGE" "$(dirname "$APP_DIR")" ".dmri-agent-stage"
    MANAGED_STAGE=""
    MANAGED_STAGE_OWNED=0
  fi
  if [[ -n "${METADATA_BACKUP:-}" ]]; then rm -f "$METADATA_BACKUP"; fi
  if [[ -n "${MARKER_BACKUP:-}" ]]; then rm -f "$MARKER_BACKUP"; fi
  if [[ -n "${SLICER_REGISTRY_BACKUP:-}" ]]; then rm -f "$SLICER_REGISTRY_BACKUP"; fi
  return "$status"
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Install dMRI-Agent and its required runtimes into a managed user directory.
Python is prepared automatically when needed. No administrator access or
Homebrew installation is required. The only frontend is the 3D Slicer plugin.

Options:
  --source-dir <dir>   Copy an existing checkout or release into the managed directory.
  --developer-in-place Use the source directory in place instead of copying it.
  --install-dir <dir>  Managed application directory.
  --bin-dir <dir>      User command directory. Default: ~/.local/bin.
  --repository <url>   Git repository used by remote/bootstrap installation.
  --ref <branch/tag>   Optional branch or tag to clone.
  --slicer <path>      Register with this Slicer installation. May be repeated.
  --no-slicer-register Do not register the module with installed Slicer versions.
  --backend-port <port> Backend port written to install.json. Default: 8765.
  --workspace <dir>    Dedicated default workspace for Slicer and the backend.
  --slicer-only        Install the Slicer plugin and backend (default).
  --no-runtime-downloads
                       Require compatible runtimes to exist already; do not download them.
  --no-path-update     Do not add the command directory to the shell PATH.
  --platform-info     Print detected system/architecture without installing.
  -h, --help           Show this help message.

Examples:
  ./install.sh
  ./install.sh --slicer-only
  ./install.sh --source-dir /path/to/dMRI-Agent
  ./install.sh --source-dir /path/to/dMRI-Agent --developer-in-place
EOF
}

log() {
  printf '[dMRI-Agent] %s\n' "$*"
}

warn() {
  printf '[dMRI-Agent] WARNING: %s\n' "$*" >&2
}

fail() {
  printf '[dMRI-Agent] ERROR: %s\n' "$*" >&2
  exit 1
}

require_value() {
  if [[ $# -lt 2 || -z "${2:-}" ]]; then
    fail "Missing value for $1"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir)
      require_value "$@"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --developer-in-place)
      DEVELOPER_IN_PLACE=1
      shift
      ;;
    --install-dir)
      require_value "$@"
      INSTALL_DIR="$2"
      shift 2
      ;;
    --bin-dir)
      require_value "$@"
      BIN_DIR="$2"
      shift 2
      ;;
    --repository)
      require_value "$@"
      REPOSITORY_URL="$2"
      shift 2
      ;;
    --ref)
      require_value "$@"
      GIT_REF="$2"
      shift 2
      ;;
    --slicer)
      require_value "$@"
      SLICER_PATHS+=("$2")
      shift 2
      ;;
    --no-slicer-register)
      REGISTER_SLICER=0
      shift
      ;;
    --backend-port)
      require_value "$@"
      BACKEND_PORT="$2"
      shift 2
      ;;
    --workspace)
      require_value "$@"
      WORKSPACE="$2"
      WORKSPACE_EXPLICIT=1
      shift 2
      ;;
    --slicer-only)
      shift
      ;;
    --no-runtime-downloads)
      RUNTIME_DOWNLOADS=0
      shift
      ;;
    --no-path-update)
      UPDATE_PATH=0
      shift
      ;;
    --platform-info)
      INSTALL_COMPLETED=1
      source "$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/runtime_bootstrap.sh"
      dmri_runtime_platform
      exit 0
      ;;
    -h|--help)
      INSTALL_COMPLETED=1
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
done

INSTALL_STARTED=1

[[ "$BACKEND_PORT" =~ ^[0-9]+$ ]] && (( BACKEND_PORT >= 1 && BACKEND_PORT <= 65535 )) || \
  fail "--backend-port must be an integer between 1 and 65535"

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [[ -z "$SOURCE_DIR" && -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_SOURCE")" && pwd)"
  if [[ -d "$SCRIPT_DIR/backend" && -f "$SCRIPT_DIR/VERSION" ]]; then
    SOURCE_DIR="$SCRIPT_DIR"
  fi
fi
if [[ "$DEVELOPER_IN_PLACE" == "1" && -z "$SOURCE_DIR" ]]; then
  fail "--developer-in-place requires --source-dir or a local source checkout"
fi

INSTALLER_DIR=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  INSTALLER_DIR="$(cd -P "$(dirname "$SCRIPT_SOURCE")" && pwd)"
fi
RUNTIME_BOOTSTRAP_LIB="$INSTALLER_DIR/scripts/runtime_bootstrap.sh"
[[ -f "$RUNTIME_BOOTSTRAP_LIB" && ! -L "$RUNTIME_BOOTSTRAP_LIB" ]] || \
  fail "Runtime bootstrap support is missing from this installer package"
# shellcheck source=scripts/runtime_bootstrap.sh
source "$RUNTIME_BOOTSTRAP_LIB"
DMRI_RUNTIME_PLATFORM="$(dmri_runtime_platform)"
RUNTIME_ROOT_RAW="${DMRI_AGENT_RUNTIME_DIR:-$STATE_HOME/runtime}"
RUNTIME_ROOT=""

find_python() {
  local candidate
  for candidate in "${DMRI_AGENT_PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
    if [[ -n "$candidate" ]] && command -v "$candidate" >/dev/null 2>&1 && \
      "$candidate" -c 'import sys,ssl,venv; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
      >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

print_prerequisite_help() {
  printf '\nA required runtime could not be prepared.\n' >&2
  printf 'Check the network connection, proxy/TLS settings, free disk space, or provide a verified runtime archive directory.\n' >&2
}

PYTHON_BIN=""
if [[ "${DMRI_AGENT_FORCE_MANAGED_PYTHON:-0}" != "1" ]]; then
  PYTHON_BIN="$(find_python || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  dmri_runtime_preflight_layout "$RUNTIME_ROOT_RAW" "$INSTALL_DIR"
  RUNTIME_ROOT="$(dmri_runtime_root "$RUNTIME_ROOT_RAW")"
  dmri_runtime_preflight_layout "$RUNTIME_ROOT" "$INSTALL_DIR"
  dmri_runtime_ensure_python "$RUNTIME_ROOT" "$RUNTIME_DOWNLOADS"
  PYTHON_BIN="$DMRI_BOOTSTRAP_PYTHON_BIN"
fi

absolute_path() {
  "$PYTHON_BIN" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}
INSTALL_DIR="$(absolute_path "$INSTALL_DIR")"
MACOS_NEW_DEFAULT_AVAILABLE=0
if [[ ! -e "$INSTALL_DIR" ]]; then
  MACOS_NEW_DEFAULT_AVAILABLE=1
elif [[ -d "$INSTALL_DIR" && ! -L "$INSTALL_DIR" && \
        -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
  MACOS_NEW_DEFAULT_AVAILABLE=1
fi
if [[ "$MACOS_DEFAULT_INSTALL" == "1" && "$MACOS_NEW_DEFAULT_AVAILABLE" == "1" ]]; then
  LEGACY_MACOS_INSTALL_DIR="$(absolute_path "$HOME/.local/share/dmri-agent/app")"
  LEGACY_MACOS_MARKER="$LEGACY_MACOS_INSTALL_DIR/.dmri-agent-install"
  if [[ -d "$LEGACY_MACOS_INSTALL_DIR" && ! -L "$LEGACY_MACOS_INSTALL_DIR" && \
        -f "$LEGACY_MACOS_MARKER" && ! -L "$LEGACY_MACOS_MARKER" && \
        "$(sed -n 's/^mode=//p' "$LEGACY_MACOS_MARKER" | head -n 1)" == "managed" ]]; then
    LEGACY_RECORDED_APP="$(sed -n 's/^app_dir=//p' "$LEGACY_MACOS_MARKER" | head -n 1)"
    if [[ -n "$LEGACY_RECORDED_APP" && \
          "$(absolute_path "$LEGACY_RECORDED_APP")" == "$LEGACY_MACOS_INSTALL_DIR" ]]; then
      INSTALL_DIR="$LEGACY_MACOS_INSTALL_DIR"
      log "Found the owned legacy macOS installation; upgrading it in place"
    fi
  fi
fi
BIN_DIR="$(absolute_path "$BIN_DIR")"
STATE_HOME="$(absolute_path "$STATE_HOME")"
CONFIG_HOME="$(absolute_path "$CONFIG_HOME")"
PROTOCOLS_HOME="$(absolute_path "$PROTOCOLS_HOME")"
DEFAULT_WORKSPACE="$(absolute_path "$DEFAULT_WORKSPACE")"
if [[ -n "$WORKSPACE" ]]; then WORKSPACE="$(absolute_path "$WORKSPACE")"; fi
SLICER_RC_PATH="$(absolute_path "$HOME/.slicerrc.py")"
INSTALL_JSON="$CONFIG_HOME/install.json"
SLICER_REGISTRY="$CONFIG_HOME/slicer-startup-registration.json"

INSTALL_MODE="managed"
SOURCE_ROOT=""
if [[ -n "$SOURCE_DIR" && "$DEVELOPER_IN_PLACE" == "1" ]]; then
  APP_DIR="$(cd "$SOURCE_DIR" 2>/dev/null && pwd)" || fail "Source directory not found: $SOURCE_DIR"
  INSTALL_MODE="in_place"
else
  APP_DIR="$INSTALL_DIR"
fi
dmri_runtime_preflight_layout "$RUNTIME_ROOT_RAW" "$APP_DIR"
if [[ -n "$RUNTIME_ROOT" ]]; then
  dmri_runtime_preflight_layout "$RUNTIME_ROOT" "$APP_DIR"
fi

# Existing managed paths are authoritative.  Silently switching XDG/HOME/bin
# locations during an upgrade would leave saved credentials, registrations,
# and command links behind even though their files still exist.  Refuse that
# discontinuity before taking the install lock or mutating APP_DIR.  The one
# intentional exception is workspace: an explicit --workspace changes it,
# while an ordinary re-run inherits the recorded workspace automatically.
PRESERVED_WORKSPACE="$("$PYTHON_BIN" - \
  "$APP_DIR" \
  "$APP_DIR/.dmri-agent-install" \
  "$BIN_DIR" \
  "$CONFIG_HOME" \
  "$STATE_HOME" \
  "$PROTOCOLS_HOME" \
  "$INSTALL_JSON" \
  "$SLICER_REGISTRY" \
  "$SLICER_RC_PATH" \
  "$WORKSPACE_EXPLICIT" <<'PY'
import json
import os
import stat
from pathlib import Path
import sys

(
    app_value,
    marker_value,
    bin_dir,
    config_home,
    state_home,
    protocols_home,
    install_json,
    slicer_registry,
    slicer_rc,
    workspace_explicit,
) = sys.argv[1:]
app = os.path.realpath(app_value)
marker = Path(marker_value)
if marker.is_symlink():
    raise SystemExit(f"Existing managed marker must not be a symbolic link: {marker}")
if not marker.is_file():
    raise SystemExit(0)

values = {}
for line in marker.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
if values.get("mode") != "managed":
    raise SystemExit(0)
recorded_app = values.get("app_dir", "")
if not recorded_app or not os.path.isabs(recorded_app):
    raise SystemExit("Existing managed marker has an invalid app_dir")
if os.path.realpath(recorded_app) != app:
    raise SystemExit("Existing managed marker does not own this application directory")


def canonical(label: str, raw: str) -> str:
    if "\n" in raw or "\r" in raw or not os.path.isabs(raw):
        raise SystemExit(f"Existing managed {label} path is invalid")
    return os.path.realpath(os.path.expanduser(raw))


def recorded(label: str, raw: str) -> str:
    resolved = canonical(label, raw)
    try:
        common = os.path.commonpath((app, resolved))
    except ValueError:
        return resolved
    if common == app:
        raise SystemExit(
            f"Protected recorded {label} path must be outside "
            f"the application directory: {resolved}"
        )
    return resolved


current = {
    "bin_dir": bin_dir,
    "config_home": config_home,
    "state_home": state_home,
    "protocols_home": protocols_home,
    "install_json": install_json,
    "slicer_registry": slicer_registry,
    "slicer_rc": slicer_rc,
}
for key, current_value in current.items():
    recorded_value = values.get(key, "")
    if recorded_value and recorded(key, recorded_value) != canonical(key, current_value):
        raise SystemExit(
            f"Existing managed {key} path differs from this run; "
            f"re-run with the original environment/path: {recorded_value}"
        )

if workspace_explicit == "1":
    raise SystemExit(0)

marker_workspace = values.get("workspace", "")
metadata_workspace = ""
metadata_path = Path(values.get("install_json") or install_json)
if metadata_path.is_symlink():
    raise SystemExit(f"Existing install metadata must not be a symbolic link: {metadata_path}")
if metadata_path.is_file():
    info = metadata_path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("Existing install metadata must be owned by this user and not writable by others")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Existing install metadata is invalid: {exc}")
    metadata_owner = payload.get("app_dir")
    if metadata_owner is not None and (
        not isinstance(metadata_owner, str)
        or not os.path.isabs(metadata_owner)
        or os.path.realpath(metadata_owner) != app
    ):
        raise SystemExit("Existing install metadata does not belong to this application")
    metadata_workspace = payload.get("workspace", "")
    if not isinstance(metadata_workspace, str):
        raise SystemExit("Existing install metadata has an invalid workspace")

# The Slicer settings UI can update the private user metadata after installation.
# The marker describes the previous installation; keep the newer user choice.
selected = metadata_workspace or marker_workspace
if selected:
    label = "metadata workspace" if metadata_workspace else "workspace"
    print(recorded(label, selected))
PY
)"
if [[ "$WORKSPACE_EXPLICIT" == "0" && -n "$PRESERVED_WORKSPACE" ]]; then
  WORKSPACE="$PRESERVED_WORKSPACE"
  log "Reusing the existing managed workspace: $WORKSPACE"
fi

# User data and recovery records must never live inside APP_DIR.  A managed
# upgrade deletes the retained old application only after commit, and uninstall
# deletes a managed APP_DIR; rejecting overlap before either mutation prevents
# a historical marker or a new option from turning those operations into data
# loss.
LAYOUT_WORKSPACE="$DEFAULT_WORKSPACE"
if [[ -n "$WORKSPACE" ]]; then LAYOUT_WORKSPACE="$WORKSPACE"; fi
"$PYTHON_BIN" - \
  "$APP_DIR" \
  "$BIN_DIR" \
  "$CONFIG_HOME" \
  "$STATE_HOME" \
  "$PROTOCOLS_HOME" \
  "$LAYOUT_WORKSPACE" \
  "$INSTALL_JSON" \
  "$SLICER_REGISTRY" \
  "$SLICER_RC_PATH" \
  "$APP_DIR/.dmri-agent-install" <<'PY'
import json
import os
from pathlib import Path
import sys

(
    app_value,
    bin_dir,
    config_home,
    state_home,
    protocols_home,
    workspace,
    install_json,
    slicer_registry,
    slicer_rc,
    marker_value,
) = sys.argv[1:]
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


for label, value in (
    ("bin_dir", bin_dir),
    ("config_home", config_home),
    ("state_home", state_home),
    ("protocols_home", protocols_home),
    ("workspace", workspace),
    ("install_json", install_json),
    ("slicer_registry", slicer_registry),
    ("slicer_rc", slicer_rc),
):
    validate(label, value)

marker = Path(marker_value)
if marker.is_file():
    values = {}
    for line in marker.read_text(encoding="utf-8").splitlines():
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

metadata = Path(install_json)
if metadata.is_file():
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Existing install metadata is invalid: {exc}")
    recorded_workspace = payload.get("workspace")
    if recorded_workspace is not None and not isinstance(recorded_workspace, str):
        raise SystemExit("Existing install metadata has an invalid workspace")
    validate("metadata workspace", recorded_workspace or "")
PY

# Serialize every mutation of one managed application directory.  Keeping the
# advisory lock on an inherited file descriptor makes it portable across
# macOS Bash 3.2 and Linux without depending on the optional `flock` command.
# The kernel releases the lock automatically after a normal exit or a crash.
INSTALL_LOCK_FILE="${APP_DIR}.dmri-agent-install.lock"
mkdir -p "$(dirname "$INSTALL_LOCK_FILE")"
umask 077
if ! "$PYTHON_BIN" -c \
  'import os,stat,sys
path=sys.argv[1]
try:
 info=os.lstat(path)
except FileNotFoundError:
 sys.exit(0)
except OSError:
 sys.exit(74)
uid_getter=getattr(os, "geteuid", None) or getattr(os, "getuid", None)
if (
 stat.S_ISLNK(info.st_mode)
 or not stat.S_ISREG(info.st_mode)
 or (uid_getter is not None and info.st_uid != uid_getter())
 or info.st_nlink != 1
):
 sys.exit(74)' "$INSTALL_LOCK_FILE"; then
  fail "Install lock path is unsafe: $INSTALL_LOCK_FILE"
fi
exec 9>>"$INSTALL_LOCK_FILE"
if ! "$PYTHON_BIN" -c \
  'import fcntl,os,stat,sys
path = sys.argv[1]
try:
    named = os.lstat(path)
    opened = os.fstat(9)
except OSError:
    sys.exit(74)
uid_getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
if (
    stat.S_ISLNK(named.st_mode)
    or not stat.S_ISREG(named.st_mode)
    or (uid_getter is not None and named.st_uid != uid_getter())
    or named.st_nlink != 1
    or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
):
    sys.exit(74)
os.fchmod(9, 0o600)
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(73)' "$INSTALL_LOCK_FILE" 9<&9; then
  fail "Install lock is unsafe or another dMRI-Agent installation is already running for: $APP_DIR"
fi
UPGRADE_JOURNAL="$(dirname "$APP_DIR")/.$(basename "$APP_DIR").dmri-agent-upgrade.json"

if [[ "$INSTALL_MODE" != "in_place" ]]; then
  if [[ -n "$SOURCE_DIR" ]]; then
    SOURCE_ROOT="$(cd "$SOURCE_DIR" 2>/dev/null && pwd -P)" || fail "Source directory not found: $SOURCE_DIR"
  elif [[ ! -d "$APP_DIR/backend" ]]; then
    command -v git >/dev/null 2>&1 || {
      print_prerequisite_help
      fail "git is required for remote installation"
    }
    BOOTSTRAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dmri-agent-bootstrap.XXXXXX")" || \
      fail "Could not create the source download staging directory"
    BOOTSTRAP_DIR_OWNED=1
    log "Cloning source into a temporary staging directory"
    if [[ -n "$GIT_REF" ]]; then
      git clone --depth 1 --branch "$GIT_REF" "$REPOSITORY_URL" "$BOOTSTRAP_DIR/source"
    else
      git clone --depth 1 "$REPOSITORY_URL" "$BOOTSTRAP_DIR/source"
    fi
    SOURCE_ROOT="$BOOTSTRAP_DIR/source"
  fi

  if [[ -n "$SOURCE_ROOT" && -d "$APP_DIR" ]]; then
    SOURCE_RELATION="$("$PYTHON_BIN" -c 'import os,sys
app,source=(os.path.realpath(value) for value in sys.argv[1:])
try:
 common=os.path.commonpath((app,source))
except ValueError:
 print("outside")
else:
 print("equal" if app == source else "inside" if common == app else "outside")' \
      "$APP_DIR" "$SOURCE_ROOT")"
    if [[ "$SOURCE_RELATION" == "inside" ]]; then
      fail "Install source must not be located inside the managed application directory"
    elif [[ "$SOURCE_RELATION" == "equal" ]]; then
      SOURCE_ROOT=""
      log "Running installed-copy dependency repair only; use an external release package for a full payload repair"
    fi
  fi

  if [[ -f "$UPGRADE_JOURNAL" ]]; then
    RECOVERY_HELPER=""
    if [[ -n "$SOURCE_ROOT" && -f "$SOURCE_ROOT/scripts/installer_support.py" ]]; then
      RECOVERY_HELPER="$SOURCE_ROOT/scripts/installer_support.py"
    elif [[ -f "$APP_DIR/scripts/installer_support.py" ]]; then
      RECOVERY_HELPER="$APP_DIR/scripts/installer_support.py"
    fi
    [[ -n "$RECOVERY_HELPER" ]] || \
      fail "An interrupted managed upgrade needs a matching installer package for recovery: $UPGRADE_JOURNAL"
    RECOVERY_ACTION="$("$PYTHON_BIN" "$RECOVERY_HELPER" recover-upgrade \
      --journal "$UPGRADE_JOURNAL")"
    log "Recovered interrupted managed upgrade ($RECOVERY_ACTION)"
  fi

  if [[ -n "$SOURCE_ROOT" ]]; then
    MANAGED_ACTION="$("$PYTHON_BIN" "$SOURCE_ROOT/scripts/installer_support.py" managed-target \
      --source "$SOURCE_ROOT" --destination "$APP_DIR")"
    if [[ "$MANAGED_ACTION" == "install" || "$MANAGED_ACTION" == "upgrade" || \
          "$MANAGED_ACTION" == "repair" ]]; then
      mkdir -p "$(dirname "$APP_DIR")"
      MANAGED_STAGE="$(mktemp -d "$(dirname "$APP_DIR")/.dmri-agent-stage.XXXXXX")"
      MANAGED_STAGE_OWNED=1
      log "Copying release files into a managed staging directory"
      "$PYTHON_BIN" "$SOURCE_ROOT/scripts/installer_support.py" managed-copy \
        --source "$SOURCE_ROOT" --destination "$MANAGED_STAGE" >/dev/null
      INSTALL_TOKEN="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(24))')"
      printf '%s\n' "$INSTALL_TOKEN" > "$MANAGED_STAGE/.dmri-agent-install-pending"
      chmod 600 "$MANAGED_STAGE/.dmri-agent-install-pending"
      if [[ "$MANAGED_ACTION" == "install" ]]; then
        if [[ -d "$APP_DIR" ]]; then
          rmdir "$APP_DIR" || fail "Install directory is not empty: $APP_DIR"
        elif [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
          fail "Install path is not an empty directory: $APP_DIR"
        fi
        mv "$MANAGED_STAGE" "$APP_DIR"
        MANAGED_STAGE=""
        MANAGED_STAGE_OWNED=0
        FRESH_MANAGED_INSTALL=1
      else
        UPGRADE_HELPER="$SOURCE_ROOT/scripts/installer_support.py"
        UPGRADE_IN_PROGRESS=1
        UPGRADE_BACKUP="$("$PYTHON_BIN" "$UPGRADE_HELPER" begin-upgrade \
          --source "$SOURCE_ROOT" \
          --destination "$APP_DIR" \
          --stage "$MANAGED_STAGE" \
          --journal "$UPGRADE_JOURNAL" \
          --token "$INSTALL_TOKEN")"
        MANAGED_STAGE=""
        MANAGED_STAGE_OWNED=0
        cp -p "$UPGRADE_BACKUP/.dmri-agent-install" "$APP_DIR/.dmri-agent-install"
        if [[ "$MANAGED_ACTION" == "upgrade" ]]; then
          log "Upgrading the managed application to $(tr -d '[:space:]' < "$APP_DIR/VERSION")"
        else
          log "Replacing the managed application with a clean same-version payload"
        fi
      fi
    else
      SOURCE_ROOT=""
      log "Recovering or repairing the managed installation"
    fi
  fi
fi

if [[ ! -d "$APP_DIR/backend" || ! -f "$APP_DIR/backend/requirements.lock.txt" || \
      ! -x "$APP_DIR/scripts/launchers/dmri_agent_service" || \
      ! -x "$APP_DIR/scripts/launchers/dmri_agent_check" || ! -x "$APP_DIR/scripts/launchers/dmri_agent_slicer" ]]; then
  fail "Invalid dMRI-Agent source directory: $APP_DIR"
fi

log "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
log "Creating Python environment"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
REQUIREMENTS_FILE="$APP_DIR/backend/requirements.lock.txt"
[[ -f "$REQUIREMENTS_FILE" && ! -L "$REQUIREMENTS_FILE" ]] || \
  fail "Pinned Python dependency lock is missing: $REQUIREMENTS_FILE"
if [[ -n "${DMRI_AGENT_PIP_COMMAND:-}" && "${DMRI_AGENT_INSTALL_TEST_MODE:-}" == "1" ]]; then
  "$DMRI_AGENT_PIP_COMMAND" --isolated install --disable-pip-version-check \
    --require-hashes --only-binary=:all: -r "$REQUIREMENTS_FILE"
else
  "$APP_DIR/.venv/bin/python" -m pip --isolated install --disable-pip-version-check \
    --require-hashes --only-binary=:all: -r "$REQUIREMENTS_FILE"
fi

# The managed payload is built at its final path, so virtual-environment script
# shebangs and build metadata cannot retain the temporary copy path.  The
# owner-only random sentinel lets the EXIT trap safely remove only this fresh
# install if a later step fails.
if [[ "$FRESH_MANAGED_INSTALL" == "1" || "$UPGRADE_IN_PROGRESS" == "1" ]]; then
  TEMPORARY_CHECK_MARKER=0
  if [[ "$FRESH_MANAGED_INSTALL" == "1" ]]; then
    TEMPORARY_CHECK_MARKER=1
    printf 'mode=%s\napp_dir=%s\nweb=%s\ncli=%s\n' \
      "$INSTALL_MODE" "$APP_DIR" "0" "0" \
      > "$APP_DIR/.dmri-agent-install"
  fi
  log "Running pre-commit installation self-check"
  if [[ "${DMRI_AGENT_INSTALL_TEST_MODE:-}" == "1" ]]; then
    "$APP_DIR/scripts/launchers/dmri_agent_check" --help >/dev/null
  else
    "$APP_DIR/scripts/launchers/dmri_agent_check" --quiet
  fi
  if [[ "$TEMPORARY_CHECK_MARKER" == "1" ]]; then
    rm -f "$APP_DIR/.dmri-agent-install"
  fi
fi

mkdir -p "$BIN_DIR"

install_link() {
  local source_path="$1"
  local target_path="$2"
  local existed=0
  if [[ -L "$target_path" ]]; then
    existed=1
    local current_target
    current_target="$(readlink "$target_path")"
    if [[ "$current_target" != "$source_path" ]]; then
      fail "Refusing to replace unrelated symlink: $target_path -> $current_target"
    fi
  elif [[ -e "$target_path" ]]; then
    fail "Refusing to replace existing path: $target_path"
  fi
  local temporary_link="$target_path.dmri-agent-new-$$-$RANDOM"
  [[ ! -e "$temporary_link" && ! -L "$temporary_link" ]] || \
    fail "Temporary command link already exists: $temporary_link"
  ln -s "$source_path" "$temporary_link"
  mv -f "$temporary_link" "$target_path"
  if [[ "$existed" == "0" ]]; then NEW_LINKS+=("$target_path"); fi
}

assert_link_available() {
  local source_path="$1"
  local target_path="$2"
  if [[ -L "$target_path" ]]; then
    local current_target
    current_target="$(readlink "$target_path")"
    [[ "$current_target" == "$source_path" ]] || \
      fail "Refusing to replace unrelated symlink: $target_path -> $current_target"
  elif [[ -e "$target_path" ]]; then
    fail "Refusing to replace existing path: $target_path"
  fi
}

remove_owned_link() {
  local target_path="$1"
  if [[ -L "$target_path" && "$(readlink "$target_path")" == "$APP_DIR"/* ]]; then
    REMOVED_LINK_PATHS+=("$target_path")
    REMOVED_LINK_TARGETS+=("$(readlink "$target_path")")
    rm "$target_path"
  fi
}

assert_link_available "$APP_DIR/scripts/launchers/dmri_agent_service" "$BIN_DIR/dmri-agent-service"
assert_link_available "$APP_DIR/scripts/launchers/dmri_agent_slicer" "$BIN_DIR/dmri-agent-slicer"
assert_link_available "$APP_DIR/scripts/launchers/dmri_agent_check" "$BIN_DIR/dmri-agent-check"
assert_link_available "$APP_DIR/uninstall.sh" "$BIN_DIR/dmri-agent-uninstall"

VERSION="$(tr -d '[:space:]' < "$APP_DIR/VERSION")"
INSTALL_JSON="$CONFIG_HOME/install.json"
SLICER_REGISTRY="$CONFIG_HOME/slicer-startup-registration.json"
SLICER_TRANSACTION="$CONFIG_HOME/.slicer-startup-transaction-$$.json"
MARKER_PATH="$APP_DIR/.dmri-agent-install"
mkdir -p "$CONFIG_HOME"
chmod 700 "$CONFIG_HOME"

if [[ -e "$SLICER_TRANSACTION" || -L "$SLICER_TRANSACTION" ]]; then
  fail "Slicer registration transaction path already exists: $SLICER_TRANSACTION"
fi
SLICER_TRANSACTION_TRACKED=1

if [[ -f "$INSTALL_JSON" ]]; then
  METADATA_BACKUP="$(mktemp "${TMPDIR:-/tmp}/dmri-agent-install-json.XXXXXX")"
  cp -p "$INSTALL_JSON" "$METADATA_BACKUP"
  METADATA_EXISTED=1
fi
METADATA_STATE_KNOWN=1
if [[ -f "$MARKER_PATH" ]]; then
  MARKER_BACKUP="$(mktemp "${TMPDIR:-/tmp}/dmri-agent-install-marker.XXXXXX")"
  cp -p "$MARKER_PATH" "$MARKER_BACKUP"
  MARKER_EXISTED=1
fi
MARKER_STATE_KNOWN=1
if [[ -f "$SLICER_REGISTRY" ]]; then
  SLICER_REGISTRY_BACKUP="$(mktemp "${TMPDIR:-/tmp}/dmri-agent-slicer-registry.XXXXXX")"
  cp -p "$SLICER_REGISTRY" "$SLICER_REGISTRY_BACKUP"
  SLICER_REGISTRY_EXISTED=1
fi
SLICER_REGISTRY_STATE_KNOWN=1

RECORDED_SLICER_RC=""
if [[ -f "$SLICER_REGISTRY" ]]; then
  RECORDED_SLICER_RC="$("$PYTHON_BIN" - "$SLICER_REGISTRY" "$APP_DIR" <<'PY'
import json
import os
import sys

registry_path, expected_owner = sys.argv[1:]
with open(registry_path, "r", encoding="utf-8") as stream:
    payload = json.load(stream)
owner = payload.get("owner_id")
rc_path = payload.get("rc_path")
if (
    payload.get("action") != "register"
    or os.path.realpath(str(owner)) != os.path.realpath(expected_owner)
    or not isinstance(rc_path, str)
    or not os.path.isabs(rc_path)
):
    raise SystemExit("Invalid Slicer startup registration record")
print(os.path.abspath(rc_path))
PY
  )" || fail "Could not validate the existing Slicer startup registration record"
elif [[ -f "$MARKER_PATH" ]]; then
  PREVIOUS_SLICER_REGISTERED="$(sed -n 's/^slicer_registered=//p' "$MARKER_PATH" | head -n 1)"
  PREVIOUS_SLICER_RC="$(sed -n 's/^slicer_rc=//p' "$MARKER_PATH" | head -n 1)"
  case "$PREVIOUS_SLICER_REGISTERED" in
    0|"") ;;
    1)
      [[ "$PREVIOUS_SLICER_RC" == /* ]] || \
        fail "Existing install marker has no safe Slicer startup recovery path"
      RECORDED_SLICER_RC="$(absolute_path "$PREVIOUS_SLICER_RC")"
      ;;
    *) fail "Existing install marker has invalid Slicer registration state" ;;
  esac
fi
if [[ -n "$RECORDED_SLICER_RC" ]]; then
  if [[ "$REGISTER_SLICER" == "1" && "$SLICER_RC_PATH" != "$RECORDED_SLICER_RC" ]]; then
    fail "This installation is already registered for another user startup file: $RECORDED_SLICER_RC"
  fi
  SLICER_RC_PATH="$RECORDED_SLICER_RC"
fi

SLICER_REGISTERED=0
if [[ "$REGISTER_SLICER" == "1" ]]; then
  SLICER_ARGS=()
  if [[ "${#SLICER_PATHS[@]}" -gt 0 ]]; then
    for slicer_path in "${SLICER_PATHS[@]}"; do
      SLICER_ARGS+=(--slicer "$slicer_path")
    done
    SLICER_DISCOVERY="$("$PYTHON_BIN" "$APP_DIR/scripts/slicer_registration.py" discover "${SLICER_ARGS[@]}")"
  else
    # macOS ships Bash 3.2, where expanding an empty array under `set -u`
    # raises an unbound-variable error.  The no-argument call is also the
    # product path that performs platform-specific Slicer auto-discovery.
    SLICER_DISCOVERY="$("$PYTHON_BIN" "$APP_DIR/scripts/slicer_registration.py" discover)"
  fi
  if [[ "$SLICER_DISCOVERY" != "[]" ]]; then
    SLICER_HOME_RCS="$(printf '%s' "$SLICER_DISCOVERY" | "$PYTHON_BIN" -c \
      'import json,sys; from pathlib import Path
values=json.load(sys.stdin)
if any("\n" in raw or "\r" in raw for raw in values):
 raise SystemExit("Slicer installation path contains a newline")
for raw in values:
 p=Path(raw); text=str(p)
 home=Path(text[:-len("/MacOS/Slicer")]) if text.endswith(".app/Contents/MacOS/Slicer") else p.parent
 print(home / ".slicerrc.py")')" || fail "Could not validate detected Slicer installations"
    SLICER_HOME_RC_ARGS=()
    while IFS= read -r slicer_home_rc; do
      [[ -n "$slicer_home_rc" ]] || fail "Slicer installation has an unsupported path"
      SLICER_HOME_RC_ARGS+=(--slicer-home-rc "$slicer_home_rc")
    done <<< "$SLICER_HOME_RCS"
    log "Registering dMRI-Agent in the current user's 3D Slicer startup"
    (
      umask 077
      "$PYTHON_BIN" "$APP_DIR/scripts/slicer_rc_registration.py" register \
        --rc "$SLICER_RC_PATH" \
        --bootstrap "$APP_DIR/slicer_extension/user_startup_bootstrap.py" \
        --owner "$APP_DIR" \
        --journal "$SLICER_TRANSACTION" \
        "${SLICER_HOME_RC_ARGS[@]}" \
        > "$SLICER_REGISTRY.tmp"
    )
    mv "$SLICER_REGISTRY.tmp" "$SLICER_REGISTRY"
    SLICER_REGISTERED=1
  elif [[ "${#SLICER_PATHS[@]}" -gt 0 ]]; then
    fail "No usable Slicer installation was found"
  else
    fail "3D Slicer was not found; install Slicer or pass --slicer /path/to/Slicer.app"
  fi
fi
if [[ -f "$SLICER_REGISTRY" ]]; then
  SLICER_REGISTERED=1
elif [[ -n "$RECORDED_SLICER_RC" ]]; then
  SLICER_REGISTERED=1
fi

WORKSPACE_ARGS=(--default-workspace "$DEFAULT_WORKSPACE")
if [[ -n "$WORKSPACE" ]]; then
  WORKSPACE_ARGS=(--workspace "$WORKSPACE")
fi
WORKSPACE="$("$PYTHON_BIN" "$APP_DIR/scripts/installer_support.py" write-metadata \
  --path "$INSTALL_JSON" --app-dir "$APP_DIR" --version "$VERSION" \
  --backend-port "$BACKEND_PORT" "${WORKSPACE_ARGS[@]}")"

"$PYTHON_BIN" "$APP_DIR/scripts/installer_support.py" write-marker \
  --path "$MARKER_PATH" \
  --mode "$INSTALL_MODE" \
  --app-dir "$APP_DIR" \
  --bin-dir "$BIN_DIR" \
  --state-home "$STATE_HOME" \
  --config-home "$CONFIG_HOME" \
  --protocols-home "$PROTOCOLS_HOME" \
  --install-json "$INSTALL_JSON" \
  --slicer-registry "$SLICER_REGISTRY" \
  --slicer-rc "$SLICER_RC_PATH" \
  --slicer-registered "$SLICER_REGISTERED" \
  --web "0" \
  --cli "0" \
  --installed-at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --workspace "$WORKSPACE"

remove_owned_link "$BIN_DIR/dmri-agent"
install_link "$APP_DIR/scripts/launchers/dmri_agent_service" "$BIN_DIR/dmri-agent-service"
remove_owned_link "$BIN_DIR/dmri-agent-web"
install_link "$APP_DIR/scripts/launchers/dmri_agent_slicer" "$BIN_DIR/dmri-agent-slicer"
install_link "$APP_DIR/scripts/launchers/dmri_agent_check" "$BIN_DIR/dmri-agent-check"
remove_owned_link "$BIN_DIR/dmri-agent-doctor"
install_link "$APP_DIR/uninstall.sh" "$BIN_DIR/dmri-agent-uninstall"

log "Running installation self-check"
if [[ "${DMRI_AGENT_INSTALL_TEST_MODE:-}" == "1" ]]; then
  "$APP_DIR/scripts/launchers/dmri_agent_check" --help >/dev/null
else
  "$APP_DIR/scripts/launchers/dmri_agent_check" --quiet
fi

# Commit the managed application before consuming the Slicer RC transaction.
# If finalizing or committing the app fails, the still-live Slicer journal lets
# the EXIT trap restore the previous RC alongside the app/config rollback.
"$PYTHON_BIN" "$APP_DIR/scripts/installer_support.py" finalize-pending \
  --destination "$APP_DIR" --app-dir "$APP_DIR" >/dev/null
if [[ "$UPGRADE_IN_PROGRESS" == "1" ]]; then
  "$PYTHON_BIN" "$UPGRADE_HELPER" commit-upgrade \
    --journal "$UPGRADE_JOURNAL" >/dev/null
  UPGRADE_IN_PROGRESS=0
fi
INSTALL_COMMITTED=1
FRESH_MANAGED_INSTALL=0
if [[ -f "$SLICER_TRANSACTION" ]]; then
  "$PYTHON_BIN" "$APP_DIR/scripts/slicer_rc_registration.py" commit \
    --journal "$SLICER_TRANSACTION" >/dev/null
fi
rm -f "$SLICER_TRANSACTION"
SLICER_TRANSACTION_TRACKED=0

if [[ "$UPDATE_PATH" == "1" ]]; then
  case ":$PATH:" in
    *":$BIN_DIR:"*)
      ;;
    *)
      RC_FILE=""
      case "${SHELL:-}" in
        */zsh) RC_FILE="$HOME/.zshrc" ;;
        */bash) RC_FILE="$HOME/.bashrc" ;;
      esac
      if [[ -n "$RC_FILE" ]]; then
        if ! touch "$RC_FILE" 2>/dev/null; then
          warn "Could not update $RC_FILE; use the absolute commands printed below"
        elif ! grep -Fq '# dMRI Agent user commands' "$RC_FILE" 2>/dev/null; then
          if printf '\n# dMRI Agent user commands\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$RC_FILE"; then
            log "Added $BIN_DIR to PATH in $RC_FILE"
          else
            warn "Could not update $RC_FILE; use the absolute commands printed below"
          fi
        fi
      else
        log "Add $BIN_DIR to PATH to use commands without an absolute path"
      fi
      ;;
  esac
fi

cat <<EOF

dMRI-Agent installation completed.

Start Slicer now with the same workspace:
  $APP_DIR/scripts/launchers/dmri_agent_slicer --workspace "$WORKSPACE"
After Slicer registration, restart ordinary Slicer and select:
  Modules > Diffusion MRI > dMRI-Agent
If Slicer is already open, close and restart it before looking for the module.
Configure the model key directly in the Slicer module with Save & Test. Credentials are stored in
$CONFIG_HOME/secrets.json and are never written into the repository.

Installation self-check:
  $BIN_DIR/dmri-agent-check
EOF

INSTALL_COMPLETED=1
