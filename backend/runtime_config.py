from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, MutableMapping

from backend.project_paths import PROJECT_ROOT, resolve_project_path


DEFAULT_RUNTIME_CONFIG = PROJECT_ROOT / "config" / "runtime.json"
ALLOWED_RUNTIME_DEFAULTS = {
    "EXECUTOR_MAX_TURNS",
    "EXECUTOR_MAX_TURNS_SLICER",
    "CONTEXT_COMPRESSION_TRIGGER_TOKENS",
    "CONTEXT_COMPRESSION_HEADROOM_TOKENS",
    "CONTEXT_COMPRESS_RECENT_USER_TOKENS",
    "CONTEXT_COMPRESSION_TESTAMENT_RETRIES",
    "EMPTY_LLM_RETRY_LIMIT",
    "LLM_STREAM_RETRY_LIMIT",
    "STREAM_HANG_SECONDS",
    "RUN_SHELL_TIMEOUT",
    "EXECUTE_STAGE_TIMEOUT",
    "TOOL_TRUNCATE_THRESHOLD",
    "TOOL_TRUNCATION_LIMIT",
    "TOOL_TRUNCATION_MODE",
    "LANGCHAIN_OPENAI_TCP_KEEPALIVE",
}


class RuntimeConfigError(ValueError):
    pass


def load_runtime_defaults(path: Path | None = None) -> dict[str, str]:
    config_path = path or resolve_project_path(
        os.environ.get("DMRI_RUNTIME_CONFIG"), DEFAULT_RUNTIME_CONFIG
    )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeConfigError(f"runtime config does not exist: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeConfigError(f"invalid runtime config JSON: {exc}") from exc
    defaults = payload.get("defaults") if isinstance(payload, Mapping) else None
    if not isinstance(defaults, Mapping):
        raise RuntimeConfigError("runtime config must contain a defaults object")
    unknown = set(defaults) - ALLOWED_RUNTIME_DEFAULTS
    if unknown:
        raise RuntimeConfigError(
            "unsupported runtime default keys: " + ", ".join(sorted(str(item) for item in unknown))
        )
    return {str(key): str(value) for key, value in defaults.items()}


def apply_runtime_defaults(
    environ: MutableMapping[str, str] | None = None,
    *,
    defaults: Mapping[str, str] | None = None,
) -> dict[str, str]:
    target = environ if environ is not None else os.environ
    values = dict(defaults or load_runtime_defaults())
    for name, value in values.items():
        if not (target.get(name) or "").strip():
            target[name] = value
    return values
