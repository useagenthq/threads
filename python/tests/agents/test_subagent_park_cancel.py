"""Subagent parking and tree-wide cancellation (spec/schema/README.md, "Subagent cancellation
and parking"; ): a child that parks parks its parent on {kind: child} and resumes it
once settled; cancelling a parent bars every unfinished descendant, the child's agent_finished
and result land before the parent's cancelled, and nothing is dispatched after a barrier."""

import asyncio
from collections.abc import Sequence

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Cancelled,
    Completed,
    EventItem,
    Failed,
    Parked,
    RunContext,
    Thread,
    agent,
    extension,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.run import execute
from threads.agents.store import now_ms, open_store
from threads.hooks.types import StopGate
from threads.log import (
    AgentFinishedData,
    AgentFinishedEvent,
    AgentSpawnedEvent,
    CancelledEvent,
    CancelRequestedEvent,
    Event,
    ModelRequestEvent,
    ParkAddress,
    ParkedEvent,
    ResumedEvent,
    ThreadId,
    ThreadStartedEvent,
    ToolResultEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.result import Err, Ok
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


SPAWN = use("spawn_agent", {"agent": "worker", "prompt": "Send it."})


class Note(BaseModel):
    text: str


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def _drop(_item: object) -> None:
    pass


def team(sent: list[str], worker: Sequence[JsonValue], lead: Sequence[JsonValue]) -> Agent[None]:
    """A lead whose worker sends through a host tool that needs approval; a child only has
    tools its parent has too."""

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return "sent"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    child = agent(
        name="worker", model=scripted_model({"responses": list(worker)}), tools=[send_tool]
    )
    script: JsonValue = {"responses": list(lead)}
    return agent(model=scripted_model(script), tools=[send_tool], subagents=[child])


def test_a_child_that_parks_parks_its_parent_and_resumes_it_once_settled() -> None:
    sent: list[str] = []

    async def main() -> None:
        store = sqlite(":memory:")
        lead = team(sent, [use("send", {"text": "x"}), text("Sent.")], [SPAWN, text("All done.")])
        parked = await lead.run("go", store=store, deps=None)
        assert isinstance(parked, Parked), parked
        events = await events_of(parked.thread)
        spawned = next(e for e in events if isinstance(e, AgentSpawnedEvent))
        child = spawned.data.child_thread_id
        address = ParkAddress(kind="child", id=child)
        assert parked.reason == "awaiting_approval"
        assert address in parked.pending
        (park,) = [e for e in events if isinstance(e, ParkedEvent)]
        assert (park.data.address, park.data.reason) == (address, "awaiting_approval")
        assert not any(isinstance(e, AgentFinishedEvent) for e in events)
        # A second run while the child still waits parks again, recording nothing new.
        again = await execute(lead.definition, None, {"thread": parked.thread}, None, _drop)
        assert isinstance(again, Parked)
        assert len(await events_of(parked.thread)) == len(events)
        opened = await open_thread(store, child)
        assert isinstance(opened, Ok)
        pending = await opened.value.pending_approvals()
        assert isinstance(pending, Ok)
        (challenge,) = pending.value
        assert isinstance(await opened.value.approve(challenge.challenge_id, LOCAL_OPERATOR), Ok)
        done = await execute(lead.definition, None, {"thread": parked.thread}, None, _drop)
        assert isinstance(done, Completed), done
        assert done.output == "All done."
        after = (await events_of(parked.thread))[len(events) :]
        resumed = next(e for e in after if isinstance(e, ResumedEvent))
        assert (resumed.data.address, resumed.data.cause_event_id) == (address, spawned.event_id)
        (finished,) = [e for e in after if isinstance(e, AgentFinishedEvent)]
        assert finished.data.status == "completed"

    asyncio.run(main())
    assert sent == ["x"]


def test_a_busy_child_lease_halts_the_parent_without_finishing_the_child() -> None:
    """Another process holding the child's lease is transient: the parent's run ends
    branch_busy, records no agent_finished, and a later run collects the child."""
    sent: list[str] = []

    async def main() -> None:
        store = sqlite(":memory:")
        lead = team(sent, [use("send", {"text": "x"}), text("Sent.")], [SPAWN, text("All done.")])
        parked = await lead.run("go", store=store, deps=None)
        assert isinstance(parked, Parked), parked
        spawned = next(
            e for e in await events_of(parked.thread) if isinstance(e, AgentSpawnedEvent)
        )
        child = await open_thread(store, spawned.data.child_thread_id)
        assert isinstance(child, Ok)
        zombie = await (await open_store(store)).acquire(child.value.branch, "zombie", now_ms)
        assert isinstance(zombie, Ok)
        busy = await execute(lead.definition, None, {"thread": parked.thread}, None, _drop)
        assert isinstance(busy, Failed), busy
        assert busy.error.code == "branch_busy"
        events = await events_of(parked.thread)
        assert not any(isinstance(e, AgentFinishedEvent) for e in events)
        await zombie.value.release()
        pending = await child.value.pending_approvals()
        assert isinstance(pending, Ok)
        (challenge,) = pending.value
        assert isinstance(await child.value.approve(challenge.challenge_id, LOCAL_OPERATOR), Ok)
        done = await execute(lead.definition, None, {"thread": parked.thread}, None, _drop)
        assert isinstance(done, Completed), done
        events = await events_of(parked.thread)
        (finished,) = [e for e in events if isinstance(e, AgentFinishedEvent)]
        assert finished.data.status == "completed"

    asyncio.run(main())
    assert sent == ["x"]


async def _keep_going(_finished: AgentFinishedData, _ctx: RunContext[None]) -> StopGate:
    return {"decision": "continue", "reason": "Keep going."}


@pytest.mark.parametrize("keep_going", [False, True])
def test_cancelling_a_parent_cancels_its_running_child_before_its_own_stop(
    keep_going: bool,
) -> None:
    """A tree cancel is final: a subagent_stop "continue" never runs a cancelled child on."""

    async def stop(_args: Note, ctx: RunContext[None]) -> str:
        # The parent is cancelled while its foreground child runs.
        parent = await _parent_of(ctx.thread_id, store)
        assert isinstance(await parent.cancel(LOCAL_OPERATOR), Ok)
        return "stopping"

    store = sqlite(":memory:")
    stop_tool = tool(
        name="stop", description="Stop.", input=Note, runs="host", execute=stop, effect="read_only"
    )
    worker = agent(
        name="worker",
        model=scripted_model({"responses": [use("stop", {"text": "x"}), *[text("never")] * 4]}),
        tools=[stop_tool],
    )
    hooks = [extension(name="ops", hooks={"subagent_stop": _keep_going})] if keep_going else []
    lead = agent(
        model=scripted_model({"responses": [SPAWN, text("never")]}),
        tools=[stop_tool],
        subagents=[worker],
        extensions=hooks,
    )

    async def main() -> None:
        result = await lead.run("go", store=store, deps=None)
        assert isinstance(result, Cancelled), result
        events = await events_of(result.thread)
        kinds = [e.type for e in events[-6:]]
        assert kinds == [
            "agent_spawned",
            "cancel_requested",
            "agent_finished",
            "tool_result",
            "cancelled",
            "turn_completed",
        ]
        finished = next(e for e in events if isinstance(e, AgentFinishedEvent))
        assert finished.data.status == "cancelled"
        answered = next(e for e in events if isinstance(e, ToolResultEvent))
        assert (answered.data.preview, answered.data.is_error) == ("cancelled: cancelled", True)
        child = await open_thread(store, finished.data.child_thread_id)
        assert isinstance(child, Ok)
        barrier = next(
            e for e in await events_of(child.value) if isinstance(e, CancelRequestedEvent)
        )
        assert (barrier.data.scope, barrier.data.reason) == ("tree", "ancestor cancelled")
        assert (barrier.actor.kind, barrier.actor.principal) == ("host", LOCAL_OPERATOR)
        kids = await events_of(child.value)
        assert any(isinstance(e, CancelledEvent) for e in kids)
        after = kids[kids.index(barrier) :]
        assert not any(isinstance(e, (ModelRequestEvent, UserInputEvent)) for e in after)

    asyncio.run(main())


def test_a_cancelled_parent_waits_on_a_parked_child_then_stops_without_dispatching() -> None:
    sent: list[str] = []

    async def main() -> None:
        store = sqlite(":memory:")
        lead = team(sent, [use("send", {"text": "x"}), text("never")], [SPAWN, text("never")])
        parked = await lead.run("go", store=store, deps=None)
        assert isinstance(parked, Parked)
        assert isinstance(await parked.thread.cancel(LOCAL_OPERATOR), Ok)
        done = await execute(lead.definition, None, {"thread": parked.thread}, None, _drop)
        assert isinstance(done, Cancelled), done
        events = await events_of(parked.thread)
        finished = next(e for e in events if isinstance(e, AgentFinishedEvent))
        assert finished.data.status == "cancelled"
        order = [e.type for e in events if e.type in ("agent_finished", "cancelled")]
        assert order == ["agent_finished", "cancelled"]
        assert isinstance(events[-1], TurnCompletedEvent)
        child = await open_thread(store, finished.data.child_thread_id)
        assert isinstance(child, Ok)
        kids = await events_of(child.value)
        assert any(isinstance(e, CancelledEvent) for e in kids)

    asyncio.run(main())
    assert sent == []


def test_a_background_child_that_parks_parks_its_parent_after_the_turn() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        background = use("spawn_agent", {"agent": "worker", "prompt": "Send.", "background": True})
        lead = team([], [use("send", {"text": "x"})], [background, text("Started it.")])
        parked = await lead.run("go", store=store, deps=None)
        assert isinstance(parked, Parked), parked
        events = await events_of(parked.thread)
        (park,) = [e for e in events if isinstance(e, ParkedEvent)]
        assert park.data.address.kind == "child"
        assert not any(isinstance(e, AgentFinishedEvent) for e in events)

    asyncio.run(main())


class _CrashError(Exception):
    pass


def test_a_child_whose_thread_was_never_created_is_recorded_cancelled_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between agent_spawned and the child's thread, then a cancel: the recovered
    parent records the child cancelled without ever creating its thread."""

    async def crash(*_args: object) -> None:
        raise _CrashError

    async def main() -> None:
        store = sqlite(":memory:")
        lead = team([], [], [SPAWN])
        monkeypatch.setattr("threads.agents.start.open_launched", crash)
        stream = lead.stream("go", store=store, deps=None)
        seen = [i.event async for i in stream if isinstance(i, EventItem)]
        with pytest.raises(_CrashError):
            await stream.result
        monkeypatch.undo()
        thread = Thread(seen[0].thread_id, seen[0].branch_id, store)
        assert isinstance(await thread.cancel(LOCAL_OPERATOR), Ok)
        done = await execute(lead.definition, None, {"thread": thread}, None, _drop)
        assert isinstance(done, Cancelled), done
        events = await events_of(thread)
        finished = next(e for e in events if isinstance(e, AgentFinishedEvent))
        assert (finished.data.status, finished.data.usage.input_tokens) == ("cancelled", None)
        assert isinstance(await open_thread(store, finished.data.child_thread_id), Err)

    asyncio.run(main())


async def _parent_of(child: ThreadId, store: object) -> Thread:
    opened = await open_thread(store, child)  # type: ignore[arg-type] - the test's store
    assert isinstance(opened, Ok)
    started = next(e for e in await events_of(opened.value) if isinstance(e, ThreadStartedEvent))
    parent = started.data.parent
    assert parent is not None
    found = await open_thread(store, ThreadId(str(parent.thread_id)))  # type: ignore[arg-type] - the test's store
    assert isinstance(found, Ok)
    return found.value
