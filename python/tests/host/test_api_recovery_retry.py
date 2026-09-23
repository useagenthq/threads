"""A host that finds an API run on a branch another lease holds looks at it again each second,
and a store error on one of those looks is no reason to stop looking."""

import asyncio

import pytest
from host.test_api_recovery import (
    ALICE,
    answering,
    expire_leases,
    fold,
    has,
    stalled,
    start,
    support,
    text,
    until,
)

from threads import sqlite
from threads.host import app
from threads.host.runs import RunTask
from threads.log import ModelRequestEvent, TurnCompletedEvent


def test_a_failed_look_is_tried_again_and_the_turn_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "REOPEN_S", 0.05)

    async def main() -> None:
        store = sqlite(":memory:")
        late = stalled(text("late"))
        first = support(store, late)
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))

        model = answering(text("done"))
        second = support(store, model)
        reopen = second._reopen  # pyright: ignore[reportPrivateUsage] - one failing look
        failed = asyncio.Event()

        async def flaky(row: app.OpenRun) -> RunTask | None:
            if failed.is_set():
                return await reopen(row)
            if second._runner.running(row[2]):  # pyright: ignore[reportPrivateUsage] - first pass
                return await reopen(row)
            failed.set()
            raise OSError("the store blinked")

        async with second:
            # The first pass loses to the live lease; then one look fails; then the lease runs out.
            await app.recovered(second)
            monkeypatch.setattr(second, "_reopen", flaky)
            await asyncio.wait_for(failed.wait(), 5)
            await expire_leases(store)
            await until(has(store, ALICE, run, TurnCompletedEvent))
        assert model.calls == 1
        assert isinstance((await fold(store, ALICE, run)).events[-1], TurnCompletedEvent)
        late.held.set()
        await first.stop()

    asyncio.run(main())
