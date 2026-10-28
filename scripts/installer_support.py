#!/usr/bin/env python3
"""Small, dependency-free helpers used by the user-level installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.platform import IS_WINDOWS, access_safe, secure_path, fd_permissions, is_reparse_point, filesystem_path, pid_alive
from typing import Iterable


SCHEMA_VERSION = 1
UPGRADE_SCHEMA_VERSION = 1
_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)(?:[-.][0-9A-Za-z.-]+)?$"
)
PROTECTED_MARKER_KEYS = (
    "bin_dir",
    "config_home",
    "state_home",
    "protocols_home",
    "workspace",
    "install_json",
    "slicer_registry",
    "slicer_rc",
)
ROOT_PATHS = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "VERSION",
    "backend",
    "config",
    "install.sh",
    "install.ps1",
    "uninstall.sh",
    "uninstall.ps1",
    "protocols",
    "slicer_extension",
    "scripts/launchers",
    "scripts/native_install.py",
    "scripts/installer_support.py",
    "scripts/runtime_bootstrap.sh",
    "scripts/slicer_registration.py",
    "scripts/slicer_rc_registration.py",
    "scripts/validate_protocol_package.py",
    "RELEASE_MANIFEST.json",
)
EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
EXCLUDED_FILE_NAMES = {".dmri-agent-install", ".env", "install.json"}
SOURCE_ONLY_PATHS = {
    Path(".gitattributes"),
    Path(".gitignore"),
    Path("pyproject.toml"),
    Path("backend/requirements.in"),
}
FORBIDDEN_SUFFIXES = (
    ".ckpt",
    ".dcm",
    ".key",
    ".nhdr",
    ".nii",
    ".nii.gz",
    ".nrrd",
    ".p12",
    ".pem",
    ".pt",
    ".pth",
    ".tck",
    ".trk",
    ".vtk",
    ".vtp",
)
MANDATORY_COPY_PATHS = (
    Path("LICENSE"),
    Path("THIRD_PARTY_NOTICES.md"),
    Path("backend/platform.py"),
    Path("install.ps1"),
    Path("uninstall.ps1"),
    Path("scripts/native_install.py"),
    Path("scripts/launchers/dmri_agent_check"),
    Path("scripts/launchers/dmri_agent_service"),
    Path("scripts/launchers/dmri_agent_slicer"),
    Path("backend/requirements.lock.txt"),
    Path("backend/service/launcher_lifecycle.sh"),
    Path("backend/service/lease_client.py"),
    Path("backend/service/lease_manager.py"),
    Path("backend/service/runtime_broker.py"),
    Path("backend/service/scene_auth.py"),
    Path("scripts/installer_support.py"),
    Path("scripts/runtime_bootstrap.sh"),
    Path("scripts/slicer_registration.py"),
    Path("scripts/slicer_rc_registration.py"),
    Path("slicer_extension/register_module_path.py"),
    Path("slicer_extension/user_startup_bootstrap.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/runtime_bootstrap.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/scene_server.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/session_lifecycle.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/service_integration.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/activity_summary.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/markdown_render.py"),
    Path("slicer_extension/DMRIAgentSlicer/DMRIAgentLib/chat_render.py"),
    Path("backend/model_adapters.py"),
    Path("backend/llm_runtime.py"),
    Path("backend/file_tools.py"),
    Path("backend/slicer_transport.py"),
    Path("protocols/DDSurfer/PROTOCOL.md"),
    Path("protocols/DDSurfer/code_templates/run_ddsurfer.sh"),
    Path("protocols/DDSurfer/code_templates/qc_ddsurfer_outputs.py"),
    Path("protocols/DDParcel/code_templates/invert_affine.py"),
    Path("protocols/WMA/code_templates/labelmap_to_reference_grid.py"),
)


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _is_excluded(relative: Path) -> bool:
    if relative in SOURCE_ONLY_PATHS:
        return True
    parts = relative.parts
    if not parts:
        return False
    if any(part in EXCLUDED_DIR_NAMES for part in parts[:-1]):
        return True
    if parts[:2] in {("docs", "archive"), ("docker", "slicer")}:
        return True
    name = parts[-1]
    if name in EXCLUDED_FILE_NAMES or name.endswith((".pyc", ".pyo", ".tsbuildinfo")):
        return True
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_core(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Unsupported managed version: {value!r}")
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def _marker_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _validate_protected_path(app_dir: Path, label: str, raw: str) -> None:
    if not raw:
        return
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"Protected {label} path must be absolute")
    app = _canonical(app_dir)
    resolved = _canonical(candidate)
    try:
        common = Path(os.path.commonpath((app, resolved)))
    except ValueError:
        return
    if common == app:
        raise ValueError(
            f"Protected {label} path must be outside the application directory: {resolved}"
        )


def validate_managed_layout(
    app_dir: Path,
    marker_path: Path,
    install_json: Path | None = None,
) -> None:
    app = app_dir.absolute()
    marker = marker_path.absolute()
    if not marker.is_file() or marker.is_symlink():
        raise ValueError(f"Managed marker is missing: {marker}")
    values = _marker_values(marker)
    for key in PROTECTED_MARKER_KEYS:
        _validate_protected_path(app, f"recorded {key}", values.get(key, ""))
    if install_json is not None and install_json.is_file():
        try:
            payload = json.loads(install_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Existing install metadata is invalid") from exc
        workspace = payload.get("workspace")
        if workspace is not None and not isinstance(workspace, str):
            raise ValueError("Existing install metadata has an invalid workspace")
        _validate_protected_path(app, "metadata workspace", workspace or "")


def _validate_managed_app(path: Path, expected_app_dir: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Managed application is not a regular directory: {path}")
    marker = path / ".dmri-agent-install"
    if not marker.is_file() or marker.is_symlink():
        raise ValueError(f"Managed marker is missing: {path}")
    values = _marker_values(marker)
    recorded_app = values.get("app_dir")
    if values.get("mode") != "managed" or not recorded_app:
        raise ValueError(f"Install directory does not contain a managed marker: {path}")
    if _canonical(Path(recorded_app)) != _canonical(expected_app_dir):
        raise ValueError(f"Managed marker does not own the install directory: {path}")
    validate_managed_layout(expected_app_dir, marker)
    return values


def _validate_token(value: str) -> str:
    if (
        len(value) != 48
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("Managed transaction token is invalid")
    return value


def _pending_token(path: Path) -> str:
    pending = path / ".dmri-agent-install-pending"
    if pending.is_symlink() or not pending.is_file():
        raise ValueError(f"Managed pending sentinel is missing: {path}")
    info = pending.stat()
    uid_getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if (
        not stat.S_ISREG(info.st_mode)
        or (uid_getter is not None and info.st_uid != uid_getter())
        or info.st_nlink != 1
    ):
        raise ValueError("Managed pending sentinel is unsafe")
    token = pending.read_text(encoding="ascii").strip()
    if not access_safe(pending, private=True):
        raise ValueError("Managed pending sentinel has unsafe permissions")
    return _validate_token(token)


def _read_managed_version(path: Path, label: str) -> str:
    version_path = path / "VERSION"
    if version_path.is_symlink() or not version_path.is_file():
        raise ValueError(f"{label} version must be a regular file: {version_path}")
    return version_path.read_text(encoding="utf-8").strip()


def _upgrade_journal_path(destination: Path) -> Path:
    return destination.parent / f".{destination.name}.dmri-agent-upgrade.json"


def _safe_transaction_child(path: Path, parent: Path, label: str) -> Path:
    absolute = path.absolute()
    if absolute.parent != parent or absolute.name in {"", ".", ".."}:
        raise ValueError(f"Managed {label} path is outside the application parent")
    return absolute


def _load_upgrade_journal(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Managed upgrade journal is not a regular file: {path}")
    info = path.stat()
    uid_getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if uid_getter is not None and info.st_uid != uid_getter():
        raise ValueError("Managed upgrade journal is not owned by the current user")
    if not access_safe(path, private=True):
        raise ValueError("Managed upgrade journal has unsafe permissions")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Managed upgrade journal is invalid") from exc
    required = {
        "schema_version",
        "app_dir",
        "backup_dir",
        "stage_dir",
        "old_version",
        "new_version",
        "token",
        "phase",
        "action",
    }
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("Managed upgrade journal is incomplete")
    if payload.get("schema_version") != UPGRADE_SCHEMA_VERSION:
        raise ValueError("Unsupported managed upgrade journal version")
    for key in required.difference({"schema_version"}):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f"Managed upgrade journal has invalid {key}")
    _validate_token(str(payload["token"]))
    if payload.get("phase") not in {"active", "commit-cleanup", "rollback-cleanup"}:
        raise ValueError("Managed upgrade journal has an invalid phase")
    if payload.get("action") not in {"upgrade", "repair"}:
        raise ValueError("Managed upgrade journal has an invalid action")
    return payload


def _validated_upgrade_paths(
    journal_path: Path, payload: dict[str, object]
) -> tuple[Path, Path, Path, str, str, str]:
    journal = journal_path.absolute()
    destination = Path(str(payload["app_dir"])).absolute()
    expected_journal = _upgrade_journal_path(destination)
    if journal != expected_journal:
        raise ValueError("Managed upgrade journal path does not match APP_DIR")
    parent = destination.parent
    backup = _safe_transaction_child(Path(str(payload["backup_dir"])), parent, "backup")
    stage = _safe_transaction_child(Path(str(payload["stage_dir"])), parent, "stage")
    if len({destination, backup, stage}) != 3:
        raise ValueError("Managed upgrade paths must be distinct")
    token = _validate_token(str(payload["token"]))
    expected_backup = parent / f".{destination.name}.dmri-agent-backup-{token}"
    if backup != expected_backup:
        raise ValueError("Managed upgrade backup path does not match its token")
    return (
        destination,
        backup,
        stage,
        str(payload["old_version"]),
        str(payload["new_version"]),
        token,
    )


def _validate_version_file(path: Path, expected: str) -> None:
    version_path = path / "VERSION"
    if not version_path.is_file() or version_path.is_symlink():
        raise ValueError(f"Managed application version is missing: {path}")
    actual = version_path.read_text(encoding="utf-8").strip()
    if actual != expected:
        raise ValueError(
            f"Managed application version mismatch at {path}: {actual!r} != {expected!r}"
        )


def _remove_transaction_tree(path: Path, parent: Path) -> None:
    path = _safe_transaction_child(path, parent, "cleanup")
    if is_reparse_point(path) or not path.is_dir():
        raise ValueError(f"Refusing to remove unsafe managed transaction path: {path}")
    # The root and parent have been checked above. The extended DOS path keeps
    # descendants accessible when the retained backup adds enough characters to
    # exceed MAX_PATH, without changing the system-wide long-path setting.
    if IS_WINDOWS:
        shutil.rmtree(_filesystem_path(path))
    else:
        shutil.rmtree(path)


def _filesystem_path(path: Path) -> Path:
    return filesystem_path(path)


def _rename_transaction_directory(source: Path, target: Path) -> None:
    """Retry brief Windows sharing violations; retain the journal on failure."""
    end = time.monotonic() + (2.0 if IS_WINDOWS else 0.0)
    while True:
        try:
            os.rename(source, target)
            return
        except OSError as exc:
            if not IS_WINDOWS or getattr(exc, 'winerror', None) not in {5, 32, 33} or time.monotonic() >= end:
                raise
            time.sleep(0.05)


def _failed_upgrade_path(destination: Path, token: str) -> Path:
    return destination.parent / f".{destination.name}.dmri-agent-failed-{token}"


def _finish_rollback_cleanup(
    journal: Path,
    payload: dict[str, object],
    destination: Path,
    backup: Path,
    stage: Path,
    old_version: str,
    token: str,
) -> None:
    parent = destination.parent
    _validate_managed_app(destination, destination)
    _validate_version_file(destination, old_version)
    if backup.exists() or backup.is_symlink():
        raise ValueError("Managed rollback cleanup still has an unexpected backup")
    for path in (_failed_upgrade_path(destination, token), stage):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise ValueError(f"Managed rollback cleanup path is unsafe: {path}")
            _remove_transaction_tree(path, parent)
    journal.unlink()


def _atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    """Write an owner-only transaction file without changing its parent mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        fd_permissions(descriptor, stat.S_IRUSR | stat.S_IWUSR, temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        secure_path(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_upgrade_phase(
    journal: Path, payload: dict[str, object], phase: str
) -> dict[str, object]:
    updated = dict(payload)
    updated["phase"] = phase
    _atomic_private_json(journal, updated)
    return updated


def _manifest_files(source: Path) -> list[Path] | None:
    manifest_path = source / "RELEASE_MANIFEST.json"
    if not manifest_path.is_file():
        return None
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, list):
        raise ValueError("RELEASE_MANIFEST.json does not contain a files list")
    result: list[Path] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("RELEASE_MANIFEST.json contains an invalid file entry")
        relative = Path(item["path"])
        source_path = source / relative
        expected_size = item.get("size")
        expected_hash = item.get("sha256")
        if not source_path.is_file():
            raise ValueError(f"Release manifest file is missing: {relative}")
        if isinstance(expected_size, int) and source_path.stat().st_size != expected_size:
            raise ValueError(f"Release manifest size mismatch: {relative}")
        if isinstance(expected_hash, str) and _file_digest(source_path) != expected_hash:
            raise ValueError(f"Release manifest checksum mismatch: {relative}")
        result.append(relative)
    result.append(Path("RELEASE_MANIFEST.json"))
    return result


def _git_files(source: Path) -> list[Path] | None:
    if not (source / ".git").exists() or shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(source), "ls-files", "-z", "--", *ROOT_PATHS],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return None
    files = [Path(os.fsdecode(value)) for value in result.stdout.split(b"\0") if value]
    known = {path.as_posix() for path in files}
    for relative in MANDATORY_COPY_PATHS:
        if relative.as_posix() not in known and (source / relative).is_file():
            files.append(relative)
    return files


