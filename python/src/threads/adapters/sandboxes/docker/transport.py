"""Where the Docker Engine API lives and how threads talks to it: the socket, resolved on the
host without opening a connection (the `Sandbox.setup` contract), and the httpx transport over
it, fenced at its send point (adapters/sandboxes/httpx_fence.py).

`MakeTransport` is the seam a test injects (spec/api.json `docker(transport)`); the fence wraps
whatever it returns, so a mocked Engine is checked exactly like the real one.
"""

import os
from collections.abc import Callable
from pathlib import Path

import httpx

from threads.agents.config import ConfigError

type MakeTransport = Callable[[], httpx.AsyncBaseTransport]
"""Makes the httpx transport every Engine API request goes through."""

DOCKER_HOST = "DOCKER_HOST"
SOCKETS = ("/var/run/docker.sock", "~/.docker/run/docker.sock")
"""Looked for in order when DOCKER_HOST is unset: the daemon, then Docker Desktop on macOS."""
API_VERSION = "v1.44"
"""Pinned in every request path: below it the daemon answers a negotiation error."""
BASE_URL = f"http://docker/{API_VERSION}"
"""The socket has no authority; httpx needs one to build a URL."""

UNREACHABLE = (
    f"Docker isn't running (looked for {', '.join(SOCKETS)}): "
    "start Docker, set DOCKER_HOST, or pass another sandbox (devSandbox(), e2b())"
)
NEEDS_1_44 = "Docker Engine API 1.44 or newer is needed (Docker 25+)"


def socket_exists(path: str) -> bool:
    return Path(path).expanduser().exists()


def socket_path() -> str:
    """The Engine socket, resolved without connecting. DOCKER_HOST wins and must be a unix
    socket; otherwise the first candidate that exists."""
    host = os.environ.get(DOCKER_HOST)
    if host:
        if not host.startswith("unix://"):
            raise ConfigError(
                "invalid_config", f"docker: DOCKER_HOST must be a unix:// socket, not {host}"
            )
        return host.removeprefix("unix://")
    found = next((p for p in SOCKETS if socket_exists(p)), None)
    if found is None:
        raise ConfigError("docker_unreachable", UNREACHABLE)
    return str(Path(found).expanduser())


def uds_transport(socket: str) -> httpx.AsyncBaseTransport:
    """The real transport: one attempt over the unix socket, no redirects followed."""
    return httpx.AsyncHTTPTransport(uds=socket, retries=0)
