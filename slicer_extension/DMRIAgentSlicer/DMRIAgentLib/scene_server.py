from __future__ import annotations

import hmac
import inspect
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse


_READ_ONLY_SCENE_PATHS = frozenset(
    {
        b"/mrml/names",
        b"/mrml/ids",
        b"/mrml/properties",
        b"/screenshot",
        b"/slice",
        b"/threeD",
    }
)


class RevocableSceneAuthorization:
    """One session-scoped credential that can be invalidated before shutdown.

    Slicer 5.12 keeps accepted request communicators alive when its listening
    socket is stopped.  Those communicators retain their request handler, so
    the authorization state must be shared with every handler and explicitly
    revoked instead of relying on socket shutdown alone.
    """

    def __init__(self, auth_token: str) -> None:
        token = str(auth_token or "").strip()
        if len(token) < 32 or not token.isascii() or not token.isalnum():
            raise ValueError("Slicer scene authorization token is invalid")
        self._token = token.encode("ascii")
        self._active = True
        self._lock = threading.Lock()

    def permits(self, supplied_token: bytes) -> bool:
        with self._lock:
            return self._active and hmac.compare_digest(supplied_token, self._token)

    def revoke(self) -> None:
        with self._lock:
            self._active = False
            self._token = b""


class AuthenticatedSlicerRequestHandler:
    """Expose only the Agent's authenticated, minimal Slicer HTTP surface."""

    def __init__(
        self,
        auth_token: str | RevocableSceneAuthorization,
        delegate: Any,
    ) -> None:
        self._authorization = (
            auth_token
            if isinstance(auth_token, RevocableSceneAuthorization)
            else RevocableSceneAuthorization(auth_token)
        )
        self._delegate = delegate

    def revoke(self) -> None:
        self._authorization.revoke()

    def _authorized_suffix(self, method: str, uri: bytes, request_body: bytes) -> bytes | None:
        try:
            parsed = urlparse(uri)
            path = parsed.path
        except (TypeError, ValueError):
            return None
        marker = b"/slicer/"
        if not path.startswith(marker):
            return None
        token_end = path.find(b"/", len(marker))
        if token_end < 0:
            return None
        supplied_token = path[len(marker) : token_end]
        if not self._authorization.permits(supplied_token):
            return None
        suffix = path[token_end:]
        if suffix == b"/exec":
            return suffix if method == "POST" and bool(request_body) else None
        if suffix in _READ_ONLY_SCENE_PATHS:
            return suffix if method == "GET" else None
        return None

    def canHandleRequest(
        self,
        method: str,
        uri: bytes,
        requestBody: bytes,
        **_kwargs,
    ) -> float:
        return 1.0 if self._authorized_suffix(method, uri, requestBody) is not None else 0.0

    def handleRequest(
        self,
        method: str,
        uri: bytes,
        requestBody: bytes,
    ) -> tuple[bytes, bytes]:
        suffix = self._authorized_suffix(method, uri, requestBody)
        if suffix is None:
            # The WebServer normally turns a zero-confidence request into 404.
            # Keep a defense-in-depth check in case it invokes us directly.
            raise RuntimeError("Slicer scene request was not authorized")
        parsed = urlparse(uri)
        delegated_uri = urlunparse(parsed._replace(path=b"/slicer" + suffix))
        return self._delegate.handleRequest(method, delegated_uri, requestBody)


@dataclass(frozen=True)
class SceneServerSnapshot:
    active: bool
    port: int
    message: str


def scene_port_from_environment(environ, default: int = 2016) -> int:
    raw = environ.get("DMRI_SLICER_SCENE_PORT") or default
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def scene_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


class SceneServerController:
    """Own the loopback Slicer scene server for one visible Agent session."""

    def __init__(
        self,
        *,
        port: int,
        server_factory: Optional[
            Callable[[int, RevocableSceneAuthorization], Any]
        ] = None,
        port_checker: Callable[[int], bool] = scene_port_available,
    ) -> None:
        self.port = port
        self._factory = server_factory or self._slicer_server_factory
        self._port_checker = port_checker
        self._server: Any = None
        self._authorization: Optional[RevocableSceneAuthorization] = None
        self._message = "Local scene access is inactive."

    def snapshot(self) -> SceneServerSnapshot:
        return SceneServerSnapshot(
            active=self._server is not None,
            port=self.port,
            message=self._message,
        )

    def start(self, *, auth_token: str) -> SceneServerSnapshot:
        token = str(auth_token or "").strip()
        if len(token) < 32 or not token.isascii() or not token.isalnum():
            raise RuntimeError("Private Slicer scene authorization is unavailable.")
        if self._server is not None:
            if self._authorization is None or not self._authorization.permits(
                token.encode("ascii")
            ):
                raise RuntimeError("Slicer scene authorization changed while the server was active.")
            return self.snapshot()
        if not self._port_checker(self.port):
            raise RuntimeError(
                f"Local scene port 127.0.0.1:{self.port} is already in use. "
                "Stop the other Slicer Agent session or choose a different scene port."
            )
        authorization = RevocableSceneAuthorization(token)
        server = None
        try:
            server = self._factory(self.port, authorization)
            server.start()
        except Exception:
            authorization.revoke()
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass
            raise
        self._server = server
        self._authorization = authorization
        self._message = f"Local scene access active on 127.0.0.1:{self.port}."
        return self.snapshot()

    def stop(self) -> SceneServerSnapshot:
        server = self._server
        authorization = self._authorization
        # Revoke first.  Slicer 5.12 closes only the listening socket and may
        # leave an already accepted communicator holding the old handler.
        if authorization is not None:
            authorization.revoke()
        self._server = None
        self._authorization = None
        if server is not None:
            try:
                server.stop()
            finally:
                self._close_request_communicators(server)
                self._message = "Local scene access stopped."
        return self.snapshot()

    @staticmethod
    def _close_request_communicators(server: Any) -> None:
        """Best-effort close of connections retained by Slicer 5.12."""

        communicators = getattr(server, "requestCommunicators", None)
        if not isinstance(communicators, dict):
            return
        for communicator in list(communicators.values()):
            for notifier_name in ("readNotifier", "writeNotifier"):
                notifier = getattr(communicator, notifier_name, None)
                if notifier is not None:
                    try:
                        notifier.setEnabled(False)
                    except Exception:
                        pass
            connection = getattr(communicator, "connectionSocket", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        communicators.clear()

    @staticmethod
    def _slicer_server_factory(
        port: int,
        authorization: RevocableSceneAuthorization,
    ):
        from WebServer import SlicerHTTPServer
        from WebServerLib import SlicerRequestHandler

        options = {"enableExec": True}
        parameters = inspect.signature(SlicerRequestHandler).parameters
        if "logMessage" in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            options["logMessage"] = lambda *_args: None
        delegate = SlicerRequestHandler(**options)
        # Older Slicer versions expose logging as a method instead of accepting
        # the constructor argument. Keep scene credentials out of those logs.
        delegate.logMessage = lambda *_args: None
        authenticated_handler = AuthenticatedSlicerRequestHandler(authorization, delegate)
        return SlicerHTTPServer(
            server_address=("127.0.0.1", port),
            requestHandlers=[authenticated_handler],
            docroot=".",
            logMessage=lambda *_args: None,
            enableCORS=False,
        )
