"""`supermemory()`: a `MemoryProvider` on the official Supermemory SDK (extra `supermemory`).

Each record is one document in a container derived from the scope, with the host's binding and
origin as metadata. The SDK sends through the fenced client with its retries off; the key names
the document, so a retried write replaces the same one. Scope is still enforced by the host's
bindings, not by the container.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

import httpx
from supermemory import AsyncSupermemory, NotFoundError

from threads.adapters.memories.http import fenced_client
from threads.adapters.memories.wire import container, hit, record_id
from threads.memory.types import (
    MemoryHit,
    MemoryRecord,
    Outcome,
    ProviderError,
    RecordRef,
    Scope,
)
from threads.result import Err, Ok
from threads.secrets import Secret, credential

if TYPE_CHECKING:
    from supermemory._types import SequenceNotStr

API_KEY: Final = "SUPERMEMORY_API_KEY"


class Supermemory:
    """spec/api.json `MemoryProvider` on Supermemory (module docstring)."""

    def __init__(
        self,
        api_key: str | Secret | None = None,
        base_url: str | None = None,
        http: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.http = http
        """The transport under the fence; None opens real connections. Tests pass one."""
        self._key = credential("supermemory", "api_key", api_key, API_KEY)

    async def setup(self) -> None:
        """Resolves the key on the host, once: a missing key fails here, not mid-run."""
        self._key()

    def _client(self) -> AsyncSupermemory:
        return AsyncSupermemory(
            api_key=self._key(),
            base_url=self.base_url,
            http_client=fenced_client(self.http),
            max_retries=0,
        )

    async def remember(self, scope: Scope, record: MemoryRecord, key: str) -> Outcome[RecordRef]:
        b = record.binding
        metadata: dict[str, str | float | bool | SequenceNotStr[str]] = {
            "namespace": b.namespace,
            "record_id": b.record_id,
            "origin": record.origin,
        }
        async with self._client() as client:
            added = await client.add(
                content=record.text,
                container_tag=container(scope),
                custom_id=record_id(key),
                metadata=metadata,
            )
        return Ok(RecordRef(id=added.id, version="1"))

    async def recall(self, scope: Scope, query: str, *, k: int = 5) -> Outcome[Sequence[MemoryHit]]:
        async with self._client() as client:
            found = await client.search.execute(q=query, container_tag=container(scope), limit=k)
        hits: list[MemoryHit] = []
        for r in found.results:
            text = "\n".join(c.content for c in r.chunks if c.is_relevant) or (r.content or "")
            item = hit(r.document_id, text, r.score, r.metadata)
            if item is not None:
                hits.append(item)
        return Ok(tuple(hits))

    async def forget(self, scope: Scope, id: str, key: str) -> Outcome[None]:
        try:
            async with self._client() as client:
                await client.documents.delete(id)
        except NotFoundError:
            return Err(ProviderError("not_found", f"no memory {id}"))
        return Ok(None)


def supermemory(*, api_key: str | Secret | None = None, base_url: str | None = None) -> Supermemory:
    """Pure. `api_key` defaults to `secret("SUPERMEMORY_API_KEY")`, resolved on the host at
    setup."""
    return Supermemory(api_key, base_url)
