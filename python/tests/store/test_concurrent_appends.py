"""On both engines: concurrent appends on one writer queue behind its lock (contiguous seqs, no
poison, each committed once), and a decision's nested transaction that rolls back leaves only
the outer transaction's rows."""

import asyncio
from collections.abc import Sequence

from store.test_writer import DONE, ROOT, Clock, run, started, user

from threads.result import Ok
from threads.store import Draft, SqliteStore
from threads.store.conn import Conn, transaction
from threads.store.writer import DecideTx, Refusal

APPENDS = 50


def test_fifty_concurrent_appends_on_one_writer_commit_once_each_in_order() -> None:
    async def test(store: SqliteStore) -> None:
        writer = await started(store, Clock())
        results = await asyncio.gather(
            *(writer.append([user(f"n{i}"), DONE]) for i in range(APPENDS))
        )
        assert all(isinstance(r, Ok) for r in results)
        seqs = sorted(e.seq for r in results if isinstance(r, Ok) for e in r.value)
        assert seqs == list(range(2, 2 + 2 * APPENDS))
        read = await store.read(ROOT, 0)
        assert isinstance(read, Ok)
        texts = [getattr(e.data, "text", None) for e in read.value.fold.events]
        assert sorted(t for t in texts if t) == sorted(f"n{i}" for i in range(APPENDS))

    run(test)


class _NestedFailedError(Exception):
    pass


def _wakes(conn: Conn) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT child_thread_id FROM pending_wakes ORDER BY child_thread_id"
    ).fetchall()


def test_a_nested_transaction_that_rolls_back_leaves_the_outer_one_committed() -> None:
    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[str]:
        insert = "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)"
        tx.conn.execute(insert, (ROOT, "outer"))
        try:
            with transaction(tx.conn):
                tx.conn.execute(insert, (ROOT, "inner"))
                raise _NestedFailedError
        except _NestedFailedError:
            pass
        return [user("hi"), DONE]

    async def test(store: SqliteStore) -> None:
        writer = await started(store, Clock())
        assert isinstance(await writer.append_decided(decide), Ok)
        assert await store.run(_wakes, read_only=True) == [("outer",)]

    run(test)
