"""`docker()`: a sandbox in a local Docker container, over the Engine API on its unix socket.

It is keyless: no registry credentials are ever sent, and nothing of the host's environment
reaches a container (invariant 4). Each container runs the image with the threads supervisor
injected into a named volume, its root filesystem read-only, no network unless asked, every
capability dropped but the four the supervisor needs, and commands as uid 1000 in a /workspace
volume. Snapshots are core's host trees, so nothing is ever committed to an image.

The sandbox, session and exec logic is the remote kit's; this adapter is its driver (driver.py)
over one event loop's client (engine.py).
"""

import re
from typing import Final

import httpx

from threads.adapters.loop_resources import LoopResources
from threads.adapters.sandboxes.docker import terminate
from threads.adapters.sandboxes.docker.create import Settings
from threads.adapters.sandboxes.docker.driver import DockerDriver
from threads.adapters.sandboxes.docker.engine import Engine, close_engine, open_engine
from threads.adapters.sandboxes.docker.transport import MakeTransport, socket_path, uds_transport
from threads.agents.config import ConfigError
from threads.sandbox.remote.sandbox import RemoteInfo, RemoteSandbox

IMAGE: Final = (
    "node:22-bookworm@sha256:363e1587494626837fa7f9a23bdb453d13b0ff3c67c705c2805cfc69c2d2fad7"
)
"""A verified multi-arch index (amd64 + arm64) carrying bash, git, python3 and tar."""
MEMORY_MIN_MB: Final = 64
_PROVIDER: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


class DockerSandbox(RemoteSandbox):
    """spec/api.json `Sandbox` on a local Docker daemon (module docstring)."""

    def __init__(  # noqa: PLR0913 - the factory's options
        self,
        *,
        image: str = IMAGE,
        allow_internet: bool = False,
        cpus: float | None = None,
        memory_mb: int | None = None,
        transport: MakeTransport | None = None,
        name: str = "docker",
        poll_s: float = 0.2,
        wait_s: float = 10.0,
    ) -> None:
        _checked(cpus, memory_mb, name)
        self._make: MakeTransport = transport or self._uds
        self._resolved: str | None = None
        self._engines = LoopResources(name, close_engine)
        settings = Settings(image, allow_internet, cpus, memory_mb)
        driver = DockerDriver(
            self._loop_engine, settings, terminate.Pacing(poll_s, wait_s), self._socket_or_blank
        )
        super().__init__(driver, RemoteInfo(name, "unenforced" if allow_internet else "enforced"))

    async def setup(self) -> None:
        """Resolves the socket on the host and opens no connection (the `Sandbox.setup`
        contract). The client is made on first use, in the run and on its event loop, and
        closed when nothing holds that loop any more."""
        self._socket()

    def _socket(self) -> str:
        if self._resolved is None:
            self._resolved = socket_path()
        return self._resolved

    def _socket_or_blank(self) -> str:
        """For a connect failure's message: empty when the socket can't be named."""
        try:
            return self._socket()
        except ConfigError:
            return ""

    def _uds(self) -> httpx.AsyncBaseTransport:
        return uds_transport(self._socket())

    def _loop_engine(self) -> Engine:
        return self._engines.get(lambda: open_engine(self._make))


def _checked(cpus: float | None, memory_mb: int | None, name: str) -> None:
    """Every limit Docker can't express is refused before anything is created."""
    if cpus is not None and not cpus > 0:
        raise ConfigError("invalid_config", f"docker: cpus must be greater than 0, not {cpus}")
    if memory_mb is not None and (type(memory_mb) is not int or memory_mb < MEMORY_MIN_MB):
        raise ConfigError(
            "invalid_config",
            f"docker: memory_mb must be an integer of at least {MEMORY_MIN_MB}, not {memory_mb}",
        )
    if _PROVIDER.fullmatch(name) is None:
        raise ConfigError("invalid_config", f"docker: provider name {name!r}: [a-z][a-z0-9_]*")


def docker(  # noqa: PLR0913 - the provider's options
    *,
    image: str = IMAGE,
    allow_internet: bool = False,
    cpus: float | None = None,
    memory_mb: int | None = None,
    transport: MakeTransport | None = None,
    name: str = "docker",
) -> DockerSandbox:
    """A local Docker sandbox provider (the `docker()` of spec/api.json conventions.adapters).
    It needs no key: `setup` resolves the Engine socket from DOCKER_HOST, `/var/run/docker.sock`
    or `~/.docker/run/docker.sock`, and connects to none of them."""
    return DockerSandbox(
        image=image,
        allow_internet=allow_internet,
        cpus=cpus,
        memory_mb=memory_mb,
        transport=transport,
        name=name,
    )
