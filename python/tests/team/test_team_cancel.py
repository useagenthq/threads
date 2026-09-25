"""cancel in a running team (spec/schema/README.md, "Teams"; design §4.14): the starter's request
is durable intent; the member's writer applies it as control mail (its receipt and
cancel_requested{scope: tree}), releasing its parks, and the member ends cancelled. A run in
flight is stopped at once (N1). Every test ends with the team replaying from its logs. Mirrors
TypeScript's test/team/cancel.test.ts."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import override

from pydantic import JsonValue
from team.run_kit import (
    Watched,
    call,
    events,
    member_events,
    receipts,
    result_of,
    say,
    sq_of,
    start,
    types,
)
from team.team_kit import assert_team_replays

from threads import Completed, agent, scripted_model, sqlite
from threads.log import AskClosedEvent, Event, MemberEndedEvent
from threads.loop.model import ModelChunk, ModelContext, ModelRequest
from threads.loop.scripted import ScriptedModel
from threads.store.sql import int_of

_FINALS: list[JsonValue] = [say("Final.")] * 6


class _Hanging(ScriptedModel):
    """A model whose request never answers: only the worker stopping its run ends it."""

    def __init__(self, began: asyncio.Event) -> None:
        super().__init__([], {})
        self._began = began

    @override
    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self._began.set()
        await asyncio.Event().wait()
        async for chunk in super().send(request, context):
            yield chunk


def _gate(n: int, event: asyncio.Event) -> Callable[[int], Awaitable[None]]:
    async def on(k: int) -> None:
        if k == n:
            await event.wait()

    return on


def _ended(log: list[Event] | tuple[Event, ...]) -> str:
    ended = next(e for e in log if isinstance(e, MemberEndedEvent))
    return ended.data.result.status


def test_a_member_whose_model_call_is_in_flight_is_stopped_at_once_and_ends_cancelled() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        began = asyncio.Event()
        script = [
            start("c1", "researcher", "Read."),
            call("c2", "cancel", {"member": "researcher-1"}),
        ]
        lead = agent(
            name="lead",
            model=Watched(scripted_model({"responses": script + _FINALS}), _gate(2, began)),
            team=[agent(name="researcher", model=_Hanging(began))],
        )
        r = await asyncio.wait_for(lead.run("Go.", store=store), 20)
        assert isinstance(r, Completed), r
        got = result_of(await events(store, r.thread), "c2")
        assert got["status"] == "cancel_requested"
        member = await member_events(store, r.team.ref.id, "researcher-1")
        assert len(receipts(member, "cancel")) == 1
        assert "cancel_requested" in types(member)
        assert _ended(list(member)) == "cancelled"
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_parked_askers_cancel_closes_its_ask_cancelled_and_it_ends() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        asked, release = asyncio.Event(), asyncio.Event()
        script = [
            start("c1", "researcher", "Read."),
            start("c2", "writer", "Ask researcher-1."),
            call("c3", "cancel", {"member": "writer-1"}),
        ]
        researcher = agent(
            name="researcher",
            model=Watched(
                scripted_model({"responses": [say("Read."), say("Late.")]}), _gate(1, release)
            ),
        )
        writer_script: list[JsonValue] = [
            call("w1", "ask", {"to": "researcher-1", "question": "?"})
        ]
        writer = agent(name="writer", model=scripted_model({"responses": writer_script}))
        lead = agent(
            name="lead",
            model=Watched(scripted_model({"responses": script + _FINALS}), _gate(3, asked)),
            team=[researcher, writer],
        )
        sq = await sq_of(store)  # opened once, before the run: the polls read the run's database
        run = asyncio.ensure_future(lead.run("Go.", store=store))
        parked = "SELECT COUNT(*) FROM team_members WHERE name = 'writer-1' AND state = 'parked'"
        for _ in range(500):
            rows = await sq.run(lambda c: c.execute(parked).fetchall())
            if int_of(rows[0][0]) > 0:
                break
            await asyncio.sleep(0.01)
        asked.set()
        release.set()
        r = await asyncio.wait_for(run, 20)
        assert isinstance(r, Completed), r
        log = list(await member_events(store, r.team.ref.id, "writer-1"))
        closed = next(e for e in log if isinstance(e, AskClosedEvent))
        assert closed.data.outcome.status == "cancelled"
        assert result_of(log, "w1")["status"] == "cancelled"
        assert _ended(log) == "cancelled"
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())


def test_a_member_that_didnt_start_the_target_is_forbidden_and_nothing_is_sent() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        script = [start("c1", "researcher", "Read."), start("c2", "writer", "Cancel it.")]
        writer_script: list[JsonValue] = [
            call("w1", "cancel", {"member": "researcher-1"}),
            say("I may not."),
        ]
        lead = agent(
            name="lead",
            model=scripted_model({"responses": script + _FINALS}),
            team=[
                agent(name="researcher", model=scripted_model({"responses": [say("Read.")]})),
                agent(name="writer", model=scripted_model({"responses": writer_script})),
            ],
        )
        r = await asyncio.wait_for(lead.run("Go.", store=store), 20)
        assert isinstance(r, Completed), r
        writer = await member_events(store, r.team.ref.id, "writer-1")
        assert result_of(writer, "w1") == {"code": "forbidden", "status": "refused"}
        researcher = await member_events(store, r.team.ref.id, "researcher-1")
        assert receipts(researcher, "cancel") == []
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())
