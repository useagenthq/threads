"""A fake Modal: the control-plane and task-command-router RPCs the adapter uses, served in memory
by grpclib.testing.ChannelFor and acted out on a FakeBackend. Every request that reaches a
handler counts as `backend.hit()`."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import grpclib.client
import grpclib.const
import grpclib.server
from google.protobuf.message import Message
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from grpclib.testing import ChannelFor
from modal_proto import api_pb2
from modal_proto import task_command_router_pb2 as pb
from sandbox_backend import Box, FakeBackend, LostAnswerError, Proc, UnavailableError

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.modal import ModalSandbox, channel, modal
from threads.adapters.sandboxes.modal.sandbox import SERVER_URL
from threads.adapters.sandboxes.modal.session import DOWNLOAD, UPLOAD

TOKEN_ID, TOKEN_SECRET = "ak-test-token-id", "as-test-token-secret"
ROUTER_URL = "https://router.modal.test"
_TASK = "ta-"


@dataclass
class _Exec:
    box: Box
    argv: tuple[str, ...]
    proc: Proc | None
    stdin: bytearray = field(default_factory=bytearray)


class FakeModal:
    """Both services' handlers; `router_url` and the created ids are what the fake answers."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.router_url = ROUTER_URL
        self.sandbox_ids: dict[str, str] = {}
        """A sandbox id to answer SandboxCreate with, instead of the backend's (by name)."""
        self.blocked: list[bool] = []
        """block_network of every SandboxCreate."""
        self._execs: dict[str, _Exec] = {}

    def __mapping__(self) -> dict[str, grpclib.const.Handler]:
        control = "/modal.client.ModalClient/"
        router = "/modal.task_command_router.TaskCommandRouter/"
        return {
            control + "AppGetOrCreate": _unary(self._app, api_pb2.AppGetOrCreateRequest),
            control + "SandboxCreate": _unary(self._create, api_pb2.SandboxCreateRequest),
            control + "SandboxGetFromName": _unary(
                self._by_name, api_pb2.SandboxGetFromNameRequest
            ),
            control + "SandboxWait": _unary(self._wait, api_pb2.SandboxWaitRequest),
            control + "SandboxTerminate": _unary(self._terminate, api_pb2.SandboxTerminateRequest),
            control + "SandboxGetTaskId": _unary(self._task, api_pb2.SandboxGetTaskIdRequest),
            control + "TaskGetCommandRouterAccess": _unary(
                self._access, api_pb2.TaskGetCommandRouterAccessRequest
            ),
            router + "TaskExecStart": _unary(self._start, pb.TaskExecStartRequest),
            router + "TaskExecStdinWrite": _unary(self._stdin, pb.TaskExecStdinWriteRequest),
            router + "TaskExecWait": _unary(self._exit, pb.TaskExecWaitRequest),
            router + "TaskExecStdioRead": grpclib.const.Handler(
                self._read,
                grpclib.const.Cardinality.UNARY_STREAM,
                pb.TaskExecStdioReadRequest,
                pb.TaskExecStdioReadResponse,
            ),
        }

    async def _app(self, _: api_pb2.AppGetOrCreateRequest) -> Message:
        return api_pb2.AppGetOrCreateResponse(app_id="ap-test")

    async def _create(self, request: api_pb2.SandboxCreateRequest) -> Message:
        name = request.definition.name
        self.blocked.append(request.definition.block_network)
        if self._find(name) is not None:
            raise GRPCError(Status.ALREADY_EXISTS, f"{name} exists")
        box = self.backend.create(name, {})
        wanted = self.sandbox_ids.get(name)
        return api_pb2.SandboxCreateResponse(sandbox_id=box.id if wanted is None else wanted)

    async def _by_name(self, request: api_pb2.SandboxGetFromNameRequest) -> Message:
        box = self._find(request.sandbox_name)
        if box is None:
            raise GRPCError(Status.NOT_FOUND, "no such sandbox")
        return api_pb2.SandboxGetFromNameResponse(sandbox_id=box.id)

    async def _wait(self, request: api_pb2.SandboxWaitRequest) -> Message:
        box = self._box(request.sandbox_id)
        if box.alive:
            return api_pb2.SandboxWaitResponse()
        ended = api_pb2.GenericResult(status=api_pb2.GenericResult.GENERIC_STATUS_TERMINATED)
        return api_pb2.SandboxWaitResponse(result=ended)

    async def _terminate(self, request: api_pb2.SandboxTerminateRequest) -> Message:
        self._box(request.sandbox_id)
        self.backend.kill(request.sandbox_id)
        return api_pb2.SandboxTerminateResponse()

    async def _task(self, request: api_pb2.SandboxGetTaskIdRequest) -> Message:
        box = self._box(request.sandbox_id)
        if not box.alive:
            ended = api_pb2.GenericResult(status=api_pb2.GenericResult.GENERIC_STATUS_TERMINATED)
            return api_pb2.SandboxGetTaskIdResponse(task_result=ended)
        return api_pb2.SandboxGetTaskIdResponse(task_id=_TASK + box.id)

    async def _access(self, _: api_pb2.TaskGetCommandRouterAccessRequest) -> Message:
        return api_pb2.TaskGetCommandRouterAccessResponse(url=self.router_url, jwt="jwt-test")

    async def _start(self, request: pb.TaskExecStartRequest) -> Message:
        box = self._box(request.task_id.removeprefix(_TASK))
        argv = tuple(request.command_args)
        env: Mapping[str, str] = dict(request.env)
        proc = None
        if argv[:3] not in (("/bin/sh", "-c", UPLOAD), ("/bin/sh", "-c", DOWNLOAD)):
            proc = self.backend.run(box, argv, env, tag=request.exec_id)
        self._execs[request.exec_id] = _Exec(box, argv, proc)
        return pb.TaskExecStartResponse()

    async def _stdin(self, request: pb.TaskExecStdinWriteRequest) -> Message:
        running = self._execs[request.exec_id]
        running.stdin += request.data
        return pb.TaskExecStdinWriteResponse()

    async def _exit(self, request: pb.TaskExecWaitRequest) -> Message:
        running = self._execs[request.exec_id]
        code = await running.proc.exit if running.proc is not None else self._file(running)[0]
        return pb.TaskExecWaitResponse(code=code)

    async def _read(
        self, stream: "grpclib.server.Stream[pb.TaskExecStdioReadRequest, Message]"
    ) -> None:
        self.backend.hit()
        request = await stream.recv_message()
        assert request is not None
        running = self._execs[request.exec_id]
        stderr = request.file_descriptor == pb.TASK_EXEC_STDIO_FILE_DESCRIPTOR_STDERR
        if running.proc is None:
            data = b"" if stderr else self._file(running)[1]
        else:
            data = running.proc.stderr if stderr else running.proc.stdout
        if data:
            await stream.send_message(pb.TaskExecStdioReadResponse(data=data))

    def _file(self, running: _Exec) -> tuple[int, bytes]:
        path, box = running.argv[4], running.box
        if self.backend.is_directory(box, path):
            return 21, b""
        if running.argv[2] == UPLOAD:
            self.backend.write(box, path, bytes(running.stdin))
            return 0, b""
        data = self.backend.read(box, path)
        return (2, b"") if data is None else (0, data)

    def _find(self, name: str) -> Box | None:
        try:
            return self.backend.find(name)
        except UnavailableError as error:
            raise GRPCError(Status.UNAVAILABLE, str(error)) from error

    def _box(self, sandbox_id: str) -> Box:
        box = self.backend.boxes.get(sandbox_id)
        if box is None:
            raise GRPCError(Status.NOT_FOUND, f"no sandbox {sandbox_id}")
        return box


