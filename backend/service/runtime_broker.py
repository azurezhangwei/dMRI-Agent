from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from backend.project_paths import PROJECT_VERSION
from backend.runtime_state import atomic_write_text
from backend.platform import IS_WINDOWS, access_safe, config_home, state_home, secure_path, pid_alive, process_descends_from


SCHEMA_VERSION = 1


class RuntimeBrokerError(RuntimeError):
    pass


class RuntimeBrokerBusy(RuntimeBrokerError):
    pass


class RuntimeBrokerConflict(RuntimeBrokerError):
    pass


@dataclass(frozen=True)
class ServiceDescriptor:
    base_url: str
    token: str
    pid: int
    version: str
    started_by: str
    managed: bool
    slicer_scene_url: str
    started_at: float
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class StartClaim:
    claim_id: str
    base_url: str
    token: str
    lease_id: str
    started_by: str
    version: str
    owner_pid: int
    slicer_scene_url: str
    created_at: float
    schema_version: int = SCHEMA_VERSION


def runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    state_root = state_home(values)
    path = state_root.resolve() / "runtime"
    _ensure_private_directory(path)
    return path


def service_descriptor_path(environ: Mapping[str, str] | None = None) -> Path:
    return runtime_dir(environ) / "service.json"


def service_start_lock_path(environ: Mapping[str, str] | None = None) -> Path:
    return runtime_dir(environ) / "service-start.json"


