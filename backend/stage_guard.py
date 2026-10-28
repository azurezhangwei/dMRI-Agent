from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Any


TERMINAL_STAGE_STATUSES = {"failed", "error", "timeout", "cancelled", "rejected"}
COMPLETION_CLAIM_PATTERNS = (
    re.compile(r"\b(?:stage|conversion|processing|installation)\b.{0,48}\b(?:completed|succeeded|successful)\b", re.I),
    re.compile(r"\b(?:completed|succeeded)\b.{0,48}\b(?:stage|conversion|processing|installation)\b", re.I),
    re.compile(r"(?:\u9636\u6bb5|\u8f6c\u6362|\u5904\u7406|\u5b89\u88c5).{0,24}(?:\u5df2\u5b8c\u6210|\u6210\u529f\u5b8c\u6210|\u6210\u529f)", re.I),
)
ACTION_REQUEST_PATTERNS = (
    re.compile(r"\b(?:install|convert|process|run|execute|create|generate)\b", re.I),
    re.compile(r"(?:\u5b89\u88c5|\u8f6c\u6362|\u5904\u7406|\u8fd0\u884c|\u6267\u884c|\u751f\u6210|\u521b\u5efa)", re.I),
)


def _existing_path(value: str, *, cwd: Path) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = cwd / path
    if any(char in str(path) for char in "*?["):
        return bool(glob.glob(str(path)))
    return path.exists()


def validate_stage_result_evidence(
    output: str,
    *,
    expected_outputs: list[str] | None = None,
    cwd: Path | None = None,
) -> str:
    """Turn unsupported executor success into a structured failure."""
    text = str(output or "").strip()
    try:
        parsed: Any = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if not isinstance(parsed, dict):
        return text
    if str(parsed.get("status") or "").strip().lower() != "success":
        return text

    base = (cwd or Path.cwd()).resolve()
    declared = [str(item).strip() for item in (expected_outputs or []) if str(item).strip()]
    raw_reported = parsed.get("output_paths")
    reported = (
        [str(item).strip() for item in raw_reported if str(item).strip()]
        if isinstance(raw_reported, list)
        else []
    )
    missing_expected = [item for item in declared if not _existing_path(item, cwd=base)]
    missing_reported = [item for item in reported if not _existing_path(item, cwd=base)]
    if not missing_expected and not missing_reported:
        return text

    parsed["status"] = "failed"
    parsed["summary"] = (
        "Stage reported success without verifiable filesystem evidence. "
        "Missing expected outputs: "
        + json.dumps(missing_expected, ensure_ascii=False)
        + "; missing reported outputs: "
        + json.dumps(missing_reported, ensure_ascii=False)
    )
    parsed["missing_expected_outputs"] = missing_expected
    parsed["missing_reported_outputs"] = missing_reported
    return json.dumps(parsed, ensure_ascii=True)


def unsupported_completion_claim(
    text: str,
    *,
    user_request: str,
    has_tool_evidence: bool,
    execute_mode: bool,
) -> bool:
    if not execute_mode or has_tool_evidence:
        return False
    if not any(pattern.search(str(user_request or "")) for pattern in ACTION_REQUEST_PATTERNS):
        return False
    return any(pattern.search(str(text or "")) for pattern in COMPLETION_CLAIM_PATTERNS)


def tool_result_is_user_rejection(output: str) -> bool:
    return str(output or "").strip().lower().startswith("[rejected]")


def stage_result_is_terminal_failure(output: str) -> bool:
    text = str(output or "").strip()
    lowered = text.lower()
    if lowered.startswith("[error]") or tool_result_is_user_rejection(text):
        return True
    try:
        parsed: Any = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    status = str(parsed.get("status") or "").strip().lower()
    return status in TERMINAL_STAGE_STATUSES
