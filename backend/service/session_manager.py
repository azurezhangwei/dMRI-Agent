from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
import warnings
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence
from backend.platform import spawn_options, terminate_tree

from backend.model_config import (
    ModelConfig,
    SecretStore,
    profile_environment,
    resolve_profile_api_key,
)
from backend.project_paths import PROJECT_ROOT
from backend.rollout_log import iso_ts, project_log_key, redact_log_value, resolve_log_root, write_jsonl
from backend.service.scene_auth import derive_scene_auth_token


class SessionError(RuntimeError):
    pass


class SessionNotFound(SessionError):
    pass


class SessionSetupRequired(SessionError):
    pass


class SessionBusy(SessionError):
    pass


@dataclass
class ManagedSession:
    session_id: str
    workspace: Path
    profile_id: str
    process: asyncio.subprocess.Process
    created_at: str
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=4000))
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    event_counter: int = 0
    closed: bool = False
    return_code: int | None = None
    closed_event: asyncio.Event = field(default_factory=asyncio.Event)
    phase: str = "idle"
    current_turn_id: str | None = None
    current_plan_id: str | None = None
    log_secrets: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        last_event_id = str(self.events[-1].get("event_id") or "") if self.events else ""
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace),
            "profile_id": self.profile_id,
            "created_at": self.created_at,
            "closed": self.closed,
            "return_code": self.return_code,
            "phase": self.phase,
            "busy": self.phase != "idle",
            "current_turn_id": self.current_turn_id,
            "last_event_id": last_event_id or None,
        }


