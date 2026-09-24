"""The remote kit's driver over Modal (sandbox/remote/driver.py): the control plane for create,
lookup and terminate, and each sandbox's task command router for exec and files, every request
on one event loop's fenced channels (plane.py)."""

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Self

from grpclib.const import Status
from grpclib.exceptions import GRPCError

from threads.adapters.sandboxes import fence
from threads.adapters.sandboxes.modal import session
from threads.adapters.sandboxes.modal.channel import SandboxEndedError, classify
from threads.adapters.sandboxes.modal.plane import Plane
from threads.adapters.sandboxes.modal.router import Router
from threads.adapters.sandboxes.streams import Pipe, pump
from threads.loop.model import Found, NotFoundNonfinal
from threads.sandbox.protocol import ExecOutput
from threads.sandbox.remote.driver import Capture, NonfinalLookup, Unconfirmed, Unmade


def resource_name(operation_key: str) -> str:
    return f"threads-{operation_key}"


class ModalDriver:
    def __init__(self, plane: Callable[[], Plane]) -> None:
        self._plane = plane
        # A create already past its fence may still land.
        self.lookup = NonfinalLookup(self._find)
        # The router has no kill for an exec and no view of its descendants.
        self.termination = Unconfirmed(self._stop)
        self.capture: Capture | None = None

    def bound(self) -> Self:
        plane = self._plane()
        return type(self)(lambda: plane)

    @property
    def classify(self) -> fence.Classify:
        return classify

    async def create(self, operation_key: str, snapshot: str | None) -> str | Unmade:
        """From the image: with no capture, the kit never restores (`snapshot` is None). Modal
        refuses a second running sandbox of one name (ALREADY_EXISTS): that is this key's."""
        control = self._plane().control
        try:
            return await control.create(resource_name(operation_key))
        except GRPCError as error:
            if error.status != Status.ALREADY_EXISTS:
                raise
            return await control.by_name(resource_name(operation_key))

    async def exists(self, sandbox_id: str) -> bool:
        return await self._plane().control.running(sandbox_id)

    async def kill(self, sandbox_id: str) -> Literal["killed", "already_gone"]:
        try:
            await self._plane().control.terminate(sandbox_id)
        except GRPCError as error:
            if error.status != Status.NOT_FOUND:
                raise
            return "already_gone"
        return "killed"

    async def run(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        process_key: str | None,
    ) -> ExecOutput:
        router = await self._routed(sandbox_id)
        exec_id = str(uuid.uuid4())
        await router.start(exec_id, argv, env, cwd)
        pipe = Pipe()
        task = pump(pipe, lambda p: router.feed(exec_id, p))
        pumps = self._plane().pumps
        pumps.add(task)
        task.add_done_callback(pumps.discard)
        return pipe.output()

    async def write(self, sandbox_id: str, path: str, data: bytes) -> None:
        await session.upload(await self._routed(sandbox_id), path, data)

    async def read(self, sandbox_id: str, path: str) -> bytes:
        return await session.download(await self._routed(sandbox_id), path)

    async def _find(self, operation_key: str) -> Found[str] | NotFoundNonfinal:
        try:
            return Found(await self._plane().control.by_name(resource_name(operation_key)))
        except GRPCError as error:
            if error.status != Status.NOT_FOUND:
                raise
            return NotFoundNonfinal()

    async def _stop(self, _sandbox_id: str, _process_key: str) -> None:
        """Nothing to send: the router can't kill an exec. The fence still answers, so a stale
        owner's terminate is refused like any other operation."""
        await fence.check()

    async def _routed(self, sandbox_id: str) -> Router:
        # ponytail: the router JWT is fetched once per sandbox and loop; refresh on
        # UNAUTHENTICATED if sandboxes outlive it.
        plane = self._plane()
        known = plane.routers.get(sandbox_id)
        if known is not None:
            return known
        access = await plane.control.router(sandbox_id)
        if access is None:
            raise SandboxEndedError(f"sandbox {sandbox_id} has ended")
        router = plane.routers[sandbox_id] = plane.router(access.url, access.task_id, access.jwt)
        return router