def _walk_files(source: Path) -> Iterable[Path]:
    for root_name in ROOT_PATHS:
        root = source / root_name
        if root.is_file() or root.is_symlink():
            yield Path(root_name)
            continue
        if not root.is_dir():
            continue
        for directory, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(name for name in dir_names if name not in EXCLUDED_DIR_NAMES)
            directory_path = Path(directory)
            for name in sorted(file_names):
                yield (directory_path / name).relative_to(source)


def managed_copy(source: Path, destination: Path) -> int:
    source = _canonical(source)
    destination = destination.absolute()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Managed staging directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    relative_files = _manifest_files(source)
    if relative_files is None:
        relative_files = _git_files(source)
    if relative_files is None:
        relative_files = list(_walk_files(source))

    copied = 0
    seen: set[str] = set()
    for relative in relative_files:
        if relative.is_absolute() or ".." in relative.parts or _is_excluded(relative):
            continue
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        source_path = source / relative
        if not source_path.exists():
            raise ValueError(f"Managed install source is missing: {relative}")
        if source_path.is_symlink():
            raise ValueError(f"Managed installs do not copy symbolic links: {relative}")
        if not source_path.is_file():
            continue
        resolved_source = _canonical(source_path)
        try:
            resolved_source.relative_to(source)
        except ValueError as exc:
            raise ValueError(f"Managed install source escapes its root: {relative}") from exc
        target = _filesystem_path(destination / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_filesystem_path(source_path), target)
        copied += 1

    required = (*MANDATORY_COPY_PATHS, Path("VERSION"), Path("install.sh"), Path("uninstall.sh"))
    missing = [relative for relative in required if not _filesystem_path(destination / relative).is_file()]
    if missing:
        raise ValueError("Managed install copy is incomplete: " + ", ".join(map(str, missing)))
    return copied