class SessionManager:
    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        python_executable: str | None = None,
        model_config_path: Path | None = None,
        secrets_path: Path | None = None,
        log_root: Path | None = None,
        agent_command: Sequence[str] | None = None,
        require_model_credential: bool | None = None,
        heartbeat_seconds: float | None = None,
        max_closed_sessions: int | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.python_executable = python_executable or os.environ.get("DMRI_AGENT_PYTHON") or sys.executable
        self.model_config_path = model_config_path
        self.secrets = SecretStore(secrets_path)
        configured_log_root = (
            log_root
            or resolve_log_root()
        )
        self.log_root = configured_log_root.expanduser().resolve()
        self.agent_command = list(agent_command) if agent_command else None
        self.require_model_credential = (
            self.agent_command is None
            if require_model_credential is None
            else bool(require_model_credential)
        )
        self.heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else float(os.environ.get("DMRI_SSE_HEARTBEAT_SECONDS", "15"))
        )
        self.max_closed_sessions = (
            max_closed_sessions
            if max_closed_sessions is not None
            else int(os.environ.get("DMRI_MAX_CLOSED_SESSIONS", "50"))
        )
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()
        self._workspace_locks: dict[Path, asyncio.Lock] = {}
        self._shutting_down = False

    def model_config(self) -> ModelConfig:
        return ModelConfig.load(self.model_config_path)

    def get(self, session_id: str) -> ManagedSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise SessionNotFound(f"session not found: {session_id}") from exc

    def list_sessions(self) -> list[dict[str, Any]]:
        return [session.public_dict() for session in self._sessions.values()]

    def active_session_for_workspace(
        self, workspace: str | os.PathLike[str]
    ) -> ManagedSession | None:
        workspace_path = Path(workspace).expanduser().resolve()
        return next(
            (
                session
                for session in self._sessions.values()
                if session.workspace == workspace_path and not session.closed
            ),
            None,
        )

    def clear_event_history(self, session_id: str) -> None:
        """Drop replayable events while preserving the monotonic event counter."""
        self.get(session_id).events.clear()

    async def create_session(
        self,
        *,
        workspace: str | os.PathLike[str],
        profile_id: str | None = None,
        auto_approve_tools: bool = False,
        replace_existing: bool = False,
    ) -> ManagedSession:
        if self._shutting_down:
            raise SessionError("service is shutting down")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise SessionError(f"workspace is not a directory: {workspace_path}")
        workspace_lock = self._workspace_locks.setdefault(workspace_path, asyncio.Lock())
        async with workspace_lock:
            existing = self.active_session_for_workspace(workspace_path)
            if existing is not None and not replace_existing:
                return existing
            if existing is not None:
                await self.terminate(existing.session_id)
            return await self._spawn_session(
                workspace_path=workspace_path,
                profile_id=profile_id,
                auto_approve_tools=auto_approve_tools,
            )

    async def _spawn_session(
        self,
        *,
        workspace_path: Path,
        profile_id: str | None,
        auto_approve_tools: bool,
    ) -> ManagedSession:
        config = self.model_config()
        profile = config.select(profile_id)
        environment = os.environ.copy()
        if self.require_model_credential and not resolve_profile_api_key(
            profile,
            environment,
            self.secrets,
        ):
            raise SessionSetupRequired(
                f"model credential required for profile: {profile.profile_id}"
            )
        environment.update(
            profile_environment(
                profile.profile_id,
                config=config,
                environ=environment,
                secrets=self.secrets,
            )
        )
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(self.project_root) + (
            os.pathsep + current_pythonpath if current_pythonpath else ""
        )
        environment["PYTHONUNBUFFERED"] = "1"
        environment["DMRI_MODEL_CONFIG"] = str(config.path)
        environment["DMRI_SECRETS_FILE"] = str(self.secrets.path)
        session_id = str(uuid.uuid4())
        environment["LOG_SESSION_ID"] = session_id
        environment["LOG_ROOT_DIR"] = str(self.log_root)
        environment["DMRI_LOG_ROOT"] = str(self.log_root)
        environment["LOG_DIR"] = str(self.log_root)

        command = self.agent_command or [self.python_executable, "-m", "backend.agent_server"]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace_path),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_options(),
        )
        session = ManagedSession(
            session_id=session_id,
            workspace=workspace_path,
            profile_id=profile.profile_id,
            process=process,
            created_at=datetime.now(timezone.utc).isoformat(),
            log_secrets=tuple(self.secrets.load().values()) + (
                derive_scene_auth_token(environment["DMRI_SERVICE_TOKEN"], session_id),
            ) if environment.get("DMRI_SERVICE_TOKEN") else tuple(self.secrets.load().values()),
        )
        async with self._lock:
            self._prune_closed_sessions_locked()
            self._sessions[session.session_id] = session
        session.tasks.update(
            {
                asyncio.create_task(self._read_stdout(session)),
                asyncio.create_task(self._read_stderr(session)),
                asyncio.create_task(self._wait_for_exit(session)),
            }
        )
        await self._publish(
            session,
            {
                "type": "session_ready",
                "value": session.public_dict(),
            },
        )
        if auto_approve_tools:
            await self.send_payload(session.session_id, {"type": "command", "name": "auto_approve_on"})
        return session

    async def send_payload(self, session_id: str, payload: Mapping[str, Any]) -> None:
        session = self.get(session_id)
        if session.closed or session.process.stdin is None:
            raise SessionError(f"session is closed: {session_id}")
        encoded = json.dumps(dict(payload), ensure_ascii=True) + "\n"
        session.process.stdin.write(encoded.encode("utf-8"))
        await session.process.stdin.drain()

    async def send_user_input(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        turn_id: str,
    ) -> str:
        session = self.get(session_id)
        async with self._lock:
            if session.closed or session.process.stdin is None:
                raise SessionError(f"session is closed: {session_id}")
            if session.phase != "idle":
                raise SessionBusy(
                    "Session is busy with another turn. Wait for it to finish or "
                    "review its pending plan before sending a new message."
                )
            session.phase = "running"
            session.current_turn_id = turn_id
            session.current_plan_id = None
        try:
            await self.send_payload(
                session_id,
                {**dict(payload), "type": "user_input", "turn_id": turn_id},
            )
        except Exception:
            async with self._lock:
                if session.current_turn_id == turn_id and session.phase == "running":
                    self._reset_turn(session)
            raise
        return turn_id

    async def send_plan_response(
        self,
        session_id: str,
        *,
        plan_id: str,
        approved: bool,
        feedback: str,
    ) -> None:
        session = self.get(session_id)
        async with self._lock:
            if session.phase != "awaiting_plan" or not session.current_turn_id:
                raise SessionError("session has no pending plan to review")
            if session.current_plan_id and plan_id != session.current_plan_id:
                raise SessionError("plan response does not match the pending plan")
            turn_id = session.current_turn_id
            # Reserve the session before writing so two frontends cannot both
            # approve/reject the same plan.
            session.phase = "running"
        try:
            await self.send_payload(
                session_id,
                {
                    "type": "plan_response",
                    "planId": plan_id,
                    "approved": approved,
                    "feedback": feedback,
                    "turn_id": turn_id,
                },
            )
        except Exception:
            async with self._lock:
                if session.current_turn_id == turn_id and session.phase == "running":
                    session.phase = "awaiting_plan"
            raise

    async def terminate(self, session_id: str) -> None:
        session = self.get(session_id)
        if session.closed:
            return
        try:
            await self.send_payload(session_id, {"type": "shutdown"})
            await asyncio.wait_for(session.process.wait(), timeout=3.0)
            await asyncio.wait_for(session.closed_event.wait(), timeout=1.0)
            return
        except (SessionError, asyncio.TimeoutError):
            pass
        if session.process.returncode is None:
            terminate_tree(session.process)
        try:
            await asyncio.wait_for(session.process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            if session.process.returncode is None:
                terminate_tree(session.process, force=True)
                await session.process.wait()
        await asyncio.wait_for(session.closed_event.wait(), timeout=1.0)

    async def shutdown(self) -> None:
        self.begin_shutdown()
        for session_id in list(self._sessions):
            await self.terminate(session_id)

    def begin_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        event = {"type": "service_shutdown", "value": {}}
        for session in self._sessions.values():
            for queue in list(session.subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    session.subscribers.discard(queue)

    async def subscribe(
        self,
        session_id: str,
        *,
        last_event_id: str | None = None,
        replay: bool = True,
        announce_ready: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._shutting_down:
            return
        session = self.get(session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            history = list(session.events) if replay or last_event_id else []
            event_gap: dict[str, Any] | None = None
            if last_event_id:
                found = False
                for index, event in enumerate(history):
                    if event.get("event_id") == last_event_id:
                        history = history[index + 1 :]
                        found = True
                        break
                if not found:
                    history = []
                    event_gap = {
                        "type": "event_gap",
                        "value": {
                            "requested_event_id": last_event_id,
                            "latest_event_id": session.public_dict().get("last_event_id"),
                        },
                    }
            # Snapshot and registration are atomic with _publish, so an event
            # cannot fall between replay history and the live queue.
            session.subscribers.add(queue)
        try:
            if announce_ready:
                yield {"type": "service_subscription_ready", "value": {}}
            if event_gap is not None:
                yield event_gap
            for event in history:
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=max(self.heartbeat_seconds, 0.1),
                    )
                except asyncio.TimeoutError:
                    yield {"type": "service_heartbeat", "value": {}}
                    continue
                yield event
                if event.get("type") in {"session_closed", "service_shutdown"}:
                    break
        finally:
            async with self._lock:
                session.subscribers.discard(queue)

    async def _publish(self, session: ManagedSession, event: dict[str, Any]) -> None:
        async with self._lock:
            payload = dict(event)
            if not payload.get("event_id"):
                session.event_counter += 1
                payload["event_id"] = f"service-{session.session_id}-{session.event_counter}"
            journal = self.log_root / "projects" / project_log_key(str(session.workspace)) / f"session_{session.session_id}" / "events.jsonl"
            try:
                write_jsonl(journal, {"timestamp": iso_ts(), "session_id": session.session_id,
                                     **redact_log_value(payload, session.log_secrets)})
            except OSError as exc:
                warnings.warn(f"Could not persist session diagnostics: {exc}", RuntimeWarning)
            session.events.append(payload)
            # Publish the terminal event before exposing its idle state to
            # readers in another thread while diagnostic I/O releases the GIL.
            self._update_turn_state(session, payload)
            stale: list[asyncio.Queue[dict[str, Any]]] = []
            for queue in session.subscribers:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    stale.append(queue)
            for queue in stale:
                session.subscribers.discard(queue)

    @staticmethod
    def _reset_turn(session: ManagedSession) -> None:
        session.phase = "idle"
        session.current_turn_id = None
        session.current_plan_id = None

    def _update_turn_state(
        self,
        session: ManagedSession,
        event: Mapping[str, Any],
    ) -> None:
        turn_id = str(event.get("turn_id") or "")
        if not turn_id or turn_id != session.current_turn_id:
            return
        kind = str(event.get("type") or "")
        value = event.get("value") if isinstance(event.get("value"), Mapping) else {}
        if kind == "turn_started":
            session.phase = "running"
        elif kind == "plan_ready":
            session.phase = "awaiting_plan"
            session.current_plan_id = str(value.get("planId") or "") or None
        elif kind == "plan_cleared":
            reason = str(value.get("reason") or "")
            if reason == "approved":
                session.phase = "running"
                session.current_plan_id = None
            else:
                self._reset_turn(session)
        elif kind == "finished":
            if str(value.get("reason") or "") != "plan_ready":
                self._reset_turn(session)
        elif kind == "error":
            self._reset_turn(session)

    async def _read_stdout(self, session: ManagedSession) -> None:
        if session.process.stdout is None:
            return
        while True:
            line = await session.process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
            except (json.JSONDecodeError, ValueError) as exc:
                event = {
                    "type": "error",
                    "value": {"error": {"message": f"invalid backend JSON: {exc}"}},
                }
                # Invalid stdout is a protocol failure for the turn that was
                # active when the bad line arrived. Bind the synthetic error so
                # the busy state is released and frontends do not filter it as
                # stale, while _publish still ignores it if that turn changed.
                if session.current_turn_id:
                    event["turn_id"] = session.current_turn_id
            await self._publish(session, event)

    async def _read_stderr(self, session: ManagedSession) -> None:
        if session.process.stderr is None:
            return
        while True:
            line = await session.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                await self._publish(
                    session,
                    {"type": "backend_stderr", "value": {"message": text}},
                )

    async def _wait_for_exit(self, session: ManagedSession) -> None:
        return_code = await session.process.wait()
        session.return_code = return_code
        session.closed = True
        self._reset_turn(session)
        await self._publish(
            session,
            {
                "type": "session_closed",
                "value": {"return_code": return_code},
            },
        )
        async with self._lock:
            self._prune_closed_sessions_locked()
        session.closed_event.set()

    def _prune_closed_sessions_locked(self) -> None:
        closed = sorted(
            (session for session in self._sessions.values() if session.closed),
            key=lambda item: item.created_at,
        )
        excess = len(closed) - max(self.max_closed_sessions, 0)
        for session in closed[: max(excess, 0)]:
            self._sessions.pop(session.session_id, None)
