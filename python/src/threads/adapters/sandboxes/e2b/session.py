"""One E2B sandbox (spec/api.json `SandboxSession`): exec, files and terminate through envd,
close through the control plane, every call fenced at its transport."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from pydantic import ValidationError

from threads.adapters.sandboxes import fence, posix
from threads.adapters.sandboxes.e2b.control import ApiError, Control, MalformedError
from threads.adapters.sandboxes.e2b.envd import Envd, FileError
from threads.adapters.sandboxes.streams import StreamLostError
from threads.log import SnapshotData
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    SandboxContext,
    SandboxError,
    SandboxId,
    invalid_path,
    is_refusal,
)

_UNAVAILABLE = (
    ApiError,
    MalformedError,
    ValidationError,
    httpx.TransportError,
    StreamLostError,
    ConnectError,
)


def classify(error: Exception) -> SandboxError | None:
    """The expected failures of an E2B call; anything else is a bug and raises."""
    if isinstance(error, FileError):
        return error.error
    deadline = isinstance(error, ConnectError) and error.code == Code.DEADLINE_EXCEEDED
    if deadline or isinstance(error, httpx.TimeoutException):
        return SandboxError("timeout", str(error))
    if isinstance(error, _UNAVAILABLE):
        return SandboxError("unavailable", str(error))
    return None


async def call[T](
    context: SandboxContext, op: Callable[[], Awaitable[T]]
) -> Ok[T] | Err[SandboxError]:
    return await fence.dispatch(context, op, classify)


@dataclass(frozen=True, slots=True)
class Owner:
    """What a session needs of its sandbox provider."""

    provider: str
    control: Control


class E2BSession:
    def __init__(self, ident: SandboxId, envd: Envd, owner: Owner) -> None:
        self._id = ident
        self._envd = envd
        self._owner = owner

    @property
    def id(self) -> SandboxId:
        return self._id

    async def exec(  # noqa: PLR0913 - the options spec/api.json names
        self,
        command: Sequence[str],
        context: SandboxContext,
        *,
        process_key: str,
        cwd: str = "/workspace",
        env: Mapping[str, str] = NO_ENV,
        timeout_ms: int | None = None,
        stdin: bytes | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        # The timeout is the sandbox layer's (run_exec); envd runs the process until killed.
        return await posix.run(
            self, command, context, process_key=process_key, cwd=cwd, env=env, stdin=stdin
        )

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        context: SandboxContext,
        process_key: str | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        return await call(context, lambda: self._envd.start(argv, env, cwd, process_key))

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        """Asks envd to kill the process it started under this key, then answers unknown:
        envd reaches that process, not what it detached (info.termination unconfirmed)."""
        killed = await call(context, lambda: self._envd.signal(process_key))
        return killed if isinstance(killed, Err) else Ok("unknown")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        bad = invalid_path(path)
        return bad or await call(context, lambda: self._envd.upload(path, data))

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        bad = invalid_path(path)
        return bad or await call(context, lambda: self._envd.download(path))

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        """Declared absent (sandbox.py, ): nothing reaches E2B."""
        return Err(SandboxError("unavailable", "e2b: this adapter takes no snapshots"))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        """Kills the sandbox; one already gone is released too."""
        killed = await call(context, lambda: self._owner.control.kill(self._id))
        if isinstance(killed, Err):
            if is_refusal(killed.error):
                return killed
            return Err(SandboxError("release_failed", killed.error.message))
        await self._envd.aclose()
        return Ok(None)
