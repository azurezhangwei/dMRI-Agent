from __future__ import annotations

import os
import json
from typing import Any, Dict
from urllib.parse import urlparse

import httpx
from backend.model_adapters import NativeMessagesAdapter, build_completion_adapter, native_thinking_options
from backend.prompt_cache import request_cache_control


def _build_http_client(timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
        trust_env=False,
        http2=False,
    )


def _resolve_temperature(default: float | None) -> float | None:
    raw = (os.environ.get("LLM_TEMPERATURE") or "").strip()
    if not raw:
        return default
    if raw.lower() in {"default", "none", "null", "omit"}:
        return None
    return float(raw)


def _resolve_extra_body() -> Dict[str, Any] | None:
    raw = (os.environ.get("LLM_EXTRA_BODY_JSON") or "").strip()
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("LLM_EXTRA_BODY_JSON must decode to a JSON object")
    return parsed


def _resolve_default_headers() -> Dict[str, str] | None:
    raw = (os.environ.get("LLM_DEFAULT_HEADERS_JSON") or "").strip()
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("LLM_DEFAULT_HEADERS_JSON must decode to a JSON object")
    headers: Dict[str, str] = {}
    for key, value in parsed.items():
        headers[str(key)] = str(value)
    return headers


def _resolve_timeout_seconds(default: float) -> float:
    raw = (os.environ.get("LLM_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return default
    return float(raw)


def _resolve_bool_env(name: str) -> bool | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _provider_style() -> str:
    raw = (os.environ.get("LLM_API_STYLE") or "").strip().lower()
    if raw:
        return raw
    base = (os.environ.get("LLM_BASE_URL") or "").strip()
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    # Anthropic-native should only be auto-selected for Anthropic endpoints.
    if "anthropic" in host or path.startswith("/v1/messages"):
        return "anthropic_native"
    return "openai_compatible"


def _resolve_max_tokens() -> int | None:
    raw = (os.environ.get("LLM_MAX_TOKENS") or "").strip()
    if not raw:
        return None
    return int(raw)


def build_chat_llm(
    *,
    api_key: str,
    model: str,
    base_url: str,
    temperature: float,
    streaming: bool,
    timeout_seconds: float = 600.0,
) -> Any:
    timeout_seconds = _resolve_timeout_seconds(timeout_seconds)
    provider_style = _provider_style()
    temperature_value = _resolve_temperature(temperature)
    extra_body = _resolve_extra_body()
    default_headers = _resolve_default_headers()
    max_tokens = _resolve_max_tokens()

    if provider_style == "anthropic_native":
        native_options: Dict[str, Any] = {
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
            "timeout": timeout_seconds,
            "max_retries": 2,
            "streaming": streaming,
        }
        if temperature_value is not None:
            native_options["temperature"] = temperature_value
        if max_tokens is not None:
            native_options["max_tokens"] = max_tokens
        thinking_options = native_thinking_options()
        if thinking_options:
            native_options["thinking"] = thinking_options
        cache_control = request_cache_control()
        if cache_control:
            native_options.setdefault("model_kwargs", {})["cache_control"] = cache_control
        if extra_body:
            native_options.update(extra_body)
        if default_headers:
            native_options["default_headers"] = default_headers
        return NativeMessagesAdapter(**native_options)

    http_client = _build_http_client(timeout_seconds)
    reasoning_effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip() or None
    use_responses_api = _resolve_bool_env("LLM_USE_RESPONSES_API")
    use_previous_response_id = _resolve_bool_env("LLM_USE_PREVIOUS_RESPONSE_ID")
    completion_options: Dict[str, Any] = {
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "temperature": temperature_value,
        "streaming": streaming,
        "stream_usage": True,
        "reasoning_effort": reasoning_effort,
        "extra_body": extra_body,
        "default_headers": default_headers,
        "request_timeout": timeout_seconds,
        "http_client": http_client,
    }
    if use_responses_api is not None:
        completion_options["use_responses_api"] = use_responses_api
    if use_previous_response_id is not None:
        completion_options["use_previous_response_id"] = use_previous_response_id
    return build_completion_adapter(base_url, completion_options)
