from __future__ import annotations

import json
import os
import re
import time
import hashlib
import platform
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from backend.platform import state_home, secure_path, IS_WINDOWS

MAX_TEXT_LEN = 4000


def resolve_log_root() -> Path:
    for name in ("DMRI_LOG_ROOT", "LOG_ROOT_DIR", "LOG_DIR"):
        if os.environ.get(name, "").strip():
            return Path(os.environ[name]).expanduser().resolve()
    return (state_home() / "log").resolve()


def redact_log_value(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    private = tuple(str(v) for k, v in os.environ.items() if v and
                    (k.endswith("API_KEY") or k in {"DMRI_SERVICE_TOKEN", "MCP_SLICER_AUTH_TOKEN"})) + secrets
    if isinstance(value, str):
        for secret in sorted(set(private), key=len, reverse=True):
            if len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if str(k).lower() in {"api_key", "password", "authorization", "access_token", "secret"}
                    else redact_log_value(v, secrets)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_log_value(v, secrets) for v in value]
    return value


@contextmanager
def private_log_file(path: Path, mode: str = "a"):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    if IS_WINDOWS:
        secure_path(path)
    with os.fdopen(descriptor, mode, encoding="utf-8") as handle:
        yield handle


def iso_ts(ts: float | None = None) -> str:
    value = ts if ts is not None else time.time()
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def resolve_session_id(log_dir: Path) -> str:
    name = log_dir.name
    if name.startswith("session_"):
        return name[len("session_") :]
    return name


def project_log_key(cwd: str) -> str:
    resolved = str(Path(cwd).resolve())
    normalized = resolved.lower() if os.name == "nt" else resolved
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._")
    if not label:
        label = "workspace"
    if len(label) > 48:
        label = label[-48:]
    return f"{label}-{digest}"


def _parse_session_date(session_id: str) -> tuple[str, str, str, str]:
    raw = session_id.replace("-", "_")
    match = re.match(r"^(\d{8})_(\d{6})(?:_|$)", raw)
    if match:
        date, time_part = match.groups()
        year = date[:4]
        month = date[4:6]
        day = date[6:8]
        return year, month, day, f"{year}-{month}-{day}T{time_part[:2]}-{time_part[2:4]}-{time_part[4:6]}"
    now = datetime.now(timezone.utc)
    return (
        f"{now.year:04d}",
        f"{now.month:02d}",
        f"{now.day:02d}",
        now.strftime("%Y-%m-%dT%H-%M-%S"),
    )


def init_rollout_log_path(log_root: Path, log_dir: Path) -> Path:
    session_id = resolve_session_id(log_dir)
    year, month, day, stamp = _parse_session_date(session_id)
    rollout_dir = log_root / "sessions" / year / month / day
    rollout_dir.mkdir(parents=True, exist_ok=True)
    return rollout_dir / f"rollout-{stamp}-{session_id}.jsonl"


