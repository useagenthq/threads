"""E2B, mocked at its transports over a FakeBackend: the control-plane REST API and envd's file
API behind an httpx transport that emits httpcore's header trace (where the adapter fences),
and envd's process service as a connect-RPC ASGI app behind pyqwest's ASGI transport. Only a
request that passed the fence reaches the backend (`hit`)."""

import json
import urllib.parse
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from connectrpc.request import Headers, RequestContext
from e2b.envd.client_shared import ENVD_JSON_CODEC
from e2b.envd.process import process_connect, process_pb
from protobuf import Oneof
from pyqwest.testing import ASGITransport
from sandbox_backend import Box, FakeBackend, LostAnswerError, UnavailableError

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.e2b.control import KEY
from threads.adapters.sandboxes.e2b.envd import Transports
from threads.adapters.sandboxes.e2b.sandbox import E2BSandbox

API_KEY = "e2b_test_key_0123456789"
DOMAIN = "e2b.test"
TEMPLATE = "base"
_JSON = {"content-type": "application/json"}
_STAMP = "2026-09-23T00:00:00Z"

type Handler = Callable[[httpx.Request], httpx.Response]


class TracedTransport(httpx.AsyncBaseTransport):
    """Like httpcore: the header trace fires before the request is written."""

    def __init__(self, backend: FakeBackend, handle: Handler) -> None:
        self.backend = backend
        self._handle = handle
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        trace = request.extensions.get("trace")
        if trace is not None:
            await trace("http11.send_request_headers.started", {})
        self.backend.hit()
        await request.aread()
        try:
            return self._handle(request)
        except LostAnswerError as lost:
            raise httpx.ReadError("the connection dropped after the request") from lost
        except UnavailableError as refused:
            return _json(503, {"code": 503, "message": str(refused)})


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, headers=_JSON, content=json.dumps(body).encode())


def _sandbox(box: Box, template: str) -> dict[str, object]:
    return {
        "templateID": template,
        "sandboxID": box.id,
        "clientID": "client",
        "envdVersion": "0.5.8",
        "envdAccessToken": f"envd-token-{box.id}",
        "domain": DOMAIN,
        "startedAt": _STAMP,
        "endAt": _STAMP,
        "cpuCount": 2,
        "memoryMB": 512,
        "diskSizeMB": 1024,
        "state": "running",
        "metadata": {KEY: box.key},
    }


def control(backend: FakeBackend) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.path.strip("/").split("/"):
            case ["v2", "sandboxes"]:
                return _v2(backend, request)
            case ["sandboxes", ident, *rest]:
                return _sandbox_routes(backend, request, ident, rest)
            case ["files"]:
                return files(backend, request)
            case _:
                return _json(400, {"code": 400, "message": f"unexpected {request.url}"})

    return handle


def _missing() -> httpx.Response:
    return _json(404, {"code": 404, "message": "not found"})


