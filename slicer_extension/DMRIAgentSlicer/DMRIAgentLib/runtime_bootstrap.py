from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
_app_root = str(Path(__file__).resolve().parents[3])
if _app_root not in sys.path:
    sys.path.insert(0, _app_root)
from backend.platform import IS_WINDOWS, access_safe, config_home, state_home, secure_path, venv_python, spawn_options, python_launch_path
from typing import Any, Callable, Mapping, Optional
from urllib import error, request


@dataclass(frozen=True)
class RuntimeConfig:
    app_dir: Optional[Path]
    backend_port: int
    workspace: Optional[Path]
    web_url: str
    auto_start: bool
    source: str
    scene_port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.backend_port}"


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: str
    message: str
    base_url: str
    owned_process: bool
    app_dir: str
    lease_active: bool
    scene_url: str


@dataclass(frozen=True)
class ProbeResult:
    state: str
    message: str = ""
    payload: Optional[dict[str, Any]] = None


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _parse_port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _parse_install_record(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("install registry must contain a JSON object")
        return payload
    payload: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def _registry_is_private(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return False
    return access_safe(path)


def _candidate_registry_paths(environ: Mapping[str, str]) -> list[Path]:
    explicit = str(environ.get("DMRI_AGENT_INSTALL_CONFIG") or "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    root = config_home(environ)
    return [
        root / "install.json",
        root / "slicer.json",
        root / "install.conf",
    ]


def _valid_app_dir(path: Path) -> Optional[Path]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    service = resolved / ("backend/service/__main__.py" if IS_WINDOWS else "scripts/launchers/dmri_agent_service")
    if not resolved.is_dir() or not service.is_file() or (not IS_WINDOWS and not os.access(service, os.X_OK)):
        return None
    if not (resolved / "backend").is_dir() or not (resolved / "VERSION").is_file():
        return None
    return resolved


def _discover_bundled_app(module_file: Path) -> Optional[Path]:
    try:
        resolved = module_file.resolve(strict=True)
    except OSError:
        return None
    for parent in resolved.parents:
        candidate = _valid_app_dir(parent)
        if candidate is not None:
            return candidate
    return None


def load_runtime_config(
    *,
    environ: Optional[Mapping[str, str]] = None,
    module_file: Optional[Path] = None,
) -> RuntimeConfig:
    values = dict(os.environ if environ is None else environ)
    record: dict[str, Any] = {}
    source = "defaults"

    for path in _candidate_registry_paths(values):
        if not path.is_file():
            continue
        if not _registry_is_private(path):
            source = f"ignored unsafe registry {path}"
            break
        try:
            record = _parse_install_record(path)
            source = str(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            source = f"invalid registry {path}: {exc}"
        break

    raw_app_dir = str(values.get("DMRI_AGENT_APP_DIR") or record.get("app_dir") or "").strip()
    app_dir = _valid_app_dir(Path(raw_app_dir)) if raw_app_dir else None
    if app_dir is None and module_file is not None:
        app_dir = _discover_bundled_app(module_file)
        if app_dir is not None:
            source = f"bundled module at {app_dir}"

    raw_port = values.get("DMRI_SERVICE_PORT") or record.get("backend_port") or 8765
    backend_port = _parse_port(raw_port, 8765)
    scene_port = _parse_port(values.get("DMRI_SLICER_SCENE_PORT"), 2016)

    raw_workspace = str(
        values.get("MRI_AGENT_WORKSPACE")
        or values.get("DMRI_DEFAULT_WORKSPACE")
        or record.get("workspace")
        or ""
    )
    workspace: Optional[Path] = None
    if raw_workspace:
        candidate_workspace = Path(raw_workspace).expanduser()
        try:
            candidate_workspace = candidate_workspace.resolve(strict=True)
        except OSError:
            candidate_workspace = Path("/__dmri_workspace_not_found__")
        if candidate_workspace.is_dir():
            workspace = candidate_workspace

    web_url = str(
        values.get("DMRI_WEB_URL")
        or record.get("web_url")
        or "http://127.0.0.1:3000/?settings=model"
    ).strip()
    auto_start = _parse_bool(
        values.get(
            "DMRI_SLICER_AUTO_START_BACKEND",
            record.get("auto_start_backend", record.get("auto_start")),
        ),
        True,
    )
    return RuntimeConfig(
        app_dir=app_dir,
        backend_port=backend_port,
        workspace=workspace,
        web_url=web_url,
        auto_start=auto_start,
        source=source,
        scene_port=scene_port,
    )


def probe_backend(base_url: str, _token: str = "") -> ProbeResult:
    req = request.Request(f"{base_url.rstrip('/')}/health", headers={"Accept": "application/json"})
    try:
        with request.build_opener(request.ProxyHandler({})).open(req, timeout=0.6) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except error.HTTPError as exc:
        return ProbeResult("conflict", f"HTTP {exc.code} on the backend port")
    except error.URLError as exc:
        return ProbeResult("unavailable", str(exc.reason))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ProbeResult("unavailable", str(exc))
    if not isinstance(payload, dict) or payload.get("service") != "dmri-agent":
        return ProbeResult("conflict", "another application is using the backend port", payload)
    if not bool(payload.get("ok")):
        return ProbeResult("conflict", "dMRI-Agent health check did not report ready", payload)
    return ProbeResult("ready", "dMRI-Agent service is ready", payload)


class SlicerRuntimeManager:
    def __init__(
        self,
        *,
        environ: Optional[Mapping[str, str]] = None,
        module_file: Optional[Path] = None,
        probe: Callable[[str, str], ProbeResult] = probe_backend,
        process_factory: Optional[Callable[..., Any]] = None,
        lifecycle_loader: Optional[Callable[[], tuple[Any, Any]]] = None,
        startup_timeout: float = 20.0,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.config = load_runtime_config(environ=self.environ, module_file=module_file)
        if IS_WINDOWS and self.config.app_dir is not None:
            # Keep the ordinary Slicer process on the same installed state and
            # credential locations as an installer hosted by an MSIX app.
            for registry in _candidate_registry_paths(self.environ):
                if not registry.is_file() or not _registry_is_private(registry):
                    continue
                try:
                    record = _parse_install_record(registry)
                except (OSError, ValueError):
                    break
                recorded_app = str(record.get("app_dir") or "")
                if recorded_app and _valid_app_dir(Path(recorded_app)) == self.config.app_dir:
                    if record.get("state_home"):
                        self.environ.setdefault("DMRI_STATE_HOME", str(record["state_home"]))
                    if record.get("config_home"):
                        self.environ.setdefault("DMRI_SECRETS_FILE", str(Path(record["config_home"]) / "secrets.json"))
                    self.environ.setdefault("DMRI_AGENT_INSTALL_CONFIG", str(registry.resolve()))
                break
        self._probe = probe
        self._process_factory = process_factory or self._spawn_process
        self._lifecycle_loader = lifecycle_loader
        self._startup_timeout = max(0.1, float(startup_timeout))
        self._lock = threading.RLock()
        self._process: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._state = "idle"
        self._message = "Backend has not been checked yet."
        self._token = str(self.environ.get("DMRI_SERVICE_TOKEN") or "").strip()
        self._lease: Any = None
        self._broker: Any = None

    @property
    def base_url(self) -> str:
        configured = str(self.environ.get("MRI_AGENT_API_BASE") or "").strip()
        return configured.rstrip("/") or self.config.base_url

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    @property
    def web_url(self) -> str:
        return self.config.web_url

    @property
    def scene_url(self) -> str:
        return f"http://127.0.0.1:{self.config.scene_port}/slicer"

    def scene_token_for_session(self, session_id: str) -> str:
        """Derive the private scene credential without persisting or exposing it."""

        app_dir = self.config.app_dir
        if app_dir is None:
            raise RuntimeError("installed dMRI-Agent app directory is unavailable")
        app_text = str(app_dir)
        if app_text not in sys.path:
            sys.path.insert(0, app_text)
        from backend.service.scene_auth import derive_scene_auth_token

        return derive_scene_auth_token(self.token, session_id)

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            process = self._process
            owned = process is not None and process.poll() is None
            return RuntimeSnapshot(
                state=self._state,
                message=self._message,
                base_url=self.base_url,
                owned_process=owned,
                app_dir=str(self.config.app_dir or ""),
                lease_active=self._lease is not None,
                scene_url=self.scene_url,
            )

    def start_async(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._state == "ready":
                return
            self._stopping = False
            self._state = "checking"
            self._message = "Checking the dMRI-Agent backend…"
            self._thread = threading.Thread(
                target=self.ensure_started,
                daemon=True,
                name="dmri-slicer-backend-bootstrap",
            )
            self._thread.start()

    def ensure_runtime_async(self) -> None:
        """Start discovery on demand after the user selects this module."""
        self.start_async()

    def retry_async(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._state = "idle"
        self.start_async()

    def set_workspace(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser().resolve(strict=True)
        if not candidate.is_dir():
            raise ValueError(f"Workspace is not a directory: {candidate}")
        if candidate == Path(candidate.anchor) or "\n" in str(candidate) or "\r" in str(candidate):
            raise ValueError("Choose a workspace folder other than the filesystem root, without line breaks.")
        if self.config.app_dir is not None and candidate.is_relative_to(self.config.app_dir):
            raise ValueError("Choose a workspace outside the dMRI-Agent application folder.")
        with self._lock:
            self._save_workspace(candidate)
            self.config = replace(self.config, workspace=candidate)
            if self._state == "workspace_required":
                self._state = "idle"
                self._message = "Workspace selected; backend has not been checked yet."
        return candidate

    def _save_workspace(self, workspace: Path) -> None:
        """Keep the shared installation default in sync with an explicit UI choice."""
        for registry in _candidate_registry_paths(self.environ):
            if not registry.exists() and not registry.is_symlink():
                continue
            if registry.is_symlink() or not _registry_is_private(registry):
                raise ValueError("The installation settings must be a private regular file.")
            original = registry.read_text(encoding="utf-8")
            record = _parse_install_record(registry)
            if record.get("workspace") == str(workspace):
                return
            record["workspace"] = str(workspace)
            if registry.suffix.lower() == ".json" or original.lstrip().startswith("{"):
                updated = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
            else:
                lines = [line for line in original.splitlines()
                         if line.split("=", 1)[0].strip() != "workspace"]
                updated = "\n".join([*lines, f"workspace={workspace}"]) + "\n"
            descriptor, name = tempfile.mkstemp(prefix=".dmri-workspace-", dir=registry.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                secure_path(temporary, stat.S_IMODE(registry.stat().st_mode))
                temporary.replace(registry)
            finally:
                temporary.unlink(missing_ok=True)
            return

    def ensure_started(self) -> RuntimeSnapshot:
        if self.config.workspace is None:
            self._set_state(
                "workspace_required",
                "Choose a dMRI workspace in Settings before starting the Agent.",
            )
            return self.snapshot()
        if self.config.app_dir is None:
            self._set_state(
                "install_required",
                "dMRI-Agent core installation was not found. Reinstall dMRI-Agent or set DMRI_AGENT_APP_DIR.",
            )
            return self.snapshot()
        # Settings may request a retry even while the shared service is ready.
        # Keep this frontend's live lease instead of orphaning another heartbeat
        # thread on every Save & Test.
        with self._lock:
            existing_lease = self._lease
            existing_token = self._token
        if (
            existing_lease is not None
            and existing_lease.snapshot().get("active")
            and self._probe(self.base_url, existing_token).state == "ready"
        ):
            with self._lock:
                if self._stopping:
                    return self.snapshot()
                if self._lease is existing_lease:
                    self._state = "ready"
                    self._message = "Shared dMRI-Agent backend is ready."
                    return self.snapshot()
        broker_environment = dict(self.environ)
        broker_environment["MCP_SLICER_WEB_SERVER_URL"] = self.scene_url
        try:
            broker, lease_type = (
                self._lifecycle_loader()
                if self._lifecycle_loader is not None
                else self._load_shared_lifecycle()
            )
            prepared = broker.prepare_service(
                self.base_url,
                "slicer",
                os.getpid(),
                environ=broker_environment,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_state("conflict", f"Could not coordinate the shared backend: {exc}")
            return self.snapshot()

        action = str(prepared.get("action") or "")
        wait_deadline = time.monotonic() + self._startup_timeout
        while action == "wait" and time.monotonic() < wait_deadline:
            if self._stopping:
                return self.snapshot()
            time.sleep(0.1)
            try:
                prepared = broker.prepare_service(
                    self.base_url,
                    "slicer",
                    os.getpid(),
                    environ=broker_environment,
                )
            except Exception as exc:  # noqa: BLE001
                self._set_state("conflict", f"Could not coordinate the shared backend: {exc}")
                return self.snapshot()
            action = str(prepared.get("action") or "")
        if action == "wait":
            self._set_state("error", "Timed out waiting for another frontend to start the backend.")
            return self.snapshot()

        token = str(prepared.get("token") or "")
        lease_id = str(prepared.get("lease_id") or "")
        claim_id = str(prepared.get("claim_id") or "")
        with self._lock:
            self._token = token
            self._broker = broker

        actual_scene_url = str(prepared.get("slicer_scene_url") or "")
        if actual_scene_url != self.scene_url:
            if action == "start":
                self._abandon_owned_start(broker, claim_id, None)
            self._set_state(
                "conflict",
                "The shared backend is configured for Slicer scene access at "
                f"{actual_scene_url or 'an unknown endpoint'}, but this Slicer requested "
                f"{self.scene_url}. Close the other dMRI-Agent frontends and retry with "
                "the same scene port.",
            )
            return self.snapshot()

        process = None
        if action == "start":
            if not self.config.auto_start:
                self._abandon_owned_start(broker, claim_id, None)
                self._set_state("offline", "Backend auto-start is disabled.")
                return self.snapshot()
            process = self._start_claimed_service(broker, claim_id, lease_id, token)
            if process is None:
                return self.snapshot()
        elif action not in {"reuse", "external"}:
            self._set_state("error", f"Runtime broker returned unsupported action: {action}")
            return self.snapshot()

        lease = lease_type(
            str(prepared.get("base_url") or self.base_url),
            lease_id,
            "slicer",
            token=token,
        )
        with self._lock:
            stopping = self._stopping
        if stopping:
            try:
                lease.stop(release=True)
            except Exception:
                pass
            return self.snapshot()
        try:
            lease.start()
        except Exception as exc:  # noqa: BLE001
            # The process is already published at this point. Never terminate it:
            # release the bootstrap lease id best-effort and let the shared
            # lifecycle decide when the managed service should exit.
            try:
                lease.stop(release=True)
            except Exception:
                pass
            with self._lock:
                self._process = None
            self._set_state("error", f"Could not acquire the Slicer service lease: {exc}")
            return self.snapshot()
        with self._lock:
            stopping = self._stopping
        if stopping:
            try:
                lease.stop(release=True)
            except Exception:
                pass
            return self.snapshot()
        with self._lock:
            previous_lease = self._lease
            self._lease = lease
            if process is not None:
                self._process = process
        if previous_lease is not None and previous_lease is not lease:
            try:
                previous_lease.stop(release=True)
            except Exception:
                pass
        self._set_state(
            "ready",
            "Shared dMRI-Agent backend is ready."
            if action != "external"
            else "Connected to a compatible external dMRI-Agent backend.",
        )
        return self.snapshot()

    def _start_claimed_service(
        self,
        broker,
        claim_id: str,
        lease_id: str,
        token: str,
    ):
        service = self.config.app_dir / "scripts/launchers/dmri_agent_service"
        child_environment = dict(self.environ)
        child_environment.update(
            {
                "DMRI_SERVICE_TOKEN": token,
                "DMRI_MANAGED_SERVICE": "1",
                "DMRI_SERVICE_BOOTSTRAP_LEASE_ID": lease_id,
                "DMRI_SERVICE_BOOTSTRAP_LEASE_CLIENT": "slicer",
                "MRI_AGENT_WORKSPACE": str(self.config.workspace),
                "DMRI_DEFAULT_WORKSPACE": str(self.config.workspace),
                "MCP_SLICER_WEB_SERVER_URL": self.scene_url,
            }
        )
        command = [str(service), "--host", "127.0.0.1", "--port", str(self.config.backend_port)]
        if IS_WINDOWS:
            python = python_launch_path(venv_python(self.config.app_dir))
            if not python.is_file():
                raise RuntimeError("The installed Agent Python environment is missing.")
            command = [str(python), "-m", "backend.service"]
            for key in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
                child_environment.pop(key, None)
            child_environment.update(PYTHONPATH=str(self.config.app_dir), PYTHONNOUSERSITE="1",
                                     PYTHONUTF8="1", DMRI_AGENT_PYTHON=str(python),
                                     DMRI_SERVICE_HOST="127.0.0.1", DMRI_SERVICE_PORT=str(self.config.backend_port))
            child_environment["PATH"] = str(python.parent) + os.pathsep + child_environment.get("PATH", "")
        try:
            process = self._process_factory(
                command=command,
                cwd=self.config.app_dir,
                env=child_environment,
                log_path=self._log_path(),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._abandon_owned_start(broker, claim_id, None)
            self._set_state("error", f"Could not start the dMRI-Agent backend: {exc}")
            return None

        with self._lock:
            stopping = self._stopping
        if stopping:
            self._abandon_owned_start(broker, claim_id, process)
            return None
        with self._lock:
            self._process = process
            self._token = token
            self._state = "starting"
            self._message = "Starting the installed dMRI-Agent backend…"

        deadline = time.monotonic() + self._startup_timeout
        last_message = "no response"
        while time.monotonic() < deadline:
            with self._lock:
                stopping = self._stopping
            if stopping:
                self._abandon_owned_start(broker, claim_id, process)
                return None
            if process.poll() is not None:
                self._set_state(
                    "error",
                    f"The dMRI-Agent backend exited during startup (code {process.returncode}).",
                )
                self._abandon_owned_start(broker, claim_id, process)
                return None
            result = self._probe(self.base_url, token)
            last_message = result.message or last_message
            if result.state == "ready":
                try:
                    broker.publish_service(
                        claim_id,
                        int(process.pid),
                        environ=self.environ,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._set_state("error", f"Could not publish shared backend state: {exc}")
                    self._abandon_owned_start(broker, claim_id, process)
                    return None
                return process
            if result.state == "conflict":
                self._set_state("conflict", result.message)
                self._abandon_owned_start(broker, claim_id, process)
                return None
            time.sleep(0.1)

        self._set_state("error", f"Backend startup timed out: {last_message}")
        self._abandon_owned_start(broker, claim_id, process)
        return None

    def stop(self) -> None:
        """Release this Slicer lease without terminating a shared backend."""
        with self._lock:
            self._stopping = True
            lease = self._lease
            self._lease = None
            self._process = None
        if lease is not None:
            try:
                lease.stop(release=True)
            except Exception:
                pass
        self._set_state("stopped", "Slicer released its shared backend lease.")

    def _load_shared_lifecycle(self):
        app_dir = self.config.app_dir
        if app_dir is None:
            raise RuntimeError("installed dMRI-Agent app directory is unavailable")
        app_text = str(app_dir)
        if app_text not in sys.path:
            sys.path.insert(0, app_text)
        from backend.service import runtime_broker
        from backend.service.lease_client import ServiceLeaseClient

        return runtime_broker, ServiceLeaseClient

    @staticmethod
    def _terminate_unpublished_process(process: Any) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _abandon_owned_start(self, broker, claim_id: str, process: Any) -> None:
        """Terminate only while this process still owns the unpublished claim."""
        try:
            owned = broker.owns_start_claim(
                claim_id,
                os.getpid(),
                environ=self.environ,
            )
        except Exception:
            owned = False
        if not owned:
            return
        self._terminate_unpublished_process(process)
        try:
            broker.abandon_service_start(claim_id, environ=self.environ)
        except Exception:
            pass

    def _set_state(self, state: str, message: str) -> None:
        with self._lock:
            self._state = state
            self._message = message

    def _log_path(self) -> Path:
        if IS_WINDOWS:
            return state_home(self.environ) / "service" / f"backend-{self.config.backend_port}.log"
        home = Path(self.environ.get("HOME") or str(Path.home())).expanduser()
        state_root = Path(
            self.environ.get("DMRI_STATE_HOME")
            or (
                str(Path(self.environ["XDG_DATA_HOME"]).expanduser() / "dmri-agent")
                if self.environ.get("XDG_DATA_HOME")
                else str(home / ".local" / "share" / "dmri-agent")
            )
        ).expanduser()
        return state_root / "service" / f"backend-{self.config.backend_port}.log"

    @staticmethod
    def _spawn_process(*, command: list[str], cwd: Path, env: dict[str, str], log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log_handle = os.fdopen(descriptor, "ab", buffering=0)
        try:
            return subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **spawn_options(),
                close_fds=True,
            )
        finally:
            log_handle.close()


_RUNTIME_MANAGER: Optional[SlicerRuntimeManager] = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime_manager(module_file: Optional[Path] = None) -> SlicerRuntimeManager:
    global _RUNTIME_MANAGER
    with _RUNTIME_LOCK:
        if _RUNTIME_MANAGER is None:
            _RUNTIME_MANAGER = SlicerRuntimeManager(module_file=module_file or Path(__file__))
        return _RUNTIME_MANAGER


def peek_runtime_manager() -> Optional[SlicerRuntimeManager]:
    """Return the process singleton only if a widget already created it."""

    with _RUNTIME_LOCK:
        return _RUNTIME_MANAGER
