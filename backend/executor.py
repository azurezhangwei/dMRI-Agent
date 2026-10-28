from __future__ import annotations

from backend.llm_runtime import (
    _empty_llm_retry_limit as _empty_llm_retry_limit,
    _llm_stream_retry_limit as _llm_stream_retry_limit,
    _llm_stream_retry_interval_seconds as _llm_stream_retry_interval_seconds,
    _llm_stream_hard_timeout_seconds as _llm_stream_hard_timeout_seconds,
    _llm_stream_hard_timeout as _llm_stream_hard_timeout,
    _extract_usage_metadata as _extract_usage_metadata,
    _normalize_usage as _normalize_usage,
    _summarize_chunk as _summarize_chunk,
    _chunk_diagnostics as _chunk_diagnostics,
    _stream_diagnostics as _stream_diagnostics,
)

import faulthandler
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.env_loader import load_env
from backend.llm_factory import build_chat_llm
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI

from backend.context import (
    build_env_context,
    build_initial_history,
    compression_decision,
    compress_history,
    is_context_window_error,
)
from backend.executor_approval import request_executor_tool_approval
from backend.prompt_processing import expand_at_file
from backend.path_mapping import map_container_to_host
from backend.prompt_cache import apply_prompt_cache_markers
from backend.llm_content import (
    coerce_visible_content,
)
from backend.rollout_log import format_rollout_records, resolve_session_id, write_jsonl
from backend.tool_call_merge import merge_tool_calls
from backend.tool_call_recovery import resolve_calls_with_recovery
from backend.tool_runner import handle_tool_calls
from backend.system_prompt import get_executor_system_prompt
from backend.task_routing import is_slicer_instruction
from backend.tools import ToolSpec, build_tools
from backend.truncation import TruncationPolicy, formatted_truncate_text


ROOT_DIR = Path(__file__).resolve().parents[1]
EMPTY_LLM_RETRY_PROMPT = (
    "Your previous executor turn returned no visible content and no tool calls. "
    "Do not stop silently. If the stage is not complete, continue by calling the "
    "next appropriate tool and strictly follow the loaded protocol. If the stage "
    "is complete or blocked, provide a concise explicit summary."
)


def _parse_tool_output_json(output: str) -> Optional[dict[str, Any]]:
    text = str(output or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_failure_signature(tool_name: str, output: str) -> tuple[bool, str]:
    text = str(output or "").strip()
    lower = text.lower()
    if lower.startswith("[error]"):
        reason = text.splitlines()[0][:240]
        return True, f"{tool_name}:{reason}"

    payload = _parse_tool_output_json(text)
    if payload is None:
        return False, ""

    if payload.get("ok") is False:
        reason = str(
            payload.get("error")
            or payload.get("detail")
            or payload.get("message")
            or "tool returned ok=false"
        ).strip()
        return True, f"{tool_name}:{reason[:240]}"

    status = str(payload.get("status", "")).strip().lower()
    if status in {"failed", "error", "timeout", "cancelled"}:
        reason = str(payload.get("summary") or payload.get("error") or status).strip()
        return True, f"{tool_name}:{reason[:240]}"

    # Structured run_shell output can encode policy blocks in the `output` field.
    nested_output = payload.get("output")
    if tool_name == "run_shell" and isinstance(nested_output, str):
        nested = nested_output.strip().lower()
        if nested.startswith("blocked:") or "not permitted" in nested:
            return True, f"{tool_name}:{nested_output.strip()[:240]}"

    return False, ""


def _claimed_uninvoked_tools(
    summary: str,
    known_tools: list[str],
    invoked_tool_counts: dict[str, int],
) -> list[str]:
    """Find uninvoked tools presented as direct calls in a summary."""
    claimed: set[str] = set()
    for line in str(summary or "").splitlines():
        normalized = line.strip()
        is_table_row = normalized.startswith("|") and normalized.endswith("|")
        is_tool_claim = bool(
            re.search(
                r"(?i)\b(?:tools?\s+(?:used|called|invoked)|called|invoked)\b|\u4f7f\u7528\u5de5\u5177|\u8c03\u7528\u5de5\u5177",
                normalized,
            )
        )
        if not is_table_row and not is_tool_claim:
            continue
        for name in known_tools:
            if invoked_tool_counts.get(name, 0) > 0:
                continue
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                normalized,
            ):
                claimed.add(name)
    return sorted(claimed)


