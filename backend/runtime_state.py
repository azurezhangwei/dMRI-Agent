from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from backend.platform import state_home as platform_state_home, secure_path


SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|secret|password)\b\s*[:=]\s*[^\s]{8,}"
    ),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
)


def state_home() -> Path:
    return platform_state_home().resolve()


def managed_protocols_home() -> Path:
    configured = (os.environ.get("DMRI_PROTOCOLS_HOME") or "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (state_home() / "protocols").resolve()
    )


def memory_db_path() -> Path:
    configured = (os.environ.get("DMRI_MEMORY_DB") or "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (state_home() / "memory.sqlite3").resolve()
    )


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        secure_path(temp_path, mode)
        temp_path.replace(path)
        secure_path(path, mode)
    finally:
        if temp_path.exists():
            temp_path.unlink()
