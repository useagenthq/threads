"""The Docker Engine API, mocked over a FakeBackend behind an httpx transport that emits the
header trace the adapter fences at. Only a request that passed the fence reaches the backend.

It emulates what the supervisor makes the daemon look like: a container that starts stopped,
one exec at a time carrying `state/records/<key-hash>.json` and `state/generation` in its
archive, and `supervise --terminate` killing the key's process group and everything uid 1000
left behind.
"""

import io
import json
import posixpath
import tarfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from docker_bytes import GENERATION, frame, tar_of, tools
from sandbox_backend import Box, FakeBackend, LostAnswerError, Proc, UnavailableError

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.docker import records
from threads.adapters.sandboxes.docker.sandbox import DockerSandbox

ARCH = "arm64"
KILLED = 137
_JSON = {"content-type": "application/json"}
_ROOTS = ("/", "/tmp", "/workspace", "/run/threads")  # noqa: S108 - paths inside a container

type Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class Exec:
    container: "Container"
    cmd: tuple[str, ...]
    env: Mapping[str, str]
    proc: Proc | None = None
    code: int | None = None
    frames: bytes = b""
    supervised: bool = False
    """`supervise` always exits 0 itself, so a supervised exec's status is never the
    command's: the command's code reaches the host through its record."""


@dataclass
class Container:
    name: str
    box: Box
    running: bool = False
    generation: str = GENERATION
    dirs: set[str] = field(default_factory=lambda: set(_ROOTS))
    keys: dict[str, Proc] = field(default_factory=dict[str, Proc])
    deadlines: dict[str, int] = field(default_factory=dict[str, int])
    terminated: set[str] = field(default_factory=set[str])

    def record(self, key: str) -> Mapping[str, object] | None:
        proc = self.keys.get(key)
        if proc is None:
            return None
        if not proc.running:
            # The supervisor's one sweep: every uid-1000 process goes with the command.
            for orphan in self.box.orphans:
                _end(orphan)
        ended = "terminated" if key in self.terminated else "exited"
        state = "running" if proc.running else ended
        return {
            "key": key,
            "generation": self.generation,
            "state": state,
            "child_pid": 7,
            "supervisor_pid": 6,
            "supervisor_start": 100,
            "deadline_ms": self.deadlines.get(key, 3_600_000),
            "exit_code": 0 if proc.running else proc.exit.result(),
        }