def _parse_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _slicer_soft_guardrail_enabled() -> bool:
    raw = str(os.environ.get("MRI_AGENT_SLICER_SOFT_GUARDRAIL", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _slicer_non_retryable_failure_message(
    tool_name: str,
    args: Dict[str, Any],
    output: str,
) -> Optional[str]:
    if tool_name != "run_slicer_module":
        return None
    plain = str(output or "").strip()
    plain_lower = plain.lower()
    if plain_lower.startswith("[error]") and "validation error for run_slicer_module" in plain_lower:
        if "module" in plain_lower and "field required" in plain_lower:
            return (
                "Non-retryable `run_slicer_module` failure (`invalid_request`): "
                "tool call payload is missing required field `module`."
            )
    payload = _parse_tool_output_json(output)
    if payload is None or payload.get("ok") is not False:
        return None

    retryable = _parse_optional_bool(payload.get("retryable"))
    error_type = str(payload.get("error_type") or "").strip().lower()
    non_retryable_types = {
        "module_not_found",
        "parameter_schema_mismatch",
        "conversion_failed",
        "file_format_invalid",
        "unsupported_module_route",
        "module_logic_unavailable",
        "invalid_request",
    }
    if retryable is True:
        return None
    if retryable is None and error_type and error_type not in non_retryable_types:
        return None

    module_name = str(
        payload.get("resolved_module")
        or payload.get("requested_module")
        or payload.get("module")
        or args.get("module")
        or ""
    ).strip() or "<unknown>"
    reason = str(
        payload.get("error")
        or payload.get("detail")
        or payload.get("message")
        or "module execution failed"
    ).strip()
    hint = str(payload.get("hint") or "").strip()
    if hint:
        return (
            f"Non-retryable `run_slicer_module` failure for `{module_name}` "
            f"({error_type or 'non_retryable'}): {reason}. Hint: {hint}"
        )
    return (
        f"Non-retryable `run_slicer_module` failure for `{module_name}` "
        f"({error_type or 'non_retryable'}): {reason}"
    )


@dataclass
class ExecutorResult:
    status: str
    summary: str
    output_paths: List[str]
    diff: str
    usage: Optional[Dict[str, int]] = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "status": self.status,
                "summary": self.summary,
                "output_paths": self.output_paths,
                "diff": self.diff,
                "usage": self.usage,
            },
            ensure_ascii=True,
        )


def _load_env() -> None:
    load_env()


def _build_llm() -> ChatOpenAI:
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    return build_chat_llm(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0.2,
        streaming=True,
    )


def _snapshot_paths(paths: List[str]) -> Dict[str, List[str]]:
    snapshot: Dict[str, List[str]] = {}
    max_depth = int(os.environ.get("DIFF_MAX_DEPTH", "3"))
    max_entries = int(os.environ.get("DIFF_MAX_ENTRIES", "200"))
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = map_container_to_host(path.resolve())
        snapshot[raw] = _snapshot_tree(path, max_depth, max_entries)
    return snapshot


def _snapshot_tree(path: Path, max_depth: int, max_entries: int) -> List[str]:
    if not path.exists():
        return []
    if path.is_file():
        return [path.name]
    entries: List[str] = []
    queue: List[tuple[Path, int]] = [(path, 0)]
    while queue and len(entries) < max_entries:
        current, depth = queue.pop(0)
        if depth > max_depth:
            continue
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except PermissionError:
            continue
        for child in children:
            if len(entries) >= max_entries:
                break
            rel = child.relative_to(path)
            label = f"{rel}/" if child.is_dir() else str(rel)
            entries.append(label)
            if child.is_dir():
                queue.append((child, depth + 1))
    return entries


