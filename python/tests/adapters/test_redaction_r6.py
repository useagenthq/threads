"""C5, round 6 (Codex #328): registration rules, the post-redaction re-scan, the byte-exact
fail-closed check (escaped JSON included), redacted setup failures of any kind, and the
byte-exact writes (config artifact, knowledge source, screenshot, import) refused."""

import asyncio
import json
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from fakes import Script, sse
from pydantic import JsonValue

from threads import (
    Completed,
    ConfigError,
    agent,
    local_knowledge,
    scripted_model,
    secret,
    sqlite,
)
from threads.adapters.models.openai.model import OpenAIModel
from threads.agents.bindings import AppTool, Fence
from threads.agents.store import open_store
from threads.log import ParseError, TurnCompletedEvent
from threads.loop.guard import block_model_requests
from threads.loop.scripted import ScriptedModel
from threads.openai import openai
from threads.redaction import contains_secret, redact_json, redact_secrets
from threads.result import Err, Ok
from threads.secrets import credential, resolve
from threads.store.verify import verify_export

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}


def files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def nothing_holds(root: Path, key: str) -> None:
    for stored in files(root):
        assert key.encode() not in stored.read_bytes(), stored


def test_a_value_shorter_than_8_characters_is_refused() -> None:
    with pytest.raises(ConfigError, match="secret values must be at least 8 characters"):
        credential("fake", "api_key", "api", "U")()
    with pytest.raises(ConfigError, match="secret values must be at least 8 characters"):
        resolve(secret("SHORT"), {"SHORT": "1234567"})


@pytest.mark.parametrize("literal", ["user_input", "model_request", "provider_error"])
def test_a_value_equal_to_a_schema_literal_is_refused(literal: str) -> None:
    with pytest.raises(ConfigError, match="can't be a literal of the event schema") as raised:
        credential("fake", "api_key", literal, "U")()
    assert raised.value.code == "invalid_config"


def test_a_marker_joined_to_its_neighbour_is_re_scanned() -> None:
    """#328 HIGH 1."""
    credential("y", "api_key", "abc[secret x.api_key]", "U")()
    credential("x", "api_key", "TOKEN-1234", "U")()
    assert redact_secrets("abcTOKEN-1234") == "[redacted]"


def test_a_numbered_key_that_would_hold_a_value_is_skipped() -> None:
    """#328 HIGH 2."""
    credential("x", "api_key", "A-value-12", "U")()
    credential("z", "api_key", "x.api_key] (2)", "U")()
    out = redact_json({"A-value-12": 1, "[secret x.api_key]": 2})
    assert isinstance(out, dict)
    assert list(out) == ["[secret x.api_key]", "[secret x.api_key] (3)"]


def test_contains_secret_matches_raw_and_json_escaped_forms() -> None:
    """#328 HIGH 3: quotes, backslashes, controls and non-ASCII."""
    value = 'q"uo\\te\nd-é-8'
    credential("fake", "api_key", value, "U")()
    for text in (
        f"raw {value}",
        json.dumps({"v": value}, ensure_ascii=False),
        json.dumps({"v": value}),
        json.dumps({value: 1}, ensure_ascii=False),
    ):
        assert contains_secret(text.encode()), text
    assert not contains_secret(b"nothing here")


@pytest.fixture
def offline_adapter() -> Generator[None]:
    """These adapters send only to a scripted transport."""
    block_model_requests(blocked=False)
    yield
    block_model_requests()


@pytest.mark.usefixtures("offline_adapter")
def test_escaped_provider_material_holding_a_value_fails_closed(tmp_path: Path) -> None:
    key = credential("fake", "api_key", 'sk-"quoted"-l9', "U")()
    item: JsonValue = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": f"gAAA{key}",
    }
    done: JsonValue = {"type": "response.completed", "response": {"usage": USAGE}}
    reply = sse([(None, {"type": "response.output_item.done", "item": item}), (None, done)])
    info = openai(
        "gpt-test", max_input_tokens=400_000, max_output_tokens=64, api_key="sk-test-openai"
    ).info
    model = OpenAIModel(info, "sk-test-openai", http=httpx2.MockTransport(Script([reply])))
    store = tmp_path / "store"

    async def main() -> Sequence[TurnCompletedEvent]:
        result = await agent(model=model).run("go", store=sqlite(str(store)))
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        return [e.event for e in timeline.value.entries if isinstance(e.event, TurnCompletedEvent)]

    ended = asyncio.run(main())
    assert [(e.data.reason, e.data.code) for e in ended] == [("error", "secret_in_provider_output")]
    nothing_holds(store, json.dumps(key)[1:-1])