def _unary[R: Message](
    act: Callable[[R], Awaitable[Message]], request_type: type[R]
) -> grpclib.const.Handler:
    """A unary handler over a FakeModal method: one request reaches the service, one answer.
    A lost answer acts first and then fails the call; an unavailable backend fails it untouched."""

    async def handle(stream: "grpclib.server.Stream[R, Message]") -> None:
        owner = getattr(act, "__self__", None)
        assert isinstance(owner, FakeModal)
        owner.backend.hit()
        request = await stream.recv_message()
        assert request is not None
        try:
            answer = await act(request)
        except (LostAnswerError, UnavailableError) as error:
            raise GRPCError(Status.UNAVAILABLE, str(error)) from error
        await stream.send_message(answer)

    return grpclib.const.Handler(
        handle, grpclib.const.Cardinality.UNARY_UNARY, request_type, Message
    )


@asynccontextmanager
async def harness(
    backend: FakeBackend, name: str, fake: FakeModal | None = None
) -> AsyncGenerator[ModalSandbox]:
    """The adapter over `backend`, its control plane and router on in-memory channels."""
    service = fake or FakeModal(backend)
    # The hold outlives the in-memory channels: ChannelFor closes its own first, then the
    # loop's release closes them again, which does nothing.
    async with holding(), AsyncExitStack() as stack:
        channels: dict[str, grpclib.client.Channel] = {
            SERVER_URL: await stack.enter_async_context(ChannelFor([service])),
            ROUTER_URL: await stack.enter_async_context(ChannelFor([service])),
        }

        def connect(url: str) -> grpclib.client.Channel:
            # Any other URL goes to the real connect, which opens nothing but https.
            return channels[url] if url in channels else channel.connect(url)

        sandbox = modal(
            image_id="im-test",
            token_id=TOKEN_ID,
            token_secret=TOKEN_SECRET,
            name=name,
            connect=connect,
        )
        yield sandbox
