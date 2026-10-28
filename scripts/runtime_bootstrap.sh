#!/usr/bin/env bash

# User-local runtime bootstrap shared by install.sh.  This file is sourced, not
# executed.  Keep it compatible with the Bash 3.2 shipped by older macOS
# releases and do not mutate system package managers or /Applications.

DMRI_BOOTSTRAP_UV_VERSION="0.12.0"
DMRI_BOOTSTRAP_PYTHON_VERSION="3.12.13"

dmri_runtime_log() {
  if declare -F log >/dev/null 2>&1; then
    log "$*"
  else
    printf '[dMRI-Agent] %s\n' "$*"
  fi
}

dmri_runtime_fail() {
  if declare -F fail >/dev/null 2>&1; then
    fail "$*"
  else
    printf '[dMRI-Agent] ERROR: %s\n' "$*" >&2
    exit 1
  fi
}

dmri_runtime_platform() {
  local system machine libc_info libc_major libc_minor
  system="$(uname -s 2>/dev/null || true)"
  machine="$(uname -m 2>/dev/null || true)"
  case "$machine" in
    arm64|aarch64) machine="aarch64" ;;
    x86_64|amd64) machine="x86_64" ;;
    *) dmri_runtime_fail "Unsupported CPU architecture for managed runtimes: $machine" ;;
  esac
  case "$system" in
    Darwin) printf 'macos:%s\n' "$machine" ;;
    Linux)
      libc_info="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
      if [[ "$libc_info" != glibc\ * ]]; then
        libc_info="$(ldd --version 2>&1 || true)"
        case "$libc_info" in
          *musl*|*MUSL*)
            dmri_runtime_fail "Automatic runtime installation currently supports glibc-based Linux, not musl"
            ;;
          *GLIBC*|*glibc*|*"GNU C Library"*) ;;
          *)
            dmri_runtime_fail "Could not verify a supported glibc Linux environment for managed runtimes"
            ;;
        esac
      fi
      if [[ "$libc_info" =~ ([0-9]+)\.([0-9]+) ]]; then
        libc_major="${BASH_REMATCH[1]}"
        libc_minor="${BASH_REMATCH[2]}"
      else
        dmri_runtime_fail "Could not determine the glibc version required by binary Python dependencies"
      fi
      if (( libc_major < 2 || (libc_major == 2 && libc_minor < 28) )); then
        dmri_runtime_fail "Linux glibc 2.28 or newer is required (found $libc_major.$libc_minor)"
      fi
      printf 'linux:%s\n' "$machine"
      ;;
    *) dmri_runtime_fail "Automatic runtime installation is not supported on $system" ;;
  esac
}

dmri_runtime_sha256() {
  local output
  if command -v shasum >/dev/null 2>&1; then
    output="$(shasum -a 256 "$1")" || return 1
  elif command -v sha256sum >/dev/null 2>&1; then
    output="$(sha256sum "$1")" || return 1
  else
    dmri_runtime_fail "A SHA-256 utility (shasum or sha256sum) is required"
  fi
  printf '%s\n' "${output%% *}"
}

dmri_runtime_expected_hash() {
  local kind platform override=""
  kind="$1"
  platform="$2"
  if [[ "${DMRI_AGENT_INSTALL_TEST_MODE:-}" == "1" ]]; then
    case "$kind" in
      uv) override="${DMRI_AGENT_TEST_UV_SHA256:-}" ;;
    esac
  fi
  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi
  case "$kind:$platform" in
    uv:macos:aarch64) printf '%s\n' '2b9e582af54f84fa50c115427451a6c13e80f43b52f8282b8af5791077317bbf' ;;
    uv:macos:x86_64) printf '%s\n' 'd41593beaefc54bab7d062af0ef6ca093bfb81d001d58ebbef39e44423f9c496' ;;
    uv:linux:aarch64) printf '%s\n' '2c5d6e3092cc5223b10ff403880cc75121bf64e84644e7a0c69f643b0d89ac95' ;;
    uv:linux:x86_64) printf '%s\n' 'eaf842262aa1c418d8ecc5605f02ee1ebfd369124fa48548e85f9481a47831a9' ;;
    *) dmri_runtime_fail "No pinned $kind runtime is available for $platform" ;;
  esac
}