def managed_target_action(source: Path, destination: Path) -> str:
    source = _canonical(source)
    destination = destination.absolute()
    if destination.is_symlink():
        raise ValueError(f"Managed install destination must not be a symbolic link: {destination}")
    if not destination.exists() or not any(destination.iterdir()):
        return "install"
    marker = destination / ".dmri-agent-install"
    if not marker.is_file():
        try:
            pending_token = _pending_token(destination)
        except (OSError, UnicodeError, ValueError):
            pending_token = ""
        if not pending_token:
            raise ValueError(f"Install directory is not empty and is not owned: {destination}")
        source_version = _read_managed_version(source, "Source")
        installed_version = _read_managed_version(destination, "Installed")
        if source_version != installed_version:
            raise ValueError("Interrupted install belongs to a different version")
        return "recover"
    _validate_managed_app(destination, destination)
    source_version = _read_managed_version(source, "Source")
    installed_version = _read_managed_version(destination, "Installed")
    if source_version != installed_version:
        source_core = _version_core(source_version)
        installed_core = _version_core(installed_version)
        if source_core <= installed_core:
            raise ValueError(
                f"Refusing managed downgrade from {installed_version} to {source_version}"
            )
        return "upgrade"
    return "repair"


def begin_managed_upgrade(
    source: Path,
    destination: Path,
    stage: Path,
    journal_path: Path,
    token: str,
) -> Path:
    """Activate a staged upgrade or repair while retaining the exact old app."""

    source = _canonical(source)
    destination = destination.absolute()
    stage = stage.absolute()
    journal = journal_path.absolute()
    token = _validate_token(token)
    replacement_action = managed_target_action(source, destination)
    if replacement_action not in {"upgrade", "repair"}:
        raise ValueError("Managed target is not eligible for replacement")
    expected_journal = _upgrade_journal_path(destination)
    if journal != expected_journal:
        raise ValueError("Managed upgrade journal must use the application-owned path")
    if journal.exists() or journal.is_symlink():
        raise ValueError(f"Managed upgrade journal already exists: {journal}")
    if stage.parent != destination.parent or stage == destination:
        raise ValueError("Managed upgrade stage must be a sibling of APP_DIR")
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError(f"Managed upgrade stage is not a regular directory: {stage}")
    if _pending_token(stage) != token:
        raise ValueError("Managed upgrade stage token does not match the transaction")

    old_version = (destination / "VERSION").read_text(encoding="utf-8").strip()
    new_version = (source / "VERSION").read_text(encoding="utf-8").strip()
    _validate_version_file(stage, new_version)
    backup = destination.parent / f".{destination.name}.dmri-agent-backup-{token}"
    if backup.exists() or backup.is_symlink():
        raise ValueError(f"Managed upgrade backup already exists: {backup}")
    payload: dict[str, object] = {
        "schema_version": UPGRADE_SCHEMA_VERSION,
        "app_dir": os.fspath(destination),
        "backup_dir": os.fspath(backup),
        "stage_dir": os.fspath(stage),
        "old_version": old_version,
        "new_version": new_version,
        "token": token,
        "phase": "active",
        "action": replacement_action,
    }
    _atomic_private_json(journal, payload)
    try:
        _rename_transaction_directory(destination, backup)
    except Exception:
        journal.unlink()
        raise
    try:
        _rename_transaction_directory(stage, destination)
    except Exception:
        # Keep the journal if restoring the old directory itself fails; the
        # next installer run can then recover from the exact recorded paths.
        try:
            _rename_transaction_directory(backup, destination)
        except Exception as restore_error:
            raise RuntimeError(
                "Could not activate or restore the managed application; "
                f"recovery journal preserved at {journal}"
            ) from restore_error
        journal.unlink()
        raise
    return backup


