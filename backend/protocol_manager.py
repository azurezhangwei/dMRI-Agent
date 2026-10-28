from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.project_paths import PROJECT_ROOT
from backend.platform import is_reparse_point
from backend.protocol_registry import parse_protocol_frontmatter
from backend.runtime_state import atomic_write_text, contains_secret, managed_protocols_home, state_home


PROTOCOL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
ALLOWED_TYPES = {"pipeline", "supplement", "knowledge", "workflow"}
MAX_PROTOCOL_BYTES = 200_000


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtocolValidation:
    valid: bool
    errors: list[str]
    metadata: dict[str, str]
    revision: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "metadata": self.metadata,
            "revision": self.revision,
        }


class ProtocolStore:
    def __init__(
        self,
        *,
        protocols_root: Path | None = None,
        state_root: Path | None = None,
    ) -> None:
        self.protocols_root = (protocols_root or managed_protocols_home()).expanduser().resolve()
        root = (state_root or state_home()).expanduser().resolve()
        self.drafts_root = root / "protocol-drafts"
        self.backups_root = root / "protocol-backups"

    def status(self, protocol_id: str = "") -> dict[str, Any]:
        if protocol_id:
            normalized = self._validate_id(protocol_id)
            return {
                "protocol_id": normalized,
                "builtin": (PROJECT_ROOT / "protocols" / normalized / "PROTOCOL.md").is_file(),
                "published": self._describe(self.protocols_root / normalized / "PROTOCOL.md"),
                "draft": self._describe(self.drafts_root / normalized / "PROTOCOL.md"),
                "backups": self._backup_revisions(normalized),
            }
        return {
            "managed_protocols": self._directory_ids(self.protocols_root),
            "drafts": self._directory_ids(self.drafts_root),
        }

    def save_draft(self, protocol_id: str, protocol_markdown: str, source_summary: str = "") -> dict[str, Any]:
        normalized = self._validate_id(protocol_id)
        if self._is_builtin(normalized):
            raise ProtocolError("built-in protocols are read-only; use a new managed protocol id")
        validation = self.validate(protocol_markdown)
        if not validation.valid:
            raise ProtocolError("invalid protocol draft: " + "; ".join(validation.errors))
        target = self.drafts_root / normalized / "PROTOCOL.md"
        self._ensure_safe_target(target, self.drafts_root)
        atomic_write_text(target, protocol_markdown.rstrip() + "\n")
        metadata = {
            "protocol_id": normalized,
            "revision": validation.revision,
            "source_summary": source_summary[:2000],
            "updated_at": _utc_now(),
        }
        atomic_write_text(
            target.parent / "draft.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        return {"ok": True, **metadata, "validation": validation.as_dict()}

    def publish(
        self,
        protocol_id: str,
        *,
        expected_revision: str = "",
        rollback_revision: str = "",
    ) -> dict[str, Any]:
        normalized = self._validate_id(protocol_id)
        if self._is_builtin(normalized):
            raise ProtocolError("built-in protocols are read-only")
        target = self.protocols_root / normalized / "PROTOCOL.md"
        self._ensure_safe_target(target, self.protocols_root)

        if rollback_revision:
            source = self.backups_root / normalized / rollback_revision / "PROTOCOL.md"
            self._ensure_safe_target(source, self.backups_root)
            if not source.is_file():
                raise ProtocolError(f"backup revision not found: {rollback_revision}")
            content = source.read_text(encoding="utf-8")
        else:
            source = self.drafts_root / normalized / "PROTOCOL.md"
            self._ensure_safe_target(source, self.drafts_root)
            if not source.is_file():
                raise ProtocolError(f"protocol draft not found: {normalized}")
            content = source.read_text(encoding="utf-8")

        validation = self.validate(content)
        if not validation.valid:
            raise ProtocolError("cannot publish invalid protocol: " + "; ".join(validation.errors))
        if expected_revision and expected_revision != validation.revision:
            raise ProtocolError(
                f"draft revision changed: expected {expected_revision}, got {validation.revision}"
            )
        backup_revision = ""
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
            backup_revision = self._revision(existing)
            backup = self.backups_root / normalized / backup_revision / "PROTOCOL.md"
            self._ensure_safe_target(backup, self.backups_root)
            if not backup.exists():
                atomic_write_text(backup, existing)
        atomic_write_text(target, content.rstrip() + "\n")
        return {
            "ok": True,
            "protocol_id": normalized,
            "revision": validation.revision,
            "backup_revision": backup_revision or None,
            "rolled_back_from": rollback_revision or None,
            "published_at": _utc_now(),
        }

    def validate(self, protocol_markdown: str) -> ProtocolValidation:
        errors: list[str] = []
        encoded = protocol_markdown.encode("utf-8")
        if len(encoded) > MAX_PROTOCOL_BYTES:
            errors.append(f"protocol exceeds {MAX_PROTOCOL_BYTES} bytes")
        if contains_secret(protocol_markdown):
            errors.append("protocol appears to contain a credential or secret")
        metadata = parse_protocol_frontmatter(protocol_markdown)
        for field in ("name", "description", "version", "type"):
            if not metadata.get(field, "").strip():
                errors.append(f"missing frontmatter field: {field}")
        version = metadata.get("version", "")
        if version and not SEMVER_RE.fullmatch(version):
            errors.append("version must use semantic versioning, for example 1.0.0")
        protocol_type = metadata.get("type", "")
        if protocol_type and protocol_type not in ALLOWED_TYPES:
            errors.append(f"type must be one of: {', '.join(sorted(ALLOWED_TYPES))}")
        body = protocol_markdown.split("---", 2)[-1] if protocol_markdown.startswith("---") else ""
        if not re.search(r"^#\s+\S", body, flags=re.MULTILINE):
            errors.append("protocol body must contain a level-1 heading")
        return ProtocolValidation(not errors, errors, metadata, self._revision(protocol_markdown))

    def _is_builtin(self, protocol_id: str) -> bool:
        return (PROJECT_ROOT / "protocols" / protocol_id / "PROTOCOL.md").is_file()

    def _validate_id(self, protocol_id: str) -> str:
        value = protocol_id.strip()
        if not PROTOCOL_ID_RE.fullmatch(value):
            raise ProtocolError("protocol_id must match [A-Za-z][A-Za-z0-9_-]{1,63}")
        return value

    def _ensure_safe_target(self, target: Path, root: Path) -> None:
        raw_root = Path(os.path.abspath(root.expanduser()))
        raw_target = Path(os.path.abspath(target.expanduser()))
        try:
            raw_target.relative_to(raw_root)
        except ValueError as exc:
            raise ProtocolError("protocol path escapes managed root") from exc
        current = raw_target
        while current != raw_root:
            try:
                linked = is_reparse_point(current)
            except FileNotFoundError:
                linked = False
            if linked:
                raise ProtocolError("symlinks are not allowed in managed protocol paths")
            current = current.parent
        resolved_root = root.resolve()
        resolved_target = target.resolve(strict=False)
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError as exc:
            raise ProtocolError("protocol path escapes managed root") from exc

    def _describe(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8")
        return self.validate(content).as_dict()

    def _directory_ids(self, root: Path) -> list[str]:
        if not root.is_dir():
            return []
        return sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and (path / "PROTOCOL.md").is_file()
        )

    def _backup_revisions(self, protocol_id: str) -> list[str]:
        root = self.backups_root / protocol_id
        if not root.is_dir():
            return []
        return sorted(
            (path.name for path in root.iterdir() if (path / "PROTOCOL.md").is_file()),
            reverse=True,
        )

    @staticmethod
    def _revision(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def protocol_status(protocol_id: str = "") -> str:
    """List managed protocol drafts, publications, validation, and backup revisions."""
    return json.dumps(ProtocolStore().status(protocol_id), ensure_ascii=False)


def protocol_draft(protocol_id: str, protocol_markdown: str, source_summary: str = "") -> str:
    """Validate and save a managed PROTOCOL.md draft without publishing it."""
    try:
        payload = ProtocolStore().save_draft(protocol_id, protocol_markdown, source_summary)
        return json.dumps(payload, ensure_ascii=False)
    except ProtocolError as exc:
        return f"[error] {exc}"


def protocol_publish(
    protocol_id: str,
    expected_revision: str = "",
    rollback_revision: str = "",
) -> str:
    """Publish a validated managed protocol draft, or restore a recorded backup revision."""
    try:
        payload = ProtocolStore().publish(
            protocol_id,
            expected_revision=expected_revision,
            rollback_revision=rollback_revision,
        )
        return json.dumps(payload, ensure_ascii=False)
    except ProtocolError as exc:
        return f"[error] {exc}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
