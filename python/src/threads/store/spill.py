"""An artifact written a chunk at a time on the store's thread."""

from threads.store.artifacts import ArtifactSink
from threads.store.worker import Worker


class Spill:
    def __init__(self, worker: Worker, sink: ArtifactSink) -> None:
        self._worker = worker
        self._sink = sink

    async def write(self, chunk: bytes) -> None:
        await self._worker.call(lambda _: self._sink.write(chunk))

    async def commit(self) -> str:
        """The bytes as a durable artifact; returns their sha256."""
        return await self._worker.call(lambda _: self._sink.commit())

    async def discard(self) -> None:
        await self._worker.call(lambda _: self._sink.discard())