def rollback_managed_upgrade(journal_path: Path) -> bool:
    """Restore the retained old managed app and discard only the token-owned new app."""

    journal = journal_path.absolute()
    if not journal.exists() and not journal.is_symlink():
        return False
    payload = _load_upgrade_journal(journal)
    destination, backup, stage, old_version, new_version, token = _validated_upgrade_paths(
        journal, payload
    )
    parent = destination.parent
    phase = str(payload["phase"])
    if phase == "commit-cleanup":
        raise ValueError("A committing managed replacement can no longer be rolled back")
    if phase == "rollback-cleanup":
        _finish_rollback_cleanup(
            journal, payload, destination, backup, stage, old_version, token
        )
        return True
    _validate_managed_app(backup, destination)
    _validate_version_file(backup, old_version)

    failed: Path | None = None
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("Refusing to replace an unsafe managed upgrade destination")
        pending = destination / ".dmri-agent-install-pending"
        if pending.exists():
            if _pending_token(destination) != token:
                raise ValueError("Managed upgrade destination is not owned by this transaction")
        else:
            _validate_managed_app(destination, destination)
        _validate_version_file(destination, new_version)
        failed = _failed_upgrade_path(destination, token)
        if failed.exists() or failed.is_symlink():
            raise ValueError(f"Managed failed-upgrade path already exists: {failed}")
        _rename_transaction_directory(destination, failed)
    _rename_transaction_directory(backup, destination)
    payload = _write_upgrade_phase(journal, payload, "rollback-cleanup")
    _finish_rollback_cleanup(
        journal, payload, destination, backup, stage, old_version, token
    )
    return True


