"""The built-in providers pass the published provider suites, and the framework's guard enforces
scope, bounds and error values whatever a provider does."""

import asyncio
import sqlite3
from collections.abc import Sequence

from hypothesis import given
from hypothesis import strategies as st

from threads.memory import passages
from threads.memory.conformance import A, B, knowledge_suite, memory_suite
from threads.memory.guard import Binder, scoped_knowledge, scoped_memory
from threads.memory.local_knowledge import LocalKnowledge, local_knowledge
from threads.memory.local_memory import FOREVER_MS, local_memory
from threads.memory.types import (
    Binding,
    MemoryHit,
    MemoryRecord,
    Outcome,
    ProviderError,
    RecordRef,
    Scope,
)
from threads.result import Err, Ok
from threads.store import SqliteStore


async def _store() -> SqliteStore:
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    return opened.value


def test_local_memory_passes_the_memory_suite() -> None:
    assert asyncio.run(memory_suite(lambda store: local_memory().bind(store))) == ()


def test_local_knowledge_passes_the_knowledge_suite() -> None:
    suite = knowledge_suite(lambda store: local_knowledge(paths=[]).bind(store))
    assert asyncio.run(suite) == ()


class _Leaky:
    """A third-party provider that ignores scope and echoes a forged binding."""

    def __init__(self, forged: Binding) -> None:
        self.forged = forged

    async def remember(self, scope: Scope, record: MemoryRecord, key: str) -> Outcome[RecordRef]:
        return Ok(RecordRef(id="x", version="1"))

    async def recall(self, scope: Scope, query: str, *, k: int = 5) -> Outcome[Sequence[MemoryHit]]:
        hit = MemoryHit(
            id="a1",
            version="1",
            text="tenant A's secret",
            score=1.0,
            origin="user",
            binding=self.forged,
        )
        return Ok((hit,))

    async def forget(self, scope: Scope, id: str, key: str) -> Outcome[None]:
        raise RuntimeError("boom")


def test_a_foreign_binding_is_dropped_and_audited_never_returned() -> None:
    async def main() -> None:
        store = await _store()
        issued = await store.bindings("memory").issue("tenant_a", "support", "user_1", "k")
        leaky = _Leaky(Binding(namespace=issued[0], record_id=issued[1]))
        b = scoped_memory(leaky, Binder(store.bindings("memory"), B, lambda: 7))
        assert await b.recall("anything", 5) == Ok([])
        a = scoped_memory(leaky, Binder(store.bindings("memory"), A, lambda: 7))
        own = await a.recall("anything", 5)
        assert isinstance(own, Ok)
        assert [h.text for h in own.value] == ["tenant A's secret"]

        def audit(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
            return conn.execute("SELECT tenant_id, code, at FROM provider_audit").fetchall()

        assert await store.run(audit) == [("tenant_b", "scope_violation", 7)]
        await store.close()

    asyncio.run(main())


def test_a_provider_that_raises_or_hangs_is_a_typed_error() -> None:
    class _Slow(_Leaky):
        async def recall(
            self, scope: Scope, query: str, *, k: int = 5
        ) -> Outcome[Sequence[MemoryHit]]:
            await asyncio.sleep(1)
            return Ok(())

    async def main() -> None:
        store = await _store()
        forged = Binding(namespace="n", record_id="r")
        raised = scoped_memory(_Leaky(forged), Binder(store.bindings("memory"), A, lambda: 0))
        assert await raised.forget("a1", "k") == Err(
            ProviderError("unavailable", "RuntimeError: boom")
        )
        slow = scoped_memory(_Slow(forged), Binder(store.bindings("memory"), A, lambda: 0))
        got = await type(slow)(slow.provider, slow.owner, timeout_s=0.01).recall("q", 5)
        assert isinstance(got, Err)
        assert got.error.code == "timeout"
        await store.close()

    asyncio.run(main())


def test_the_knowledge_index_rebuilds_to_the_same_results() -> None:
    async def main() -> None:
        store = await _store()
        provider = await LocalKnowledge(()).bind(store)
        k = scoped_knowledge(provider, Binder(store.bindings("knowledge"), A, lambda: 0))
        text = b"Alpha beta.\n\nGamma delta refunds.\n\nRefunds are quick."
        assert isinstance(await k.ingest("doc", "text/plain", text, None, "k1"), Ok)
        before = await k.search("refunds", 5, None, None)
        await provider.rebuild_index()
        assert await k.search("refunds", 5, None, None) == before
        assert isinstance(before, Ok)
        assert len(before.value) == 2  # noqa: PLR2004 - two passages mention refunds
        await store.close()

    asyncio.run(main())


def test_knowledge_get_is_scoped() -> None:
    async def main() -> None:
        store = await _store()
        provider = await LocalKnowledge(()).bind(store)
        a = scoped_knowledge(provider, Binder(store.bindings("knowledge"), A, lambda: 0))
        b = scoped_knowledge(provider, Binder(store.bindings("knowledge"), B, lambda: 0))
        v = await a.ingest("doc", "text/plain", b"hello", None, "k1")
        assert isinstance(v, Ok)
        doc = await a.get("doc", v.value.version)
        assert isinstance(doc, Ok)
        assert doc.value.content_ref.bytes == len(b"hello")
        other = await b.get("doc", v.value.version)
        assert isinstance(other, Err)
        assert other.error.code == "not_found"
        await store.close()

    asyncio.run(main())


@given(st.lists(st.text(), max_size=6))
def test_passage_spans_are_the_exact_utf8_bytes(parts: list[str]) -> None:
    text = "\n\n".join(parts)
    raw = text.encode("utf-8")
    for (start, end), body in passages.split(text):
        assert raw[start:end].decode("utf-8") == body
        assert body.strip() == body
        assert body


def test_forgetting_an_unknown_id_is_invalid() -> None:
    async def main() -> None:
        store = await _store()
        provider = await local_memory().bind(store)
        missing = await provider.forget(A, "no-such-id", "k1")
        assert isinstance(missing, Err)
        assert missing.error.code == "invalid"
        await store.close()

    asyncio.run(main())


def test_local_memory_declares_idempotent_keyed_writes() -> None:
    provider = local_memory()
    assert provider.write_effect == "idempotent"
    assert provider.dedup_window_ms == FOREVER_MS


def test_knowledge_revision_takes_the_scope() -> None:
    async def main() -> None:
        store = await _store()
        provider = await LocalKnowledge(()).bind(store)
        assert await provider.revision(A) == Ok(0)
        await store.close()

    asyncio.run(main())
