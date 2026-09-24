"""The remote kit's driver straight over a FakeBackend (sandbox_backend.py), each call behind the
fence as a real transport's send would be: for testing RemoteSandbox without any provider's wire
format. Declarations are parameters, so the kit's derived SandboxInfo can be tested too."""

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Literal, Self

from sandbox_backend import Box, FakeBackend, LostAnswerError, UnavailableError

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes import fence
from threads.loop.model import Found, LookupUnknown, NotFoundNonfinal
from threads.sandbox.protocol import NO_ENV, ExecOutput, SandboxError
from threads.sandbox.remote.driver import (
    Capture,
    Confirmed,
    FileError,
    FinalLookup,
    NonfinalLookup,
    Taken,
    Unconfirmed,
    Unmade,
)
from threads.sandbox.remote.sandbox import RemoteInfo, RemoteSandbox


def classify(error: Exception) -> SandboxError | None:
    if isinstance(error, LostAnswerError | UnavailableError):
        return SandboxError("unavailable", str(error))
    return None


class MemoryDriver:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.lookup: NonfinalLookup | FinalLookup = NonfinalLookup(self.find)
        self.termination: Unconfirmed | Confirmed = Unconfirmed(self.stop_process)
        self.capture: Capture | None = Capture("stopped", self.take, self.delete)

    def bound(self) -> Self:
        return self

    @property
    def classify(self) -> fence.Classify:
        return classify

    async def _sent(self) -> None:
        await fence.check()
        self.backend.hit()

    def _box(self, sandbox_id: str) -> Box:
        box = self.backend.get(sandbox_id)
        if box is None:
            raise UnavailableError(f"no sandbox {sandbox_id}")
        return box

    async def create(self, operation_key: str, snapshot: str | None) -> str | Unmade:
        await self._sent()
        if snapshot is None:
            return self.backend.create(operation_key, NO_ENV).id
        box = self.backend.restore(snapshot, operation_key, NO_ENV)
        return Unmade("snapshot_missing", f"no {snapshot}") if box is None else box.id

    async def find(self, operation_key: str) -> Found[str] | NotFoundNonfinal | LookupUnknown:
        await self._sent()
        box = self.backend.find(operation_key)
        return NotFoundNonfinal() if box is None else Found(box.id)

    async def exists(self, sandbox_id: str) -> bool:
        await self._sent()
        return self.backend.get(sandbox_id) is not None

    async def kill(self, sandbox_id: str) -> Literal["killed", "already_gone"]:
        await self._sent()
        return "killed" if self.backend.kill(sandbox_id) else "already_gone"

    async def run(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        process_key: str | None,
    ) -> ExecOutput:
        await self._sent()
        proc = self.backend.run(self._box(sandbox_id), argv, env, process_key)
        return ExecOutput(proc.exit, _once(proc.stdout), _once(proc.stderr))

    async def stop_process(self, sandbox_id: str, process_key: str) -> None:
        await self._sent()
        self.backend.signal(self._box(sandbox_id), process_key)

    async def write(self, sandbox_id: str, path: str, data: bytes) -> None:
        await self._sent()
        box = self._box(sandbox_id)
        if self.backend.is_directory(box, path):
            raise FileError(SandboxError("is_directory", path))
        self.backend.write(box, path, data)

    async def read(self, sandbox_id: str, path: str) -> bytes:
        await self._sent()
        box = self._box(sandbox_id)
        data = self.backend.read(box, path)
        if data is None:
            raise FileError(SandboxError("not_found", path))
        return data

    async def take(self, sandbox_id: str, operation_key: str) -> Taken:
        await self._sent()
        box = self._box(sandbox_id)
        self.backend.stop(box)
        return Taken(self.backend.snapshot(box, operation_key, frozen=False).id, None)

    async def delete(self, ref: str) -> Literal["released", "already_gone"]:
        await self._sent()
        return "released" if self.backend.delete_snapshot(ref) else "already_gone"


async def _once(chunk: bytes) -> AsyncIterator[bytes]:
    if chunk:
        yield chunk


@asynccontextmanager
async def serve(backend: FakeBackend, name: str) -> AsyncGenerator[RemoteSandbox]:
    """sandbox_contract.Make: the kit over a memory driver."""
    async with holding():  # the deadline suite's exec spawns under the loop's hold
        yield RemoteSandbox(MemoryDriver(backend), RemoteInfo(name, "enforced"))
