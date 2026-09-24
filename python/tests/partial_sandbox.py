"""Custom sandboxes over the fake that implement only some of the optional lookups
(`LooksUpSandbox`, `LooksUpSnapshot`), declaring whatever `declares` says."""

from dataclasses import dataclass, replace
from typing import Literal

from threads.log import SnapshotData
from threads.loop.model import LookupResult
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, LookupSupport, SandboxError, SandboxInfo, SandboxSession
from threads.sandbox.protocol import SandboxContext

NEITHER = LookupSupport(create="none", snapshot="none")


@dataclass
class NoLookups:
    """A sandbox with neither lookup method."""

    inner: FakeSandbox
    declares: LookupSupport = NEITHER

    @property
    def info(self) -> SandboxInfo:
        return replace(self.inner.info, lookup=self.declares)

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return await self.inner.create(operation_key, context)

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return await self.inner.restore(snapshot_id, manifest_hash, operation_key, context)

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return await self.inner.attach(ref, context)

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        return await self.inner.release(ref, context)


@dataclass
class CreateLookupOnly(NoLookups):
    async def lookup(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SandboxSession]:
        return await self.inner.lookup(operation_key, context)


@dataclass
class SnapshotLookupOnly(NoLookups):
    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SnapshotData]:
        return await self.inner.lookup_snapshot(operation_key, context)