def commit_managed_upgrade(journal_path: Path) -> bool:
    """Delete the retained old app only after the new managed install is complete."""

    journal = journal_path.absolute()
    if not journal.exists() and not journal.is_symlink():
        return False
    payload = _load_upgrade_journal(journal)
    destination, backup, stage, old_version, new_version, _ = _validated_upgrade_paths(
        journal, payload
    )
    parent = destination.parent
    phase = str(payload["phase"])
    if phase == "rollback-cleanup":
        raise ValueError("A rolling-back managed replacement cannot be committed")
    _validate_managed_app(destination, destination)
    _validate_version_file(destination, new_version)
    if (destination / ".dmri-agent-install-pending").exists():
        raise ValueError("Cannot commit a managed upgrade while its pending sentinel exists")
    if phase == "active":
        _validate_managed_app(backup, destination)
        _validate_version_file(backup, old_version)
        if stage.exists() or stage.is_symlink():
            raise ValueError("Cannot commit a managed upgrade while its stage still exists")
        payload = _write_upgrade_phase(journal, payload, "commit-cleanup")
    if backup.exists() or backup.is_symlink():
        if backup.is_symlink() or not backup.is_dir():
            raise ValueError("Managed commit cleanup path is unsafe")
        _remove_transaction_tree(backup, parent)
    if stage.exists() or stage.is_symlink():
        raise ValueError("Managed commit cleanup found an unexpected stage")
    journal.unlink()
    return True


