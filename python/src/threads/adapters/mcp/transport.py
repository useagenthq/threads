"""Where an MCP client fences: at the real transport, never before the SDK call.

- HTTP: the SDK sends through our httpx transport. Each request re-checks the fence when the
  transport gets it, after the SDK's own queueing; a refused request is answered locally with a
  JSON-RPC error and nothing is written. The connection's send point re-checks once more and
  raises, so a lease lost in the pool wait still sends nothing (the session then fails closed).
- stdio: the host spawns the server and writes each message to its stdin only after the fence
  passes; a refused request is answered locally the same way.

A locally refused request carries `FENCE_REFUSED`: the tool reports it as never sent.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Final

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.shared.message import SessionMessage
from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest
from pydantic import ValidationError

type Fence = Callable[[], Awaitable[bool]]
type Streams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]
]

FENCE_REFUSED: Final = -32050
"""The JSON-RPC error code of a request the fence kept from being written."""
_LINE_LIMIT: Final = 16 * 1024 * 1024


class FenceRefusedError(Exception):
    """The fence failed at a connection's send point: that request was not written."""


def refusal(request_id: str | int) -> JSONRPCMessage:
    error = ErrorData(code=FENCE_REFUSED, message="stale_epoch: this run no longer owns the branch")
    return JSONRPCMessage(JSONRPCError(jsonrpc="2.0", id=request_id, error=error))


class FencedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, fence: Fence) -> None:
        self._inner = inner
        self._fence = fence

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not await self._fence():
            return _refused(request)
        request.extensions["trace"] = self._trace
        return await self._inner.handle_async_request(request)

    async def _trace(self, event: str, _info: Mapping[str, object]) -> None:
        if event.endswith(".send_request_headers.started") and not await self._fence():
            raise FenceRefusedError("lease lost before sending")

    async def aclose(self) -> None:
        await self._inner.aclose()


def _refused(request: httpx.Request) -> httpx.Response:
    """A JSON-RPC error for a refused POST request; any other refused request is a 409."""
    if request.method == "POST":
        try:
            message = JSONRPCMessage.model_validate_json(request.content)
        except ValidationError:
            message = None
        if message is not None and isinstance(message.root, JSONRPCRequest):
            body = refusal(message.root.id).model_dump(by_alias=True, exclude_none=True)
            return httpx.Response(200, json=body, headers={"content-type": "application/json"})
    return httpx.Response(409, text="stale_epoch")


@asynccontextmanager
async def stdio(
    command: str, args: tuple[str, ...], env: Mapping[str, str], fence: Fence
) -> AsyncGenerator[Streams]:
    """A host-side stdio server bridged to the SDK's session streams, fenced per write."""
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(env),
        limit=_LINE_LIMIT,
    )
    stdin, stdout = proc.stdin, proc.stdout
    if stdin is None or stdout is None:
        raise AssertionError("both pipes were requested")
    inbox, read = anyio.create_memory_object_stream[SessionMessage | Exception](16)
    write, outbox = anyio.create_memory_object_stream[SessionMessage](16)

    async def pump_in() -> None:
        async with inbox:
            async for line in stdout:
                try:
                    await inbox.send(SessionMessage(JSONRPCMessage.model_validate_json(line)))
                except ValidationError as error:
                    await inbox.send(error)

    async def pump_out() -> None:
        async with outbox:
            async for item in outbox:
                message = item.message.root
                if not await fence():
                    if isinstance(message, JSONRPCRequest):
                        await inbox.send(SessionMessage(refusal(message.id)))
                    continue
                line = item.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                stdin.write(json.dumps(line).encode("utf-8") + b"\n")
                await stdin.drain()

    tasks = (asyncio.create_task(pump_in()), asyncio.create_task(pump_out()))
    try:
        yield read, write
    finally:
        for task in tasks:
            task.cancel()
        stdin.close()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        await proc.wait()
