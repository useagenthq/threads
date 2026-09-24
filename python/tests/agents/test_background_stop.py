"""A stop reaches the whole tree (spec/schema/README.md, "Subagent cancellation and parking" and
"Run completion"): a tree cancel or an early end bars a subagent that is idle while it waits on
its own background subagents, so it never wakes; a cancel during the early-end stop ends the
wait; and a run right after a cancel adopts the subagent still finishing its step."""

import asyncio
from collections.abc import AsyncIterator, Sequence

from agents.wake_kit import ALICE, Gated, events_of, spawns, text, wakes
from pydantic import JsonValue

from threads import (
    Agent,
    Cancelled,
    Completed,
    EventItem,
    Failed,
    ModelContext,
    ModelRequest,
    Store,
    Thread,
    agent,
    scripted_model,
    sqlite,
)
from threads.agents.store import open_store
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    CancelRequestedEvent,
    Event,
    TurnCompletedEvent,
)
from threads.loop.model import ModelChunk
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.thread.handle import open_thread

TOO_LONG: JsonValue = {"error": {"reason": "prompt_too_long", "http_status": 400}}


async def child_log(store: Store, parent: Sequence[Event]) -> list[Event]:
    """The log of the first background child the given log spawned."""
    spawned = next((e for e in parent if isinstance(e, AgentSpawnedEvent)), None)
    if spawned is None:
        return []
    sq = await open_store(store)
    root = await sq.root(spawned.data.child_thread_id)
    if not isinstance(root, Ok):
        return []
    read = await sq.read(root.value, 0)
    return list(read.value.fold.events) if isinstance(read, Ok) else []


def tree(gate: asyncio.Event, lead_script: list[JsonValue]) -> Agent[None, str]:
    """lead -> mid (background) -> grand (background, gated): mid answers, then waits."""
    grand = agent(name="grand", model=Gated({"responses": [text("Grand done.")]}, gate))
    mid = agent(
        name="mid",
        model=scripted_model(
            {"responses": [spawns("grand"), text("Mid started."), text("Mid woke.")]}
        ),
        subagents=[grand],
    )
    return agent(name="lead", model=scripted_model({"responses": lead_script}), subagents=[mid])


def test_a_tree_cancel_bars_an_idle_middle_subagent_so_it_never_wakes() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        lead = tree(gate, [spawns("mid"), text("Lead started."), text("never")])
        stream = lead.stream("Go.", store=store, principal=ALICE, deps=None)
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                here = Thread(item.event.thread_id, item.event.branch_id, store)
                for _ in range(400):
                    mid = await child_log(store, await events_of(here))
                    if any(isinstance(e, TurnCompletedEvent) for e in mid):
                        break
                    await asyncio.sleep(0.005)
                opened = await open_thread(store, item.event.thread_id)
                assert isinstance(opened, Ok)
                assert isinstance(await opened.value.cancel(ALICE), Ok)
        result = await stream.result
        assert isinstance(result, Cancelled)
        gate.set()
        await asyncio.sleep(0.1)
        mid = await child_log(store, await events_of(result.thread))
        assert wakes(mid) == []
        assert any(isinstance(e, CancelRequestedEvent) for e in mid)

    asyncio.run(main())


def test_an_early_end_bars_the_middle_subagent_so_it_never_wakes() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        lead = tree(gate, [spawns("mid"), TOO_LONG, text("never")])
        result = await lead.run("Go.", store=store, principal=ALICE, deps=None)
        assert isinstance(result, Failed)
        mid = await child_log(store, await events_of(result.thread))
        assert wakes(mid) == []
        assert any(isinstance(e, CancelRequestedEvent) for e in mid)
        gate.set()

    asyncio.run(main())


class FailsOnceKidIsIn(ScriptedModel):
    """The lead's model: its second answer (the failure) waits until the child is in its call."""

    def __init__(self, entered: asyncio.Event) -> None:
        super().__init__(scripted_model({"responses": [spawns("scanner"), TOO_LONG]})._entries, {})
        self._entered = entered
        self._asked = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self._asked += 1
        if self._asked == 2:  # noqa: PLR2004
            await self._entered.wait()
        async for chunk in super().send(request, context):
            yield chunk


def test_a_cancel_during_the_early_end_stop_ends_the_wait_even_while_a_child_hangs() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        kid = Gated({"responses": [text("Clean.")]}, gate)
        lead = agent(
            name="lead",
            model=FailsOnceKidIsIn(kid.entered),
            subagents=[agent(name="scanner", model=kid)],
        )
        stream = lead.stream("Scan.", store=store, principal=ALICE, deps=None)
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                opened = await open_thread(store, item.event.thread_id)
                assert isinstance(opened, Ok)
                assert isinstance(await opened.value.cancel(ALICE), Ok)
        result = await asyncio.wait_for(stream.result, 5)
        gate.set()
        assert isinstance(result, Failed)

    asyncio.run(main())


def test_a_run_right_after_a_cancel_adopts_the_subagent_and_succeeds() -> None:
    async def main() -> None:
        store, gate = sqlite(":memory:"), asyncio.Event()
        kid = Gated({"responses": [text("Clean.")]}, gate)
        scanner = agent(name="scanner", model=kid)
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [spawns("scanner"), text("Started."), text("x")]}),
            subagents=[scanner],
        )
        stream = lead.stream("Scan.", store=store, principal=ALICE, deps=None)
        async for item in stream:
            if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
                await kid.entered.wait()
                opened = await open_thread(store, item.event.thread_id)
                assert isinstance(opened, Ok)
                assert isinstance(await opened.value.cancel(ALICE), Ok)
        first = await stream.result
        assert isinstance(first, Cancelled)
        asyncio.get_running_loop().call_later(0.02, gate.set)
        again = agent(
            name="lead",
            model=scripted_model({"responses": [text("Second.")]}),
            subagents=[scanner],
        )
        second = await again.run(
            "Again.", store=store, principal=ALICE, thread=first.thread, deps=None
        )
        assert isinstance(second, Completed)
        assert second.output == "Second."
        assert kid.calls == 1
        events = await events_of(first.thread)
        assert len([e for e in events if isinstance(e, AgentFinishedEvent)]) == 1
        assert wakes(events) == []

    asyncio.run(main())
