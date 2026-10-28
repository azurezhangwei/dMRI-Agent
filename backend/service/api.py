from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import uuid
from dataclasses import replace
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr

from backend.model_config import ModelConfigError, resolve_profile_api_key, validate_model_name
from backend.project_paths import PROJECT_ROOT, PROJECT_VERSION
from backend.protocol_registry import iter_protocol_entries
from backend.request_controls import normalize_protocol_ids
from backend.service.history_store import HistoryError, HistoryNotFound, HistoryStore
from backend.service.lease_manager import ServiceLeaseManager
from backend.service.model_catalog import ModelCatalogError, fetch_model_catalog
from backend.service.runtime_broker import RuntimeBrokerError, clear_service_descriptor
from backend.service.session_manager import (
    SessionError,
    SessionBusy,
    SessionManager,
    SessionNotFound,
    SessionSetupRequired,
)


class CreateSessionRequest(BaseModel):
    workspace: str = Field(min_length=1)
    profile_id: str | None = None
    auto_approve_tools: bool = False
    replace_existing: bool = False


class MessageRequest(BaseModel):
    text: str
    mode: Literal["execute", "plan"] = "execute"
    protocol_ids: list[str] = Field(default_factory=list)
    turn_id: str | None = Field(default=None, min_length=1, max_length=128)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class RestoreHistoryRequest(BaseModel):
    messages: list[HistoryMessage]


class ToolApprovalRequest(BaseModel):
    call_id: str
    approved: bool


class PlanResponseRequest(BaseModel):
    plan_id: str
    approved: bool
    feedback: str = ""


class CommandRequest(BaseModel):
    name: str


class ActiveProfileRequest(BaseModel):
    profile_id: str


class CredentialRequest(BaseModel):
    api_key: str


class ModelTestRequest(BaseModel):
    api_key: SecretStr | None = None
    save_on_success: bool = False
    model: str | None = None


class ModelCatalogRequest(BaseModel):
    api_key: SecretStr | None = None


class ProfileUpdateRequest(BaseModel):
    label: str | None = None
    provider: str | None = None
    api_style: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    reasoning_effort: str | None = None
    anthropic_thinking: str | None = None
    cache_strategy: str | None = None
    cache_ttl: str | None = None
    use_responses_api: bool | None = None
    use_previous_response_id: bool | None = None
    extra_body: dict[str, Any] | None = None
    default_headers: dict[str, str] | None = None


class ServiceLeaseRequest(BaseModel):
    lease_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    client: str = Field(default="desktop", min_length=1, max_length=32)
    ttl_seconds: int = Field(default=20, ge=5, le=120)


class ServiceLeaseHeartbeatRequest(BaseModel):
    ttl_seconds: int = Field(default=20, ge=5, le=120)


