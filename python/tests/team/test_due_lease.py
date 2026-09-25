"""A parked member's ask is past its deadline while another process holds the member's lease
(reviewer probe H2, 21E.1): the holder closes it, so this worker leaves the member alone and yields.
Once the lease is free, the worker wakes the member and the ask closes timed out. Mirrors
TypeScript's test/team/due-lease.test.ts."""

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from team.clock_kit import Held, count, elapsing, until
from team.run_kit import call, member_events, result_of, say, sq_of, start
from team.team_kit import assert_team_replays

from threads import Completed, agent, scripted_model, sqlite
from threads.agents.store import now_ms
from threads.agents.team_worker import TeamWorker
from threads.log import BranchId
from threads.result import Ok
from threads.store.sql import text_of
from threads.team.constants import TEAM_CONSTANTS

if TYPE_CHECKING:
    from pydantic import JsonValue

_WHERE = "FROM team_members WHERE name = 'writer-1' AND state = 'parked'"
_PARKED = "SELECT branch_id " + _WHERE
_PARKS = "SELECT COUNT(*) " + _WHERE


def test_a_due_ask_under_another_holders_lease_the_worker_yields_then_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapse = elapsing(monkeypatch)
    launches = [0]
    member = TeamWorker._member  # pyright: ignore[reportPrivateUsage] - counts member launches

    async def counted(self: TeamWorker, *args: object, **kwargs: object) -> None:
        launches[0] += 1
        await member(self, *args, **kwargs)  # type: ignore[arg-type]  # the same arguments

    monkeypatch.setattr(TeamWorker, "_member", counted)

    async def main() -> None:
        store = sqlite(":memory:")
        release = asyncio.Event()
        researcher = agent(name="researcher", model=Held([say("Done."), say("Late.")], release))
        writer_script: list[JsonValue] = [
            call("w1", "ask", {"to": "researcher-1", "question": "Topic?"}),
            say("Report."),
        ]
        writer = agent(name="writer", model=scripted_model({"responses": writer_script}))
        script = [start("c1", "researcher", "Go."), start("c2", "writer", "Ask researcher-1.")]
        script += [say("Started.")] + [say("Final.")] * 8
        lead = agent(
            name="lead", model=scripted_model({"responses": script}), team=[researcher, writer]
        )
        sq = await sq_of(store)  # opened once, before the run: the polls read the run's database
        run = asyncio.ensure_future(lead.run("Go.", store=store))

        async def parked() -> bool:
            return await count(store, _PARKS) > 0

        await until(parked)
        rows = await sq.run(lambda c: c.execute(_PARKED).fetchall())
        # The parked writer's run releases its lease as it ends; then another process takes it.
        other = await sq.acquire(BranchId(text_of(rows[0][0])), "another-process", now_ms)
        for _ in range(500):
            if isinstance(other, Ok):
                break
            await asyncio.sleep(0.01)
            other = await sq.acquire(BranchId(text_of(rows[0][0])), "another-process", now_ms)
        assert isinstance(other, Ok), other
        await elapse(store, TEAM_CONSTANTS.ask_wait_default_ms)

        # Several worker polls pass under the other lease: the worker never launches the member.
        before = launches[0]
        window = 4 * TEAM_CONSTANTS.wake_poll_in_process_ms / 1000
        t0 = time.perf_counter()
        await asyncio.sleep(window)
        assert time.perf_counter() - t0 < window + 2
        assert launches[0] == before

        await other.value.release()

        async def closed() -> bool:
            return await count(store, "SELECT COUNT(*) FROM asks WHERE state = 'open'") == 0

        await until(closed)
        release.set()
        r = await run
        assert isinstance(r, Completed)
        w = await member_events(store, r.team.ref.id, "writer-1")
        assert result_of(w, "w1")["status"] == "timed_out"
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())
