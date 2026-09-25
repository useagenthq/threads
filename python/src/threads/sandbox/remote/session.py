"""One remote sandbox as a `SandboxSession` (spec/api.json): every operation fences through the
driver's transport, and everything above the raw provider calls is the shared POSIX kit
(adapters/sandboxes/posix.py)."""

from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from typing import assert_never

from threads.adapters.sandboxes import posix
from threads.adapters.sandboxes.fence import dispatch
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
from threads.sandbox.remote.driver import (
    Capture,
    Confirmed,
    FileError,
    SandboxDriver,
    Taken,
    Unconfirmed,
)
from threads.sandbox.trees import tree_hash


async def call[T](
    driver: SandboxDriver, context: SandboxContext, op: Callable[[], Awaitable[T]]
) -> Ok[T] | Err[SandboxError]:
    """One driver operation under `context`: a refused fence is its typed refusal, a FileError
    its named failure, anything else what the driver's `classify` says it is."""

    def classify(error: Exception) -> SandboxError | None:
        return error.error if isinstance(error, FileError) else driver.classify(error)

    return await dispatch(context, op, classify)


def released(error: SandboxError) -> Err[SandboxError]:
    """A failed release: a refusal as it is, anything else release_failed."""
    return Err(error if is_refusal(error) else SandboxError("release_failed", error.message))


class RemoteSession:
    def __init__(self, driver: SandboxDriver, provider: str, ident: str) -> None:
        self._driver, self._provider = driver, provider
        self._id = SandboxId(ident)

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
        # The deadline is the sandbox layer's (run_exec): it reports a timeout and terminates.
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
        """posix.Primitives: the provider's own exec."""
        return await call(
            self._driver, context, lambda: self._driver.run(self._id, argv, env, cwd, process_key)
        )

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        """A confirmed driver's answer; otherwise a best-effort kill that never claims it
        worked: a descendant can drop out of what the provider tracks (the effect parks)."""
        match self._driver.termination:
            case Confirmed(terminate=terminate):
                return await call(self._driver, context, lambda: terminate(self._id, process_key))
            case Unconfirmed(stop_process=stop):
                stopped = await call(self._driver, context, lambda: stop(self._id, process_key))
                return stopped if isinstance(stopped, Err) else Ok("unknown")
            case _:
                assert_never(self._driver.termination)

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        bad = invalid_path(path)
        if bad is not None:
            return bad
        return await call(self._driver, context, lambda: self._driver.write(self._id, path, data))

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        bad = invalid_path(path)
        if bad is not None:
            return bad
        return await call(self._driver, context, lambda: self._driver.read(self._id, path))

    async def export_tree(self, context: SandboxContext) -> Ok[ExecOutput] | Err[SandboxError]:
        return await posix.export_tree(self, context)

    async def import_tree(
        self, tar: AsyncIterable[bytes], context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        return await posix.import_tree(self, tar, context)

    async def measured(self, context: SandboxContext) -> Ok[str] | Err[SandboxError]:
        """The manifest hash of /workspace through its export; an archive the sandbox
        answered that the reader refuses is unavailable."""
        hashed = await tree_hash(self, context)
        if isinstance(hashed, Ok):
            return hashed
        failed = hashed.error
        if isinstance(failed, SandboxError):
            return Err(failed)
        return Err(SandboxError("unavailable", failed.message))

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        """Only a provider-owned whole-sandbox boundary makes a capture quiescent. The tree
        before and after it must also match, or a writer ran around it (not_quiescent). An
        A->B->A writer still passes here: core proves the image by a ledgered restore."""
        capture = self._driver.capture
        if capture is None or capture.quiescence == "unconfirmed":
            message = f"{self._provider} has no confirmed quiescent snapshot"
            return Err(SandboxError("unavailable", message))
        before = await self.measured(context)
        if isinstance(before, Err):
            return before
        taken = await call(self._driver, context, lambda: capture.take(self._id, operation_key))
        if isinstance(taken, Err):
            return taken
        after = await self.measured(context)
        hashed = before.value
        if isinstance(after, Ok) and after.value == hashed:
            return Ok(self._data(capture, taken.value, hashed))
        await call(self._driver, context, lambda: capture.delete(taken.value.ref))
        if isinstance(after, Err):
            return after
        return Err(SandboxError("not_quiescent", f"the tree of {self._id} changed around it"))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        killed = await call(self._driver, context, lambda: self._driver.kill(self._id))
        return released(killed.error) if isinstance(killed, Err) else Ok(None)

    def _data(self, capture: Capture, taken: Taken, hashed: str) -> SnapshotData:
        # The boundary covered the whole sandbox: every process in it was frozen, or ended.
        whole = [self._id]
        paused = capture.quiescence == "paused"
        return SnapshotData.model_validate(
            {
                "snapshot_id": taken.ref,
                "provider": self._provider,
                "sandbox_id": self._id,
                "capture_class": "filesystem",
                "expires_at": taken.expires_at,
                "manifest_hash": hashed,
                "quiesced": {
                    "frozen": whole if paused else [],
                    "stopped": [] if paused else whole,
                    "excluded": [],
                },
            }
        )
