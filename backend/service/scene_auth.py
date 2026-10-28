from __future__ import annotations

import hashlib
import hmac


_SCENE_AUTH_CONTEXT = b"dmri-agent-slicer-scene-v1"


def derive_scene_auth_token(service_token: str, session_id: str) -> str:
    """Derive one high-entropy scene credential for one Agent session.

    The private service token is already distributed through the owner-only
    runtime descriptor.  Deriving instead of persisting another secret keeps
    the scene credential out of descriptors, logs, API payloads, and model
    messages while ensuring that every Agent session gets a different token.
    """

    private_token = str(service_token or "").strip()
    resolved_session_id = str(session_id or "").strip()
    if not private_token:
        raise ValueError("private service authorization is unavailable")
    if not resolved_session_id:
        raise ValueError("Agent session identity is unavailable")
    return hmac.new(
        private_token.encode("utf-8"),
        _SCENE_AUTH_CONTEXT + b"\0" + resolved_session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
