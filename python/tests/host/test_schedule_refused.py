"""A schedule's new thread the index hooks refuse (store.sql `schedule_threads`): the reservation
writes nothing and comes back as the refusal, a value, never an exception."""

import asyncio
from dataclasses import replace

from team.team_kit import CASES, verified
from team.writes import draft_of

from threads import agent, scripted_model, sqlite
from threads.agents.store import now_ms, open_store
from threads.host.runs import Runner
from threads.host.schedule_pass import Pass
from threads.host.schedule_threads import reserve_due
from threads.result import Err, Ok
from threads.store.lines import uuid7
from threads.store.schedules import Due


def test_a_new_thread_the_hooks_refuse_reserves_nothing_and_is_the_refusal() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        runner = Runner(
            store, {"bot": agent(name="bot", model=scripted_model({"responses": []}))}, {}
        )
        p = Pass(runner, store, "local")
        # A lead's thread_started names its team and the team's log branch.
        lead = verified((CASES / "team-settle-wakes-lead" / "logs" / "lead.jsonl").read_bytes())
        assert isinstance(lead, Ok)
        started = draft_of(lead.value.fold.events[0])
        first = [Due("a", 60_000, "bot", "Go.", "UTC", missed=False)]
        assert await reserve_due(p, started, first, now_ms()) is None
        # The team log opens under the schedule pass's holder, never an empty one.
        team = started.data["team"]
        assert isinstance(team, dict)
        sq = await open_store(store)
        log_branch = team["log_branch_id"]
        holders = await sq.run(
            lambda c: c.execute(
                "SELECT holder_id FROM leases WHERE branch_id = ?", (log_branch,)
            ).fetchall()
        )
        assert [h for (h,) in holders if str(h).startswith("schedule-")] == [h for (h,) in holders]
        assert holders
        # Another team whose log branch is the first one's: the team log can't open twice.
        other = replace(started, data={**started.data, "team": {**team, "id": str(uuid7(1))}})
        again = [Due("b", 60_000, "bot", "Go.", "UTC", missed=False)]
        refused = await reserve_due(p, other, again, now_ms())
        assert isinstance(refused, Err), refused
        assert refused.error.code == "invalid_transition"
        rows = await sq.run(
            lambda c: c.execute("SELECT schedule_id FROM schedule_occurrences").fetchall()
        )
        assert rows == [("a",)]
        await runner.stop()

    asyncio.run(main())
