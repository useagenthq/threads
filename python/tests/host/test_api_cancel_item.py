"""An `api` inbox item skips the channel adapter (lane 29F): it is addressed to the thread itself,
its agent comes from the thread's own log, and nothing is delivered for it. A host that finds one
unconsumed on its sweep applies it once, which is what happens after the holder of the branch is
gone."""

import asyncio
from http import HTTPStatus

from test_http import ALICE, as_, bearer, sender, served, sse, start

from threads import sqlite
from threads.agents.store import now_ms, open_store, scoped
from threads.host import host
from threads.host.app import recovered
from threads.log import BranchId
from threads.result import Ok
from threads.store.sql import int_of, text_of


def test_the_hosts_sweep_applies_a_pending_api_item_once() -> None:
    async def main() -> tuple[list[str], list[tuple[str, int | None]]]:
        store = sqlite(":memory:")
        bot = sender([])
        async with served(bot, store=store) as client:
            receipt = (
                await start(client, "alice", "k", {"agent": "support", "input": "hi"})
            ).json()
            branch = BranchId(receipt["branch_id"])
            # The run parks on its approval, so its own lease is free to hold from elsewhere.
            sse(
                await client.get(
                    f"/v1/threads/{receipt['thread_id']}/runs/{receipt['run_id']}/events",
                    headers=as_("alice"),
                )
            )
            sq = await open_store(scoped(store, ALICE.tenant))
            # Another process holds the branch: the route answers 202 with the durable item.
            held = await sq.acquire(branch, "elsewhere", now_ms)
            assert isinstance(held, Ok)
            answered = await client.post(
                f"/v1/threads/{receipt['thread_id']}/cancel", headers=as_("alice")
            )
            assert answered.status_code == HTTPStatus.ACCEPTED
            assert answered.json()["item_key"]
            await held.value.release()
        # A fresh host sweeps the unconsumed row; channel "api" has no adapter, and the item's
        # agent is the one the thread's own log pins.
        swept = host(store=store, agents={"support": bot}, authenticate=bearer)
        async with swept:
            await recovered(swept)
        read = await sq.read(branch, now_ms())
        assert isinstance(read, Ok)
        rows = await sq.run(
            lambda c: c.execute("SELECT channel, consumed_seq FROM inbox").fetchall()
        )
        return (
            [e.type for e in read.value.fold.events],
            [(text_of(c), None if s is None else int_of(s)) for c, s in rows],
        )

    log, inbox = asyncio.run(main())
    assert log.count("cancel_requested") == 1
    assert len(inbox) == 1
    channel, consumed = inbox[0]
    assert channel == "api"
    assert consumed is not None
