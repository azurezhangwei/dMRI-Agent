from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_CONFIG = PROJECT_ROOT / "config" / "models.json"


def read_project_version() -> str:
    version_file = PROJECT_ROOT / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0+unknown"
    return version or "0.0.0+unknown"


PROJECT_VERSION = read_project_version()


def resolve_project_path(value: str | os.PathLike[str] | None, default: Path) -> Path:
    if value is None or not str(value).strip():
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()
