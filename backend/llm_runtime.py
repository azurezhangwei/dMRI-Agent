"""Shared stream settings, token accounting, and diagnostics for both agents.

Orchestration, retry decisions, approval, and cancellation stay with each agent.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import threading
from typing import Any, Dict, List

from langchain_core.messages import BaseMessage
from backend.llm_content import coerce_visible_content, extract_reasoning_content


DEFAULT_EMPTY_LLM_RETRY_LIMIT = 3
DEFAULT_LLM_STREAM_RETRY_LIMIT = 10
DEFAULT_LLM_STREAM_RETRY_INTERVAL_SECONDS = 5.0
DEFAULT_LLM_STREAM_HARD_TIMEOUT_SECONDS = 600.0


def _empty_llm_retry_limit() -> int:
    raw = os.environ.get("EMPTY_LLM_RETRY_LIMIT", str(DEFAULT_EMPTY_LLM_RETRY_LIMIT))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_EMPTY_LLM_RETRY_LIMIT


def _llm_stream_retry_limit() -> int:
    raw = os.environ.get("LLM_STREAM_RETRY_LIMIT", str(DEFAULT_LLM_STREAM_RETRY_LIMIT))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_LLM_STREAM_RETRY_LIMIT


def _llm_stream_retry_interval_seconds() -> float:
    raw = os.environ.get(
        "LLM_STREAM_RETRY_INTERVAL_SECONDS",
        str(DEFAULT_LLM_STREAM_RETRY_INTERVAL_SECONDS),
    )
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_LLM_STREAM_RETRY_INTERVAL_SECONDS


def _llm_stream_hard_timeout_seconds() -> float:
    raw = os.environ.get(
        "LLM_STREAM_HARD_TIMEOUT_SECONDS",
        str(DEFAULT_LLM_STREAM_HARD_TIMEOUT_SECONDS),
    )
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_LLM_STREAM_HARD_TIMEOUT_SECONDS


@contextmanager
def _llm_stream_hard_timeout() -> Any:
    timeout_seconds = _llm_stream_hard_timeout_seconds()
    if (
        timeout_seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
    ):
        yield None
        return

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _handle_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(
            f"LLM stream idle timeout after {timeout_seconds:.0f}s without new chunks"
        )

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

    def _reset_timer() -> None:
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

    try:
        yield _reset_timer
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


def _extract_usage_metadata(chunk: BaseMessage) -> Dict[str, int] | None:
    usage = getattr(chunk, "usage_metadata", None)
    normalized = _normalize_usage(usage)
    if normalized:
        return normalized
    response_metadata = getattr(chunk, "response_metadata", None) or {}
    for key in ("token_usage", "usage", "usage_metadata"):
        normalized = _normalize_usage(response_metadata.get(key))
        if normalized:
            return normalized
    additional = getattr(chunk, "additional_kwargs", None) or {}
    for key in ("token_usage", "usage"):
        normalized = _normalize_usage(additional.get(key))
        if normalized:
            return normalized
    return None


def _normalize_usage(usage: Any) -> Dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    cached = usage.get("cached_tokens")
    cache_write = usage.get("cache_write_tokens")
    if prompt is None and "input_tokens" in usage:
        prompt = usage.get("input_tokens")
    if completion is None and "output_tokens" in usage:
        completion = usage.get("output_tokens")
    if cached is None:
        prompt_details = (
            usage.get("prompt_tokens_details")
            or usage.get("input_tokens_details")
            or usage.get("input_token_details")
            or {}
        )
        if isinstance(prompt_details, dict):
            cached = (
                prompt_details.get("cached_tokens")
                or prompt_details.get("cache_read")
                or prompt_details.get("cache_read_input_tokens")
                or prompt_details.get("prompt_cache_hit_tokens")
            )
            cache_write = (
                prompt_details.get("cache_creation_input_tokens")
                if cache_write is None
                else cache_write
            )
            if cache_write in {None, 0}:
                cache_write = (
                    prompt_details.get("ephemeral_5m_input_tokens")
                    or prompt_details.get("ephemeral_1h_input_tokens")
                    or cache_write
                )
            cache_creation = prompt_details.get("cache_creation") or {}
            if cache_write in {None, 0} and isinstance(cache_creation, dict):
                cache_write = (
                    cache_creation.get("ephemeral_5m_input_tokens")
                    or cache_creation.get("ephemeral_1h_input_tokens")
                    or cache_write
                )
            elif cache_write in {None, 0} and isinstance(cache_creation, int):
                cache_write = cache_creation
    if cached is None and "cache_read_input_tokens" in usage:
        cached = usage.get("cache_read_input_tokens")
    if cached is None and "prompt_cache_hit_tokens" in usage:
        cached = usage.get("prompt_cache_hit_tokens")
    if cache_write is None and "cache_creation_input_tokens" in usage:
        cache_write = usage.get("cache_creation_input_tokens")
    if total is None and "total_tokens" in usage:
        total = usage.get("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = int(prompt) + int(completion)
    if (
        prompt is None
        and completion is None
        and total is None
        and cached is None
        and cache_write is None
    ):
        return None
    normalized = {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(total or 0),
    }
    if cached is not None:
        normalized["cached_tokens"] = int(cached or 0)
    if cache_write is not None:
        normalized["cache_write_tokens"] = int(cache_write or 0)
    return normalized


def _summarize_chunk(chunk: BaseMessage) -> Dict[str, Any]:
    content = coerce_visible_content(getattr(chunk, "content", ""))
    raw_tool_calls = getattr(chunk, "tool_calls", None)
    raw_tool_chunks = getattr(chunk, "tool_call_chunks", None)
    additional = getattr(chunk, "additional_kwargs", {}) or {}
    preview_len = int(os.environ.get("LOG_CHUNK_PREVIEW_MAX", "160"))
    return {
        "type": type(chunk).__name__,
        "content_len": len(content or ""),
        "content_preview": (content or "")[:preview_len],
        "tool_calls": raw_tool_calls,
        "tool_call_chunks": raw_tool_chunks,
        "additional_kwargs": additional,
    }


def _chunk_diagnostics(chunk: BaseMessage) -> Dict[str, Any]:
    raw_content = getattr(chunk, "content", "")
    content = coerce_visible_content(raw_content) or ""
    additional = getattr(chunk, "additional_kwargs", {}) or {}
    response_metadata = getattr(chunk, "response_metadata", {}) or {}
    reasoning = additional.get("reasoning_content")
    if reasoning is None:
        reasoning = additional.get("reasoning")
    if reasoning is None:
        reasoning = extract_reasoning_content(
            raw_content,
        )
    finish_reason = response_metadata.get("finish_reason") or additional.get(
        "finish_reason"
    )
    return {
        "type": type(chunk).__name__,
        "content_len": len(content),
        "tool_calls_len": len(getattr(chunk, "tool_calls", None) or []),
        "tool_call_chunks_len": len(getattr(chunk, "tool_call_chunks", None) or []),
        "finish_reason": finish_reason,
        "additional_keys": sorted(str(key) for key in additional.keys()),
        "response_metadata_keys": sorted(str(key) for key in response_metadata.keys()),
        "reasoning_len": len(reasoning) if isinstance(reasoning, str) else 0,
    }


def _stream_diagnostics(
    *,
    turn: int,
    chunk_records: List[Dict[str, Any]],
    chunk_count: int,
    elapsed: float,
    content_chars: int,
    tool_call_count: int,
    usage_seen: bool,
) -> Dict[str, Any]:
    finish_reasons: List[str] = []
    reasoning_chars = 0
    for record in chunk_records:
        diagnostics = record.get("diagnostics") or {}
        finish_reason = diagnostics.get("finish_reason")
        if finish_reason is not None:
            finish_reasons.append(str(finish_reason))
        reasoning_chars += int(diagnostics.get("reasoning_len") or 0)
    last_chunk = (chunk_records[-1].get("diagnostics") if chunk_records else None)
    return {
        "turn": turn,
        "chunks": chunk_count,
        "elapsed": elapsed,
        "content_chars": content_chars,
        "tool_calls": tool_call_count,
        "usage_seen": usage_seen,
        "finish_reasons": finish_reasons,
        "reasoning_chars": reasoning_chars,
        "last_chunk": last_chunk,
    }
