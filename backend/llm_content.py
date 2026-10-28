"""Extract visible text and reasoning from model message content blocks."""
from __future__ import annotations

from typing import Any


def _field(block: Any, name: str) -> Any:
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def coerce_visible_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "".join(coerce_visible_content(block) for block in content)
    kind = _field(content, "type")
    if kind in {"text", "output_text", "input_text"}:
        text = _field(content, "text")
        return text if isinstance(text, str) else ""
    if kind in {"message", "content"}:
        return coerce_visible_content(_field(content, "content"))
    return ""


def extract_reasoning_content(content: Any) -> str:
    if content is None or isinstance(content, str):
        return ""
    if isinstance(content, (list, tuple)):
        return "\n".join(text for block in content if (text := extract_reasoning_content(block)))
    kind = _field(content, "type")
    if kind in {"reasoning", "thinking"}:
        parts = []
        for name in ("text", "thinking", "reasoning"):
            value = _field(content, name)
            if isinstance(value, str) and value:
                parts.append(value)
        summary = _field(content, "summary")
        if isinstance(summary, list):
            for block in summary:
                value = _field(block, "text")
                if isinstance(value, str) and value:
                    parts.append(value)
        return "\n".join(parts)
    if kind in {"message", "content"}:
        return extract_reasoning_content(_field(content, "content"))
    return ""
