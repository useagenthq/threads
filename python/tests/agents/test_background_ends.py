"""When a run's end is decided otherwise, it is not woken and run() returns at once
(spec/schema/README.md, "Run completion" and "Background wakes"): a failed or budget-exhausted
turn, a cancel while the lead waits, and a handoff. A wake turn can also spawn another child, and
the run then waits for that one too."""

import asyncio
from collections.abc import AsyncIterator, Sequence

from agents.wake_kit import ALICE, USAGE, Gated, child, events_of, lates, spawns, text, wakes
from pydantic import JsonValue

from threads import (
    BudgetExhausted,
    Cancelled,
    Completed,
    EventItem,
    Failed,
    HandedOff,
    ModelContext,
    ModelRequest,
    agent,
    scripted_model,
    sqlite,
)
from threads.log import AgentFinishedEvent, Budget, TurnCompletedEvent
from threads.loop.model import ModelChunk
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.thread.handle import open_thread

TOO_LONG: JsonValue = {"error": {"reason": "prompt_too_long", "http_status": 400}}


def test_a_failed_first_turn_stops_its_child_and_the_next_run_succeeds() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        kid = Gated({"responses": [text("Clean.")]}, gate)
        scanner = agent(name="scanner", model=kid)
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [spawns("scanner"), TOO_LONG, text("never")]}),
            subagents=[scanner],
        )
        stream = lead.stream("Scan.", store=store, principal=ALICE, deps=None)
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                gate.set()
        first = await stream.result
        assert isinstance(first, Failed)
        events = await events_of(first.thread)
        assert wakes(events) == []
        assert len(lates(events)) == 1
        finished = [e for e in events if isinstance(e, AgentFinishedEvent)]
        assert [f.data.status for f in finished] == ["cancelled"]
        again = agent(
            name="lead",
            model=scripted_model({"responses": [text("Second.")]}),
            subagents=[scanner],
        )
        second = await again.run("Again.", store=store, principal=ALICE, thread=first.thread)
        assert isinstance(second, Completed)
        assert second.output == "Second."
        assert kid.calls == 1

    asyncio.run(main())


def test_a_wake_turn_that_exhausts_the_budget_ends_the_run_budget_exhausted() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [spawns("scanner"), text("Started."), text("x")]}),
            subagents=[child("scanner", "Clean.", gate)],
        )
        stream = lead.stream(
            "Scan.",
            store=store,
            principal=ALICE,
            deps=None,
            budget=Budget(max_model_requests=2),
        )
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                gate.set()
        result = await stream.result
        assert isinstance(result, BudgetExhausted)
        events = await events_of(result.thread)
        assert [w.data.causes for w in wakes(events)] == [lates(events)]

    asyncio.run(main())


def test_a_cancel_while_the_lead_waits_on_a_hung_child_stops_the_run_at_once() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        kid = Gated({"responses": [text("Clean.")]}, gate)
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [spawns("scanner"), text("Started."), text("never")]}
            ),
            subagents=[agent(name="scanner", model=kid)],
        )
        stream = lead.stream("Scan.", store=store, principal=ALICE, deps=None)
        # The child is inside its model call and never returns: only the cancel on the lead's
        # own log can end the wait.
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                await kid.entered.wait()
                opened = await open_thread(store, item.event.thread_id)
                assert isinstance(opened, Ok)
                assert isinstance(await opened.value.cancel(ALICE), Ok)
        result = await asyncio.wait_for(stream.result, 5)
        gate.set()
        assert isinstance(result, Cancelled)
        assert wakes(await events_of(result.thread)) == []

    asyncio.run(main())


def test_a_lead_that_hands_off_with_a_child_running_is_never_woken() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        target = agent(name="target", model=scripted_model({"responses": [text("Target here.")]}))
        handoff: JsonValue = {
            "content": [
                {
                    "type": "tool_use",
                    "call_id": "h1",
                    "name": "handoff",
                    "input": {"agent": "target"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": USAGE,
        }
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [spawns("scanner"), handoff, text("never")]}),
            subagents=[child("scanner", "Clean.", gate)],
            handoffs=[target],
        )
        stream = lead.stream("Scan, then hand off.", store=store, principal=ALICE, deps=None)
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                gate.set()
        result = await stream.result
        assert isinstance(result, HandedOff)

    asyncio.run(main())


class Stepped(ScriptedModel):
    """A scripted model whose n-th answer waits for the lead's n-th completed turn."""

    def __init__(self, responses: Sequence[JsonValue]) -> None:
        super().__init__(scripted_model({"responses": list(responses)})._entries, {})
        self.turns = [asyncio.Event() for _ in responses]
        self._asked = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self._asked += 1
        await self.turns[self._asked - 1].wait()
        async for chunk in super().send(request, context):
            yield chunk


def test_a_wake_turn_that_spawns_another_child_waits_for_it_too() -> None:
    async def main() -> None:
        kid = Stepped([text("A is clean."), text("B is clean.")])
        responses = [
            spawns("scanner"),
            text("Started A."),
            spawns("scanner", first=2),
            text("Started B."),
            text("Both are clean."),
        ]
        lead = agent(
            name="lead",
            model=scripted_model({"responses": responses}),
            subagents=[agent(name="scanner", model=kid)],
        )
        stream = lead.stream("Scan twice.", store=sqlite(":memory:"), principal=ALICE, deps=None)
        ended = 0
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                if ended < len(kid.turns):
                    kid.turns[ended].set()
                ended += 1
        result = await stream.result
        assert isinstance(result, Completed)
        assert result.output == "Both are clean."
        assert len(wakes(await events_of(result.thread))) == 2  # noqa: PLR2004

    asyncio.run(main())
