"""The public surface of this slice (spec/api.json): agent(), tool(), run(), stream(),
scripted_model() and sqlite(), end to end on the scripted model."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Completed,
    ConfigError,
    EventItem,
    ModelContext,
    ModelInfo,
    ModelRequest,
    Parked,
    RunContext,
    Tool,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.log import Permissions, ThreadStartedEvent, ToolResultEvent
from threads.loop.model import ModelChunk, NotFound
from threads.loop.scripted import SCRIPTED_INFO
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALLOW_ECHO = Permissions(
    mode="default",
    allow=["echo"],
    ask=[],
    deny=[],
    protected_paths=[".git/**"],
    allow_bypass=False,
    plan_exit_mode="default",
)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


NO_REPLIES: dict[str, JsonValue] = {"responses": []}


class Echo(BaseModel):
    text: str


def test_readme_example() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hello!")]}), instructions="Greet.")
        result = await bot.run("hi", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        assert result.output == "Hello!"

    asyncio.run(main())


def test_a_tool_call_parses_through_its_input_model_and_runs() -> None:
    seen: list[str] = []

    async def run(args: Echo, _ctx: RunContext[None]) -> str:
        seen.append(args.text)
        return args.text

    echo = tool(
        name="echo",
        description="Echo text.",
        input=Echo,
        runs="host",
        execute=run,
        effect="read_only",
    )
    script: JsonValue = {"responses": [use("echo", {"text": "hello"}), text("Done.")]}

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script), tools=[echo], permissions=ALLOW_ECHO)
        result = await bot.run("say hello", store=store, deps=None)
        assert isinstance(result, Completed)
        assert result.output == "Done."
        # name, description and the input model's schema are what thread_started pins.
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        started = next(e.event for e in timeline.value.entries if e.event.type == "thread_started")
        assert isinstance(started, ThreadStartedEvent)
        pinned = next(t for t in started.data.tools if t.name == "echo")
        assert pinned.description == "Echo text."
        assert pinned.input_schema == Echo.model_json_schema()

    asyncio.run(main())
    assert seen == ["hello"]


def test_bad_arguments_are_an_error_result_and_the_tool_never_runs() -> None:
    seen: list[str] = []

    async def run(args: Echo, _ctx: RunContext[None]) -> str:
        seen.append(args.text)
        return args.text

    echo = tool(
        name="echo", description="Echo.", input=Echo, runs="host", execute=run, effect="read_only"
    )
    script: JsonValue = {"responses": [use("echo", {"text": 7}), text("Sorry.")]}

    async def main() -> list[ToolResultEvent]:
        bot = agent(model=scripted_model(script), tools=[echo], permissions=ALLOW_ECHO)
        stream = bot.stream("go", store=sqlite(":memory:"), deps=None)
        results = [
            i.event
            async for i in stream
            if isinstance(i, EventItem) and isinstance(i.event, ToolResultEvent)
        ]
        assert isinstance(await stream.result, Completed)
        return results

    results = asyncio.run(main())
    assert seen == []
    assert [(r.data.is_error, r.data.origin) for r in results] == [(True, "not_executed")]


def test_an_unguarded_tool_asks_in_default_mode_and_the_run_parks() -> None:
    async def send(args: Echo, _ctx: RunContext[None]) -> str:
        raise AssertionError("an unapproved call must not run")

    send_tool = tool(name="send", description="Send.", input=Echo, runs="host", execute=send)
    script: JsonValue = {"responses": [use("send", {"text": "x"})]}

    async def main() -> None:
        bot = agent(model=scripted_model(script), tools=[send_tool])
        result = await bot.run("send it", store=sqlite(":memory:"), deps=None)
        assert isinstance(result, Parked)
        assert result.reason == "awaiting_approval"

    asyncio.run(main())


def test_a_second_run_continues_the_thread() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model({"responses": [text("One."), text("Two.")]}))
        first = await bot.run("1", store=store)
        second = await bot.run("2", thread=first.thread)
        assert isinstance(second, Completed)
        assert (second.output, second.thread) == ("Two.", first.thread)

    asyncio.run(main())


def test_a_sandbox_tool_is_a_setup_error() -> None:
    async def run(args: Echo, _ctx: RunContext[None]) -> str:
        return args.text

    with pytest.raises(ConfigError) as raised:
        tool(name="echo", description="Echo.", input=Echo, runs="sandbox", execute=run)
    assert raised.value.code == "capability_missing"
    assert "runs" in str(raised.value)
    with pytest.raises(ConfigError):
        tool(name="echo", description="Echo.", input=Echo, runs="elsewhere", execute=run)  # pyright: ignore[reportArgumentType] - runs is host or sandbox


def test_a_tool_without_runs_is_a_host_tool() -> None:
    heard: list[str] = []

    async def run(args: Echo, _ctx: RunContext[None]) -> str:
        heard.append(args.text)
        return args.text

    echo = tool(name="echo", description="Echo.", input=Echo, execute=run, effect="read_only")
    script: JsonValue = {"responses": [use("echo", {"text": "hello"}), text("Done.")]}

    async def main() -> None:
        bot = agent(model=scripted_model(script), tools=[echo], permissions=ALLOW_ECHO)
        result = await bot.run("say hello", store=sqlite(":memory:"), deps=None)
        assert isinstance(result, Completed)
        assert result.output == "Done."

    asyncio.run(main())
    assert heard == ["hello"]


def test_omitted_and_explicit_host_runs_pin_identical_bytes() -> None:
    async def run(args: Echo, _ctx: RunContext[None]) -> str:
        return args.text

    omitted = tool(name="echo", description="Echo.", input=Echo, execute=run)
    host = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=run)
    assert omitted.spec() == host.spec()

    def pinned(t: Tool[Echo, str, None]) -> JsonValue:
        return agent(model=scripted_model(NO_REPLIES), tools=[t]).definition.thread_started()[
            "config_hash"
        ]

    assert pinned(omitted) == pinned(host)


class RealModel:
    """Stands in for a provider adapter: the guard must stop it before any request."""

    @property
    def info(self) -> ModelInfo:
        return SCRIPTED_INFO

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        raise AssertionError(f"reached a provider with {request.request_id}")
        yield  # pragma: no cover

    async def lookup(self, request_id: str, context: ModelContext) -> NotFound:
        return NotFound()


def test_the_guard_blocks_every_model_but_the_scripted_one() -> None:
    async def main() -> None:
        bot = agent(model=RealModel())
        await bot.run("hi", store=sqlite(":memory:"))

    with pytest.raises(RuntimeError, match="blocked in tests"):
        asyncio.run(main())
