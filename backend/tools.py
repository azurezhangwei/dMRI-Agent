from __future__ import annotations

from backend.file_tools import (
    read_file as read_file,
    write_file as write_file,
    grep_files as grep_files,
    glob_files as glob_files,
    list_dir as list_dir,
    _ensure_path as _ensure_path,
    _is_slicer_stage_context as _is_slicer_stage_context,
)

import json
import os
import re
import subprocess
import sys
import time
import signal
import ctypes
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
try:
    import pwd
    import pty
except ImportError:  # Native Windows has neither module; no PTY is advertised there.
    pwd = pty = None
import uuid

from langchain_core.tools import StructuredTool
from backend.platform import IS_WINDOWS, shell_command, spawn_options, terminate_tree

from backend.project_paths import PROJECT_ROOT, resolve_project_path
from backend.cancel_state import cancel_generation
from backend.executor_approval import (
    EXECUTOR_APPROVAL_DIR_ENV,
    ExecutorApprovalBroker,
    current_executor_approval_handler,
)
from backend.experience_memory import memory_record, memory_search, memory_update
from backend.protocol_registry import find_protocol_doc, iter_protocol_entries
from backend.protocol_manager import protocol_draft, protocol_publish, protocol_status
from backend.software_manager import software_inventory, software_manage
from backend.stage_guard import validate_stage_result_evidence
from backend.task_routing import is_slicer_instruction
from backend.truncation import TruncationPolicy, formatted_truncate_text
from backend.slicer_tools import (
    mcp_slicer_capture_screenshot,
    mcp_slicer_execute_python_code,
    mcp_slicer_load_tractography,
    mcp_slicer_list_nodes,
    run_slicer_module,
)

TOOL_ACCESS_CONFIG = resolve_project_path(
    os.environ.get("TOOL_ACCESS_CONFIG"), PROJECT_ROOT / "backend" / "tool_access.json"
)


@dataclass
class ToolSpec:
    tool: StructuredTool
    dangerous: bool = False


def _get_agent_role() -> str:
    return os.environ.get("AGENT_ROLE", "planner").strip().lower()


def list_protocols() -> str:
    entries = [
        {
            "id": entry.protocol_id,
            "name": entry.name,
            "description": entry.description,
            "type": entry.protocol_type,
            "version": entry.version,
            "path": str(entry.path),
        }
        for entry in iter_protocol_entries()
    ]
    if not entries:
        return "[error] protocols directory not found"
    return json.dumps({"protocols": entries}, ensure_ascii=True)


def load_protocol(name: str) -> str:
    if not name:
        return "[error] protocol name is required"
    if name.strip().lower() in {"runtime_local", "local runtime software inventory"}:
        refreshed = software_inventory(refresh=True)
        if refreshed.startswith("[error]"):
            return refreshed
    protocol_path = find_protocol_doc(name)
    if not protocol_path or not protocol_path.exists():
        return f"[error] protocol not found: {name}"
    return protocol_path.read_text(encoding="utf-8")


def load_protocol_resource(name: str, relative_path: str) -> str:
    if _get_agent_role() not in {"planner", "main", "executor"}:
        return "[error] protocol resources are unavailable for this role"
    if not name or not relative_path:
        return "[error] protocol name and relative_path are required"
    normalized = relative_path.strip().lstrip("/")
    parts = Path(normalized).parts
    if len(parts) >= 3 and parts[0] == "..":
        protocol_path = find_protocol_doc(parts[1])
        normalized = "/".join(parts[2:])
    else:
        protocol_path = find_protocol_doc(name)
    if not protocol_path:
        return f"[error] protocol not found: {name}"
    protocol_root = protocol_path.parent.resolve()
    if normalized.startswith("code_template/"):
        normalized = normalized.replace("code_template/", "code_templates/", 1)
    if not normalized.startswith("code_templates/"):
        return "[error] protocol resources must be under code_templates/"
    target = (protocol_root / normalized).resolve()
    try:
        target.relative_to(protocol_root)
    except ValueError:
        return "[error] invalid protocol resource path"
    if not target.exists() or not target.is_file():
        return f"[error] protocol resource not found: {relative_path}"
    content = target.read_text(encoding="utf-8")
    return f"Protocol resource absolute path: {target}\n\n{content}"