def recover_managed_upgrade(journal_path: Path) -> str:
    """Resolve a crash: keep a completed new app, otherwise restore the old app."""

    journal = journal_path.absolute()
    if not journal.exists() and not journal.is_symlink():
        return "none"
    payload = _load_upgrade_journal(journal)
    destination, backup, stage, old_version, new_version, token = _validated_upgrade_paths(
        journal, payload
    )
    parent = destination.parent
    phase = str(payload["phase"])
    action = str(payload["action"])

    if phase == "commit-cleanup":
        commit_managed_upgrade(journal)
        return "committed"
    if phase == "rollback-cleanup":
        _finish_rollback_cleanup(
            journal, payload, destination, backup, stage, old_version, token
        )
        return "rolled-back"

    # Journal creation precedes the first directory rename.  For a same-version
    # repair, VERSION cannot distinguish the old payload from the new one, so an
    # exact token-owned stage with no backup is definitive pre-activation state.
    if not backup.exists() and (stage.exists() or stage.is_symlink()):
        if stage.is_symlink() or _pending_token(stage) != token:
            raise ValueError("Managed pre-activation stage is not owned by this transaction")
        _validate_version_file(stage, new_version)
        _validate_managed_app(destination, destination)
        _validate_version_file(destination, old_version)
        _write_upgrade_phase(journal, payload, "rollback-cleanup")
        _finish_rollback_cleanup(
            journal, payload, destination, backup, stage, old_version, token
        )
        return "rolled-back"

    if backup.exists() and destination.exists():
        try:
            _validate_managed_app(destination, destination)
            _validate_version_file(destination, new_version)
            completed = not (destination / ".dmri-agent-install-pending").exists()
        except ValueError:
            completed = False
        if completed:
            commit_managed_upgrade(journal)
            return "committed"
        rollback_managed_upgrade(journal)
        return "rolled-back"

    if backup.exists() and not destination.exists():
        rollback_managed_upgrade(journal)
        return "rolled-back"

    if destination.exists() and not backup.exists():
        _validate_managed_app(destination, destination)
        failed = _failed_upgrade_path(destination, token)
        if failed.exists() or failed.is_symlink():
            _validate_version_file(destination, old_version)
            _write_upgrade_phase(journal, payload, "rollback-cleanup")
            _finish_rollback_cleanup(
                journal, payload, destination, backup, stage, old_version, token
            )
            return "rolled-back"
        if action == "repair":
            raise ValueError(
                "Same-version replacement state is ambiguous after its stage disappeared; "
                "preserving the journal and application"
            )
        try:
            _validate_version_file(destination, new_version)
            completed = not (destination / ".dmri-agent-install-pending").exists()
        except ValueError:
            completed = False
        if completed:
            journal.unlink()
            return "committed"
        _validate_version_file(destination, old_version)
        journal.unlink()
        return "rolled-back"

    raise ValueError("Managed upgrade journal does not match a recoverable filesystem state")


def finalize_pending_install(destination: Path, app_dir: Path) -> bool:
    """Remove the owner-only interrupted-install sentinel after a valid marker exists."""

    destination = _canonical(destination)
    app_dir = _canonical(app_dir)
    if destination != app_dir:
        raise ValueError("Pending install destination does not match APP_DIR")
    marker = destination / ".dmri-agent-install"
    if not marker.is_file():
        raise ValueError("Cannot finalize an install without its managed marker")
    values: dict[str, str] = {}
    for line in marker.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("mode") != "managed" or _canonical(values.get("app_dir", "")) != app_dir:
        raise ValueError("Managed marker does not own the pending install")
    pending = destination / ".dmri-agent-install-pending"
    if not pending.exists() and not pending.is_symlink():
        return False
    _pending_token(destination)
    pending.unlink()
    return True


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
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


