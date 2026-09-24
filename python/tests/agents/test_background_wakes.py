"""Legacy background subagents wake their lead (spec/schema/README.md, "Background wakes" and
"Run completion"; Gate 1 §2.7.3): a late result recorded while no turn is open carries a woken in
the same append, the woken turn belongs to the run that spawned the child, run() returns the
answer after the last wake, and results of two runs are never recorded under one woken."""

import asyncio

import pytest
from agents.wake_kit import (
    ALICE,
    BOB,
    USAGE,
    child,
    events_of,
    lates,
    spawns,
    streamed,
    text,
    wakes,
)
from pydantic import BaseModel, JsonValue

from threads import (
    Cancelled,
    Completed,
    EventItem,
    RunContext,
    Thread,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.background import Background
from threads.log import ToolResultEvent, ToolResultLateEvent
from threads.loop.scripted import ScriptExhaustedError
from threads.result import Ok
from threads.thread.handle import open_thread


@pytest.fixture
def one_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead records ends only once every running child has ended, so children that end
    close together are recorded at one boundary however the event loop orders them."""

    async def every_end(self: Background) -> None:
        done, _ = await asyncio.wait(self.running.values())
        for task in done:
            task.result()

    monkeypatch.setattr(Background, "next_end", every_end)


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


def test_a_run_cancelled_mid_turn_returns_cancelled_and_is_never_woken() -> None:
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
        assert wakes(events) == []

    asyncio.run(main())
