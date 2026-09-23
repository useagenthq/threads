"""`tool(concurrent=True)` (spec/api.json `tool.concurrent`): only read-only tools that don't end
the turn may run in parallel; the option is covered by config_hash but never shown to the model;
and two concurrent reads of one response really run at the same time (F1.1)."""

import asyncio

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Completed,
    ConfigError,
    EventItem,
    RunContext,
    Tool,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.tool import Reconcile
from threads.hooks.extension import extension
from threads.log import ToolResultEvent
from threads.loop.model import LookupResult, NotFound
from threads.store import StoredEvent

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
DONE: JsonValue = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}


class Query(BaseModel):
    pass


async def _nothing(_args: Query, _ctx: RunContext[None]) -> str:
    return "ok"


async def _lookup(_key: str, _ctx: RunContext[None]) -> LookupResult[str]:
    return NotFound()


def calls(*names: str) -> JsonValue:
    parts: list[JsonValue] = [
        {"type": "tool_use", "call_id": f"call_{i + 1}", "name": n, "input": {}}
        for i, n in enumerate(names)
    ]
    return {"content": parts, "stop_reason": "tool_use", "usage": USAGE}


def _search(*, concurrent: bool = False) -> Tool[Query, str, None]:
    return tool(
        name="search",
        description="Search.",
        input=Query,
        effect="read_only",
        concurrent=concurrent,
        execute=_nothing,
    )


def test_concurrent_needs_read_only_and_no_ends_turn() -> None:
    q, run = Query, _nothing
    with pytest.raises(ConfigError, match="tool search: concurrent") as unguarded:
        tool(name="search", description="S.", input=q, execute=run, concurrent=True)
    assert unguarded.value.code == "invalid_config"
    with pytest.raises(ConfigError, match="tool search: concurrent"):
        tool(
            name="search",
            description="S.",
            input=q,
            execute=run,
            concurrent=True,
            effect="unguarded",
        )
    window = 1000
    with pytest.raises(ConfigError, match="tool search: concurrent"):
        tool(
            name="search",
            description="S.",
            input=q,
            execute=run,
            concurrent=True,
            effect="idempotent",
            dedup_window_ms=window,
        )
    reconcile = Reconcile(_lookup, "final")
    with pytest.raises(ConfigError, match="tool search: concurrent"):
        tool(
            name="search",
            description="S.",
            input=q,
            execute=run,
            concurrent=True,
            effect="reconcilable",
            reconcile=reconcile,
        )
    with pytest.raises(ConfigError, match="tool search: concurrent"):
        tool(
            name="search",
            description="S.",
            input=q,
            execute=run,
            concurrent=True,
            effect="read_only",
            ends_turn=True,
        )


def test_the_tool_spec_is_unchanged_and_config_hash_covers_it() -> None:
    assert _search(concurrent=True).spec() == _search().spec()

    def pinned(t: Tool[Query, str, None]) -> JsonValue:
        bot = agent(model=scripted_model({"responses": []}), tools=[t])
        return bot.definition.thread_started()["config_hash"]

    assert pinned(_search(concurrent=False)) == pinned(_search())
    assert pinned(_search(concurrent=True)) != pinned(_search())

    def raw(t: Tool[Query, str, None]) -> bytes:
        return agent(model=scripted_model({"responses": []}), tools=[t]).definition.pin()[1]

    assert b'"concurrent_tools":["search"]' in raw(_search(concurrent=True))
    assert b"concurrent_tools" not in raw(_search())


def test_turning_it_on_for_an_existing_thread_fails_closed() -> None:
    async def main() -> None:
        first = agent(model=scripted_model({"responses": [DONE]}), tools=[_search()])
        seed = await first.run("hi", store=sqlite(":memory:"), deps=None)
        tools = [_search(concurrent=True)]
        flipped = agent(model=scripted_model({"responses": [DONE]}), tools=tools)
        with pytest.raises(ConfigError, match="another config"):
            await flipped.run("again", thread=seed.thread, deps=None)

    asyncio.run(main())


