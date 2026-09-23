"""An artifact written a chunk at a time on the store's thread, with every resolved secret
redacted as it streams in (C5): what is stored is what a read returns."""

from threads.redaction import StreamRedactor, generation, unchanged_since
from threads.store.artifacts import ArtifactSink
from threads.store.worker import Worker


class Spill:
    def __init__(self, worker: Worker, sink: ArtifactSink) -> None:
        self._worker = worker
        self._sink = sink
        self._redactor = StreamRedactor()
        self._since = generation()
        self.written: int = 0
        """Bytes stored so far, after redaction: the artifact's length once committed."""

    async def write(self, chunk: bytes) -> None:
        await self._store(self._redactor.feed(chunk))

    async def commit(self) -> str | None:
        """The bytes as a durable artifact; returns their sha256. Bytes already written can't be
        revisited, so a value registered while they streamed drops them: None."""
        await self._store(self._redactor.end())
        sha = await self._worker.call(lambda _: unchanged_since(self._since, self._sink.commit))
        if sha is None:
            await self.discard()
        return sha

    async def _store(self, data: bytes) -> None:
        self.written += len(data)
        await self._worker.call(lambda _: self._sink.write(data))

    async def discard(self) -> None:
        await self._worker.call(lambda _: self._sink.discard())
