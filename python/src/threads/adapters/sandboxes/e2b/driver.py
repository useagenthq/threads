"""The remote kit's driver over E2B (sandbox/remote/driver.py): the control plane for create,
lookup and kill, and each sandbox's envd for exec, files and the process kill, every call on
one event loop's fenced transports (plane.py)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Self

import httpx
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from pydantic import ValidationError

from threads.adapters.sandboxes.e2b.control import ApiError, MalformedError
from threads.adapters.sandboxes.e2b.envd import Envd
from threads.adapters.sandboxes.e2b.plane import Plane
from threads.adapters.sandboxes.e2b.wire import Sandbox as Described
from threads.adapters.sandboxes.fence import Classify
from threads.adapters.sandboxes.streams import StreamLostError
from threads.loop.model import Found, LookupUnknown, NotFoundNonfinal
from threads.sandbox.protocol import ExecOutput, SandboxError
from threads.sandbox.remote.driver import Capture, NonfinalLookup, Unconfirmed, Unmade

_UNAVAILABLE = (
    ApiError,
    MalformedError,
    ValidationError,
    httpx.TransportError,
    StreamLostError,
    ConnectError,
)
_NOT_FOUND = 404


def classify(error: Exception) -> SandboxError | None:
    """The expected failures of an E2B call; anything else is a bug and raises."""
    deadline = isinstance(error, ConnectError) and error.code == Code.DEADLINE_EXCEEDED
    if deadline or isinstance(error, httpx.TimeoutException):
        return SandboxError("timeout", str(error))
    if isinstance(error, _UNAVAILABLE):
        return SandboxError("unavailable", str(error))
    return None


@dataclass(frozen=True, slots=True)
class Settings:
    template: str
    timeout_s: int
    """E2B's sandbox timeout: it kills the sandbox this long after create."""
    internet: bool


class E2BDriver:
    def __init__(self, plane: Callable[[], Plane], settings: Settings) -> None:
        self._plane, self._settings = plane, settings
        # A create that passed its fence may still be in flight when the query runs.
        self.lookup: NonfinalLookup = NonfinalLookup(self._find)
        # envd kills the process it started, not what that process detached.
        self.termination: Unconfirmed = Unconfirmed(self._signal)
        self.capture: Capture | None = None

    def bound(self) -> Self:
        plane = self._plane()
        return type(self)(lambda: plane, self._settings)

    @property
    def classify(self) -> Classify:
        return classify

    async def create(self, operation_key: str, snapshot: str | None) -> str | Unmade:
        """From the template: with no capture, the kit never restores (`snapshot` is None)."""
        s = self._settings
        control = self._plane().control
        made = await control.create(
            s.template, operation_key, timeout_s=s.timeout_s, internet=s.internet
        )
        if made is None:
            return Unmade("snapshot_missing", f"E2B has no template {s.template}")
        self._open(made)
        return made.sandbox_id

    async def exists(self, sandbox_id: str) -> bool:
        described = await self._plane().control.describe(sandbox_id)
        if described is not None:
            self._open(described)
        return described is not None

    async def kill(self, sandbox_id: str) -> Literal["killed", "already_gone"]:
        """Kills the sandbox, then closes its envd clients."""
        plane = self._plane()
        killed = await plane.control.kill(sandbox_id)
        envd = plane.envds.get(sandbox_id)
        if envd is not None:
            await envd.aclose()
        return "killed" if killed else "already_gone"

    async def run(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        process_key: str | None,
    ) -> ExecOutput:
        return await (await self._envd(sandbox_id)).start(argv, env, cwd, process_key)

    async def write(self, sandbox_id: str, path: str, data: bytes) -> None:
        await (await self._envd(sandbox_id)).upload(path, data)

    async def read(self, sandbox_id: str, path: str) -> bytes:
        return await (await self._envd(sandbox_id)).download(path)

    async def _find(self, operation_key: str) -> Found[str] | NotFoundNonfinal | LookupUnknown:
        match await self._plane().control.find(operation_key):
            case []:
                return NotFoundNonfinal()
            case [ref]:
                return Found(ref)
            case refs:
                return LookupUnknown(f"several sandboxes carry {operation_key}: {refs}")

    async def _signal(self, sandbox_id: str, process_key: str) -> None:
        await (await self._envd(sandbox_id)).signal(process_key)

    async def _envd(self, sandbox_id: str) -> Envd:
        known = self._plane().envds.get(sandbox_id)
        if known is not None:
            return known
        described = await self._plane().control.describe(sandbox_id)
        if described is None:
            raise ApiError(_NOT_FOUND, f"E2B has no sandbox {sandbox_id}".encode())
        return self._open(described)

    def _open(self, described: Described) -> Envd:
        """The sandbox's envd clients on this plane, opened once (they list themselves in
        `plane.envds` until closed)."""
        plane = self._plane()
        known = plane.envds.get(described.sandbox_id)
        if known is not None:
            return known
        domain = described.domain or plane.config.domain
        url = plane.config.get_sandbox_url(described.sandbox_id, domain)
        return Envd(described, url, plane.transports, plane.envds)
