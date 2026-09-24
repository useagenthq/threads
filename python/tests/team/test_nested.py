"""A nested lead (spec/schema/README.md, "Teams"): a member whose own definition has a team starts
members of its own team. The lead's worker runs the nested team too; the nested member's
settlement wakes the nested lead, and both teams replay. Approval authority on a nested member
walks team member parents up to the root lead. Mirrors TypeScript's test/team/nested.test.ts."""

import asyncio

from team.run_kit import Watched, events, receipts, say, sq_of, start
from team.team_kit import assert_team_replays

from threads import Completed, Principal, agent, scripted_model, sqlite
from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import SqliteStore
from threads.team.rows import member_rows
from threads.thread.authority import Checked, refused

ALICE = Principal(issuer="api", tenant="local", subject="alice")
BOB = Principal(issuer="api", tenant="local", subject="bob")


async def _teams(sq: SqliteStore) -> list[tuple[str, str]]:
    return await sq.run(
        lambda c: c.execute("SELECT team_id, lead_thread_id FROM teams ORDER BY team_id").fetchall()
    )


def test_a_nested_lead_starts_its_own_member_which_wakes_it_both_teams_replay() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        woke = asyncio.Event()

        async def researcher_on(n: int) -> None:
            if n == 3:  # noqa: PLR2004 - the researcher's wake turn
                woke.set()

        async def lead_on(n: int) -> None:
            # The lead's final answer waits until the scanner's settlement woke the researcher.
            if n == 3:  # noqa: PLR2004 - the lead's wake turn
                await woke.wait()

        scanner = agent(name="scanner", model=scripted_model({"responses": [say("Scanned.")]}))
        researcher_script = [
            start("r1", "scanner", "Scan the notes."),
            say("Waiting for the scanner."),
            say("The scan is done."),
        ]
        researcher = agent(
            name="researcher",
            model=Watched(scripted_model({"responses": researcher_script}), researcher_on),
            team=[scanner],
        )
        lead_script = [start("c1", "researcher", "Research."), say("Started."), say("Final.")]
        lead = agent(
            name="lead",
            model=Watched(scripted_model({"responses": lead_script}), lead_on),
            team=[researcher],
        )
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Completed), r
        assert r.output == "Final."

        sq = await sq_of(store)
        teams = await _teams(sq)
        assert len(teams) == 2  # noqa: PLR2004 - the lead's and the researcher's
        outer = r.team.ref.id
        row = next(
            m for m in await sq.run(lambda c: member_rows(c, outer)) if m.name == "researcher-1"
        )
        inner = next(t for t, lead_thread in teams if lead_thread == row.thread_id)
        assert row.branch_id is not None
        nested = await sq.read(BranchId(row.branch_id), 0)
        assert isinstance(nested, Ok)
        assert len(receipts(nested.value.fold.events, "member_settled")) == 1
        names = [m.name for m in await sq.run(lambda c: member_rows(c, inner))]
        assert names == ["researcher", "scanner-1"]
        await assert_team_replays(sq, outer)
        await assert_team_replays(sq, inner)

    asyncio.run(main())


def test_approval_authority_on_a_nested_member_is_the_root_leads_originator() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        scanner = agent(name="scanner", model=scripted_model({"responses": [say("Scanned.")]}))
        researcher = agent(
            name="researcher",
            model=scripted_model(
                {"responses": [start("r1", "scanner", "Scan."), say("Started."), say("Done.")]}
            ),
            team=[scanner],
        )
        lead_script = [start("c1", "researcher", "Go."), say("Started."), say("Done."), say("Ok.")]
        lead = agent(
            name="lead", model=scripted_model({"responses": lead_script}), team=[researcher]
        )
        r = await lead.run("Work.", store=store, principal=ALICE)
        assert isinstance(r, Completed), r
        # Bob's input makes Bob the root run's originating principal; the members' inputs stay
        # Alice's.
        again = await lead.run("More.", store=store, principal=BOB, thread=r.thread)
        assert isinstance(again, Completed), again
        assert [e.type for e in await events(store, r.thread)].count("user_input") == 2  # noqa: PLR2004

        sq = await sq_of(store)
        teams = await _teams(sq)
        researcher_row = next(
            m for m in await sq.run(lambda c: member_rows(c, r.team.ref.id)) if m.role == "member"
        )
        inner = next(t for t, lead_thread in teams if lead_thread == researcher_row.thread_id)
        scanner_row = next(
            m for m in await sq.run(lambda c: member_rows(c, inner)) if m.role == "member"
        )
        nested = ThreadId(scanner_row.thread_id)
        assert await refused(store, nested, BOB, Checked(None)) is None
        assert await refused(store, nested, ALICE, Checked(None)) is not None
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())
