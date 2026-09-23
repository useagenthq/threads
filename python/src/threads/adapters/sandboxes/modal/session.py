"""One Modal sandbox (spec/api.json `SandboxSession`). Exec, upload and download all run through
the task command router, each operation fenced at its channels' send points."""

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final

from threads.adapters.sandboxes import posix
from threads.adapters.sandboxes.fence import dispatch
from threads.adapters.sandboxes.modal.channel import SandboxEndedError, classify
from threads.adapters.sandboxes.modal.control import Control
from threads.adapters.sandboxes.modal.router import Router
from threads.adapters.sandboxes.streams import Pipe, pump
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
    refused,
)

if TYPE_CHECKING:
    import asyncio

UPLOAD: Final = r"""[ -d "$1" ] && exit 21
mkdir -p "$(dirname "$1")" 2>/dev/null
cat > "$1" 2>/dev/null || exit 13
"""
DOWNLOAD: Final = r"""[ -d "$1" ] && exit 21
[ -e "$1" ] || exit 2
[ -r "$1" ] || exit 13
exec cat -- "$1"
"""
_CODES: Mapping[int, SandboxError] = {
    2: SandboxError("not_found", "no such file"),
    13: SandboxError("permission_denied", "permission denied"),
    21: SandboxError("is_directory", "is a directory"),
}

type Routers = Callable[[str, str, str], Router]
"""Router for (url, task_id, jwt), on a channel the sandbox owns."""


class ModalSession:
    def __init__(self, ident: str, control: Control, routers: Routers) -> None:
        self._id = SandboxId(ident)
        self._control = control
        self._routers = routers
        self._router: Router | None = None
        self._pumps: set[asyncio.Task[None]] = set()

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
        async def call() -> ExecOutput:
            router = await self._routed()
            exec_id = str(uuid.uuid4())
            await router.start(exec_id, argv, env, cwd)
            pipe = Pipe()
            task = pump(pipe, lambda p: router.feed(exec_id, p))
            self._pumps.add(task)
            task.add_done_callback(self._pumps.discard)
            return pipe.output()

        return await dispatch(context, call, classify)

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        """The router has no kill for an exec and no view of its descendants, so nothing proves
        a process group gone: always unknown, and the effect parks."""
        refusal = await refused(context)
        return refusal or Ok("unknown")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        bad = invalid_path(path)
        if bad is not None:
            return bad

        async def call() -> int:
            router = await self._routed()
            exec_id = str(uuid.uuid4())
            argv = ("/bin/sh", "-c", UPLOAD, "threads", path)
            await router.start(exec_id, argv, NO_ENV, "/", stdout=False, stderr=False)
            await router.write(exec_id, data)
            return await router.wait(exec_id)

        ran = await dispatch(context, call, classify)
        return ran if isinstance(ran, Err) else _file(ran.value, None)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        bad = invalid_path(path)
        if bad is not None:
            return bad

        async def call() -> tuple[int, bytes]:
            router = await self._routed()
            exec_id = str(uuid.uuid4())
            argv = ("/bin/sh", "-c", DOWNLOAD, "threads", path)
            await router.start(exec_id, argv, NO_ENV, "/", stderr=False)
            data = b"".join([chunk async for chunk in router.read(exec_id)])
            return await router.wait(exec_id), data

        ran = await dispatch(context, call, classify)
        if isinstance(ran, Err):
            return ran
        code, data = ran.value
        return _file(code, data)

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        """Declared absent: Modal's filesystem snapshot ends the sandbox and can't
        run during an exec, and it freezes nothing, so it can't be a quiescent capture of a
        parent that continues."""
        return Err(SandboxError("unavailable", "modal: this adapter declares no snapshots"))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        closed = await dispatch(context, lambda: self._control.terminate(self._id), classify)
        if isinstance(closed, Err) and closed.error.code == "not_found":
            return Ok(None)
        return closed

    async def _routed(self) -> Router:
        # ponytail: the router JWT is fetched once per session; refresh on UNAUTHENTICATED if
        # sessions outlive it.
        if self._router is None:
            access = await self._control.router(self._id)
            if access is None:
                raise SandboxEndedError(f"sandbox {self._id} has ended")
            self._router = self._routers(access.url, access.task_id, access.jwt)
        return self._router


def _file[T](code: int, value: T) -> Ok[T] | Err[SandboxError]:
    if code == 0:
        return Ok(value)
    return Err(_CODES.get(code, SandboxError("unavailable", f"the file command exited {code}")))
