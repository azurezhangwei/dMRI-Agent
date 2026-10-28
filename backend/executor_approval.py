from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from backend.runtime_state import atomic_write_text


EXECUTOR_APPROVAL_DIR_ENV = "DMRI_EXECUTOR_APPROVAL_DIR"
ExecutorApprovalHandler = Callable[[Mapping[str, Any]], bool]
_HANDLER_STATE = threading.local()


@contextmanager
def executor_approval_handler(handler: ExecutorApprovalHandler) -> Iterator[None]:
    previous = getattr(_HANDLER_STATE, "handler", None)
    _HANDLER_STATE.handler = handler
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_HANDLER_STATE, "handler")
            except AttributeError:
                pass
        else:
            _HANDLER_STATE.handler = previous


def current_executor_approval_handler() -> ExecutorApprovalHandler | None:
    handler = getattr(_HANDLER_STATE, "handler", None)
    return handler if callable(handler) else None


class ExecutorApprovalBroker:
    """Bridge dangerous child-executor calls back to the parent Agent session."""

    def __init__(self, handler: ExecutorApprovalHandler | None) -> None:
        self.handler = handler
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self._handled: set[str] = set()

    def __enter__(self) -> "ExecutorApprovalBroker":
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="dmri-agent-executor-approval-"
        )
        self.root = Path(self._temporary_directory.name).resolve()
        (self.root / "requests").mkdir(mode=0o700)
        (self.root / "responses").mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
        self._temporary_directory = None
        self.root = None

    @property
    def environment_value(self) -> str:
        if self.root is None:
            raise RuntimeError("executor approval broker is not active")
        return str(self.root)

    def poll(self) -> int:
        if self.root is None:
            return 0
        processed = 0
        for request_path in sorted((self.root / "requests").glob("*.json")):
            request_id = request_path.stem
            if request_id in self._handled:
                continue
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(request, dict) or request.get("request_id") != request_id:
                continue
            self._handled.add(request_id)
            approved = False
            reason = "parent approval handler unavailable"
            if self.handler is not None:
                try:
                    approved = bool(self.handler(request))
                    reason = "approved" if approved else "rejected"
                except Exception as exc:  # noqa: BLE001
                    reason = f"approval handler failed: {exc}"
            response_path = self.root / "responses" / f"{request_id}.json"
            atomic_write_text(
                response_path,
                json.dumps(
                    {
                        "request_id": request_id,
                        "approved": approved,
                        "reason": reason,
                        "decided_at": time.time(),
                    },
                    ensure_ascii=True,
                )
                + "\n",
                mode=0o600,
            )
            processed += 1
        return processed


def request_executor_tool_approval(
    call_id: str,
    name: str,
    args: Mapping[str, Any],
    *,
    channel_root: str | Path | None = None,
) -> bool:
    """Ask the parent Agent process to approve one dangerous executor tool call."""

    configured = channel_root or os.environ.get(EXECUTOR_APPROVAL_DIR_ENV)
    if not configured:
        # Standalone executor runs retain their historical behavior. Production
        # execute_stage always provides a broker and therefore cannot bypass it.
        return True
    root = Path(configured).expanduser().resolve()
    requests_root = root / "requests"
    responses_root = root / "responses"
    if not requests_root.is_dir() or not responses_root.is_dir():
        return False

    request_id = uuid.uuid4().hex
    request_path = requests_root / f"{request_id}.json"
    response_path = responses_root / f"{request_id}.json"
    atomic_write_text(
        request_path,
        json.dumps(
            {
                "request_id": request_id,
                "executor_call_id": str(call_id),
                "name": str(name),
                "args": dict(args),
                "requested_at": time.time(),
            },
            ensure_ascii=True,
        )
        + "\n",
        mode=0o600,
    )

    raw_timeout = os.environ.get("EXECUTOR_TOOL_APPROVAL_TIMEOUT", "3600")
    try:
        timeout_seconds = max(1.0, float(raw_timeout))
    except ValueError:
        timeout_seconds = 3600.0
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if not root.is_dir():
                return False
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                time.sleep(0.1)
                continue
            except (OSError, json.JSONDecodeError):
                time.sleep(0.1)
                continue
            if isinstance(payload, dict) and payload.get("request_id") == request_id:
                return bool(payload.get("approved"))
        return False
    finally:
        try:
            request_path.unlink()
        except FileNotFoundError:
            pass
        try:
            response_path.unlink()
        except FileNotFoundError:
            pass
