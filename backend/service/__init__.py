"""Transport-neutral session service for dMRI-Agent frontends.

Keep package import dependency-free: 3D Slicer imports the standard-library
runtime broker from this package using its bundled Python, while the full API
dependencies live in the installed Agent virtual environment.
"""

from __future__ import annotations

__all__ = ["SessionManager", "create_app"]


def __getattr__(name: str):
    if name == "create_app":
        from backend.service.api import create_app

        return create_app
    if name == "SessionManager":
        from backend.service.session_manager import SessionManager

        return SessionManager
    raise AttributeError(name)