def test_two_concurrent_reads_of_one_response_run_at_the_same_time() -> None:
    """tool-parallel-safe-reads: each body waits on a latch only the other one releases."""

    model = scripted_model({"responses": [calls("orders", "invoices"), DONE]})

    async def main() -> list[StoredEvent]:
        latches = {"orders": asyncio.Event(), "invoices": asyncio.Event()}
        other = {"orders": "invoices", "invoices": "orders"}

        def lookup(name: str) -> Tool[Query, str, None]:
            async def run(_args: Query, _ctx: RunContext[None]) -> str:
                latches[other[name]].set()
                try:
                    await asyncio.wait_for(latches[name].wait(), 2)
                except TimeoutError:
                    return f"{name} ran alone"
                return f"{name} ran together"

            return tool(
                name=name,
                description=f"Look up {name}.",
                input=Query,
                effect="read_only",
                concurrent=True,
                execute=run,
            )

        bot = agent(model=model, tools=[lookup("orders"), lookup("invoices")])
        stream = bot.stream("go", store=sqlite(":memory:"), deps=None)
        logged = [i.event async for i in stream if isinstance(i, EventItem)]
        assert isinstance(await stream.result, Completed)
        return logged

    logged = asyncio.run(main())
    shown = [(e.data.call_id, e.data.preview) for e in logged if isinstance(e, ToolResultEvent)]
    assert shown == [("call_1", "orders ran together"), ("call_2", "invoices ran together")]
    # Both results are in before the next request.
    kinds = [e.type for e in logged]
    first = kinds.index("tool_result")
    assert kinds[first : first + 3] == ["tool_result", "tool_result", "model_request"]
    for _, text in shown:
        assert model.sent[-1].body.count(text.encode()) == 1


def test_an_extension_tool_joins_a_group_and_framework_and_builtin_tools_run_alone() -> None:
    trace: list[str] = []

    def traced(name: str) -> Tool[Query, str, None]:
        async def run(_args: Query, _ctx: RunContext[None]) -> str:
            trace.append(f"start {name}")
            await asyncio.sleep(0.005)
            trace.append(f"end {name}")
            return name

        return tool(
            name=name,
            description=f"The {name} tool.",
            input=Query,
            effect="read_only",
            concurrent=True,
            execute=run,
        )

    todos: JsonValue = {"todos": [{"id": "1", "content": "Read", "status": "pending"}]}
    past: JsonValue = {"call_id": "call_1", "offset": 0, "length": 1}
    parts: list[JsonValue] = [
        {"type": "tool_use", "call_id": "call_1", "name": "a", "input": {}},
        {"type": "tool_use", "call_id": "call_2", "name": "todo_write", "input": todos},
        {"type": "tool_use", "call_id": "call_3", "name": "b", "input": {}},
        {"type": "tool_use", "call_id": "call_4", "name": "ext__c", "input": {}},
        {"type": "tool_use", "call_id": "call_5", "name": "read_tool_result", "input": past},
        {"type": "tool_use", "call_id": "call_6", "name": "d", "input": {}},
    ]
    response: JsonValue = {"content": parts, "stop_reason": "tool_use", "usage": USAGE}
    bot = agent(
        model=scripted_model({"responses": [response, DONE]}),
        tools=[traced("a"), traced("b"), traced("d")],
        extensions=[extension(name="ext", tools=[traced("c")])],
    )
    assert bot.definition.concurrent_tools() == {"a", "b", "d", "ext__c"}

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"), deps=None)
        assert isinstance(result, Completed)

    asyncio.run(main())
    at = trace.index
    assert at("end a") < at("start b")  # todo_write (framework) is a barrier
    assert at("start c") < at("end b")  # the extension tool joins b's group
    assert max(at("end b"), at("end c")) < at("start d")  # read_tool_result (built-in) too