dmri_runtime_download() {
  local url destination archive_name cached
  url="$1"
  destination="$2"
  archive_name="$3"
  cached=""
  if [[ -n "${DMRI_AGENT_RUNTIME_ARCHIVE_DIR:-}" ]]; then
    cached="${DMRI_AGENT_RUNTIME_ARCHIVE_DIR%/}/$archive_name"
  fi
  if [[ -n "$cached" && -f "$cached" && ! -L "$cached" ]]; then
    cp "$cached" "$destination" || dmri_runtime_fail "Could not copy cached runtime: $cached"
    return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
      --connect-timeout 20 --silent --show-error "$url" -o "$destination" || \
      dmri_runtime_fail "Could not download required runtime: $url"
  elif command -v wget >/dev/null 2>&1; then
    wget --https-only --tries=3 --timeout=20 -q "$url" -O "$destination" || \
      dmri_runtime_fail "Could not download required runtime: $url"
  else
    dmri_runtime_fail "curl or wget is required to download the managed runtime"
  fi
}

dmri_runtime_verify_archive() {
  local archive expected actual label
  archive="$1"
  expected="$2"
  label="$3"
  actual="$(dmri_runtime_sha256 "$archive")" || \
    dmri_runtime_fail "Could not calculate the SHA-256 for $label"
  if [[ "$actual" != "$expected" ]]; then
    dmri_runtime_fail "$label failed SHA-256 verification; nothing was installed"
  fi
}

dmri_runtime_validate_tar_root() {
  local archive expected_root entry
  archive="$1"
  expected_root="$2"
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    case "$entry" in
      /*|../*|*/../*|*/..) dmri_runtime_fail "Runtime archive contains an unsafe path: $entry" ;;
    esac
    case "$entry" in
      "$expected_root"|"$expected_root"/*) ;;
      *) dmri_runtime_fail "Runtime archive contains an unexpected top-level path: $entry" ;;
    esac
  done < <(tar -tzf "$archive")
}

dmri_runtime_root() {
  local raw resolved
  raw="$1"
  [[ -n "$raw" ]] || dmri_runtime_fail "Managed runtime directory is unsafe: $raw"
  resolved="$(dmri_runtime_prospective_path "$raw")"
  [[ -n "${resolved//\//}" ]] || dmri_runtime_fail "Managed runtime directory is unsafe: $raw"
  if [[ -L "$raw" ]]; then
    dmri_runtime_fail "Managed runtime directory must not be a symbolic link: $raw"
  fi
  mkdir -p "$raw" || dmri_runtime_fail "Could not create managed runtime directory: $raw"
  resolved="$(cd -P "$raw" && pwd)" || dmri_runtime_fail "Could not resolve managed runtime directory: $raw"
  [[ -n "${resolved//\//}" ]] || dmri_runtime_fail "Managed runtime directory is unsafe: $resolved"
  printf '%s\n' "$resolved"
}

dmri_runtime_prospective_path() {
  local unresolved suffix leaf parent resolved remaining component normalized
  unresolved="$1"
  case "$unresolved" in
    /*) ;;
    *) unresolved="$PWD/$unresolved" ;;
  esac
  suffix=""
  while [[ ! -e "$unresolved" && ! -L "$unresolved" ]]; do
    leaf="$(basename "$unresolved")"
    parent="$(dirname "$unresolved")"
    [[ "$parent" != "$unresolved" ]] || dmri_runtime_fail "Could not resolve path: $1"
    suffix="/$leaf$suffix"
    unresolved="$parent"
  done
  if [[ -d "$unresolved" ]]; then
    resolved="$(cd -P "$unresolved" && pwd)" || dmri_runtime_fail "Could not resolve path: $1"
  else
    parent="$(cd -P "$(dirname "$unresolved")" && pwd)" || dmri_runtime_fail "Could not resolve path: $1"
    resolved="$parent/$(basename "$unresolved")"
  fi

  # The suffix can contain dot components when its first missing directory
  # prevented the kernel from resolving the complete path.  Normalize those
  # components relative to the canonical existing ancestor so an apparent
  # outside path such as outside/missing/../../app/runtime cannot bypass the
  # protected application-directory check.
  normalized="$resolved"
  remaining="${suffix#/}"
  while [[ -n "$remaining" ]]; do
    if [[ "$remaining" == */* ]]; then
      component="${remaining%%/*}"
      remaining="${remaining#*/}"
    else
      component="$remaining"
      remaining=""
    fi
    case "$component" in
      ""|.) ;;
      ..)
        if [[ "$normalized" != "/" ]]; then
          normalized="${normalized%/*}"
          [[ -n "$normalized" ]] || normalized="/"
        fi
        ;;
      *)
        if [[ "$normalized" == "/" ]]; then
          normalized="/$component"
        else
          normalized="$normalized/$component"
        fi
        ;;
    esac
  done
  printf '%s\n' "$normalized"
}

