#!/usr/bin/env python3
"""Safely manage dMRI-Agent's single block in a user's Slicer RC file.

This module deliberately does not start Slicer and does not write Slicer settings.
It is intended for macOS Slicer packages whose revision settings live inside the
application bundle.  The managed block delegates to a bootstrap script on each
ordinary Slicer startup.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.platform import IS_WINDOWS, access_safe, secure_path, file_lock
if not IS_WINDOWS:
    import fcntl
from typing import Any, Iterable


SCHEMA_VERSION = 1
BLOCK_VERSION = 1
# Persistent identifiers written by earlier releases; keep them stable across
# display-name changes so upgrades replace the existing startup block.
BEGIN_MARKER = "# BEGIN dMRI Agent managed Slicer startup"
END_MARKER = "# END dMRI Agent managed Slicer startup"
METADATA_PREFIX = "# dMRI Agent metadata: "
RUN_NAME = "__dmri_agent_slicer_startup__"


class SlicerRCError(RuntimeError):
    """Base class for safe Slicer RC management errors."""


class UnsafePathError(SlicerRCError):
    """Raised when an RC or transaction path is not safe to mutate."""


class StartupConflictError(SlicerRCError):
    """Raised when a higher-priority Slicer RC would shadow the user RC."""


class ManagedBlockError(SlicerRCError):
    """Raised when the managed block is missing structural guarantees."""


class BlockOwnershipError(SlicerRCError):
    """Raised when an older installation tries to remove a newer block."""


class TransactionError(SlicerRCError):
    """Raised when a transaction cannot be safely created or rolled back."""


_ABSENT = object()


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _current_uid() -> int | None:
    getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    return getter() if getter is not None else None


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _validate_stat(path: Path, info: os.stat_result, *, directory: bool) -> None:
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected:
        kind = "directory" if directory else "regular file"
        raise UnsafePathError(f"Expected {kind}: {path}")
    uid = _current_uid()
    if uid is not None and info.st_uid != uid:
        raise UnsafePathError(f"Path is not owned by the current user: {path}")
    unsafe = not access_safe(path) if IS_WINDOWS else bool(stat.S_IMODE(info.st_mode) & 0o022)
    if unsafe:
        if IS_WINDOWS:
            raise UnsafePathError(f"Path ownership or Windows access permissions are unsafe: {path}")
        raise UnsafePathError(f"Path is group/world writable: {path}")


def _validate_parent(path: Path) -> None:
    parent = path.parent
    try:
        info = os.lstat(parent)
    except FileNotFoundError as exc:
        raise UnsafePathError(f"Parent directory does not exist: {parent}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"Parent directory must not be a symbolic link: {parent}")
    _validate_stat(parent, info, directory=True)


def _validate_existing_file(path: Path) -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise UnsafePathError(f"File disappeared while validating it: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"Refusing to use a symbolic link: {path}")
    _validate_stat(path, info, directory=False)
    return info


def _secure_read(path: Path) -> tuple[bytes, int, tuple[int, int]]:
    info = _validate_existing_file(path)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _validate_stat(path, opened, directory=False)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafePathError(f"File changed while opening it: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read()
        # Windows reports synthetic 0666 mode bits even for a protected DACL.
        # The ACL was checked above; preserve it in place and journal a logical
        # private mode rather than rejecting our own Windows rollback record.
        mode = 0o600 if IS_WINDOWS else stat.S_IMODE(opened.st_mode)
        return data, mode, (opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)


def _read_optional(path: Path) -> tuple[bool, bytes, int, tuple[int, int] | None]:
    if not _lexists(path):
        _validate_parent(path)
        return False, b"", 0o600, None
    data, mode, identity = _secure_read(path)
    return True, data, mode, identity


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _assert_unchanged(path: Path, expected: bytes | object) -> None:
    if expected is _ABSENT:
        if _lexists(path):
            raise TransactionError(f"Path appeared during the transaction: {path}")
        return
    if not isinstance(expected, bytes):
        raise TypeError("expected must be bytes or _ABSENT")
    if not _lexists(path):
        raise TransactionError(f"Path disappeared during the transaction: {path}")
    current, _, _ = _secure_read(path)
    if current != expected:
        raise TransactionError(f"Path changed during the transaction: {path}")


def _windows_write(path: Path, data: bytes, expected: bytes | object) -> None:
    _validate_parent(path)
    with file_lock(path.with_name(path.name + ".dmri-agent.lock")):
        _assert_unchanged(path, expected)
        if expected is not _ABSENT:
            # Preserve the existing user's RC security descriptor and identity.
            with path.open("r+b") as stream:
                if stream.read() != expected:
                    raise TransactionError(f"Path changed during the transaction: {path}")
                stream.seek(0)
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            return
        fd, name = tempfile.mkstemp(prefix=".dmri-slicerrc-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                secure_path(temporary)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _assert_unchanged(path, expected)
            if expected is _ABSENT:
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int,
    expected: bytes | object,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    if IS_WINDOWS:
        _windows_write(path, data, expected)
        return
    _validate_parent(path)
    safe_mode = stat.S_IMODE(mode)
    if safe_mode & 0o022:
        raise UnsafePathError(f"Refusing to create a group/world-writable file: {path}")
    if expected is _ABSENT:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, safe_mode)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise TransactionError(f"Path appeared during the transaction: {path}") from exc
            _fsync_directory(path.parent)
        finally:
            os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return
    if not isinstance(expected, bytes):
        raise TypeError("expected must be bytes or _ABSENT")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = os.fstat(descriptor)
        _validate_stat(path, opened, directory=False)
        if expected_identity is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != expected_identity:
            raise TransactionError(f"Path was replaced before the transaction: {path}")
        current = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            current += chunk
        if current != expected:
            raise TransactionError(f"Path changed during the transaction: {path}")
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise TransactionError(f"Path was replaced during the transaction: {path}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.ftruncate(descriptor, len(data))
        os.fchmod(descriptor, safe_mode)
        os.fsync(descriptor)
        named_after_write = os.lstat(path)
        if (named_after_write.st_dev, named_after_write.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise TransactionError(
                f"Path was replaced while applying the transaction: {path}"
            )
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_delete(
    path: Path,
    *,
    expected: bytes,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    if IS_WINDOWS:
        _validate_parent(path)
        with file_lock(path.with_name(path.name + ".dmri-agent.lock")):
            _assert_unchanged(path, expected)
            path.unlink()
        return
    _validate_parent(path)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = os.fstat(descriptor)
        _validate_stat(path, opened, directory=False)
        if expected_identity is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != expected_identity:
            raise TransactionError(f"Path was replaced before the transaction: {path}")
        current = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            current += chunk
        if current != expected:
            raise TransactionError(f"Path changed during the transaction: {path}")
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise TransactionError(f"Path was replaced during the transaction: {path}")
        recovery_descriptor, recovery_name = tempfile.mkstemp(
            prefix=f".{path.name}.dmri-agent-delete-", dir=path.parent
        )
        os.close(recovery_descriptor)
        recovery = Path(recovery_name)
        recovery.unlink()
        os.rename(path, recovery)
        moved = os.lstat(recovery)
        os.lseek(descriptor, 0, os.SEEK_SET)
        moved_data = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            moved_data += chunk
        if (
            (moved.st_dev, moved.st_ino) != (opened.st_dev, opened.st_ino)
            or moved_data != expected
        ):
            try:
                os.link(recovery, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise TransactionError(
                    "Path changed while removing it; concurrent content was preserved at "
                    f"{recovery}"
                ) from exc
            recovery.unlink()
            raise TransactionError(f"Path was replaced during the transaction: {path}")
        recovery.unlink()
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _decode(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManagedBlockError(f"Slicer RC file is not valid UTF-8: {path}") from exc


def _newline_for(text: str) -> str:
    first_lf = text.find("\n")
    if first_lf >= 1 and text[first_lf - 1] == "\r":
        return "\r\n"
    return "\n"


def _line_records(text: str) -> list[tuple[int, int, str, str]]:
    records: list[tuple[int, int, str, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        ending = ""
        body = line
        if line.endswith("\r\n"):
            body, ending = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, ending = line[:-1], line[-1]
        records.append((offset, offset + len(line), body, ending))
        offset += len(line)
    if offset < len(text):
        records.append((offset, len(text), text[offset:], ""))
    return records


def _parse_block(text: str) -> dict[str, Any] | None:
    records = _line_records(text)
    begins = [index for index, record in enumerate(records) if record[2] == BEGIN_MARKER]
    ends = [index for index, record in enumerate(records) if record[2] == END_MARKER]
    marker_text_present = BEGIN_MARKER in text or END_MARKER in text
    if not begins and not ends:
        if marker_text_present:
            raise ManagedBlockError("Malformed dMRI-Agent marker text in Slicer RC file")
        return None
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise ManagedBlockError("Slicer RC file must contain exactly one complete managed block")
    begin_index, end_index = begins[0], ends[0]
    if begin_index + 1 >= end_index:
        raise ManagedBlockError("Managed Slicer RC block has no metadata")
    metadata_line = records[begin_index + 1][2]
    if not metadata_line.startswith(METADATA_PREFIX):
        raise ManagedBlockError("Managed Slicer RC block metadata is missing")
    try:
        metadata = json.loads(metadata_line[len(METADATA_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ManagedBlockError("Managed Slicer RC block metadata is invalid") from exc
    if not isinstance(metadata, dict) or metadata.get("block_version") != BLOCK_VERSION:
        raise ManagedBlockError("Unsupported managed Slicer RC block version")
    owner_id = metadata.get("owner_id")
    bootstrap_path = metadata.get("bootstrap_path")
    prefix_separator = metadata.get("prefix_separator")
    prefix_sha256 = metadata.get("prefix_sha256")
    file_created = metadata.get("file_created")
    if not isinstance(owner_id, str) or not owner_id:
        raise ManagedBlockError("Managed Slicer RC block has no owner")
    if not isinstance(bootstrap_path, str) or not bootstrap_path:
        raise ManagedBlockError("Managed Slicer RC block has no bootstrap path")
    if prefix_separator not in {"", "\n", "\r\n"}:
        raise ManagedBlockError("Managed Slicer RC block has invalid boundary metadata")
    if not isinstance(prefix_sha256, str) or len(prefix_sha256) != 64:
        raise ManagedBlockError("Managed Slicer RC block has invalid prefix metadata")
    if not isinstance(file_created, bool):
        raise ManagedBlockError("Managed Slicer RC block has invalid file metadata")
    return {
        "start": records[begin_index][0],
        "end": records[end_index][1],
        "trailing_newline": records[end_index][3],
        "metadata": metadata,
    }


def _validate_identifier(owner_id: str) -> str:
    if not isinstance(owner_id, str) or not owner_id or "\n" in owner_id or "\r" in owner_id:
        raise ValueError("owner_id must be a non-empty single-line string")
    if len(owner_id) > 1024:
        raise ValueError("owner_id is unreasonably long")
    # Installer ownership identifiers are absolute application paths.  Resolve
    # platform aliases such as macOS /var -> /private/var so install and
    # uninstall compare the same owner even when launched through different
    # path spellings.  Non-path identifiers remain useful in unit tests.
    return os.path.realpath(owner_id) if os.path.isabs(owner_id) else owner_id


def _validate_bootstrap_path(path: Path | str) -> Path:
    bootstrap = _absolute(path)
    _validate_existing_file(bootstrap)
    return bootstrap.resolve(strict=True)


def _render_block(
    bootstrap_path: Path,
    owner_id: str,
    *,
    newline: str,
    trailing_newline: str,
    prefix_separator: str,
    prefix_sha256: str,
    file_created: bool,
) -> str:
    metadata = {
        "block_version": BLOCK_VERSION,
        "bootstrap_path": os.fspath(bootstrap_path),
        "file_created": file_created,
        "owner_id": owner_id,
        "prefix_separator": prefix_separator,
        "prefix_sha256": prefix_sha256,
    }
    metadata_json = json.dumps(metadata, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    bootstrap_literal = repr(os.fspath(bootstrap_path))
    lines = [
        BEGIN_MARKER,
        METADATA_PREFIX + metadata_json,
        "try:",
        "    import runpy as _dmri_agent_runpy",
        f"    _dmri_agent_bootstrap = _dmri_agent_runpy.run_path({bootstrap_literal}, run_name={RUN_NAME!r})",
        "    _dmri_agent_bootstrap['install']()",
        "except Exception:",
        "    import logging as _dmri_agent_logging",
        "    _dmri_agent_logging.getLogger('dmri_agent.slicer.user_startup').exception(",
        "        'Could not load the installed dMRI-Agent startup bootstrap; Slicer will continue'",
        "    )",
        "finally:",
        "    try:",
        "        del _dmri_agent_bootstrap",
        "    except NameError:",
        "        pass",
        "    try:",
        "        del _dmri_agent_logging",
        "    except NameError:",
        "        pass",
        "    try:",
        "        del _dmri_agent_runpy",
        "    except NameError:",
        "        pass",
        END_MARKER,
    ]
    return newline.join(lines) + trailing_newline


def validate_startup_resolution(
    rc_path: Path | str,
    *,
    slicer_home_rc: Path | str | Iterable[Path | str] | None = None,
    slicerrc_override: str | None = None,
) -> None:
    """Reject startup state that would shadow ``rc_path``.

    Slicer checks an application-home RC first, then ``SLICERRC``, then the
    user's ``~/.slicerrc.py``.  Callers discover the target Slicer home and pass
    its candidate here; this module never guesses an application path.
    """

    target = _absolute(rc_path)
    if slicer_home_rc is None:
        home_candidates: Iterable[Path | str] = ()
    elif isinstance(slicer_home_rc, (str, os.PathLike)):
        home_candidates = (slicer_home_rc,)
    else:
        home_candidates = slicer_home_rc
    for raw_candidate in home_candidates:
        home_candidate = _absolute(raw_candidate)
        if home_candidate != target and _lexists(home_candidate):
            raise StartupConflictError(
                f"Slicer application-home RC shadows the user RC: {home_candidate}"
            )
    if slicerrc_override:
        override = _absolute(slicerrc_override)
        if override != target:
            raise StartupConflictError(
                f"SLICERRC points to a different startup file: {override}"
            )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_journal(
    journal_path: Path,
    *,
    rc_path: Path,
    previous_exists: bool,
    previous_data: bytes,
    previous_mode: int,
    after_exists: bool,
    after_data: bytes,
    after_mode: int | None,
) -> bytes:
    if journal_path == rc_path:
        raise TransactionError("Transaction journal must not be the Slicer RC file")
    if _lexists(journal_path):
        raise TransactionError(f"Transaction journal already exists: {journal_path}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rc_path": os.fspath(rc_path),
        "previous_exists": previous_exists,
        "previous_content_b64": base64.b64encode(previous_data).decode("ascii"),
        "previous_mode": stat.S_IMODE(previous_mode),
        "after_exists": after_exists,
        "after_sha256": _digest(after_data) if after_exists else None,
        "after_mode": stat.S_IMODE(after_mode) if after_exists and after_mode is not None else None,
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(journal_path, data, mode=0o600, expected=_ABSENT)
    return data


def register(
    rc_path: Path | str,
    bootstrap_path: Path | str,
    owner_id: str,
    *,
    slicer_home_rc: Path | str | Iterable[Path | str] | None = None,
    slicerrc_override: str | None = None,
    journal_path: Path | str | None = None,
) -> dict[str, Any]:
    """Insert or replace the one managed block, atomically and idempotently."""

    rc = _absolute(rc_path)
    bootstrap = _validate_bootstrap_path(bootstrap_path)
    owner = _validate_identifier(owner_id)
    if os.path.isabs(owner):
        owner_path = Path(owner).resolve(strict=True)
        try:
            bootstrap.relative_to(owner_path)
        except ValueError as exc:
            raise UnsafePathError(
                f"Slicer startup bootstrap is outside the owning application: {bootstrap}"
            ) from exc
    validate_startup_resolution(
        rc, slicer_home_rc=slicer_home_rc, slicerrc_override=slicerrc_override
    )
    existed, previous_data, mode, previous_identity = _read_optional(rc)
    text = _decode(previous_data, rc)
    parsed = _parse_block(text)
    newline = _newline_for(text)
    previous_owner = None
    if parsed is None:
        prefix_separator = "" if not text or text.endswith(("\n", "\r")) else newline
        created_file = not existed
        prefix_sha256 = _digest(previous_data)
        trailing_newline = newline
        block = _render_block(
            bootstrap,
            owner,
            newline=newline,
            trailing_newline=trailing_newline,
            prefix_separator=prefix_separator,
            prefix_sha256=prefix_sha256,
            file_created=created_file,
        )
        updated_text = text + prefix_separator + block
    else:
        metadata = parsed["metadata"]
        previous_owner = metadata["owner_id"]
        if previous_owner != owner:
            raise BlockOwnershipError(
                f"Managed block belongs to {previous_owner!r}; uninstall it before installing {owner!r}"
            )
        trailing_newline = parsed["trailing_newline"]
        block = _render_block(
            bootstrap,
            owner,
            newline=newline,
            trailing_newline=trailing_newline,
            prefix_separator=metadata["prefix_separator"],
            prefix_sha256=metadata["prefix_sha256"],
            file_created=metadata["file_created"],
        )
        updated_text = text[: parsed["start"]] + block + text[parsed["end"] :]
        created_file = metadata["file_created"]
    updated_data = updated_text.encode("utf-8")
    if updated_data == previous_data:
        return {
            "action": "register",
            "changed": False,
            "rc_path": os.fspath(rc),
            "owner_id": owner,
            "previous_owner_id": previous_owner,
        }
    journal = _absolute(journal_path) if journal_path is not None else None
    if journal is not None:
        _write_journal(
            journal,
            rc_path=rc,
            previous_exists=existed,
            previous_data=previous_data,
            previous_mode=mode,
            after_exists=True,
            after_data=updated_data,
            after_mode=mode if existed else 0o600,
        )
    _atomic_write(
        rc,
        updated_data,
        mode=mode if existed else 0o600,
        expected=previous_data if existed else _ABSENT,
        expected_identity=previous_identity,
    )
    return {
        "action": "register",
        "changed": True,
        "created_file": created_file,
        "rc_path": os.fspath(rc),
        "owner_id": owner,
        "previous_owner_id": previous_owner,
    }


def unregister_from_record(
    registry_path: Path | str,
    owner_id: str,
) -> dict[str, Any]:
    """Remove a registration using its owner-only installer record.

    The uninstall entrypoint may run with a different ``HOME`` than the one
    used during installation.  The exact RC path therefore comes from the
    private registration record, never from the current process environment.
    """

    registry = _absolute(registry_path)
    expected_owner = _validate_identifier(owner_id)
    data, _, _ = _secure_read(registry)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedBlockError("Slicer startup registration record is invalid") from exc
    if not isinstance(payload, dict) or payload.get("action") != "register":
        raise ManagedBlockError("Slicer startup registration record is incomplete")
    recorded_owner = payload.get("owner_id")
    rc_value = payload.get("rc_path")
    if recorded_owner != expected_owner:
        raise BlockOwnershipError(
            f"Registration record belongs to {recorded_owner!r}, not {expected_owner!r}"
        )
    if not isinstance(rc_value, str) or not os.path.isabs(rc_value):
        raise ManagedBlockError("Slicer startup registration record has an invalid RC path")
    result = unregister(rc_value, expected_owner)
    result["registry_path"] = os.fspath(registry)
    return result


def unregister(
    rc_path: Path | str,
    owner_id: str,
    *,
    journal_path: Path | str | None = None,
) -> dict[str, Any]:
    """Remove only the matching managed block and preserve all user content."""

    rc = _absolute(rc_path)
    owner = _validate_identifier(owner_id)
    if not _lexists(rc):
        _validate_parent(rc)
        return {"action": "unregister", "changed": False, "rc_path": os.fspath(rc)}
    previous_data, mode, previous_identity = _secure_read(rc)
    text = _decode(previous_data, rc)
    parsed = _parse_block(text)
    if parsed is None:
        return {"action": "unregister", "changed": False, "rc_path": os.fspath(rc)}
    metadata = parsed["metadata"]
    if metadata["owner_id"] != owner:
        raise BlockOwnershipError(
            f"Managed block belongs to {metadata['owner_id']!r}, not {owner!r}"
        )
    before = text[: parsed["start"]]
    separator = metadata["prefix_separator"]
    if separator and before.endswith(separator):
        candidate = before[: -len(separator)]
        if _digest(candidate.encode("utf-8")) == metadata["prefix_sha256"]:
            before = candidate
    updated_text = before + text[parsed["end"] :]
    updated_data = updated_text.encode("utf-8")
    delete_file = bool(metadata["file_created"] and not updated_data)
    journal = _absolute(journal_path) if journal_path is not None else None
    if journal is not None:
        _write_journal(
            journal,
            rc_path=rc,
            previous_exists=True,
            previous_data=previous_data,
            previous_mode=mode,
            after_exists=not delete_file,
            after_data=updated_data,
            after_mode=None if delete_file else mode,
        )
    if delete_file:
        _atomic_delete(
            rc,
            expected=previous_data,
            expected_identity=previous_identity,
        )
    else:
        _atomic_write(
            rc,
            updated_data,
            mode=mode,
            expected=previous_data,
            expected_identity=previous_identity,
        )
    return {
        "action": "unregister",
        "changed": True,
        "deleted_file": delete_file,
        "rc_path": os.fspath(rc),
        "owner_id": owner,
    }


def _load_journal(
    journal_path: Path,
) -> tuple[dict[str, Any], bytes, tuple[int, int]]:
    data, _, identity = _secure_read(journal_path)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransactionError("Slicer RC transaction journal is invalid") from exc
    required = {
        "schema_version",
        "rc_path",
        "previous_exists",
        "previous_content_b64",
        "previous_mode",
        "after_exists",
        "after_sha256",
        "after_mode",
    }
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise TransactionError("Slicer RC transaction journal is incomplete")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise TransactionError("Unsupported Slicer RC transaction journal version")
    return payload, data, identity


def rollback(journal_path: Path | str) -> dict[str, Any]:
    """Restore the exact pre-change RC state if no third-party edit intervened."""

    journal = _absolute(journal_path)
    payload, journal_data, journal_identity = _load_journal(journal)
    rc_value = payload.get("rc_path")
    if not isinstance(rc_value, str) or not os.path.isabs(rc_value):
        raise TransactionError("Transaction journal has an invalid RC path")
    rc = _absolute(rc_value)
    try:
        previous_data = base64.b64decode(payload["previous_content_b64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise TransactionError("Transaction journal has invalid prior content") from exc
    previous_exists = payload.get("previous_exists")
    after_exists = payload.get("after_exists")
    previous_mode = payload.get("previous_mode")
    after_digest = payload.get("after_sha256")
    after_mode = payload.get("after_mode")
    if not isinstance(previous_exists, bool) or not isinstance(after_exists, bool):
        raise TransactionError("Transaction journal has invalid existence metadata")
    if not isinstance(previous_mode, int) or previous_mode & 0o022:
        raise TransactionError("Transaction journal has an unsafe prior mode")
    if after_exists and (not isinstance(after_mode, int) or after_mode & 0o022):
        raise TransactionError("Transaction journal has an unsafe resulting mode")
    if not after_exists and after_mode is not None:
        raise TransactionError("Transaction journal has inconsistent mode metadata")

    current_exists = _lexists(rc)
    current_data = b""
    current_mode = None
    if current_exists:
        current_data, current_mode, current_identity = _secure_read(rc)
    else:
        current_identity = None
    previous_matches = current_exists == previous_exists and (
        not previous_exists or (current_data == previous_data and current_mode == previous_mode)
    )
    if previous_matches:
        changed = False
    else:
        expected_after = (
            current_exists == after_exists
            and (
                (not after_exists)
                or (
                    isinstance(after_digest, str)
                    and _digest(current_data) == after_digest
                    and current_mode == after_mode
                )
            )
        )
        if not expected_after:
            raise TransactionError("Slicer RC changed after the managed transaction; refusing rollback")
        if previous_exists:
            _atomic_write(
                rc,
                previous_data,
                mode=previous_mode,
                expected=current_data if current_exists else _ABSENT,
                expected_identity=current_identity,
            )
        elif current_exists:
            _atomic_delete(
                rc,
                expected=current_data,
                expected_identity=current_identity,
            )
        changed = True
    _atomic_delete(
        journal,
        expected=journal_data,
        expected_identity=journal_identity,
    )
    return {
        "action": "rollback",
        "changed": changed,
        "rc_path": os.fspath(rc),
    }


def commit(journal_path: Path | str) -> dict[str, Any]:
    """Consume a successful transaction journal without changing the RC file."""

    journal = _absolute(journal_path)
    if not _lexists(journal):
        return {"action": "commit", "changed": False}
    _, data, identity = _load_journal(journal)
    _atomic_delete(journal, expected=data, expected_identity=identity)
    return {"action": "commit", "changed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--rc", type=Path, required=True)
    register_parser.add_argument("--bootstrap", type=Path, required=True)
    register_parser.add_argument("--owner", required=True)
    register_parser.add_argument("--slicer-home-rc", type=Path, action="append")
    register_parser.add_argument("--slicerrc", default=os.environ.get("SLICERRC"))
    register_parser.add_argument("--journal", type=Path)
    unregister_parser = subparsers.add_parser("unregister")
    unregister_parser.add_argument("--rc", type=Path, required=True)
    unregister_parser.add_argument("--owner", required=True)
    unregister_parser.add_argument("--journal", type=Path)
    unregister_record_parser = subparsers.add_parser("unregister-record")
    unregister_record_parser.add_argument("--registry", type=Path, required=True)
    unregister_record_parser.add_argument("--owner", required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--journal", type=Path, required=True)
    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("--journal", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "register":
        result = register(
            args.rc,
            args.bootstrap,
            args.owner,
            slicer_home_rc=args.slicer_home_rc,
            slicerrc_override=args.slicerrc,
            journal_path=args.journal,
        )
    elif args.command == "unregister":
        result = unregister(args.rc, args.owner, journal_path=args.journal)
    elif args.command == "unregister-record":
        result = unregister_from_record(args.registry, args.owner)
    elif args.command == "rollback":
        result = rollback(args.journal)
    else:
        result = commit(args.journal)
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
