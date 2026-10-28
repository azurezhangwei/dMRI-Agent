# Shared lifecycle helpers for the POSIX Slicer launcher.
# The caller must define APP_DIR, PYTHON_BIN, and BACKEND_URL before sourcing.

DMRI_SERVICE_LEASE_ID=""
DMRI_SERVICE_LEASE_HEARTBEAT_PID=""
DMRI_SERVICE_LEASE_TTL_SECONDS="${DMRI_SERVICE_LEASE_TTL_SECONDS:-20}"
DMRI_SERVICE_LEASE_HEARTBEAT_SECONDS="${DMRI_SERVICE_LEASE_HEARTBEAT_SECONDS:-5}"
DMRI_BROKER_ACTION=""
DMRI_BROKER_CLAIM_ID=""
DMRI_SERVICE_PUBLISHED=0
DMRI_SERVICE_LEASE_ACQUIRED=0
DMRI_BROKER_SLICER_SCENE_URL=""

dmri_prepare_service() {
  local client="$1"
  local prepared
  prepared="$(PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m backend.service.runtime_broker prepare \
      --base-url "$BACKEND_URL" --client "$client" --owner-pid "$$" --format shell)" || return 1
  eval "$prepared"
  BACKEND_URL="$DMRI_BROKER_BASE_URL"
  DMRI_SERVICE_LEASE_ID="$DMRI_BROKER_LEASE_ID"
  DMRI_BROKER_CLAIM_ID="$DMRI_BROKER_CLAIM_ID"
  export DMRI_SERVICE_TOKEN="$DMRI_BROKER_TOKEN"
}

dmri_publish_service() {
  [[ -n "$DMRI_BROKER_CLAIM_ID" ]] || return 0
  PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m backend.service.runtime_broker publish \
      --claim-id "$DMRI_BROKER_CLAIM_ID" --pid "$BACKEND_PID" >/dev/null
  DMRI_BROKER_CLAIM_ID=""
  DMRI_SERVICE_PUBLISHED=1
}

dmri_abandon_service_start() {
  if [[ -n "$DMRI_BROKER_CLAIM_ID" ]]; then
    PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON_BIN" -m backend.service.runtime_broker abandon \
        --claim-id "$DMRI_BROKER_CLAIM_ID" >/dev/null 2>&1 || true
    DMRI_BROKER_CLAIM_ID=""
  fi
}

dmri_resolve_default_workspace() {
  PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m backend.service.runtime_broker workspace
}

dmri_service_lease_call() {
  local action="$1"
  local client="$2"
  PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m backend.service.lease_client \
      "$action" "$BACKEND_URL" "$DMRI_SERVICE_LEASE_ID" \
      --client "$client" --ttl "$DMRI_SERVICE_LEASE_TTL_SECONDS"
}

dmri_acquire_service_lease() {
  local client="$1"
  local lease_owner_pid="$$"
  if [[ -z "$DMRI_SERVICE_LEASE_ID" ]]; then
    DMRI_SERVICE_LEASE_ID="$("$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4())')"
  fi
  dmri_service_lease_call acquire "$client" >/dev/null || return 1
  DMRI_SERVICE_LEASE_ACQUIRED=1
  (
    trap 'exit 0' INT TERM
    while sleep "$DMRI_SERVICE_LEASE_HEARTBEAT_SECONDS"; do
      kill -0 "$lease_owner_pid" >/dev/null 2>&1 || exit 0
      dmri_service_lease_call heartbeat "$client" >/dev/null 2>&1 || exit 0
    done
  ) &
  DMRI_SERVICE_LEASE_HEARTBEAT_PID=$!
}

dmri_release_service_lease() {
  local client="$1"
  if [[ -n "$DMRI_SERVICE_LEASE_HEARTBEAT_PID" ]]; then
    kill "$DMRI_SERVICE_LEASE_HEARTBEAT_PID" >/dev/null 2>&1 || true
    wait "$DMRI_SERVICE_LEASE_HEARTBEAT_PID" 2>/dev/null || true
    DMRI_SERVICE_LEASE_HEARTBEAT_PID=""
  fi
  if [[ -n "$DMRI_SERVICE_LEASE_ID" ]]; then
    # This may be a bootstrap lease or an acquire whose response was lost.
    # DELETE is idempotent, so every cleanup path makes the best-effort release.
    dmri_service_lease_call release "$client" >/dev/null 2>&1 || true
    DMRI_SERVICE_LEASE_ACQUIRED=0
  fi
  if [[ "$DMRI_SERVICE_PUBLISHED" == "1" || -z "$DMRI_BROKER_CLAIM_ID" ]]; then
    DMRI_SERVICE_LEASE_ID=""
  fi
}

dmri_cleanup_unpublished_service() {
  [[ "$DMRI_SERVICE_PUBLISHED" == "0" ]] || return 0
  [[ -n "$DMRI_BROKER_CLAIM_ID" && -n "${BACKEND_PID:-}" ]] || return 0
  PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m backend.service.runtime_broker owns-claim \
      --claim-id "$DMRI_BROKER_CLAIM_ID" --owner-pid "$$" >/dev/null 2>&1 || return 0
  if kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    kill -TERM "$BACKEND_PID" >/dev/null 2>&1 || true
    local attempt
    for attempt in $(seq 1 30); do
      kill -0 "$BACKEND_PID" >/dev/null 2>&1 || break
      sleep 0.1
    done
    if kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
      kill -KILL "$BACKEND_PID" >/dev/null 2>&1 || true
    fi
  fi
  wait "$BACKEND_PID" 2>/dev/null || true
  dmri_abandon_service_start
}
