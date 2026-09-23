"""`zep()`: a `MemoryProvider` on the official Zep Cloud SDK (extra `zep`).

Each scope is one Zep graph named by a digest of the scope; each record is a text episode whose
metadata holds the host's binding and origin. Recall searches episodes. The SDK sends through
the fenced client with its retries off. Scope is still enforced by the host's bindings.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Final

import httpx
from zep_cloud.client import AsyncZep
from zep_cloud.core.api_error import ApiError
from zep_cloud.core.request_options import RequestOptions

from threads.adapters.memories.http import fenced_client
from threads.adapters.memories.wire import container, hit
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

API_KEY: Final = "ZEP_API_KEY"
_ONCE: Final = RequestOptions(max_retries=0)


class Zep:
    """spec/api.json `MemoryProvider` on Zep Cloud (module docstring)."""

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
        self._key = credential("zep", "api_key", api_key, API_KEY)

    async def setup(self) -> None:
        """Resolves the key on the host, once: a missing key fails here, not mid-run."""
        self._key()

    @asynccontextmanager
    async def _client(self) -> AsyncGenerator[AsyncZep]:
        async with fenced_client(self.http) as http:
            yield AsyncZep(api_key=self._key(), base_url=self.base_url, httpx_client=http)

    async def remember(self, scope: Scope, record: MemoryRecord, key: str) -> Outcome[RecordRef]:
        graph = container(scope)
        b = record.binding
        metadata: dict[str, object] = {
            "namespace": b.namespace,
            "record_id": b.record_id,
            "origin": record.origin,
        }
        async with self._client() as client:
            add = client.graph.add
            try:
                episode = await add(
                    data=record.text,
                    type="text",
                    graph_id=graph,
                    metadata=metadata,
                    request_options=_ONCE,
                )
            except ApiError as error:
                if error.status_code != HTTPStatus.NOT_FOUND:
                    raise
                # The scope's graph doesn't exist yet: a 404 wrote nothing; create it and add.
                await client.graph.create(graph_id=graph, request_options=_ONCE)
                episode = await add(
                    data=record.text,
                    type="text",
                    graph_id=graph,
                    metadata=metadata,
                    request_options=_ONCE,
                )
        return Ok(RecordRef(id=episode.uuid_, version="1"))

    async def recall(self, scope: Scope, query: str, *, k: int = 5) -> Outcome[Sequence[MemoryHit]]:
        try:
            async with self._client() as client:
                found = await client.graph.search(
                    query=query,
                    graph_id=container(scope),
                    scope="episodes",
                    limit=k,
                    request_options=_ONCE,
                )
        except ApiError as error:
            if error.status_code != HTTPStatus.NOT_FOUND:
                raise
            return Ok(())  # no graph: nothing was ever saved in this scope
        hits: list[MemoryHit] = []
        for e in found.episodes or []:
            item = hit(e.uuid_, e.content, e.score or e.relevance or 0.0, e.metadata)
            if item is not None:
                hits.append(item)
        return Ok(tuple(hits))

    async def forget(self, scope: Scope, id: str, key: str) -> Outcome[None]:
        try:
            async with self._client() as client:
                await client.graph.episode.delete(id, request_options=_ONCE)
        except ApiError as error:
            if error.status_code != HTTPStatus.NOT_FOUND:
                raise
            return Err(ProviderError("not_found", f"no memory {id}"))
        return Ok(None)


def zep(*, api_key: str | Secret | None = None, base_url: str | None = None) -> Zep:
    """Pure. `api_key` defaults to `secret("ZEP_API_KEY")`, resolved on the host at setup."""
    return Zep(api_key, base_url)