def test_setup_failures_of_any_kind_are_redacted_values() -> None:
    """#328 HIGH 5: a plain RuntimeError from an adapter setup and from an MCP connect."""
    key = credential("fake", "api_key", "sk-l9-plain-5e6f", "U")()

    class Boom(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([], {})

        async def setup(self) -> None:
            raise RuntimeError(f"boom {key}")

    class Down:
        @property
        def name(self) -> str:
            return "down"

        def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
            return self._connect()

        @asynccontextmanager
        async def _connect(self) -> AsyncGenerator[Sequence[AppTool[object]]]:
            raise RuntimeError(f"refused {key}")
            yield ()

    async def main() -> None:
        checked = await agent(model=Boom()).check()
        assert isinstance(checked, Err)
        assert checked.error.code == "invalid_config"
        assert "boom [secret fake.api_key]" in checked.error.message
        with pytest.raises(ConfigError) as raised:
            await agent(model=Boom()).run("go", store=sqlite(":memory:"))
        assert key not in raised.value.message
        mcp = await agent(model=scripted_model({"responses": []}), tools=[Down()]).check()
        assert isinstance(mcp, Err)
        assert mcp.error.code == "mcp_unreachable"
        assert key not in mcp.error.message

    asyncio.run(main())


def test_a_config_holding_a_value_is_never_pinned(tmp_path: Path) -> None:
    """#328 HIGH 6: the config artifact is byte-exact (its hash is config_hash)."""
    key = credential("fake", "api_key", "sk-l9-config-7a8b", "U")()
    bot = agent(model=scripted_model({"responses": []}), instructions=f"use {key}")
    store = tmp_path / "store"
    with pytest.raises(ConfigError) as raised:
        asyncio.run(bot.run("go", store=sqlite(str(store))))
    assert raised.value.code == "invalid_config"
    nothing_holds(tmp_path, key)


def test_a_knowledge_source_holding_a_value_is_not_ingested(tmp_path: Path) -> None:
    """#328 HIGH 7."""
    key = credential("fake", "api_key", "sk-l9-doc-9c0d", "U")()
    doc = tmp_path / "notes.md"
    doc.write_text(f"the key is {key}")
    store = tmp_path / "store"
    bot = agent(
        model=scripted_model({"responses": []}),
        knowledge=local_knowledge(paths=[str(doc)]),
    )
    with pytest.raises(ConfigError) as raised:
        asyncio.run(bot.run("go", store=sqlite(str(store))))
    assert raised.value.code == "invalid_config"
    nothing_holds(store, key)


def test_an_import_holding_a_value_is_refused() -> None:
    """The torn tail and every line of an import are byte-exact."""
    later = "sk-l9-import-1e2f"
    say: JsonValue = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }

    async def main() -> Ok[None] | Err[ParseError]:
        store = sqlite(":memory:")
        ran = await agent(model=scripted_model({"responses": [say]})).run(
            f"note {later}", store=store
        )
        assert isinstance(ran, Completed)
        exported = await (await open_store(store)).export(ran.thread.branch)
        assert isinstance(exported, Ok)
        credential("fake", "api_key", later, "U")()
        verified = verify_export(exported.value, 2_000_000_000_000)
        assert isinstance(verified, Ok)
        fresh = await open_store(sqlite(":memory:"))
        return await fresh.import_log(verified.value)

    imported = asyncio.run(main())
    assert isinstance(imported, Err)
    assert imported.error.code == "secret_in_stored_bytes"
