"""C5, round 7 (Codex #359): the stored line's canonical bytes, MCP cleanup that fails, a value
registered while a spill streams, direct knowledge ingest, and a torn-tail import."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from pydantic import JsonValue

from threads import Failed, agent, scripted_model, sqlite
from threads.agents.bindings import AppTool, Fence
from threads.log import BranchId, ThreadId
from threads.memory.conformance import A
from threads.memory.local_knowledge import LocalKnowledge
from threads.memory.types import Binding, KnowledgeSource
from threads.redaction import register
from threads.result import Err, Ok
from threads.secrets import credential
from threads.store import Draft, SqliteStore, Writer, verify_export

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000
ALICE: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}


def started(settings: dict[str, JsonValue]) -> Draft:
    return Draft(
        "thread_started",
        {
            "agent_name": "demo",
            "config_hash": "0" * 64,
            "instructions": "You are a helpful agent.",
            "model": {"provider": "scripted", "name": "scripted-1"},
            "model_params": {"max_tokens": 1024},
            "adapter": {"name": "scripted", "version": "1", "settings": settings},
            "tools": [],
        },
    )


def user(text: str) -> Draft:
    return Draft("user_input", {"source": "api", "text": text}, actor=ALICE)


async def opened() -> SqliteStore:
    store = await SqliteStore.open(":memory:")
    assert isinstance(store, Ok)
    return store.value


async def writer(store: SqliteStore) -> Writer:
    assert await store.create(THREAD, ROOT, T0) == Ok(None)
    acquired = await store.acquire(ROOT, "holder", lambda: T0)
    assert isinstance(acquired, Ok)
    return acquired.value


def escaping_marker() -> None:
    """Redacting FIRSTVALUE gives `[secret X"Y]`; canonical JSON escapes its quote into the
    second value."""
    register("FIRSTVALUE", 'X"Y')
    register('[secret X\\"Y]', "Z")


def test_a_marker_whose_quote_canonical_json_escapes_into_a_value_is_refused() -> None:
    escaping_marker()

    async def main() -> None:
        store = await opened()
        w = await writer(store)
        assert isinstance(await w.append([started({})]), Ok)
        appended = await w.append([user("FIRSTVALUE")])
        assert isinstance(appended, Err)
        assert appended.error.code == "secret_in_stored_bytes"
        exported = await store.export(ROOT)
        assert isinstance(exported, Ok)
        assert b'[secret X\\"Y]' not in exported.value
        await store.close()

    asyncio.run(main())


def test_strings_that_json_punctuation_joins_into_a_value_are_refused() -> None:
    register('alpha","beta', "Z")

    async def main() -> None:
        store = await opened()
        appended = await (await writer(store)).append([started({"t": ["alpha", "beta"]})])
        assert isinstance(appended, Err)
        assert appended.error.code == "secret_in_stored_bytes"
        await store.close()

    asyncio.run(main())


def test_a_run_whose_model_output_the_writer_refuses_fails_with_that_code() -> None:
    escaping_marker()
    say: JsonValue = {
        "content": [{"type": "text", "text": "a FIRSTVALUE"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    bot = agent(model=scripted_model({"responses": [say]}))
    result = asyncio.run(bot.run("go", store=sqlite(":memory:")))
    assert isinstance(result, Failed)
    assert result.error.code == "secret_in_stored_bytes"


class Server:
    """An MCP server double: its session's close raises, or its connect does."""

    def __init__(self, name: str, *, refuse: str | None = None, close: str | None = None) -> None:
        self._name, self._refuse, self._close = name, refuse, close

    @property
    def name(self) -> str:
        return self._name

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        if self._refuse is not None:
            raise RuntimeError(self._refuse)
        return self._session()

    @asynccontextmanager
    async def _session(self) -> AsyncGenerator[Sequence[AppTool[object]]]:
        yield ()
        if self._close is not None:
            raise RuntimeError(self._close)


def test_a_sibling_whose_close_fails_never_replaces_the_redacted_failure() -> None:
    key = credential("fake", "api_key", "sk-l9-close-3c4d", "U")()
    up = Server("up", close=f"close {key}")
    down = Server("down", refuse=f"refused {key}")

    async def main() -> None:
        checked = await agent(model=scripted_model({"responses": []}), tools=[up, down]).check()
        assert isinstance(checked, Err)
        assert checked.error.code == "mcp_unreachable"
        assert key not in checked.error.message
        alone = await agent(model=scripted_model({"responses": []}), tools=[up]).check()
        assert alone == Ok(None)

    asyncio.run(main())


def test_a_value_registered_while_a_spill_streams_drops_the_spill() -> None:
    async def main() -> None:
        store = await opened()
        spill = await store.spill()
        await spill.write(b"abcd")
        register("abcdefgh", "late")
        await spill.write(b"efgh")
        assert await spill.commit() is None
        await store.close()

    asyncio.run(main())


def test_the_local_knowledge_provider_refuses_a_source_holding_a_value() -> None:
    key = credential("fake", "api_key", "sk-l9-ingest-5e6f", "U")()

    async def main() -> None:
        store = await opened()
        provider = await LocalKnowledge(()).bind(store)
        source = KnowledgeSource(
            source_id="doc.md",
            media_type="text/markdown",
            content=f"the key is {key}".encode(),
            binding=Binding(namespace="n", record_id="doc"),
        )
        got = await provider.ingest(A, source, "doc@1")
        assert isinstance(got, Err)
        assert got.error.code == "invalid"
        assert await provider.revision(A) == Ok(0)
        await store.close()

    asyncio.run(main())


def test_a_torn_tail_holding_a_value_refuses_the_import() -> None:
    later = "sk-l9-torn-7a8b"

    async def main() -> None:
        store = await opened()
        w = await writer(store)
        assert isinstance(await w.append([started({}), user("hi")]), Ok)
        exported = await store.export(ROOT)
        assert isinstance(exported, Ok)
        body = exported.value[: exported.value.rindex(b"\n", 0, -1) + 1]
        tail = f'{{"torn":"{later}'.encode()
        verified = verify_export(body + tail, T0)
        assert isinstance(verified, Ok)
        assert verified.value.dropped == tail
        credential("fake", "api_key", later, "U")()
        imported = await (await opened()).import_log(verified.value)
        assert isinstance(imported, Err)
        assert imported.error.code == "secret_in_stored_bytes"
        await store.close()

    asyncio.run(main())
