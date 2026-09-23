"""The provider conformance suites: the same checks the built-in
providers run, for any third-party provider. Each suite takes a factory that makes a fresh
provider on a store and returns the failed checks by name; an empty tuple passes.

    assert await memory_suite(lambda store: local_memory().bind(store)) == ()
"""

from collections.abc import Awaitable, Callable

from threads.log import EventId, ThreadId
from threads.memory.guard import Binder, scoped_knowledge, scoped_memory
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
from threads.memory.types import Provenance, Scope
from threads.result import Err, Ok
from threads.store import SqliteStore

A = Scope(tenant_id="tenant_a", agent="support", scope="user_1")
B = Scope(tenant_id="tenant_b", agent="support", scope="user_1")
_PROVENANCE = Provenance(
    thread_id=ThreadId("0192e000-0000-7000-8000-000000000001"),
    event_ids=(EventId("0192e000-0000-7000-8000-000000000002"),),
)


def _clock() -> int:
    return 0


async def memory_suite(
    make: Callable[[SqliteStore], Awaitable[MemoryProvider]], *, key_conflicts: bool = True
) -> tuple[str, ...]:
    """`key_conflicts`: the provider can tell the same key with other content (a capability
    flag; the loop never reuses a key with other content, so it is not a safety rule)."""
    opened = await SqliteStore.open()
    if isinstance(opened, Err):
        raise AssertionError(opened.error.message)
    store = opened.value
    try:
        provider = await make(store)
        a = scoped_memory(provider, Binder(store.bindings("memory"), A, _clock))
        b = scoped_memory(provider, Binder(store.bindings("memory"), B, _clock))
        failed: list[str] = []
        saved = await a.remember("the user prefers tabs", "user", _PROVENANCE, "k1")
        again = await a.remember("the user prefers tabs", "user", _PROVENANCE, "k1")
        if not isinstance(saved, Ok) or again != saved:
            failed.append("same key, same content is a no-op")
        clash = await a.remember("something else", "user", _PROVENANCE, "k1")
        if key_conflicts and not (isinstance(clash, Err) and clash.error.code == "invalid"):
            failed.append("same key, other content is invalid")
        hits = await a.recall("tabs", 5)
        if not (isinstance(hits, Ok) and [h.text for h in hits.value] == ["the user prefers tabs"]):
            failed.append("recall finds a saved record in its scope")
        raw = await provider.recall(B, "tabs", k=5)
        other = await b.recall("user prefers tabs", 5)
        if not (isinstance(raw, Ok) and not raw.value and other == Ok([])):
            failed.append("another tenant never recalls it")
        if isinstance(saved, Ok):
            gone = await a.forget(saved.value.id, "k2")
            twice = await a.forget(saved.value.id, "k2")
            after = await a.recall("tabs", 5)
            if not (isinstance(gone, Ok) and isinstance(twice, Ok) and after == Ok([])):
                failed.append("a forgotten record is never recalled")
        missing = await a.forget("no-such-id", "k3")
        if not (isinstance(missing, Err) and missing.error.code == "not_found"):
            failed.append("errors are typed values")
        return tuple(failed)
    finally:
        await store.close()


async def knowledge_suite(
    make: Callable[[SqliteStore], Awaitable[KnowledgeProvider]],
) -> tuple[str, ...]:
    opened = await SqliteStore.open()
    if isinstance(opened, Err):
        raise AssertionError(opened.error.message)
    store = opened.value
    try:
        provider = await make(store)
        a = scoped_knowledge(provider, Binder(store.bindings("knowledge"), A, _clock))
        b = scoped_knowledge(provider, Binder(store.bindings("knowledge"), B, _clock))
        failed: list[str] = []
        text = "Refunds take 5 days.\n\nShipping is free over 50 euros."
        v1 = await a.ingest("policy.md", "text/markdown", text.encode(), "policy.md", "k1")
        hits = await a.search("refunds", 5, None, None)
        first = hits.value[0] if isinstance(hits, Ok) and hits.value else None
        if not (isinstance(v1, Ok) and first is not None and first.version == v1.value.version):
            failed.append("a hit names its admitted version")
        elif text.encode()[first.span.start : first.span.end].decode() != first.text:
            failed.append("a hit's span is its exact bytes")
        if (await b.search("refunds", 5, None, None)) != Ok([]):
            failed.append("another tenant never retrieves it")
        v2 = await a.ingest("policy.md", "text/markdown", b"Refunds take 9 days.", None, "k2")
        now = await a.search("refunds", 5, None, None)
        if not (isinstance(v2, Ok) and isinstance(now, Ok) and "9 days" in now.value[0].text):
            failed.append("a refreshed source is searched at its new version")
        elif isinstance(v1, Ok):
            old = await a.search("refunds", 5, None, v1.value.revision)
            if not (isinstance(old, Ok) and old.value and "5 days" in old.value[0].text):
                failed.append("a search as of a revision sees that revision")
        removed = await a.remove("policy.md", "k3")
        if not (isinstance(removed, Ok) and (await a.search("refunds", 5, None, None)) == Ok([])):
            failed.append("a removed source is excluded from new searches")
        bad = await a.ingest("scan.pdf", "application/pdf", b"%PDF", None, "k4")
        if not (isinstance(bad, Err) and bad.error.code == "invalid"):
            failed.append("a parse failure is an explicit error")
        return tuple(failed)
    finally:
        await store.close()
