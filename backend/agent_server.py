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
import subprocess
import signal
import ctypes
import sys
import threading
import time
import traceback
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List

from backend.env_loader import load_env, resolve_env_file
from backend.llm_factory import build_chat_llm
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from backend.context import (
    build_env_context,
    build_initial_history,
    compression_decision,
    compress_history,
    estimate_tokens,
    is_context_window_error,
)
from backend.cancel_state import cancel_generation, request_cancel
from backend.executor_approval import executor_approval_handler
from backend.prompt_processing import expand_at_file
from backend.request_controls import controlled_user_prompt, normalize_protocol_ids, normalize_turn_mode
from backend.prompt_cache import apply_prompt_cache_markers
from backend.llm_content import (
    coerce_visible_content,
)
from backend.experience_memory import load_memory_context
from backend.rollout_log import (
    build_manifest,
    format_rollout_records,
    init_rollout_log_path,
    project_log_key,
    resolve_session_id,
    resolve_log_root,
    write_json,
    write_jsonl,
)
from backend.system_prompt import get_main_system_prompt
from backend.stage_guard import (
    stage_result_is_terminal_failure,
    tool_result_is_user_rejection,
    unsupported_completion_claim,
)
from backend.events import (
    content_event,
    error_event,
    finished_event,
    info_event,
    thought_event,
    plan_ready_event,
    plan_cleared_event,
    stage_done_event,
    stage_start_event,
    shutdown_complete_event,
    tool_call_request_event,
    tool_call_response_event,
    turn_started_event,
    usage_event,
)
from backend.tools import ToolSpec, build_tools
from backend.tool_call_merge import merge_tool_calls
from backend.tool_call_recovery import resolve_calls_with_recovery
from backend.tool_runner import handle_tool_calls

ROOT_DIR = Path(__file__).resolve().parents[1]
MAX_TURNS = 100
PLAN_TOOL_NAMES = {
    "list_protocols",
    "load_protocol",
    "load_protocol_resource",
    "memory_search",
    "protocol_status",
    "read_file",
    "grep_files",
    "glob_files",
    "list_dir",
}


EMPTY_LLM_RETRY_PROMPT = (
    "Your previous assistant turn returned no visible content and no tool calls. "
    "Do not stop silently. If the task is not complete, continue by calling the "
    "next appropriate tool and strictly follow the specified protocol. If the "
    "task is complete or blocked, provide a concise explicit summary."
)

AUTO_CONTINUE_AFTER_STAGE_PROMPT = (
    "Unattended stage continuation is active. Based on the executor result, do one of the following in this "
    "next reply: (1) immediately call `execute_stage` for the next unfinished stage; "
    "(2) if all planned stages are complete, provide the final summary; or (3) if a "
    "true fatal blocker or a material protocol change is required, "
    "state that explicitly and stop. Do not wait for user approval between "
    "stages, and do not emit a standalone next-stage plan without a tool call."
)

SINGLE_STAGE_FINAL_PROMPT = (
    "Exactly one executor stage is allowed per user turn. The requested stage has finished. "
    "Tool access is now disabled for the rest of this turn. Summarize only the returned status, "
    "verified evidence paths, and any warnings. Do not start or propose another stage as if it "
    "were already authorized; a new explicit user message is required for the next stage."
)

GROUNDING_RETRY_PROMPT = (
    "Your previous draft claimed that an action completed, but this user turn has no successful "
    "tool evidence for that claim. Do not claim completion from plans, descriptions, or metadata. "
    "Call the required tool now, or state clearly that the action was not run or could not be verified."
)


_DICOM_UID_RE = re.compile(r"(?<![\d.])(?:\d+\.){4,}\d+(?![\d.])")
_PRIVATE_FIELD_RE = re.compile(
    r"(?i)(\b(?:PatientID|PatientName|StudyInstanceUID|SeriesInstanceUID)\b\s*[\"']?\s*[:=]\s*[\"']?)([^\s,;}\]\n\"']+)"
)


def _redact_public_text(value: str) -> str:
    redacted = _PRIVATE_FIELD_RE.sub(r"\1[REDACTED]", str(value or ""))
    return _DICOM_UID_RE.sub("[REDACTED_UID]", redacted)


def _redact_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_public_text(value)
    if isinstance(value, list):
        return [_redact_public_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_public_value(item) for key, item in value.items()}
    return value

FAILED_STAGE_FINAL_PROMPT = (
    "The preceding execute_stage result is terminal for this user request. "
    "Tool access is now disabled for the rest of this turn. Provide one concise final response "
    "using only that returned stage result: identify the failed/rejected stage, reason, returned "
    "evidence paths, and that a new explicit user instruction is required before retrying. "
    "Do not propose or emit any tool call."
)

REJECTED_TOOL_FINAL_PROMPT = (
    "The user rejected the dangerous tool `{tool_name}`. That rejection is final for this turn, "
    "and tool access is now disabled. Provide one concise final response saying which tool was "
    "rejected and that no alternate route was attempted. Do not retry, rename the operation, "
    "switch tools, inspect partial outputs, or emit any tool call. Wait for a new explicit user "
    "instruction before taking further action."
)


def _load_env() -> None:
    load_env()


def _provider_label(base_url: str) -> str:
    base = (base_url or "").lower()
    if "deepseek" in base:
        return "DeepSeek"
    if "dashscope" in base or "aliyuncs" in base:
        return "DashScope/Qwen"
    if "openai" in base:
        return "OpenAI"
    return base_url or "configured provider"


def _format_llm_error(exc: Exception) -> str:
    text = str(exc)
    provider = _provider_label(_get_env("LLM_BASE_URL"))
    model = _get_env("LLM_MODEL") or "(unknown model)"
    env_path = str(resolve_env_file())

    if "Insufficient Balance" in text or "Error code: 402" in text:
        return (
            f"{provider} returned 402 Insufficient Balance for model `{model}`. "
            f"Update `{env_path}` with a funded `LLM_API_KEY` or switch "
            f"`LLM_MODEL`/`LLM_BASE_URL` to a working provider."
        )

    invalid_key_markers = (
        "invalid_api_key",
        "Incorrect API key",
        "Error code: 401",
        "Missing required env var: LLM_API_KEY",
    )
    if any(marker in text for marker in invalid_key_markers):
        return (
            f"{provider} rejected the configured API key for model `{model}`. "
            f"Check `LLM_API_KEY`, `LLM_MODEL`, and `LLM_BASE_URL` in `{env_path}`."
        )

    return f"LLM stream failed: {text}"


def _build_llm() -> ChatOpenAI:
    api_key = _require_env("LLM_API_KEY")
    model = _require_env("LLM_MODEL")
    base_url = _require_env("LLM_BASE_URL")
    return build_chat_llm(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0.2,
        streaming=True,
    )


