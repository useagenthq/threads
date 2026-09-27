"""Two hosts, one store: the same inbound messageId must still make one task and **one run**. The
receipt row and the run's user_input commit in one transaction, so the loser of the race rolls its
append back and answers as a replay rather than starting a second run.

Every assertion counts `user_input` rows, not task ids: an equal id proves the two answers agree,
and only the row count proves no second run was started. Mirrors
typescript/packages/host/test/a2a/two-hosts.test.ts."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager

from a2a_agents import talker
from a2a_kit import ALICE, Served, fault_name, message, result, served, task, user_inputs

from threadsai import sqlite


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


@asynccontextmanager
async def pair() -> AsyncGenerator[tuple[Served, Served]]:
    """Two hosts of the same agent on one store, both through ready()."""
    store = sqlite(":memory:")
    async with (
        served({"support": talker("one", "two")}, store=store) as first,
        served({"support": talker("one", "two")}, store=store) as second,
    ):
        yield (first, second)


def test_the_same_message_id_sent_to_two_hosts_gives_one_task_and_one_run() -> None:
    async def main() -> None:
        async with pair() as (first, second):
            here = task(await first.rpc("SendMessage", message("dup", "hello"), as_=ALICE))
            there = task(await second.rpc("SendMessage", message("dup", "hello"), as_=ALICE))
            assert (there["id"], there["contextId"]) == (here["id"], here["contextId"])
            assert await user_inputs(first.store) == 1

    run(main)


def test_two_hosts_racing_on_one_message_id_still_make_one_task_and_one_run() -> None:
    async def main() -> None:
        async with pair() as (first, second):
            # Both in flight at once: whichever wins the key, the other answers as a replay.
            answered = await asyncio.gather(
                first.rpc("SendMessage", message("race", "hello"), as_=ALICE),
                second.rpc("SendMessage", message("race", "hello"), as_=ALICE),
            )
            ids = [task(a)["id"] for a in answered]
            assert ids[0] == ids[1]
            assert await user_inputs(first.store) == 1

    run(main)


def test_a_task_started_on_one_host_is_readable_and_listed_on_the_other() -> None:
    async def main() -> None:
        async with pair() as (first, second):
            here = task(await first.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            # The task is the log's, not the process's, so the second host answers for it in full.
            got = task(await second.rpc("GetTask", {"id": here["id"]}, as_=ALICE))
            assert got["id"] == here["id"]
            listed = result(await second.rpc("ListTasks", {}, as_=ALICE))
            assert isinstance(listed, dict)
            tasks = listed["tasks"]
            assert isinstance(tasks, list)
            ids = [t["id"] for t in tasks if isinstance(t, dict)]
            assert here["id"] in ids

    run(main)


def test_a_reused_message_id_with_another_body_is_refused_by_the_second_host_too() -> None:
    async def main() -> None:
        async with pair() as (first, second):
            task(await first.rpc("SendMessage", message("re", "hello"), as_=ALICE))
            other = await second.rpc("SendMessage", message("re", "different"), as_=ALICE)
            assert fault_name(other) == "InvalidParamsError"
            assert await user_inputs(first.store) == 1

    run(main)
