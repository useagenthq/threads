"""ask and reply in a running team (spec/schema/README.md, "Teams"; design §4.8 and §4.9): the
asker's call stays pending and its turn parks; the asked member's reply is control mail that closes
the ask answered, resumes the asker and records the call's one result, and the turn goes on. Every
test ends with the team replaying from its logs. Mirrors TypeScript's test/team/ask.test.ts."""

import asyncio
from collections.abc import Callable, Sequence

from pydantic import JsonValue
from team.run_kit import (
    Answering,
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


def _nothing(_request: str) -> JsonValue:
    return say("Nothing more.")


def _scripted(answers: Sequence[Callable[[str], JsonValue]]) -> Answering:
    """A model whose nth answer (from 0) is made from its rendered request."""
    n = [0]

    def answer(request: str) -> JsonValue:
        made = answers[n[0]] if n[0] < len(answers) else _nothing
        n[0] += 1
        return made(request)

    return Answering(answer)


def test_the_lead_asks_a_member_its_reply_is_the_asks_result_and_the_turn_goes_on() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(
            name="researcher",
            model=_scripted(
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
            model=_scripted(
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
