"""A team's budgets (spec/schema/README.md, "Teams"; design §2.6): a member's model requests are
reserved against its own budget, every ancestor thread's budget, and the run budget of the request
its turn belongs to; start checks headroom first. Mirrors TypeScript's test/team/budgets.test.ts."""

import asyncio

from team.run_kit import Watched, call, events, member_events, say, sq_of, start, types
from team.team_kit import assert_team_replays

from threads import BudgetExhausted, Completed, Principal, Store, agent, scripted_model, sqlite
from threads.log import Budget, MemberEndedEvent, ToolResultEvent, UserInputEvent

BOB = Principal(issuer="api", tenant="local", subject="bob")


async def _reserved_for(store: Store, branch: str) -> list[str]:
    """Which budgets each member attempt was reserved against."""
    sq = await sq_of(store)
    rows: list[tuple[str, str]] = await sq.run(
        lambda c: c.execute(
            "SELECT budget_id, attempt_key FROM budget_ledger"
            " WHERE limit_name = 'max_model_requests' ORDER BY attempt_key, budget_id"
        ).fetchall()
    )
    return [budget for budget, key in rows if key.startswith(f"{branch}:")]


def test_a_member_exhausts_its_requests_run_budget_it_and_the_run_end_budget_exhausted() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(name="researcher", model=scripted_model({"responses": [say("never")]}))
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("Started.")]}
            ),
            team=[researcher],
        )
        r = await lead.run("Work.", store=store, budget=Budget(max_model_requests=2))
        assert isinstance(r, BudgetExhausted), r
        member = await member_events(store, r.team.ref.id, "researcher-1")
        assert "model_request" not in types(member)
        ended = next(e for e in member if isinstance(e, MemberEndedEvent))
        result = ended.data.model_dump(mode="json", by_alias=True)["result"]
        assert result["status"] == "budget_exhausted"
        budget = result["budget"]
        assert (budget["scope"], budget["limit"], budget["limit_value"]) == (
            "run",
            "max_model_requests",
            2,
        )
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_start_without_headroom_for_one_member_request_is_refused_budget_exceeded() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(name="researcher", model=scripted_model({"responses": []}))
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [start("c1", "researcher", "Go.")]}),
            team=[researcher],
        )
        r = await lead.run("Work.", store=store, budget=Budget(max_model_requests=1))
        assert isinstance(r, BudgetExhausted), r
        result = next(e for e in await events(store, r.thread) if isinstance(e, ToolResultEvent))
        assert result.data.preview == '{"code":"budget_exceeded","status":"refused"}'
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_member_requests_reserve_against_the_leads_thread_budget_and_their_turns_run() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researched = asyncio.Event()
        requests = 0

        async def counted(_n: int) -> None:
            nonlocal requests
            requests += 1
            if requests == 2:  # noqa: PLR2004 - the researcher took the second run's message
                researched.set()

        async def last_waits(n: int) -> None:
            # The lead's last answer waits until the researcher took the second run's message.
            if n == 5:  # noqa: PLR2004 - the lead's last request
                await researched.wait()

        researcher_model = scripted_model({"responses": [say("Task done."), say("Message done.")]})
        researcher = agent(name="researcher", model=Watched(researcher_model, counted))
        lead_script = [
            start("c1", "researcher", "Go."),
            say("Started."),
            say("Reported."),
            call("c2", "send", {"to": "researcher-1", "text": "One more."}),
            say("Sent."),
        ]
        lead = agent(
            name="lead",
            budget=Budget(max_model_requests=50),
            model=Watched(scripted_model({"responses": lead_script}), last_waits),
            team=[researcher],
        )
        first = await lead.run("Work.", store=store, budget=Budget(max_model_requests=20))
        second = await lead.run(
            "And more.",
            store=store,
            thread=first.thread,
            principal=BOB,
            budget=Budget(max_model_requests=30),
        )
        assert isinstance(second, Completed), second
        assert second.output == "Sent."
        log = await events(store, first.thread)
        inputs = [e.event_id for e in log if isinstance(e, UserInputEvent)]
        member = await member_events(store, first.team.ref.id, "researcher-1")
        lead0 = first.thread.id
        assert await _reserved_for(store, member[0].branch_id) == [
            f"run:{lead0}:{inputs[0]}",
            f"thread:{lead0}",
            f"run:{lead0}:{inputs[1]}",
            f"thread:{lead0}",
        ]
        await assert_team_replays(await sq_of(store), first.team.ref.id)

    asyncio.run(main())
