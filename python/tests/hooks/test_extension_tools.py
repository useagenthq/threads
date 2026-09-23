"""extension(tools=...), as in TS: each tool is namespaced
`<ext>__<tool>`, sorted by that name after the app tools, pinned in thread_started.tools and so in
config_hash, and dispatched through the same authorize and effect path as an app tool."""

import asyncio

import pytest
from pydantic import BaseModel, JsonValue

from threads import Completed, ConfigError, RunContext, agent, extension, scripted_model, sqlite
from threads import tool as make_tool
from threads.log import Permissions, ThreadStartedEvent, ToolResultEvent
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALLOW = Permissions(
    mode="default",
    allow=["ops__ping"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Empty(BaseModel):
    pass


async def pong(_args: Empty, ctx: RunContext[None]) -> str:
    return f"pong {ctx.call_id}"


PING = make_tool(name="ping", description="Ping.", input=Empty, runs="host", execute=pong)
ALPHA = make_tool(name="alpha", description="A.", input=Empty, runs="host", execute=pong)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": name, "input": {}}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def test_extension_tools_are_namespaced_sorted_pinned_and_run() -> None:
    async def main() -> None:
        ops = extension(name="ops", instructions="Ops.", tools=[PING, ALPHA])
        bot = agent(
            model=scripted_model({"responses": [call("ops__ping"), text("done")]}),
            permissions=ALLOW,
            extensions=[ops],
        )
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        events = [e.event for e in timeline.value.entries]
        started = next(e for e in events if isinstance(e, ThreadStartedEvent))
        names = [t.name for t in started.data.tools]
        assert names[-2:] == ["ops__alpha", "ops__ping"]
        done = next(e for e in events if isinstance(e, ToolResultEvent))
        assert (done.data.preview, done.data.is_error) == ("pong call_1", False)

    asyncio.run(main())


def test_extension_tools_change_the_config_hash() -> None:
    def pinned(*, with_tool: bool) -> str:
        ops = extension(name="ops", tools=[PING] if with_tool else [])
        bot = agent(model=scripted_model({"responses": []}), extensions=[ops])
        started = bot._definition.thread_started()  # pyright: ignore[reportPrivateUsage]  # test reads the pin
        value = started["config_hash"]
        assert isinstance(value, str)
        return value

    assert pinned(with_tool=False) != pinned(with_tool=True)


def test_a_namespaced_name_that_collides_is_a_config_error() -> None:
    clash = make_tool(name="ops__ping", description="X.", input=Empty, runs="host", execute=pong)
    with pytest.raises(ConfigError):
        agent(
            model=scripted_model({"responses": []}),
            tools=[clash],
            extensions=[extension(name="ops", tools=[PING])],
        )