class DockerEngine:
    """The endpoints the adapter uses. Test knobs are the public attributes."""

    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.containers: dict[str, Container] = {}
        self.created: list[dict[str, Any]] = []
        self.injected: list[bytes] = []
        self.pulls: list[str] = []
        self.warnings: tuple[str, ...] = ()
        self.conflict_names: set[str] = set()
        self.missing_image: str | None = None
        self.arch = ARCH
        self.check_stdout: bytes | None = None
        self.bad_record: bytes | None = None
        self.corrupt_archive = False
        self.too_old = False
        self.refuse_admission = False
        self.supervisor_dies = False
        self.drop_records = False
        self.started: list[tuple[str, tuple[str, ...]]] = []
        self._execs: dict[str, Exec] = {}
        self._next = 0

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one per route
        if self.too_old:
            # Measured on Docker 29.8.0 (MinAPIVersion 1.40).
            return _json(
                400,
                {
                    "message": "client version 1.30 is too old. Minimum supported API version "
                    "is 1.40, please upgrade your client to a newer version"
                },
            )
        parts = request.url.path.removeprefix("/v1.44").strip("/").split("/")
        match [request.method, *parts]:
            case ["POST", "containers", "create"]:
                return self._create(request)
            case ["GET", "containers", "json"]:
                return self._find(request)
            case ["POST", "images", "create"]:
                self.pulls.append(request.url.params["fromImage"])
                self.missing_image = None
                return httpx.Response(200, content=b'{"status":"Downloaded"}\n')
            case ["GET", "images", *rest] if rest[-1] == "json":
                return _json(200, {"Architecture": self.arch})
            case [method, "containers", name, *rest]:
                return self._container(request, method, name, rest)
            case ["POST", "exec", ident, "start"]:
                return self._start_exec(ident)
            case ["GET", "exec", ident, "json"]:
                made = self._execs[ident]
                running = made.proc is not None and made.proc.running
                return _json(200, {"Running": running, "ExitCode": None if running else made.code})
            case ["DELETE", "volumes", _name]:
                return httpx.Response(204)
            case _:
                return _json(400, {"message": f"unexpected {request.method} {request.url}"})

    # The control plane.

    def _create(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params["name"]
        if name in self.conflict_names:
            return _json(409, {"message": f"the name {name} is already in use"})
        body = json.loads(request.content)
        self.created.append(body)
        if self.missing_image == body["Image"]:
            return _json(404, {"message": f"No such image: {body['Image']}"})
        key = str(body["Labels"]["threads.operation_key"])
        env = dict(pair.split("=", 1) for pair in body["Env"])
        try:
            box = self.backend.create(key, env)
        except LostAnswerError as lost:
            self.containers[name] = Container(name, _renamed(self.backend, str(lost.args[0]), name))
            raise
        self.containers[name] = Container(name, _renamed(self.backend, box.id, name))
        return _json(201, {"Id": name, "Warnings": list(self.warnings)})

    def _find(self, request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.url.params["filters"])
        key = str(filters["label"][0]).removeprefix("threads.operation_key=")
        box = self.backend.find(key)
        rows = [
            {"Id": c.box.id, "Names": [f"/{c.name}"]}
            for c in self.containers.values()
            if box is not None and c.box is box
        ]
        return _json(200, rows)

    def _container(  # noqa: PLR0911 - one per route
        self, request: httpx.Request, method: str, name: str, rest: Sequence[str]
    ) -> httpx.Response:
        found = self.containers.get(name)
        if method == "DELETE":
            if found is None or not self.backend.kill(found.box.id):
                return _json(404, {"message": f"no container {name}"})
            del self.containers[name]
            return httpx.Response(204)
        if found is None or not found.box.alive:
            return _json(404, {"message": f"no container {name}"})
        match [method, *rest]:
            case ["GET", "json"]:
                return _json(200, described(found))
            case ["POST", "start"]:
                found.running = True
                return httpx.Response(204)
            case ["PUT", "archive"]:
                return self._put(found, request)
            case ["GET", "archive"]:
                return self._get(found, request.url.params["path"])
            case ["POST", "exec"]:
                return self._make_exec(found, json.loads(request.content))
            case _:
                return _json(400, {"message": f"unexpected {method} {name} {rest}"})

    # Archives.

    def _put(self, container: Container, request: httpx.Request) -> httpx.Response:
        at = request.url.params["path"]
        if at not in container.dirs:
            return _json(404, {"message": f"{at} does not exist"})
        with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:") as tar:
            for info in tar:
                path = posixpath.normpath(f"{at}/{info.name}")
                if info.isdir():
                    container.dirs.add(path)
                    continue
                body = tar.extractfile(info)
                data = b"" if body is None else body.read()
                if at == "/run/threads":
                    self.injected.append(data)
                else:
                    self.backend.write(container.box, path, data)
        return httpx.Response(200)

    def _get(self, container: Container, at: str) -> httpx.Response:
        if at == "/run/threads/state":
            if self.drop_records:
                return httpx.Response(200, content=tar_of({"state/generation": b"boot-0000 4242"}))
            return httpx.Response(200, content=state_tar(container, self.bad_record))
        if self.corrupt_archive:
            return httpx.Response(200, content=b"not a tar at all")
        data = self.backend.read(container.box, at)
        if data is None:
            if self.backend.is_directory(container.box, at) or at in container.dirs:
                return httpx.Response(200, content=tar_of({}, dirs=(posixpath.basename(at),)))
            return _json(404, {"message": f"no {at}"})
        return httpx.Response(200, content=tar_of({posixpath.basename(at): data}))

    # Execs.

    def _make_exec(self, container: Container, body: "dict[str, Any]") -> httpx.Response:
        self._next += 1
        ident = f"exec_{self._next}"
        cmd = tuple(str(a) for a in body["Cmd"])
        env = dict(str(p).split("=", 1) for p in body["Env"])
        self.started.append((str(body["User"]), cmd))
        self._execs[ident] = Exec(container, cmd, env)
        return _json(201, {"Id": ident})

    def _start_exec(self, ident: str) -> httpx.Response:
        made = self._execs[ident]
        match made.cmd:
            case (records.SUPERVISE, "--check"):
                made.code, made.frames = 0, frame(1, self.check_stdout or tools())
            case (records.SUPERVISE, "--terminate", key):
                made.code = _terminate(made.container, key)
            case (records.SUPERVISE, _key, _deadline, *_argv) if self.refuse_admission:
                # What the supervisor does when a live command already holds the container.
                made.code = 125
                made.frames = frame(2, b"threads: admission refused\n")
            case (records.SUPERVISE, _key, _deadline, *_argv) if self.supervisor_dies:
                # Every `die()` in mode_run exits 1, and every one of them is pre-record.
                made.code = 1
                made.frames = frame(2, b"threads: the container is not ready\n")
            case (records.SUPERVISE, key, deadline, *argv):
                made.container.deadlines[key] = int(deadline)
                made.proc = self.backend.run(made.container.box, argv, made.env, key)
                made.container.keys[key] = made.proc
                made.supervised = True
            case argv:
                made.proc = self.backend.run(made.container.box, argv, made.env)
        return httpx.Response(200, content=_stream(made))


def described(container: Container) -> Mapping[str, object]:
    return {
        "Id": container.box.id,
        "Name": f"/{container.name}",
        "State": {"Running": container.running, "Status": "running"},
        "HostConfig": {"NanoCpus": 0, "Memory": 0, "PidsLimit": 1024},
    }


def state_tar(container: Container, bad: bytes | None = None) -> bytes:
    files = {"state/generation": container.generation.encode()}
    for key in container.keys:
        record = container.record(key)
        if record is not None:
            files[f"state/records/{key}.json"] = bad or json.dumps(record).encode()
    return tar_of(files, dirs=("state", "state/records"))


def _renamed(backend: FakeBackend, ident: str, name: str) -> Box:
    """A container's identity is its name: the backend's box takes it, so a test reads the
    sandbox id in `backend.boxes` as every other adapter's."""
    box = backend.boxes.pop(ident)
    box.id = name
    backend.boxes[name] = box
    return box


def _terminate(container: Container, key: str) -> int:
    """The D-2 probe: exit 0 when K isn't live, else the group is killed and 11 reported."""
    proc = container.keys.get(key)
    if proc is None or not proc.running:
        return 0
    _end(proc)
    for orphan in container.box.orphans:
        _end(orphan)
    container.terminated.add(key)
    return 11


async def _stream(made: Exec) -> AsyncIterator[bytes]:
    """The multiplexed stream, then the exit code the inspect reports."""
    if made.frames:
        yield made.frames
    if made.proc is None:
        return
    if made.proc.stdout:
        yield frame(1, made.proc.stdout)
    if made.proc.stderr:
        yield frame(2, made.proc.stderr)
    ended = await made.proc.exit
    made.code = 0 if made.supervised else ended


def _end(proc: Proc) -> None:
    if proc.running:
        proc.exit.set_result(KILLED)


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, headers=_JSON, content=json.dumps(body).encode())


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
            return _json(503, {"message": str(refused)})


def adapter(  # noqa: PLR0913, PLR0917 - the adapter's options a test varies
    backend: FakeBackend,
    name: str,
    engine: DockerEngine | None = None,
    made: list[TracedTransport] | None = None,
    cpus: float | None = None,
    memory_mb: int | None = None,
) -> DockerSandbox:
    """The adapter over `backend`; `engine` replaces the mocked daemon."""
    daemon = engine or DockerEngine(backend)

    def transport() -> httpx.AsyncBaseTransport:
        one = TracedTransport(backend, daemon.handle)
        if made is not None:
            made.append(one)
        return one

    return DockerSandbox(
        transport=transport, name=name, cpus=cpus, memory_mb=memory_mb, poll_s=0.0, wait_s=1.0
    )


@asynccontextmanager
async def make(backend: FakeBackend, name: str) -> AsyncGenerator[DockerSandbox]:
    async with holding():
        yield adapter(backend, name)
