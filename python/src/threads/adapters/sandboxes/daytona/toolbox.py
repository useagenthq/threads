"""One Daytona sandbox's toolbox through the official generated async client: files, and
commands run in a per-process-key session whose logs stream over a WebSocket.

Session commands take no env or cwd, so exec uploads the env as a file (values travel through
the provider's file API, never argv) that a small shell prefix sources, removes, then `cd`s and
execs the command."""

import asyncio
import shlex
from collections.abc import Mapping, Sequence

import aiohttp
from daytona_toolbox_api_client_async import (
    ApiClient,
    Configuration,
    FileSystemApi,
    ProcessApi,
)
from daytona_toolbox_api_client_async.models.create_session_request import (
    CreateSessionRequest,
)
from daytona_toolbox_api_client_async.models.session_execute_request import (
    SessionExecuteRequest,
)

from threads.adapters.sandboxes.daytona.control import NOT_FOUND, StatusError, body
from threads.adapters.sandboxes.daytona.logs import Demux
from threads.adapters.sandboxes.daytona.wire import Command, Executed
from threads.adapters.sandboxes.posix import RUN_DIR
from threads.adapters.sandboxes.streams import Pipe, pump
from threads.log.digest import sha256_hex
from threads.sandbox.protocol import ExecOutput

PREFIX = (
    'if [ -n "$1" ]; then set -a; . "$1"; set +a; rm -f "$1"; fi; '
    'cd "$2" || exit 1; shift 2; exec "$@"'
)
"""$1 the env file (empty: none), $2 the cwd, then the command."""

_BAD_REQUEST = 400


def session_id(process_key: str) -> str:
    return f"threads-{sha256_hex(process_key.encode('utf-8'))[:32]}"


def command_line(argv: Sequence[str], env_file: str, cwd: str) -> str:
    return shlex.join(["/bin/sh", "-c", PREFIX, "threads", env_file, cwd, *argv])


def env_file(env: Mapping[str, str]) -> bytes:
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()).encode("utf-8")


class Toolbox:
    def __init__(  # noqa: PLR0913 - the toolbox's address, session, pacing and pumps
        self,
        proxy_url: str,
        sandbox_id: str,
        *,
        session: aiohttp.ClientSession,
        poll_s: float,
        wait_s: float,
        tasks: set[asyncio.Task[None]],
    ) -> None:
        self._host = f"{proxy_url.rstrip('/')}/{sandbox_id}"
        api = ApiClient(Configuration(host=self._host))
        api.rest_client.pool_manager = session
        self._files, self._process = FileSystemApi(api), ProcessApi(api)
        self._session = session
        self._poll_s, self._wait_s = poll_s, wait_s
        self._tasks = tasks
        """The adapter's running pumps, cancelled when it closes."""

    async def upload(self, path: str, data: bytes) -> None:
        await body(self._files.upload_file_without_preload_content(path, data))

    async def download(self, path: str) -> bytes:
        return await body(self._files.download_file_without_preload_content(path))

    async def start(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: str, key: str
    ) -> ExecOutput:
        """Runs `argv` in session `key`; returns once its log stream is open."""
        sid = session_id(key)
        fed = ""
        if env:
            fed = f"{RUN_DIR}/{sid}.env"
            await self.upload(fed, env_file(env))
        try:
            await body(
                self._process.create_session_without_preload_content(
                    CreateSessionRequest(session_id=sid)
                )
            )
        except StatusError as error:
            if error.status not in (_BAD_REQUEST, 409):  # it exists: this key ran before
                raise
        request = SessionExecuteRequest(command=command_line(argv, fed, cwd), run_async=True)
        sent = self._process.session_execute_command_without_preload_content(sid, request)
        cmd = Executed.model_validate_json(await body(sent)).cmd_id
        url = f"{self._host}/process/session/{sid}/command/{cmd}/logs?follow=true"
        ws = await self._session.ws_connect(url.replace("http", "ws", 1))
        pipe = Pipe()
        task = pump(pipe, lambda p: self._relay(ws, p, sid, cmd))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return pipe.output()

    async def kill(self, key: str) -> None:
        """Deletes the key's session; Daytona ends its processes. Nothing confirms the whole
        group is gone (a command can detach descendants)."""
        try:
            await body(self._process.delete_session_without_preload_content(session_id(key)))
        except StatusError as error:
            if error.status != NOT_FOUND:
                raise

    async def _relay(
        self, ws: aiohttp.ClientWebSocketResponse, pipe: Pipe, sid: str, cmd: str
    ) -> int:
        demux = Demux()
        async with ws:
            async for message in ws:
                if message.type not in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                    raise StatusError(0, f"log stream: {message.type}".encode())
                data = message.data if isinstance(message.data, bytes) else b""
                if isinstance(message.data, str):
                    data = message.data.encode("utf-8")
                for stream, chunk in demux.feed(data):
                    await (pipe.stdout if stream == "stdout" else pipe.stderr)(chunk)
        for stream, chunk in demux.flush():
            await (pipe.stdout if stream == "stdout" else pipe.stderr)(chunk)
        if ws.close_code not in (None, 1000):
            raise StatusError(0, f"log stream closed with {ws.close_code}".encode())
        return await self._exit_code(sid, cmd)

    async def _exit_code(self, sid: str, cmd: str) -> int:
        async with asyncio.timeout(self._wait_s):
            while True:
                sent = self._process.get_session_command_without_preload_content(sid, cmd)
                code = Command.model_validate_json(await body(sent)).exit_code
                if code is not None:
                    return code
                await asyncio.sleep(self._poll_s)
