from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from backend.model_config import apply_active_profile_env
from backend.runtime_config import apply_runtime_defaults


ROOT_DIR = Path(__file__).resolve().parents[1]


def resolve_env_file() -> Path:
    explicit = os.environ.get("MRI_AGENT_ENV_FILE") or os.environ.get("AGENT_ENV_FILE")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return path
    return ROOT_DIR / ".env"


def load_env() -> Path:
    env_path = resolve_env_file()
    if env_path.exists():
        load_dotenv(env_path)
    apply_runtime_defaults()
    apply_active_profile_env()
    return env_path
