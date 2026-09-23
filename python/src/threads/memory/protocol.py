"""The provider interfaces (spec/api.json `MemoryProvider`, `KnowledgeProvider`; ).

A provider only stores and searches. Scope checks, bindings, logging, trust framing and write
authority are the framework's (`threads.memory.guard`), so a swapped provider can't weaken them.
A new provider implements these methods and passes `threads.memory.conformance`.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

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
from threads.tools.specs import Writes


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


@runtime_checkable
class DeclaresWrites(Protocol):
    """A memory provider that declares its writes' effect class. One that doesn't
    is `unguarded`: an uncertain write parks, it is never retried blindly."""

    @property
    def writes(self) -> Writes: ...


@runtime_checkable
class Revisioned(Protocol):
    """A knowledge provider with a monotonic revision: snapshots record it,
    and a pinned fork searches `as_of` it. Without one, forks search the live corpus."""

    async def revision(self) -> int: ...
