"""One Daytona sandbox as a `SandboxSession` (spec/api.json)."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from threads.adapters.sandboxes import posix
from threads.adapters.sandboxes.daytona.control import NOT_FOUND, Control, StatusError, classify
from threads.adapters.sandboxes.daytona.toolbox import Toolbox
from threads.adapters.sandboxes.fence import dispatch
from threads.log import SnapshotData
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox.manifest import manifest_hash
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    SandboxContext,
    SandboxError,
    SandboxErrorCode,
    SandboxId,
    invalid_path,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threads.sandbox.manifest import ManifestEntry


def resource_name(operation_key: str) -> str:
    """A sandbox's or snapshot's Daytona name: unique per organization, found by key."""
    return f"threads-{operation_key}"


class DaytonaSession:
    def __init__(self, ident: str, provider: str, control: Control, toolbox: Toolbox) -> None:
        self._id = SandboxId(ident)
        self._provider, self._control, self._toolbox = provider, control, toolbox

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
        key = process_key or "threads-control"  # the manifest's session
        return await dispatch(context, lambda: self._toolbox.start(argv, env, cwd, key), classify)

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        """Best effort: the session is deleted, but nothing confirms the group is gone."""
        killed = await dispatch(context, lambda: self._toolbox.kill(process_key), classify)
        return killed if isinstance(killed, Err) else Ok("unknown")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        bad = invalid_path(path)
        if bad is not None:
            return bad
        return await dispatch(context, lambda: self._toolbox.upload(path, data), _file_error)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        bad = invalid_path(path)
        if bad is not None:
            return bad
        return await dispatch(context, lambda: self._toolbox.download(path), _file_error)

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        """A cold snapshot: the whole sandbox is stopped (every process ends), captured, then
        started again. Quiescent because stopped, and disruptive for that reason. The manifest
        is the parent's just before the stop: a claim that core proves against the image by a
        ledgered restore before it records the snapshot (thread/snapshot.py)."""
        tree = await posix.manifest(self, context)
        if isinstance(tree, Err):
            return tree
        name = resource_name(operation_key)
        taken = await dispatch(context, lambda: self._capture(name), classify)
        if isinstance(taken, Err):
            return taken
        return Ok(self._data(taken.value, tree.value))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        """Deletes the sandbox and waits until Daytona reports it destroyed."""
        closed = await dispatch(context, lambda: self._control.delete(self._id), classify)
        if isinstance(closed, Err) and closed.error.code == "unavailable":
            return Err(SandboxError("release_failed", closed.error.message))
        return closed if isinstance(closed, Err) else Ok(None)

    async def _capture(self, name: str) -> str:
        """The snapshot's Daytona id."""
        await self._control.stop(self._id)
        snap = await self._control.capture(self._id, name)
        await self._control.start(self._id)
        return snap.id

    def _data(self, snapshot_id: str, tree: "list[ManifestEntry]") -> SnapshotData:
        data: dict[str, JsonValue] = {
            "snapshot_id": snapshot_id,
            "provider": self._provider,
            "sandbox_id": self._id,
            "capture_class": "filesystem",
            "expires_at": None,
            "manifest_hash": manifest_hash(tree),
            "quiesced": {"frozen": [], "stopped": [f"sandbox {self._id}"], "excluded": []},
        }
        return SnapshotData.model_validate(data)


def _file_error(error: Exception) -> SandboxError | None:
    if isinstance(error, StatusError):
        return SandboxError(_FILE_CODES.get(error.status, "unavailable"), str(error))
    return classify(error)


_FILE_CODES: Mapping[int, SandboxErrorCode] = {
    400: "invalid_path",
    403: "permission_denied",
    NOT_FOUND: "not_found",
    413: "too_large",
}
