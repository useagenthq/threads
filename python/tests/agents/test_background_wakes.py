"""Legacy background subagents wake their lead (spec/schema/README.md, "Background wakes" and
"Run completion"; Gate 1 §2.7.3): a late result recorded while no turn is open carries a woken in
the same append, the woken turn belongs to the run that spawned the child, run() returns the
answer after the last wake, and results of two runs are never recorded under one woken."""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Cancelled,
    Completed,
    EventItem,
    ModelContext,
    ModelRequest,
    RunContext,
    RunResult,
    Store,
    Thread,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.background import Background
from threads.log import (
    Event,
    Principal,
    ToolResultEvent,
    ToolResultLateEvent,
    TurnCompletedEvent,
    WokenEvent,
)
from threads.loop.model import ModelChunk
from threads.loop.scripted import ScriptedModel, ScriptExhaustedError
from threads.result import Ok
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="local", subject="alice")
BOB = Principal(issuer="api", tenant="local", subject="bob")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def spawns(*names: str, first: int = 1) -> JsonValue:
    parts: list[JsonValue] = [
        {
            "type": "tool_use",
            "call_id": f"c{i}",
            "name": "spawn_agent",
            "input": {"agent": name, "prompt": "Scan.", "background": True},
        }
        for i, name in enumerate(names, first)
    ]
    return {"content": parts, "stop_reason": "tool_use", "usage": USAGE}


class Gated(ScriptedModel):
    """A scripted model whose every answer waits for `gate`."""

    def __init__(self, script: Mapping[str, JsonValue], gate: asyncio.Event) -> None:
        super().__init__(scripted_model(script)._entries, {})
        self._gate = gate

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        await self._gate.wait()
        async for chunk in super().send(request, context):
            yield chunk


def child(name: str, reply: str, gate: asyncio.Event) -> Agent[None, str]:
    return agent(name=name, model=Gated({"responses": [text(reply)]}, gate))


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


