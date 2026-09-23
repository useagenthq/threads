"""The framework side of every provider call.

A provider is only storage. Here the host issues each write's binding before the provider sees
it, parses whatever the provider returns, keeps only items whose binding the host recorded for
the calling scope (auditing the rest), bounds what comes back, and turns a timeout or a raised
exception into a typed error. None of it depends on the provider behaving.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from threads.memory.fence import Fence, bound, refused
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
from threads.memory.types import (
    Binding,
    Doc,
    DocVersion,
    KnowledgeHit,
    KnowledgeSource,
    MemoryHit,
    MemoryRecord,
    Origin,
    Outcome,
    Provenance,
    ProviderError,
    RecordRef,
    Scope,
    knowledge_hits,
    memory_hits,
)
from threads.result import Err, Ok
from threads.store.bindings import Bindings
from threads.store.worker import Clock

TIMEOUT_S: Final = 10.0
MAX_HIT_BYTES: Final = 16_384
"""Returned context is bounded by k and by bytes."""


async def call[T](
    op: Callable[[], Awaitable[Outcome[T]]], fence: Fence, timeout_s: float
) -> Outcome[T]:
    """One provider call under the run's fence: a timeout or a raise is a typed error."""
    try:
        with bound(fence):
            return await asyncio.wait_for(op(), timeout_s)
    except TimeoutError:
        return Err(ProviderError("timeout", f"the provider didn't answer in {timeout_s}s"))
    except Exception as error:
        if refused(error):
            return Err(ProviderError("unavailable", "stale_epoch: fence refused", sent=False))
        # Provider code is trusted host code, but its failure must never crash the run.
        return Err(ProviderError("unavailable", f"{type(error).__name__}: {error}"))


@dataclass(frozen=True, slots=True)
class _Owner:
    bindings: Bindings
    scope: Scope
    clock: Clock
    fence: Fence

    async def call[T](
        self, op: Callable[[], Awaitable[Outcome[T]]], timeout_s: float
    ) -> Outcome[T]:
        return await call(op, self.fence, timeout_s)

    async def issue(self, key: str) -> Binding:
        s = self.scope
        namespace, record_id = await self.bindings.issue(s.tenant_id, s.agent, s.scope, key)
        return Binding(namespace=namespace, record_id=record_id)

    async def owns(self, binding: Binding) -> bool:
        """Whether the host issued `binding` to this scope; a foreign one is audited."""
        s = self.scope
        pair = (binding.namespace, binding.record_id)
        if pair in await self.bindings.owned(s.tenant_id, s.agent, s.scope, (pair,)):
            return True
        await self.bindings.violation(s.tenant_id, *pair, self.clock())
        return False

    async def keep[H: (MemoryHit, KnowledgeHit)](self, hits: Sequence[H], k: int) -> list[H]:
        """Hits whose binding belongs to the calling scope, at most k and MAX_HIT_BYTES."""
        s = self.scope
        pairs = tuple((h.binding.namespace, h.binding.record_id) for h in hits)
        owned = await self.bindings.owned(s.tenant_id, s.agent, s.scope, pairs)
        kept: list[H] = []
        size = 0
        for hit, pair in zip(hits, pairs, strict=True):
            if pair not in owned:
                await self.bindings.violation(s.tenant_id, *pair, self.clock())
                continue
            size += len(hit.text.encode("utf-8"))
            if len(kept) == k or size > MAX_HIT_BYTES:
                break
            kept.append(hit)
        return kept


@dataclass(frozen=True, slots=True)
class ScopedMemory:
    """A memory provider bound to one host-issued scope."""

    provider: MemoryProvider
    owner: _Owner
    timeout_s: float = TIMEOUT_S

    async def _call[T](self, op: Callable[[], Awaitable[Outcome[T]]]) -> Outcome[T]:
        return await self.owner.call(op, self.timeout_s)

    async def remember(
        self, text: str, origin: Origin, provenance: Provenance, key: str
    ) -> Outcome[RecordRef]:
        binding = await self.owner.issue(key)
        record = MemoryRecord(text=text, origin=origin, provenance=provenance, binding=binding)
        scope = self.owner.scope
        return await self._call(lambda: self.provider.remember(scope, record, key))

    async def recall(self, query: str, k: int) -> Outcome[list[MemoryHit]]:
        scope = self.owner.scope
        got = await self._call(lambda: self.provider.recall(scope, query, k=k))
        if isinstance(got, Err):
            return got
        parsed = memory_hits(got.value)
        return parsed if isinstance(parsed, Err) else Ok(await self.owner.keep(parsed.value, k))

    async def forget(self, id: str, key: str) -> Outcome[None]:
        scope = self.owner.scope
        return await self._call(lambda: self.provider.forget(scope, id, key))


@dataclass(frozen=True, slots=True)
class ScopedKnowledge:
    """A knowledge provider bound to one host-issued scope. Only the host ingests."""

    provider: KnowledgeProvider
    owner: _Owner
    timeout_s: float = TIMEOUT_S

    async def _call[T](self, op: Callable[[], Awaitable[Outcome[T]]]) -> Outcome[T]:
        return await self.owner.call(op, self.timeout_s)

    async def ingest(
        self, source_id: str, media_type: str, content: bytes, location: str | None, key: str
    ) -> Outcome[DocVersion]:
        binding = await self.owner.issue(key)
        source = KnowledgeSource(
            source_id=source_id,
            media_type=media_type,
            content=content,
            location=location,
            binding=binding,
        )
        scope = self.owner.scope
        return await self._call(lambda: self.provider.ingest(scope, source, key))

    async def remove(self, doc_id: str, key: str) -> Outcome[None]:
        scope = self.owner.scope
        return await self._call(lambda: self.provider.remove(scope, doc_id, key))

    async def search(
        self, query: str, k: int, sources: Sequence[str] | None, as_of: int | None
    ) -> Outcome[list[KnowledgeHit]]:
        scope = self.owner.scope

        def op() -> Awaitable[Outcome[Sequence[KnowledgeHit]]]:
            return self.provider.search(scope, query, k=k, sources=sources, as_of=as_of)

        got = await self._call(op)
        if isinstance(got, Err):
            return got
        parsed = knowledge_hits(got.value)
        return parsed if isinstance(parsed, Err) else Ok(await self.owner.keep(parsed.value, k))

    async def get(self, doc_id: str, version: str) -> Outcome[Doc]:
        scope = self.owner.scope
        got = await self._call(lambda: self.provider.get(scope, doc_id, version))
        if isinstance(got, Err):
            return got
        if not await self.owner.owns(got.value.binding):
            return Err(ProviderError("scope_violation", f"{doc_id}@{version} is not in scope"))
        return got


async def unfenced() -> bool:
    """For providers outside a run (the conformance suites): nothing to lose."""
    return True


@dataclass(frozen=True, slots=True)
class Binder:
    """What binds a provider to one run: the host tables, the scope, the clock and the fence."""

    bindings: Bindings
    scope: Scope
    clock: Clock
    fence: Fence = unfenced


def scoped_memory(provider: MemoryProvider, by: Binder) -> ScopedMemory:
    return ScopedMemory(provider, _Owner(by.bindings, by.scope, by.clock, by.fence))


def scoped_knowledge(provider: KnowledgeProvider, by: Binder) -> ScopedKnowledge:
    return ScopedKnowledge(provider, _Owner(by.bindings, by.scope, by.clock, by.fence))
