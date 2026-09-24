"""The provider interfaces (spec/api.json `MemoryProvider`, `KnowledgeProvider`).

A provider only stores and searches. Scope checks, bindings, logging, trust framing and write
authority are the framework's (`threads.memory.guard`), so a swapped provider can't weaken them.
A new provider implements these methods and passes `threads.memory.conformance`.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from threads.log import EffectClass
from threads.memory.types import (
    Doc,
    DocVersion,
    KnowledgeHit,
    KnowledgeSource,
    MemoryHit,
    MemoryRecord,
    Outcome,
    RecordRef,
    Scope,
)


class MemoryProvider(Protocol):
    async def remember(self, scope: Scope, record: MemoryRecord, key: str) -> Outcome[RecordRef]:
        """The same key with the same content is a no-op; with other content, `invalid`."""
        ...

    async def recall(
        self, scope: Scope, query: str, *, k: int = 5
    ) -> Outcome[Sequence[MemoryHit]]: ...

    async def forget(self, scope: Scope, id: str, key: str) -> Outcome[None]: ...


class KnowledgeProvider(Protocol):
    async def ingest(
        self, scope: Scope, source: KnowledgeSource, key: str
    ) -> Outcome[DocVersion]: ...

    async def remove(self, scope: Scope, doc_id: str, key: str) -> Outcome[None]: ...

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        k: int = 5,
        sources: Sequence[str] | None = None,
        as_of: int | None = None,
    ) -> Outcome[Sequence[KnowledgeHit]]:
        """`as_of`: answer as of that store revision (a pinned fork)."""
        ...

    async def get(self, scope: Scope, doc_id: str, version: str) -> Outcome[Doc]: ...

    async def revision(self, scope: Scope) -> Outcome[int]:
        """The store's monotonic revision: a snapshot records it, and a pinned fork searches
        `as_of` it. When it fails, that turn end takes no snapshot."""
        ...


@runtime_checkable
class DeclaresWrites(Protocol):
    """A memory provider that declares its writes' effect class. One that doesn't
    is `unguarded`: an uncertain write parks, it is never retried blindly."""

    @property
    def write_effect(self) -> EffectClass: ...

    @property
    def dedup_window_ms(self) -> int | None: ...
