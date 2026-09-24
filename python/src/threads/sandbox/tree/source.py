"""Exact reads over a chunked async byte source."""

import inspect
from collections.abc import AsyncIterable, Callable


class Source:
    """Pulls exact byte counts from chunks of any size; `offset` counts the bytes taken."""

    def __init__(self, source: AsyncIterable[bytes]) -> None:
        self._it = aiter(source)
        # A view, so taking a piece never copies the rest of the chunk.
        self._head = memoryview(b"")
        self.offset = 0

    async def next(self, most: int) -> bytes | None:
        """The next bytes, at most `most`; None at the end of the source."""
        while not self._head:
            try:
                self._head = memoryview(await anext(self._it))
            except StopAsyncIteration:
                return None
        piece, self._head = self._head[:most], self._head[most:]
        self.offset += len(piece)
        return piece.tobytes()

    async def take(self, n: int, each: Callable[[bytes], None]) -> bool:
        """Passes the next `n` bytes to `each`; False when the source ends first."""
        left = n
        while left > 0:
            piece = await self.next(left)
            if piece is None:
                return False
            each(piece)
            left -= len(piece)
        return True

    async def exact(self, n: int) -> bytes | None:
        """The next `n` bytes; None when the source ends first."""
        out = bytearray()
        return bytes(out) if await self.take(n, out.extend) else None

    async def close(self) -> None:
        """Stops a generator source early, so its producer can release what it holds."""
        it: object = self._it
        if inspect.isasyncgen(it):
            await it.aclose()