dmri_runtime_preflight_layout() {
  local runtime_path app_path
  runtime_path="$(dmri_runtime_prospective_path "$1")"
  app_path="$(dmri_runtime_prospective_path "$2")"
  case "$runtime_path/" in
    "$app_path/"*) dmri_runtime_fail "Protected managed runtime path must be outside the application directory: $runtime_path" ;;
  esac
}

dmri_runtime_uv_asset() {
  local platform system machine
  platform="$1"
  system="${platform%%:*}"
  machine="${platform##*:}"
  case "$system" in
    macos) printf 'uv-%s-apple-darwin.tar.gz\n' "$machine" ;;
    linux) printf 'uv-%s-unknown-linux-gnu.tar.gz\n' "$machine" ;;
    *) dmri_runtime_fail "Unsupported uv platform: $platform" ;;
  esac
}

dmri_runtime_ensure_uv() {
  local runtime_root downloads platform asset expected url target marker temp archive extract root uv_path ready
  runtime_root="$1"
  downloads="$2"
  target="$runtime_root/uv-$DMRI_BOOTSTRAP_UV_VERSION"
  marker="$target/.dmri-agent-runtime"

  if [[ -n "${DMRI_AGENT_UV_BINARY:-}" ]]; then
    [[ -x "$DMRI_AGENT_UV_BINARY" ]] || dmri_runtime_fail "DMRI_AGENT_UV_BINARY is not executable"
    case "$("$DMRI_AGENT_UV_BINARY" --version 2>/dev/null || true)" in
      "uv $DMRI_BOOTSTRAP_UV_VERSION"*) DMRI_BOOTSTRAP_UV_BIN="$DMRI_AGENT_UV_BINARY"; return 0 ;;
      *) dmri_runtime_fail "DMRI_AGENT_UV_BINARY must be uv $DMRI_BOOTSTRAP_UV_VERSION" ;;
    esac
  fi

  if [[ -x "$target/uv" && -f "$marker" && ! -L "$marker" ]] && \
     [[ "$(< "$marker")" == "uv:$DMRI_BOOTSTRAP_UV_VERSION" ]] && \
     [[ "$("$target/uv" --version 2>/dev/null || true)" == "uv $DMRI_BOOTSTRAP_UV_VERSION"* ]]; then
    DMRI_BOOTSTRAP_UV_BIN="$target/uv"
    return 0
  fi
  [[ "$downloads" == "1" ]] || dmri_runtime_fail \
    "Python is missing and runtime downloads were disabled (--no-runtime-downloads)"
  if [[ -e "$target" || -L "$target" ]]; then
    dmri_runtime_fail "Managed uv runtime exists but is invalid: $target"
  fi

  platform="$(dmri_runtime_platform)"
  asset="$(dmri_runtime_uv_asset "$platform")"
  expected="$(dmri_runtime_expected_hash uv "$platform")"
  url="${DMRI_AGENT_UV_BASE_URL:-https://releases.astral.sh/github/uv/releases/download}/$DMRI_BOOTSTRAP_UV_VERSION/$asset"
  temp="$(mktemp -d "$runtime_root/.uv-bootstrap.XXXXXX")" || dmri_runtime_fail "Could not create uv staging directory"
  RUNTIME_BOOTSTRAP_TMP="$temp"
  RUNTIME_BOOTSTRAP_TMP_PARENT="$runtime_root"
  RUNTIME_BOOTSTRAP_TMP_OWNED=1
  archive="$temp/$asset"
  extract="$temp/extract"
  mkdir -p "$extract"
  dmri_runtime_log "Python 3.10+ was not found; preparing a private Python runtime"
  dmri_runtime_download "$url" "$archive" "$asset"
  dmri_runtime_verify_archive "$archive" "$expected" "uv $DMRI_BOOTSTRAP_UV_VERSION"
  root="${asset%.tar.gz}"
  dmri_runtime_validate_tar_root "$archive" "$root"
  tar -xzf "$archive" -C "$extract" || dmri_runtime_fail "Could not extract the verified uv runtime"
  uv_path="$extract/$root/uv"
  [[ -f "$uv_path" && ! -L "$uv_path" ]] || dmri_runtime_fail "Verified uv archive is missing its executable"
  ready="$temp/ready"
  mkdir "$ready" || dmri_runtime_fail "Could not create uv activation staging directory"
  cp "$uv_path" "$ready/uv" || dmri_runtime_fail "Could not stage the verified uv executable"
  chmod 755 "$ready/uv"
  printf 'uv:%s\n' "$DMRI_BOOTSTRAP_UV_VERSION" > "$ready/.dmri-agent-runtime"
  chmod 600 "$ready/.dmri-agent-runtime"
  [[ "$("$ready/uv" --version 2>/dev/null || true)" == "uv $DMRI_BOOTSTRAP_UV_VERSION"* ]] || \
    dmri_runtime_fail "Staged uv runtime failed its self-check"
  mv "$ready" "$target" || dmri_runtime_fail "Could not atomically activate the uv runtime"
  rm -rf "$temp"
  RUNTIME_BOOTSTRAP_TMP=""
  RUNTIME_BOOTSTRAP_TMP_PARENT=""
  RUNTIME_BOOTSTRAP_TMP_OWNED=0
  DMRI_BOOTSTRAP_UV_BIN="$target/uv"
}

