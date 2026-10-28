from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ServiceLease:
    lease_id: str
    client: str
    expires_at: float


class ServiceLeaseManager:
    """Tracks frontend leases for a launcher-managed backend process.

    A backend started outside the desktop launchers is deliberately unmanaged:
    clients may still register leases, but their departure never stops it.
    """

    def __init__(
        self,
        *,
        managed: bool = False,
        startup_grace_seconds: float = 60.0,
        empty_grace_seconds: float = 1.5,
        poll_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        bootstrap_lease_id: str = "",
        bootstrap_lease_client: str = "launcher",
        bootstrap_lease_ttl_seconds: float = 60.0,
    ) -> None:
        self.managed = managed
        self.startup_grace_seconds = max(0.0, startup_grace_seconds)
        self.empty_grace_seconds = max(0.0, empty_grace_seconds)
        self.poll_seconds = max(0.01, poll_seconds)
        self._clock = clock
        self._leases: dict[str, ServiceLease] = {}
        self._lock = asyncio.Lock()
        self._started_at = self._clock()
        self._empty_since: float | None = self._started_at
        self._ever_acquired = False
        self._shutdown_claimed = False
        if bootstrap_lease_id:
            now = self._clock()
            self._leases[bootstrap_lease_id] = ServiceLease(
                lease_id=bootstrap_lease_id,
                client=bootstrap_lease_client,
                expires_at=now + max(5.0, bootstrap_lease_ttl_seconds),
            )
            self._ever_acquired = True
            self._empty_since = None

    @classmethod
    def from_environment(cls) -> "ServiceLeaseManager":
        return cls(
            managed=os.environ.get("DMRI_MANAGED_SERVICE", "").strip() == "1",
            startup_grace_seconds=_env_float(
                "DMRI_SERVICE_LEASE_STARTUP_GRACE_SECONDS", 60.0
            ),
            empty_grace_seconds=_env_float(
                "DMRI_SERVICE_LEASE_EMPTY_GRACE_SECONDS", 1.5
            ),
            poll_seconds=_env_float("DMRI_SERVICE_LEASE_POLL_SECONDS", 0.25),
            bootstrap_lease_id=os.environ.get(
                "DMRI_SERVICE_BOOTSTRAP_LEASE_ID", ""
            ).strip(),
            bootstrap_lease_client=os.environ.get(
                "DMRI_SERVICE_BOOTSTRAP_LEASE_CLIENT", "launcher"
            ).strip()
            or "launcher",
            bootstrap_lease_ttl_seconds=_env_float(
                "DMRI_SERVICE_BOOTSTRAP_LEASE_TTL_SECONDS", 60.0
            ),
        )

    def _prune_locked(self, now: float) -> None:
        expired = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.expires_at <= now
        ]
        for lease_id in expired:
            self._leases.pop(lease_id, None)
        if self._leases:
            self._empty_since = None
        elif self._empty_since is None:
            self._empty_since = now

    async def acquire(
        self,
        lease_id: str,
        client: str,
        ttl_seconds: float,
    ) -> dict[str, object]:
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            self._leases[lease_id] = ServiceLease(
                lease_id=lease_id,
                client=client,
                expires_at=now + ttl_seconds,
            )
            self._ever_acquired = True
            self._empty_since = None
            return self._status_locked(now)

    async def renew(self, lease_id: str, ttl_seconds: float) -> dict[str, object] | None:
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            lease = self._leases.get(lease_id)
            if lease is None:
                return None
            self._leases[lease_id] = ServiceLease(
                lease_id=lease.lease_id,
                client=lease.client,
                expires_at=now + ttl_seconds,
            )
            return self._status_locked(now)

    async def release(self, lease_id: str) -> tuple[bool, dict[str, object]]:
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            released = self._leases.pop(lease_id, None) is not None
            if not self._leases and self._empty_since is None:
                self._empty_since = now
            return released, self._status_locked(now)

    async def status(self) -> dict[str, object]:
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            return self._status_locked(now)

    def _status_locked(self, now: float) -> dict[str, object]:
        return {
            "managed": self.managed,
            "active_leases": len(self._leases),
            "expires_in": {
                lease_id: max(0.0, lease.expires_at - now)
                for lease_id, lease in self._leases.items()
            },
        }

    async def _claim_shutdown_if_due(self) -> bool:
        if not self.managed:
            return False
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            if self._leases or self._shutdown_claimed or self._empty_since is None:
                return False
            grace = (
                self.empty_grace_seconds
                if self._ever_acquired
                else self.startup_grace_seconds
            )
            if now - self._empty_since < grace:
                return False
            self._shutdown_claimed = True
            return True

    async def watchdog(
        self,
        shutdown_callback: Callable[[], None] | None = None,
    ) -> None:
        callback = shutdown_callback or _terminate_current_process
        while True:
            await asyncio.sleep(self.poll_seconds)
            if await self._claim_shutdown_if_due():
                callback()
                return


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _terminate_current_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)