def write_metadata(
    path: Path,
    app_dir: Path,
    version: str,
    backend_port: int,
    *,
    workspace: Path | None = None,
    default_workspace: Path | None = None,
) -> Path:
    if not 1 <= backend_port <= 65535:
        raise ValueError("backend-port must be between 1 and 65535")
    preserved: dict[str, object] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        for key in ("workspace", "auto_start", "auto_start_backend"):
            if key in existing:
                preserved[key] = existing[key]
    if workspace is not None:
        selected_workspace = workspace
    elif isinstance(preserved.get("workspace"), str) and preserved["workspace"]:
        selected_workspace = Path(str(preserved["workspace"]))
    elif default_workspace is not None:
        selected_workspace = default_workspace
    else:
        raise ValueError("A workspace or default-workspace is required")
    selected_workspace = selected_workspace.expanduser().absolute()
    selected_workspace.mkdir(parents=True, exist_ok=True)
    selected_workspace = _canonical(selected_workspace)
    if selected_workspace == Path(selected_workspace.anchor):
        raise ValueError("Refusing to use a filesystem root as the workspace")
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "app_dir": os.fspath(_canonical(app_dir)),
        "backend_port": backend_port,
        **preserved,
        "workspace": os.fspath(selected_workspace),
    }
    _atomic_json(path, payload)
    return selected_workspace


