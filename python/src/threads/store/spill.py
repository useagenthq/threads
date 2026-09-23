"""An artifact written a chunk at a time on the store's thread, with every resolved secret
redacted as it streams in (C5): what is stored is what a read returns."""

from threads.secrets import StreamRedactor
from threads.store.artifacts import ArtifactSink
from threads.store.worker import Worker


class Spill:
    def __init__(self, worker: Worker, sink: ArtifactSink) -> None:
        self._worker = worker
        self._sink = sink
        self._redactor = StreamRedactor()
        self.written: int = 0
        """Bytes stored so far, after redaction: the artifact's length once committed."""

    async def write(self, chunk: bytes) -> None:
        await self._store(self._redactor.feed(chunk))

    async def commit(self) -> str:
        """The bytes as a durable artifact; returns their sha256."""
        await self._store(self._redactor.end())
        return await self._worker.call(lambda _: self._sink.commit())

    async def _store(self, data: bytes) -> None:
        self.written += len(data)
        await self._worker.call(lambda _: self._sink.write(data))

    async def discard(self) -> None:
        await self._worker.call(lambda _: self._sink.discard())
