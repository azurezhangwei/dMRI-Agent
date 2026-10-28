#!/usr/bin/env python3
"""Discover Slicer installations and persistently register dMRI-Agent."""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.platform import secure_path, fd_permissions


SCHEMA_VERSION = 1


def canonical(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(path))))


def normalize_executable(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_dir() and candidate.suffix == ".app":
        candidate = candidate / "Contents" / "MacOS" / "Slicer"
    return Path(canonical(candidate))


def discover_slicers(explicit: list[str] | None = None) -> list[Path]:
    candidates: list[Path | str] = []
    explicit = explicit or []
    candidates.extend(explicit)
    environment_paths = os.environ.get("DMRI_SLICER_PATHS", "")
    if not explicit and environment_paths:
        candidates.extend(path for path in environment_paths.split(os.pathsep) if path)

    system = platform.system()
    if not explicit:
        if system == "Darwin":
            for pattern in (
                "/Applications/Slicer*.app",
                os.fspath(Path.home() / "Applications" / "Slicer*.app"),
            ):
                candidates.extend(sorted(glob.glob(pattern)))
        elif system == "Windows":
            path_candidate = shutil.which("Slicer.exe")
            if path_candidate:
                candidates.append(path_candidate)
            local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
            program_files = Path(os.environ.get("ProgramFiles") or "C:/Program Files")
            for pattern in (local / "slicer.org/Slicer*/Slicer.exe", local / "Slicer*/Slicer.exe", program_files / "Slicer*/Slicer.exe"):
                candidates.extend(sorted(glob.glob(str(pattern))))
        elif system == "Linux":
            path_candidate = shutil.which("Slicer")
            if path_candidate:
                candidates.append(path_candidate)
            for pattern in (
                "/opt/Slicer*/Slicer",
                os.fspath(Path.home() / "Applications" / "Slicer*" / "Slicer"),
            ):
                candidates.extend(sorted(glob.glob(pattern)))

    result: list[Path] = []
    seen: set[str] = set()
    for raw_candidate in candidates:
        candidate = normalize_executable(raw_candidate)
        key = canonical(candidate)
        if key in seen:
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            result.append(candidate)
            seen.add(key)
        elif raw_candidate in explicit:
            raise ValueError(f"Slicer executable was not found or is not executable: {raw_candidate}")
    return result


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_path(path.parent, stat.S_IRWXU)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        fd_permissions(descriptor, stat.S_IRUSR | stat.S_IWUSR, temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        secure_path(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_registry(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "installations": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported Slicer registration schema: {payload.get('schema_version')}")
    if not isinstance(payload.get("installations"), list):
        raise ValueError("Slicer registration file has an invalid installations list")
    return payload


def invoke_worker(
    executable: Path,
    worker: Path,
    action: str,
    module_path: str,
    *,
    timeout: int,
) -> dict[str, object]:
    descriptor, result_name = tempfile.mkstemp(prefix="dmri-slicer-registration-", suffix=".json")
    os.close(descriptor)
    result_path = Path(result_name)
    result_path.unlink()
    environment = {
        **os.environ,
        "DMRI_SLICER_REGISTRATION_ACTION": action,
        "DMRI_SLICER_MODULE_PATH": module_path,
        "DMRI_SLICER_REGISTRATION_RESULT": os.fspath(result_path),
    }
    try:
        result = subprocess.run(
            [
                os.fspath(executable),
                "--ignore-slicerrc",
                "--disable-cli-modules",
                "--no-splash",
                "--no-main-window",
                "--python-script",
                os.fspath(worker),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout,
        )
        if not result_path.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Slicer did not write a registration result (exit {result.returncode}): {detail}"
            )
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if result.returncode != 0 or not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or f"Slicer exited {result.returncode}"))
        payload["executable"] = canonical(executable)
        return payload
    finally:
        try:
            result_path.unlink()
        except FileNotFoundError:
            pass


def register(
    registry_path: Path,
    worker: Path,
    module_path: Path,
    explicit: list[str],
    timeout: int,
    journal_path: Path | None = None,
) -> int:
    module = canonical(module_path)
    if not (Path(module) / "DMRIAgentSlicer.py").is_file():
        raise ValueError(f"Slicer module directory is invalid: {module}")
    previous = load_registry(registry_path)
    previous_records = {
        canonical(str(record["executable"])): record
        for record in previous["installations"]
        if isinstance(record, dict) and isinstance(record.get("executable"), str)
    }
    slicers = discover_slicers(explicit)
    successful: list[dict[str, object]] = []
    newly_added: list[dict[str, object]] = []
    removed_old: list[dict[str, object]] = []
    journal: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "registry_path": os.fspath(registry_path.absolute()),
        "previous_registry_exists": registry_path.is_file(),
        "previous_registry": previous,
        "actions": [],
    }
    if journal_path is not None:
        atomic_json(journal_path, journal)

    def record_action(kind: str, executable: Path, path: str) -> None:
        actions = journal["actions"]
        assert isinstance(actions, list)
        actions.append(
            {
                "kind": kind,
                "executable": canonical(executable),
                "module_path": path,
            }
        )
        if journal_path is not None:
            atomic_json(journal_path, journal)

    try:
        for executable in slicers:
            old_record = previous_records.get(canonical(executable))
            previously_owned = bool(
                old_record
                and old_record.get("added", old_record.get("owned", False))
            )
            same_previous_path = bool(
                old_record
                and canonical(str(old_record.get("module_path", ""))) == module
            )
            old_path = canonical(str(old_record.get("module_path", ""))) if old_record else ""
            if previously_owned and old_path and old_path != module:
                removal_result = invoke_worker(
                    executable,
                    worker,
                    "unregister",
                    str(old_record["module_path"]),
                    timeout=timeout,
                )
                if removal_result.get("changed"):
                    record_action("removed", executable, str(old_record["module_path"]))
                    removed_old.append(old_record)
            result = invoke_worker(executable, worker, "register", module, timeout=timeout)
            if result.get("changed"):
                record_action("added", executable, module)
            added = bool(result.get("changed")) or (
                previously_owned and same_previous_path
            )
            previous = None
            if old_record:
                previous = {
                    "module_path": old_record.get("module_path"),
                    "added": previously_owned,
                    "preexisting": bool(old_record.get("preexisting", False)),
                }
            record = {
                **result,
                "module_path": module,
                "added": added,
                "owned": added,
                "preexisting": not added,
                "previous": previous,
            }
            successful.append(record)
            if result.get("changed"):
                newly_added.append(record)
    except Exception:
        if journal_path is not None:
            try:
                rollback(journal_path, worker, timeout)
            except Exception:
                pass
        else:
            for record in reversed(newly_added):
                try:
                    invoke_worker(
                        Path(str(record["executable"])),
                        worker,
                        "unregister",
                        str(record["module_path"]),
                        timeout=timeout,
                    )
                except Exception:
                    pass
            for record in removed_old:
                try:
                    invoke_worker(
                        Path(str(record["executable"])),
                        worker,
                        "register",
                        str(record["module_path"]),
                        timeout=timeout,
                    )
                except Exception:
                    pass
        raise

    records_by_executable = dict(previous_records)
    for record in successful:
        records_by_executable[canonical(str(record["executable"]))] = record
    payload = {
        "schema_version": SCHEMA_VERSION,
        "module_path": module,
        "installations": sorted(
            records_by_executable.values(), key=lambda item: str(item.get("executable", ""))
        ),
    }
    atomic_json(registry_path, payload)
    print(f"Registered dMRI-Agent with {len(successful)} Slicer installation(s).")
    return 0


def rollback(journal_path: Path, worker: Path, timeout: int) -> int:
    if not journal_path.is_file():
        return 0
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported Slicer rollback journal schema")
    actions = journal.get("actions")
    if not isinstance(actions, list):
        raise ValueError("Slicer rollback journal has invalid actions")
    failures: list[str] = []
    for action in reversed(actions):
        if not isinstance(action, dict):
            failures.append("invalid action")
            continue
        executable = action.get("executable")
        module_path = action.get("module_path")
        kind = action.get("kind")
        if not isinstance(executable, str) or not isinstance(module_path, str):
            failures.append("invalid action fields")
            continue
        reverse_action = "unregister" if kind == "added" else "register" if kind == "removed" else ""
        if not reverse_action:
            failures.append(f"invalid action kind: {kind}")
            continue
        try:
            invoke_worker(
                Path(executable), worker, reverse_action, module_path, timeout=timeout
            )
        except Exception as exc:
            failures.append(f"{executable}: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))

    registry_value = journal.get("registry_path")
    previous_registry = journal.get("previous_registry")
    if not isinstance(registry_value, str) or not isinstance(previous_registry, dict):
        raise ValueError("Slicer rollback journal has invalid registry data")
    registry_path = Path(registry_value)
    if journal.get("previous_registry_exists"):
        atomic_json(registry_path, previous_registry)
    else:
        try:
            registry_path.unlink()
        except FileNotFoundError:
            pass
    journal_path.unlink()
    print("Rolled back dMRI-Agent Slicer registration.")
    return 0


def unregister(registry_path: Path, worker: Path, timeout: int) -> int:
    payload = load_registry(registry_path)
    failures: list[dict[str, object]] = []
    removed = 0
    for record in payload["installations"]:
        if not isinstance(record, dict):
            continue
        executable_value = record.get("executable")
        module_value = record.get("module_path") or payload.get("module_path")
        if not isinstance(executable_value, str) or not isinstance(module_value, str):
            failures.append(record)
            continue
        if not record.get("added", record.get("owned", False)):
            removed += 1
            continue
        executable = Path(executable_value)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            failures.append(record)
            continue
        try:
            invoke_worker(executable, worker, "unregister", module_value, timeout=timeout)
            removed += 1
        except Exception as exc:
            failures.append({**record, "uninstall_error": str(exc)})
    if failures:
        atomic_json(
            registry_path,
            {
                **payload,
                "installations": failures,
            },
        )
        raise RuntimeError(
            f"Could not remove dMRI-Agent from {len(failures)} Slicer installation(s); "
            f"registration record preserved at {registry_path}"
        )
    try:
        registry_path.unlink()
    except FileNotFoundError:
        pass
    print(f"Removed dMRI-Agent from {removed} Slicer installation(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--slicer", action="append", default=[])

    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--registry", type=Path, required=True)
    register_parser.add_argument("--worker", type=Path, required=True)
    register_parser.add_argument("--module-path", type=Path, required=True)
    register_parser.add_argument("--slicer", action="append", default=[])
    register_parser.add_argument("--timeout", type=int, default=120)
    register_parser.add_argument("--journal", type=Path)

    unregister_parser = subparsers.add_parser("unregister")
    unregister_parser.add_argument("--registry", type=Path, required=True)
    unregister_parser.add_argument("--worker", type=Path, required=True)
    unregister_parser.add_argument("--timeout", type=int, default=120)

    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--journal", type=Path, required=True)
    rollback_parser.add_argument("--worker", type=Path, required=True)
    rollback_parser.add_argument("--timeout", type=int, default=120)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "discover":
        print(json.dumps([os.fspath(path) for path in discover_slicers(args.slicer)]))
        return 0
    if args.command == "register":
        return register(
            args.registry,
            args.worker,
            args.module_path,
            args.slicer,
            args.timeout,
            args.journal,
        )
    if args.command == "unregister":
        return unregister(args.registry, args.worker, args.timeout)
    return rollback(args.journal, args.worker, args.timeout)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Slicer registration error: {exc}") from exc