VALID_COMMANDS = {"compress", "clear", "cancel", "auto_approve_on", "auto_approve_off"}


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SessionSetupRequired):
        return HTTPException(status_code=428, detail=str(exc))
    if isinstance(exc, SessionNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SessionBusy):
        return HTTPException(
            status_code=409,
            detail={"code": "session_busy", "message": str(exc)},
        )
    if isinstance(exc, HistoryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (SessionError, HistoryError, ModelConfigError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="internal service error")


def _sse(event: dict[str, Any]) -> str:
    event_id = str(event.get("event_id") or "")
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _redact_secret(value: Any, secret: str) -> Any:
    """Remove a probe credential from any data returned by the model test."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            _redact_secret(key, secret) if isinstance(key, str) else key: _redact_secret(
                item, secret
            )
            for key, item in value.items()
        }
    return value


def create_app(
    manager: SessionManager | None = None,
    lease_manager: ServiceLeaseManager | None = None,
) -> FastAPI:
    service = manager or SessionManager()
    leases = lease_manager or ServiceLeaseManager.from_environment()
    history = HistoryStore(service.log_root)

    def request_graceful_shutdown() -> None:
        service.begin_shutdown()
        server = getattr(app.state, "uvicorn_server", None)
        if server is not None:
            server.should_exit = True
            return
        # A managed app normally runs under backend.service.__main__. The
        # signal fallback keeps custom ASGI hosts from leaking a managed
        # service when they do not expose a server shutdown hook.
        from backend.service.lease_manager import _terminate_current_process

        _terminate_current_process()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        lease_watchdog = asyncio.create_task(
            leases.watchdog(request_graceful_shutdown)
        )
        try:
            yield
        finally:
            lease_watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await lease_watchdog
            if leases.managed:
                with suppress(OSError, RuntimeBrokerError):
                    clear_service_descriptor(os.getpid())
            await service.shutdown()

    app = FastAPI(title="dMRI-Agent Service", version=PROJECT_VERSION, lifespan=lifespan)
    app.state.session_manager = service
    app.state.service_lease_manager = leases
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in os.environ.get(
                "DMRI_CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000",
            ).split(",")
            if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    service_token = (os.environ.get("DMRI_SERVICE_TOKEN") or "").strip()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if service_token and request.url.path != "/health":
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {service_token}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        lease_status = await leases.status()
        return {
            "ok": True,
            "service": "dmri-agent",
            "version": app.version,
            "service_leases": True,
            "managed": lease_status["managed"],
        }

    @app.post("/v1/service-leases")
    async def acquire_service_lease(request: ServiceLeaseRequest) -> dict[str, Any]:
        status = await leases.acquire(
            request.lease_id,
            request.client,
            request.ttl_seconds,
        )
        return {"ok": True, "lease_id": request.lease_id, **status}

    @app.get("/v1/service-leases")
    async def service_lease_status() -> dict[str, Any]:
        status = await leases.status()
        return {
            "ok": True,
            "service": "dmri-agent",
            "version": app.version,
            "service_leases": True,
            "pid": os.getpid(),
            "slicer_scene_url": os.environ.get(
                "MCP_SLICER_WEB_SERVER_URL", "http://127.0.0.1:2016/slicer"
            ).rstrip("/"),
            **status,
        }

    @app.put("/v1/service-leases/{lease_id}")
    async def heartbeat_service_lease(
        lease_id: str,
        request: ServiceLeaseHeartbeatRequest,
    ) -> dict[str, Any]:
        status = await leases.renew(lease_id, request.ttl_seconds)
        if status is None:
            raise HTTPException(status_code=404, detail="service lease not found")
        return {"ok": True, "lease_id": lease_id, **status}

    @app.delete("/v1/service-leases/{lease_id}")
    async def release_service_lease(lease_id: str) -> dict[str, Any]:
        released, status = await leases.release(lease_id)
        return {"ok": True, "lease_id": lease_id, "released": released, **status}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        try:
            config = service.model_config()
            values = os.environ
            profiles = [
                {
                    **profile.public_dict(configured=bool(resolve_profile_api_key(profile, values, service.secrets))),
                    "default_model": config.default_models.get(profile.profile_id, profile.model),
                }
                for profile in config.profiles.values()
            ]
            return {
                "active_profile": config.active_profile,
                "profiles": profiles,
            }
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/protocols")
    async def list_protocols() -> dict[str, Any]:
        return {
            "protocols": [
                {
                    "id": entry.protocol_id,
                    "name": entry.name,
                    "description": entry.description,
                    "type": entry.protocol_type,
                    "version": entry.version,
                }
                for entry in iter_protocol_entries()
            ]
        }

    @app.put("/v1/models/active")
    async def set_active_model(request: ActiveProfileRequest) -> dict[str, Any]:
        try:
            config = service.model_config()
            config.set_active_profile(request.profile_id)
            return {"ok": True, "active_profile": request.profile_id}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/models/{profile_id}/catalog")
    async def model_catalog(profile_id: str, request: ModelCatalogRequest) -> dict[str, Any]:
        try:
            profile = service.model_config().select(profile_id)
            key = request.api_key.get_secret_value().strip() if request.api_key else ""
            key = key or resolve_profile_api_key(profile, os.environ, service.secrets) or ""
            if not key:
                raise ModelConfigError("Enter an API key or save one before fetching models.")
        except Exception as exc:
            raise _http_error(exc) from exc
        logger = logging.getLogger("uvicorn.error")
        try:
            models = await asyncio.wait_for(fetch_model_catalog(profile.provider, key), timeout=30.0)
        except (ModelCatalogError, asyncio.TimeoutError) as exc:
            error = exc if isinstance(exc, ModelCatalogError) else ModelCatalogError(
                "Fetching models timed out. Try again or enter a model name manually.", code="model_catalog_timeout", status=504
            )
            logger.warning("Model catalog failed: provider=%s code=%s status=%s", profile.provider, error.code, error.status)
            raise HTTPException(status_code=error.status, detail={"code": error.code, "message": str(error)}) from None
        except Exception:
            logger.warning("Model catalog failed: provider=%s code=unexpected_error", profile.provider)
            raise HTTPException(status_code=502, detail="Could not fetch models. You can enter a model name manually.") from None
        logger.info("Model catalog fetched: provider=%s count=%s", profile.provider, len(models))
        return {"profile_id": profile_id, "models": models}

    @app.patch("/v1/models/{profile_id}")
    async def update_model(profile_id: str, request: ProfileUpdateRequest) -> dict[str, Any]:
        try:
            updates = request.model_dump(exclude_unset=True)
            profile = service.model_config().update_profile(profile_id, updates)
            configured = bool(resolve_profile_api_key(profile, os.environ, service.secrets))
            return {"ok": True, "profile": profile.public_dict(configured)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.put("/v1/models/{profile_id}/credential")
    async def set_credential(profile_id: str, request: CredentialRequest) -> dict[str, Any]:
        try:
            service.model_config().select(profile_id)
            service.secrets.set(profile_id, request.api_key)
            return {"ok": True, "profile_id": profile_id, "api_key_configured": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/v1/models/{profile_id}/credential")
    async def delete_credential(profile_id: str) -> dict[str, Any]:
        try:
            service.model_config().select(profile_id)
            service.secrets.delete(profile_id)
            return {"ok": True, "profile_id": profile_id, "api_key_configured": False}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/models/{profile_id}/test")
    async def test_model(
        profile_id: str,
        request: ModelTestRequest | None = None,
    ) -> dict[str, Any]:
        try:
            config = service.model_config()
            profile = config.select(profile_id)
            submitted_model = validate_model_name(request.model) if request and request.model is not None else None
            if submitted_model is not None:
                profile = replace(profile, model=submitted_model)
            submitted_key = (
                request.api_key.get_secret_value().strip()
                if request and request.api_key
                else ""
            )
            save_on_success = bool(request and request.save_on_success)
            if save_on_success and not submitted_key and submitted_model is None:
                raise ModelConfigError("api_key or model is required when save_on_success is enabled")

            from backend.model_config import profile_environment

            environment = os.environ.copy()
            if submitted_key:
                # Test the exact credential supplied by the Slicer setup UI.
                # Do not let a provider environment variable or an older saved
                # secret take precedence over this one-time candidate.
                environment.update(profile.to_environment(submitted_key))
                environment[profile.api_key_env] = submitted_key
            else:
                environment.update(
                    profile_environment(
                        profile_id,
                        config=config,
                        environ=environment,
                        secrets=service.secrets,
                    )
                )
                # Use the saved credential with the exact candidate model,
                # without changing the live configuration before the probe.
                environment["LLM_MODEL"] = profile.model
            probe_key = (environment.get("LLM_API_KEY") or "").strip()
            environment["PYTHONPATH"] = str(PROJECT_ROOT) + (
                os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
            )
        except Exception as exc:
            raise _http_error(exc) from exc

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "backend.service.model_probe",
                cwd=str(PROJECT_ROOT),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(), timeout=45.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise
            if process.returncode != 0:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "model_connection_test_failed",
                        "message": "Model connection test failed. Check the API key and model settings.",
                    },
                )
            payload = json.loads(stdout.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "model_connection_test_failed",
                        "message": "Model connection test failed. Check the API key and model settings.",
                    },
                )
            if submitted_model is not None and save_on_success:
                try:
                    config.save_model_choice(profile_id, submitted_model)
                except Exception as exc:
                    raise HTTPException(status_code=500, detail={
                        "code": "model_choice_save_failed",
                        "message": "The model was valid but could not be saved.",
                    }) from exc
            if submitted_key and save_on_success:
                # Persist only a credential that completed the real provider
                # probe.  A typo therefore cannot overwrite a working key.
                try:
                    service.secrets.set(profile_id, submitted_key)
                except Exception as exc:
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "code": "model_credential_save_failed",
                            "message": "The API key was valid but could not be saved.",
                        },
                    ) from exc
            return {
                "ok": True,
                "profile_id": profile.profile_id,
                "model": profile.model,
                "result": _redact_secret(payload, probe_key),
            }
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="model connection test timed out") from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "model_connection_test_error",
                    "message": "Model connection test could not be completed.",
                },
            ) from exc

    @app.get("/v1/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": service.list_sessions()}

    @app.get("/v1/history")
    async def list_history(workspace: str) -> dict[str, Any]:
        try:
            return {"sessions": [entry.public_dict() for entry in history.list(workspace)]}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/history/{history_id}")
    async def read_history(history_id: str, workspace: str) -> dict[str, Any]:
        try:
            entry, content = history.read(workspace, history_id)
            return {"session": entry.public_dict(), "content": content}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/history/{history_id}/resume-messages")
    async def history_resume_messages(
        history_id: str,
        workspace: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        try:
            return {"messages": history.resume_messages(workspace, history_id, limit=limit)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/v1/history/{history_id}")
    async def delete_history(history_id: str, workspace: str) -> dict[str, bool]:
        try:
            try:
                active = service.get(history_id)
            except SessionNotFound:
                active = None
            if active is not None and not active.closed:
                raise HistoryError("cannot delete an active session")
            history.delete(workspace, history_id)
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/sessions")
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        try:
            existing = service.active_session_for_workspace(request.workspace)
            session = await service.create_session(
                workspace=request.workspace,
                profile_id=request.profile_id,
                auto_approve_tools=request.auto_approve_tools,
                replace_existing=request.replace_existing,
            )
            payload = session.public_dict()
            payload["reused"] = existing is session and not request.replace_existing
            return payload
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            return service.get(session_id).public_dict()
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: MessageRequest) -> dict[str, Any]:
        try:
            protocol_ids = normalize_protocol_ids(request.protocol_ids)
            turn_id = request.turn_id or str(uuid.uuid4())
            await service.send_user_input(
                session_id,
                {
                    "text": request.text,
                    "mode": request.mode,
                    "protocol_ids": protocol_ids,
                },
                turn_id=turn_id,
            )
            return {"ok": True, "turn_id": turn_id}
        except SessionBusy as exc:
            session = service.get(session_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_busy",
                    "message": str(exc),
                    "phase": session.phase,
                    "current_turn_id": session.current_turn_id,
                },
            ) from exc
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/sessions/{session_id}/restore-history")
    async def restore_history(session_id: str, request: RestoreHistoryRequest) -> dict[str, bool]:
        try:
            await service.send_payload(
                session_id,
                {
                    "type": "restore_history",
                    "messages": [message.model_dump() for message in request.messages],
                },
            )
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/sessions/{session_id}/tool-approval")
    async def tool_approval(session_id: str, request: ToolApprovalRequest) -> dict[str, bool]:
        try:
            await service.send_payload(
                session_id,
                {"type": "tool_call_response", "callId": request.call_id, "approved": request.approved},
            )
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/sessions/{session_id}/plan-response")
    async def plan_response(session_id: str, request: PlanResponseRequest) -> dict[str, bool]:
        try:
            await service.send_plan_response(
                session_id,
                plan_id=request.plan_id,
                approved=request.approved,
                feedback=request.feedback,
            )
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/sessions/{session_id}/commands")
    async def command(session_id: str, request: CommandRequest) -> dict[str, bool]:
        if request.name not in VALID_COMMANDS:
            raise HTTPException(status_code=400, detail="invalid command")
        try:
            if request.name == "clear":
                service.clear_event_history(session_id)
            await service.send_payload(session_id, {"type": "command", "name": request.name})
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/v1/sessions/{session_id}")
    async def terminate_session(session_id: str) -> dict[str, bool]:
        try:
            await service.terminate(session_id)
            return {"ok": True}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/sessions/{session_id}/events")
    async def events(
        session_id: str,
        last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
        replay: bool = True,
    ) -> StreamingResponse:
        try:
            session = service.get(session_id)
            if session.closed:
                raise HTTPException(status_code=409, detail="session is closed")
        except HTTPException:
            raise
        except Exception as exc:
            raise _http_error(exc) from exc

        async def stream() -> AsyncIterator[str]:
            async for event in service.subscribe(
                session_id,
                last_event_id=last_event_id_header,
                replay=replay,
                announce_ready=True,
            ):
                if event.get("type") == "service_subscription_ready":
                    yield "event: ready\ndata: {}\n\n"
                    continue
                yield _sse(event)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