def _v2(backend: FakeBackend, request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        query = urllib.parse.parse_qs(request.url.params["metadata"])
        box = backend.find(urllib.parse.unquote(query[KEY][0]))
        return _json(200, [] if box is None else [_sandbox(box, TEMPLATE)])
    body = json.loads(request.content)
    key, env, template = body["metadata"][KEY], body["envVars"], body["templateID"]
    box = backend.create(key, env) if template == TEMPLATE else backend.restore(template, key, env)
    return _missing() if box is None else _json(201, _sandbox(box, template))


def _sandbox_routes(
    backend: FakeBackend, request: httpx.Request, ident: str, rest: list[str]
) -> httpx.Response:
    if request.method == "DELETE":
        return httpx.Response(204) if backend.kill(ident) else _missing()
    box = backend.get(ident)
    if box is None:
        return _missing()
    return _json(200, _sandbox(box, TEMPLATE))


def files(backend: FakeBackend, request: httpx.Request) -> httpx.Response:
    box = backend.get(request.headers["E2b-Sandbox-Id"])
    if box is None:
        return httpx.Response(502)
    path = request.url.params["path"]
    if backend.is_directory(box, path):
        return _json(400, {"code": 400, "message": f"{path} is a directory"})
    if request.method == "POST":
        backend.write(box, path, _multipart_file(request))
        return _json(200, [{"name": path, "type": "file", "path": path}])
    data = backend.read(box, path)
    return httpx.Response(200, content=data) if data is not None else _json(404, {"code": 404})


def _multipart_file(request: httpx.Request) -> bytes:
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    part = request.content.split(b"--" + boundary)[1]
    return part.split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")


class Processes(process_connect.Process):
    """envd's process service over the backend: tags are envd's process records."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend

    def _box(self, headers: Headers) -> Box:
        self.backend.hit()
        box = self.backend.get(headers.get("e2b-sandbox-id") or "")
        if box is None:
            raise ConnectError(Code.UNAVAILABLE, "no such sandbox")
        return box

    async def start(
        self,
        request: process_pb.StartRequest,
        ctx: RequestContext[process_pb.StartRequest, process_pb.StartResponse],
    ) -> AsyncIterator[process_pb.StartResponse]:
        box = self._box(ctx.request_headers)
        config = request.process
        argv = [config.cmd, *config.args] if config is not None else []
        tag = request.tag if request.has_field("tag") else None
        proc = self.backend.run(box, argv, dict(config.envs) if config else {}, tag)
        start: _Event = Oneof(field="start", value=Event.StartEvent(pid=1))
        yield _response(start)
        if proc.stdout:
            out: _Output = Oneof(field="stdout", value=proc.stdout)
            yield _response(Oneof(field="data", value=Event.DataEvent(output=out)))
        if proc.stderr:
            err: _Output = Oneof(field="stderr", value=proc.stderr)
            yield _response(Oneof(field="data", value=Event.DataEvent(output=err)))
        end = Event.EndEvent(exit_code=await proc.exit, exited=True)
        yield _response(Oneof(field="end", value=end))

    async def send_signal(
        self,
        request: process_pb.SendSignalRequest,
        ctx: RequestContext[process_pb.SendSignalRequest, process_pb.SendSignalResponse],
    ) -> process_pb.SendSignalResponse:
        box = self._box(ctx.request_headers)
        match request.process.selector if request.process else None:
            case Oneof(field="tag", value=tag) if self.backend.signal(box, tag):
                return process_pb.SendSignalResponse()
            case _:
                raise ConnectError(Code.NOT_FOUND, "no such process")


Event = process_pb.ProcessEvent
type _Output = Oneof[Literal["stdout"], bytes] | Oneof[Literal["stderr"], bytes]
type _Event = (
    Oneof[Literal["start"], Event.StartEvent]
    | Oneof[Literal["data"], Event.DataEvent]
    | Oneof[Literal["end"], Event.EndEvent]
)


def _response(event: _Event) -> process_pb.StartResponse:
    return process_pb.StartResponse(event=Event(event=event))


def transports(backend: FakeBackend, handle: Handler | None = None) -> Transports:
    """One event loop's transports to the mocked E2B over `backend`."""
    app = process_connect.ProcessASGIApplication(Processes(backend), codecs=[ENVD_JSON_CODEC])
    return Transports(TracedTransport(backend, handle or control(backend)), ASGITransport(app))


def adapter(
    backend: FakeBackend,
    name: str,
    handle: Handler | None = None,
    made: list[Transports] | None = None,
) -> E2BSandbox:
    """The adapter over `backend`; `handle` replaces the REST API (for broken answers); `made`
    records the transports it makes, one pair per event loop."""

    def make() -> Transports:
        pair = transports(backend, handle)
        if made is not None:
            made.append(pair)
        return pair

    return E2BSandbox(
        API_KEY,
        make,
        template=TEMPLATE,
        lifetime_ms=600_000,
        internet=False,
        name=name,
        domain=DOMAIN,
        api_url=f"https://api.{DOMAIN}",
    )


@asynccontextmanager
async def make(backend: FakeBackend, name: str) -> AsyncGenerator[E2BSandbox]:
    async with holding():
        yield adapter(backend, name)
