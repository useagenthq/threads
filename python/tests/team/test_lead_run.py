"""A lead's run with its team, in-process (spec/schema/README.md, "Teams" and "Run completion"):
it starts members, answers, wakes for each run-owned member's settlement and returns the last
answer with the team. Every test ends with the team's replay check. Mirrors TypeScript's
test/team/lead-run.test.ts."""

import asyncio

from pydantic import JsonValue
from team.run_kit import USAGE, call, events, member_events, receipts, say, sq_of, start, types
from team.team_kit import assert_team_replays

from threads import Agent, Completed, Failed, agent, scripted_model, sqlite
from threads.log import ToolResultEvent
from threads.team.rows import team_row


def _researcher(answer: str) -> Agent[None, str]:
    return agent(name="researcher", model=scripted_model({"responses": [say(answer)]}))


def test_first_answer_the_member_settles_the_lead_wakes_and_gives_the_final_answer() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead = agent(
            name="lead",
            model=scripted_model(
                {
                    "responses": [
                        start("c1", "researcher", "Batteries."),
                        say("Started the researcher."),
                        say("The researcher says battery prices fell."),
                    ]
                }
            ),
            team=[_researcher("Battery prices fell.")],
        )
        r = await lead.run("Research batteries.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "The researcher says battery prices fell."
        assert r.team.ref.tenant == store.tenant
        log = await events(store, r.thread)
        # Both answers stay in the timeline; each lead turn that ended well left it idle.
        assert types(log).count("member_idle") == 2  # noqa: PLR2004 - one per answer
        assert len(receipts(log, "member_settled")) == 1
        member = await member_events(store, r.team.ref.id, "researcher-1")
        assert types(member) == [
            "thread_started",
            "user_input",
            "model_request",
            "model_response",
            "turn_completed",
            "member_idle",
            "message_sent",
        ]
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_members_send_to_the_lead_opens_a_lead_turn_of_the_same_run() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        chatty = agent(
            name="researcher",
            model=scripted_model(
                {
                    "responses": [
                        call("m1", "send", {"to": "lead", "text": "Halfway."}),
                        say("Done."),
                    ]
                }
            ),
        )
        lead = agent(
            name="lead",
            model=scripted_model(
                {
                    "responses": [
                        start("c1", "researcher", "Go."),
                        say("Started."),
                        say("Noted."),
                        say("All done."),
                    ]
                }
            ),
            team=[chatty],
        )
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "All done."
        log = await events(store, r.thread)
        assert len(receipts(log, "message")) == 1
        assert len(receipts(log, "member_settled")) == 1
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_wake_turn_that_fails_after_the_first_answer_fails_the_run_and_closes_the_team() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        # The lead's wake turn's request is refused.
        refused: JsonValue = {"error": {"reason": "provider_error", "http_status": 400}}
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("Started."), refused]}
            ),
            team=[_researcher("Done.")],
        )
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Failed)
        log = await events(store, r.thread)
        # The lead's end closes its team and cancels its live members, in the same append.
        assert types(log)[-3:] == ["turn_completed", "member_ended", "message_sent"]
        sq = await sq_of(store)
        row = await sq.run(lambda c: team_row(c, r.team.ref.id))
        assert row is not None
        assert row.closed_at is not None
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())


def test_an_empty_team_is_one_no_model_can_grow_start_is_refused_unknown_agent() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("No one to start.")]}
            ),
            team=[],
        )
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "No one to start."
        log = await events(store, r.thread)
        result = next(e for e in log if isinstance(e, ToolResultEvent))
        assert result.data.preview == '{"code":"unknown_agent","status":"refused"}'
        assert "message_policy_decided" in types(log)
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_team_limits_concurrent_caps_the_members_starting_and_running_at_once() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        one, two = start("c1", "researcher", "One."), start("c2", "researcher", "Two.")
        assert isinstance(one, dict)
        assert isinstance(two, dict)
        parts = [*_list(one["content"]), *_list(two["content"])]
        both: JsonValue = {"content": parts, "stop_reason": "tool_use", "usage": USAGE}
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [both, say("One started."), say("It reported.")]}),
            team=[_researcher("Done.")],
            team_limits={"concurrent": 1},
        )
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "It reported."
        previews = [
            e.data.preview for e in await events(store, r.thread) if isinstance(e, ToolResultEvent)
        ]
        assert previews[1] == '{"code":"concurrency_cap","status":"refused"}'
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def _list(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value
