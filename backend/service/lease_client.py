from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ServiceLeaseClient:
    """Synchronous frontend lease client with an optional heartbeat thread."""

    def __init__(
        self,
        backend_url: str,
        lease_id: str,
        client: str,
        *,
        token: str | None = None,
        ttl_seconds: int = 20,
        heartbeat_seconds: float = 5.0,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.lease_id = lease_id
        self.client = client
        self.token = token
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = max(0.25, heartbeat_seconds)
        self._active = False
        self._last_error = ""
        self._last_heartbeat = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def acquire(self) -> dict[str, Any]:
        result = _request(
            self.backend_url,
            "POST",
            "/v1/service-leases",
            {
                "lease_id": self.lease_id,
                "client": self.client,
                "ttl_seconds": self.ttl_seconds,
            },
            token=self.token,
        )
        with self._lock:
            self._active = True
            self._last_error = ""
            self._last_heartbeat = time.monotonic()
        return result

    def heartbeat(self) -> dict[str, Any]:
        try:
            result = _request(
                self.backend_url,
                "PUT",
                self._lease_path,
                {"ttl_seconds": self.ttl_seconds},
                token=self.token,
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            result = self.acquire()
        with self._lock:
            self._active = True
            self._last_error = ""
            self._last_heartbeat = time.monotonic()
        return result

    def release(self) -> dict[str, Any]:
        try:
            result = _request(
                self.backend_url,
                "DELETE",
                self._lease_path,
                token=self.token,
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            result = {"ok": True, "released": False, "lease_id": self.lease_id}
        with self._lock:
            self._active = False
            self._last_error = ""
        return result

    def start(self) -> None:
        self.acquire()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                daemon=True,
                name=f"dmri-{self.client}-service-lease",
            )
            self._thread.start()

    def stop(self, *, release: bool = True) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.heartbeat_seconds + 0.5))
        if release:
            try:
                self.release()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                with self._lock:
                    self._active = False
                    self._last_error = str(exc)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "lease_id": self.lease_id,
                "client": self.client,
                "active": self._active,
                "last_error": self._last_error,
                "last_heartbeat": self._last_heartbeat,
            }

    @property
    def _lease_path(self) -> str:
        return f"/v1/service-leases/{urllib.parse.quote(self.lease_id, safe='')}"

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_seconds):
            try:
                self.heartbeat()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                with self._lock:
                    self._last_error = str(exc)
                    self._active = False


def _request(
    backend_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{backend_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    resolved_token = (
        os.environ.get("DMRI_SERVICE_TOKEN", "").strip()
        if token is None
        else token.strip()
    )
    if resolved_token:
        request.add_header("Authorization", f"Bearer {resolved_token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=2.0) as response:
        return json.load(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage a dMRI frontend service lease")
    parser.add_argument("action", choices=("acquire", "heartbeat", "release"))
    parser.add_argument("backend_url")
    parser.add_argument("lease_id")
    parser.add_argument("--client", default="desktop")
    parser.add_argument("--ttl", type=int, default=20)
    args = parser.parse_args(argv)

    client = ServiceLeaseClient(
        args.backend_url,
        args.lease_id,
        args.client,
        ttl_seconds=args.ttl,
    )
    try:
        if args.action == "acquire":
            result = client.acquire()
        elif args.action == "heartbeat":
            result = client.heartbeat()
        else:
            result = client.release()
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        print(f"service lease {args.action} failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
