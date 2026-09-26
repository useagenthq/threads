"""A sandbox whose sessions have no Trees capability: everything else delegates to the fake."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import SnapshotData
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, SandboxError, SandboxInfo, SandboxSession
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    SandboxContext,
    SandboxId,
    Termination,
)


@dataclass
class NoTreesSession:
    inner: SandboxSession

    @property
    def id(self) -> SandboxId:
        return self.inner.id

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
        return await self.inner.exec(
            command,
            context,
            process_key=process_key,
            cwd=cwd,
            env=env,
            timeout_ms=timeout_ms,
            stdin=stdin,
        )

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        return await self.inner.terminate(process_key, context)

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        return await self.inner.upload(path, data, context)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        return await self.inner.download(path, context)

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        return await self.inner.snapshot(operation_key, context)

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        return await self.inner.close(context)


@dataclass
class NoTreesSandbox:
    inner: FakeSandbox

    @property
    def info(self) -> SandboxInfo:
        return self.inner.info

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        made = await self.inner.create(operation_key, context)
        return made if isinstance(made, Err) else Ok(NoTreesSession(made.value))

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        made = await self.inner.restore(snapshot_id, manifest_hash, operation_key, context)
        return made if isinstance(made, Err) else Ok(NoTreesSession(made.value))

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        made = await self.inner.attach(ref, context)
        return made if isinstance(made, Err) else Ok(NoTreesSession(made.value))

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        return await self.inner.release(ref, context)
