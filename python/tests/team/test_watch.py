"""wait and monitor in a running team, and the deadlines of asks and waits (spec/schema/README.md,
"Teams", Waits and monitors; design §4.12 and §4.13). A wait parks its caller until every listed
member settles, or until its deadline, when it returns what settled so far. Mirrors TypeScript's
test/team/watch.test.ts."""

import asyncio

import pytest
from pydantic import JsonValue, TypeAdapter
from team.clock_kit import Held, count, elapsing, until
from team.run_kit import call, events, member_events, receipts, result_of, say, sq_of, start, types
from team.team_kit import assert_team_replays

from threads import Completed, MonitorResult, agent, scripted_model, sqlite
from threads.log import MessageSentEvent
from threads.team.constants import TEAM_CONSTANTS

FAILS: JsonValue = {"error": {"reason": "provider_error", "http_status": 400}}


def test_the_lead_waits_for_two_members_both_results_in_the_waits_order() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        script: list[JsonValue] = [
            start("c1", "researcher", "Sales."),
            start("c2", "researcher", "Batteries."),
            call("c3", "wait", {"members": ["researcher-1", "researcher-2"]}),
            say("Both reported."),
            say("Final."),
            say("Final."),
            say("Final."),
        ]
        member = agent(
            name="researcher",
            model=scripted_model({"responses": [say("Sales rose."), say("Prices fell.")]}),
        )
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        r = await lead.run("Research.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "Final."
        waited = result_of(await events(store, r.thread), "c3")
        assert waited["status"] == "waited"
        assert waited["timed_out"] is False
        finished = waited["finished"]
        assert isinstance(finished, list)
        names = [f["member"]["name"] for f in finished if isinstance(f, dict)]  # type: ignore[index] - JSON
        assert names == ["researcher-1", "researcher-2"]
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_at_the_deadline_a_wait_returns_what_settled_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapse = elapsing(monkeypatch)

    async def main() -> None:
        store = sqlite(":memory:")
        release = asyncio.Event()
        script: list[JsonValue] = [
            start("c1", "researcher", "Go."),
            call("c2", "wait", {"members": ["researcher-1"]}),
            say("It is still working."),
            say("Final."),
        ]
        member = agent(name="researcher", model=Held([say("Done.")], release))
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        await sq_of(store)  # opened once, before the run: the polls read the run's database
        run = asyncio.ensure_future(lead.run("Research.", store=store))

        async def waiting() -> bool:
            return await count(store, "SELECT COUNT(*) FROM monitors WHERE kind = 'settle'") > 0

        await until(waiting)
        await elapse(store, TEAM_CONSTANTS.ask_wait_default_ms)

        async def finished() -> bool:
            return not await waiting()

        await until(finished)
        release.set()
        r = await run
        assert isinstance(r, Completed)
        assert r.output == "Final."
        waited = result_of(await events(store, r.thread), "c2")
        assert (waited["status"], waited["finished"], waited["timed_out"]) == ("waited", [], True)
        member_log = await member_events(store, r.team.ref.id, "researcher-1")
        settled = [
            e
            for e in member_log
            if isinstance(e, MessageSentEvent) and e.data.envelope.kind == "member_settled"
        ]
        # The settlement after the deadline sent no settle notice for the finished wait.
        assert len(settled) == 1
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_an_ask_no_one_answers_closes_timed_out_and_the_turn_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapse = elapsing(monkeypatch)

    async def main() -> None:
        store = sqlite(":memory:")
        release = asyncio.Event()
        script: list[JsonValue] = [
            start("c1", "researcher", "Go."),
            call("c2", "ask", {"to": "researcher-1", "question": "Topic?"}),
            say("No answer."),
            say("Final."),
            say("Final."),
        ]
        member = agent(
            name="researcher", model=Held([say("Done."), say("Too late to answer.")], release)
        )
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        await sq_of(store)  # opened once, before the run: the polls read the run's database
        run = asyncio.ensure_future(lead.run("Ask.", store=store))

        async def open_() -> bool:
            return await count(store, "SELECT COUNT(*) FROM asks WHERE state = 'open'") > 0

        await until(open_)
        await elapse(store, TEAM_CONSTANTS.ask_wait_default_ms)

        async def closed() -> bool:
            return not await open_()

        await until(closed)
        release.set()
        r = await run
        assert isinstance(r, Completed)
        assert r.output == "Final."
        assert result_of(await events(store, r.thread), "c2")["status"] == "timed_out"
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_a_monitored_member_that_ends_wakes_the_idle_lead_with_itsresult_of() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        script: list[JsonValue] = [
            start("c1", "researcher", "Go."),
            call("c2", "monitor", {"member": "researcher-1"}),
            say("Watching."),
            say("It failed."),
            say("It failed."),
        ]
        member = agent(name="researcher", model=scripted_model({"responses": [FAILS]}))
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        r = await lead.run("Watch.", store=store)
        assert isinstance(r, Completed)
        assert r.output == "It failed."
        log = await events(store, r.thread)
        assert result_of(log, "c2", TypeAdapter[MonitorResult](MonitorResult))["status"] == (
            "monitoring"
        )
        # One notice for the end monitor, one for the start's task monitor.
        ends = sorted(
            str(e.data.envelope.monitor_id).split(":")[-1] for e in receipts(log, "member_ended")
        )
        assert ends == ["researcher-1", "task"]
        assert "monitor_set" in types(log)
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_monitoring_a_member_that_already_ended_returns_its_result_at_once() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        script: list[JsonValue] = [
            start("c1", "researcher", "Go."),
            say("Started."),
            call("c2", "monitor", {"member": "researcher-1"}),
            say("It had failed."),
        ]
        member = agent(name="researcher", model=scripted_model({"responses": [FAILS]}))
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        r = await lead.run("Watch.", store=store)
        assert isinstance(r, Completed)
        log = await events(store, r.thread)
        got = result_of(log, "c2")
        assert got["status"] == "ended"
        result = got["result"]
        assert isinstance(result, dict)
        assert result["status"] == "failed"
        assert "member_observed" in types(log)
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())
