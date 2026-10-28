from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from backend.path_mapping import map_container_to_host


@dataclass
class Injection:
    content: str
    start_index: int
    end_index: int


@dataclass
class InjectionResult:
    original: str
    mapped: str
    status: str


def extract_injections(prompt: str, trigger: str) -> List[Injection]:
    injections: List[Injection] = []
    index = 0
    while index < len(prompt):
        start_index = prompt.find(trigger, index)
        if start_index == -1:
            break
        current_index = start_index + len(trigger)
        brace_count = 1
        found_end = False
        while current_index < len(prompt):
            char = prompt[current_index]
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    content = prompt[start_index + len(trigger) : current_index].strip()
                    end_index = current_index + 1
                    injections.append(
                        Injection(
                            content=content,
                            start_index=start_index,
                            end_index=end_index,
                        )
                    )
                    index = end_index
                    found_end = True
                    break
            current_index += 1
        if not found_end:
            raise ValueError(
                f"Invalid syntax: Unclosed injection starting at index {start_index} ('{trigger}')."
            )
    return injections


def expand_at_file(prompt: str, cwd: str) -> Tuple[str, List[str], List[InjectionResult]]:
    if "@{" not in prompt:
        return prompt, [], []
    injections = extract_injections(prompt, "@{")
    if not injections:
        return prompt, [], []

    output: List[str] = []
    notices: List[str] = []
    records: List[InjectionResult] = []
    last_index = 0
    for injection in injections:
        prefix = prompt[last_index : injection.start_index]
        if prefix:
            output.append(prefix)
        content, notice, mapped, status = read_path_from_workspace(injection.content, cwd)
        if notice:
            notices.append(notice)
        records.append(
            InjectionResult(
                original=injection.content,
                mapped=mapped,
                status=status,
            )
        )
        output.append(content)
        last_index = injection.end_index
    suffix = prompt[last_index:]
    if suffix:
        output.append(suffix)
    return "".join(output), notices, records


def read_path_from_workspace(path_str: str, cwd: str) -> Tuple[str, str, str, str]:
    workspace = Path(cwd).resolve()
    requested = Path(path_str).expanduser()
    if not requested.is_absolute():
        requested = (workspace / requested).resolve()
    requested = map_container_to_host(requested)

    if not _is_within_workspace(requested, workspace):
        return f"[error] Path outside workspace: {path_str}", (
            f"Failed to inject '@{{{path_str}}}': outside workspace."
        ), str(requested), "outside_workspace"
    if not requested.exists():
        return f"[error] Path not found: {path_str}", (
            f"Failed to inject '@{{{path_str}}}': path not found."
        ), str(requested), "not_found"

    if requested.is_dir():
        return _read_directory(requested, path_str), "", str(requested), "ok"
    return _read_file(requested, path_str), "", str(requested), "ok"


def _read_directory(dir_path: Path, display: str) -> str:
    parts: List[str] = [f"--- Start of content for directory: {display} ---\n"]
    files = sorted(p for p in dir_path.rglob("*") if p.is_file())
    max_files = 200
    for file_path in files[:max_files]:
        rel = file_path.relative_to(dir_path)
        parts.append(f"--- {rel} ---\n")
        parts.append(_read_file(file_path, str(rel)))
        parts.append("\n")
    if len(files) > max_files:
        parts.append(f"[info] Truncated directory listing at {max_files} files.\n")
    parts.append(f"--- End of content for directory: {display} ---")
    return "".join(parts)


def _read_file(path: Path, display: str) -> str:
    try:
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return f"[error] Failed to read {display}: {exc}"
    if _is_binary(data):
        return f"[binary] {display} omitted"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    return f"--- Start of content for file: {display} ---\n{text}\n--- End of content for file: {display} ---"


def _is_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:1024]
    text_chars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)))
    return bool(sample.translate(None, text_chars))


def _is_within_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.relative_to(workspace)
        return True
    except ValueError:
        return False
