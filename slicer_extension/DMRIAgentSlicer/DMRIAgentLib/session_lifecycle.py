from __future__ import annotations

import logging

from .service_client import ServiceError


def detach_session(client) -> None:
    """Detach one frontend without terminating the shared backend session."""
    if client is not None:
        client.stop_stream()


def stop_session(client, session_id: str) -> None:
    """Explicitly terminate a user-selected Agent session, then detach."""
    if client is None or not session_id:
        return
    try:
        client.shutdown(session_id)
    except ServiceError as exc:
        logging.warning("Agent service shutdown failed: %s", exc)
    finally:
        client.stop_stream()
