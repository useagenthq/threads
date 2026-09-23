"""One sandbox's envd, the E2B agent inside it: processes through the SDK's connect-RPC stubs
(e2b.envd.process) and files through envd's HTTP file API, both on fenced transports.

Processes run as root: the sandbox is its own VM, and /workspace sits outside the default
user's home. Each process carries its process key as envd's tag, so a later terminate can name
it to envd; envd's kill reaches the process it started, not descendants a command detached, so
it never proves a group gone.
"""

import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus

import httpx
import pyqwest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from e2b.connection_config import KEEPALIVE_PING_HEADER, KEEPALIVE_PING_INTERVAL_SEC
from e2b.envd.api import ENVD_API_FILES_ROUTE
from e2b.envd.client_shared import ENVD_JSON_CODEC
from e2b.envd.process import process_connect, process_pb
from protobuf import Oneof

from threads.adapters.sandboxes.e2b import wire
from threads.adapters.sandboxes.e2b.transport import FencedHttpx, FencedPyqwest
from threads.adapters.sandboxes.streams import Pipe, StreamLostError, pump
from threads.sandbox.protocol import ExecOutput, SandboxError, SandboxErrorCode

USER = "root"
ENVD_PORT = 49983


class FileError(Exception):
    """envd refused a file operation with a status that names why."""

    def __init__(self, error: SandboxError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True, slots=True)
class Transports:
    """The real (or, in tests, mocked) transports every envd client wraps with its fence."""

    http: httpx.AsyncBaseTransport
    rpc: pyqwest.Transport


class Envd:
    def __init__(self, sandbox: wire.Sandbox, url: str, transports: Transports) -> None:
        user = base64.b64encode(f"{USER}:".encode()).decode()
        self._headers: dict[str, str] = {
            "E2b-Sandbox-Id": sandbox.sandbox_id,
            "E2b-Sandbox-Port": str(ENVD_PORT),
            "Authorization": f"Basic {user}",
        }
        if sandbox.envd_access_token is not None:
            self._headers["X-Access-Token"] = sandbox.envd_access_token
        self._files = httpx.AsyncClient(
            base_url=url, transport=FencedHttpx(transports.http), headers=self._headers
        )
        self._rpc = process_connect.ProcessClient(
            url,
            codec=ENVD_JSON_CODEC,
            send_compression=None,
            accept_compression=(),
            http_client=pyqwest.Client(FencedPyqwest(transports.rpc)),
        )
        self._pumps: set[object] = set()

    async def start(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: str, tag: str | None
    ) -> ExecOutput:
        """Starts `argv`; returns once envd reports the process started. Its output streams
        from then on, with backpressure."""
        config = process_pb.ProcessConfig(cmd=argv[0], args=list(argv[1:]), envs=dict(env), cwd=cwd)
        request = process_pb.StartRequest(process=config, tag=tag)
        headers = {**self._headers, KEEPALIVE_PING_HEADER: str(KEEPALIVE_PING_INTERVAL_SEC)}
        events = aiter(self._rpc.start(request, headers=headers))
        pipe = Pipe()
        await _started(events, pipe)
        task = pump(pipe, lambda p: _feed(events, p))
        self._pumps.add(task)
        task.add_done_callback(self._pumps.discard)
        return pipe.output()

    async def signal(self, tag: str) -> bool:
        """SIGKILL to the process envd started under `tag`. False: none runs."""
        selector = process_pb.ProcessSelector(selector=Oneof(field="tag", value=tag))
        request = process_pb.SendSignalRequest(process=selector, signal=process_pb.Signal.SIGKILL)
        try:
            await self._rpc.send_signal(request, headers=self._headers)
        except ConnectError as error:
            if error.code == Code.NOT_FOUND:
                return False
            raise
        return True

    async def upload(self, path: str, data: bytes) -> None:
        res = await self._files.post(
            ENVD_API_FILES_ROUTE,
            params={"path": path, "username": USER},
            files={"file": (path, data)},
        )
        _checked(res, path)

    async def download(self, path: str) -> bytes:
        res = await self._files.get(ENVD_API_FILES_ROUTE, params={"path": path, "username": USER})
        return _checked(res, path)

    async def aclose(self) -> None:
        await self._files.aclose()


async def _started(events: AsyncIterator[process_pb.StartResponse], pipe: Pipe) -> None:
    """Sends the request (fenced) and waits for envd's start event; output that arrives first
    goes to the pipe."""
    async for response in events:
        event = response.event.event if response.event is not None else None
        match event:
            case Oneof(field="start"):
                return
            case Oneof(field="data", value=data):
                await _data(data, pipe)
            case _:
                pass
    raise StreamLostError("envd ended the process stream before it started")


async def _feed(events: AsyncIterator[process_pb.StartResponse], pipe: Pipe) -> int:
    async for response in events:
        event = response.event.event if response.event is not None else None
        match event:
            case Oneof(field="data", value=data):
                await _data(data, pipe)
            case Oneof(field="end", value=end):
                return end.exit_code
            case _:
                pass
    raise StreamLostError("envd ended the process stream without its exit")


async def _data(data: process_pb.ProcessEvent.DataEvent, pipe: Pipe) -> None:
    match data.output:
        case Oneof(field="stdout", value=chunk):
            await pipe.stdout(chunk)
        case Oneof(field="stderr", value=chunk):
            await pipe.stderr(chunk)
        case _:
            pass


def _checked(res: httpx.Response, path: str) -> bytes:
    """A file response's body, or the FileError its status names."""
    if res.status_code == HTTPStatus.OK:
        return res.content
    text = res.text[:500]
    code: SandboxErrorCode
    match res.status_code:
        case HTTPStatus.NOT_FOUND:
            code = "not_found"
        case HTTPStatus.BAD_REQUEST:
            code = "is_directory" if "directory" in text.lower() else "invalid_path"
        case HTTPStatus.UNAUTHORIZED | HTTPStatus.FORBIDDEN:
            code = "permission_denied"
        case HTTPStatus.REQUEST_ENTITY_TOO_LARGE | HTTPStatus.INSUFFICIENT_STORAGE:
            code = "too_large"
        case _:
            code = "unavailable"
    raise FileError(SandboxError(code, f"{path}: envd {res.status_code}: {text}"))