def _diff_snapshots(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> str:
    lines: List[str] = []
    max_lines = int(os.environ.get("DIFF_MAX_LINES", "200"))
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
        before_set = set(before.get(key, []))
        after_set = set(after.get(key, []))
        added = sorted(after_set - before_set)
        removed = sorted(before_set - after_set)
        if not added and not removed:
            continue
        lines.append(f"Changes under {key}:")
        if added:
            lines.append(f"Added ({len(added)}):")
            for item in added[: max_lines]:
                lines.append(f"+ {item}")
            if len(added) > max_lines:
                lines.append(f"+ ... {len(added) - max_lines} more")
        if removed:
            lines.append(f"Removed ({len(removed)}):")
            for item in removed[: max_lines]:
                lines.append(f"- {item}")
            if len(removed) > max_lines:
                lines.append(f"- ... {len(removed) - max_lines} more")
    return "\n".join(lines)


def _get_diff_truncation_policy() -> TruncationPolicy:
    mode = os.environ.get("DIFF_TRUNCATION_MODE") or os.environ.get(
        "TOOL_TRUNCATION_MODE", "bytes"
    )
    raw_limit = os.environ.get("DIFF_TRUNCATION_LIMIT") or os.environ.get(
        "TOOL_TRUNCATION_LIMIT"
    ) or os.environ.get("TOOL_TRUNCATE_THRESHOLD", "25000")
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 25000
    mode = str(mode).lower().strip()
    if mode not in {"bytes", "tokens"}:
        mode = "bytes"
    return TruncationPolicy(mode=mode, limit=max(0, limit))


def _collect_added(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> List[str]:
    added_paths: List[str] = []
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
        before_set = set(before.get(key, []))
        after_set = set(after.get(key, []))
        added = sorted(after_set - before_set)
        for item in added:
            if item:
                added_paths.append(f"{key.rstrip('/')}/{item}")
    return added_paths


def _build_history(system_prompt: str, protocol_id: str | None) -> List[BaseMessage]:
    env_context = build_env_context(os.getcwd())
    merged_system_prompt = system_prompt
    if protocol_id:
        merged_system_prompt = (
            f"{system_prompt}\n\n"
            f"Active protocol id: {protocol_id}\n"
            "Treat the active protocol as binding for this stage."
        )
    return build_initial_history(merged_system_prompt, env_context)


def _default_paths(paths: List[str]) -> List[str]:
    if paths:
        return paths
    processed = os.environ.get("PROCESSED_DIR", "").strip() or "processed"
    if processed:
        return [processed]
    return []


def _is_likely_slicer_instruction(instruction: str) -> bool:
    return is_slicer_instruction(instruction)


def _build_authoritative_inputs_block(
    paths: List[str],
    expected_outputs: List[str],
    *,
    max_paths: int = 12,
    max_outputs: int = 8,
) -> str:
    path_items = [str(p).strip() for p in (paths or []) if str(p).strip()]
    output_items = [str(p).strip() for p in (expected_outputs or []) if str(p).strip()]
    if not path_items and not output_items:
        return ""

    dedup_paths = list(dict.fromkeys(path_items))[: max(1, max_paths)]
    dedup_outputs = list(dict.fromkeys(output_items))[: max(1, max_outputs)]
    lines = [
        "Authoritative inputs (must prefer before any global scan):",
        *[f"- {p}" for p in dedup_paths],
    ]
    if dedup_outputs:
        lines.extend(
            [
                "",
                "Expected outputs (verify/write here):",
                *[f"- {p}" for p in dedup_outputs],
            ]
        )
    lines.extend(
        [
            "",
            "Execution constraint:",
            "- Do not run broad list_dir/glob across /workspace/docker_data/data unless these paths are missing or invalid.",
        ]
    )
    return "\n".join(lines)


def _coerce_chunk_content(content: Any) -> str:
    return coerce_visible_content(content)


def _stream_executor_llm_once(
    *,
    llm_with_tools: Any,
    history: List[BaseMessage],
    log_file: Path | None,
    turn: int,
    prior_usage: Dict[str, int] | None,
) -> tuple[List[str], Dict[str, Dict[str, Any]], List[Dict[str, Any]], int, Dict[str, int] | None]:
    content_buffer: List[str] = []
    tool_calls: Dict[str, Dict[str, Any]] = {}
    chunk_records: List[Dict[str, Any]] = []
    usage = prior_usage
    chunk_count = 0
    stream_start = time.time()
    request_history = apply_prompt_cache_markers(history)
    hang_seconds = int(os.environ.get("STREAM_HANG_SECONDS", "0") or 0)
    last_chunk_time = stream_start
    stop_event = None
    if hang_seconds > 0 and log_file:
        stop_event = threading.Event()

        def watch_hang() -> None:
            while not stop_event.wait(hang_seconds):
                elapsed = time.time() - last_chunk_time
                if elapsed >= hang_seconds:
                    _log(
                        log_file,
                        "stream_hang",
                        {
                            "turn": turn,
                            "elapsed": elapsed,
                            "chunks": chunk_count,
                        },
                    )

        threading.Thread(target=watch_hang, daemon=True).start()
    try:
        if log_file:
            _log(
                log_file,
                "llm_prompt",
                {"turn": turn, "messages": [_serialize_message(m) for m in request_history]},
            )
            _log(log_file, "llm_stream_start", {"turn": turn})
        with _llm_stream_hard_timeout() as reset_timeout:
            stream = llm_with_tools.stream(request_history)
            for chunk in stream:
                chunk_count += 1
                last_chunk_time = time.time()
                if reset_timeout:
                    reset_timeout()
                try:
                    chunk_content = _coerce_chunk_content(getattr(chunk, "content", ""))
                    raw_tool_calls = getattr(chunk, "tool_calls", None)
                    raw_tool_chunks = getattr(chunk, "tool_call_chunks", None)
                    record: Dict[str, Any] = {
                        "index": chunk_count,
                        "ts": last_chunk_time,
                        "content": chunk_content,
                        "tool_calls": raw_tool_calls,
                        "tool_call_chunks": raw_tool_chunks,
                        "diagnostics": _chunk_diagnostics(chunk),
                        "reasoning_content": (getattr(chunk, "additional_kwargs", {}) or {}).get("reasoning_content", ""),
                    }
                    if log_file and os.environ.get("LOG_CHUNK_META", ""):
                        record["chunk_meta"] = _summarize_chunk(chunk)
                    chunk_records.append(record)
                    if chunk_content:
                        content_buffer.append(chunk_content)
                    usage_candidate = _extract_usage_metadata(chunk)
                    if usage_candidate:
                        usage = usage_candidate
                    merge_tool_calls(tool_calls, chunk)
                except Exception as exc:  # noqa: BLE001
                    if log_file:
                        _log(
                            log_file,
                            "error_traceback",
                            {
                                "where": "executor_chunk",
                                "error": str(exc),
                                "traceback": traceback.format_exc(),
                                "chunk_meta": _summarize_chunk(chunk),
                            },
                        )
                    raise
        if log_file:
            _log(log_file, "llm_stream_end", {"turn": turn})
            _log(log_file, "llm_stream_chunks", {"turn": turn, "chunks": chunk_records})
            _log(
                log_file,
                "llm_stream_stats",
                _stream_diagnostics(
                    turn=turn,
                    chunk_records=chunk_records,
                    chunk_count=chunk_count,
                    elapsed=time.time() - stream_start,
                    content_chars=sum(len(part) for part in content_buffer),
                    tool_call_count=len([v for v in tool_calls.values() if v.get("name")]),
                    usage_seen=usage is not None,
                ),
            )
            if usage:
                _log(log_file, "llm_usage", usage)
        return content_buffer, tool_calls, chunk_records, chunk_count, usage
    finally:
        if stop_event:
            stop_event.set()


def _stream_executor_llm_with_retries(
    *,
    llm_with_tools: Any,
    history: List[BaseMessage],
    log_file: Path | None,
    turn: int,
    prior_usage: Dict[str, int] | None,
) -> tuple[List[str], Dict[str, Dict[str, Any]], List[Dict[str, Any]], int, Dict[str, int] | None]:
    retry_limit = _llm_stream_retry_limit()
    retry_interval = _llm_stream_retry_interval_seconds()
    for attempt in range(retry_limit + 1):
        try:
            return _stream_executor_llm_once(
                llm_with_tools=llm_with_tools,
                history=history,
                log_file=log_file,
                turn=turn,
                prior_usage=prior_usage,
            )
        except Exception as exc:  # noqa: BLE001
            if is_context_window_error(exc):
                if log_file:
                    _log(
                        log_file,
                        "llm_context_window_error",
                        {"turn": turn, "error": str(exc)},
                    )
                raise
            remaining = retry_limit - attempt
            if log_file:
                _log(
                    log_file,
                    "llm_stream_retry",
                    {
                        "turn": turn,
                        "attempt": attempt + 1,
                        "max_attempts": retry_limit + 1,
                        "remaining_retries": remaining,
                        "sleep_seconds": retry_interval if remaining > 0 else 0,
                        "error": str(exc),
                    },
                )
            if remaining <= 0:
                raise
            time.sleep(retry_interval)
    raise RuntimeError("unreachable LLM stream retry state")


def _maybe_compress_history(
    *,
    llm: ChatOpenAI,
    history: List[BaseMessage],
    model_name: str,
    log_file: Path | None,
    force: bool,
) -> List[BaseMessage]:
    system_msg = None
    body = history
    if body and isinstance(body[0], SystemMessage):
        system_msg = body[0]
        body = body[1:]
    decision = compression_decision(body, llm, force)
    if not decision.should_compress:
        return history
    result = compress_history(llm, body, model_name, force)
    if log_file:
        _log(
            log_file,
            "compression_attempt",
            {
                "force": force,
                "reason": result.reason,
                "status": result.status,
                "summary": result.summary,
                "split_point": result.split_point,
                "original_tokens": result.original_tokens,
                "new_tokens": result.new_tokens,
                "original_len": result.original_len,
                "new_len": result.new_len,
                "trigger_tokens": result.trigger_tokens,
                "headroom_tokens": result.headroom_tokens,
                "effective_trigger_tokens": result.effective_trigger_tokens,
                "context_window_tokens": result.context_window_tokens,
                "testament_valid": result.testament_valid,
                "testament_sections": list(result.testament_sections),
                "summary_attempts": result.summary_attempts,
                "validation_error": result.validation_error,
            },
        )
    if result.new_history is None:
        return history
    if system_msg:
        return [system_msg, *result.new_history]
    return result.new_history


def _usage_prompt_tokens(usage: Dict[str, int] | None) -> int:
    if not usage:
        return 0
    for key in ("prompt_tokens", "promptTokens", "input_tokens", "inputTokens"):
        raw = usage.get(key)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


def _usage_requires_compression(
    *,
    llm: ChatOpenAI,
    history: List[BaseMessage],
    usage: Dict[str, int] | None,
) -> bool:
    prompt_tokens = _usage_prompt_tokens(usage)
    if prompt_tokens <= 0:
        return False
    body = history[1:] if history and isinstance(history[0], SystemMessage) else history
    decision = compression_decision(body, llm, False)
    return prompt_tokens >= decision.effective_trigger_tokens


def run_executor(
    instruction: str,
    paths: List[str],
    expected_outputs: List[str],
    log_path: str | None,
    protocol_id: str | None,
) -> ExecutorResult:
    paths = _default_paths(paths)
    likely_slicer_stage = _is_likely_slicer_instruction(str(instruction or ""))
    llm = _build_llm()
    system_prompt = get_executor_system_prompt()
    history = _build_history(system_prompt, protocol_id)
    processed, _notices, _injects = expand_at_file(instruction, os.getcwd())
    if likely_slicer_stage:
        authoritative = _build_authoritative_inputs_block(paths, expected_outputs)
        if authoritative:
            processed = f"{processed.rstrip()}\n\n{authoritative}"
    history.append(HumanMessage(content=processed))

    tools = build_tools(include_executor=False)
    llm_with_tools = llm.bind_tools([spec.tool for spec in tools.values()])

    log_file = Path(log_path) if log_path else None
    debug_handle = _install_debug_handlers(log_file)
    if log_file:
        _log(
            log_file,
            "executor_start",
            {"instruction": instruction, "protocol_id": protocol_id},
        )

    before = _snapshot_paths(paths)
    status = "success"
    summary = ""
    usage: Dict[str, int] | None = None
    invoked_tool_counts: Dict[str, int] = {}
    summary_audit_retries = 0

    base_max_turns = int(os.environ.get("EXECUTOR_MAX_TURNS", "80"))
    slicer_max_turns = int(os.environ.get("EXECUTOR_MAX_TURNS_SLICER", "40"))
    if likely_slicer_stage:
        max_turns = max(5, slicer_max_turns)
    else:
        max_turns = max(5, base_max_turns)
    empty_turns = 0
    empty_retry_limit = _empty_llm_retry_limit()
    repeat_tool_failure_limit = max(
        2, int(os.environ.get("EXECUTOR_REPEAT_TOOL_FAILURE_LIMIT", "3") or "3")
    )
    blocked_shell_limit = max(
        1, int(os.environ.get("EXECUTOR_SLICER_BLOCKED_SHELL_LIMIT", "1") or "1")
    )
    last_failure_signature = ""
    last_failure_message = ""
    same_failure_streak = 0
    blocked_slicer_shell_count = 0
    slicer_module_failures: Dict[str, int] = {}
    rejected_tool_name = ""

    for turn_idx in range(max_turns):
        history = _maybe_compress_history(
            llm=llm,
            history=history,
            model_name=os.environ.get("LLM_MODEL", ""),
            log_file=log_file,
            force=_usage_requires_compression(llm=llm, history=history, usage=usage),
        )
        try:
            content_buffer, tool_calls, chunk_records, chunk_count, usage = (
                _stream_executor_llm_with_retries(
                    llm_with_tools=llm_with_tools,
                    history=history,
                    log_file=log_file,
                    turn=turn_idx + 1,
                    prior_usage=usage,
                )
            )
        except Exception as exc:  # noqa: BLE001
            if is_context_window_error(exc):
                compressed_history = _maybe_compress_history(
                    llm=llm,
                    history=history,
                    model_name=os.environ.get("LLM_MODEL", ""),
                    log_file=log_file,
                    force=True,
                )
                if compressed_history is not history:
                    history = compressed_history
                    if log_file:
                        _log(
                            log_file,
                            "context_window_recovery",
                            {
                                "where": "executor_stream",
                                "turn": turn_idx + 1,
                                "error": str(exc),
                            },
                        )
                    continue
            status = "failed"
            summary = str(exc)
            if log_file:
                _log(
                    log_file,
                    "error_summary",
                    {"where": "executor_stream", "error": str(exc)},
                )
                _log(
                    log_file,
                    "error_traceback",
                    {"where": "executor_stream", "traceback": traceback.format_exc()},
                )
                _log(log_file, "executor_summary", {"summary": summary})
            break

        tool_call_list = [v for v in tool_calls.values() if v.get("name")]
        text = "".join(content_buffer)
        reasoning_kwargs = {"reasoning_content": "".join(
            record.get("reasoning_content", "")
            for record in chunk_records
            if isinstance(record.get("reasoning_content", ""), str)
        )}
        if log_file:
            _log(
                log_file,
                "llm_turn_summary",
                {"turn": turn_idx + 1, "content": text, "tool_calls": tool_call_list},
            )
        if tool_call_list:
            empty_turns = 0
            resolved_calls, recovery_stats = resolve_calls_with_recovery(
                tool_call_list,
                text,
            )
            if log_file and (
                recovery_stats.get("recovered_used", 0) > 0
                or recovery_stats.get("dedup_removed", 0) > 0
            ):
                _log(
                    log_file,
                    "tool_call_recovery",
                    recovery_stats,
                )
            history.append(AIMessage(content=text, tool_calls=resolved_calls, additional_kwargs=reasoning_kwargs))
            if log_file:
                _log(
                    log_file,
                    "assistant_message",
                    {"content": text, "tool_calls": resolved_calls},
                )
                _log(log_file, "executor_tool_calls", {"calls": resolved_calls})
            def on_request(
                call_id: str, name: str, args: Dict[str, Any], _spec: ToolSpec
            ) -> None:
                invoked_tool_counts[name] = invoked_tool_counts.get(name, 0) + 1
                if log_file:
                    _log(
                        log_file,
                        "tool_call_request",
                        {"callId": call_id, "name": name, "args": args},
                    )

            def on_start(
                call_id: str, name: str, args: Dict[str, Any], _spec: ToolSpec
            ) -> None:
                if log_file:
                    _log(
                        log_file,
                        "tool_call_start",
                        {"callId": call_id, "name": name, "args": args},
                    )

            def on_response(
                call_id: str,
                name: str,
                output: str,
                elapsed: float | None,
            ) -> None:
                if log_file:
                    _log(
                        log_file,
                        "executor_tool_result",
                        {"name": name, "output": output, "elapsed": elapsed},
                    )
                    _log(
                        log_file,
                        "tool_call_response",
                        {
                            "callId": call_id,
                            "name": name,
                            "output": output,
                            "elapsed": elapsed,
                        },
                    )

            def execute(spec: ToolSpec, args: Dict[str, Any]) -> tuple[str, float | None]:
                nonlocal last_failure_signature
                nonlocal last_failure_message
                nonlocal same_failure_streak
                nonlocal blocked_slicer_shell_count
                start = time.time()

                if (
                    spec.tool.name == "mcp_slicer_execute_python_code"
                    and any(v >= 2 for v in slicer_module_failures.values())
                ):
                    output = (
                        "[error] blocked: repeated run_slicer_module failure detected; "
                        "do not continue ad-hoc mcp_slicer_execute_python_code probing. "
                        "Stop this stage and report the failing module/error."
                    )
                    is_failure, signature = _tool_failure_signature(spec.tool.name, output)
                    if is_failure:
                        if signature == last_failure_signature:
                            same_failure_streak += 1
                        else:
                            last_failure_signature = signature
                            same_failure_streak = 1
                        last_failure_message = signature
                    return output, time.time() - start

                try:
                    output = str(spec.tool.invoke(args))
                except Exception as exc:  # noqa: BLE001
                    output = f"[error] {exc}"
                    if log_file:
                        _log(
                            log_file,
                            "error_summary",
                            {
                                "where": "executor_tool",
                                "tool": spec.tool.name,
                                "error": str(exc),
                            },
                        )
                is_failure, signature = _tool_failure_signature(spec.tool.name, output)
                if is_failure:
                    if signature == last_failure_signature:
                        same_failure_streak += 1
                    else:
                        last_failure_signature = signature
                        same_failure_streak = 1
                    last_failure_message = signature
                else:
                    last_failure_signature = ""
                    same_failure_streak = 0
                    last_failure_message = ""

                if (
                    spec.tool.name == "run_shell"
                    and is_failure
                    and "direct slicer cli execution via run_shell is disabled"
                    in output.lower()
                ):
                    blocked_slicer_shell_count += 1

                if spec.tool.name == "run_slicer_module":
                    module_name = str((args or {}).get("module") or "").strip()
                    non_retryable_message = _slicer_non_retryable_failure_message(
                        spec.tool.name,
                        args or {},
                        output,
                    )
                    if non_retryable_message:
                        if _slicer_soft_guardrail_enabled():
                            # Soft guardrail mode: keep non-retryable classification,
                            # but allow the LLM one bounded chance to pick an
                            # alternative route (for example non-Slicer fallback)
                            # instead of failing the whole stage immediately.
                            if module_name:
                                slicer_module_failures[module_name] = (
                                    slicer_module_failures.get(module_name, 0) + 1
                                )
                            last_failure_signature = (
                                f"run_slicer_module:{module_name or '<unknown>'}:non_retryable_soft"
                            )
                            same_failure_streak = max(same_failure_streak, 1)
                            last_failure_message = (
                                non_retryable_message
                                + " Soft guardrail: choose one alternative route or stop."
                            )
                        else:
                            repeated_signature = f"run_slicer_module:{module_name or '<unknown>'}:non_retryable"
                            last_failure_signature = repeated_signature
                            same_failure_streak = max(
                                same_failure_streak, repeat_tool_failure_limit
                            )
                            last_failure_message = non_retryable_message
                            if module_name:
                                slicer_module_failures[module_name] = max(
                                    slicer_module_failures.get(module_name, 0) + 1,
                                    2,
                                )
                        return output, time.time() - start
                    if module_name:
                        if is_failure:
                            failure_count = slicer_module_failures.get(module_name, 0) + 1
                            slicer_module_failures[module_name] = failure_count
                            if failure_count >= 2:
                                repeated_signature = f"run_slicer_module:{module_name}:repeated_failure"
                                last_failure_signature = repeated_signature
                                same_failure_streak = max(
                                    same_failure_streak, repeat_tool_failure_limit
                                )
                                last_failure_message = (
                                    f"`run_slicer_module` for module `{module_name}` failed repeatedly. "
                                    f"Latest error: {signature}"
                                )
                        else:
                            slicer_module_failures[module_name] = 0
                return output, time.time() - start

            def approve(
                call_id: str,
                name: str,
                args: Dict[str, Any],
                spec: ToolSpec,
            ) -> bool:
                nonlocal rejected_tool_name
                if not spec.dangerous:
                    return True
                approved = request_executor_tool_approval(call_id, name, args)
                if not approved:
                    rejected_tool_name = name
                return approved

            handle_tool_calls(
                resolved_calls,
                tools,
                history,
                lambda call: call.get("args") or {},
                execute,
                on_request=on_request,
                on_start=on_start,
                on_response=on_response,
                approve=approve,
            )
            if rejected_tool_name:
                status = "failed"
                summary = (
                    f"User rejected executor tool `{rejected_tool_name}`; "
                    "stopped the stage without trying an alternative route."
                )
                if log_file:
                    _log(log_file, "executor_summary", {"summary": summary})
                break
            if blocked_slicer_shell_count >= blocked_shell_limit:
                status = "failed"
                summary = (
                    "Slicer stage blocked: repeated `run_shell` attempts to invoke "
                    "Slicer CLI were denied by policy. Use "
                    "`run_slicer_module(action='inspect'|'run')` only."
                )
                if log_file:
                    _log(log_file, "executor_summary", {"summary": summary})
                break

            if same_failure_streak >= repeat_tool_failure_limit and last_failure_message:
                status = "failed"
                summary = (
                    f"Repeated tool failure ({same_failure_streak}x): "
                    f"{last_failure_message}. Stopping to avoid an infinite retry loop."
                )
                if log_file:
                    _log(log_file, "executor_summary", {"summary": summary})
                break
            continue

        if text:
            empty_turns = 0
            candidate_summary = text.strip()
            uninvoked_claims = _claimed_uninvoked_tools(
                candidate_summary,
                list(tools),
                invoked_tool_counts,
            )
            if uninvoked_claims and invoked_tool_counts and summary_audit_retries < 1:
                summary_audit_retries += 1
                actual_calls = ", ".join(
                    f"{name} x{count}" for name, count in sorted(invoked_tool_counts.items())
                )
                history.append(AIMessage(content=text, additional_kwargs=reasoning_kwargs))
                history.append(
                    HumanMessage(
                        content=(
                            "Your draft failed the authoritative tool-call audit. It presented "
                            f"these tools as direct calls even though they were not invoked: "
                            f"{', '.join(uninvoked_claims)}. Actual direct calls were: "
                            f"{actual_calls}. Rewrite the complete final summary now. Use only "
                            "actual direct tool names in operation tables. Describe evidence "
                            "nested inside another tool's response as nested evidence, not as "
                            "a separate tool call. Do not call any more tools."
                        )
                    )
                )
                if log_file:
                    _log(
                        log_file,
                        "summary_tool_audit_retry",
                        {
                            "uninvoked_claims": uninvoked_claims,
                            "invoked_tool_counts": invoked_tool_counts,
                        },
                    )
                continue
            if uninvoked_claims:
                actual_calls = ", ".join(
                    f"`{name}` x{count}"
                    for name, count in sorted(invoked_tool_counts.items())
                )
                candidate_summary = (
                    "**Authoritative tool audit:** Direct calls were "
                    f"{actual_calls or 'none'}. The draft mentioned "
                    f"{', '.join(f'`{name}`' for name in uninvoked_claims)} as direct calls, "
                    "but those calls are not present in the execution log.\n\n"
                    + candidate_summary
                )
                if log_file:
                    _log(
                        log_file,
                        "summary_tool_audit_warning",
                        {
                            "uninvoked_claims": uninvoked_claims,
                            "invoked_tool_counts": invoked_tool_counts,
                        },
                    )
            history.append(AIMessage(content=text, additional_kwargs=reasoning_kwargs))
            summary = candidate_summary
            if log_file:
                _log(log_file, "assistant_message", {"content": text})
                _log(log_file, "executor_summary", {"summary": summary})
        else:
            empty_turns += 1
            elapsed = 0.0
            if chunk_records:
                first_ts = float(chunk_records[0].get("ts") or time.time())
                last_ts = float(chunk_records[-1].get("ts") or first_ts)
                elapsed = max(0.0, last_ts - first_ts)
            if log_file:
                _log(
                    log_file,
                    "empty_llm_turn",
                    {
                        "turn": turn_idx + 1,
                        "empty_turns": empty_turns,
                        "retry_limit": empty_retry_limit,
                        "stream_diagnostics": _stream_diagnostics(
                            turn=turn_idx + 1,
                            chunk_records=chunk_records,
                            chunk_count=chunk_count,
                            elapsed=elapsed,
                            content_chars=sum(len(part) for part in content_buffer),
                            tool_call_count=0,
                            usage_seen=usage is not None,
                        ),
                    },
                )
            if empty_turns <= empty_retry_limit:
                history.append(HumanMessage(content=EMPTY_LLM_RETRY_PROMPT))
                continue
            status = "failed"
            summary = (
                "LLM returned no visible content and no tool calls repeatedly; "
                "stopping executor to avoid an infinite loop."
            )
            if log_file:
                _log(log_file, "executor_summary", {"summary": summary})
        break
    else:
        status = "failed"
        summary = (
            f"Reached executor turn limit ({max_turns}) without a terminal stage "
            "result. Stopped to avoid infinite retries."
        )
        if log_file:
            _log(log_file, "executor_summary", {"summary": summary})

    if status == "success" and not str(summary or "").strip():
        status = "failed"
        summary = (
            "Executor finished without a non-empty summary; treating this stage as "
            "failed to avoid false success."
        )
        if log_file:
            _log(log_file, "executor_summary", {"summary": summary})

    after = _snapshot_paths(paths)
    diff = _diff_snapshots(before, after)
    diff = formatted_truncate_text(diff, _get_diff_truncation_policy())
    added = _collect_added(before, after)
    max_outputs = int(os.environ.get("DIFF_MAX_OUTPUTS", "200"))
    output_paths = added[:max_outputs]
    if not output_paths and expected_outputs:
        output_paths = expected_outputs
    if log_file:
        _log(
            log_file,
            "executor_finish",
            {
                "output_paths": output_paths,
                "diff_length": len(diff),
            },
        )
    return ExecutorResult(
        status=status,
        summary=summary,
        output_paths=output_paths,
        diff=diff,
        usage=usage,
    )


def main() -> None:
    _load_env()
    os.environ["AGENT_ROLE"] = "executor"
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw else {}
    instruction = str(payload.get("instruction", "")).strip()
    paths = payload.get("paths") or []
    expected_outputs = payload.get("expected_outputs") or []
    log_path = payload.get("log_path") or os.environ.get("LOG_PATH")
    protocol_id = payload.get("protocol_id")
    result = run_executor(
        instruction,
        paths,
        expected_outputs,
        log_path,
        protocol_id,
    )
    sys.stdout.write(result.to_json())


def _log(log_path: Path, kind: str, payload: Dict[str, Any]) -> None:
    line = _format_log_line(kind, payload)
    if line is None:
        return
    ts = time.time()
    record = {"ts": ts, **line}
    try:
        write_jsonl(log_path, record)
    except Exception:
        pass
    rollout_path = os.environ.get("SESSION_ROLLOUT_LOG_PATH", "").strip()
    if not rollout_path:
        return
    try:
        session_dir = Path(os.environ.get("LOG_DIR", str(log_path.parent))).resolve()
        session_id = os.environ.get("LOG_SESSION_ID", "").strip() or resolve_session_id(session_dir)
        records = format_rollout_records(
            kind=kind,
            payload=payload,
            agent_role=payload.get("role", "executor"),
            session_id=session_id,
            log_dir=session_dir,
            model=os.environ.get("LLM_MODEL", ""),
            base_url=os.environ.get("LLM_BASE_URL", ""),
            cwd=os.getcwd(),
            ts=ts,
        )
        for rollout_record in records:
            write_jsonl(Path(rollout_path), rollout_record)
    except Exception:
        return


def _format_log_line(kind: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    role = payload.get("role", "executor")
    if kind == "executor_start":
        return {
            "type": "human",
            "role": role,
            "text": payload.get("instruction", ""),
            "source": "planner_instruction",
        }
    if kind == "assistant_message":
        return {
            "type": "llm",
            "role": role,
            "text": payload.get("content", ""),
        }
    if kind == "tool_call_request":
        return {
            "type": "tool_call",
            "role": role,
            "call_id": payload.get("callId"),
            "name": payload.get("name"),
            "args": payload.get("args"),
        }
    if kind == "tool_call_response":
        return {
            "type": "tool_response",
            "role": role,
            "call_id": payload.get("callId"),
            "name": payload.get("name"),
            "is_error": payload.get("isError"),
            "elapsed": payload.get("elapsed"),
            "output": payload.get("output"),
        }
    if kind in {
        "compression_attempt",
        "error_summary",
        "error_traceback",
        "empty_llm_turn",
        "signal",
        "signal_process_info",
        "thread_exception",
        "stream_hang",
        "llm_usage",
        "llm_stream_stats",
        "llm_stream_retry",
        "context_window_recovery",
        "executor_finish",
    }:
        return {"type": "meta", "event": kind, "role": role, **payload}
    return None


def _serialize_message(message: BaseMessage) -> Dict[str, Any]:
    content = getattr(message, "content", "")
    data: Dict[str, Any] = {"type": message.type, "content": content}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        data["tool_calls"] = tool_calls
    return data


def _install_debug_handlers(log_file: Path | None) -> object | None:
    if not log_file:
        return None
    try:
        descriptor = os.open(log_file.with_name("executor-crash.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        handle = os.fdopen(descriptor, "a", encoding="utf-8")
        faulthandler.enable(file=handle, all_threads=True)
    except Exception:
        return None

    handling = {"active": False}

    def _signal_handler(signum: int, _frame: object) -> None:
        if handling["active"]:
            return
        handling["active"] = True
        try:
            name = signal.Signals(signum).name
        except Exception:
            name = str(signum)
        _log(log_file, "signal", {"signal": signum, "name": name})
        try:
            faulthandler.dump_traceback(file=handle, all_threads=True)
        except Exception:
            pass
        action = os.environ.get("HANDLE_SIGTERM", "exit").lower()
        if action != "log_only":
            try:
                signal.signal(signum, signal.SIG_DFL)
            except Exception:
                pass
            os.kill(os.getpid(), signum)

    for sig in (getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGQUIT", "SIGUSR1") if hasattr(signal, name)):
        try:
            signal.signal(sig, _signal_handler)
        except Exception:
            continue

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        _log(
            log_file,
            "thread_exception",
            {"thread": args.thread.name, "error": str(args.exc_value)},
        )
        _log(
            log_file,
            "error_traceback",
            {
                "where": "thread",
                "traceback": "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                ),
            },
        )

    threading.excepthook = _thread_excepthook

    def _sys_excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: object,
    ) -> None:
        _log(
            log_file,
            "error_traceback",
            {
                "where": "sys",
                "traceback": "".join(
                    traceback.format_exception(exc_type, exc_value, exc_traceback)
                ),
            },
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = _sys_excepthook
    return handle


if __name__ == "__main__":
    main()
