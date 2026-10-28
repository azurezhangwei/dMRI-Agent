from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error, parse, request


DIRECT_OPENER = request.build_opener(request.ProxyHandler({}))


class ServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or message

    @property
    def setup_required(self) -> bool:
        return self.status_code == 428


@dataclass
class SessionInfo:
    session_id: str
    mode: str
    workspace: str
    profile_id: str = ""
    reused: bool = False


class AgentServiceClient:
    """Small standard-library client for backend.service.

    3D Slicer ships its own Python runtime, so this client deliberately avoids
    depending on requests, httpx, or the agent virtual environment.
    """

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = (token if token is not None else os.environ.get("DMRI_SERVICE_TOKEN", "")).strip()
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop: Optional[threading.Event] = None
        self._stream_response: Any = None
        self._stream_lock = threading.Lock()
        self._last_event_id = ""
        self._stream_session_id = ""

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", "/health")

    def list_protocols(self) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "/v1/protocols")
        protocols = payload.get("protocols")
        if not isinstance(protocols, list):
            raise ServiceError("Agent service returned an invalid Protocols list")
        return [item for item in protocols if isinstance(item, dict)]

    def list_models(self) -> dict[str, Any]:
        payload = self._request_json("GET", "/v1/models")
        profiles = payload.get("profiles")
        active_profile = payload.get("active_profile")
        if not isinstance(profiles, list) or not isinstance(active_profile, str):
            raise ServiceError("Agent service returned invalid model configuration")
        return {
            "active_profile": active_profile,
            "profiles": [item for item in profiles if isinstance(item, dict)],
        }

    def model_readiness(self) -> dict[str, Any]:
        payload = self.list_models()
        active_profile = payload["active_profile"]
        profile = next(
            (
                item
                for item in payload["profiles"]
                if str(item.get("id") or "") == active_profile
            ),
            None,
        )
        if profile is None:
            return {
                **payload,
                "ready": False,
                "message": f"Active model profile was not found: {active_profile}",
                "profile": None,
            }
        configured = bool(profile.get("api_key_configured"))
        return {
            **payload,
            "ready": configured,
            "message": (
                f"Model ready: {profile.get('label') or active_profile}"
                if configured
                else f"Model setup required: {profile.get('label') or active_profile} has no API key"
            ),
            "profile": profile,
        }

    def fetch_model_catalog(self, profile_id: str, *, api_key: str = "") -> dict[str, Any]:
        key = api_key.strip()
        sanitized_error = None
        try:
            payload = self._request_json(
                "POST", f"/v1/models/{parse.quote(profile_id, safe='')}/catalog",
                {"api_key": key} if key else {}, timeout_seconds=40.0,
            )
            models = payload.get("models")
            if payload.get("profile_id") != profile_id or not isinstance(models, list) or not models:
                raise ServiceError("Agent service returned an invalid model list")
            if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in models):
                raise ServiceError("Agent service returned an invalid model list")
            return payload
        except ServiceError as exc:
            sanitized_error = ServiceError(
                str(exc).replace(key, "[redacted]") if key else str(exc),
                status_code=exc.status_code,
                detail=exc.detail.replace(key, "[redacted]") if key else exc.detail,
            )
        raise sanitized_error

    def set_active_model(self, profile_id: str) -> dict[str, Any]:
        return self._request_json(
            "PUT",
            "/v1/models/active",
            {"profile_id": profile_id},
        )

    def set_model_credential(self, profile_id: str, api_key: str) -> dict[str, Any]:
        key = api_key.strip()
        if not key:
            raise ServiceError("Model API key cannot be empty")
        return self._request_json(
            "PUT",
            f"/v1/models/{parse.quote(profile_id)}/credential",
            {"api_key": key},
        )

    def test_model(
        self,
        profile_id: str,
        *,
        api_key: str = "",
        save_on_success: bool = False,
        model: str | None = None,
    ) -> dict[str, Any]:
        key = api_key.strip()
        candidate_model = model.strip() if model is not None else None
        if candidate_model is not None and not candidate_model:
            raise ServiceError("Model name cannot be empty")
        if save_on_success and not key and candidate_model is None:
            raise ServiceError("Model API key cannot be empty")
        payload: dict[str, Any] = {}
        if key:
            payload = {"api_key": key, "save_on_success": bool(save_on_success)}
        if candidate_model is not None:
            payload.update(model=candidate_model, save_on_success=bool(save_on_success))
        sanitized_error: ServiceError | None = None
        try:
            return self._request_json(
                "POST",
                f"/v1/models/{parse.quote(profile_id, safe='')}/test",
                payload,
                timeout_seconds=60.0,
            )
        except ServiceError as exc:
            if not key:
                raise
            redacted = "[redacted]"
            sanitized_error = ServiceError(
                str(exc).replace(key, redacted),
                status_code=exc.status_code,
                detail=exc.detail.replace(key, redacted),
            )
        # Raise outside the handler so the exception chain cannot retain a
        # provider response that echoed the submitted credential.
        raise sanitized_error

    def session_status(self, session_id: str) -> dict[str, Any]:
        payload = self._request_json("GET", f"/v1/sessions/{parse.quote(session_id)}")
        return {
            **payload,
            "sessionId": str(payload.get("session_id") or session_id),
            "workspace": str(payload.get("workspace") or ""),
            "alive": not bool(payload.get("closed")),
            "subscriberCount": int(self.stream_active),
            "pid": "service",
            "phase": str(payload.get("phase") or "idle"),
            "busy": bool(payload.get("busy")),
            "currentTurnId": str(payload.get("current_turn_id") or ""),
            "lastEventId": str(payload.get("last_event_id") or ""),
        }

    def create_session(
        self,
        workspace: str,
        profile_id: str | None = None,
        auto_approve_tools: bool = False,
    ) -> SessionInfo:
        requested_workspace = str(workspace or "").strip()
        if not requested_workspace:
            raise ServiceError("Choose a dMRI workspace before starting a session")
        workspace_path = str(Path(requested_workspace).expanduser().resolve())
        payload = self._request_json(
            "POST",
            "/v1/sessions",
            {
                "workspace": workspace_path,
                "profile_id": profile_id or None,
                "auto_approve_tools": bool(auto_approve_tools),
                "replace_existing": False,
            },
        )
        return SessionInfo(
            session_id=str(payload["session_id"]),
            mode="service",
            workspace=str(payload.get("workspace") or workspace_path),
            profile_id=str(payload.get("profile_id") or profile_id or ""),
            reused=bool(payload.get("reused")),
        )

    def send_message(
        self,
        session_id: str,
        text: str,
        mode: str = "execute",
        protocol_ids: Optional[list[str]] = None,
        turn_id: str = "",
    ) -> dict[str, Any]:
        requested_turn_id = turn_id or str(uuid.uuid4())
        return self._request_json(
            "POST",
            f"/v1/sessions/{parse.quote(session_id)}/messages",
            {
                "text": text,
                "mode": mode,
                "protocol_ids": protocol_ids or [],
                "turn_id": requested_turn_id,
            },
        )

    def approve_tool(self, session_id: str, call_id: str, approved: bool) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/sessions/{parse.quote(session_id)}/tool-approval",
            {"call_id": call_id, "approved": bool(approved)},
        )

    def approve_plan(
        self,
        session_id: str,
        plan_id: str,
        approved: bool,
        feedback: str = "",
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/sessions/{parse.quote(session_id)}/plan-response",
            {"plan_id": plan_id, "approved": bool(approved), "feedback": feedback},
        )

    def send_command(self, session_id: str, name: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/sessions/{parse.quote(session_id)}/commands",
            {"name": name},
        )

    def shutdown(self, session_id: str) -> dict[str, Any]:
        return self._request_json("DELETE", f"/v1/sessions/{parse.quote(session_id)}")

    @property
    def stream_active(self) -> bool:
        return self._stream_thread is not None and self._stream_thread.is_alive()

    def start_stream(
        self,
        session_id: str,
        on_event: Callable[[dict[str, Any]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
        last_event_id: str = "",
        replay: bool = False,
    ) -> None:
        self.stop_stream()
        if self._stream_session_id and self._stream_session_id != session_id:
            self._last_event_id = ""
        self._stream_session_id = session_id
        stop_event = threading.Event()
        ready_event = threading.Event()
        self._stream_stop = stop_event

        def _run() -> None:
            headers = self._headers(accept="text/event-stream")
            resume_event_id = last_event_id or self._last_event_id
            if resume_event_id:
                headers["Last-Event-ID"] = resume_event_id
            query = "" if resume_event_id or replay else "?replay=false"
            req = request.Request(
                f"{self.base_url}/v1/sessions/{parse.quote(session_id)}/events{query}",
                headers=headers,
            )
            try:
                with DIRECT_OPENER.open(req, timeout=75) as response:
                    with self._stream_lock:
                        self._stream_response = response
                    event_name = ""
                    event_id = ""
                    data_lines: list[str] = []
                    while not stop_event.is_set():
                        raw_line = response.readline()
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                payload = json.loads("\n".join(data_lines))
                                if not isinstance(payload, dict):
                                    raise ServiceError("SSE payload is not an object")
                                if event_id and "event_id" not in payload:
                                    payload["event_id"] = event_id
                                if event_id:
                                    self._last_event_id = event_id
                                if event_name:
                                    payload.setdefault("_sse_event", event_name)
                                on_event(payload)
                                if event_name == "ready":
                                    ready_event.set()
                            event_name = ""
                            event_id = ""
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_name = line.split(":", 1)[1].strip()
                        elif line.startswith("id:"):
                            event_id = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line.split(":", 1)[1].lstrip())
            except Exception as exc:  # noqa: BLE001
                if on_error is not None and not stop_event.is_set():
                    on_error(self._normalize_error(exc))
            finally:
                with self._stream_lock:
                    self._stream_response = None

        self._stream_thread = threading.Thread(
            target=_run,
            daemon=True,
            name="dmri-agent-service-sse",
        )
        self._stream_thread.start()
        if not ready_event.wait(5.0):
            self.stop_stream()
            raise ServiceError("Timed out while connecting to the session event stream")

    def stop_stream(self) -> None:
        if self._stream_stop is not None:
            self._stream_stop.set()
        with self._stream_lock:
            response = self._stream_response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        if self._stream_thread is not None and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)
        self._stream_thread = None
        self._stream_stop = None

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"{self.base_url}{path}",
            method=method,
            data=body,
            headers=headers,
        )
        try:
            with DIRECT_OPENER.open(req, timeout=timeout_seconds) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:  # noqa: BLE001
            raise self._normalize_error(exc) from exc

    @staticmethod
    def _normalize_error(exc: Exception) -> ServiceError:
        if isinstance(exc, ServiceError):
            return exc
        if isinstance(exc, error.HTTPError):
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
                detail = payload.get("detail") or payload
                message = (
                    str(detail.get("message") or detail)
                    if isinstance(detail, dict)
                    else str(detail)
                )
            except (json.JSONDecodeError, AttributeError):
                message = raw.strip() or exc.reason
            return ServiceError(
                f"HTTP {exc.code}: {message}",
                status_code=exc.code,
                detail=message,
            )
        if isinstance(exc, error.URLError):
            return ServiceError(str(exc.reason))
        return ServiceError(str(exc))