def write_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with private_log_file(path) as handle:
        handle.write(json.dumps(redact_log_value(record), ensure_ascii=False) + "\n")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    with private_log_file(path, "w") as handle:
        json.dump(redact_log_value(payload), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_manifest(
    *,
    session_id: str,
    log_root: Path,
    log_dir: Path,
    legacy_log_path: Path,
    readable_log_path: Path | None,
    rollout_log_path: Path,
    model: str | None = None,
    base_url: str | None = None,
    cwd: str | None = None,
    project_key: str | None = None,
) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "created_at": iso_ts(),
        "log_root": str(log_root),
        "session_dir": str(log_dir),
        "project_key": project_key or "",
        "legacy_log_path": str(legacy_log_path),
        "readable_log_path": str(readable_log_path) if readable_log_path else "",
        "rollout_log_path": str(rollout_log_path),
        "model": model or "",
        "base_url": base_url or "",
        "cwd": cwd or "",
        "roles": ["planner", "executor"],
        "format": "dmri-agent-rollout-v1",
        "runtime": {"python": sys.version, "platform": platform.platform(),
                    "agent_version": (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip(),
                    "context_window_tokens": os.environ.get("LLM_CONTEXT_WINDOW_TOKENS", ""),
                    "reasoning_effort": os.environ.get("LLM_REASONING_EFFORT", "")},
        "crash_log_path": str(log_dir / "crash.log"),
    }


def _compact_text(value: Any, limit: int = MAX_TEXT_LEN) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def format_rollout_records(
    *,
    kind: str,
    payload: Dict[str, Any],
    agent_role: str,
    session_id: str,
    log_dir: Path,
    model: str | None = None,
    base_url: str | None = None,
    cwd: str | None = None,
    ts: float | None = None,
) -> List[Dict[str, Any]]:
    now = ts if ts is not None else time.time()
    timestamp = iso_ts(now)

    if kind == "session_start":
        session_meta = {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": timestamp,
                "cwd": cwd or payload.get("cwd", ""),
                "source": "dmri-agent",
                "model": model or payload.get("model", ""),
                "base_url": base_url or payload.get("base_url", ""),
                "session_dir": str(log_dir),
                "agent_roles": ["planner", "executor"],
            },
        }
        turn_context = {
            "timestamp": timestamp,
            "type": "turn_context",
            "payload": {
                "cwd": cwd or payload.get("cwd", ""),
                "session_id": session_id,
                "agent_role": agent_role,
                "model": model or payload.get("model", ""),
            },
        }
        return [session_meta, turn_context]

    if kind == "user_input":
        return [
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "agent_role": agent_role,
                    "origin": "user_input",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _compact_text(payload.get("processed", "")),
                        }
                    ],
                },
            }
        ]

    if kind == "executor_start":
        return [
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "agent_role": agent_role,
                    "origin": "planner_instruction",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _compact_text(payload.get("instruction", "")),
                        }
                    ],
                },
            }
        ]

    if kind == "assistant_message":
        return [
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "agent_role": agent_role,
                    "content": [
                        {
                            "type": "output_text",
                            "text": _compact_text(payload.get("content", "")),
                        }
                    ],
                },
            }
        ]

    if kind == "tool_call_start":
        return [
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "agent_role": agent_role,
                    "call_id": payload.get("callId"),
                    "name": payload.get("name"),
                    "arguments": _compact_text(
                        json.dumps(payload.get("args", {}), ensure_ascii=False)
                    ),
                },
            }
        ]

    if kind == "tool_call_response":
        return [
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "agent_role": agent_role,
                    "call_id": payload.get("callId"),
                    "name": payload.get("name"),
                    "output": _compact_text(payload.get("output", "")),
                    "elapsed": payload.get("elapsed"),
                },
            }
        ]

    if kind == "llm_usage":
        source = str(payload.get("source", agent_role) or agent_role)
        category = "llm_turn" if source == agent_role else "tool_usage"
        return [
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "agent_role": agent_role,
                        "source": source,
                        "category": category,
                        "prompt_tokens": int(payload.get("promptTokens", payload.get("prompt_tokens", 0)) or 0),
                        "completion_tokens": int(
                            payload.get("completionTokens", payload.get("completion_tokens", 0)) or 0
                        ),
                        "total_tokens": int(payload.get("totalTokens", payload.get("total_tokens", 0)) or 0),
                        "cached_tokens": int(payload.get("cachedTokens", payload.get("cached_tokens", 0)) or 0),
                        "cache_write_tokens": int(
                            payload.get("cacheWriteTokens", payload.get("cache_write_tokens", 0)) or 0
                        ),
                        "session_total_tokens": int(
                            payload.get("sessionTotalTokens", payload.get("session_total_tokens", 0)) or 0
                        ),
                        "turn": payload.get("turn"),
                    },
                },
            }
        ]

    if kind == "plan_ready":
        return [
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "plan",
                    "info": {
                        "agent_role": agent_role,
                        "plan_id": payload.get("planId"),
                        "steps": payload.get("steps") or [],
                        "current": payload.get("current"),
                    },
                },
            }
        ]

    if kind in {"stage_start", "stage_done"}:
        return [
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "stage_status",
                    "info": {
                        "agent_role": agent_role,
                        "status": "start" if kind == "stage_start" else "done",
                        "stage": payload.get("stage"),
                        "tool": payload.get("tool"),
                        "call_id": payload.get("callId"),
                        "elapsed": payload.get("elapsed"),
                    },
                },
            }
        ]

    if kind in {
        "compression_attempt",
        "error_summary",
        "error_traceback",
        "signal",
        "signal_process_info",
        "thread_exception",
        "stream_hang",
        "tool_call_approval",
        "tool_args_parse_error",
        "history_restore",
    }:
        return [
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": kind,
                    "info": {
                        "agent_role": agent_role,
                        **payload,
                    },
                },
            }
        ]

    return []
