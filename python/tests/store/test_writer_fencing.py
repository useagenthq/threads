"""What a writer may still claim about its own authority (invariant 2).

`fence` and `renew` are the two answers a dispatch acts on: a model attempt, a tool body and a
provider transport all send on the strength of one. So they have to be as strict as `append` about a
writer whose last commit is in doubt, and they have to judge the lease by the time the check is
actually made, not by the time the caller asked. Both are proved by fault injection here, because
neither is reachable through ordinary use.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import cast

from test_writer import DONE, ROOT, TTL, Clock, started, user

from threadsai.result import Err, Ok
from threadsai.store import SqliteStore
from threadsai.store.conn import CommitUnknownError, Conn
from threadsai.store.worker import Worker


class _UnknownAfterCommit:
    """The first statement commits and then loses its answer, as a dropped connection after COMMIT
    does: the writer is left not knowing whether its rows are durable."""

    def __init__(self, inner: Worker) -> None:
        self._inner = inner
        self._fail = True

    async def call[T](self, statement: Callable[[Conn], T]) -> T:
        value = await self._inner.call(statement)
        if self._fail:
            self._fail = False
            raise CommitUnknownError("synthetic commit outcome unknown")
        return value

    async def read[T](self, statement: Callable[[Conn], T]) -> T:
        return await self._inner.read(statement)

    async def free[T](self, job: Callable[[Conn], T]) -> T:
        return await self._inner.free(job)


def test_an_uncertain_commit_refuses_a_fence_and_a_renewal_too() -> None:
    """A writer poisoned by an uncertain commit does not know its own head, so it cannot say the
    branch is still its own. `append` refuses it; `fence` and `renew` must refuse it identically,
    or the run that just lost certainty still reaches a model call."""

    async def main() -> None:
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        store = opened.value
        clock = Clock()
        try:
            writer = await started(store, clock)
            # Fault injection: nothing in ordinary use loses a commit's answer, so the writer's
            # worker is wrapped here. The cast says the wrapper stands in for one.
            inner = writer._worker  # pyright: ignore[reportPrivateUsage] - fault injection
            wrapped = cast("Worker", _UnknownAfterCommit(inner))
            writer._worker = wrapped  # pyright: ignore[reportPrivateUsage] - fault injection
            with contextlib.suppress(CommitUnknownError):
                await writer.append([user("maybe"), DONE])
            appended = await writer.append([user("again")])
            assert isinstance(appended, Err)
            assert appended.error.code == "writer_poisoned"
            fenced = await writer.fence()
            assert isinstance(fenced, Err)
            assert fenced.error.code == "writer_poisoned"
            renewed = await writer.renew()
            assert isinstance(renewed, Err)
            assert renewed.error.code == "writer_poisoned"
        finally:
            await store.close()

    asyncio.run(main())


def test_a_fence_that_waited_for_the_store_is_judged_at_the_time_it_ran() -> None:
    """The store runs one statement at a time, so a fence can wait behind another. If it read the
    clock before queueing, it would answer for a moment that has passed: here the lease runs out
    while the check waits, and the answer has to be the one a fresh check gives."""

    async def main() -> None:
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        store = opened.value
        clock = Clock()
        try:
            writer = await started(store, clock)
            entered, release = threading.Event(), threading.Event()

            def occupy(_conn: object) -> None:
                entered.set()
                release.wait(timeout=5)

            worker = writer._worker  # pyright: ignore[reportPrivateUsage] - occupies the thread
            blocking = asyncio.create_task(worker.free(occupy))
            while not entered.is_set():
                await asyncio.sleep(0)
            queued = asyncio.create_task(writer.fence())
            await asyncio.sleep(0.02)
            # The lease runs out while the check is still waiting for the store's thread.
            clock.now += TTL + 1
            release.set()
            await blocking
            waited = await queued
            assert isinstance(waited, Err), "a fence answered from the clock it was queued at"
            assert waited.error.code == "stale_epoch"
            # And once lost it stays lost, whoever asks next.
            fresh = await writer.fence()
            assert isinstance(fresh, Err)
            assert fresh.error.code == "writer_poisoned"
        finally:
            await store.close()

    asyncio.run(main())


def test_a_renewal_that_waited_for_the_store_does_not_revive_a_lost_lease() -> None:
    """`renew` never revives an expired lease. Read before queueing, its clock could still show the
    lease live after it has run out, and the branch would be extended from under its next owner."""

    async def main() -> None:
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        store = opened.value
        clock = Clock()
        try:
            writer = await started(store, clock)
            entered, release = threading.Event(), threading.Event()

            def occupy(_conn: object) -> None:
                entered.set()
                release.wait(timeout=5)

            worker = writer._worker  # pyright: ignore[reportPrivateUsage] - occupies the thread
            blocking = asyncio.create_task(worker.free(occupy))
            while not entered.is_set():
                await asyncio.sleep(0)
            queued = asyncio.create_task(writer.renew())
            await asyncio.sleep(0.02)
            clock.now += TTL + 1
            release.set()
            await blocking
            waited = await queued
            assert isinstance(waited, Err), "a renewal answered from the clock it was queued at"
            assert waited.error.code == "stale_epoch"
            # Nothing was extended: the branch is free for its next owner at a new epoch.
            taken = await store.acquire(ROOT, "next", clock)
            assert isinstance(taken, Ok)
            assert taken.value.epoch == writer.epoch + 1
        finally:
            await store.close()

    asyncio.run(main())
