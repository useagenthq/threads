"""supermemory() and zep() on their official SDKs, against recorded HTTP (no network): the host's
binding rides as metadata, items without it are dropped, the fence refuses before any byte is
written, and mem0() refuses at setup. Live runs are behind THREADS_LIVE=1."""

import asyncio
import json
import os
from collections.abc import Callable

import httpx
import pytest
from pydantic import JsonValue

from threads import ConfigError
from threads.log import EventId, ThreadId
from threads.mem0 import mem0
from threads.memory.conformance import A, memory_suite
from threads.memory.guard import Binder, scoped_memory
from threads.memory.protocol import MemoryProvider
from threads.memory.types import MemoryHit, Outcome, Provenance, RecordRef
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.supermemory import supermemory
from threads.zep import zep

PROVENANCE = Provenance(
    thread_id=ThreadId("0192e000-0000-7000-8000-000000000001"),
    event_ids=(EventId("0192e000-0000-7000-8000-000000000002"),),
)


def recorder(
    answers: dict[str, JsonValue],
) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for suffix, body in answers.items():
            if f"{request.method} {request.url.path}".endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": "not found"})

    return seen, httpx.MockTransport(handle)


def body(request: httpx.Request) -> dict[str, JsonValue]:
    loaded: JsonValue = json.loads(request.content)
    assert isinstance(loaded, dict)
    return loaded


async def roundtrip(
    provider: MemoryProvider, fence_ok: bool = True
) -> tuple[Outcome[RecordRef], Outcome[list[MemoryHit]]]:
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)

    async def fence() -> bool:
        return fence_ok

    scoped = scoped_memory(provider, Binder(opened.value.bindings("memory"), A, lambda: 0, fence))
    saved = await scoped.remember("prefers tabs", "user", PROVENANCE, "b:call_1")
    recalled = await scoped.recall("tabs", 5)
    await opened.value.close()
    return saved, recalled


def test_supermemory_stores_the_binding_and_drops_foreign_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERMEMORY_API_KEY", "sm-key")
    stored: dict[str, JsonValue] = {}

    def results() -> JsonValue:
        mine: dict[str, JsonValue] = {
            "documentId": "doc1",
            "score": 0.9,
            "createdAt": "",
            "updatedAt": "",
        }
        chunk: JsonValue = {
            "content": "prefers tabs",
            "isRelevant": True,
            "position": 0,
            "score": 1,
        }
        foreign: dict[str, JsonValue] = {
            **mine,
            "documentId": "doc2",
            "metadata": {"namespace": "x", "record_id": "y", "origin": "user"},
            "chunks": [],
        }
        found: list[JsonValue] = [{**mine, "chunks": [chunk], "metadata": dict(stored)}, foreign]
        return {"results": found, "timing": 1, "total": 2}

    seen, mock = recorder({"POST /v3/documents": {"id": "doc1", "status": "queued"}})

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/search":
            return httpx.Response(200, json=results())
        answer = mock.handle_request(request)
        stored.update(body(request).get("metadata") or {})  # pyright: ignore[reportArgumentType, reportCallIssue] - test double
        return answer

    provider = supermemory()
    provider = type(provider)(provider.api_key, "http://sm.test", httpx.MockTransport(handle))
    saved, recalled = asyncio.run(roundtrip(provider))
    assert isinstance(saved, Ok)
    assert isinstance(recalled, Ok)
    assert [h.id for h in recalled.value] == ["doc1"]  # doc2's binding isn't ours
    add = body(seen[0])
    assert add["containerTag"] != A.tenant_id  # a digest, never scope text
    assert seen[0].headers["authorization"] == "Bearer sm-key"


def test_zep_creates_the_scope_graph_once_and_parses_episodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZEP_API_KEY", "z-key")
    created: list[str] = []
    stored: dict[str, JsonValue] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/graph/create"):
            created.append(path)
            return httpx.Response(201, json={"graph_id": "g", "uuid": "g1"})
        if path.endswith("/graph/search"):
            episode = {"uuid": "e1", "content": "prefers tabs", "created_at": "", "score": 0.5}
            return httpx.Response(200, json={"episodes": [{**episode, "metadata": stored}]})
        if path.endswith("/graph") and request.method == "POST":
            if not created:
                return httpx.Response(404, json={"message": "graph not found"})
            stored.update(body(request).get("metadata") or {})  # pyright: ignore[reportArgumentType, reportCallIssue] - test double
            return httpx.Response(200, json={"uuid": "e1", "content": "x", "created_at": ""})
        return httpx.Response(404, json={"message": path})

    provider = zep()
    provider = type(provider)(
        provider.api_key, "http://zep.test/api/v2", httpx.MockTransport(handle)
    )
    saved, recalled = asyncio.run(roundtrip(provider))
    assert saved == Ok(RecordRef(id="e1", version="1"))
    assert len(created) == 1
    assert isinstance(recalled, Ok)
    assert [h.text for h in recalled.value] == ["prefers tabs"]


def test_a_stale_fence_writes_nothing_and_is_proven_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERMEMORY_API_KEY", "sm-key")
    seen, mock = recorder({})
    provider = supermemory()
    provider = type(provider)(provider.api_key, "http://sm.test", mock)
    saved, _ = asyncio.run(roundtrip(provider, fence_ok=False))
    assert isinstance(saved, Err)
    assert saved.error.sent is False
    assert seen == []


def test_mem0_refuses_at_setup_because_its_sdk_cannot_be_fenced() -> None:
    with pytest.raises(ConfigError) as raised:
        mem0()
    assert raised.value.code == "transport_fence_unsupported"


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("THREADS_LIVE") != "1", reason="set THREADS_LIVE=1")
@pytest.mark.parametrize("make", [supermemory, zep])
def test_live_hosted_memory_passes_the_suite(make: Callable[[], MemoryProvider]) -> None:
    async def bound(_store: SqliteStore) -> MemoryProvider:
        return make()

    # Hosted providers dedup by key only as far as their APIs allow: no key-conflict check.
    assert asyncio.run(memory_suite(bound, key_conflicts=False)) == ()
