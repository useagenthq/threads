"""Subagents through agent.run: a child thread with its own log linked to
the parent's agent_spawned, one terminal agent_finished per child, background children with a
deferred placeholder and a late result, narrowing at setup and at dispatch, the tree-wide budget,
the subagent hooks, and a crash mid-child that resumes the same child."""

import asyncio
from collections.abc import Sequence

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    BudgetExhausted,
    Completed,
    ConfigError,
    EventItem,
    RunContext,
    Store,
    Thread,
    agent,
    extension,
    scripted_model,
    sqlite,
    tool,
)
from threads.hooks.types import StopGate, SwitchGate
from threads.log import (
    AgentFinishedData,
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Budget,
    Event,
    HookDecisionEvent,
    Permissions,
    ThreadId,
    ThreadStartedEvent,
    ToolCallData,
    ToolResultEvent,
    ToolResultLateEvent,
    UserInputEvent,
)
from threads.loop.scripted import ScriptExhaustedError
from threads.result import Ok
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def perms(allow: Sequence[str] = (), deny: Sequence[str] = ()) -> Permissions:
    return Permissions(
        mode="default",
        allow=list(allow),
        ask=[],
        deny=list(deny),
        protected_paths=[],
        allow_bypass=False,
        plan_exit_mode="default",
    )


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def spawn(agent_name: str, *, background: bool = False, call_id: str = "call_1") -> JsonValue:
    args: dict[str, JsonValue] = {"agent": agent_name, "prompt": "Review the diff."}
    if background:
        args["background"] = True
    return call("spawn_agent", args, call_id)


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


async def child_events(store: Store, child: ThreadId) -> list[Event]:
    opened = await open_thread(store, child)
    assert isinstance(opened, Ok)
    return await events_of(opened.value)


def only[T](events: Sequence[Event], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


def test_a_foreground_child_runs_in_its_own_thread_and_feeds_the_call_result() -> None:
    async def main() -> None:
        reviewer = agent(name="reviewer", model=scripted_model({"responses": [text("LGTM")]}))
        lead = agent(
            model=scripted_model({"responses": [spawn("reviewer"), text("done")]}),
            subagents=[reviewer],
        )
        store = sqlite(":memory:")
        result = await lead.run("go", store=store)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        spawned = only(events, AgentSpawnedEvent)
        finished = only(events, AgentFinishedEvent)
        assert len(spawned) == 1
        assert len(finished) == 1
        assert (spawned[0].data.mode, spawned[0].data.isolation) == ("foreground", "none")
        assert finished[0].data.status == "completed"
        assert (finished[0].data.usage.input_tokens, finished[0].data.usage.output_tokens) == (
            10,
            2,
        )
        kinds = [e.type for e in events]
        assert (
            kinds.index("agent_spawned")
            < kinds.index("agent_finished")
            < kinds.index("tool_result")
        )
        assert only(events, ToolResultEvent)[0].data.preview == "LGTM"
        child = await child_events(store, spawned[0].data.child_thread_id)
        started = only(child, ThreadStartedEvent)[0].data
        assert started.agent_name == "reviewer"
        link = started.model_dump(mode="json")["parent"]
        assert link["relation"] == "subagent"
        assert link["event_id"] == spawned[0].event_id
        assert only(child, UserInputEvent)[0].data.source == "parent_agent"
        children = await result.thread.children()
        assert isinstance(children, Ok)
        assert [c.status for c in children.value] == ["completed"]

    asyncio.run(main())


def test_a_background_child_gets_a_placeholder_then_a_late_result() -> None:
    async def main() -> None:
        scanner = agent(name="scanner", model=scripted_model({"responses": [text("no vulns")]}))
        lead = agent(
            model=scripted_model({"responses": [spawn("scanner", background=True), text("ok")]}),
            subagents=[scanner],
        )
        result = await lead.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        placeholder = only(events, ToolResultEvent)[0]
        assert placeholder.data.origin == "deferred"
        late = only(events, ToolResultLateEvent)
        assert [(r.data.call_id, r.data.preview) for r in late] == [("call_1", "no vulns")]
        assert [f.data.status for f in only(events, AgentFinishedEvent)] == ["completed"]
        assert [e.data.mode for e in only(events, AgentSpawnedEvent)] == ["background"]

    asyncio.run(main())


class Echo(BaseModel):
    text: str


async def echo(args: Echo, _ctx: RunContext[None]) -> str:
    return args.text


ECHO = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=echo)


def test_a_subagent_with_a_tool_its_parent_lacks_is_a_config_error() -> None:
    wider = agent(name="wider", model=scripted_model({"responses": []}), tools=[ECHO])
    with pytest.raises(ConfigError):
        agent(model=scripted_model({"responses": []}), subagents=[wider])


def test_a_child_never_gains_a_permission_its_parent_lacks() -> None:
    async def main() -> None:
        child = agent(
            name="helper",
            model=scripted_model({"responses": [call("echo", {"text": "x"}), text("tried")]}),
            tools=[ECHO],
            permissions=perms(allow=["echo"]),
        )
        lead = agent(
            model=scripted_model({"responses": [spawn("helper"), text("done")]}),
            tools=[ECHO],
            permissions=perms(deny=["echo"]),
            subagents=[child],
        )
        store = sqlite(":memory:")
        result = await lead.run("go", store=store, deps=None)
        assert isinstance(result, Completed)
        spawned = only(await events_of(result.thread), AgentSpawnedEvent)[0]
        inside = await child_events(store, spawned.data.child_thread_id)
        echoed = only(inside, ToolResultEvent)[0]
        assert (echoed.data.origin, echoed.data.is_error) == ("denied", True)

    asyncio.run(main())


