"""C5, round 5: the writer redacts JSON keys and the actor, provider material holding a
registered value fails closed, web_fetch stores its cited page redacted, and adapter and MCP
setup errors are returned redacted."""

import asyncio
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from corpus import Clock
from fakes import Script, sse
from kit import T0, Tools, open_store, start
from pydantic import BaseModel, ConfigDict, JsonValue

from threads import (
    Completed,
    ConfigError,
    RunContext,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.adapters.models.openai.model import OpenAIModel
from threads.agents.bindings import AppTool, Fence
from threads.log import ArtifactRef, CitationPart, Permissions, Principal, TurnCompletedEvent
from threads.loop.drive import drive
from threads.loop.guard import block_model_requests
from threads.loop.runtime import Runtime
from threads.loop.scripted import ScriptedModel
from threads.openai import openai
from threads.result import Err
from threads.secrets import credential
from threads.web.fetch import Page
from threads.web.results import page_output

BYPASS = Permissions.model_validate(
    {
        "mode": "bypass",
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": [],
        "allow_bypass": True,
        "plan_exit_mode": "default",
    }
)
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}


class Anything(BaseModel):
    model_config = ConfigDict(extra="allow")


def test_the_writer_redacts_json_keys_and_the_actor(tmp_path: Path) -> None:
    key = credential("fake", "api_key", "sk-l9-key-4d5e", "U")()

    async def ok(_args: Anything, _ctx: RunContext[None]) -> str:
        return "ok"

    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "any", "input": {key: "x"}}
    call: JsonValue = {"content": [part], "stop_reason": "tool_use", "usage": USAGE}
    done: JsonValue = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
    anything = tool(name="any", description="Takes anything.", input=Anything, execute=ok)
    bot = agent(
        model=scripted_model({"responses": [call, done]}), tools=[anything], permissions=BYPASS
    )
    store = tmp_path / "store"
    who = Principal(issuer="api", tenant="local", subject=key)
    ran = asyncio.run(bot.run("go", store=sqlite(str(store)), deps=None, principal=who))
    assert isinstance(ran, Completed)
    for stored in (p for p in store.rglob("*") if p.is_file()):
        assert key.encode() not in stored.read_bytes(), stored


@pytest.fixture
def offline_adapter() -> Generator[None]:
    """These adapters send only to a scripted transport."""
    block_model_requests(blocked=False)
    yield
    block_model_requests()


def _openai_turn(item: JsonValue) -> Runtime:
    done: JsonValue = {"type": "response.completed", "response": {"usage": USAGE}}
    reply = sse([(None, {"type": "response.output_item.done", "item": item}), (None, done)])
    info = openai(
        "gpt-test",
        hosted_tools=[{"type": "web_search"}],
        context_window=400_000,
        max_output_tokens=64,
        api_key="sk-test-openai",
    ).info
    model = OpenAIModel(info, "sk-test-openai", http=httpx2.MockTransport(Script([reply])))

    async def main() -> Runtime:
        clock = Clock(T0)
        rt = await start(await open_store(), [], model, Tools({}, clock), clock)
        await drive(rt)
        return rt

    return asyncio.run(main())


@pytest.mark.usefixtures("offline_adapter")
@pytest.mark.parametrize("kind", ["reasoning", "web_search_call"])
def test_provider_material_holding_a_secret_fails_closed(kind: str) -> None:
    key = credential("fake", "api_key", f"sk-l9-{kind}-2f3a", "U")()
    item: JsonValue = (
        {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": f"gAAA{key}"}
        if kind == "reasoning"
        else {
            "id": "ws_1",
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "query": f"find {key}"},
        }
    )
    rt = _openai_turn(item)
    ended = [e for e in rt.events if isinstance(e, TurnCompletedEvent)]
    assert [(e.data.reason, e.data.code) for e in ended] == [("error", "secret_in_provider_output")]
    assert all(key not in e.model_dump_json() for e in rt.events)


def test_web_fetch_stores_its_cited_page_redacted() -> None:
    key = credential("fake", "api_key", "sk-l9-page-6f7a", "U")()
    stored: dict[str, bytes] = {}

    async def put(data: bytes, media_type: str) -> ArtifactRef:
        stored["page"] = data
        return ArtifactRef(sha256="0" * 64, bytes=len(data), media_type=media_type)

    page = Page("https://example.com/p", 200, "text/plain", "utf-8", f"key {key}".encode(), False)
    out = asyncio.run(page_output(page, put))
    assert stored["page"] == b"key [secret fake.api_key]"
    (cite,) = [p for p in out.content if isinstance(p, CitationPart)]
    assert cite.ref is not None
    assert key not in out.text


def test_a_binary_page_holding_a_secret_is_not_stored() -> None:
    key = credential("fake", "api_key", "sk-l9-bin-7a8b", "U")()
    stored: list[bytes] = []

    async def put(data: bytes, media_type: str) -> ArtifactRef:
        stored.append(data)
        return ArtifactRef(sha256="0" * 64, bytes=len(data), media_type=media_type)

    body = b"\x89PNG" + key.encode()
    page = Page("https://example.com/i.png", 200, "image/png", None, body, False)
    out = asyncio.run(page_output(page, put))
    assert stored == []
    assert out.is_error


class Failing(ScriptedModel):
    """A scripted model whose setup rejects its registered key in the error text."""

    def __init__(self, key: str) -> None:
        super().__init__([], {})
        self.key = key

    async def setup(self) -> None:
        raise ConfigError("invalid_config", f"rejected {self.key}")


def test_an_adapter_setup_error_is_returned_redacted() -> None:
    key = credential("fake", "api_key", "sk-l9-adapter-8b9c", "U")()
    bot = agent(model=Failing(key))

    async def main() -> None:
        checked = await bot.check()
        assert isinstance(checked, Err)
        assert checked.error.message == "rejected [secret fake.api_key]"
        with pytest.raises(ConfigError) as raised:
            await bot.run("go", store=sqlite(":memory:"))
        assert raised.value.message == "rejected [secret fake.api_key]"

    asyncio.run(main())


class Down:
    """An MCP-like tool server whose connect fails with its key in the error text."""

    def __init__(self, key: str) -> None:
        self.key = key

    @property
    def name(self) -> str:
        return "down"

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        return self._connect()

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[Sequence[AppTool[object]]]:
        raise ConfigError("mcp_unreachable", f"401 for {self.key}")
        yield ()


def test_an_mcp_connect_error_is_returned_redacted() -> None:
    key = credential("fake", "api_key", "sk-l9-mcp-0d1e", "U")()
    bot = agent(model=scripted_model({"responses": []}), tools=[Down(key)])

    async def main() -> None:
        checked = await bot.check()
        assert isinstance(checked, Err)
        assert checked.error.message == "401 for [secret fake.api_key]"
        with pytest.raises(ConfigError) as raised:
            await bot.run("go", store=sqlite(":memory:"))
        assert raised.value.message == "401 for [secret fake.api_key]"

    asyncio.run(main())
