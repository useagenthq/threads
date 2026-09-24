"""A team lead behind the host (spec/schema/README.md, "Teams"): a run started over the HTTP API
opens the lead's team in its first append, the lead starts a member, and the run's result is the
lead's answer after the member reported. Mirrors TypeScript's host/test/team.test.ts."""

from typing import TYPE_CHECKING

from host.test_http import as_, run, served, sse, start, text, use

from threads import agent, scripted_model, sqlite
from threads.agents.store import open_store, scoped
from threads.host.runs import Runner
from threads.host.schedule_pass import Pass
from threads.host.schedule_threads import reserve_due
from threads.log import BranchId, ThreadStartedEvent
from threads.result import Ok
from threads.store.schedules import Due

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_a_run_over_the_api_opens_the_team_starts_a_member_and_answers_after_it_reports() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        researcher = agent(name="researcher", model=scripted_model({"responses": [text("Fell.")]}))
        lead_script = [
            use("start", {"agent": "researcher", "task": "Go."}),
            text("Started."),
            text("The researcher says prices fell."),
        ]
        lead = agent(
            name="lead", model=scripted_model({"responses": lead_script}), team=[researcher]
        )
        async with served(lead, store=store) as client:
            body: JsonValue = {"agent": "support", "input": "Research prices."}
            receipt = (await start(client, "alice", "k-1", body)).json()
            follow = f"/v1/threads/{receipt['thread_id']}/runs/{receipt['run_id']}/events"
            last = sse(await client.get(follow, headers=as_("alice")))[-1][1]
            assert isinstance(last, dict)
            result = last["result"]
            assert isinstance(result, dict)
            assert (result["status"], result["output"]) == (
                "completed",
                "The researcher says prices fell.",
            )
        sq = await open_store(scoped(store, "acme"))
        read = await sq.read(BranchId(receipt["branch_id"]), 0)
        assert isinstance(read, Ok)
        started = next(e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent))
        assert started.data.model_dump(mode="json")["team"] is not None
        assert any(e.type == "member_started" for e in read.value.fold.events)

    run(main)


def test_two_schedules_of_one_lead_in_one_pass_each_new_thread_opens_a_team_of_its_own() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead = agent(name="lead", model=scripted_model({"responses": []}), team=[])
        runner = Runner(store, {"lead": lead}, {})
        # A pass pins the lead once and reuses the pin for every schedule of it.
        p = Pass(runner, store, "local")
        started = await p.started("lead")
        for schedule in ("morning", "evening"):
            due = [Due(schedule, 60_000, "lead", "Go.", "UTC", missed=False)]
            assert await reserve_due(p, started, due, 60_000) is None
        sq = await open_store(store)
        teams: list[tuple[str, str]] = await sq.run(
            lambda c: c.execute("SELECT team_id, lead_thread_id FROM teams").fetchall()
        )
        assert len(teams) == 2  # noqa: PLR2004 - one per schedule
        assert {thread for _, thread in teams} == set(await sq.tables.schedules.threads())
        await runner.stop()

    run(main)
