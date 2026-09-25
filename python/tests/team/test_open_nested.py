"""open_team on a nested lead's team: the nested lead is rebound as its thread was pinned, as a
member with the defer_tools it inherited from the outer lead. Mirrors TypeScript's
test/team/open-nested.test.ts."""

import asyncio

from team.run_kit import say, sq_of, start
from team.team_kit import assert_team_replays

from threads import Completed, Principal, TeamRef, agent, open_team, scripted_model, sqlite
from threads.agents.team_tools import Started
from threads.loop.defaults import CONTEXT
from threads.result import Ok
from threads.store.sql import text_of
from threads.team.rows import member_rows

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


def test_open_team_rebinds_a_nested_lead_that_inherited_defer_tools() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        scanner = agent(name="scanner", model=scripted_model({"responses": []}))
        # Sets no context: it inherits the outer lead's defer_tools.
        researcher = agent(
            name="researcher",
            model=scripted_model({"responses": [say("Ready.")]}),
            team=[scanner],
        )
        script = [start("c1", "researcher", "Get ready."), say("Started."), say("Done.")]
        lead = agent(
            name="lead",
            model=scripted_model({"responses": script}),
            context=CONTEXT.model_copy(update={"defer_tools": "always"}),
            team=[researcher],
        )
        r = await lead.run("Go.", store=store)
        assert isinstance(r, Completed)
        sq = await sq_of(store)
        rows = await sq.run(lambda c: member_rows(c, r.team.ref.id))
        nested = next(m for m in rows if m.name == "researcher-1")
        found = await sq.run(
            lambda c: c.execute(
                "SELECT team_id FROM teams WHERE lead_thread_id = ?", (nested.thread_id,)
            ).fetchall()
        )
        ((inner_column,),) = found
        inner = text_of(inner_column)
        opened = await open_team(store, TeamRef(r.team.ref.tenant, inner), principal=OPERATOR)
        assert isinstance(opened, Ok), opened
        assert isinstance(await opened.value.start("scanner", "Scan."), Started)
        await assert_team_replays(sq, inner)
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())
