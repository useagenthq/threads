"""A test sandbox session over real local processes: /workspace is a temp directory. It runs
the built-in tools' POSIX commands for real, and records the environment of every exec."""

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Literal

from threads.log import ParseError, SnapshotData
from threads.loop.model import LookupResult, LookupUnknown, NotFound
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxId,
    SandboxInfo,
    SandboxSession,
)


class LocalSession:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.envs: list[dict[str, str]] = []
        self.procs: dict[str, asyncio.subprocess.Process] = {}

    @property
    def id(self) -> SandboxId:
        return SandboxId("local")

    async def open(self) -> Ok[SandboxSession] | Err[SandboxError | ParseError]:
        """This session as `SandboxTools` opens one."""
        return Ok(self)

    def _host(self, path: str) -> Path:
        if not path.startswith("/workspace"):
            raise ValueError(f"test sandbox paths stay in /workspace: {path}")
        return self.root / path.removeprefix("/workspace").lstrip("/")

    async def exec(  # noqa: PLR0913 - the protocol's options
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
        if isinstance(await context.fence(), Err):
            return Err(SandboxError("stale_epoch", "fenced"))
        self.envs.append(dict(env))
        # Sandbox paths in argv are the temp tree's, and the tree's paths in output read back
        # as /workspace, so commands see and report sandbox paths.
        argv = [str(self._host(a)) if a.startswith("/workspace") else a for a in command]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._host(cwd),
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.procs[process_key] = proc
        if proc.stdout is None or proc.stderr is None:
            raise AssertionError("pipes were requested")
        exit_code = asyncio.ensure_future(proc.wait())
        home = str(self.root).encode()
        out, err = _read(proc.stdout, home), _read(proc.stderr, home)
        return Ok(ExecOutput(exit_code, out, err))

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        proc = self.procs.get(process_key)
        if proc is None or proc.returncode is not None:
            return Ok("already_exited")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return Ok("terminated")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        target = self._host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return Ok(None)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        target = self._host(path)
        if target.is_dir():
            return Err(SandboxError("is_directory", path))
        if not target.exists():
            return Err(SandboxError("not_found", path))
        return Ok(target.read_bytes())

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        return Err(SandboxError("unavailable", "the local test sandbox has no snapshots"))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        return Ok(None)


LOCAL_INFO = SandboxInfo(
    provider="local",
    egress="enforced",
    capture_classes=(),
    browser="none",
    desktop="none",
    lookup=LookupSupport(create="final", snapshot="none"),
    termination="confirmed",
)


class LocalSandbox:
    """One `LocalSession` as a provider: create makes it, attach finds it again."""

    def __init__(self, root: Path, info: SandboxInfo = LOCAL_INFO) -> None:
        self.session = LocalSession(root)
        self.created = 0
        self._info = info

    @property
    def info(self) -> SandboxInfo:
        return self._info

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        self.created += 1
        return Ok(self.session)

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return Err(SandboxError("unavailable", "no snapshots"))

    async def lookup(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SandboxSession]:
        return NotFound()

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SnapshotData]:
        return LookupUnknown("no snapshots")

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return Ok(self.session)

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        return Ok("released")


async def _read(stream: asyncio.StreamReader, home: bytes) -> AsyncIterator[bytes]:
    while chunk := await stream.read(65536):
        yield chunk.replace(home, b"/workspace")