dmri_runtime_managed_python_path() {
  local runtime_root platform system machine libc
  runtime_root="$1"
  platform="$(dmri_runtime_platform)"
  system="${platform%%:*}"
  machine="${platform##*:}"
  case "$system" in
    macos) libc="none" ;;
    linux) libc="gnu" ;;
    *) dmri_runtime_fail "Unsupported managed Python platform: $platform" ;;
  esac
  printf '%s/python/cpython-%s-%s-%s-%s/bin/python3.12\n' \
    "$runtime_root" "$DMRI_BOOTSTRAP_PYTHON_VERSION" "$system" "$machine" "$libc"
}

dmri_runtime_python_is_valid() {
  local candidate
  candidate="$1"
  [[ -x "$candidate" ]] || return 1
  if [[ "${DMRI_AGENT_INSTALL_TEST_MODE:-}" == "1" && \
        "${DMRI_AGENT_TEST_RELAX_PYTHON_VERSION:-0}" == "1" ]]; then
    "$candidate" -c 'import sys,ssl,venv; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
      >/dev/null 2>&1
  else
    "$candidate" -c 'import sys,ssl,venv; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 13) else 1)' \
      >/dev/null 2>&1
  fi
}

dmri_runtime_ensure_python() {
  local runtime_root downloads candidate cache_dir
  runtime_root="$1"
  downloads="$2"
  candidate="$(dmri_runtime_managed_python_path "$runtime_root")"
  if dmri_runtime_python_is_valid "$candidate"; then
    DMRI_BOOTSTRAP_PYTHON_BIN="$candidate"
    return 0
  fi
  dmri_runtime_ensure_uv "$runtime_root" "$downloads"
  cache_dir="$runtime_root/cache/uv"
  mkdir -p "$cache_dir"
  dmri_runtime_log "Installing private Python $DMRI_BOOTSTRAP_PYTHON_VERSION (no administrator access required)"
  UV_CACHE_DIR="$cache_dir" \
  UV_PYTHON_INSTALL_DIR="$runtime_root/python" \
    "$DMRI_BOOTSTRAP_UV_BIN" python install "$DMRI_BOOTSTRAP_PYTHON_VERSION" \
      --install-dir "$runtime_root/python" --no-bin --managed-python \
      --no-config --no-progress || dmri_runtime_fail "Could not install the private Python runtime"
  [[ -x "$candidate" ]] || dmri_runtime_fail "Private Python installation did not create the expected interpreter"
  dmri_runtime_python_is_valid "$candidate" || \
    dmri_runtime_fail "Private Python runtime failed its self-check"
  DMRI_BOOTSTRAP_PYTHON_BIN="$candidate"
}
