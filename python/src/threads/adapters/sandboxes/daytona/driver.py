"""The remote kit's driver over Daytona's control and toolbox clients (sandbox/remote/driver.py).
A sandbox is named by its operation key, so a lost create is found by name. Every wait polls
the control plane, each poll fenced."""

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Self

from threads.adapters.sandboxes.daytona.clients import Clients
from threads.adapters.sandboxes.daytona.control import NOT_FOUND, Placement, StatusError, classify
from threads.adapters.sandboxes.daytona.toolbox import Toolbox
from threads.adapters.sandboxes.daytona.wire import SandboxDto
from threads.adapters.sandboxes.fence import Classify
from threads.loop.model import Found, NotFoundNonfinal
from threads.sandbox.protocol import ExecOutput, SandboxError, SandboxErrorCode
from threads.sandbox.remote.driver import (
    Capture,
    FileError,
    NonfinalLookup,
    Taken,
    Unconfirmed,
    Unmade,
)


def resource_name(operation_key: str) -> str:
    """A sandbox's or snapshot's Daytona name: unique per organization, found by key."""
    return f"threads-{operation_key}"


class DaytonaDriver:
    def __init__(
        self,
        clients: Callable[[], Clients],
        base: str | None,
        placed: Placement,
        pacing: tuple[float, float],
    ) -> None:
        self._clients, self._base, self._placed = clients, base, placed
        self._poll_s, self._wait_s = pacing
        # A create that passed its fence may still be in flight at the provider.
        self.lookup = NonfinalLookup(self._find)
        # Deleting the command's session is Daytona's only kill, and it can't see detached
        # descendants.
        self.termination = Unconfirmed(self._stop)
        # Cold only: stop (every process ends), capture, start.
        self.capture = Capture("stopped", self._take, self._delete)

    def bound(self) -> Self:
        clients = self._clients()
        return type(self)(lambda: clients, self._base, self._placed, (self._poll_s, self._wait_s))

    @property
    def classify(self) -> Classify:
        return classify

    async def create(self, operation_key: str, snapshot: str | None) -> str | Unmade:
        base = self._base
        if snapshot is not None:
            snap = await self._clients().control.snapshot(snapshot)
            if snap is None:
                return Unmade("snapshot_missing", f"no snapshot {snapshot}")
            if snap.state == "inactive":
                return Unmade("snapshot_expired", f"snapshot {snapshot} is inactive")
            if snap.state != "active":
                return Unmade("snapshot_restore_failed", f"snapshot {snapshot} is {snap.state}")
            base = snap.name
        dto = await self._clients().control.create(resource_name(operation_key), base, self._placed)
        await self._toolbox_of(dto).prepare()
        return dto.id

    async def exists(self, sandbox_id: str) -> bool:
        return await self._get(sandbox_id) is not None

    async def kill(self, sandbox_id: str) -> Literal["killed", "already_gone"]:
        """Deletes and waits until Daytona reports the sandbox destroyed."""
        clients = self._clients()
        clients.toolboxes.pop(sandbox_id, None)
        return "killed" if await clients.control.delete(sandbox_id) else "already_gone"

    async def run(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        process_key: str | None,
    ) -> ExecOutput:
        toolbox = await self._toolbox(sandbox_id)
        return await toolbox.start(argv, env, cwd, process_key or "threads-control")

    async def write(self, sandbox_id: str, path: str, data: bytes) -> None:
        toolbox = await self._toolbox(sandbox_id)
        try:
            await toolbox.upload(path, data)
        except StatusError as error:
            raise _file_error(error) from error

    async def read(self, sandbox_id: str, path: str) -> bytes:
        toolbox = await self._toolbox(sandbox_id)
        try:
            return await toolbox.download(path)
        except StatusError as error:
            raise _file_error(error) from error

    async def _find(self, operation_key: str) -> Found[str] | NotFoundNonfinal:
        found = await self._get(resource_name(operation_key))
        return NotFoundNonfinal() if found is None else Found(found.id)

    async def _stop(self, sandbox_id: str, process_key: str) -> None:
        await (await self._toolbox(sandbox_id)).kill(process_key)

    async def _take(self, sandbox_id: str, operation_key: str) -> Taken:
        control = self._clients().control
        await control.stop(sandbox_id)
        snap = await control.capture(sandbox_id, resource_name(operation_key))
        await control.start(sandbox_id)
        return Taken(snap.id, None)

    async def _delete(self, ref: str) -> Literal["released", "already_gone"]:
        return "released" if await self._clients().control.remove(ref) else "already_gone"

    async def _get(self, ref: str) -> SandboxDto | None:
        found = await self._clients().control.get(ref)
        if found is not None:
            self._toolbox_of(found)
        return found

    async def _toolbox(self, sandbox_id: str) -> Toolbox:
        known = self._clients().toolboxes.get(sandbox_id)
        if known is not None:
            return known
        found = await self._clients().control.get(sandbox_id)
        if found is None:
            raise StatusError(NOT_FOUND, f"no sandbox {sandbox_id}".encode())
        return self._toolbox_of(found)

    def _toolbox_of(self, dto: SandboxDto) -> Toolbox:
        clients = self._clients()
        known = clients.toolboxes.get(dto.id)
        if known is None:
            known = clients.toolboxes[dto.id] = Toolbox(
                dto.toolbox_proxy_url,
                dto.id,
                session=clients.session,
                poll_s=self._poll_s,
                wait_s=self._wait_s,
                tasks=clients.pumps,
            )
        return known


def _file_error(error: StatusError) -> FileError:
    return FileError(SandboxError(_FILE_CODES.get(error.status, "unavailable"), str(error)))


_FILE_CODES: Mapping[int, SandboxErrorCode] = {
    400: "invalid_path",
    403: "permission_denied",
    NOT_FOUND: "not_found",
    413: "too_large",
}
