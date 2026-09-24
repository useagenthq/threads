"""agent(team=...) (spec/api.json agent.team): a TeamAgent, whose run() and stream() results carry
the team; setup refuses a member that hands off and a team tool's name. Mirrors TypeScript's
test/team/agent.test.ts."""

import asyncio
import re
from typing import assert_never, assert_type

import pytest
from pydantic import BaseModel
from team.run_kit import say, sq_of, start
from team.team_kit import assert_team_replays

from threads import (
    Agent,
    Completed,
    ConfigError,
    EventItem,
    RunContext,
    RunResult,
    StartResult,
    TeamAgent,
    TeamRunResult,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.team_tools import Started, StartRefused
from threads.log import MemberRef
from threads.result import Err, Ok


def test_the_overloads_no_team_is_an_agent_team_empty_or_a_list_is_a_team_agent() -> None:
    async def main() -> None:
        plain = agent(model=scripted_model({"responses": [say("Hi.")]}))
        assert_type(plain, Agent[None, str])
        lead = agent(model=scripted_model({"responses": [say("Hi.")]}), team=[])
        assert_type(lead, TeamAgent[None, str])
        r = await plain.run("Hi.", store=sqlite(":memory:"))
        assert_type(r, RunResult[str])
        assert not hasattr(r, "team")
        led = await lead.run("Hi.", store=sqlite(":memory:"))
        assert_type(led, TeamRunResult[str])
        assert re.fullmatch(r"[0-9a-f-]{36}", led.team.ref.id)

        class N(BaseModel):
            n: int

        typed = agent(model=scripted_model({"responses": [say("Hi.")]}), output=N, team=[])
        assert_type(typed, TeamAgent[None, N])
        assert typed.name == "agent"

    asyncio.run(main())


def test_stream_the_committed_events_then_a_result_with_the_team() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(name="researcher", model=scripted_model({"responses": [say("Hi.")]}))
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("Started."), say("Done.")]}
            ),
            team=[researcher],
        )
        run = lead.stream("Work.", store=store)
        seen = [item.event.type async for item in run if isinstance(item, EventItem)]
        result = await run.result
        assert isinstance(result, Completed), result
        assert result.output == "Done."
        assert "member_started" in seen
        assert result.team.ref.tenant == store.tenant
        await assert_team_replays(await sq_of(store), result.team.ref.id)

    asyncio.run(main())


def test_check_a_member_that_hands_off_is_refused_handoff_in_team() -> None:
    async def main() -> None:
        target = agent(name="billing", model=scripted_model({"responses": []}))
        desk = agent(name="desk", model=scripted_model({"responses": []}), handoffs=[target])
        lead = agent(name="lead", model=scripted_model({"responses": []}), team=[desk])
        checked = await lead.check()
        assert isinstance(checked, Err)
        assert checked.error.code == "handoff_in_team"
        with pytest.raises(ConfigError):
            await lead.run("Hi.", store=sqlite(":memory:"))

    asyncio.run(main())


class _Q(BaseModel):
    q: str


async def _ok(_args: _Q, _ctx: RunContext[None]) -> str:
    return "ok"


def test_check_an_own_tool_named_like_a_team_tool_is_refused_duplicate_name_naming_it() -> None:
    async def main() -> None:
        ask = tool(
            name="ask",
            description="Ask someone.",
            input=_Q,
            runs="host",
            effect="read_only",
            execute=_ok,
        )
        lead = agent(model=scripted_model({"responses": []}), tools=[ask], team=[])
        checked = await lead.check()
        assert isinstance(checked, Err)
        assert checked.error.code == "duplicate_name"
        assert "tool ask" in checked.error.message
        # Outside a team the name is free.
        assert await agent(model=scripted_model({"responses": []}), tools=[ask]).check() == Ok(None)

        # A listed member's own tool is refused at the lead's setup too.
        desk = agent(name="desk", model=scripted_model({"responses": []}), tools=[ask])
        member = await agent(model=scripted_model({"responses": []}), team=[desk]).check()
        assert isinstance(member, Err)
        assert member.error.code == "duplicate_name"
        assert "agent desk" in member.error.message

    asyncio.run(main())


def test_check_two_agents_of_one_name_in_a_team_tree_are_refused_duplicate_name() -> None:
    async def main() -> None:
        lead = agent(
            model=scripted_model({"responses": []}),
            team=[
                agent(name="writer", model=scripted_model({"responses": []})),
                agent(name="writer", model=scripted_model({"responses": []})),
            ],
        )
        checked = await lead.check()
        assert isinstance(checked, Err)
        assert checked.error.code == "duplicate_name"

    asyncio.run(main())


def test_assert_never_ends_a_match_over_a_tool_results_statuses() -> None:
    def describe(r: StartResult) -> str:
        match r:
            case Started():
                return r.member.name
            case StartRefused():
                return r.code
            case _:
                assert_never(r)

    assert describe(StartRefused("team_closed")) == "team_closed"
    member = MemberRef(
        tenant="local", team="0192e001-0000-7000-8000-000000000001", name="writer-1", generation=1
    )
    assert describe(Started(member)) == "writer-1"
