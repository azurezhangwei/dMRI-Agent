"""Authenticated transport to the visible Slicer session.

Behavior adapted from zhaoyouj/mcp-slicer; see THIRD_PARTY_NOTICES.md.
This module never starts Slicer or executes scene code in the backend process.
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from backend.service.scene_auth import derive_scene_auth_token


SLICER_WEB_SERVER_URL = os.environ.get("MCP_SLICER_WEB_SERVER_URL", "http://127.0.0.1:2016/slicer").rstrip("/")


MCP_SLICER_TOOL_TIMEOUT_SEC = float(os.environ.get("MCP_SLICER_TOOL_TIMEOUT_SEC", "45"))


_SCENE_TOKEN_REDACTION = "[redacted-scene-token]"


def get_proxy_config(url: str) -> Optional[dict[str, Optional[str]]]:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1") or hostname.startswith("127."):
        return {"http": None, "https": None}
    return None


def _scene_auth_token() -> str:
    """Recreate the private per-session token without persisting it."""

    return derive_scene_auth_token(
        os.environ.get("DMRI_SERVICE_TOKEN", ""),
        os.environ.get("LOG_SESSION_ID", ""),
    )


def _scene_request_url(endpoint: str) -> str:
    suffix = str(endpoint or "").strip()
    if not suffix.startswith("/"):
        raise ValueError("Slicer scene endpoint must start with '/'")
    return f"{SLICER_WEB_SERVER_URL}/{_scene_auth_token()}{suffix}"


def _public_scene_url(endpoint: str = "") -> str:
    suffix = str(endpoint or "").strip()
    if suffix and not suffix.startswith("/"):
        suffix = f"/{suffix}"
    return f"{SLICER_WEB_SERVER_URL}{suffix}"


def _redact_scene_secret(text: Any) -> str:
    rendered = str(text)
    try:
        token = _scene_auth_token()
    except ValueError:
        return rendered
    return rendered.replace(token, _SCENE_TOKEN_REDACTION)


def _sanitize_scene_value(value: Any) -> Any:
    """Remove the route credential from any model-visible response value."""

    if isinstance(value, str):
        return _redact_scene_secret(value)
    if isinstance(value, dict):
        return {
            _redact_scene_secret(key) if isinstance(key, str) else key: _sanitize_scene_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_scene_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_scene_value(item) for item in value)
    return value


def _classify_transport_error(detail: str) -> tuple[bool, str]:
    lowered = str(detail or "").strip().lower()
    if not lowered:
        return False, "transport_error"
    retryable_markers = (
        "connection error",
        "connection refused",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "http 502",
        "http 503",
        "http 504",
    )
    if any(marker in lowered for marker in retryable_markers):
        if "timeout" in lowered or "timed out" in lowered:
            return True, "transport_timeout"
        return True, "transport_unavailable"
    return False, "transport_error"


def _fetch_mrml_json(endpoint: str, params: dict[str, str]) -> tuple[str, Any]:
    url = _scene_request_url(endpoint)
    resp = requests.get(
        url,
        params=params,
        timeout=MCP_SLICER_TOOL_TIMEOUT_SEC,
        proxies=get_proxy_config(url),
    )
    resp.raise_for_status()
    return _public_scene_url(endpoint), _sanitize_scene_value(resp.json())


def _webserver_healthcheck(require_exec: bool = False) -> tuple[bool, str]:
    try:
        names_url = _scene_request_url("/mrml/names")
        names_resp = requests.get(
            names_url,
            timeout=min(5.0, MCP_SLICER_TOOL_TIMEOUT_SEC),
            proxies=get_proxy_config(names_url),
        )
        if names_resp.status_code != 200:
            return False, f"mrml/names status={names_resp.status_code}"
        if not require_exec:
            return True, "ok"
        exec_url = _scene_request_url("/exec")
        code = "__execResult={'ping': 'ok'}"
        exec_resp = requests.post(
            exec_url,
            data=code.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=min(5.0, MCP_SLICER_TOOL_TIMEOUT_SEC),
            proxies=get_proxy_config(exec_url),
        )
        if exec_resp.status_code == 200:
            return True, "ok"
        return False, f"exec status={exec_resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, _redact_scene_secret(exc)


def _ensure_webserver(require_exec: bool = False) -> tuple[bool, str]:
    ok, reason = _webserver_healthcheck(require_exec=require_exec)
    if ok:
        return True, "already ready"
    hint = (
        "Open dMRI-Agent in the running Slicer instance and reconnect the Agent session "
        "to restore its authenticated scene interface."
    )
    return False, f"web server unavailable: {reason}. {hint}"


def _execute_python_via_mcp(code: str, timeout_sec: Optional[float] = None) -> tuple[bool, Any, str, dict[str, Any]]:
    ok, msg = _ensure_webserver(require_exec=True)
    if not ok:
        return False, None, msg, {"request_url": _public_scene_url("/exec")}

    url = _scene_request_url("/exec")
    public_url = _public_scene_url("/exec")
    effective_timeout = float(timeout_sec if timeout_sec is not None else MCP_SLICER_TOOL_TIMEOUT_SEC)
    effective_timeout = max(3.0, min(effective_timeout, 7200.0))
    try:
        resp = requests.post(
            url,
            data=code.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=effective_timeout,
            proxies=get_proxy_config(url),
        )
    except Exception as exc:  # noqa: BLE001
        return False, None, f"Connection error: {_redact_scene_secret(exc)}", {"request_url": public_url}

    meta = {"request_url": public_url, "status_code": resp.status_code}
    try:
        payload = _sanitize_scene_value(resp.json()) if resp.content else {}
    except Exception as exc:  # noqa: BLE001
        text_preview = _redact_scene_secret((resp.text or "")[:500])
        return False, None, f"Invalid JSON response: {_redact_scene_secret(exc)}; preview={text_preview}", meta

    if resp.status_code != 200:
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload)
        else:
            detail = str(payload)
        return False, None, f"HTTP {resp.status_code}: {detail}", meta

    if isinstance(payload, dict) and payload.get("success", True) is False:
        detail = str(payload.get("message", "Unknown Python execution error"))
        return False, None, detail, meta

    if isinstance(payload, dict):
        if "__execResult" in payload:
            return True, payload.get("__execResult"), "", meta
        if "message" in payload:
            return True, payload.get("message"), "", meta
        return True, payload, "", meta
    return True, payload, "", meta
