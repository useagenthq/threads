"""branch.open: a new branch, its first events and its first lease at epoch 1, in one
transaction; an existing branch is already_open, and a failure leaves nothing behind."""

import sqlite3
from collections.abc import Callable

from store.test_writer import CHILD, DONE, ROOT, STARTED, T0, THREAD, TTL, Clock, run, user

from threads.result import Ok
from threads.store import SqliteStore, Writer
from threads.store.lease import Lease
from threads.store.opening import BranchOpening, OpenedBranch, open_branch
from threads.store.sql import transaction


def _rows(table: str) -> Callable[[sqlite3.Connection], list[tuple[object, ...]]]:
    return lambda conn: conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608


def test_it_stores_the_branch_its_events_and_first_lease_and_returns_a_writer() -> None:
    async def test(store: SqliteStore) -> None:
        clock = Clock()
        opened = await store.open_branch(
            THREAD, ROOT, [STARTED, user("hi")], holder_id="opener", clock=clock
        )
        assert isinstance(opened, Ok)
        writer = opened.value
        assert isinstance(writer, Writer)
        assert writer.epoch == 1
        assert await store.run(_rows("leases")) == [(ROOT, "opener", 1, T0 + TTL)]
        assert isinstance(await writer.append([DONE]), Ok)
        read = await store.read(ROOT, clock())
        assert isinstance(read, Ok)
        assert [(e.seq, e.epoch) for e in read.value.fold.events] == [(1, 1), (2, 1), (3, 1)]

    run(test)


def test_an_existing_branch_is_already_open_and_nothing_is_written() -> None:
    async def test(store: SqliteStore) -> None:
        clock = Clock()
        first = await store.open_branch(THREAD, ROOT, [STARTED], holder_id="a", clock=clock)
        assert isinstance(first, Ok)
        again = await store.open_branch(
            THREAD, ROOT, [STARTED, user("x")], holder_id="b", clock=clock
        )
        assert again == Ok("already_open")
        read = await store.read(ROOT, clock())
        assert isinstance(read, Ok)
        assert read.value.fold.seq == 1

    run(test)


def test_drafts_that_fail_admission_leave_no_thread_branch_or_lease() -> None:
    async def test(store: SqliteStore) -> None:
        opened = await store.open_branch(
            THREAD, ROOT, [STARTED, user("a"), user("b")], holder_id="a", clock=Clock()
        )
        assert not isinstance(opened, Ok)
        for table in ("threads", "branches", "leases", "events"):
            assert await store.run(_rows(table)) == []

    run(test)


class _OuterFailedError(Exception):
    """The write around branch.open fails after it."""


def test_inside_another_transaction_it_is_rolled_back_with_it() -> None:
    def outer(conn: sqlite3.Connection) -> None:
        o = BranchOpening("local", THREAD, CHILD, Lease("a", 1, T0 + TTL), (STARTED,))
        try:
            with transaction(conn):
                assert isinstance(open_branch(conn, o, T0), OpenedBranch)
                raise _OuterFailedError
        except _OuterFailedError:
            pass

    async def test(store: SqliteStore) -> None:
        await store.run(outer)
        assert await store.run(_rows("branches")) == []

    run(test)
