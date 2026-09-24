"""Tenants (design §2.6, "Tenant"): a team lives in its lead's tenant, frozen into every ref, and
nothing crosses tenants: a lead of another tenant, on the same database, can't address its members;
each team's rows and replay stay its own. Mirrors TypeScript's test/team/tenants.test.ts."""

import asyncio

from team.run_kit import call, events, say, sq_of, start
from team.team_kit import assert_team_replays

from threads import Completed, agent, scripted_model, sqlite
from threads.agents.store import scoped
from threads.log import ToolResultEvent


def test_a_member_of_another_tenants_team_is_not_addressable() -> None:
    async def main() -> None:
        root = sqlite(":memory:")
        acme, globex = scoped(root, "acme"), scoped(root, "globex")
        researcher = agent(name="researcher", model=scripted_model({"responses": [say("Done.")]}))
        first = await agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("Started."), say("Final.")]}
            ),
            team=[researcher],
        ).run("Work.", store=acme)
        assert isinstance(first, Completed), first
        assert first.team.ref.tenant == "acme"

        other = await agent(
            name="lead",
            model=scripted_model(
                {
                    "responses": [
                        call("c1", "send", {"to": "researcher-1", "text": "Hello?"}),
                        say("No one there."),
                    ]
                }
            ),
            team=[],
        ).run("Try.", store=globex)
        assert other.team.ref.tenant == "globex"
        result = next(
            e for e in await events(globex, other.thread) if isinstance(e, ToolResultEvent)
        )
        assert result.data.preview == '{"code":"unknown_member","status":"refused"}'
        await assert_team_replays(await sq_of(acme), first.team.ref.id)
        await assert_team_replays(await sq_of(globex), other.team.ref.id)

    asyncio.run(main())
