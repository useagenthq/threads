"""One dev sandbox as a `SandboxSession`: commands run inside the platform's confinement
(confine.py), and everything else is a host file operation on the sandbox's directory that never
follows a symlink (walk.py). The fence is checked immediately before the spawn and before each
file operation's first syscall, so a stale writer spawns nothing and writes nothing."""

import asyncio
import contextlib
import os
import shutil
import signal
import sys
from collections.abc import AsyncIterable, Mapping, Sequence

from threads.adapters.sandboxes.dev.confine import WORKSPACE, ConfinedSpec, Confinement
from threads.adapters.sandboxes.dev.trees import export_dir, import_dir
from threads.adapters.sandboxes.dev.walk import host_dir, read_in, workspace_parts, write_in
from threads.adapters.sandboxes.streams import Pipe, pump
from threads.log import SnapshotData
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox.admit import admit_exec
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    SandboxContext,
    SandboxError,
    SandboxId,
    refused,
)

_SYSTEM_PATH: tuple[str, ...] = (
    ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
    if sys.platform == "darwin"
    else ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
)
"""Where a bare command name is looked up. The confinement's environment is exactly the call's,
which has no PATH, so argv[0] is resolved here first, as the remote kit's script does inside its
sandbox. Linux binds these paths read-only, so the answer is the same inside."""

_CHUNK = 64 * 1024


def _program(name: str) -> str | None:
    if "/" in name:
        return name
    return shutil.which(name, path=":".join(_SYSTEM_PATH))


async def _not_found(name: str) -> ExecOutput:
    """What a sandbox answers for a command it has no executable for, as a shell does."""
    pipe = Pipe()

    async def feed(p: Pipe) -> int:
        await p.stderr(f"threads: command not found: {name}\n".encode())
        return 127

    pump(pipe, feed)
    return pipe.output()


async def _feed(process: asyncio.subprocess.Process, pipe: Pipe) -> int:
    async def drain(stream: asyncio.StreamReader | None, out: bool) -> None:
        if stream is None:
            return
        while chunk := await stream.read(_CHUNK):
            await (pipe.stdout(chunk) if out else pipe.stderr(chunk))

    await asyncio.gather(drain(process.stdout, True), drain(process.stderr, False))
    return await process.wait()


class DevSession:
    """spec/api.json `SandboxSession` on one host directory inside an OS confinement."""

    def __init__(self, directory: str, ident: str, confined: Confinement) -> None:
        self._dir, self._confined = directory, confined
        self._id = SandboxId(ident)
        self._running: dict[str, asyncio.subprocess.Process] = {}

    @property
    def id(self) -> SandboxId:
        return self._id

    async def exec(  # noqa: PLR0913 - the options spec/api.json names
        self,
        command: Sequence[str],
        context: SandboxContext,
        *,
        process_key: str,
        cwd: str = WORKSPACE,
        env: Mapping[str, str] = NO_ENV,
        timeout_ms: int | None = None,
        stdin: bytes | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        # Before any await, so a caller bug never reads as a sandbox failure.
        argv, exact = admit_exec(command, env)
        inside = workspace_parts(cwd)
        if isinstance(inside, Err):
            return inside
        at = host_dir(self._dir, inside.value)
        if isinstance(at, Err):
            return at
        program = _program(argv[0])
        if program is None:
            return Ok(await _not_found(argv[0]))
        wrapped = self._confined.wrap(
            ConfinedSpec(
                self._dir, (program, *argv[1:]), self._confined.guest(self._dir, cwd), exact
            )
        )
        # The deadline is the sandbox layer's (run_exec): it reports a timeout and terminates.
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        try:
            process = await asyncio.create_subprocess_exec(
                *wrapped,
                cwd=at.value,
                env=self._confined.spawn_env(exact),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            return Err(SandboxError("unavailable", f"{self._confined.tool}: {error}"))
        self._running[process_key] = process
        if process.stdin is not None:
            process.stdin.write(stdin or b"")
            with contextlib.suppress(OSError, BrokenPipeError):
                process.stdin.close()
        pipe = Pipe()
        pump(pipe, lambda p: self._reaped(process_key, process, p))
        return Ok(pipe.output())

    async def _reaped(
        self, process_key: str, process: asyncio.subprocess.Process, pipe: Pipe
    ) -> int:
        try:
            return await _feed(process, pipe)
        finally:
            self._running.pop(process_key, None)

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        """Kills the process group the key started. A group leader can still leave a descendant
        behind (a double fork on macOS, where there is no pid namespace), so this proves nothing
        and the answer stays unknown, as `SandboxInfo` declares."""
        process = self._running.get(process_key)
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        self._kill(process)
        return Ok("unknown")

    def _kill(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        # Already gone, or in another session: the answer is unknown either way.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        parts = workspace_parts(path)
        if isinstance(parts, Err):
            return parts
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return write_in(self._dir, parts.value, data, 0o644)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        parts = workspace_parts(path)
        if isinstance(parts, Err):
            return parts
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return read_in(self._dir, parts.value)

    async def export_tree(self, context: SandboxContext) -> Ok[ExecOutput] | Err[SandboxError]:
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return export_dir(self._dir)

    async def import_tree(
        self, tar: AsyncIterable[bytes], context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return await import_dir(self._dir, tar)

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return Err(SandboxError("unavailable", "the dev sandbox has no snapshots"))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        for process in list(self._running.values()):
            self._kill(process)
        self._running.clear()
        try:
            shutil.rmtree(self._dir, ignore_errors=False)
        except FileNotFoundError:
            return Ok(None)
        except OSError as error:
            return Err(SandboxError("release_failed", f"{self._dir}: {error}"))
        return Ok(None)
