from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.rollout_log import project_log_key


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
LEGACY_SESSION_RE = re.compile(r"^session_(?:\d{8}_\d{6}|[a-f0-9-]{36})\.jsonl$")


class HistoryError(RuntimeError):
    pass


class HistoryNotFound(HistoryError):
    pass


@dataclass(frozen=True)
class HistoryEntry:
    session_id: str
    path: Path
    session_dir: Path | None
    model: str
    cwd: str
    mtime: float
    size: int
    lines: int
    layout: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "mtime": self.mtime * 1000,
            "size": self.size,
            "lines": self.lines,
            "model": self.model,
            "cwd": self.cwd,
            "layout": self.layout,
        }


class HistoryStore:
    def __init__(self, log_root: Path) -> None:
        self.log_root = log_root.expanduser().resolve()
        self.max_read_bytes = int(os.environ.get("DMRI_HISTORY_MAX_BYTES", str(32 * 1024 * 1024)))

    def list(self, workspace: str | os.PathLike[str]) -> list[HistoryEntry]:
        entries: dict[str, HistoryEntry] = {}
        project_dir = self._project_dir(workspace)
        if project_dir.is_dir():
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir() or not session_dir.name.startswith("session_"):
                    continue
                session_id = session_dir.name[len("session_") :]
                if not SESSION_ID_RE.fullmatch(session_id):
                    continue
                path = session_dir / "session.jsonl"
                if path.is_file():
                    entry = self._entry(session_id, path, session_dir, "project")
                    entries[entry.session_id] = entry

        if self.log_root.is_dir():
            for path in self.log_root.iterdir():
                if path.is_file() and LEGACY_SESSION_RE.fullmatch(path.name):
                    entry = self._entry(path.name, path, None, "legacy")
                    entries.setdefault(entry.session_id, entry)
        return sorted(entries.values(), key=lambda item: item.mtime, reverse=True)

    def read(self, workspace: str | os.PathLike[str], session_id: str) -> tuple[HistoryEntry, str]:
        entry = self._locate(workspace, session_id)
        if entry.size > self.max_read_bytes:
            raise HistoryError(
                f"session log is too large ({entry.size} bytes; limit {self.max_read_bytes})"
            )
        return entry, entry.path.read_text(encoding="utf-8", errors="replace")

    def resume_messages(
        self,
        workspace: str | os.PathLike[str],
        session_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, str]]:
        _, content = self.read(workspace, session_id)
        messages: list[dict[str, str]] = []
        for line in content.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            text = str(record.get("text") or "").strip()
            if not text:
                continue
            if kind == "human" and record.get("source") != "planner_instruction" and record.get("role") != "executor":
                messages.append({"role": "user", "content": text})
            elif kind == "llm":
                messages.append({"role": "assistant", "content": text})
        return messages[-limit:] if limit > 0 else messages

    def delete(self, workspace: str | os.PathLike[str], session_id: str) -> None:
        entry = self._locate(workspace, session_id)
        if entry.session_dir is not None:
            shutil.rmtree(entry.session_dir)
        else:
            entry.path.unlink()

    def _project_dir(self, workspace: str | os.PathLike[str]) -> Path:
        resolved_workspace = Path(workspace).expanduser().resolve()
        return self.log_root / "projects" / project_log_key(str(resolved_workspace))

    def _locate(self, workspace: str | os.PathLike[str], session_id: str) -> HistoryEntry:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise HistoryNotFound(f"invalid or unknown session id: {session_id}")
        for entry in self.list(workspace):
            if entry.session_id == session_id:
                return entry
        raise HistoryNotFound(f"history session not found: {session_id}")

    def _entry(
        self,
        session_id: str,
        path: Path,
        session_dir: Path | None,
        layout: str,
    ) -> HistoryEntry:
        stat = path.stat()
        manifest: dict[str, Any] = {}
        if session_dir is not None:
            try:
                payload = json.loads((session_dir / "session_manifest.json").read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    manifest = payload
            except (OSError, json.JSONDecodeError):
                pass
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = sum(1 for line in handle if line.strip())
        return HistoryEntry(
            session_id=session_id,
            path=path,
            session_dir=session_dir,
            model=str(manifest.get("model") or ""),
            cwd=str(manifest.get("cwd") or ""),
            mtime=stat.st_mtime,
            size=stat.st_size,
            lines=lines,
            layout=layout,
        )
