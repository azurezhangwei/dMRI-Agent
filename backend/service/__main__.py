from __future__ import annotations

import os
from types import FrameType

import uvicorn

from backend.service.api import app


class GracefulAgentServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        app.state.session_manager.begin_shutdown()
        super().handle_exit(sig, frame)


def main() -> None:
    config = uvicorn.Config(
        app,
        host=os.environ.get("DMRI_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("DMRI_SERVICE_PORT", "8765")),
        timeout_graceful_shutdown=int(
            os.environ.get("DMRI_GRACEFUL_SHUTDOWN_SECONDS", "5")
        ),
    )
    server = GracefulAgentServer(config)
    app.state.uvicorn_server = server
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