def _load_tool_access_config() -> dict[str, Any]:
    if not TOOL_ACCESS_CONFIG.exists():
        return {}
    try:
        return json.loads(TOOL_ACCESS_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
def _allowed_tools_for_role(config: dict[str, Any], role: str) -> List[str]:
    default_tools = config.get("default") or []
    roles = config.get("roles") or {}
    allowed = roles.get(role)
    if allowed is None:
        allowed = default_tools
    if isinstance(allowed, list):
        return [str(item) for item in allowed]
    return []


def _get_tool_truncation_policy(max_output_tokens: Optional[float] = None) -> TruncationPolicy:
    mode = os.environ.get("TOOL_TRUNCATION_MODE", "bytes").lower().strip()
    raw_limit = os.environ.get("TOOL_TRUNCATION_LIMIT") or os.environ.get(
        "TOOL_TRUNCATE_THRESHOLD", "25000"
    )
    if max_output_tokens is not None:
        try:
            token_limit = int(max_output_tokens)
        except (TypeError, ValueError):
            token_limit = 0
        if token_limit > 0:
            return TruncationPolicy(mode="tokens", limit=token_limit)
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 25000
    if mode not in {"bytes", "tokens"}:
        mode = "bytes"
    return TruncationPolicy(mode=mode, limit=max(0, limit))


def _build_exec_output(stdout: str, stderr: str) -> str:
    stdout = stdout or ""
    stderr = stderr or ""
    if stdout and stderr:
        return f"{stdout}\n{stderr}"
    return stdout or stderr


def _format_exec_output_structured(
    output: str,
    exit_code: int,
    duration_seconds: float,
    policy: TruncationPolicy,
    timed_out: bool = False,
) -> str:
    formatted_output = formatted_truncate_text(output, policy)
    payload = {
        "output": formatted_output,
        "metadata": {
            "exit_code": int(exit_code),
            "duration_seconds": round(float(duration_seconds), 1),
            "timed_out": bool(timed_out),
        },
    }
    return json.dumps(payload, ensure_ascii=True)


def _resolve_workdir(workdir: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if workdir is None:
        return None, None
    text = str(workdir).strip()
    if not text:
        return None, None
    resolved = _ensure_path(text)
    if not resolved.exists():
        return None, f"workdir does not exist: {resolved}"
    if not resolved.is_dir():
        return None, f"workdir is not a directory: {resolved}"
    return str(resolved), None


def _poll_exec_session(
    session: Dict[str, Any],
    process_id: str,
    yield_time_ms: Optional[float] = None,
    max_output_tokens: Optional[float] = None,
    timeout_seconds: float = 0.0,
    allow_yield: Optional[bool] = None,
) -> str:
    proc = session["proc"]
    context = session["context"]
    cancel_token = cancel_generation()
    poll_interval = float(os.environ.get("TOOL_POLL_INTERVAL", "0.2") or 0.2)
    effective_allow_yield = bool(yield_time_ms) if allow_yield is None else allow_yield
    status, stdout, stderr, context = _wait_with_control(
        proc,
        cancel_token=cancel_token,
        timeout=timeout_seconds,
        poll_interval=poll_interval,
        timeout_message="exec_poll timeout",
        cancel_message="exec_poll cancelled",
        yield_time=(float(yield_time_ms) / 1000.0) if yield_time_ms else 0.0,
        allow_yield=effective_allow_yield,
        context=context,
        kill_on_timeout=False,
    )
    elapsed = time.monotonic() - session["start"]
    policy = _get_tool_truncation_policy(max_output_tokens)
    exit_code = proc.returncode if proc.returncode is not None else 1
    content = _build_exec_output(stdout.strip(), stderr.strip())
    if status == "timeout" and context is not None:
        session["context"] = context
        payload = {
            "output": formatted_truncate_text(content, policy),
            "metadata": {
                "exit_code": int(exit_code),
                "duration_seconds": round(float(elapsed), 1),
                "timed_out": False,
                "running": True,
                "process_id": process_id,
            },
        }
        return json.dumps(payload, ensure_ascii=True)
    if status == "yield" and context is not None:
        session["context"] = context
        payload = {
            "output": formatted_truncate_text(content, policy),
            "metadata": {
                "exit_code": int(exit_code),
                "duration_seconds": round(float(elapsed), 1),
                "timed_out": False,
                "running": True,
                "process_id": process_id,
            },
        }
        return json.dumps(payload, ensure_ascii=True)
    _remove_exec_session(process_id)
    return _format_exec_output_structured(content, exit_code, elapsed, policy, timed_out=False)


def _non_terminating_shell_reason(command: str) -> str | None:
    patterns = (
        (r"(^|[;&|]{1,2}\s*)tail\s+[^;&|]*-(?:[A-Za-z]*f|-[^\s]*follow)", "tail follow"),
        (r"(^|[;&|]{1,2}\s*)less\s+\+F(\s|$)", "less follow"),
        (r"(^|[;&|]{1,2}\s*)watch(\s|$)", "watch"),
    )
    for pattern, label in patterns:
        if re.search(pattern, command):
            return (
                f"non-terminating shell command rejected ({label}); "
                "run_shell is for commands that exit. Use bounded log snapshots "
                "such as `tail -n 100 <log>` or `sed -n '1,120p' <log>`."
            )
    return None


def _software_mutation_shell_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_PACKAGE_MANAGER_MUTATION_FROM_SHELL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    segment = r"(?im)(^|[;&|]{1,2}\s*|\n\s*)"
    prefix = r"(?:sudo\s+)?(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+)?"
    patterns = (
        segment + prefix + r"(?:[^\s;&|]*/)?brew\s+(?:install|uninstall|reinstall|upgrade|tap|untap|update)(?:\s|$)",
        segment + prefix + r"(?:[^\s;&|]*/)?(?:conda|mamba|micromamba)\s+(?:install|create|remove|uninstall|update)(?:\s|$)",
        segment + prefix + r"(?:[^\s;&|]*/)?pip(?:3(?:\.\d+)?)?\s+(?:install|uninstall)(?:\s|$)",
        segment + prefix + r"(?:[^\s;&|]*/)?python(?:3(?:\.\d+)?)?\s+-m\s+pip\s+(?:install|uninstall)(?:\s|$)",
        segment + prefix + r"(?:[^\s;&|]*/)?(?:apt|apt-get|dnf|yum|zypper|pacman)\s+(?:install|remove|erase|upgrade|update)(?:\s|$)",
        segment + prefix + r"(?:[^\s;&|]*/)?(?:docker|podman)\s+(?:pull|image\s+rm)(?:\s|$)",
    )
    if any(re.search(pattern, command) for pattern in patterns):
        return (
            "blocked: package-manager mutations via shell are disabled. "
            "Use `software_inventory`, then `software_manage` with a catalogued "
            "software_id, action, strategy, and explicit execute flag."
        )
    return None


def _slicer_spawn_shell_reason(command: str) -> str | None:
    # Keep Slicer work inside the currently running Slicer session.
    # Reject run_shell commands that spawn a new Slicer process/window.
    if os.environ.get("ALLOW_SLICER_SPAWN_FROM_RUN_SHELL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    lowered = command.lower()
    patterns = (
        # Slicer launcher invocations, usually creating a separate process/session.
        r"(^|[;&|]{1,2}\s*)(?:[^\s]*/)?Slicer(\s|$)",
        r"(^|[;&|]{1,2}\s*)(?:[^\s]*/)?SlicerApp-real(\s|$)",
    )
    if any(re.search(p, command) for p in patterns) and any(
        marker in command for marker in ("--launch", "--python-script", "--python-code", "--no-main-window")
    ):
        return (
            "blocked: run_shell cannot spawn a new Slicer process/session. "
            "Use `run_slicer_module(action='inspect'|'run')` (or `mcp_slicer_execute_python_code`) "
            "to execute inside the current Slicer window."
        )

    # Also block direct Slicer CLI module binaries from shell.
    # Match real command-invocation shapes only (start of command segment),
    # so text payloads (e.g., heredoc report content mentioning UKFTractography)
    # do not get false-positive blocked.
    cli_exec_patterns = (
        # Generic Slicer CLI path execution
        r"(?im)(^|[;&|]{1,2}\s*|\n\s*)(?:sudo\s+)?(?:env\s+[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:[^\s;|]*/)?[^\s;|]*/cli-modules/[^\s;|]+",
        # Common module executable names invoked directly
        r"(?im)(^|[;&|]{1,2}\s*|\n\s*)(?:sudo\s+)?(?:env\s+[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:[^\s;|]*/)?(?:dwitodtiestimation|diffusiontensorscalarmeasurements|dwiconvert|resamplescalarvectordwivolume|ukftractography|fibertractmeasurements)(\s|$)",
    )
    if any(re.search(pattern, command) for pattern in cli_exec_patterns):
        return (
            "blocked: direct Slicer CLI execution via run_shell is disabled. "
            "Use `run_slicer_module(action='inspect'|'run')` so parameters, "
            "scene nodes, and outputs are managed in the current Slicer session."
        )
    bridge_probe_markers = (
        "127.0.0.1:18999",
        "localhost:18999",
        "/slicer/exec",
        "/mcp-slicer/exec",
        "/inspect_module",
        "/run_module",
        "/mrml/properties",
    )
    bridge_probe_tools = ("curl ", "wget ", "nc ", "netcat ", "telnet ", "http ")
    if any(tool in lowered for tool in bridge_probe_tools) and any(marker in lowered for marker in bridge_probe_markers):
        return (
            "blocked: do not probe Slicer bridge/WebServer endpoints from run_shell. "
            "Use `mcp_slicer_list_nodes`, `run_slicer_module(action='inspect'|'run')`, "
            "`mcp_slicer_load_tractography`, or `mcp_slicer_execute_python_code`. "
            "If `:2016` is unavailable, report the WebServer failure directly instead of guessing `:18999` endpoints."
        )
    return None


def _slicer_stage_shell_reason(command: str) -> str | None:
    """Block fragile script-rewrite loops during Slicer-focused executor stages."""
    if not _is_slicer_stage_context():
        return None
    lowered = command.lower()
    risky_mutation_patterns = (
        r"(?im)(^|[;&|]{1,2}\s*|\n\s*)cat\s*>+\s*[^\n]*\.(sh|py)\b",
        r"(?im)(^|[;&|]{1,2}\s*|\n\s*)sed\s+-i\b",
        r"(?im)(^|[;&|]{1,2}\s*|\n\s*)(cp|mv)\s+[^\n]*\.(sh|py)\b",
        r"(?im)(^|[;&|]{1,2}\s*|\n\s*)chmod\s+\+x\s+[^\n]*\.(sh|py)\b",
    )
    if any(re.search(pattern, command) for pattern in risky_mutation_patterns):
        return (
            "blocked: script mutation via run_shell is disabled during Slicer stages. "
            "Use `run_slicer_module(action='inspect'|'run')` and existing protocol resources; "
            "do not rewrite shell/python scripts inside `_processing_code`."
        )

    # Avoid ad-hoc diagnostic helper scripts that usually trigger infinite patch loops.
    if "_processing_code" in lowered and ("fix_" in lowered or "ddparcel_process" in lowered):
        if re.search(r"(?im)(^|[;&|]{1,2}\s*|\n\s*)python\s+[^\n]*\.py\b", command):
            return (
                "blocked: ad-hoc python diagnostics under `_processing_code` are disabled in "
                "Slicer stages. Prefer structured module inspection (`run_slicer_module(action='inspect')`)."
            )
    return None


def run_shell(
    command: Optional[str] = None,
    cmd: Optional[str] = None,
    workdir: Optional[str] = None,
    login: bool = True,
    max_output_tokens: Optional[float] = None,
    sandbox_permissions: Optional[str] = None,
    justification: Optional[str] = None,
) -> str:
    command = command if command is not None else cmd
    command = "" if command is None else str(command)
    if not command.strip():
        policy = _get_tool_truncation_policy(max_output_tokens)
        output = "command is required"
        return _format_exec_output_structured(output, 2, 0.0, policy)
    non_terminating_reason = _non_terminating_shell_reason(command)
    if non_terminating_reason:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured(non_terminating_reason, 2, 0.0, policy)
    software_mutation_reason = _software_mutation_shell_reason(command)
    if software_mutation_reason:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured(software_mutation_reason, 2, 0.0, policy)
    slicer_stage_reason = _slicer_stage_shell_reason(command)
    if slicer_stage_reason:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured(slicer_stage_reason, 2, 0.0, policy)
    slicer_spawn_reason = _slicer_spawn_shell_reason(command)
    if slicer_spawn_reason:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured(slicer_spawn_reason, 2, 0.0, policy)
    resolved_workdir, workdir_error = _resolve_workdir(workdir)
    if workdir_error:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured(workdir_error, 2, 0.0, policy)
    env = os.environ.copy()
    cmd_list = shell_command(command, login=login)
    timeout = float(os.environ.get("RUN_SHELL_TIMEOUT", "0") or 0)
    poll_interval = float(os.environ.get("TOOL_POLL_INTERVAL", "0.2") or 0.2)
    cancel_token = cancel_generation()
    if sandbox_permissions:
        env["MRI_AGENT_SANDBOX_PERMISSIONS"] = str(sandbox_permissions)
    if justification:
        env["MRI_AGENT_SANDBOX_JUSTIFICATION"] = str(justification)
    proc = _spawn_process(cmd_list, env, stdin_data="", cwd=resolved_workdir)
    start = time.monotonic()
    status, stdout, stderr, context = _wait_with_control(
        proc,
        cancel_token=cancel_token,
        timeout=timeout,
        poll_interval=poll_interval,
        timeout_message="run_shell timeout",
        cancel_message="run_shell cancelled",
    )
    elapsed = time.monotonic() - start
    policy = _get_tool_truncation_policy(max_output_tokens)
    exit_code = proc.returncode if proc.returncode is not None else 1
    content = _build_exec_output(stdout.strip(), stderr.strip())
    timed_out = status == "timeout"
    if timed_out:
        exit_code = 124
        content = (
            f"command timed out after {int(elapsed * 1000)} milliseconds\n{content}".strip()
        )
    elif status == "cancelled":
        exit_code = 130
        content = f"command cancelled\n{content}".strip()
    return _format_exec_output_structured(content, exit_code, elapsed, policy, timed_out=timed_out)


def _normalize_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            return [text]
        return [text]
    return [str(value)]


def run_vlm_tract_qc(
    images: Union[List[str], str],
    out_md: Optional[str] = None,
    workdir: Optional[str] = None,
) -> str:
    img_list = _normalize_list(images)
    if not img_list:
        return json.dumps(
            {"summary": "[error] images is required", "output_paths": []},
            ensure_ascii=True,
        )
    protocol_doc = find_protocol_doc("WMA")
    if protocol_doc is None:
        return json.dumps(
            {"summary": "[error] WMA protocol not found", "output_paths": []},
            ensure_ascii=True,
        )
    script = (protocol_doc.parent / "scripts" / "vlm_tract_qc.py").resolve()
    cmd: List[str] = [sys.executable, str(script), "--images", *img_list]
    if out_md:
        cmd += ["--out_md", str(out_md)]
    timeout = float(os.environ.get("VLM_QC_TIMEOUT", "0") or 0)
    try:
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout if timeout > 0 else None,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"summary": "[error] vlm tract qc timeout", "output_paths": []},
            ensure_ascii=True,
        )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    summary = stderr or stdout or "vlm tract qc finished"
    out_path = ""
    if stdout:
        out_path = stdout.splitlines()[-1].strip()
    output_paths = [out_path] if out_path else []
    if result.returncode != 0 and summary and not summary.startswith("[error]"):
        summary = f"[error] {summary}"
    return json.dumps(
        {"summary": summary, "output_paths": output_paths},
        ensure_ascii=True,
    )


def _init_executor_log_path(env: Dict[str, str]) -> Optional[str]:
    log_dir = env.get("LOG_DIR", "")
    if not log_dir:
        return None
    try:
        target = Path(log_dir) / "executor"
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return str(target / f"executor_{timestamp}_{suffix}.jsonl")


def ask_user(question: str) -> str:
    if _get_agent_role() != "executor":
        return "[error] ask_user is restricted to executor role"
    text = (question or "").strip()
    if not text:
        return "[error] question is required"
    log_dir = os.environ.get("LOG_DIR", "")
    if not log_dir:
        return "[error] LOG_DIR not set for ask_user"
    ask_dir = Path(log_dir) / "ask_user"
    ask_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    qid = f"q_{stamp}_{uuid.uuid4().hex[:8]}"
    question_path = ask_dir / f"{qid}.json"
    response_path = ask_dir / f"{qid}.response.json"
    payload = {"id": qid, "question": text, "ts": time.time()}
    try:
        question_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"[error] failed to write ask_user question: {exc}"
    timeout = float(os.environ.get("ASK_USER_TIMEOUT", "0") or 0)
    poll_interval = float(os.environ.get("ASK_USER_POLL_INTERVAL", "0.2") or 0.2)
    start = time.monotonic()
    while True:
        if response_path.exists():
            try:
                content = response_path.read_text(encoding="utf-8").strip()
            except Exception:
                content = ""
            if content:
                try:
                    parsed = json.loads(content)
                    response = str(parsed.get("response", "")).strip()
                except Exception:
                    response = content
                if response:
                    return response
            return "[error] empty ask_user response"
        if timeout and (time.monotonic() - start) >= timeout:
            return "[error] ask_user timed out waiting for response"
        time.sleep(poll_interval)


def _extract_output_paths_from_summary(summary: Any, max_items: int = 40) -> List[str]:
    text = str(summary or "")
    if not text.strip():
        return []
    out: List[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        if len(out) >= max_items:
            return
        path = raw.rstrip(".,;:)]}\"'`")
        if len(path) < 2 or path.startswith("//") or "://" in path:
            return
        if path in seen or not path.startswith("/"):
            return
        seen.add(path)
        out.append(path)

    def paths_in(fragment: str) -> List[str]:
        found: List[str] = []
        scrubbed = fragment
        quoted = re.compile(r'(?P<quote>["\'])(?P<path>/[^"\'\n]+)(?P=quote)')
        for match in quoted.finditer(fragment):
            found.append(match.group("path"))
        scrubbed = quoted.sub(" ", scrubbed)
        code_span = re.compile(r"(?<!`)`(?P<path>/[^`\n]+)`(?!`)")
        for match in code_span.finditer(scrubbed):
            found.append(match.group("path"))
        scrubbed = code_span.sub(" ", scrubbed)
        found.extend(
            re.findall(
                r"(?<![A-Za-z0-9:/])(/[A-Za-z0-9._/+\\-]+)",
                scrubbed,
            )
        )
        return found

    output_flag = re.compile(
        r"(?:--(?:output|out|output-dir|output_dir)|-o)\s+"
        r"(?:\"(?P<double>/[^\"\n]+)\"|'(?P<single>/[^'\n]+)'|(?P<plain>/[^\s`|]+))",
        flags=re.IGNORECASE,
    )
    for match in output_flag.finditer(text):
        add(match.group("double") or match.group("single") or match.group("plain") or "")

    output_marker = re.compile(
        r"(?:\boutputs?\b|output\s+paths?|produced\s+files?|created|generated|written|saved|"
        r"\u8f93\u51fa|\u4ea7\u7269|\u751f\u6210|\u5199\u5165|\u4fdd\u5b58)",
        flags=re.IGNORECASE,
    )
    in_output_section = False
    for line in text.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            in_output_section = bool(output_marker.search(heading.group(1)))
        if "--output" in line or "--output-dir" in line or "--output_dir" in line:
            continue
        if in_output_section or output_marker.search(line):
            for candidate in paths_in(line):
                add(candidate)

    # Preserve compatibility for terse summaries that list only an absolute path.
    if not out:
        for candidate in paths_in(text):
            add(candidate)
    return out


def _normalize_output_path_alias(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return value
    aliases_env = os.environ.get("MRI_AGENT_PATH_ALIASES", "").strip()
    aliases: List[tuple[str, str]] = []
    if aliases_env:
        for item in aliases_env.split(","):
            token = item.strip()
            if not token or "=" not in token:
                continue
            legacy_prefix, canonical_prefix = token.split("=", 1)
            legacy_prefix = legacy_prefix.strip().rstrip("/\\")
            canonical_prefix = canonical_prefix.strip().rstrip("/\\")
            if legacy_prefix and canonical_prefix:
                aliases.append((legacy_prefix, canonical_prefix))

    for legacy_prefix, canonical_prefix in aliases:
        if value == legacy_prefix or value.startswith(legacy_prefix + os.sep):
            suffix = value[len(legacy_prefix):]
            canonical = canonical_prefix + suffix
            # Prefer canonical if it exists; otherwise keep original to avoid hiding real outputs.
            if Path(canonical).exists() or not Path(value).exists():
                return canonical
    # Generic fallback: normalize any path component named "data_processed".
    if "/data_processed/" in value:
        canonical = value.replace("/data_processed/", "/processed/")
        if Path(canonical).exists() or not Path(value).exists():
            return canonical
    if value.endswith("/data_processed"):
        canonical = value[: -len("/data_processed")] + "/processed"
        if Path(canonical).exists() or not Path(value).exists():
            return canonical
    return value


def run_executor_stage(
    instruction: str,
    paths: Optional[Union[List[str], str]] = None,
    expected_outputs: Optional[Union[List[str], str]] = None,
    protocol_id: Optional[str] = None,
) -> str:
    return _run_executor_stage_impl(
        instruction=instruction,
        paths=paths,
        expected_outputs=expected_outputs,
        protocol_id=protocol_id,
    )


def execute_slicer_stage(
    instruction: str,
    paths: Optional[Union[List[str], str]] = None,
    expected_outputs: Optional[Union[List[str], str]] = None,
    protocol_id: Optional[str] = None,
) -> str:
    # Backward compatibility shim: legacy callers still using execute_slicer_stage.
    return _run_executor_stage_impl(
        instruction=instruction,
        paths=paths,
        expected_outputs=expected_outputs,
        protocol_id=protocol_id,
    )


def _run_executor_stage_impl(
    instruction: str,
    paths: Optional[Union[List[str], str]] = None,
    expected_outputs: Optional[Union[List[str], str]] = None,
    protocol_id: Optional[str] = None,
) -> str:
    slicer_stage = is_slicer_instruction(instruction)
    normalized_instruction = str(instruction or "")
    if slicer_stage:
        # Planner can prepend a "Protocol Priority" block that biases executor into
        # non-Slicer protocols (for example DMRI_METRICS/FSL routes). In Slicer
        # stages we keep the actionable Plan/Current/Task block and drop that prefix.
        lowered = normalized_instruction.lower()
        protocol_anchor = lowered.find("protocol priority:")
        plan_anchor = lowered.find("plan:")
        if protocol_anchor != -1 and plan_anchor != -1 and plan_anchor > protocol_anchor:
            normalized_instruction = normalized_instruction[plan_anchor:].lstrip()
        if "use slicerdmri" not in normalized_instruction.lower():
            normalized_instruction = (
                "Slicer execution required: stay inside current Slicer session, "
                "prefer run_slicer_module(action='inspect'|'run') for module calls, "
                "allow mcp_slicer_execute_python_code only for scene glue "
                "(load volume/mask, node binding, display control), "
                "and prefer Slicer-first routing. If repeated non-retryable module "
                "errors occur, you may take one bounded fallback route and must "
                "state the fallback reason explicitly.\n\n"
                + normalized_instruction
            )
    effective_protocol_id = protocol_id

    payload = {
        "instruction": normalized_instruction,
        "paths": _normalize_list(paths),
        "expected_outputs": _normalize_list(expected_outputs),
    }
    if effective_protocol_id:
        payload["protocol_id"] = str(effective_protocol_id)
    env = os.environ.copy()
    if slicer_stage:
        env["MRI_AGENT_SLICER_STAGE"] = "1"
    else:
        env.pop("MRI_AGENT_SLICER_STAGE", None)
    log_path = env.get("LOG_PATH")
    executor_log_path = _init_executor_log_path(env)
    if executor_log_path:
        env["LOG_PATH"] = executor_log_path
        payload["log_path"] = executor_log_path
    timeout = float(os.environ.get("EXECUTE_STAGE_TIMEOUT", "0") or 0)
    poll_interval = float(os.environ.get("TOOL_POLL_INTERVAL", "0.2") or 0.2)
    cancel_token = cancel_generation()
    with ExecutorApprovalBroker(current_executor_approval_handler()) as approval_broker:
        env[EXECUTOR_APPROVAL_DIR_ENV] = approval_broker.environment_value
        proc = _spawn_process(
            [sys.executable, "-m", "backend.executor"],
            env,
            stdin_data=json.dumps(payload, ensure_ascii=True),
        )
        status, stdout, stderr, _ = _wait_with_control(
            proc,
            cancel_token=cancel_token,
            timeout=timeout,
            poll_interval=poll_interval,
            timeout_message="execute_stage timeout",
            cancel_message="execute_stage cancelled",
            on_poll=approval_broker.poll,
        )
    if status in {"timeout", "cancelled"}:
        return json.dumps(
            {
                "status": status,
                "summary": stderr or stdout or status,
                "output_paths": [],
                "diff": "",
                "executor_log_path": executor_log_path,
            },
            ensure_ascii=True,
        )
    if proc.returncode != 0:
        return json.dumps(
            {
                "status": "failed",
                "summary": stderr.strip() or "executor failed",
                "output_paths": [],
                "diff": "",
                "executor_log_path": executor_log_path,
            },
            ensure_ascii=True,
        )
    output = stdout.strip()
    try:
        parsed = json.loads(output)
    except Exception:
        return output
    if isinstance(parsed, dict):
        max_summary_paths = int(os.environ.get("EXECUTOR_SUMMARY_PATH_MAX", "40") or 40)
        existing_paths = parsed.get("output_paths")
        if (
            str(parsed.get("status") or "").strip().lower() == "success"
            and (not isinstance(existing_paths, list) or not existing_paths)
        ):
            inferred_paths = _extract_output_paths_from_summary(parsed.get("summary"), max_items=max_summary_paths)
            if inferred_paths:
                parsed["output_paths"] = inferred_paths
        if isinstance(parsed.get("output_paths"), list):
            parsed["output_paths"] = [
                _normalize_output_path_alias(str(p))
                for p in parsed.get("output_paths")
                if str(p).strip()
            ]
        parsed.setdefault("status", "success")
        status_text = str(parsed.get("status") or "").strip().lower()
        summary_text = str(parsed.get("summary") or "").strip()
        diff_text = str(parsed.get("diff") or "").strip()
        output_paths = parsed.get("output_paths") if isinstance(parsed.get("output_paths"), list) else []

        # Guardrail: empty result objects must not be treated as success.
        if status_text == "success" and not summary_text and not output_paths and not diff_text:
            parsed["status"] = "failed"
            parsed["summary"] = (
                "executor produced no summary, no outputs, and no diff; "
                "treated as failed to avoid false success."
            )
            status_text = "failed"

        # Respect explicit status lines emitted by executor final text.
        normalized_summary = summary_text.lower()
        if normalized_summary.startswith("status: error") or normalized_summary.startswith("status: failed"):
            parsed["status"] = "failed"
            status_text = "failed"

        # Stop hard when known transport/policy failures are surfaced in summary.
        if status_text == "success":
            hard_fail_markers = (
                "connection refused",
                "web server unavailable",
                "failed to run slicer module",
                "blocked: direct slicer cli execution",
                "reached executor turn limit",
            )
            if any(marker in normalized_summary for marker in hard_fail_markers):
                parsed["status"] = "failed"
                if summary_text:
                    parsed["summary"] = summary_text
                else:
                    parsed["summary"] = "slicer stage failed due to backend/webserver/tool routing error."
        parsed.setdefault("executor_log_path", executor_log_path)
        return validate_stage_result_evidence(
            json.dumps(parsed, ensure_ascii=True),
            expected_outputs=payload["expected_outputs"],
            cwd=Path.cwd(),
        )
    return output


def exec_poll(
    process_id: str,
    yield_time_ms: Optional[float] = None,
    max_output_tokens: Optional[float] = None,
) -> str:
    session = _get_exec_session(process_id)
    if not session:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured("process not found", 2, 0.0, policy)
    return _poll_exec_session(
        session,
        process_id,
        yield_time_ms=yield_time_ms,
        max_output_tokens=max_output_tokens,
    )


def exec_command(
    command: Optional[str] = None,
    cmd: Optional[str] = None,
    workdir: Optional[str] = None,
    login: bool = True,
    yield_time_ms: float = 10000.0,
    max_output_tokens: Optional[float] = None,
    tty: bool = False,
) -> str:
    """Start a command and return a process id when it outlives the yield window."""

    command = command if command is not None else cmd
    command = "" if command is None else str(command)
    policy = _get_tool_truncation_policy(max_output_tokens)
    if not command.strip():
        return _format_exec_output_structured("command is required", 2, 0.0, policy)
    non_terminating_reason = _non_terminating_shell_reason(command)
    if non_terminating_reason:
        return _format_exec_output_structured(non_terminating_reason, 2, 0.0, policy)
    software_mutation_reason = _software_mutation_shell_reason(command)
    if software_mutation_reason:
        return _format_exec_output_structured(software_mutation_reason, 2, 0.0, policy)
    slicer_spawn_reason = _slicer_spawn_shell_reason(command)
    if slicer_spawn_reason:
        return _format_exec_output_structured(slicer_spawn_reason, 2, 0.0, policy)
    resolved_workdir, workdir_error = _resolve_workdir(workdir)
    if workdir_error:
        return _format_exec_output_structured(workdir_error, 2, 0.0, policy)

    env = os.environ.copy()
    shell_args = ["-lc" if login else "-c", command]
    if IS_WINDOWS and tty:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured("Native Windows does not provide a PTY. Use tty=false.", 2, 0.0, policy)
    proc = _spawn_process(shell_command(command, login=login), env, stdin_data=None, cwd=resolved_workdir, tty=tty)
    started = time.monotonic()
    context = _start_capture(proc)
    cancel_token = cancel_generation()
    poll_interval = float(os.environ.get("TOOL_POLL_INTERVAL", "0.2") or 0.2)
    status, stdout, stderr, context = _wait_with_control(
        proc,
        cancel_token=cancel_token,
        timeout=0.0,
        poll_interval=poll_interval,
        timeout_message="exec_command timeout",
        cancel_message="exec_command cancelled",
        yield_time=max(0.0, float(yield_time_ms) / 1000.0),
        allow_yield=True,
        context=context,
        kill_on_timeout=False,
    )
    elapsed = time.monotonic() - started
    content = _build_exec_output(stdout.strip(), stderr.strip())
    if status == "yield" and context is not None:
        process_id = _register_exec_session(
            {
                "proc": proc,
                "context": context,
                "start": started,
                "command": command,
                "workdir": resolved_workdir,
            }
        )
        payload = {
            "output": formatted_truncate_text(content, policy),
            "metadata": {
                "exit_code": None,
                "duration_seconds": round(float(elapsed), 1),
                "timed_out": False,
                "running": True,
                "process_id": process_id,
            },
        }
        return json.dumps(payload, ensure_ascii=True)
    exit_code = proc.returncode if proc.returncode is not None else 1
    if status == "cancelled":
        exit_code = 130
        content = f"command cancelled\n{content}".strip()
    return _format_exec_output_structured(content, exit_code, elapsed, policy)


def write_stdin(
    process_id: str,
    text: str,
    close: bool = False,
    yield_time_ms: Optional[float] = None,
    max_output_tokens: Optional[float] = None,
) -> str:
    session = _get_exec_session(process_id)
    if not session:
        policy = _get_tool_truncation_policy(max_output_tokens)
        return _format_exec_output_structured("process not found", 2, 0.0, policy)
    proc = session["proc"]
    if proc.stdin:
        try:
            proc.stdin.write(text)
            proc.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            policy = _get_tool_truncation_policy(max_output_tokens)
            return _format_exec_output_structured(f"write_stdin failed: {exc}", 2, 0.0, policy)
        if close:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.stdin = None
            except Exception:
                pass
    else:
        pty_master_fd = getattr(proc, "_pty_master_fd", None)
        if pty_master_fd is not None:
            try:
                os.write(pty_master_fd, text.encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                policy = _get_tool_truncation_policy(max_output_tokens)
                return _format_exec_output_structured(f"write_stdin failed: {exc}", 2, 0.0, policy)
        else:
            policy = _get_tool_truncation_policy(max_output_tokens)
            return _format_exec_output_structured("stdin not available", 2, 0.0, policy)

    if yield_time_ms is None:
        return "ok"
    if close:
        return _poll_exec_session(
            session,
            process_id,
            yield_time_ms=None,
            max_output_tokens=max_output_tokens,
            timeout_seconds=max(0.0, float(yield_time_ms) / 1000.0),
            allow_yield=False,
        )
    return _poll_exec_session(
        session,
        process_id,
        yield_time_ms=yield_time_ms,
        max_output_tokens=max_output_tokens,
    )


def _set_parent_death_signal(parent_pid: int) -> None:
    if sys.platform != "linux":
        return
    try:
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


def _spawn_process(
    cmd: List[str],
    env: Dict[str, str],
    stdin_data: Optional[str] = None,
    cwd: Optional[str] = None,
    tty: bool = False,
) -> subprocess.Popen[str]:
    env = _normalize_subprocess_env(env)
    parent_pid = os.getpid()

    def _preexec() -> None:
        os.setsid()
        _set_parent_death_signal(parent_pid)

    if IS_WINDOWS:
        if tty:
            raise ValueError("Native Windows requires tty=false")
        proc = subprocess.Popen(cmd, text=True, encoding="utf-8", errors="replace", env=env,
                                cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **spawn_options())
    elif tty:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            cmd,
            text=False,
            env=env,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_preexec,
        )
        try:
            os.close(slave_fd)
        except Exception:
            pass
        setattr(proc, "_pty_master_fd", master_fd)
    else:
        proc = subprocess.Popen(
            cmd,
            text=True,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_preexec,
        )
    if stdin_data is not None and proc.stdin:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.stdin = None
    return proc


def _normalize_subprocess_env(env: Dict[str, str]) -> Dict[str, str]:
    normalized = dict(env)
    if IS_WINDOWS:
        normalized.setdefault("HOME", str(Path.home()))
        normalized.setdefault("PYTHONUTF8", "1")
        return normalized
    try:
        pw = pwd.getpwuid(os.getuid())
        default_user = pw.pw_name or "root"
        default_home = pw.pw_dir or f"/home/{default_user}"
        default_shell = pw.pw_shell or "/bin/bash"
    except Exception:
        default_user = "root"
        default_home = f"/home/{default_user}"
        default_shell = "/bin/bash"

    if not str(normalized.get("USER", "")).strip():
        normalized["USER"] = default_user
    if not str(normalized.get("LOGNAME", "")).strip():
        normalized["LOGNAME"] = normalized["USER"]
    if not str(normalized.get("HOME", "")).strip():
        normalized["HOME"] = default_home
    if not str(normalized.get("SHELL", "")).strip():
        normalized["SHELL"] = default_shell
    return normalized


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        terminate_tree(proc, force=True)
        proc.wait(timeout=15)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


def _start_capture(proc: subprocess.Popen[str]) -> Dict[str, Any]:
    truncate_threshold = int(os.environ.get("TOOL_TRUNCATE_THRESHOLD", "25000"))
    capture_max = int(os.environ.get("TOOL_CAPTURE_MAX", "0") or 0)
    if capture_max <= 0:
        capture_max = truncate_threshold * 4

    stdout_buffer = {"chunks": [], "size": 0, "max": capture_max}
    stderr_buffer = {"chunks": [], "size": 0, "max": capture_max}
    buffer_lock = threading.Lock()
    reader_threads: List[threading.Thread] = []

    def _append_buffer(buf: Dict[str, Any], chunk: str) -> None:
        if not chunk:
            return
        chunks = buf["chunks"]
        buf["size"] += len(chunk)
        chunks.append(chunk)
        max_capture = buf["max"]
        if max_capture and buf["size"] > max_capture:
            excess = buf["size"] - max_capture
            while chunks and excess > 0:
                head = chunks[0]
                if len(head) <= excess:
                    excess -= len(head)
                    buf["size"] -= len(head)
                    chunks.pop(0)
                else:
                    chunks[0] = head[excess:]
                    buf["size"] -= excess
                    excess = 0

    def _read_stream(stream: Optional[Any], buf: Dict[str, Any], is_binary: bool = False) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if is_binary and isinstance(chunk, (bytes, bytearray)):
                    try:
                        chunk = chunk.decode("utf-8", errors="replace")
                    except Exception:
                        chunk = ""
                with buffer_lock:
                    _append_buffer(buf, chunk)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    pty_master_fd = getattr(proc, "_pty_master_fd", None)
    if pty_master_fd is not None:
        try:
            pty_stream = os.fdopen(pty_master_fd, "rb", buffering=0)
        except Exception:
            pty_stream = None
        if pty_stream is not None:
            thread = threading.Thread(
                target=_read_stream, args=(pty_stream, stdout_buffer, True), daemon=True
            )
            thread.start()
            reader_threads.append(thread)
    else:
        for stream, buf in ((proc.stdout, stdout_buffer), (proc.stderr, stderr_buffer)):
            if stream is None:
                continue
            thread = threading.Thread(target=_read_stream, args=(stream, buf), daemon=True)
            thread.start()
            reader_threads.append(thread)

    return {
        "stdout": stdout_buffer,
        "stderr": stderr_buffer,
        "buffer_lock": buffer_lock,
        "reader_threads": reader_threads,
        "pty_master_fd": pty_master_fd,
    }


def _snapshot_output(context: Dict[str, Any]) -> tuple[str, str]:
    with context["buffer_lock"]:
        stdout = "".join(context["stdout"]["chunks"])
        stderr = "".join(context["stderr"]["chunks"])
    return stdout, stderr


def _finalize_capture(proc: subprocess.Popen[str], context: Dict[str, Any], label: str) -> tuple[str, str]:
    for attr in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, attr, None)
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
        try:
            setattr(proc, attr, None)
        except Exception:
            pass
    pty_master_fd = context.get("pty_master_fd")
    if pty_master_fd is not None:
        try:
            os.close(pty_master_fd)
        except Exception:
            pass
    for thread in context.get("reader_threads", []):
        thread.join(timeout=5)
    stdout, stderr = _snapshot_output(context)
    if not stdout and not stderr and proc.poll() is None:
        return "", f"[error] {label}: process still running"
    return stdout, stderr


def _wait_with_control(
    proc: subprocess.Popen[str],
    cancel_token: int,
    timeout: float,
    poll_interval: float,
    timeout_message: str,
    cancel_message: str,
    yield_time: float = 0.0,
    allow_yield: bool = False,
    context: Optional[Dict[str, Any]] = None,
    kill_on_timeout: bool = True,
    on_poll: Optional[Callable[[], Any]] = None,
) -> tuple[str, str, str, Optional[Dict[str, Any]]]:
    context = context or _start_capture(proc)
    start = time.monotonic()
    while True:
        if cancel_generation() != cancel_token:
            _terminate_process(proc)
            stdout, stderr = _finalize_capture(proc, context, "cancelled while reading output")
            return "cancelled", stdout, f"[error] {cancel_message}", None
        if on_poll is not None:
            try:
                on_poll()
            except Exception:
                _terminate_process(proc)
                _finalize_capture(proc, context, "approval broker failure")
                raise
        if yield_time and allow_yield and (time.monotonic() - start) >= yield_time:
            stdout, stderr = _snapshot_output(context)
            return "yield", stdout, stderr, context
        if proc.poll() is not None:
            break
        if timeout and (time.monotonic() - start) >= timeout:
            if kill_on_timeout:
                _terminate_process(proc)
                stdout, stderr = _finalize_capture(proc, context, "timeout while reading output")
                return "timeout", stdout, f"[error] {timeout_message} after {timeout:.0f}s", None
            stdout, stderr = _snapshot_output(context)
            return "timeout", stdout, stderr, context
        time.sleep(poll_interval)
    stdout, stderr = _finalize_capture(proc, context, "failed to read output")
    return "completed", stdout, stderr, None


def build_tools(
    include_executor: bool = True,
) -> Dict[str, ToolSpec]:
    tools: List[ToolSpec] = [
        ToolSpec(
            tool=StructuredTool.from_function(
                func=list_protocols,
                name="list_protocols",
                description="List available processing protocols (id, name, description, type, version, path).",
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=load_protocol,
                name="load_protocol",
                description="Load PROTOCOL.md for a processing protocol by id or name.",
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=load_protocol_resource,
                name="load_protocol_resource",
                description=(
                    "Load a bundled resource from code_templates and return its executable absolute "
                    "path plus content. Use relative_path like "
                    "'code_templates/brain_mask_median_otsu.py'."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=software_inventory,
                name="software_inventory",
                description=(
                    "Probe installed neuroimaging software and refresh the generated RUNTIME_LOCAL "
                    "protocol. Use this before relying on static runtime assumptions."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=software_manage,
                name="software_manage",
                description=(
                    "Plan or execute a catalogued software install/remove action. Start with "
                    "execute=false; execution is restricted to structured Homebrew, Conda, or "
                    "container commands and requires approval."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=memory_search,
                name="memory_search",
                description="Search persistent operational lessons before retrying a software or workflow issue.",
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=memory_record,
                name="memory_record",
                description=(
                    "Record a concise, evidenced operational lesson after verifying it. Never store "
                    "credentials, raw user data, or unverified guesses."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=memory_update,
                name="memory_update",
                description="Confirm, deprecate, or replace an existing persistent lesson by memory id.",
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=protocol_status,
                name="protocol_status",
                description="Inspect managed protocol drafts, validation state, publications, and backup revisions.",
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=protocol_draft,
                name="protocol_draft",
                description=(
                    "Validate and save a managed PROTOCOL.md draft distilled from a repeatable workflow. "
                    "Built-in protocols and credentials cannot be written."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=protocol_publish,
                name="protocol_publish",
                description=(
                    "Publish a validated managed protocol draft with revision checking, or roll back to a backup revision."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=read_file,
                name="read_file",
                description=(
                    "Reads a local file with 1-indexed line numbers, supporting slice "
                    "and indentation-aware block modes. Use this only for text files; "
                    "do not use it on medical-image or binary payloads such as `.nrrd`, "
                    "`.nii.gz`, `.vtk`, `.vtp`, or `.npy`."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=write_file,
                name="write_file",
                description=(
                    "Write UTF-8 text to a local file, creating parent directories when needed. "
                    "This can overwrite an existing file and therefore requires approval."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=run_vlm_tract_qc,
                name="run_vlm_tract_qc",
                description=(
                    "Executor-only. Run the VLM tract QC script with rendered images and "
                    "return the report path. Provide `images` (list of PNG/JPG paths) and "
                    "optional `out_md`."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=grep_files,
                name="grep_files",
                description=(
                    "Search file contents for a regex pattern and return grouped matching lines with file paths and line numbers."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=glob_files,
                name="glob_files",
                description=(
                    "Find files by glob pattern such as '*.py' or '**/*.nii.gz'. Use this for file discovery before reading or grepping."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=list_dir,
                name="list_dir",
                description=(
                    "List entries in a directory and return a flat list of absolute paths. Accepts relative or absolute paths."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=mcp_slicer_list_nodes,
                name="mcp_slicer_list_nodes",
                description=(
                    "List MRML nodes via the Slicer Web Server API. "
                    "Based on mcp-slicer list_nodes behavior. "
                    "Supports filter_type: names|ids|properties and optional class/name/id filters. "
                    "Prefer `names` or `ids` for broad scene introspection. "
                    "`properties` is for targeted inspection only and should be used with class/name/id filters. "
                    "By default filters out Slicer system/runtime nodes to save tokens; set "
                    "`include_system_nodes=true` for full scene listing."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=mcp_slicer_execute_python_code,
                name="mcp_slicer_execute_python_code",
                description=(
                    "Execute Python code in the active Slicer instance via WebServer /slicer/exec. "
                    "Based on mcp-slicer execute_python_code. "
                    "Use only for approved scene operations, node load/save, display control, "
                    "or small Slicer-side glue code. Do not use this for global scene clearing/reloading. "
                    "For structured results, assign __execResult to a JSON-serializable dictionary. "
                    "Read volume geometry from volumeNode.GetSpacing() / GetOrigin(); "
                    "GetImageData().GetSpacing() / GetOrigin() are VTK buffer geometry, not MRML physical geometry. "
                    "Use GetImageData().GetDimensions() for voxel dimensions. "
                    "For diffusion MRI inputs on disk as NIfTI + bval + bvec, do not use this tool "
                    "to manually construct a diffusion-weighted MRML node from low-level VTK/NIfTI parsing; "
                    "prefer converting to NHDR/NRRD first and then loading the converted diffusion volume into Slicer. "
                    "Do not use this as the default path to run "
                    "Slicer CLI algorithm modules when `run_slicer_module` can express the call. "
                    "Returns structured execution result."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=mcp_slicer_load_tractography,
                name="mcp_slicer_load_tractography",
                description=(
                    "Load a tractography file (.vtp/.vtk) into the current Slicer scene as a "
                    "fiber bundle node without clearing or rebuilding the scene. Use this to "
                    "display existing tractography results safely."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=mcp_slicer_capture_screenshot,
                name="mcp_slicer_capture_screenshot",
                description=(
                    "Capture Slicer screenshots (application/slice/3d) via Slicer Web Server. "
                    "Based on mcp-slicer capture_screenshot. "
                    "Returns metadata and saved image path."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=run_slicer_module,
                name="run_slicer_module",
                description=(
                    "Unified Slicer module tool. Use action='inspect' to return structured metadata "
                    "(module type, compact CLI schema, and optional logic methods), and action='run' "
                    "to execute with structured inputs/outputs/parameters. "
                    "Supports routing mode auto|cli|logic and optional method/methodArgs for logic modules. "
                    "Prefer passing scene node IDs (`node:<vtkMRML...ID>`) for inputs/outputs. "
                    "For file-like CLI parameters, node inputs are automatically staged to temporary files when needed. "
                    "Supports structured DICOM diffusion import through `module='DICOM'` with "
                    "`inputs.dicomDirectory=<folder>` in the active Slicer session. "
                    "This is the preferred path for Slicer CLI algorithm modules such as tensor fitting, "
                    "scalar measurement, masking, registration, and tractography."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=run_shell,
                name="run_shell",
                description=(
                    "Run a shell command in the local runtime environment. " +
                    ("Executes via native Windows PowerShell 5.1; use PowerShell syntax. " if IS_WINDOWS else
                     "Executes via bash -lc (login shell), with standard Bash shell features. ") +
                    "Accepts relative or absolute workdir. This is a non-interactive command "
                    "runner: stdin is closed, no TTY is allocated, and the call returns only "
                    "after the command exits or the configured timeout/cancel occurs. "
                    "Package-manager mutations are rejected; use software_manage for "
                    "catalogued software installation or removal. "
                    "Continuous follow commands such as tail -f, less +F, and watch are rejected; "
                    "inspect logs with bounded snapshots such as tail -n or sed -n."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=exec_command,
                name="exec_command",
                description=(
                    "Start a shell command with a bounded initial wait. If it is still running, "
                    "return a process_id for later exec_poll or write_stdin calls. Use this for "
                    "long-running commands that need progress polling; do not use continuous log "
                    "followers such as tail -f or watch. Package-manager mutations are rejected; "
                    "use software_manage for catalogued software installation or removal."
                ),
            ),
            dangerous=True,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=exec_poll,
                name="exec_poll",
                description=(
                    "Poll a process started by exec_command and return current output or final exit status."
                ),
            ),
            dangerous=False,
        ),
        ToolSpec(
            tool=StructuredTool.from_function(
                func=write_stdin,
                name="write_stdin",
                description=(
                    "Write text to a process started by exec_command. Set close=true when no more "
                    "input is needed; optionally wait for follow-up output with yield_time_ms."
                ),
            ),
            dangerous=True,
        ),
    ]
    if include_executor:
        tools.append(
            ToolSpec(
                tool=StructuredTool.from_function(
                    func=run_executor_stage,
                    name="execute_stage",
                    description=(
                        "Execute exactly one stage of a genuinely long data pipeline using the isolated "
                        "executor subagent. Do not use this for software management, Protocol/memory/config "
                        "work, troubleshooting, reports, or short one-stage data tasks; the Main Agent "
                        "must handle those directly. Provide a self-contained instruction because the "
                        "Executor does not receive the full conversation. Protocol requirements are authoritative. "
                        "Include exact task-required output locations in the instruction, especially when "
                        "the task requires separate outputs per subject/session. "
                        "When the user or protocol provides an exact output path, also pass it in "
                        "expected_outputs so the structured result preserves paths containing spaces. "
                        "If user-approved overrides exist, include them explicitly. "
                        "The response includes `executor_log_path` (JSON field) which points to the "
                        "executor session log for this stage and can be used for detailed review. "
                        "Executor must perform protocol-defined QC checks for each assigned stage and report findings."
                    ),
                ),
                dangerous=True,
            )
        )
    config = _load_tool_access_config()
    if config:
        role = _get_agent_role()
        allowed = set(_allowed_tools_for_role(config, role))
        if allowed:
            tools = [spec for spec in tools if spec.tool.name in allowed]
    return {spec.tool.name: spec for spec in tools}
_EXEC_SESSIONS: Dict[str, Any] = {}
_EXEC_SESSIONS_LOCK = threading.Lock()


def _register_exec_session(session: Dict[str, Any]) -> str:
    exec_id = str(uuid.uuid4())
    with _EXEC_SESSIONS_LOCK:
        _EXEC_SESSIONS[exec_id] = session
    return exec_id


def _get_exec_session(exec_id: str) -> Optional[Dict[str, Any]]:
    with _EXEC_SESSIONS_LOCK:
        return _EXEC_SESSIONS.get(exec_id)


def _remove_exec_session(exec_id: str) -> None:
    with _EXEC_SESSIONS_LOCK:
        _EXEC_SESSIONS.pop(exec_id, None)
