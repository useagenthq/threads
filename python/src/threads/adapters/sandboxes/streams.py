"""One provider stream of process events split into the `ExecOutput` the protocol returns: two
byte streams and an exit code, with backpressure (a full queue stops reading the provider), so
output is never buffered whole."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from threads.sandbox.protocol import ExecOutput

_DEPTH = 16
"""Chunks buffered per stream before the pump waits for the reader."""


class StreamLostError(Exception):
    """The provider's event stream broke before the process's exit was reported: the command
    may still be running, so its outcome is unknown."""


class Pipe:
    """Fed by one pump task; read as an `ExecOutput`."""

    def __init__(self) -> None:
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue(_DEPTH)
        self._err: asyncio.Queue[bytes | None] = asyncio.Queue(_DEPTH)
        self._exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def stdout(self, chunk: bytes) -> None:
        if chunk:
            await self._out.put(chunk)

    async def stderr(self, chunk: bytes) -> None:
        if chunk:
            await self._err.put(chunk)

    async def end(self, code: int | BaseException) -> None:
        """The exit code, or why none will come. Ends both streams."""
        await self._out.put(None)
        await self._err.put(None)
        if isinstance(code, BaseException):
            self._exit.set_exception(code)
        else:
            self._exit.set_result(code)

    def output(self) -> ExecOutput:
        return ExecOutput(self._exit, _drain(self._out), _drain(self._err))


def pump(pipe: Pipe, feed: Callable[[Pipe], Awaitable[int]]) -> "asyncio.Task[None]":
    """Runs `feed` (which pushes chunks and returns the exit code) as the pipe's producer.
    A feed that fails ends the pipe with StreamLostError."""

    async def run() -> None:
        try:
            code = await feed(pipe)
        except Exception as error:
            lost = StreamLostError(str(error))
            lost.__cause__ = error
            await pipe.end(lost)
            return
        await pipe.end(code)

    return asyncio.create_task(run())


async def _drain(queue: "asyncio.Queue[bytes | None]") -> AsyncIterator[bytes]:
    while (chunk := await queue.get()) is not None:
        yield chunk