@pytest.fixture
def one_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead records ends only once every running child has ended, so children that end
    close together are recorded at one boundary however the event loop orders them."""

    async def every_end(self: Background) -> None:
        done, _ = await asyncio.wait(self.running.values())
        for task in done:
            task.result()

    monkeypatch.setattr(Background, "next_end", every_end)


def lates(events: Sequence[Event]) -> list[str]:
    return [e.event_id for e in events if isinstance(e, ToolResultLateEvent)]


def wakes(events: Sequence[Event]) -> list[WokenEvent]:
    return [e for e in events if isinstance(e, WokenEvent)]


async def streamed(
    lead: Agent[None, str],
    text_in: str,
    gate: asyncio.Event,
    where: Store | Thread,
    principal: Principal = ALICE,
) -> RunResult[str]:
    """Runs `lead` on a new thread of a store, or on a thread, opening `gate` once its first
    turn completes."""
    stream = (
        lead.stream(text_in, store=where.store, principal=principal, thread=where, deps=None)
        if isinstance(where, Thread)
        else lead.stream(text_in, store=where, principal=principal, deps=None)
    )
    async for item in stream:
        if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
            gate.set()
    return await stream.result


def test_the_late_result_and_its_woken_are_one_block_and_run_returns_the_wake_answer() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [spawns("scanner"), text("Scan started."), text("Nothing found.")]}
            ),
            subagents=[child("scanner", "No vulnerable deps.", gate)],
        )
        result = await streamed(lead, "Scan in the background.", gate, store)
        assert isinstance(result, Completed)
        assert result.output == "Nothing found."
        events = await events_of(result.thread)
        kinds = [e.type for e in events]
        at = kinds.index("agent_finished")
        assert kinds[at:] == [
            "agent_finished",
            "tool_result_late",
            "woken",
            "model_request",
            "model_response",
            "turn_completed",
        ]
        assert [w.data.causes for w in wakes(events)] == [lates(events)]
        assert wakes(events)[0].actor.principal == ALICE
        results = [e for e in events if isinstance(e, ToolResultEvent | ToolResultLateEvent)]
        assert [(e.type, e.data.preview) for e in results] == [
            ("tool_result", "started in background"),
            ("tool_result_late", "No vulnerable deps."),
        ]

    asyncio.run(main())


@pytest.mark.usefixtures("one_boundary")
def test_two_children_of_one_run_ending_at_one_boundary_share_one_woken() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [spawns("scanner", "licenses"), text("Both started."), text("Done.")]}
            ),
            subagents=[child("scanner", "Clean.", gate), child("licenses", "MIT only.", gate)],
        )
        result = await streamed(lead, "Scan and check.", gate, store)
        assert isinstance(result, Completed)
        assert result.output == "Done."
        events = await events_of(result.thread)
        calls = [e.data.call_id for e in events if isinstance(e, ToolResultLateEvent)]
        assert calls == ["c1", "c2"]
        assert [w.data.causes for w in wakes(events)] == [lates(events)]

    asyncio.run(main())


@pytest.mark.usefixtures("one_boundary")
def test_children_of_two_runs_at_one_boundary_get_one_woken_each_in_spawn_order() -> None:
    """The writer rule: a mixed boundary never leaves one run's late result without its wake."""

    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        crashing = agent(name="scanner", model=scripted_model({"responses": []}))
        first = agent(
            name="lead",
            model=scripted_model({"responses": [spawns("scanner"), text("Alice's scan runs.")]}),
            subagents=[crashing, child("licenses", "unused", gate)],
        )
        # Alice's child dies with the process: her run ends with it still to report.
        stream = first.stream("Scan the dependencies.", store=store, principal=ALICE)
        seen = [item.event async for item in stream if isinstance(item, EventItem)]
        with pytest.raises(ScriptExhaustedError):
            await stream.result
        thread = Thread(seen[0].thread_id, seen[0].branch_id, store)
        second = agent(
            name="lead",
            model=scripted_model(
                {
                    "responses": [
                        spawns("licenses", first=2),
                        text("Bob's check runs."),
                        text("Alice's scan is clean."),
                        text("Bob's licenses are MIT."),
                    ]
                }
            ),
            subagents=[child("scanner", "Clean.", gate), child("licenses", "MIT only.", gate)],
        )
        result = await streamed(second, "Check the licenses.", gate, thread, BOB)
        assert isinstance(result, Completed)
        assert result.output == "Bob's licenses are MIT."
        events = await events_of(result.thread)
        woken = wakes(events)
        assert [w.actor.principal for w in woken] == [ALICE, BOB]
        assert [w.data.causes for w in woken] == [[late] for late in lates(events)]

    asyncio.run(main())


class Empty(BaseModel):
    pass


def test_a_late_result_after_the_lead_was_cancelled_is_recorded_with_no_woken() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()

        async def stop(_args: Empty, ctx: RunContext[None]) -> str:
            opened = await open_thread(store, ctx.thread_id)
            assert isinstance(opened, Ok)
            assert isinstance(await opened.value.cancel(ALICE), Ok)
            return "stopping"

        stopping = tool(
            name="stop",
            description="Stop.",
            input=Empty,
            runs="host",
            execute=stop,
            effect="read_only",
        )
        stop_call: JsonValue = {
            "content": [{"type": "tool_use", "call_id": "c2", "name": "stop", "input": {}}],
            "stop_reason": "tool_use",
            "usage": USAGE,
        }
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [spawns("scanner"), stop_call]}),
            subagents=[child("scanner", "Clean.", gate)],
            tools=[stopping],
        )
        result = await streamed(lead, "Scan, then stop.", gate, store)
        assert isinstance(result, Cancelled)
        events = await events_of(result.thread)
        assert len(lates(events)) == 1
        assert wakes(events) == []

    asyncio.run(main())