def _require_env(name: str) -> str:
    value = _get_env(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _get_env(name: str) -> str:
    return os.environ.get(name, "")


def _set_parent_death_signal() -> None:
    if sys.platform != "linux":
        return
    try:
        parent_pid = os.getppid()
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        PR_SET_PDEATHSIG = 1
        if prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
            return
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        return


class AgentTurnCancelled(Exception):
    pass


class InputReader:
    def __init__(self) -> None:
        self.queue: "Queue[Dict[str, Any]]" = Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "user_input":
                payload["_cancel_token"] = cancel_generation()
            elif payload.get("type") == "command" and payload.get("name") == "cancel":
                request_cancel()
                payload["_cancel_signalled"] = True
            self.queue.put(payload)

    def get(self) -> Dict[str, Any]:
        return self.queue.get()


class AgentServer:
    def __init__(self) -> None:
        os.environ["AGENT_ROLE"] = "planner"
        self.role = "planner"
        self.input_reader = InputReader()
        self.model_name = _require_env("LLM_MODEL")
        self.base_system_prompt = get_main_system_prompt()
        self.history = self._build_initial_history()
        self.tool_specs = build_tools()
        self.llm = _build_llm()
        self.llm_with_tools = self.llm.bind_tools(
            [spec.tool for spec in self.tool_specs.values()]
        )
        self.plan_tool_specs = {
            name: spec for name, spec in self.tool_specs.items() if name in PLAN_TOOL_NAMES
        }
        self.llm_with_plan_tools = self.llm.bind_tools(
            [spec.tool for spec in self.plan_tool_specs.values()]
        )
        self.log_dir = self._init_log_dir()
        self.log_root = Path(os.environ.get("LOG_ROOT_DIR", str(self.log_dir.parent)))
        self.session_id = resolve_session_id(self.log_dir)
        self.log_path = self._init_log_path()
        # Keep JSONL as the single source of truth. Human-readable markdown logs
        # duplicate information and are easy to confuse with canonical records.
        self.readable_log_path: Path | None = None
        self.rollout_log_path = self._init_rollout_log_path()
        self.manifest_path = self._init_manifest_path()
        self._faulthandler_handle = None
        self._handling_signal = False
        self._event_counter = 0
        self._plan_counter = 0
        self._pending_plan: Dict[str, Any] | None = None
        self._auto_approve_tools = _get_env("AUTO_APPROVE_TOOLS").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._tool_lock_for_turn = False
        self._tool_lock_prompt = ""
        self._turn_mode = "execute"
        self._turn_user_text = ""
        self._turn_protocol_ids: List[str] = []
        self._turn_id = ""
        self._mutating_tool_evidence_for_turn = False
        self._session_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        }
        self._install_debug_handlers()
        self._write_session_manifest()
        self._log_event(
            "session_start",
            {
                "model": self.model_name,
                "base_url": _get_env("LLM_BASE_URL"),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "cwd": os.getcwd(),
            },
        )

    def emit(self, payload: Dict[str, Any]) -> None:
        output = _redact_public_value(dict(payload))
        if self._turn_id and not output.get("turn_id"):
            output["turn_id"] = self._turn_id
        output.setdefault("event_id", self._next_event_id())
        self._log_event("event", output)
        sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
        sys.stdout.flush()

    def _next_event_id(self) -> str:
        self._event_counter += 1
        return f"{os.getpid()}-{self._event_counter}"

    def run(self) -> None:
        while True:
            msg = self.input_reader.get()
            msg_type = msg.get("type")
            if msg_type == "user_input":
                if self._pending_plan is not None:
                    self._clear_pending_plan("superseded_by_user_input")
                self._turn_id = str(msg.get("turn_id") or "")
                if self._turn_id:
                    self.emit(turn_started_event(self._turn_id).to_dict())
                text = str(msg.get("text", ""))
                processed, notices, injects = expand_at_file(text, os.getcwd())
                self._turn_user_text = processed
                self._turn_mode = normalize_turn_mode(msg.get("mode"))
                try:
                    protocol_ids = normalize_protocol_ids(msg.get("protocol_ids"))
                except ValueError as exc:
                    self.emit(error_event(str(exc)).to_dict())
                    self._turn_id = ""
                    continue
                self._turn_protocol_ids = protocol_ids
                controlled = controlled_user_prompt(processed, self._turn_mode, protocol_ids)
                self._log_event(
                    "user_input",
                    {
                        "raw": text,
                        "processed": processed,
                        "mode": self._turn_mode,
                        "protocol_ids": protocol_ids,
                    },
                )
                if injects:
                    self._log_event(
                        "injections",
                        {"items": [inject.__dict__ for inject in injects]},
                    )
                for notice in notices:
                    self.emit(info_event(notice).to_dict())
                self.history.append(HumanMessage(content=controlled))
                self._next_turn_cancel_token = int(msg.get("_cancel_token", cancel_generation()))
                self._run_agent_turn()
                if self._pending_plan is None:
                    self._turn_id = ""
            elif msg_type == "command":
                self._log_event("command", {"name": msg.get("name", "")})
                self._handle_command(str(msg.get("name", "")), bool(msg.get("_cancel_signalled")))
            elif msg_type == "plan_response":
                self._handle_plan_response(msg)
            elif msg_type == "restore_history":
                self._restore_history(msg)
            elif msg_type == "shutdown":
                self._log_event("shutdown", {})
                self.emit(shutdown_complete_event().to_dict())
                break

    def _run_agent_turn(self) -> None:
        self._turn_cancel_token = getattr(self, "_next_turn_cancel_token", cancel_generation())
        if hasattr(self, "_next_turn_cancel_token"):
            del self._next_turn_cancel_token
        try:
            self._run_agent_turn_unchecked()
        except AgentTurnCancelled:
            completed = {m.tool_call_id for m in self.history if isinstance(m, ToolMessage)}
            for message in list(self.history):
                for call in getattr(message, "tool_calls", []) or []:
                    if call["id"] not in completed:
                        self.history.append(ToolMessage(tool_call_id=call["id"], content="[cancelled] user stopped this task"))
                        completed.add(call["id"])
            self._pending_plan = None
            self._log_event("turn_cancelled", {})
            self.emit(finished_event("cancelled").to_dict())

    def _check_cancelled(self) -> None:
        if cancel_generation() != getattr(self, "_turn_cancel_token", cancel_generation()):
            raise AgentTurnCancelled()

    def _run_agent_turn_unchecked(self) -> None:
        self._tool_lock_for_turn = False
        self._tool_lock_prompt = ""
        self._mutating_tool_evidence_for_turn = False
        empty_turns = 0
        grounding_retries = 0
        empty_retry_limit = _empty_llm_retry_limit()
        for turn_idx in range(MAX_TURNS):
            self._check_cancelled()
            self._maybe_compress(False)
            self._log_event(
                "llm_prompt",
                {
                    "turn": turn_idx + 1,
                    "messages": [self._serialize_message(m) for m in self.history],
                },
            )
            try:
                self._log_event("llm_stream_start", {"turn": turn_idx + 1})
                full_text, tool_calls, usage, stream_diagnostics = self._stream_llm_with_retries(
                    turn_idx + 1
                )
                self._log_event("llm_stream_end", {"turn": turn_idx + 1})
            except AgentTurnCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if is_context_window_error(exc) and self._maybe_compress(True):
                    self._log_event(
                        "context_window_recovery",
                        {"where": "planner_stream", "turn": turn_idx + 1, "error": str(exc)},
                    )
                    continue
                friendly_error = _format_llm_error(exc)
                self._log_event(
                    "error_traceback",
                    {
                        "where": "planner_stream",
                        "traceback": traceback.format_exc(),
                    },
                )
                self._log_event(
                    "error_summary",
                    {"where": "planner_stream", "error": str(exc), "friendly_error": friendly_error},
                )
                self.emit(error_event(friendly_error).to_dict())
                return
            self._check_cancelled()
            reasoning_kwargs = {"reasoning_content": getattr(self, "_last_reasoning_content", "")}
            self._log_event(
                "llm_turn_summary",
                {"turn": turn_idx + 1, "content": full_text, "tool_calls": tool_calls},
            )
            if usage:
                usage_payload = self._update_session_usage(
                    usage,
                    turn_idx + 1,
                    source=self.role,
                )
                self._log_event("llm_usage", usage_payload)
                self.emit(usage_event(usage_payload).to_dict())
            if tool_calls:
                empty_turns = 0
                resolved_calls, recovery_stats = resolve_calls_with_recovery(
                    tool_calls,
                    full_text,
                )
                if recovery_stats.get("recovered_used", 0) > 0 or recovery_stats.get(
                    "dedup_removed", 0
                ) > 0:
                    self._log_event("tool_call_recovery", recovery_stats)
                safe_text = _redact_public_text(full_text)
                if safe_text:
                    self.emit(content_event(safe_text).to_dict())
                self.history.append(AIMessage(content=safe_text, tool_calls=resolved_calls, additional_kwargs=reasoning_kwargs))
                self._log_event(
                    "assistant_message",
                    {"content": safe_text, "tool_calls": resolved_calls},
                )
                self._handle_tool_calls(resolved_calls)
                self._check_cancelled()
                continue
            if full_text:
                empty_turns = 0
                safe_text = _redact_public_text(full_text)
                unsupported = unsupported_completion_claim(
                    safe_text,
                    user_request=self._turn_user_text,
                    has_tool_evidence=self._mutating_tool_evidence_for_turn,
                    execute_mode=self._turn_mode == "execute",
                )
                if unsupported and grounding_retries < 1:
                    grounding_retries += 1
                    self._log_event(
                        "unsupported_completion_retry",
                        {"turn": turn_idx + 1, "draft": safe_text},
                    )
                    self.history.append(AIMessage(content=safe_text, additional_kwargs=reasoning_kwargs))
                    self.history.append(HumanMessage(content=GROUNDING_RETRY_PROMPT))
                    continue
                if unsupported:
                    safe_text = (
                        "I could not verify that the requested action completed because this turn "
                        "produced no successful tool evidence. No completion status is being recorded."
                    )
                self.history.append(AIMessage(content=safe_text, additional_kwargs=reasoning_kwargs))
                self._log_event("assistant_message", {"content": safe_text})
                if self._turn_mode == "plan":
                    self._set_pending_plan(safe_text)
                    self.emit(finished_event("plan_ready").to_dict())
                    return
                self.emit(content_event(safe_text).to_dict())
            else:
                empty_turns += 1
                self._log_event(
                    "empty_llm_turn",
                    {
                        "turn": turn_idx + 1,
                        "empty_turns": empty_turns,
                        "retry_limit": empty_retry_limit,
                        "stream_diagnostics": stream_diagnostics,
                    },
                )
                if empty_turns <= empty_retry_limit:
                    retry_prompt = (
                        self._tool_lock_prompt or FAILED_STAGE_FINAL_PROMPT
                        if self._tool_lock_for_turn
                        else EMPTY_LLM_RETRY_PROMPT
                    )
                    self.history.append(HumanMessage(content=retry_prompt))
                    continue
                self.emit(
                    error_event(
                        "LLM returned no visible content and no tool calls repeatedly; "
                        "stopping to avoid an infinite loop."
                    ).to_dict()
                )
            self.emit(finished_event("stop").to_dict())
            return
        self.emit(error_event("Agent loop exceeded max iterations").to_dict())

    def _stream_llm(
        self, turn: int
    ) -> tuple[str, List[Dict[str, Any]], Dict[str, int] | None, Dict[str, Any]]:
        content_buffer: List[str] = []
        self._last_reasoning_content = ""
        last_thought_at = 0.0
        tool_calls: Dict[str, Dict[str, Any]] = {}
        chunk_records: List[Dict[str, Any]] = []
        usage: Dict[str, int] | None = None
        chunk_count = 0
        stream_start = time.time()
        hang_seconds = int(os.environ.get("STREAM_HANG_SECONDS", "0") or 0)
        last_chunk_time = stream_start
        stop_event = threading.Event()

        def watch_hang() -> None:
            while not stop_event.wait(hang_seconds):
                elapsed = time.time() - last_chunk_time
                if elapsed >= hang_seconds:
                    self._log_event(
                        "stream_hang",
                        {
                            "turn": turn,
                            "elapsed": elapsed,
                            "chunks": chunk_count,
                        },
                    )

        if hang_seconds > 0:
            threading.Thread(target=watch_hang, daemon=True).start()

        with _llm_stream_hard_timeout() as reset_timeout:
            request_history = apply_prompt_cache_markers(self.history)
            if self._tool_lock_for_turn:
                llm_client = self.llm
            elif self._turn_mode == "plan":
                llm_client = self.llm_with_plan_tools
            else:
                llm_client = self.llm_with_tools
            stream = llm_client.stream(request_history)
            for chunk in stream:
                self._check_cancelled()
                chunk_count += 1
                last_chunk_time = time.time()
                if reset_timeout:
                    reset_timeout()
                try:
                    chunk_content = self._coerce_chunk_content(getattr(chunk, "content", ""))
                    raw_tool_calls = getattr(chunk, "tool_calls", None)
                    raw_tool_chunks = getattr(chunk, "tool_call_chunks", None)
                    reasoning = (getattr(chunk, "additional_kwargs", {}) or {}).get("reasoning_content", "")
                    if isinstance(reasoning, str) and reasoning:
                        self._last_reasoning_content += reasoning
                        if last_thought_at == 0 or last_chunk_time - last_thought_at >= 5:
                            self.emit(thought_event(
                                f"Thinking · Reasoning received: {len(self._last_reasoning_content):,} characters",
                                subject="Model",
                            ).to_dict())
                            last_thought_at = last_chunk_time
                    record: Dict[str, Any] = {
                        "index": chunk_count,
                        "ts": last_chunk_time,
                        "content": chunk_content,
                        "tool_calls": raw_tool_calls,
                        "tool_call_chunks": raw_tool_chunks,
                        "diagnostics": self._chunk_diagnostics(chunk),
                    }
                    if os.environ.get("LOG_CHUNK_META", ""):
                        record["chunk_meta"] = self._summarize_chunk(chunk)
                    chunk_records.append(record)
                    if chunk_content:
                        content_buffer.append(chunk_content)
                    usage_candidate = self._extract_usage_metadata(chunk)
                    if usage_candidate:
                        usage = usage_candidate
                    self._merge_tool_calls(tool_calls, chunk)
                except Exception as exc:  # noqa: BLE001
                    self._log_event(
                        "error_traceback",
                        {
                            "where": "planner_chunk",
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                            "chunk_meta": self._summarize_chunk(chunk),
                        },
                    )
                    stop_event.set()
                    raise
        stop_event.set()

        if self._last_reasoning_content:
            self.emit(thought_event(
                f"Thinking complete · Reasoning received: {len(self._last_reasoning_content):,} characters",
                subject="Model",
            ).to_dict())

        tool_call_list = [v for v in tool_calls.values() if v.get("name")]
        elapsed = time.time() - stream_start
        stream_diagnostics = self._stream_diagnostics(
            turn=turn,
            chunk_records=chunk_records,
            chunk_count=chunk_count,
            elapsed=elapsed,
            content_chars=sum(len(part) for part in content_buffer),
            tool_call_count=len(tool_call_list),
            usage_seen=usage is not None,
        )
        self._log_event(
            "llm_stream_chunks",
            {"turn": turn, "chunks": chunk_records},
        )
        self._log_event(
            "llm_stream_stats",
            stream_diagnostics,
        )
        return "".join(content_buffer), tool_call_list, usage, stream_diagnostics

    def _coerce_chunk_content(self, content: Any) -> str:
        return coerce_visible_content(content)


    def _stream_llm_with_retries(
        self, turn: int
    ) -> tuple[str, List[Dict[str, Any]], Dict[str, int] | None, Dict[str, Any]]:
        retry_limit = _llm_stream_retry_limit()
        retry_interval = _llm_stream_retry_interval_seconds()
        for attempt in range(retry_limit + 1):
            self._check_cancelled()
            try:
                return self._stream_llm(turn)
            except AgentTurnCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                self._check_cancelled()
                if is_context_window_error(exc):
                    self._log_event(
                        "llm_context_window_error",
                        {"turn": turn, "error": str(exc)},
                    )
                    raise
                remaining = retry_limit - attempt
                self._log_event(
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

    _extract_usage_metadata = staticmethod(_extract_usage_metadata)

    _normalize_usage = staticmethod(_normalize_usage)

    def _update_session_usage(
        self, usage: Dict[str, int], turn: int | None, source: str
    ) -> Dict[str, Any]:
        self._session_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
        self._session_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
        self._session_usage["total_tokens"] += int(usage.get("total_tokens", 0))
        self._session_usage["cached_tokens"] += int(usage.get("cached_tokens", 0))
        self._session_usage["cache_write_tokens"] += int(usage.get("cache_write_tokens", 0))
        payload = {
            "promptTokens": usage.get("prompt_tokens", 0),
            "completionTokens": usage.get("completion_tokens", 0),
            "totalTokens": usage.get("total_tokens", 0),
            "sessionTotalTokens": self._session_usage["total_tokens"],
            "cachedTokens": usage.get("cached_tokens", 0),
            "cacheWriteTokens": usage.get("cache_write_tokens", 0),
            "sessionCachedTokens": self._session_usage["cached_tokens"],
            "sessionCacheWriteTokens": self._session_usage["cache_write_tokens"],
            "source": source,
        }
        if turn is not None:
            payload["turn"] = turn
        return payload

    def _merge_tool_calls(
        self, acc: Dict[str, Dict[str, Any]], chunk: BaseMessage
    ) -> None:
        merge_tool_calls(acc, chunk)

    def _next_plan_id(self) -> str:
        self._plan_counter += 1
        return f"plan-{os.getpid()}-{self._plan_counter}"

    def _extract_plan_steps(self, text: str) -> List[str]:
        if not text:
            return []
        inline = re.search(
            r"^\s*(?:\*\*|__)?\s*Plan:\s*(.+?)(?:\*\*|__)?\s*$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if inline:
            values = [part.strip() for part in re.split(r"[;；]", inline.group(1))]
            return [value for value in values if value][:20]

        headings: List[str] = []
        pattern = re.compile(
            r"^\s*#{2,6}\s*((?:\u9636\u6bb5|Stage)\s*[0-9\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+\s*[:：-]?\s*.+?)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        for match in pattern.finditer(text):
            heading = re.sub(r"\s+", " ", match.group(1)).strip(" *_")
            if heading and heading not in headings:
                headings.append(heading)
        return headings[:20]

    def _set_pending_plan(self, raw: str) -> None:
        plan_id = self._next_plan_id()
        steps = self._extract_plan_steps(raw)
        current = steps[0] if steps else None
        self._pending_plan = {
            "planId": plan_id,
            "steps": steps,
            "current": current,
            "raw": raw,
            "user_text": self._turn_user_text,
            "protocol_ids": list(self._turn_protocol_ids),
        }
        self._log_event(
            "plan_ready",
            {
                "planId": plan_id,
                "steps": steps,
                "current": current,
                "raw": raw,
            },
        )
        self.emit(plan_ready_event(plan_id, steps, current=current, raw=raw).to_dict())
        self.emit(info_event("Plan ready. Awaiting approval.").to_dict())

    def _clear_pending_plan(self, reason: str) -> None:
        if self._pending_plan is None:
            return
        plan_id = str(self._pending_plan.get("planId") or "")
        self._log_event(
            "plan_cleared",
            {"reason": reason, "planId": plan_id},
        )
        self._pending_plan = None
        self.emit(plan_cleared_event(plan_id, reason).to_dict())

    def _handle_plan_response(self, msg: Dict[str, Any]) -> None:
        pending = self._pending_plan
        if pending is None:
            self.emit(info_event("No pending plan to review.").to_dict())
            return
        plan_id = str(msg.get("planId") or "")
        if plan_id != str(pending.get("planId") or ""):
            self.emit(info_event("Plan response ignored because the plan is stale.").to_dict())
            return

        approved = bool(msg.get("approved"))
        feedback = str(msg.get("feedback") or "").strip()
        self._clear_pending_plan("approved" if approved else "rejected")
        if not approved:
            if feedback:
                self.history.append(HumanMessage(content=f"Plan feedback: {feedback}"))
            self.emit(info_event("Plan rejected. Describe any changes in a new message.").to_dict())
            self._turn_id = ""
            return

        self.emit(info_event("Plan approved. Executing the original task.").to_dict())
        self._turn_mode = "execute"
        self._turn_user_text = str(pending.get("user_text") or self._turn_user_text)
        self._turn_protocol_ids = list(pending.get("protocol_ids") or [])
        execution_request = (
            "The user approved the preceding plan. Execute the original task now according to "
            "that approved plan. Do not generate another approval plan. Preserve the original "
            "data scope, selected Protocols, QC requirements, and failure boundaries."
        )
        self.history.append(
            HumanMessage(
                content=controlled_user_prompt(
                    execution_request,
                    "execute",
                    self._turn_protocol_ids,
                )
            )
        )
        self._run_agent_turn()
        self._turn_id = ""

    def _handle_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> bool:
        stage_by_call: Dict[str, str] = {}
        mutating_by_call: Dict[str, bool] = {}
        stage_finished = False
        terminal_stage_failure = False
        rejected_tool_name = ""

        def requires_approval(name: str, args: Dict[str, Any], spec: ToolSpec) -> bool:
            if not spec.dangerous:
                return False
            return not (name == "software_manage" and args.get("execute") is not True)

        def _extract_stage(instruction: str) -> str | None:
            if not instruction:
                return None
            match = re.search(r"^\s*Current:\s*(.+)$", instruction, re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(1).strip()
            return None

        def on_request(
            call_id: str, name: str, args: Dict[str, Any], spec: ToolSpec
        ) -> None:
            self._check_cancelled()
            mutating_by_call[call_id] = spec.dangerous or name in {
                "execute_stage",
                "execute_slicer_stage",
            }
            if requires_approval(name, args, spec):
                self.emit(tool_call_request_event(call_id, name, args).to_dict())
                self._log_event(
                    "tool_call_request",
                    {"callId": call_id, "name": name, "args": args},
                )
            self._log_event(
                "tool_call_start",
                {"callId": call_id, "name": name, "args": args},
            )

        def on_start(
            call_id: str, name: str, args: Dict[str, Any], _spec: ToolSpec
        ) -> None:
            self._check_cancelled()
            from backend.events import tool_call_start_event
            self.emit(tool_call_start_event(call_id, name, args).to_dict())
            if name in {"execute_stage", "execute_slicer_stage"}:
                instruction = str(args.get("instruction", ""))
                stage = _extract_stage(instruction) or name
                stage_by_call[call_id] = stage
                self._log_event(
                    "stage_start",
                    {"stage": stage, "tool": name, "callId": call_id},
                )
                self.emit(stage_start_event(stage, call_id=call_id, tool=name).to_dict())
                return

        def on_response(
            call_id: str,
            name: str,
            output: str,
            elapsed: float | None,
        ) -> None:
            nonlocal stage_finished, terminal_stage_failure, rejected_tool_name
            payload = {
                "callId": call_id,
                "name": name,
                "output": output,
            }
            if elapsed is not None:
                payload["elapsed"] = elapsed
            self._log_event("tool_call_response", payload)
            self.emit(tool_call_response_event(call_id, name, output).to_dict())
            result_rejected = tool_result_is_user_rejection(output)
            if result_rejected:
                rejected_tool_name = name
            usage_from_tool = self._extract_usage_from_tool_output(output)
            if usage_from_tool:
                usage_payload = self._update_session_usage(
                    usage_from_tool,
                    None,
                    source=name,
                )
                self._log_event("llm_usage", usage_payload)
                self.emit(usage_event(usage_payload).to_dict())
            if name in {"execute_stage", "execute_slicer_stage"}:
                stage_finished = True
                stage = stage_by_call.get(call_id) or name
                terminal_stage_failure = stage_result_is_terminal_failure(output)
                stage_status = "failed" if terminal_stage_failure else "done"
                self._log_event(
                    "stage_done",
                    {
                        "stage": stage,
                        "status": stage_status,
                        "tool": name,
                        "callId": call_id,
                        "elapsed": elapsed,
                    },
                )
                self.emit(
                    stage_done_event(
                        stage,
                        stage_status,
                        call_id=call_id,
                        tool=name,
                        elapsed=elapsed,
                    ).to_dict()
                )
            result_failed = (
                result_rejected
                or output.lstrip().lower().startswith("[error]")
                or (name in {"execute_stage", "execute_slicer_stage"} and terminal_stage_failure)
            )
            if mutating_by_call.get(call_id) and not result_failed:
                self._mutating_tool_evidence_for_turn = True

        def approve(
            call_id: str, name: str, args: Dict[str, Any], spec: ToolSpec
        ) -> bool:
            if not requires_approval(name, args, spec):
                return True
            if self._auto_approve_tools:
                self._log_event(
                    "tool_call_approval",
                    {"callId": call_id, "approved": True, "auto": True},
                )
                return True
            return self._wait_for_tool_approval(call_id)

        active_tool_specs = self.plan_tool_specs if self._turn_mode == "plan" else self.tool_specs
        handle_tool_calls(
            tool_calls,
            active_tool_specs,
            self.history,
            self._resolve_tool_args,
            self._execute_tool,
            on_request=on_request,
            on_start=on_start,
            on_response=on_response,
            approve=approve,
            should_stop=lambda: stage_finished or bool(rejected_tool_name),
            sanitize_output=_redact_public_text,
        )
        if stage_finished or rejected_tool_name:
            self._tool_lock_for_turn = True
            final_prompt = (
                FAILED_STAGE_FINAL_PROMPT
                if terminal_stage_failure
                else SINGLE_STAGE_FINAL_PROMPT
                if stage_finished
                else REJECTED_TOOL_FINAL_PROMPT.format(tool_name=rejected_tool_name)
            )
            self.history.append(HumanMessage(content=final_prompt))
            self._tool_lock_prompt = final_prompt
            self._log_event(
                "terminal_tool_lock",
                {
                    "enabled": True,
                    "tool_calls": len(tool_calls),
                    "stage_failure": terminal_stage_failure,
                    "rejected_tool": rejected_tool_name,
                },
            )
            return True
        return False

    def _execute_tool(self, spec: ToolSpec, args: Dict[str, Any]) -> tuple[str, float]:
        try:
            start = time.time()
            if spec.tool.name in {"execute_stage", "execute_slicer_stage"}:
                os.environ["LOG_PATH"] = str(self.log_path)
                with executor_approval_handler(self._approve_executor_tool):
                    result = str(spec.tool.invoke(args))
            else:
                result = str(spec.tool.invoke(args))
            return result, time.time() - start
        except Exception as exc:  # noqa: BLE001
            self._log_event("error_traceback", {"where": "tool_execution", "tool": spec.tool.name,
                                                "traceback": traceback.format_exc()})
            return f"[error] {exc}", 0.0

    def _approve_executor_tool(self, request: Dict[str, Any]) -> bool:
        call_id = str(request.get("request_id") or "")
        name = str(request.get("name") or "executor_tool")
        args = request.get("args")
        if not isinstance(args, dict):
            args = {}
        self.emit(tool_call_request_event(call_id, name, args).to_dict())
        self._log_event(
            "executor_tool_approval_request",
            {
                "callId": call_id,
                "executorCallId": request.get("executor_call_id"),
                "name": name,
                "args": args,
            },
        )
        if self._auto_approve_tools:
            self._log_event(
                "tool_call_approval",
                {"callId": call_id, "approved": True, "auto": True, "nested": True},
            )
            return True
        return self._wait_for_tool_approval(call_id)

    def _resolve_tool_args(self, call: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(call.get("args"), dict):
            return call["args"]
        if call.get("args_str"):
            return self._parse_args(call["args_str"])
        return {}

    def _parse_args(self, args_str: str) -> Dict[str, Any]:
        if not args_str:
            return {}
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            self._log_event(
                "tool_args_parse_error",
                {"args_preview": args_str[:200], "args_len": len(args_str)},
            )
            return {}

    def _install_debug_handlers(self) -> None:
        try:
            descriptor = os.open(self.log_dir / "crash.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._faulthandler_handle = os.fdopen(descriptor, "a", encoding="utf-8")
            faulthandler.enable(file=self._faulthandler_handle, all_threads=True)
        except Exception:
            self._faulthandler_handle = None

        def _signal_handler(signum: int, _frame: object) -> None:
            if self._handling_signal:
                return
            self._handling_signal = True
            try:
                name = signal.Signals(signum).name
            except Exception:
                name = str(signum)
            self._log_event("signal", {"signal": signum, "name": name})
            proc_info = self._capture_process_info()
            if proc_info:
                self._log_event("signal_process_info", proc_info)
            if self._faulthandler_handle:
                try:
                    faulthandler.dump_traceback(
                        file=self._faulthandler_handle, all_threads=True
                    )
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
            self._log_event(
                "thread_exception",
                {"thread": args.thread.name, "error": str(args.exc_value)},
            )
            self._log_event(
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
            self._log_event(
                "error_traceback",
                {
                    "where": "sys",
                    "traceback": "".join(
                        traceback.format_exception(
                            exc_type, exc_value, exc_traceback
                        )
                    ),
                },
            )
            sys.__excepthook__(exc_type, exc_value, exc_traceback)

        sys.excepthook = _sys_excepthook

    def _wait_for_tool_approval(self, call_id: str) -> bool:
        while True:
            msg = self.input_reader.get()
            msg_type = msg.get("type")
            if msg_type == "command":
                name = str(msg.get("name") or "")
                self._handle_command(name, bool(msg.get("_cancel_signalled")))
                self._check_cancelled()
                if self._auto_approve_tools:
                    self._log_event(
                        "tool_call_approval",
                        {"callId": call_id, "approved": True, "auto": True},
                    )
                    return True
                continue
            if msg_type != "tool_call_response":
                continue
            if str(msg.get("callId")) != call_id:
                continue
            self._log_event(
                "tool_call_approval",
                {"callId": call_id, "approved": bool(msg.get("approved"))},
            )
            return bool(msg.get("approved"))

    def _handle_command(self, name: str, cancel_signalled: bool = False) -> None:
        if name == "compress":
            self._maybe_compress(True)
        elif name == "clear":
            self.history = self._build_initial_history()
            self.emit(info_event("History cleared.").to_dict())
        elif name == "cancel":
            if not cancel_signalled:
                request_cancel()
            if self._pending_plan is not None:
                self._clear_pending_plan("cancelled")
                self.emit(finished_event("cancelled").to_dict())
                self._turn_id = ""
            self.emit(info_event("Cancel requested.").to_dict())
        elif name == "auto_approve_on":
            self._auto_approve_tools = True
            self.emit(info_event("Auto-approve tools enabled.").to_dict())
        elif name == "auto_approve_off":
            self._auto_approve_tools = False
            self.emit(info_event("Auto-approve tools disabled.").to_dict())

    def _maybe_compress(self, force: bool) -> bool:
        system_msg = None
        history = self.history
        if history and isinstance(history[0], SystemMessage):
            system_msg = history[0]
            history = history[1:]
        decision = compression_decision(history, self.llm, force)
        if not decision.should_compress:
            return False
        result = compress_history(self.llm, history, self.model_name, force)
        self._log_event(
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
            if force:
                self.emit(
                    info_event(
                        "Compression not applied "
                        f"({result.status}): history was preserved."
                    ).to_dict()
                )
            return False
        if system_msg:
            self.history = [system_msg, *result.new_history]
        else:
            self.history = result.new_history
        self.emit(
            info_event(
                "Chat compressed with continuity testament "
                f"({result.status}): {result.original_tokens} -> {result.new_tokens}"
            ).to_dict()
        )
        return True

    def _build_initial_history(self) -> List[BaseMessage]:
        system_prompt = self.base_system_prompt
        env_context = build_env_context(os.getcwd())
        memory_context = load_memory_context(os.getcwd())
        if memory_context:
            env_context = f"{env_context}\n\n{memory_context}"
        return build_initial_history(system_prompt, env_context)

    def _init_log_path(self) -> Path:
        return self.log_dir / "session.jsonl"

    def _init_readable_log_path(self) -> Path:
        return self.log_dir / "session_readable.md"

    def _init_log_dir(self) -> Path:
        root = self._resolve_log_root()
        os.environ["LOG_ROOT_DIR"] = str(root)
        cwd = os.getcwd()
        project_key = project_log_key(cwd)
        project_root = root / "projects" / project_key
        project_root.mkdir(parents=True, exist_ok=True)
        os.environ["LOG_PROJECT_KEY"] = project_key
        os.environ["LOG_PROJECT_DIR"] = str(project_root)
        session_id = os.environ.get("LOG_SESSION_ID", "").strip()
        if session_id:
            target = project_root / f"session_{session_id}"
        else:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            session_id = timestamp
            target = project_root / f"session_{timestamp}"
        target.mkdir(parents=True, exist_ok=True)
        os.environ["LOG_SESSION_ID"] = session_id
        os.environ["LOG_DIR"] = str(target)
        return target

    def _resolve_log_root(self) -> Path:
        requested = resolve_log_root()
        requested.mkdir(mode=0o700, parents=True, exist_ok=True)
        return requested

    def _init_rollout_log_path(self) -> Path:
        path = init_rollout_log_path(self.log_root, self.log_dir)
        os.environ["SESSION_ROLLOUT_LOG_PATH"] = str(path)
        return path

    def _init_manifest_path(self) -> Path:
        return self.log_dir / "session_manifest.json"

    def _write_session_manifest(self) -> None:
        payload = build_manifest(
            session_id=self.session_id,
            log_root=self.log_root,
            log_dir=self.log_dir,
            legacy_log_path=self.log_path,
            readable_log_path=self.readable_log_path,
            rollout_log_path=self.rollout_log_path,
            model=self.model_name,
            base_url=_get_env("LLM_BASE_URL"),
            cwd=os.getcwd(),
            project_key=os.environ.get("LOG_PROJECT_KEY", ""),
        )
        try:
            write_json(self.manifest_path, payload)
        except Exception:
            pass

    def _log_event(self, kind: str, payload: Dict[str, Any]) -> None:
        if kind == "event":
            return
        line = self._format_log_line(kind, payload)
        if line is None:
            return
        record = {"ts": time.time(), "session_id": self.session_id,
                  "turn_id": self._turn_id, **line}
        try:
            write_jsonl(self.log_path, record)
        except Exception:
            pass
        self._log_rollout(kind, payload, ts=record["ts"])
        self._log_readable(kind, payload)

    def _log_rollout(self, kind: str, payload: Dict[str, Any], ts: float | None = None) -> None:
        try:
            records = format_rollout_records(
                kind=kind,
                payload=payload,
                agent_role=self.role,
                session_id=self.session_id,
                log_dir=self.log_dir,
                model=self.model_name,
                base_url=_get_env("LLM_BASE_URL"),
                cwd=os.getcwd(),
                ts=ts,
            )
            for record in records:
                write_jsonl(self.rollout_log_path, record)
        except Exception:
            pass

    def _log_readable(self, kind: str, payload: Dict[str, Any]) -> None:
        if not self.readable_log_path:
            return
        skip = {
            "llm_stream_start",
            "llm_stream_end",
            "tool_call_start",
            "tool_call_request",
            "tool_call_approval",
            "tool_args_parse_error",
            "llm_stream_stats",
            "llm_stream_chunks",
        }
        if kind in skip:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.readable_log_path.open("a", encoding="utf-8") as handle:
                block = self._format_readable(kind, payload, ts)
                if block:
                    handle.write(block + "\n")
        except Exception:
            return

    def _format_readable(self, kind: str, payload: Dict[str, Any], ts: str) -> str:
        if kind == "session_start":
            return (
                f"# Session Log ({ts})\n\n"
                f"- model: {payload.get('model','')}\n"
                f"- base_url: {payload.get('base_url','')}\n"
                f"- cwd: {payload.get('cwd','')}\n"
                f"\n## Timeline\n"
            )
        if kind == "plan_ready":
            steps = payload.get("steps") or []
            current = payload.get("current") or ""
            raw = str(payload.get("raw", "")).strip()
            block = f"\n### Plan ({ts})\n\n"
            block += "**Plan**\n\n"
            if steps:
                for step in steps:
                    block += f"- {step}\n"
            if current:
                block += f"\n**Current**\n\n`{current}`\n"
            if raw:
                block += f"\n**Raw**\n\n```\n{self._truncate_readable_block(raw)}\n```\n"
            return block
        if kind == "user_input":
            text = str(payload.get("processed", "")).strip()
            return f"\n### User ({ts})\n\n{text}\n"
        if kind == "assistant_message":
            text = str(payload.get("content", "")).strip()
            if not text:
                return ""
            return f"\n### Assistant ({ts})\n\n{text}\n"
        if kind == "llm_usage":
            prompt = payload.get("promptTokens", 0)
            completion = payload.get("completionTokens", 0)
            total = payload.get("totalTokens", 0)
            session_total = payload.get("sessionTotalTokens", 0)
            cached = payload.get("cachedTokens", 0)
            cache_write = payload.get("cacheWriteTokens", 0)
            source = payload.get("source", "")
            return (
                f"\n### LLM Usage ({ts})\n\n"
                f"**Summary**\n\n"
                f"- source: `{source}`\n"
                f"- prompt_tokens: `{prompt}`\n"
                f"- cached_tokens: `{cached}`\n"
                f"- cache_write_tokens: `{cache_write}`\n"
                f"- completion_tokens: `{completion}`\n"
                f"- total_tokens: `{total}`\n"
                f"- session_total_tokens: `{session_total}`\n"
            )
        if kind == "tool_call_response":
            name = payload.get("name", "")
            output = str(payload.get("output", ""))
            return self._format_tool_response(ts, name, output)
        if kind in {"error_summary", "error_traceback"}:
            error = payload.get("error") or payload.get("traceback") or ""
            return f"\n### Error ({ts})\n\n```\n{error}\n```\n"
        return ""

    def _format_tool_response(self, ts: str, name: str, output: str) -> str:
        header = (
            f"\n### Tool ({ts})\n\n"
            f"**Tool**\n\n"
            f"- name: `{name}`\n"
        )
        if name == "run_shell":
            parsed = self._try_parse_json(output)
            if isinstance(parsed, dict):
                stdout = str(parsed.get("stdout", "")).strip()
                stderr = str(parsed.get("stderr", "")).strip()
                formatted_output = str(parsed.get("output", "")).strip()
                metadata = parsed.get("metadata") or {}
                exit_code = parsed.get("exit_code")
                if exit_code is None and isinstance(metadata, dict):
                    exit_code = metadata.get("exit_code")
                # run_shell output already includes truncation and execution metadata,
                # so avoid double truncation here.
                body = f"- exit_code: `{exit_code}`\n"
                if isinstance(metadata, dict) and metadata:
                    duration = metadata.get("duration_seconds")
                    timed_out = metadata.get("timed_out")
                    if duration is not None:
                        body += f"- duration_seconds: `{duration}`\n"
                    if timed_out is not None:
                        body += f"- timed_out: `{timed_out}`\n"
                if formatted_output:
                    body += f"\n**Output**\n\n```\n{self._truncate_readable_block(formatted_output)}\n```\n"
                else:
                    if stdout:
                        body += f"\n**Stdout**\n\n```\n{self._truncate_readable_block(stdout)}\n```\n"
                    if stderr:
                        body += f"\n**Stderr**\n\n```\n{self._truncate_readable_block(stderr)}\n```\n"
                return header + body
        if name in {"execute_stage", "execute_slicer_stage"}:
            parsed = self._try_parse_json(output)
            if isinstance(parsed, dict):
                summary = self._truncate_readable_block(str(parsed.get("summary", "")).strip())
                paths = parsed.get("output_paths") or []
                if paths:
                    body = "\n**Outputs**\n\n| path |\n| --- |\n" + "\n".join(f"| {p} |" for p in paths) + "\n"
                else:
                    body = ""
                body += f"\n**Summary**\n\n```\n{summary}\n```\n" if summary else ""
                return header + body
        text = self._truncate_readable_block(output.strip())
        if text:
            return header + f"\n**Output**\n\n```\n{text}\n```\n"
        return header

    def _try_parse_json(self, text: str) -> Any:
        try:
            return json.loads(text)
        except Exception:
            return None

    def _extract_usage_from_tool_output(self, output: str) -> Dict[str, int] | None:
        parsed = self._try_parse_json(output)
        if not isinstance(parsed, dict):
            return None
        usage = parsed.get("usage")
        return self._normalize_usage(usage)

    def _truncate_text(self, text: str, limit: int = 2000) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n...[truncated]"

    def _truncate_readable_block(
        self, text: str, max_lines: int = 80, max_chars: int = 4000
    ) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        truncated = False
        if max_lines > 0 and len(lines) > max_lines:
            head = max(1, max_lines // 3)
            tail = max_lines - head
            lines = lines[:head] + ["... [CONTENT TRUNCATED] ..."] + lines[-tail:]
            truncated = True
        text = "\n".join(lines)
        if max_chars > 0 and len(text) > max_chars:
            head = max(1, max_chars // 3)
            tail = max_chars - head
            text = text[:head].rstrip() + "\n... [CONTENT TRUNCATED] ...\n" + text[-tail:].lstrip()
            truncated = True
        if truncated and not text.endswith("[TRUNCATED]"):
            text = f"{text}\n[TRUNCATED]"
        return text

    def _format_log_line(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        role = payload.get("role", self.role)
        if kind == "session_start":
            return {"type": "meta", "event": "session_start", "role": role, **payload}
        if kind == "user_input":
            processed = payload.get("processed", "")
            return {
                "type": "human",
                "role": role,
                "text": processed,
                "raw": payload.get("raw", ""),
                "history_len": len(self.history),
                "history_tokens": estimate_tokens(self.history, self.llm),
            }
        if kind == "assistant_message":
            return {
                "type": "llm",
                "role": role,
                "text": payload.get("content", ""),
                "history_len": len(self.history),
                "history_tokens": estimate_tokens(self.history, self.llm),
            }
        if kind == "llm_usage":
            return {"type": "meta", "event": "llm_usage", "role": role, **payload}
        if kind in {"plan_ready", "plan_cleared"}:
            return {"type": "meta", "event": kind, "role": role, **payload}
        if kind == "tool_call_start":
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
                "elapsed": payload.get("elapsed"),
                "output": payload.get("output"),
            }
        if kind in {
            "compression_attempt",
            "error_summary",
            "error_traceback",
            "empty_llm_turn",
            "llm_stream_stats",
            "llm_stream_retry",
            "context_window_recovery",
        }:
            return {"type": "meta", "event": kind, "role": role, **payload}
        if kind in {"signal", "signal_process_info", "thread_exception", "stream_hang"}:
            return {"type": "meta", "event": kind, "role": role, **payload}
        if kind in {"tool_call_approval", "tool_args_parse_error"}:
            return {"type": "meta", "event": kind, "role": role, **payload}
        if kind == "history_restore":
            return {"type": "meta", "event": kind, "role": role, **payload}
        return None

    def _restore_history(self, payload: Dict[str, Any]) -> None:
        raw_messages = payload.get("messages") or []
        limit = int(os.environ.get("RESUME_HISTORY_LIMIT", "200") or 200)
        trimmed = raw_messages[-limit:] if limit > 0 else raw_messages
        base_history = self._build_initial_history()
        restored: List[BaseMessage] = []
        if base_history and isinstance(base_history[0], SystemMessage):
            restored.append(base_history[0])
        for entry in trimmed:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", "")).strip().lower()
            content = str(entry.get("content", "")).strip()
            if not content:
                continue
            if role in {"user", "human"}:
                restored.append(HumanMessage(content=content))
            elif role in {"assistant", "ai"}:
                restored.append(AIMessage(content=content))
        if len(restored) <= 1 and base_history:
            restored = base_history
        self.history = restored
        self._pending_plan = None
        self._log_event("history_restore", {"count": max(len(restored) - 1, 0)})
        self.emit(info_event(f"History restored ({max(len(restored) - 1, 0)} messages)").to_dict())

    def _serialize_message(self, message: BaseMessage) -> Dict[str, Any]:
        content = getattr(message, "content", "")
        data: Dict[str, Any] = {"type": message.type, "content": content}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            data["tool_calls"] = tool_calls
        return data

    _summarize_chunk = staticmethod(_summarize_chunk)

    _chunk_diagnostics = staticmethod(_chunk_diagnostics)

    _stream_diagnostics = staticmethod(_stream_diagnostics)

    def _capture_process_info(self) -> Dict[str, Any]:
        pid = os.getpid()
        ppid = os.getppid()
        info: Dict[str, Any] = {"pid": pid, "ppid": ppid}
        try:
            info["pgid"] = os.getpgid(pid)
        except Exception:
            info["pgid"] = None
        try:
            info["sid"] = os.getsid(pid)
        except Exception:
            info["sid"] = None
        try:
            output = subprocess.check_output(
                [
                    "ps",
                    "-o",
                    "pid,ppid,pgid,sid,stat,etime,cmd",
                    "-p",
                    str(pid),
                    "-p",
                    str(ppid),
                ],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
            info["ps"] = output
        except Exception as exc:  # noqa: BLE001
            info["ps_error"] = str(exc)
        pgid = info.get("pgid")
        if pgid is not None:
            try:
                group_output = subprocess.check_output(
                    [
                        "ps",
                        "-o",
                        "pid,ppid,pgid,sid,stat,etime,cmd",
                        "-g",
                        str(pgid),
                    ],
                    text=True,
                    stderr=subprocess.STDOUT,
                ).strip()
                info["ps_group"] = group_output
            except Exception as exc:  # noqa: BLE001
                info["ps_group_error"] = str(exc)
        return info


def main() -> None:
    _load_env()
    _set_parent_death_signal()
    server = AgentServer()
    try:
        server.run()
    except Exception as exc:  # noqa: BLE001
        server._log_event(
            "error_traceback",
            {"where": "main", "traceback": traceback.format_exc(), "error": str(exc)},
        )
        server.emit(error_event(str(exc)).to_dict())


if __name__ == "__main__":
    main()
