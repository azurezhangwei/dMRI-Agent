from __future__ import annotations

from threading import Lock


_cancel_generation = 0
_lock = Lock()


def cancel_generation() -> int:
    with _lock:
        return _cancel_generation


def request_cancel() -> int:
    global _cancel_generation
    with _lock:
        _cancel_generation += 1
        return _cancel_generation