def resolve_default_workspace(
    environ: Mapping[str, str] | None = None,
    *,
    create: bool = True,
) -> Path:
    values = os.environ if environ is None else environ
    for key in ("MRI_AGENT_WORKSPACE", "DMRI_DEFAULT_WORKSPACE"):
        raw = str(values.get(key) or "").strip()
        if raw:
            return _require_workspace(Path(raw), source=key)

    install_path = _install_config_path(values)
    if install_path.is_file():
        _validate_private_file(install_path)
        try:
            payload = json.loads(install_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeBrokerError(f"invalid install registry {install_path}: {exc}") from exc
        raw = str(payload.get("workspace") or "").strip() if isinstance(payload, dict) else ""
        if raw:
            return _require_workspace(Path(raw), source=str(install_path))

    configured_workspace = str(values.get("DMRI_WORKSPACE_HOME") or "").strip()
    if configured_workspace:
        workspace = Path(configured_workspace).expanduser()
    else:
        home = Path(values.get("HOME") or str(Path.home())).expanduser()
        workspace = home / "dMRI-Agent-Workspace"
    if create:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _require_workspace(workspace, source="dedicated dMRI workspace")


def install_backend_port(
    environ: Mapping[str, str] | None = None,
    *,
    default: int = 8765,
) -> int:
    values = os.environ if environ is None else environ
    install_path = _install_config_path(values)
    if not install_path.is_file():
        return default
    _validate_private_file(install_path)
    try:
        payload = json.loads(install_path.read_text(encoding="utf-8"))
        port = int(payload.get("backend_port", default)) if isinstance(payload, dict) else default
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default
    return port if 1 <= port <= 65535 else default


def discover_service(
    expected_base_url: str | None = None,
    *,
    expected_version: str | None = PROJECT_VERSION,
    environ: Mapping[str, str] | None = None,
    timeout: float = 0.8,
) -> ServiceDescriptor | None:
    path = service_descriptor_path(environ)
    if not path.exists():
        return None
    payload = _read_private_json(path)
    descriptor = _descriptor_from_payload(payload)
    if not _pid_alive(descriptor.pid):
        _unlink_if_same(path, "pid", descriptor.pid)
        return None

    normalized_expected = _normalize_base_url(expected_base_url) if expected_base_url else None
    if normalized_expected and descriptor.base_url != normalized_expected:
        raise RuntimeBrokerConflict(
            f"managed backend already runs at {descriptor.base_url}; requested {normalized_expected}"
        )
    if expected_version and descriptor.version != expected_version:
        raise RuntimeBrokerConflict(
            f"managed backend version {descriptor.version} is incompatible with {expected_version}"
        )
    try:
        status = _probe_lease_capability(descriptor.base_url, descriptor.token, timeout=timeout)
    except RuntimeBrokerError:
        if _probe_health(descriptor.base_url, timeout=timeout) is None:
            _unlink_if_same(path, "pid", descriptor.pid)
            return None
        raise
    if status.get("version") != descriptor.version:
        raise RuntimeBrokerConflict("runtime descriptor version does not match the live backend")
    if int(status.get("pid") or 0) != descriptor.pid:
        raise RuntimeBrokerConflict("runtime descriptor pid does not match the live backend")
    if status.get("service") != "dmri-agent" or not status.get("service_leases"):
        raise RuntimeBrokerConflict("live backend does not support shared frontend lifecycle")
    if bool(status.get("managed")) != descriptor.managed:
        raise RuntimeBrokerConflict(
            "runtime descriptor lifecycle mode does not match the live backend"
        )
    live_scene_url = _normalize_scene_url(str(status.get("slicer_scene_url") or ""))
    if live_scene_url != descriptor.slicer_scene_url:
        raise RuntimeBrokerConflict(
            "runtime descriptor Slicer scene endpoint does not match the live backend"
        )
    return descriptor


def claim_service_start(
    base_url: str,
    started_by: str,
    *,
    service_token: str | None = None,
    owner_pid: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> StartClaim:
    normalized_url = _normalize_base_url(base_url)
    lock_path = service_start_lock_path(environ)
    claim = StartClaim(
        claim_id=secrets.token_urlsafe(24),
        base_url=normalized_url,
        token=(service_token or secrets.token_urlsafe(32)).strip(),
        lease_id=secrets.token_urlsafe(24),
        started_by=_safe_client_name(started_by),
        version=PROJECT_VERSION,
        owner_pid=int(owner_pid or os.getpid()),
        slicer_scene_url=_normalize_scene_url(
            str((os.environ if environ is None else environ).get(
                "MCP_SLICER_WEB_SERVER_URL", "http://127.0.0.1:2016/slicer"
            ))
        ),
        created_at=time.time(),
    )
    if claim.owner_pid <= 0:
        raise RuntimeBrokerError("owner pid must be positive")
    for _ in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            existing = _read_start_claim(lock_path)
            if _pid_alive(existing.owner_pid):
                raise RuntimeBrokerBusy(
                    f"{existing.started_by} is already starting {existing.base_url}"
                )
            _unlink_if_same(lock_path, "claim_id", existing.claim_id)
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(claim), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            secure_path(lock_path, 0o600)
            return claim
        except Exception:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
    raise RuntimeBrokerBusy("another frontend is starting the backend")


def load_start_claim(
    claim_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> StartClaim:
    claim = _read_start_claim(service_start_lock_path(environ))
    if not secrets.compare_digest(claim.claim_id, claim_id):
        raise RuntimeBrokerError("backend start claim no longer belongs to this frontend")
    return claim


def owns_start_claim(
    claim_id: str,
    owner_pid: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    try:
        claim = load_start_claim(claim_id, environ=environ)
    except (FileNotFoundError, RuntimeBrokerError):
        return False
    return claim.owner_pid == int(owner_pid)


def publish_service(
    claim: StartClaim | str,
    pid: int,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 1.0,
) -> ServiceDescriptor:
    resolved_claim = (
        load_start_claim(claim, environ=environ) if isinstance(claim, str) else claim
    )
    if not _pid_alive(pid):
        raise RuntimeBrokerError("cannot publish a backend process that is not running")
    status = _probe_lease_capability(
        resolved_claim.base_url,
        resolved_claim.token,
        timeout=timeout,
    )
    if status.get("version") != resolved_claim.version:
        raise RuntimeBrokerConflict("started backend version does not match this installation")
    worker_pid = int(status.get("pid") or 0)
    if worker_pid != int(pid) and not (IS_WINDOWS and process_descends_from(worker_pid, int(pid))):
        raise RuntimeBrokerConflict("started backend pid does not match the claimed process")
    if not bool(status.get("managed")) or not status.get("service_leases"):
        raise RuntimeBrokerConflict("started backend is not managed by the shared lifecycle")
    live_scene_url = _normalize_scene_url(str(status.get("slicer_scene_url") or ""))
    if live_scene_url != resolved_claim.slicer_scene_url:
        raise RuntimeBrokerConflict("started backend Slicer scene endpoint does not match its claim")
    descriptor = ServiceDescriptor(
        base_url=resolved_claim.base_url,
        token=resolved_claim.token,
        pid=worker_pid,
        version=resolved_claim.version,
        started_by=resolved_claim.started_by,
        managed=True,
        slicer_scene_url=resolved_claim.slicer_scene_url,
        started_at=time.time(),
    )
    path = service_descriptor_path(environ)
    atomic_write_text(path, json.dumps(asdict(descriptor), sort_keys=True) + "\n", mode=0o600)
    try:
        abandon_service_start(resolved_claim, environ=environ)
    except RuntimeBrokerError:
        # The descriptor is already the authoritative published state. A stale
        # claim is harmless because discovery checks the descriptor first.
        pass
    return descriptor


def abandon_service_start(
    claim: StartClaim | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    claim_id = claim if isinstance(claim, str) else claim.claim_id
    return _unlink_if_same(service_start_lock_path(environ), "claim_id", claim_id)


def clear_service_descriptor(
    pid: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    return _unlink_if_same(service_descriptor_path(environ), "pid", int(pid))


def prepare_service(
    base_url: str,
    client: str,
    owner_pid: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = os.environ if environ is None else environ
    normalized_url = _normalize_base_url(base_url)
    descriptor = discover_service(normalized_url, environ=values)
    if descriptor is not None:
        return {
            "action": "reuse",
            "base_url": descriptor.base_url,
            "token": descriptor.token,
            "lease_id": secrets.token_urlsafe(24),
            "claim_id": "",
            "slicer_scene_url": descriptor.slicer_scene_url,
        }

    start_lock = service_start_lock_path(values)
    if start_lock.exists():
        existing_claim = _read_start_claim(start_lock)
        claim_age = max(0.0, time.time() - existing_claim.created_at)
        if _pid_alive(existing_claim.owner_pid) and claim_age < 120.0:
            return {
                "action": "wait",
                "base_url": normalized_url,
                "token": "",
                "lease_id": "",
                "claim_id": "",
                "slicer_scene_url": existing_claim.slicer_scene_url,
            }
        try:
            status = _probe_lease_capability(
                existing_claim.base_url,
                existing_claim.token,
                timeout=0.8,
            )
        except RuntimeBrokerError:
            status = {}
        recovered_pid = int(status.get("pid") or 0)
        if (
            status.get("version") == existing_claim.version
            and status.get("managed") is True
            and status.get("service_leases") is True
            and _pid_alive(recovered_pid)
        ):
            recovered = publish_service(existing_claim, recovered_pid, environ=values)
            return {
                "action": "reuse",
                "base_url": recovered.base_url,
                "token": recovered.token,
                "lease_id": secrets.token_urlsafe(24),
                "claim_id": "",
                "slicer_scene_url": recovered.slicer_scene_url,
            }
        if claim_age < 60.0:
            return {
                "action": "wait",
                "base_url": normalized_url,
                "token": "",
                "lease_id": "",
                "claim_id": "",
                "slicer_scene_url": existing_claim.slicer_scene_url,
            }
        _unlink_if_same(start_lock, "claim_id", existing_claim.claim_id)

    health = _probe_health(normalized_url, timeout=0.4)
    if health is not None:
        if health.get("service") != "dmri-agent":
            raise RuntimeBrokerConflict(f"another application is using {normalized_url}")
        external_token = str(values.get("DMRI_SERVICE_TOKEN") or "").strip()
        try:
            status = _probe_lease_capability(normalized_url, external_token, timeout=0.8)
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            if start_lock.exists():
                try:
                    concurrent_claim = _read_start_claim(start_lock)
                except (FileNotFoundError, RuntimeBrokerError):
                    concurrent_claim = None
                if concurrent_claim is not None and max(
                    0.0, time.time() - concurrent_claim.created_at
                ) < 120.0:
                    return {
                        "action": "wait",
                        "base_url": normalized_url,
                        "token": "",
                        "lease_id": "",
                        "claim_id": "",
                        "slicer_scene_url": concurrent_claim.slicer_scene_url,
                    }
            raise RuntimeBrokerConflict(
                "a dMRI backend is running without a usable private runtime descriptor"
            ) from exc
        if status.get("version") != PROJECT_VERSION or not status.get("service_leases"):
            raise RuntimeBrokerConflict("running backend is incompatible with this installation")
        if "managed" not in status or not status.get("slicer_scene_url"):
            raise RuntimeBrokerConflict("running backend has an incomplete shared lifecycle contract")
        return {
            "action": "external",
            "base_url": normalized_url,
            "token": external_token,
            "lease_id": secrets.token_urlsafe(24),
            "claim_id": "",
            "slicer_scene_url": _normalize_scene_url(
                str(status.get("slicer_scene_url") or "")
            ),
        }

    try:
        claim = claim_service_start(
            normalized_url,
            client,
            service_token=str(values.get("DMRI_SERVICE_TOKEN") or "").strip() or None,
            owner_pid=owner_pid,
            environ=values,
        )
    except RuntimeBrokerBusy:
        return {
            "action": "wait",
            "base_url": normalized_url,
            "token": "",
            "lease_id": "",
            "claim_id": "",
            "slicer_scene_url": _normalize_scene_url(
                str(values.get("MCP_SLICER_WEB_SERVER_URL") or "http://127.0.0.1:2016/slicer")
            ),
        }
    return {
        "action": "start",
        "base_url": claim.base_url,
        "token": claim.token,
        "lease_id": claim.lease_id,
        "claim_id": claim.claim_id,
        "slicer_scene_url": claim.slicer_scene_url,
    }


def _probe_health(base_url: str, *, timeout: float) -> dict[str, Any] | None:
    if IS_WINDOWS:
        # Windows may delay connection refusal beyond the short health timeout.
        # An exclusive bind proves the loopback port is free; a timed-out HTTP
        # response alone must never authorize starting over another service.
        parsed = urllib.parse.urlsplit(base_url)
        family = socket.AF_INET6 if parsed.hostname == '::1' else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind((parsed.hostname, parsed.port))
            except OSError:
                pass  # Occupied/restricted: inspect its health contract below.
            else:
                return None
    request = urllib.request.Request(f"{base_url}/health", headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError) or getattr(exc.reason, "errno", None) in {
            errno.ECONNREFUSED,
            errno.ETIMEDOUT,
        }:
            return None
        raise RuntimeBrokerConflict(f"cannot inspect {base_url}: {exc.reason}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeBrokerConflict(f"invalid health response from {base_url}: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _probe_lease_capability(base_url: str, token: str, *, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}/v1/service-leases",
        headers={"Accept": "application/json"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeBrokerError(f"backend rejected runtime descriptor credentials (HTTP {exc.code})") from exc
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeBrokerError(f"backend runtime probe failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeBrokerError("backend runtime probe returned invalid data")
    return payload


def _normalize_base_url(value: str | None) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeBrokerError("backend URL must use HTTP on loopback")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeBrokerError("backend URL has an invalid port") from exc
    if port is None or not 1 <= port <= 65535 or parsed.path or parsed.query or parsed.fragment:
        raise RuntimeBrokerError("backend URL must contain only a loopback host and port")
    host = "127.0.0.1" if parsed.hostname in {"127.0.0.1", "localhost"} else "[::1]"
    return f"http://{host}:{port}"


def _normalize_scene_url(value: str | None) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path.rstrip("/") != "/slicer"
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeBrokerError("Slicer scene URL must use a loopback HTTP /slicer endpoint")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeBrokerError("Slicer scene URL has an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise RuntimeBrokerError("Slicer scene URL must include a valid port")
    host = "127.0.0.1" if parsed.hostname in {"127.0.0.1", "localhost"} else "[::1]"
    return f"http://{host}:{port}/slicer"


def _descriptor_from_payload(payload: dict[str, Any]) -> ServiceDescriptor:
    try:
        descriptor = ServiceDescriptor(
            base_url=_normalize_base_url(str(payload["base_url"])),
            token=str(payload["token"]),
            pid=int(payload["pid"]),
            version=str(payload["version"]),
            started_by=_safe_client_name(str(payload["started_by"])),
            managed=bool(payload["managed"]),
            slicer_scene_url=_normalize_scene_url(str(payload["slicer_scene_url"])),
            started_at=float(payload["started_at"]),
            schema_version=int(payload["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeBrokerError("invalid runtime service descriptor") from exc
    if descriptor.schema_version != SCHEMA_VERSION:
        raise RuntimeBrokerError("unsupported runtime service descriptor")
    if descriptor.pid <= 0 or not descriptor.token:
        raise RuntimeBrokerError("invalid runtime service descriptor identity")
    return descriptor


def _claim_from_payload(payload: dict[str, Any]) -> StartClaim:
    try:
        claim = StartClaim(
            claim_id=str(payload["claim_id"]),
            base_url=_normalize_base_url(str(payload["base_url"])),
            token=str(payload["token"]),
            lease_id=str(payload["lease_id"]),
            started_by=_safe_client_name(str(payload["started_by"])),
            version=str(payload["version"]),
            owner_pid=int(payload["owner_pid"]),
            slicer_scene_url=_normalize_scene_url(str(payload["slicer_scene_url"])),
            created_at=float(payload["created_at"]),
            schema_version=int(payload["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeBrokerError("invalid backend start claim") from exc
    if claim.schema_version != SCHEMA_VERSION or not claim.claim_id or not claim.token:
        raise RuntimeBrokerError("unsupported backend start claim")
    return claim


def _safe_client_name(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned or len(cleaned) > 32 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in cleaned):
        raise RuntimeBrokerError("invalid frontend client name")
    return cleaned


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeBrokerError(f"runtime path is not a private directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeBrokerError(f"runtime directory is owned by another user: {path}")
    if IS_WINDOWS or stat.S_IMODE(info.st_mode) & 0o077:
        secure_path(path, 0o700)


def _validate_private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeBrokerError(f"runtime file is not regular: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeBrokerError(f"runtime file is owned by another user: {path}")
    if not access_safe(path, private=True):
        raise RuntimeBrokerError(f"runtime file permissions are not private: {path}")


def _read_private_json(path: Path) -> dict[str, Any]:
    _validate_private_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeBrokerError(f"invalid runtime file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeBrokerError(f"invalid runtime file {path}")
    return payload


def _read_start_claim(path: Path) -> StartClaim:
    """Read a just-created O_EXCL claim after its small JSON write completes."""
    last_error: RuntimeBrokerError | None = None
    for attempt in range(20):
        try:
            return _claim_from_payload(_read_private_json(path))
        except FileNotFoundError:
            raise
        except RuntimeBrokerError as exc:
            last_error = exc
            if attempt < 19:
                time.sleep(0.01)
    assert last_error is not None
    raise last_error


def _unlink_if_same(path: Path, key: str, expected: object) -> bool:
    try:
        before = path.lstat()
        payload = _read_private_json(path)
    except FileNotFoundError:
        return False
    if payload.get(key) != expected:
        return False
    try:
        after = path.lstat()
    except FileNotFoundError:
        return False
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        return False
    path.unlink()
    return True


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _install_config_path(values: Mapping[str, str]) -> Path:
    explicit = str(values.get("DMRI_AGENT_INSTALL_CONFIG") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return config_home(values) / "install.json"


def _require_workspace(path: Path, *, source: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeBrokerError(f"workspace from {source} was not found: {path}") from exc
    if not resolved.is_dir():
        raise RuntimeBrokerError(f"workspace from {source} is not a directory: {resolved}")
    return resolved


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coordinate the shared dMRI backend")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--base-url", required=True)
    prepare.add_argument("--client", required=True)
    prepare.add_argument("--owner-pid", required=True, type=int)
    prepare.add_argument("--format", choices=("json", "pipe", "shell"), default="json")
    publish = subparsers.add_parser("publish")
    publish.add_argument("--claim-id", required=True)
    publish.add_argument("--pid", required=True, type=int)
    abandon = subparsers.add_parser("abandon")
    abandon.add_argument("--claim-id", required=True)
    clear = subparsers.add_parser("clear")
    clear.add_argument("--pid", required=True, type=int)
    owns = subparsers.add_parser("owns-claim")
    owns.add_argument("--claim-id", required=True)
    owns.add_argument("--owner-pid", required=True, type=int)
    subparsers.add_parser("workspace")
    subparsers.add_parser("backend-port")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare_service(args.base_url, args.client, args.owner_pid)
        if args.format == "pipe":
            print("|".join(str(result[key]) for key in ("action", "base_url", "token", "lease_id", "claim_id", "slicer_scene_url")))
        elif args.format == "shell":
            import shlex

            for key in ("action", "base_url", "token", "lease_id", "claim_id", "slicer_scene_url"):
                print(f"DMRI_BROKER_{key.upper()}={shlex.quote(str(result[key]))}")
        else:
            print(json.dumps(result, sort_keys=True))
    elif args.command == "publish":
        print(json.dumps(asdict(publish_service(args.claim_id, args.pid)), sort_keys=True))
    elif args.command == "abandon":
        print(json.dumps({"removed": abandon_service_start(args.claim_id)}))
    elif args.command == "owns-claim":
        if not owns_start_claim(args.claim_id, args.owner_pid):
            return 1
        print(json.dumps({"owned": True}))
    elif args.command == "clear":
        print(json.dumps({"removed": clear_service_descriptor(args.pid)}))
    elif args.command == "backend-port":
        print(install_backend_port())
    else:
        print(resolve_default_workspace())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except RuntimeBrokerError as exc:
        raise SystemExit(f"runtime broker error: {exc}") from exc
