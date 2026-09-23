"""The edges of API run recovery: a corrupt receipt row is skipped and the valid ones still
recover; a run that fails for a reason other than a held lease is not retried, stays open in the
log and is logged with the reason; and a store error in the first pass never stops `stop()` from
ending the host's runs."""

import asyncio
import logging

import pytest
from host.test_api_recovery import (
    ALICE,
    EVE,
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

from threads import agent, sqlite
from threads.agents.store import open_store
from threads.host import app, host
from threads.host.app import recovered
from threads.log import ModelRequestEvent, TurnCompletedEvent
from threads.store.tables import Tables

LOGGER = "threads"


def test_a_corrupt_receipt_row_is_skipped_and_the_valid_ones_recover(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        late = stalled(text("late"), text("late"))
        first = support(store, late)
        good, bad = await start(first, ALICE), await start(first, EVE)
        for who, run in ((ALICE, good), (EVE, bad)):
            await until(has(store, who, run, ModelRequestEvent))
        sq = await open_store(store)
        await sq.run(
            lambda c: c.execute(
                "UPDATE run_receipts SET thread_id = 'not-a-uuid' WHERE branch_id = ?",
                (bad.branch_id,),
            )
        )
        await expire_leases(store)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            async with support(store, answering(text("done"))) as second:
                await recovered(second)
        assert isinstance((await fold(store, ALICE, good)).events[-1], TurnCompletedEvent)
        assert (await fold(store, EVE, bad)).in_turn
        skipped = [r for r in caplog.records if "run_receipts" in r.getMessage()]
        assert len(skipped) == 1
        assert bad.branch_id in skipped[0].getMessage()
        late.held.set()
        await first.stop()

    asyncio.run(main())


def test_a_run_that_fails_another_way_is_not_retried_and_stays_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app, "REOPEN_S", 0.05)

    async def main() -> None:
        store = sqlite(":memory:")
        late = stalled(text("late"))
        first = support(store, late)
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))
        await expire_leases(store)

        # The host was redeployed with another config: the open run can't continue here.
        changed = agent(model=answering(text("never")), instructions="Be brief.")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            async with host(store=store, agents={"support": changed}):
                await asyncio.sleep(0.5)
        gave_up = [r for r in caplog.records if "not retried" in r.getMessage()]
        assert len(gave_up) == 1
        assert "another config" in gave_up[0].getMessage()
        assert (await fold(store, ALICE, run)).in_turn
        late.held.set()
        await first.stop()

    asyncio.run(main())


def test_stop_ends_the_runs_after_a_store_error_in_the_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blinked(_self: Tables) -> tuple[()]:
        raise OSError("the store blinked")

    monkeypatch.setattr(Tables, "unfinished_runs", blinked)

    async def main() -> None:
        store = sqlite(":memory:")
        served = support(store, stalled(text("late")))
        await served.ready()
        run = await start(served, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))
        with pytest.raises(OSError, match="blinked"):
            await served.stop()
        runner = served._runner  # pyright: ignore[reportPrivateUsage] - what stop() ended
        assert not runner.running(run.branch_id)

    asyncio.run(main())
