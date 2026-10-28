from __future__ import annotations

import os
from typing import Any, Dict, List

from langchain_core.messages import BaseMessage


_ENV_CONTEXT_MARKER_1 = "Today's date is"
_ENV_CONTEXT_MARKER_2 = "I'm currently working in the directory:"


def cache_mode() -> str:
    return (
        os.environ.get("LLM_ANTHROPIC_CACHE_MODE")
        or os.environ.get("LLM_CACHE_MODE")
        or ""
    ).strip().lower()


def _uses_messages_api() -> bool:
    return (os.environ.get("LLM_API_STYLE") or "").strip().lower() == "anthropic_native"


def cache_enabled() -> bool:
    if not _uses_messages_api():
        return False
    return cache_mode() in {"explicit_ephemeral", "anthropic_explicit_ephemeral"}


def _automatic_cache_enabled() -> bool:
    return cache_mode() in {"anthropic_auto", "anthropic_automatic"}


def request_cache_control() -> Dict[str, Any] | None:
    """Return top-level Anthropic automatic cache control when configured.

    Anthropic's recommended multi-turn mode is request-level `cache_control`,
    not rolling explicit markers on recent message blocks. Rolling markers can
    cause large repeated cache writes when the tail of the conversation changes.
    """
    if not _uses_messages_api() or not _automatic_cache_enabled():
        return None
    return _ephemeral_cache_control()


def _is_env_context_message(text: str) -> bool:
    return _ENV_CONTEXT_MARKER_1 in text and _ENV_CONTEXT_MARKER_2 in text


def _ephemeral_cache_control() -> Dict[str, Any]:
    ttl = (
        os.environ.get("LLM_ANTHROPIC_CACHE_TTL")
        or os.environ.get("LLM_CACHE_TTL")
        or ""
    ).strip()
    payload: Dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        payload["ttl"] = ttl
    return payload


def _content_with_cache_marker(content: Any) -> Any:
    marker = _ephemeral_cache_control()
    if isinstance(content, list):
        updated: List[Any] = []
        text_indices: List[int] = []
        for item in content:
            if not isinstance(item, dict):
                updated.append(item)
                continue
            cloned = dict(item)
            cloned.pop("cache_control", None)
            if cloned.get("type") == "text":
                text_indices.append(len(updated))
            updated.append(cloned)
        if text_indices:
            # Anthropic caches the prompt prefix through the marked content
            # block. Mark the last text block in the message so the whole
            # message, not only its first block, is included in the prefix.
            updated[text_indices[-1]]["cache_control"] = marker
            return updated
        return [
            *updated,
            {"type": "text", "text": "", "cache_control": marker},
        ]
    text = str(content or "")
    return [{"type": "text", "text": text, "cache_control": marker}]


def _has_cacheable_text_content(content: Any) -> bool:
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                if str(item.get("text") or "").strip():
                    return True
            elif isinstance(item, str) and item.strip():
                return True
        return False
    return bool(str(content or "").strip())


def _clone_message_with_content(message: BaseMessage, content: Any) -> BaseMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    if hasattr(message, "copy"):
        return message.copy(update={"content": content})
    return message


def _cache_breakpoint_offsets() -> List[int]:
    raw = (os.environ.get("LLM_ANTHROPIC_CACHE_BREAKPOINT_OFFSETS") or "").strip()
    if not raw:
        # Anthropic documents that a breakpoint can search earlier content
        # block boundaries, but some providers/backends do not reliably hit a
        # previously-created prefix unless that recent boundary is explicit in
        # the current request. Cover the latest block, the two immediately
        # preceding cacheable blocks, and one older rolling window for
        # tool-heavy histories.
        raw = "0,1,2,12"
    offsets: List[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value < 0:
            continue
        offsets.append(value)
    if 0 not in offsets:
        offsets.insert(0, 0)
    deduped: List[int] = []
    for value in offsets:
        if value not in deduped:
            deduped.append(value)
    return deduped[:4]


def apply_prompt_cache_markers(history: List[BaseMessage]) -> List[BaseMessage]:
    if not cache_enabled() or not history:
        return history

    # Dynamic full-history markers: Anthropic prompt caching is prefix-based
    # over tools -> system -> messages. A single latest marker works for short
    # chats, but agentic tool loops can miss cache hits when the previous
    # cached boundary is not explicitly represented in the next request. Mark
    # several recent boundaries plus one older rolling boundary while staying
    # within Anthropic's four-breakpoint limit.
    cacheable_indices: List[int] = []
    for idx, message in enumerate(history):
        message = history[idx]
        if _has_cacheable_text_content(getattr(message, "content", "")):
            cacheable_indices.append(idx)
    if not cacheable_indices:
        return history

    transformed = list(history)
    target_indices: List[int] = []
    for offset in _cache_breakpoint_offsets():
        if offset >= len(cacheable_indices):
            continue
        target_index = cacheable_indices[-1 - offset]
        if target_index not in target_indices:
            target_indices.append(target_index)

    for target_index in target_indices:
        target = transformed[target_index]
        transformed[target_index] = _clone_message_with_content(
            target, _content_with_cache_marker(getattr(target, "content", ""))
        )
    return transformed
