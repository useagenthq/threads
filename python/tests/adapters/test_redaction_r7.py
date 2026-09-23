"""C5: the stored line's canonical bytes, MCP cleanup that fails, a value
registered while a spill streams, direct knowledge ingest, and a torn-tail import."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING

from redaction_kit import ROOT, T0, escaping_marker, opened, started, user, writer

from threads import Failed, agent, scripted_model, sqlite
from threads.agents.bindings import AppTool, Fence
from threads.log.digest import sha256_hex
from threads.memory.conformance import A
from threads.memory.local_knowledge import LocalKnowledge
from threads.memory.types import Binding, KnowledgeSource
from threads.redaction import register
from threads.result import Err, Ok
from threads.secrets import credential
from threads.store import verify_export

if TYPE_CHECKING:
    from pydantic import JsonValue


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
        assert isinstance(await store.get_artifact(sha256_hex(source.content)), Err)
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
