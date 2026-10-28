"""Text and directory tools, including dMRI binary and DICOM listing guards."""
from __future__ import annotations

import fnmatch
import glob as glob_module
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from backend.path_mapping import map_container_to_host, map_host_to_container


READ_FILE_MAX_LINE_LENGTH = 500


READ_FILE_TAB_WIDTH = 4


READ_FILE_COMMENT_PREFIXES = ("#", "//", "--")


READ_FILE_BLOCKED_SUFFIXES = (
    ".nrrd",
    ".nrrd.gz",
    ".nii",
    ".nii.gz",
    ".mgz",
    ".mgh",
    ".nhdr.raw.gz",
    ".vtk",
    ".vtp",
    ".vti",
    ".vtu",
    ".fib",
    ".trk",
    ".tck",
    ".npy",
    ".npz",
    ".h5",
    ".hdf5",
)


def _ensure_path(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _compile_search_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _matches_include_glob(relative_path: Path, include_glob: Optional[str]) -> bool:
    if not include_glob:
        return True
    rel = relative_path.as_posix()
    return fnmatch.fnmatch(rel, include_glob) or fnmatch.fnmatch(relative_path.name, include_glob)


def _iter_regular_files(root: Path, include_glob: Optional[str] = None) -> List[tuple[Path, Path]]:
    files: List[tuple[Path, Path]] = []
    if root.is_file():
        relative = Path(root.name)
        if _matches_include_glob(relative, include_glob):
            files.append((root, relative))
        return files
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            relative = Path(candidate.name)
        if _matches_include_glob(relative, include_glob):
            files.append((candidate, relative))
    return files


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return True
    return b"\x00" in chunk


def _matches_blocked_read_suffix(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in READ_FILE_BLOCKED_SUFFIXES)


def _blocked_read_message(display_path: Path, target: Path) -> str:
    sibling_hints: List[str] = []
    lower_name = target.name.lower()
    if lower_name.endswith(".nrrd"):
        nhdr = target.with_suffix(".nhdr")
        if nhdr.exists():
            sibling_hints.append(f"read the NHDR header instead: `{map_host_to_container(nhdr)}`")
    if lower_name.endswith(".nii.gz"):
        base = target.name[:-7]
        for suffix in (".bval", ".bvec", ".json", ".nhdr"):
            candidate = target.with_name(base + suffix)
            if candidate.exists():
                sibling_hints.append(f"inspect companion file: `{map_host_to_container(candidate)}`")
    elif lower_name.endswith(".nii"):
        base = target.stem
        for suffix in (".bval", ".bvec", ".json", ".nhdr"):
            candidate = target.with_name(base + suffix)
            if candidate.exists():
                sibling_hints.append(f"inspect companion file: `{map_host_to_container(candidate)}`")

    hints = [
        f"`read_file` does not support raw medical-image/binary payloads like `{display_path}`.",
        "Use `list_dir` or `glob_files` for discovery.",
        "Use `read_file` on text sidecars only, such as `.nhdr`, `.bval`, `.bvec`, `.json`, or logs.",
    ]
    hints.extend(sibling_hints[:3])
    return " ".join(hints)


def _parse_grep_like_output(output: str, base_path: Path, display_base_path: Path) -> List[dict[str, Any]]:
    matches: List[dict[str, Any]] = []
    if not output.strip():
        return matches
    for line in output.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^(.*?):(\d+):(.*)$", line)
        if match is None:
            continue
        file_part, line_no, content = match.groups()
        try:
            parsed_line_no = int(line_no)
        except ValueError:
            continue
        candidate = Path(file_part)
        absolute = candidate if candidate.is_absolute() else (base_path / candidate).resolve()
        try:
            display = str(map_host_to_container(absolute))
        except Exception:
            try:
                relative = absolute.relative_to(base_path)
                display = str(display_base_path / relative)
            except ValueError:
                display = str(absolute)
        matches.append({"file": display, "line": parsed_line_no, "text": content})
    return matches


def _format_grouped_matches(pattern: str, matches: List[dict[str, Any]], limit: int) -> str:
    if not matches:
        return "No matches found."
    total_matches = len(matches)
    shown_matches = matches[:limit]
    grouped: Dict[str, List[dict[str, Any]]] = {}
    for match in shown_matches:
        grouped.setdefault(str(match["file"]), []).append(match)
    lines = [f'Found {total_matches} matches for pattern "{pattern}":', "---"]
    for file_path in sorted(grouped):
        lines.append(f"File: {file_path}")
        for match in sorted(grouped[file_path], key=lambda item: int(item["line"])):
            lines.append(f"L{match['line']}: {str(match['text']).strip()}")
        lines.append("---")
    if total_matches > len(shown_matches):
        lines.append(f"[{total_matches - len(shown_matches)} lines truncated] ...")
    return "\n".join(lines)


def _take_bytes_at_char_boundary(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    used_bytes = 0
    last_ok = 0
    for idx, ch in enumerate(text):
        next_bytes = used_bytes + len(ch.encode("utf-8"))
        if next_bytes > max_bytes:
            break
        used_bytes = next_bytes
        last_ok = idx + 1
    return text[:last_ok]


def _format_read_line(raw_bytes: bytes) -> str:
    decoded = raw_bytes.decode("utf-8", errors="replace")
    if len(decoded.encode("utf-8")) > READ_FILE_MAX_LINE_LENGTH:
        return _take_bytes_at_char_boundary(decoded, READ_FILE_MAX_LINE_LENGTH)
    return decoded


@dataclass
class _LineRecord:
    number: int
    raw: str
    display: str
    indent: int

    def is_blank(self) -> bool:
        return self.raw.strip() == ""

    def is_comment(self) -> bool:
        trimmed = self.raw.strip()
        return any(trimmed.startswith(prefix) for prefix in READ_FILE_COMMENT_PREFIXES)


@dataclass
class _ReadSliceResult:
    lines: List[str]
    total_lines: int
    start_line: int
    end_line: int

    @property
    def is_truncated(self) -> bool:
        if not self.lines:
            return False
        return self.start_line > 1 or self.end_line < self.total_lines


def _measure_indent(line: str) -> int:
    indent = 0
    for char in line:
        if char == " ":
            indent += 1
        elif char == "\t":
            indent += READ_FILE_TAB_WIDTH
        else:
            break
    return indent


def _compute_effective_indents(records: List[_LineRecord]) -> List[int]:
    effective: List[int] = []
    previous_indent = 0
    for record in records:
        if record.is_blank():
            effective.append(previous_indent)
        else:
            previous_indent = record.indent
            effective.append(previous_indent)
    return effective


def _trim_empty_lines(records: List[_LineRecord]) -> List[_LineRecord]:
    start = 0
    end = len(records)
    while start < end and records[start].raw.strip() == "":
        start += 1
    while end > start and records[end - 1].raw.strip() == "":
        end -= 1
    return records[start:end]


def _read_file_slice(path: Path, offset: int, limit: int) -> Union[str, _ReadSliceResult]:
    collected: List[str] = []
    total_lines = 0
    try:
        with path.open("rb") as handle:
            while True:
                raw = handle.readline()
                if not raw:
                    break
                if raw.endswith(b"\n"):
                    raw = raw[:-1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                total_lines += 1
                if total_lines < offset:
                    continue
                if len(collected) == limit:
                    continue
                collected.append(f"L{total_lines}: {_format_read_line(raw)}")
    except OSError as exc:
        return f"failed to read file: {exc}"
    if total_lines < offset:
        return "offset exceeds file length"
    end_line = min(total_lines, offset + len(collected) - 1) if collected else 0
    return _ReadSliceResult(
        lines=collected,
        total_lines=total_lines,
        start_line=offset,
        end_line=end_line,
    )


def _read_file_indentation(
    path: Path,
    offset: int,
    limit: int,
    indentation: dict[str, Any],
) -> Union[str, List[str]]:
    anchor_line = indentation.get("anchor_line") if indentation else None
    anchor_line = offset if anchor_line is None else int(anchor_line)
    if anchor_line == 0:
        return "anchor_line must be a 1-indexed line number"
    max_levels = int(indentation.get("max_levels", 0) if indentation else 0)
    include_siblings = bool(indentation.get("include_siblings", False) if indentation else False)
    include_header = bool(indentation.get("include_header", True) if indentation else True)
    guard_limit = indentation.get("max_lines") if indentation else None
    guard_limit = limit if guard_limit is None else int(guard_limit)
    if guard_limit == 0:
        return "max_lines must be greater than zero"

    records: List[_LineRecord] = []
    number = 0
    try:
        with path.open("rb") as handle:
            while True:
                raw = handle.readline()
                if not raw:
                    break
                if raw.endswith(b"\n"):
                    raw = raw[:-1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                number += 1
                raw_text = raw.decode("utf-8", errors="replace")
                records.append(
                    _LineRecord(
                        number=number,
                        raw=raw_text,
                        display=_format_read_line(raw),
                        indent=_measure_indent(raw_text),
                    )
                )
    except OSError as exc:
        return f"failed to read file: {exc}"

    if not records or anchor_line > len(records):
        return "anchor_line exceeds file length"

    anchor_index = anchor_line - 1
    effective_indents = _compute_effective_indents(records)
    anchor_indent = effective_indents[anchor_index]
    min_indent = 0 if max_levels == 0 else max(anchor_indent - max_levels * READ_FILE_TAB_WIDTH, 0)
    final_limit = min(limit, guard_limit, len(records))
    if final_limit == 1:
        record = records[anchor_index]
        return [f"L{record.number}: {record.display}"]

    i = anchor_index - 1
    j = anchor_index + 1
    i_counter_min_indent = 0
    j_counter_min_indent = 0

    out: List[_LineRecord] = [records[anchor_index]]

    while len(out) < final_limit:
        progressed = 0
        if i >= 0:
            iu = i
            if effective_indents[iu] >= min_indent:
                out.insert(0, records[iu])
                progressed += 1
                i -= 1
                if effective_indents[iu] == min_indent and not include_siblings:
                    allow_header_comment = include_header and records[iu].is_comment()
                    can_take_line = allow_header_comment or i_counter_min_indent == 0
                    if can_take_line:
                        i_counter_min_indent += 1
                    else:
                        out.pop(0)
                        progressed -= 1
                        i = -1
                if len(out) >= final_limit:
                    break
            else:
                i = -1

        if j < len(records):
            ju = j
            if effective_indents[ju] >= min_indent:
                out.append(records[ju])
                progressed += 1
                j += 1
                if effective_indents[ju] == min_indent and not include_siblings:
                    if j_counter_min_indent > 0:
                        out.pop()
                        progressed -= 1
                        j = len(records)
                    j_counter_min_indent += 1
            else:
                j = len(records)

        if progressed == 0:
            break

    out = _trim_empty_lines(out)
    return [f"L{record.number}: {record.display}" for record in out]


def read_file(
    file_path: str,
    offset: int = 1,
    limit: int = 2000,
    mode: str = "slice",
    indentation: Optional[dict[str, Any]] = None,
) -> str:
    if offset == 0:
        return "offset must be a 1-indexed line number"
    if limit == 0:
        return "limit must be greater than zero"
    display_path = _ensure_path(file_path)
    target = map_container_to_host(display_path)
    if mode not in ("slice", "indentation"):
        return "mode must be either 'slice' or 'indentation'"
    if _matches_blocked_read_suffix(target) or _looks_binary(target):
        return _blocked_read_message(display_path, target)
    if mode == "indentation":
        result = _read_file_indentation(target, offset, limit, indentation or {})
    else:
        result = _read_file_slice(target, offset, limit)
    if isinstance(result, str):
        if str(target) in result and str(display_path) != str(target):
            result = result.replace(str(target), str(display_path))
        return result
    if mode == "slice":
        header = ""
        if result.is_truncated:
            header = (
                f"Showing lines {result.start_line}-{result.end_line} "
                f"of {result.total_lines} total lines.\n---\n"
            )
        return header + "\n".join(result.lines)
    return "\n".join(result)


def grep_files(
    pattern: str,
    glob: Optional[str] = None,
    path: Optional[str] = None,
    limit: int = 100,
) -> str:
    if not pattern or not pattern.strip():
        return "pattern must not be empty"
    if limit == 0:
        return "limit must be greater than zero"
    capped_limit = min(int(limit), 2000)
    display_path = Path.cwd() if path is None else _ensure_path(path)
    search_path = map_container_to_host(display_path)
    try:
        search_path.stat()
    except OSError as exc:
        message = str(exc)
        if str(search_path) in message and str(display_path) != str(search_path):
            message = message.replace(str(search_path), str(display_path))
        return f"unable to access `{display_path}`: {message}"
    include_glob = glob.strip() if glob and glob.strip() else None
    try:
        regex = _compile_search_regex(pattern)
    except re.error as exc:
        return f"invalid regex pattern: {exc}"
    base_path = search_path if search_path.is_dir() else search_path.parent
    display_base_path = display_path if display_path.is_dir() else display_path.parent

    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--color", "never", "--max-count", str(capped_limit)]
        if include_glob:
            cmd.extend(["--glob", include_glob])
        cmd.extend([pattern, str(search_path)])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode in (0, 1):
                matches = _parse_grep_like_output(proc.stdout, base_path, display_base_path)
                return _format_grouped_matches(pattern, matches, capped_limit)
        except subprocess.TimeoutExpired:
            return "rg timed out after 30 seconds"

    if shutil.which("grep"):
        grep_args = ["grep", "-R", "-n", "-H", "-E", pattern]
        if include_glob:
            grep_args.append(f"--include={include_glob}")
        grep_target = "." if search_path.is_dir() else str(search_path)
        grep_args.append(grep_target)
        try:
            proc = subprocess.run(
                grep_args,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                cwd=str(base_path),
            )
            if proc.returncode in (0, 1):
                matches = _parse_grep_like_output(proc.stdout, base_path, display_base_path)
                return _format_grouped_matches(pattern, matches, capped_limit)
        except subprocess.TimeoutExpired:
            return "grep timed out after 30 seconds"

    matches: List[dict[str, Any]] = []
    for candidate, relative in _iter_regular_files(search_path, include_glob):
        if _looks_binary(candidate):
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        file_display = str(display_path / relative) if search_path.is_dir() else str(display_path)
        for idx, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append({"file": file_display, "line": idx, "text": line})
    return _format_grouped_matches(pattern, matches, capped_limit)


def glob_files(
    pattern: str,
    path: str = ".",
    limit: int = 100,
) -> str:
    if not pattern or not pattern.strip():
        return "pattern must not be empty"
    if limit == 0:
        return "limit must be greater than zero"
    display_path = _ensure_path(path)
    search_path = map_container_to_host(display_path)
    try:
        search_path.stat()
    except OSError as exc:
        message = str(exc)
        if str(search_path) in message and str(display_path) != str(search_path):
            message = message.replace(str(search_path), str(display_path))
        return f"unable to access `{display_path}`: {message}"

    if search_path.is_file():
        candidates = [search_path] if fnmatch.fnmatch(search_path.name, pattern) else []
    else:
        absolute_pattern = str(search_path / pattern)
        candidates = [Path(p) for p in glob_module.glob(absolute_pattern, recursive=True)]
    files: List[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_file():
                files.append(candidate.resolve())
        except OSError:
            continue
    unique_files = sorted(set(files), key=lambda item: (-item.stat().st_mtime if item.exists() else 0.0, str(item)))
    if not unique_files:
        return "No matches found."
    results = [str(map_host_to_container(path_obj)) for path_obj in unique_files[: min(int(limit), 2000)]]
    return "\n".join(results)


def _list_dir_impl(
    dir_path: str,
    offset: int = 1,
    limit: int = 25,
    depth: int = 2,
    flat: bool = True,
) -> str:
    if offset == 0:
        return "offset must be a 1-indexed entry number"
    if limit == 0:
        return "limit must be greater than zero"
    if depth == 0:
        return "depth must be greater than zero"
    path = Path(dir_path)
    if not path.is_absolute():
        return "dir_path must be an absolute path"
    display_path = path
    target = map_container_to_host(display_path)
    try:
        target_stat = target.stat()
    except OSError as exc:
        message = str(exc)
        if str(target) in message and str(display_path) != str(target):
            message = message.replace(str(target), str(display_path))
        return f"unable to access `{display_path}`: {message}"
    if not target_stat or not target.is_dir():
        return f"path is not a directory: {display_path}"
    slicer_dir_summary = _maybe_slicer_large_dir_summary(
        target=target,
        display_path=display_path,
        offset=offset,
        limit=limit,
        flat=flat,
    )
    if slicer_dir_summary is not None:
        return slicer_dir_summary
    entries = _list_dir_slice(target, offset, limit, depth)
    if isinstance(entries, str):
        if str(target) in entries and str(display_path) != str(target):
            entries = entries.replace(str(target), str(display_path))
        return entries
    if flat:
        output: List[str] = []
        for entry in entries:
            rel = entry.get("relative_path")
            if rel is None:
                if entry.get("name") == "more_entries":
                    output.append(entry.get("display_name", "More entries found"))
                continue
            abs_path = display_path / rel
            line = _format_entry_name(abs_path)
            if entry.get("kind") == "directory":
                line = f"{line}/"
            output.append(line)
    else:
        output = [f"Absolute path: {display_path}"]
        for entry in entries:
            if entry.get("name") == "more_entries" or entry.get("relative_path") is None:
                output.append(entry.get("display_name", "More entries found"))
            else:
                output.append(_format_entry_line(entry))
    return "\n".join(output)


def _is_slicer_stage_context() -> bool:
    return str(os.environ.get("MRI_AGENT_SLICER_STAGE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_dicom_like_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return lowered.endswith(".dcm")


def _maybe_slicer_large_dir_summary(
    target: Path,
    display_path: Path,
    offset: int,
    limit: int,
    flat: bool,
) -> Optional[str]:
    # Slicer-stage only: avoid dumping huge DICOM directory listings into LLM context.
    if not _is_slicer_stage_context():
        return None
    if not flat or int(offset) != 1:
        return None

    try:
        entry_names = [entry.name for entry in target.iterdir()]
    except OSError:
        return None
    if not entry_names:
        return None

    total = len(entry_names)
    threshold = max(200, int(os.environ.get("SLICER_LIST_DIR_SUMMARY_THRESHOLD", "500") or "500"))
    if total < threshold:
        return None

    dcm_count = sum(1 for name in entry_names if _is_dicom_like_name(name))
    dcm_ratio = dcm_count / max(1, total)
    if dcm_count < 100 or dcm_ratio < 0.5:
        return None

    sample_limit = max(5, min(20, int(os.environ.get("SLICER_LIST_DIR_SUMMARY_SAMPLE", "12") or "12")))
    sample_names = sorted(entry_names)[:sample_limit]
    sample_lines = [f"{_format_entry_name(display_path / name)}" for name in sample_names]

    lines = [
        f"Directory summary: {display_path}",
        f"Total entries: {total}",
        f"DICOM files: {dcm_count}",
        f"Sample entries ({len(sample_lines)}):",
        *sample_lines,
        (
            "Large DICOM directory summarized to save context. "
            "Do not paginate this directory; proceed with conversion/import pipeline. "
            "Only treat files ending in `.dcm` as DICOM payloads and ignore sidecars such as "
            "`SOURCE_PATH.txt`, `.json`, or other non-DICOM metadata files during import."
        ),
    ]
    return "\n".join(lines)


def list_dir(
    dir_path: str,
    offset: int = 1,
    limit: int = 25,
    depth: int = 2,
) -> str:
    return _list_dir_impl(str(_ensure_path(dir_path)), offset=offset, limit=limit, depth=depth, flat=True)


def write_file(path: str, content: str) -> str:
    target = _ensure_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content.encode('utf-8'))} bytes to {target}"


def _format_entry_name(path: Path) -> str:
    normalized = str(path).replace("\\", "/")
    if len(normalized.encode("utf-8")) > READ_FILE_MAX_LINE_LENGTH:
        return _take_bytes_at_char_boundary(normalized, READ_FILE_MAX_LINE_LENGTH)
    return normalized


def _format_entry_component(name: str) -> str:
    if len(name.encode("utf-8")) > READ_FILE_MAX_LINE_LENGTH:
        return _take_bytes_at_char_boundary(name, READ_FILE_MAX_LINE_LENGTH)
    return name


def _format_entry_line(entry: dict[str, Any]) -> str:
    indent = " " * (entry["depth"] * 2)
    name = entry["display_name"]
    kind = entry["kind"]
    if kind == "directory":
        name = f"{name}/"
    elif kind == "symlink":
        name = f"{name}@"
    elif kind == "other":
        name = f"{name}?"
    return f"{indent}{name}"


def _list_dir_slice(
    path: Path,
    offset: int,
    limit: int,
    depth: int,
) -> Union[str, List[dict[str, Any]]]:
    entries: List[dict[str, Any]] = []
    queue: List[tuple[Path, Path, int]] = [(path, Path(""), depth)]

    while queue:
        current_dir, prefix, remaining_depth = queue.pop(0)
        try:
            dir_entries = list(current_dir.iterdir())
        except OSError as exc:
            return f"failed to read directory: {exc}"

        bucket: List[tuple[Path, Path, str, dict[str, Any]]] = []
        for entry in dir_entries:
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except OSError:
                is_symlink = False
                is_dir = False
                is_file = False
            if str(prefix) in ("", "."):
                relative_path = Path(entry.name)
                display_depth = 0
            else:
                relative_path = prefix / entry.name
                display_depth = len(prefix.parts)
            display_name = _format_entry_component(entry.name)
            sort_key = _format_entry_name(relative_path)
            if is_symlink:
                kind = "symlink"
            elif is_dir:
                kind = "directory"
            elif is_file:
                kind = "file"
            else:
                kind = "other"
            bucket.append(
                (
                    entry,
                    relative_path,
                    kind,
                    {
                        "name": sort_key,
                        "display_name": display_name,
                        "depth": display_depth,
                        "kind": kind,
                        "relative_path": relative_path,
                    },
                )
            )

        def sort_key(item: tuple[Path, Path, str, dict[str, Any]]) -> tuple[int, str]:
            kind = item[2]
            if kind == "directory":
                rank = 0
            elif kind == "file":
                rank = 1
            elif kind == "symlink":
                rank = 2
            else:
                rank = 3
            return (rank, item[3]["name"])

        bucket.sort(key=sort_key)
        for entry_path, relative_path, kind, payload in bucket:
            if kind == "directory" and remaining_depth > 1:
                queue.append((entry_path, relative_path, remaining_depth - 1))
            entries.append(payload)

    if not entries:
        return []
    start_index = offset - 1
    if start_index >= len(entries):
        return "offset exceeds directory entry count"
    remaining_entries = len(entries) - start_index
    capped_limit = min(limit, remaining_entries)
    end_index = start_index + capped_limit
    selected = entries[start_index:end_index]
    if end_index < len(entries):
        selected.append(
            {
                "name": "more_entries",
                "display_name": f"More entries available. Use offset={end_index + 1} to continue.",
                "depth": 0,
                "kind": "other",
                "relative_path": None,
            }
        )
    return selected
