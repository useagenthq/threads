"""A deferred MCP server end to end (spec/schema/README.md, "Deferred tools and tool_search"): the
first request offers only the plain tools and tool_search, a search loads exact names in one
batch, the loaded tools run, and the declared prefix never changes (invariant 5) across two
loads and a compaction, proven on the recorded bytes."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from itertools import pairwise

from mcp_kit import TracedAsgi, server
from pydantic import BaseModel, JsonValue

from threads import Completed, RunContext, agent, scripted_model, sqlite, tool
from threads.adapters.models.render import parse
from threads.agents.results import Thread
from threads.log import (
    Event,
    ModelRequestEvent,
    Permissions,
    ThreadStartedEvent,
    ToolResultEvent,
    ToolsLoadedEvent,
)
from threads.loop.model import ModelRequest
from threads.mcp import mcp
from threads.result import Ok
from threads.thread.control import LOCAL_OPERATOR

ALLOW = Permissions(
    mode="default",
    allow=["mcp__kit__echo", "mcp__kit__sendemail", "lookup_order"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)
EXACT = "mcp__kit__echo, mcp__kit__sendemail"


class Order(BaseModel):
    id: str


async def _lookup(args: Order, _ctx: RunContext[None]) -> str:
    return f"order {args.id}: shipped"


LOOKUP = tool(name="lookup_order", description="Look up an order.", input=Order, execute=_lookup)


def _usage(cache_read: int) -> JsonValue:
    return {"input_tokens": 9000, "output_tokens": 5, "cache_read_tokens": cache_read}


def _uses(*calls: tuple[str, str, JsonValue], cache_read: int = 8000) -> JsonValue:
    parts: list[JsonValue] = [
        {"type": "tool_use", "call_id": c, "name": n, "input": i} for c, n, i in calls
    ]
    return {"content": parts, "stop_reason": "tool_use", "usage": _usage(cache_read)}


def _text(reply: str, cache_read: int = 8000) -> JsonValue:
    return {
        "content": [{"type": "text", "text": reply}],
        "stop_reason": "end_turn",
        "usage": _usage(cache_read),
    }


async def _events(thread: Thread) -> Sequence[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def _names(request: ModelRequest) -> list[str]:
    return [t.name for t in parse(request.body).tools]


def test_a_deferred_server_loads_on_search_and_the_prefix_never_changes() -> None:
    app = server()
    asgi = TracedAsgi(app)
    script: list[JsonValue] = [
        _uses(("call_1", "tool_search", {"query": EXACT})),
        _uses(("call_2", "mcp__kit__echo", {"text": "hi"}), cache_read=0),
        _text("Echoed."),
        _uses(("call_3", "tool_search", {"query": "crash"})),
        _text("Loaded crash.", cache_read=0),
        _text("The summary: echo and crash are loaded."),
        _text("Still here."),
    ]
    model = scripted_model({"responses": script})

    async def main() -> Sequence[Event]:
        async with app.session_manager.run():
            kit = replace(mcp(name="kit", url="http://127.0.0.1:8000/mcp", defer=True), http=asgi)
            bot = agent(model=model, tools=[LOOKUP, kit], permissions=ALLOW)
            store = sqlite(":memory:")
            first = await bot.run("Echo hi.", store=store)
            assert isinstance(first, Completed), first
            again = await bot.run("Load crash.", thread=first.thread)
            assert isinstance(again, Completed), again
            compacting = await first.thread.compact(LOCAL_OPERATOR)
            assert isinstance(compacting, Ok), compacting
            last = await bot.run("Are you there?", thread=first.thread)
            assert isinstance(last, Completed), last
            assert isinstance(await first.thread.replay(), Ok)
            breaks = await first.thread.cache_breaks()
            assert isinstance(breaks, Ok)
            causes = [b.likely_cause for b in breaks.value]
            assert causes[:2] == ["tools_loaded", "tools_loaded"]
            return await _events(first.thread)

    log = asyncio.run(main())
    sent = model.sent
    first = _names(sent[0])
    assert "lookup_order" in first
    assert "tool_search" in first
    assert not any(n.startswith("mcp__kit__") for n in first)
    search = next(t for t in parse(sent[0].body).tools if t.name == "tool_search")
    names = "mcp__kit__crash, mcp__kit__echo, mcp__kit__read_resource, mcp__kit__sendemail"
    listed = f"Deferred tools (search to load): {names}"
    assert search.description.endswith(listed)
    started = next(e for e in log if isinstance(e, ThreadStartedEvent))
    assert all(
        "input_schema" not in t.model_dump(exclude_unset=True)
        for t in started.data.tools
        if t.name.startswith("mcp__kit__")
    )
    # The search's result and its load are one batch, right after one another.
    loads = [i for i, e in enumerate(log) if isinstance(e, ToolsLoadedEvent)]
    assert [log[i - 1].type for i in loads] == ["tool_result", "tool_result"]
    first_load = log[loads[0]]
    assert isinstance(first_load, ToolsLoadedEvent)
    assert [t.name for t in first_load.data.tools] == ["mcp__kit__echo", "mcp__kit__sendemail"]
    assert {"mcp__kit__echo", "mcp__kit__sendemail"} <= set(_names(sent[1]))
    echoed = next(e for e in log if isinstance(e, ToolResultEvent) and e.data.call_id == "call_2")
    assert "echo: hi" in echoed.data.preview
    _prefix_holds(log, sent, len(script))


def _prefix_holds(log: Sequence[Event], sent: Sequence[ModelRequest], responses: int) -> None:
    # Invariant 5: one declared prefix for every request, compaction included, byte for byte.
    requests = [e for e in log if isinstance(e, ModelRequestEvent)]
    assert (
        len({(r.data.declared_prefix.bytes, r.data.declared_prefix.sha256) for r in requests}) == 1
    )
    head = sent[0].body.split(b"\n", 1)[0]
    assert all(r.body.split(b"\n", 1)[0] == head for r in sent)
    # Each turn request extends the one before it, until the compaction.
    turn = [r.body for r in sent[:5]]
    assert all(later.startswith(earlier) for earlier, later in pairwise(turn))
    assert any(e.type == "compacted" for e in log)
    assert len(sent) == responses
    assert b'"role":"tools_loaded"' in sent[-1].body


def test_calling_a_deferred_tool_before_loading_it_fails_before_any_effect() -> None:
    app = server()
    asgi = TracedAsgi(app)
    script: list[JsonValue] = [_uses(("call_1", "mcp__kit__echo", {"text": "hi"})), _text("Ok.")]
    model = scripted_model({"responses": script})

    async def main() -> Sequence[Event]:
        async with app.session_manager.run():
            kit = replace(mcp(name="kit", url="http://127.0.0.1:8000/mcp", defer=True), http=asgi)
            bot = agent(model=model, tools=[LOOKUP, kit], permissions=ALLOW)
            done = await bot.run("Echo hi.", store=sqlite(":memory:"))
            assert isinstance(done, Completed), done
            return await _events(done.thread)

    log = asyncio.run(main())
    result = next(e for e in log if isinstance(e, ToolResultEvent))
    text = "tool_not_loaded: mcp__kit__echo; find it with tool_search first"
    assert (result.data.origin, result.data.preview) == ("not_executed", text)
    assert not any(e.type.startswith("effect_") for e in log)


def test_two_searches_in_one_response_run_in_order_and_bad_input_loads_nothing() -> None:
    app = server()
    asgi = TracedAsgi(app)
    script: list[JsonValue] = [
        _uses(
            ("call_1", "tool_search", {"query": "mcp__kit__echo"}),
            ("call_2", "tool_search", {"query": "mcp__kit__echo"}),
            ("call_3", "tool_search", {"query": ""}),
            ("call_4", "tool_search", {"query": "x" * 201}),
            ("call_5", "tool_search", {"query": "echo", "limit": 11}),
            ("call_6", "tool_search", {"query": "echo", "limit": 1.5}),
        ),
        _text("Done."),
    ]
    model = scripted_model({"responses": script})

    async def main() -> Sequence[Event]:
        async with app.session_manager.run():
            kit = replace(mcp(name="kit", url="http://127.0.0.1:8000/mcp", defer=True), http=asgi)
            bot = agent(model=model, tools=[kit], permissions=ALLOW)
            done = await bot.run("Load echo.", store=sqlite(":memory:"))
            assert isinstance(done, Completed), done
            return await _events(done.thread)

    log = asyncio.run(main())
    results = {str(e.data.call_id): e.data for e in log if isinstance(e, ToolResultEvent)}
    assert results["call_1"].preview == "mcp__kit__echo: Echo text back."
    assert results["call_2"].preview == "mcp__kit__echo: already loaded"
    for bad in ("call_3", "call_4", "call_5", "call_6"):
        assert (results[bad].origin, results[bad].is_error) == ("not_executed", True)
        assert results[bad].preview.startswith("invalid input")
    assert len([e for e in log if isinstance(e, ToolsLoadedEvent)]) == 1