def test_the_parent_budget_covers_the_child_and_a_child_budget_stops_only_the_child() -> None:
    async def main() -> None:
        todo: JsonValue = {"todos": [{"id": "1", "content": "scan", "status": "pending"}]}
        child = agent(
            name="scanner",
            model=scripted_model({"responses": [call("todo_write", todo), text("never")]}),
            budget=Budget(max_model_requests=1),
        )
        lead = agent(
            model=scripted_model({"responses": [spawn("scanner"), text("carried on")]}),
            subagents=[child],
        )
        result = await lead.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        assert result.output == "carried on"
        finished = only(await events_of(result.thread), AgentFinishedEvent)[0]
        assert finished.data.status == "budget_exhausted"

        reviewer = agent(name="reviewer", model=scripted_model({"responses": [text("never")]}))
        tight = agent(
            model=scripted_model({"responses": [spawn("reviewer"), text("never")]}),
            subagents=[reviewer],
        )
        store = sqlite(":memory:")
        over = await tight.run("go", store=store, budget=Budget(max_model_requests=1))
        assert isinstance(over, BudgetExhausted)
        spawned = only(await events_of(over.thread), AgentSpawnedEvent)[0]
        inside = await child_events(store, spawned.data.child_thread_id)
        refused = next(e for e in inside if e.type == "budget_exceeded")
        data = refused.data.model_dump(mode="json")
        assert (data["scope"], data["owner_thread_id"]) == ("ancestor", over.thread.id)

    asyncio.run(main())


def test_subagent_start_can_deny_and_subagent_stop_can_continue_once() -> None:
    async def deny(_call: ToolCallData, _ctx: RunContext[None]) -> SwitchGate:
        return {"decision": "deny", "reason": "no subagents today"}

    stops: list[str] = []

    async def again(finished: AgentFinishedData, _ctx: RunContext[None]) -> StopGate:
        stops.append(finished.status)
        if len(stops) == 1:
            return {"decision": "continue", "reason": "check the tests too"}
        return {"decision": "stop"}

    async def main() -> None:
        reviewer = agent(name="reviewer", model=scripted_model({"responses": []}))
        denied = agent(
            model=scripted_model({"responses": [spawn("reviewer"), text("fine")]}),
            subagents=[reviewer],
            extensions=[extension(name="ops", hooks={"subagent_start": deny})],
        )
        first = await denied.run("go", store=sqlite(":memory:"))
        assert isinstance(first, Completed)
        events = await events_of(first.thread)
        assert not only(events, AgentSpawnedEvent)
        assert only(events, ToolResultEvent)[0].data.origin == "denied"

        twice = agent(name="reviewer", model=scripted_model({"responses": [text("a"), text("b")]}))
        lead = agent(
            model=scripted_model({"responses": [spawn("reviewer"), text("done")]}),
            subagents=[twice],
            extensions=[extension(name="ops", hooks={"subagent_stop": again})],
        )
        store = sqlite(":memory:")
        result = await lead.run("go", store=store)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        decisions = [(d.data.hook, d.data.decision) for d in only(events, HookDecisionEvent)]
        assert decisions == [("subagent_stop", "continue"), ("subagent_stop", "stop")]
        assert only(events, ToolResultEvent)[0].data.preview == "b"
        spawned = only(events, AgentSpawnedEvent)[0]
        inside = await child_events(store, spawned.data.child_thread_id)
        assert [u.data.text for u in only(inside, UserInputEvent)] == [
            "Review the diff.",
            "check the tests too",
        ]

    asyncio.run(main())


def test_a_crash_mid_child_resumes_the_same_child_once() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        broken = agent(name="reviewer", model=scripted_model({"responses": []}))
        lead = agent(model=scripted_model({"responses": [spawn("reviewer")]}), subagents=[broken])
        stream = lead.stream("go", store=store)
        seen = [item.event async for item in stream if isinstance(item, EventItem)]
        first = seen[0]
        with pytest.raises(ScriptExhaustedError):
            await stream.result
        parent = Thread(first.thread_id, first.branch_id, store)
        # The same configs, restarted: the parent recovers its open spawn and resumes the child.
        fixed = agent(name="reviewer", model=scripted_model({"responses": [text("LGTM")]}))
        again = agent(
            model=scripted_model({"responses": [text("done"), text("next")]}),
            subagents=[fixed],
        )
        result = await again.run("next?", store=store, thread=parent)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        spawned = only(events, AgentSpawnedEvent)
        assert len(spawned) == 1
        assert [f.data.status for f in only(events, AgentFinishedEvent)] == ["completed"]
        assert only(events, ToolResultEvent)[0].data.preview == "LGTM"
        inside = await child_events(store, spawned[0].data.child_thread_id)
        assert len(only(inside, ThreadStartedEvent)) == 1
        assert len(only(inside, UserInputEvent)) == 1

    asyncio.run(main())
