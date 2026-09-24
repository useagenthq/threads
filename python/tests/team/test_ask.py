"""ask and reply in a running team (spec/schema/README.md, "Teams"; design §4.8 and §4.9): the
asker's call stays pending and its turn parks; the asked member's reply is control mail that closes
the ask answered, resumes the asker and records the call's one result, and the turn goes on. Every
test ends with the team replaying from its logs. Mirrors TypeScript's test/team/ask.test.ts."""

import asyncio
from typing import TYPE_CHECKING

from team.run_kit import (
    answers,
    call,
    events,
    member_events,
    receipts,
    reply_to,
    result_of,
    say,
    sq_of,
    start,
    types,
)
from team.team_kit import assert_team_replays

from threads import Completed, agent, scripted_model, sqlite
from threads.log import Budget

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_the_lead_asks_a_member_its_reply_is_the_asks_result_and_the_turn_goes_on() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(
            name="researcher",
            model=answers(
                [
                    lambda _r: say("Read about batteries."),
                    lambda r: reply_to("r1", r, "Batteries."),
                    lambda _r: say("Replied."),
                ]
            ),
        )
        lead_script: list[JsonValue] = [
            start("c1", "researcher", "Topic: batteries."),
            call("c2", "ask", {"to": "researcher-1", "question": "Which topic?"}),
            say("It read about batteries."),
            say("Final."),
            say("Final."),
        ]
        lead = agent(
            name="lead", model=scripted_model({"responses": lead_script}), team=[researcher]
        )
        r = await lead.run("Find the topic.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "Final."
        log = await events(store, r.thread)
        got = result_of(log, "c2")
        assert isinstance(got, dict)
        assert got["status"] == "answered"
        assert got["text"] == "Batteries."
        member = got["member"]
        assert isinstance(member, dict)
        assert (member["name"], member["generation"]) == ("researcher-1", 1)
        # Parked on the ask, resumed by the reply: the reply's receipt, the close, the resume
        # and the call's result are one append.
        seen = types(log)
        closed = seen.index("ask_closed")
        assert seen[closed - 1 : closed + 3] == [
            "message_received",
            "ask_closed",
            "resumed",
            "tool_result",
        ]
        assert "parked" in seen
        asked = await member_events(store, r.team.ref.id, "researcher-1")
        assert len(receipts(asked, "ask")) == 1
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_member_asks_another_member_and_parks_the_reply_runs_it_on() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(
            name="researcher",
            model=answers(
                [
                    lambda _r: say("Read."),
                    lambda r: reply_to("r1", r, "Batteries."),
                    lambda _r: say("Replied."),
                ]
            ),
        )
        writer_script: list[JsonValue] = [
            call("w1", "ask", {"to": "researcher-1", "question": "Which topic?"}),
            say("Report: batteries."),
        ]
        writer = agent(name="writer", model=scripted_model({"responses": writer_script}))
        lead_script: list[JsonValue] = [
            start("c1", "researcher", "Read the notes."),
            start("c2", "writer", "Ask researcher-1 its topic, then report."),
            say("Started."),
            say("Final."),
            say("Final."),
            say("Final."),
        ]
        lead = agent(
            name="lead",
            model=scripted_model({"responses": lead_script}),
            team=[researcher, writer],
        )
        r = await lead.run("Report.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "Final."
        member = await member_events(store, r.team.ref.id, "writer-1")
        got = result_of(member, "w1")
        assert isinstance(got, dict)
        assert (got["status"], got["text"]) == ("answered", "Batteries.")
        assert "member_idle" in types(member)
        # The writer's first park sent its lead one member_parked notice.
        assert len(receipts(await events(store, r.thread), "member_parked")) == 1
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_an_ask_of_an_unknown_member_is_refused_and_nothing_parks() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead_script: list[JsonValue] = [
            call("c1", "ask", {"to": "nobody-1", "question": "Hello?"}),
            say("Nobody is there."),
        ]
        member = agent(name="researcher", model=scripted_model({"responses": []}))
        lead = agent(name="lead", model=scripted_model({"responses": lead_script}), team=[member])
        r = await lead.run("Ask.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "Nobody is there."
        log = await events(store, r.thread)
        assert result_of(log, "c1") == {"code": "unknown_member", "status": "refused"}
        assert "parked" not in types(log)
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_an_ask_of_a_member_with_no_room_in_its_own_budget_is_refused_budget_exceeded() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead_script: list[JsonValue] = [
            start("c1", "a", "Go."),
            call("c2", "wait", {"members": ["a-1"]}),
            call("c3", "ask", {"to": "a-1", "question": "More?"}),
            say("No room."),
            say("Final."),
        ]
        member = agent(
            name="a",
            budget=Budget(max_model_requests=1),
            model=scripted_model({"responses": [say("done."), say("more.")]}),
        )
        lead = agent(name="lead", model=scripted_model({"responses": lead_script}), team=[member])
        r = await lead.run("Go.", store=store)
        log = await events(store, r.thread)
        assert result_of(log, "c3") == {"code": "budget_exceeded", "status": "refused"}
        assert receipts(await member_events(store, r.team.ref.id, "a-1"), "ask") == []
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())
