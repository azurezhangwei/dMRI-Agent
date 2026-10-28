"""SDK adaptations for the supported completion and messages APIs."""

from __future__ import annotations

from typing import Any
import json
import os
from functools import cached_property
from urllib.parse import urlparse

import httpx
from anthropic import Client as MessagesClient, AsyncClient as AsyncMessagesClient
from langchain_anthropic import ChatAnthropic

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI


class ReasoningCompletionAdapter(ChatOpenAI):
    def get_num_tokens_from_messages(self, messages, tools=None):
        try:
            count = super().get_num_tokens_from_messages(messages, tools=tools)
        except (NotImplementedError, KeyError):
            # Some supported model names have no native tokenizer mapping.
            visible = "".join(
                f"{message.type}:{message.content}\n" + json.dumps(getattr(message, "tool_calls", []), ensure_ascii=False)
                for message in messages
            )
            count = max(1, len(visible) // 4)
        for message in messages:
            reasoning = message.additional_kwargs.get("reasoning_content", "")
            if isinstance(reasoning, str) and reasoning:
                count += max(1, len(reasoning) // 4)
        return count

    def _create_chat_result(self, response: Any, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get("choices", [])):
            reasoning = (choice.get("message") or {}).get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is None or not isinstance(generation.message, AIMessageChunk):
            return generation
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation

    def _get_request_payload(self, input_: Any, *, stop=None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        source_messages = self._convert_input(input_).to_messages()
        for wire_message, source_message in zip(payload.get("messages", []), source_messages):
            if isinstance(source_message, AIMessage):
                reasoning = source_message.additional_kwargs.get("reasoning_content", "")
                if isinstance(reasoning, str):
                    wire_message["reasoning_content"] = reasoning
        return payload



def native_thinking_options() -> dict[str, Any] | None:
    raw_json = (os.environ.get("LLM_ANTHROPIC_THINKING_JSON") or "").strip()
    if raw_json:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ValueError("LLM_ANTHROPIC_THINKING_JSON must decode to a JSON object")
        return parsed

    raw = (os.environ.get("LLM_ANTHROPIC_THINKING") or "").strip().lower()
    if not raw or raw in {"none", "off", "false", "0"}:
        return None
    if raw == "adaptive":
        return {"type": "adaptive"}
    raise ValueError(
        "LLM_ANTHROPIC_THINKING must be one of: adaptive, none/off, "
        "or use LLM_ANTHROPIC_THINKING_JSON"
    )


class NativeMessagesAdapter(ChatAnthropic):
    """Apply per-client network policy without changing the process environment."""

    def _http_options(self) -> dict[str, Any]:
        disable_proxy = os.environ.get("LLM_DISABLE_PROXY", "1").strip().lower()
        options = {
            "timeout": self._client_params.get("timeout", 600.0),
            "trust_env": disable_proxy in {"0", "false", "no", "off"},
            "http2": False,
        }
        if self.anthropic_proxy:
            options["proxy"] = self.anthropic_proxy
        return options

    @cached_property
    def _client(self):
        return MessagesClient(**self._client_params, http_client=httpx.Client(**self._http_options()))

    @cached_property
    def _async_client(self):
        return AsyncMessagesClient(**self._client_params, http_client=httpx.AsyncClient(**self._http_options()))


def build_completion_adapter(base_url: str, options: dict[str, Any]):
    """Select the wire-format adaptation required by the official endpoint."""
    options = dict(options)
    adapter = ChatOpenAI
    if (urlparse(base_url).hostname or "").lower() == "api.deepseek.com":
        extra = dict(options.get("extra_body") or {})
        extra["thinking"] = {"type": "enabled"}
        options["extra_body"] = extra
        if options.get("reasoning_effort") not in {"low", "high", "max"}:
            options["reasoning_effort"] = "high"
        adapter = ReasoningCompletionAdapter
    return adapter(**options)