def remove_metadata(path: Path, app_dir: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    recorded = payload.get("app_dir")
    if not isinstance(recorded, str) or _canonical(Path(recorded)) != _canonical(app_dir):
        return False
    path.unlink()
    return True


def write_marker(path: Path, values: dict[str, str]) -> None:
    # Markers written before the Slicer-first installer existed always
    # included Web.  Preserve that meaning for direct callers and upgrades.
    if "web" not in values:
        values = {**values, "web": "1"}
    required = {
        "mode",
        "app_dir",
        "bin_dir",
        "state_home",
        "config_home",
        "protocols_home",
        "install_json",
        "slicer_registry",
        "slicer_rc",
        "slicer_registered",
        "web",
        "cli",
        "installed_at",
        "workspace",
    }
    missing = sorted(required.difference(values))
    if missing:
        raise ValueError("Install marker is missing: " + ", ".join(map(str, missing)))
    if values["mode"] not in {"managed", "in_place"}:
        raise ValueError(f"Invalid install mode: {values['mode']}")
    for key, value in values.items():
        if (
            "\n" in key
            or "\r" in key
            or "=" in key
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError(f"Install marker contains an unsafe value for {key}")
    ordered_keys = (
        "mode",
        "app_dir",
        "bin_dir",
        "state_home",
        "config_home",
        "protocols_home",
        "install_json",
        "slicer_registry",
        "slicer_rc",
        "slicer_registered",
        "web",
        "cli",
        "installed_at",
        "workspace",
    )
    payload = "".join(f"{key}={values[key]}\n" for key in ordered_keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        fd_permissions(descriptor, stat.S_IRUSR | stat.S_IWUSR, temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        secure_path(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def require_backend_stopped(state_root: Path) -> None:
    descriptor = state_root / "runtime" / "service.json"
    if not descriptor.exists():
        return
    try:
        payload = json.loads(descriptor.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Could not validate the backend state; close Slicer and retry") from exc
    if pid > 0 and pid_alive(pid):
        raise ValueError("Close Slicer and wait for the Agent backend to exit before changing this installation")


def validate_purge_paths(targets: Iterable[Path], workspaces: Iterable[Path]) -> None:
    """Validate all deletion targets before removing any registration or file."""
    preserved = [_canonical(path.expanduser()) for path in workspaces]
    home = _canonical(Path.home())
    for path in targets:
        if not path.is_absolute():
            raise ValueError(f"Refusing an unsafe purge target (not absolute): {path}")
        target = _canonical(path)
        # Protect home itself and its ancestors, including spelling and symlink aliases.
        if target == Path(target.anchor) or home.is_relative_to(target):
            raise ValueError(f"Refusing an unsafe purge target: {path}")
        if any(workspace.is_relative_to(target) or target.is_relative_to(workspace)
               for workspace in preserved):
            raise ValueError("Refusing purge: the preserved workspace overlaps a deletion target")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    idle_parser = subparsers.add_parser("assert-idle")
    idle_parser.add_argument("--state-home", type=Path, required=True)

    purge_parser = subparsers.add_parser("validate-purge")
    purge_parser.add_argument("--target", type=Path, action="append", required=True)
    purge_parser.add_argument("--workspace", type=Path, action="append", default=[])

    copy_parser = subparsers.add_parser("managed-copy")
    copy_parser.add_argument("--source", type=Path, required=True)
    copy_parser.add_argument("--destination", type=Path, required=True)

    target_parser = subparsers.add_parser("managed-target")
    target_parser.add_argument("--source", type=Path, required=True)
    target_parser.add_argument("--destination", type=Path, required=True)

    begin_upgrade_parser = subparsers.add_parser("begin-upgrade")
    begin_upgrade_parser.add_argument("--source", type=Path, required=True)
    begin_upgrade_parser.add_argument("--destination", type=Path, required=True)
    begin_upgrade_parser.add_argument("--stage", type=Path, required=True)
    begin_upgrade_parser.add_argument("--journal", type=Path, required=True)
    begin_upgrade_parser.add_argument("--token", required=True)

    rollback_upgrade_parser = subparsers.add_parser("rollback-upgrade")
    rollback_upgrade_parser.add_argument("--journal", type=Path, required=True)

    commit_upgrade_parser = subparsers.add_parser("commit-upgrade")
    commit_upgrade_parser.add_argument("--journal", type=Path, required=True)

    recover_upgrade_parser = subparsers.add_parser("recover-upgrade")
    recover_upgrade_parser.add_argument("--journal", type=Path, required=True)

    validate_layout_parser = subparsers.add_parser("validate-layout")
    validate_layout_parser.add_argument("--app-dir", type=Path, required=True)
    validate_layout_parser.add_argument("--marker", type=Path, required=True)
    validate_layout_parser.add_argument("--install-json", type=Path)

    metadata_parser = subparsers.add_parser("write-metadata")
    metadata_parser.add_argument("--path", type=Path, required=True)
    metadata_parser.add_argument("--app-dir", type=Path, required=True)
    metadata_parser.add_argument("--version", required=True)
    metadata_parser.add_argument("--backend-port", type=int, required=True)
    metadata_parser.add_argument("--workspace", type=Path)
    metadata_parser.add_argument("--default-workspace", type=Path)

    remove_parser = subparsers.add_parser("remove-metadata")
    remove_parser.add_argument("--path", type=Path, required=True)
    remove_parser.add_argument("--app-dir", type=Path, required=True)

    finalize_parser = subparsers.add_parser("finalize-pending")
    finalize_parser.add_argument("--destination", type=Path, required=True)
    finalize_parser.add_argument("--app-dir", type=Path, required=True)

    marker_parser = subparsers.add_parser("write-marker")
    marker_parser.add_argument("--path", type=Path, required=True)
    for key in (
        "mode",
        "app-dir",
        "bin-dir",
        "state-home",
        "config-home",
        "protocols-home",
        "install-json",
        "slicer-registry",
        "slicer-rc",
        "slicer-registered",
        "web",
        "cli",
        "installed-at",
        "workspace",
    ):
        marker_parser.add_argument(f"--{key}", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "assert-idle":
        try:
            require_backend_stopped(args.state_home)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "validate-purge":
        validate_purge_paths(args.target, args.workspace)
    elif args.command == "managed-copy":
        count = managed_copy(args.source, args.destination)
        print(count)
    elif args.command == "managed-target":
        print(managed_target_action(args.source, args.destination))
    elif args.command == "begin-upgrade":
        print(
            begin_managed_upgrade(
                args.source,
                args.destination,
                args.stage,
                args.journal,
                args.token,
            )
        )
    elif args.command == "rollback-upgrade":
        rollback_managed_upgrade(args.journal)
    elif args.command == "commit-upgrade":
        commit_managed_upgrade(args.journal)
    elif args.command == "recover-upgrade":
        print(recover_managed_upgrade(args.journal))
    elif args.command == "validate-layout":
        validate_managed_layout(args.app_dir, args.marker, args.install_json)
    elif args.command == "write-metadata":
        workspace = write_metadata(
            args.path,
            args.app_dir,
            args.version,
            args.backend_port,
            workspace=args.workspace,
            default_workspace=args.default_workspace,
        )
        print(workspace)
    elif args.command == "remove-metadata":
        remove_metadata(args.path, args.app_dir)
    elif args.command == "finalize-pending":
        finalize_pending_install(args.destination, args.app_dir)
    elif args.command == "write-marker":
        write_marker(
            args.path,
            {
                "mode": args.mode,
                "app_dir": args.app_dir,
                "bin_dir": args.bin_dir,
                "state_home": args.state_home,
                "config_home": args.config_home,
                "protocols_home": args.protocols_home,
                "install_json": args.install_json,
                "slicer_registry": args.slicer_registry,
                "slicer_rc": args.slicer_rc,
                "slicer_registered": args.slicer_registered,
                "web": args.web,
                "cli": args.cli,
                "installed_at": args.installed_at,
                "workspace": args.workspace,
            },
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"installer support error: {exc}") from exc
