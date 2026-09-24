"""A background child a crash stopped still reports (Gate 1 §2.7.3): its pending_wakes row
survives the crash, and the next host runs the branch on, relaunches the child and records its
end with the woken that wakes the lead. The run's outcome is the wake turn's answer, and an
index wipe rebuilds the rows from the log alone."""

import asyncio
import logging

import pytest
from host.test_api_recovery import (
    ALICE,
    USAGE,
    Counted,
    answering,
    expire_leases,
    fold,
    stalled,
    start,
    status,
    text,
    until,
)
from pydantic import JsonValue

from threads import Store, agent, sqlite
from threads._generated.host_api_v1 import RunAccepted
from threads.agents.store import open_store
from threads.host import Host, host, reopen
from threads.host.app import recovered
from threads.log import ToolResultLateEvent, TurnCompletedEvent, WokenEvent
from threads.reduce.wakes import pending_wakes
from threads.store import wakes

SCAN: JsonValue = {
    "content": [
        {
            "type": "tool_use",
            "call_id": "c1",
            "name": "spawn_agent",
            "input": {"agent": "scanner", "prompt": "Scan.", "background": True},
        }
    ],
    "stop_reason": "tool_use",
    "usage": USAGE,
}


def serve(store: Store, lead: Counted, child: Counted, instructions: str = "") -> Host:
    scanner = agent(name="scanner", model=child)
    support = agent(model=lead, subagents=[scanner], instructions=instructions)
    return host(store=store, agents={"support": support})


async def turns(store: Store, run: RunAccepted) -> int:
    events = (await fold(store, ALICE, run)).events
    return sum(1 for e in events if isinstance(e, TurnCompletedEvent))


async def rows(store: Store) -> list[tuple[object, ...]]:
    sq = await open_store(store)
    return await sq.run(
        lambda c: c.execute("SELECT branch_id, child_thread_id FROM pending_wakes").fetchall()
    )


def test_the_next_host_finishes_the_child_and_wakes_the_lead_once() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        first = serve(store, answering(SCAN, text("Started.")), stalled(text("late")))
        run = await start(first, ALICE)

        async def answered() -> bool:
            return await turns(store, run) == 1

        await until(answered)
        assert len(await rows(store)) == 1
        await expire_leases(store)

        lead, child = answering(text("The scan is clean.")), answering(text("No deps."))
        async with serve(store, lead, child) as second:
            await recovered(second)

            async def woke() -> bool:
                return await turns(store, run) == 2  # noqa: PLR2004

            await until(woke)
            events = (await fold(store, ALICE, run)).events
            late = [e.event_id for e in events if isinstance(e, ToolResultLateEvent)]
            woken = [e.data.causes for e in events if isinstance(e, WokenEvent)]
            assert len(late) == 1
            assert woken == [late]
            assert await rows(store) == []
            assert await status(second, ALICE, run, 0) == "completed"

    asyncio.run(main())


def test_an_index_wipe_rebuilds_the_rows_from_the_log() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        first = serve(store, answering(SCAN, text("Started.")), stalled(text("late")))
        run = await start(first, ALICE)

        async def answered() -> bool:
            return await turns(store, run) == 1

        await until(answered)
        before = await rows(store)
        events = (await fold(store, ALICE, run)).events
        assert before == [(run.branch_id, c) for c in pending_wakes(events, run.branch_id)]
        sq = await open_store(store)
        await sq.run(lambda c: c.execute("DELETE FROM pending_wakes"))
        assert await rows(store) == []
        await sq.run(lambda c: wakes.rebuild(c, lambda b: events if b == run.branch_id else []))
        assert await rows(store) == before
        await first.stop()

    asyncio.run(main())


def test_a_pending_wake_whose_rerun_fails_for_good_is_not_run_again_until_its_log_moves(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(reopen, "REOPEN_S", 0.05)

    async def main() -> None:
        store = sqlite(":memory:")
        first = serve(store, answering(SCAN, text("Started.")), stalled(text("late")))
        run = await start(first, ALICE)

        async def answered() -> bool:
            return await turns(store, run) == 1

        await until(answered)
        await expire_leases(store)
        # The next host's agent has another config: every rerun of the thread fails.
        async with serve(store, answering(), stalled(), "Changed.") as second:
            await recovered(second)
            await asyncio.sleep(0.5)
        said = [r.getMessage() for r in caplog.records if run.branch_id in r.getMessage()]
        assert len([m for m in said if "not retried" in m]) == 1
        assert len(await rows(store)) == 1

    with caplog.at_level(logging.WARNING):
        asyncio.run(main())
