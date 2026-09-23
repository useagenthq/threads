"""A mocked Daytona API and toolbox on loopback: aiohttp.web routes that speak Daytona's wire
calls and act on a FakeBackend (sandbox_backend.py). Every request that reaches it counts."""

import asyncio
import shlex
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Literal

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, Field
from sandbox_backend import Box, FakeBackend, LostAnswerError, Proc, UnavailableError

from threads.adapters.sandboxes.daytona import DaytonaSandbox
from threads.adapters.sandboxes.daytona.logs import STDERR, STDOUT
from threads.adapters.sandboxes.daytona.toolbox import PREFIX
from threads.sandbox.fake import FakeCrashError

API_KEY = "dtn-test-secret-key"
type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class _Create(BaseModel):
    name: str
    snapshot: str | None = None
    network_block_all: Literal[True] = Field(alias="networkBlockAll")  # egress denied by default


class _Named(BaseModel):
    name: str


class _Exec(BaseModel):
    command: str


class DaytonaServer:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.crash = False
        self.states: dict[str, str] = {}
        self.commands: dict[str, Proc] = {}
        self.app = web.Application(middlewares=[self._count])
        r = self.app.router
        r.add_post("/sandbox", self._create)
        r.add_get("/sandbox/{ref}", self._get)
        r.add_delete("/sandbox/{ref}", self._delete)
        r.add_post("/sandbox/{ref}/stop", self._stop)
        r.add_post("/sandbox/{ref}/start", self._start)
        r.add_post("/sandbox/{ref}/snapshot", self._snapshot)
        r.add_get("/snapshots/{ref}", self._get_snapshot)
        r.add_delete("/snapshots/{ref}", self._delete_snapshot)
        t = "/toolbox/{box}"
        r.add_post(t + "/files/upload-v2", self._upload)
        r.add_get(t + "/files/download", self._download)
        r.add_post(t + "/process/session", self._ok)
        r.add_delete(t + "/process/session/{sid}", self._kill)
        r.add_post(t + "/process/session/{sid}/exec", self._exec)
        r.add_get(t + "/process/session/{sid}/command/{cmd}", self._command)
        r.add_get(t + "/process/session/{sid}/command/{cmd}/logs", self._logs)
        self.base = ""

    @web.middleware
    async def _count(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        self.backend.hit()
        try:
            return await handler(request)
        except UnavailableError:
            return web.Response(status=503)
        except LostAnswerError:
            if request.transport is not None:
                request.transport.close()
            return web.Response()
        except FakeCrashError:
            self.crash = True  # the host dies: crash_trace raises it on the client side
            return web.Response(status=500)

    def crash_trace(self) -> aiohttp.TraceConfig:
        async def end(_s: aiohttp.ClientSession, _c: SimpleNamespace, _p: object) -> None:
            if self.crash:
                self.crash = False
                raise FakeCrashError("the host died after the provider acted")

        config = aiohttp.TraceConfig()
        config.on_request_end.append(end)
        return config

    def _dto(self, box: Box) -> web.Response:
        state = self.states.get(box.id, "started")
        toolbox = f"{self.base}/toolbox"
        body = {"id": box.id, "name": box.key or box.id, "state": state, "toolboxProxyUrl": toolbox}
        return web.json_response(body)

    def _box(self, request: web.Request, key: str = "ref") -> Box | None:
        ref = request.match_info[key]
        return self.backend.get(ref) or self.backend.find(ref)

    async def _create(self, request: web.Request) -> web.Response:
        want = _Create.model_validate_json(await request.read())
        if any(b.alive and b.key == want.name for b in self.backend.boxes.values()):
            return web.Response(status=409)
        if want.snapshot is None:
            return self._dto(self.backend.create(want.name, {}))
        snap = self.backend.snaps.get(want.snapshot) or self.backend.find_snapshot(want.snapshot)
        box = None if snap is None else self.backend.restore(snap.id, want.name, {})
        return web.Response(status=404) if box is None else self._dto(box)

    async def _get(self, request: web.Request) -> web.Response:
        box = self._box(request)
        return web.Response(status=404) if box is None else self._dto(box)

    async def _delete(self, request: web.Request) -> web.Response:
        box = self._box(request)
        if box is None or not self.backend.kill(box.id):
            return web.Response(status=404)
        return web.Response()

    async def _stop(self, request: web.Request) -> web.Response:
        box = self._box(request)
        if box is None:
            return web.Response(status=404)
        self.backend.stop(box)
        self.states[box.id] = "stopped"
        return web.Response()

    async def _start(self, request: web.Request) -> web.Response:
        box = self._box(request)
        if box is None:
            return web.Response(status=404)
        self.states[box.id] = "started"
        return web.Response()

    async def _snapshot(self, request: web.Request) -> web.Response:
        box = self._box(request)
        if box is None:
            return web.Response(status=404)
        want = _Named.model_validate_json(await request.read())
        self.backend.snapshot(box, want.name, frozen=False)
        return self._dto(box)

    async def _get_snapshot(self, request: web.Request) -> web.Response:
        ref = request.match_info["ref"]
        snap = self.backend.snaps.get(ref) or self.backend.find_snapshot(ref)
        if snap is None:
            return web.Response(status=404)
        return web.json_response({"id": snap.id, "name": snap.key or snap.id, "state": "active"})

    async def _delete_snapshot(self, request: web.Request) -> web.Response:
        gone = not self.backend.delete_snapshot(request.match_info["ref"])
        return web.Response(status=404 if gone else 200)

    async def _upload(self, request: web.Request) -> web.Response:
        box, path = self._box(request, "box"), request.query["path"]
        form = await request.post()
        field = form["file"]
        if box is None or not isinstance(field, web.FileField):
            return web.Response(status=404)
        if self.backend.is_directory(box, path):
            return web.Response(status=400)
        self.backend.write(box, path, field.file.read())
        return web.Response()

    async def _download(self, request: web.Request) -> web.Response:
        box, path = self._box(request, "box"), request.query["path"]
        data = None if box is None else self.backend.read(box, path)
        return web.Response(status=404) if data is None else web.Response(body=data)

    async def _ok(self, _request: web.Request) -> web.Response:
        return web.Response()

    async def _kill(self, request: web.Request) -> web.Response:
        box = self._box(request, "box")
        if box is not None:
            self.backend.signal(box, request.match_info["sid"])
        return web.Response()

    async def _exec(self, request: web.Request) -> web.Response:
        box = self._box(request, "box")
        if box is None:
            return web.Response(status=404)
        line = shlex.split(_Exec.model_validate_json(await request.read()).command)
        assert line[:4] == ["/bin/sh", "-c", PREFIX, "threads"], line
        env_file, argv = line[4], line[6:]
        env: dict[str, str] = {}
        if env_file:
            for text in box.files.pop(env_file).decode().splitlines():
                name, _, value = text.partition("=")
                env[name] = shlex.split(value)[0] if value else ""
        cmd = f"cmd_{len(self.commands)}"
        self.commands[cmd] = self.backend.run(box, argv, env, tag=request.match_info["sid"])
        return web.json_response({"cmdId": cmd})

    async def _command(self, request: web.Request) -> web.Response:
        proc = self.commands[request.match_info["cmd"]]
        return web.json_response({"exitCode": proc.exit.result() if proc.exit.done() else None})

    async def _logs(self, request: web.Request) -> web.WebSocketResponse:
        proc = self.commands[request.match_info["cmd"]]
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if proc.stdout:
            await ws.send_bytes(STDOUT + proc.stdout)
        if proc.stderr:
            await ws.send_bytes(STDERR + proc.stderr)
        await asyncio.shield(proc.exit)
        await ws.close()
        return ws


@asynccontextmanager
async def serve(backend: FakeBackend, name: str) -> AsyncGenerator[DaytonaSandbox]:
    """The adapter wired to a mocked Daytona over loopback."""
    server = DaytonaServer(backend)
    async with TestServer(server.app, host="127.0.0.1") as test:
        server.base = str(test.make_url("")).rstrip("/")
        sandbox = DaytonaSandbox(
            API_KEY,
            api_url=server.base,
            name=name,
            poll_s=0.0,
            wait_s=5.0,
            traces=[server.crash_trace()],
        )
        try:
            yield sandbox
        finally:
            await sandbox.aclose()
