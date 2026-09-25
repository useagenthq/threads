"""append_decided: one transaction that checks the lease and head, lets the decision read the
store and build the drafts on the store's thread, admits them and commits. A refusal rolls back
and the writer goes on; the append settles even when its caller is cancelled mid-decision."""

import asyncio
import threading
from collections.abc import Sequence

from store.test_writer import ROOT, TTL, Clock, run, started, user

from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, Writer
from threads.store.conn import Conn, one
from threads.store.sql import int_of
from threads.store.writer import Decide, DecideTx, Refusal


def _wakes(conn: Conn) -> int:
    (n,) = one(conn.execute("SELECT COUNT(*) FROM pending_wakes").fetchone())
    return int_of(n)


def _drafts(*drafts: Draft) -> Decide[str]:
    return lambda _tx: drafts


def test_the_decision_reads_the_store_in_the_transaction_and_its_drafts_are_appended() -> None:
    async def test(store: SqliteStore) -> None:
        clock = Clock()
        writer = await started(store, clock)

        def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[str]:
            (head,) = one(
                tx.conn.execute(
                    "SELECT head_seq FROM branches WHERE branch_id = ?", (ROOT,)
                ).fetchone()
            )
            assert head == tx.fold.seq
            assert tx.now == clock()
            return [user(f"after {head}")]

        done = await writer.append_decided(decide)
        assert isinstance(done, Ok)
        assert [e.seq for e in done.value] == [2]
        assert [e.time for e in done.value] == [clock()]
        assert [e.seq for e in writer.fold.events] == [1, 2]

    run(test)


def test_a_refusal_rolls_back_what_the_decision_wrote_and_the_writer_goes_on() -> None:
    async def test(store: SqliteStore) -> None:
        writer = await started(store, Clock())

        def decide(tx: DecideTx) -> Refusal[str]:
            tx.conn.execute(
                "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, 'c')", (ROOT,)
            )
            return Refusal("mailbox_full")

        assert await writer.append_decided(decide) == Refusal("mailbox_full")
        assert await store.run(_wakes) == 0
        assert writer.fold.seq == 1
        assert isinstance(await writer.append([user("hi")]), Ok)

    run(test)


def test_drafts_that_fail_admission_roll_back_and_do_not_poison() -> None:
    async def test(store: SqliteStore) -> None:
        writer = await started(store, Clock())
        bad = await writer.append_decided(_drafts(user("a"), user("b")))
        assert isinstance(bad, Err)
        assert bad.error.code == "invalid_transition"
        assert writer.fold.seq == 1
        assert isinstance(await writer.append_decided(_drafts(user("hi"))), Ok)

    run(test)


def test_a_lost_lease_is_stale_epoch_before_the_decision_runs_and_poisons() -> None:
    async def test(store: SqliteStore) -> None:
        clock = Clock()
        writer = await started(store, clock)
        clock.now += TTL + 1
        assert isinstance(await store.acquire(ROOT, "b", clock), Ok)
        decided: list[bool] = []

        def decide(_tx: DecideTx) -> Sequence[Draft] | Refusal[str]:
            decided.append(True)
            return [user("late")]

        stale = await writer.append_decided(decide)
        assert isinstance(stale, Err)
        assert stale.error.code == "stale_epoch"
        assert decided == []
        again = await writer.append_decided(decide)
        assert isinstance(again, Err)
        assert again.error.code == "writer_poisoned"

    run(test)


def test_a_stale_writer_with_a_bad_draft_learns_stale_epoch_as_typescript_does() -> None:
    async def test(store: SqliteStore) -> None:
        clock = Clock()
        writer = await started(store, clock)
        clock.now += TTL + 1
        assert isinstance(await store.acquire(ROOT, "b", clock), Ok)
        stale = await writer.append([user("a"), user("b")])
        assert isinstance(stale, Err)
        assert stale.error.code == "stale_epoch"

    run(test)


def test_an_empty_decided_batch_commits_nothing_and_moves_nothing() -> None:
    async def test(store: SqliteStore) -> None:
        writer = await started(store, Clock())
        before = writer.fold
        moved = asyncio.ensure_future(writer.moved())
        assert await writer.append_decided(_drafts()) == Ok(())
        await asyncio.sleep(0)
        assert not moved.done()
        moved.cancel()
        assert writer.fold is before

    run(test)


def test_a_caller_cancelled_mid_decision_leaves_the_append_settled_and_the_writer_in_step() -> None:
    async def test(store: SqliteStore) -> None:
        writer = await started(store, Clock())
        entered, go = threading.Event(), threading.Event()

        def decide(_tx: DecideTx) -> Sequence[Draft] | Refusal[str]:
            entered.set()
            go.wait(5)
            return [user("decided")]

        task = asyncio.ensure_future(writer.append_decided(decide))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        go.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the caller was cancelled")
        assert [e.seq for e in writer.fold.events] == [1, 2]
        await _appends(writer)

    run(test)


async def _appends(writer: Writer) -> None:
    done = await writer.append([Draft("turn_completed", {"reason": "end_turn"})])
    assert isinstance(done, Ok), done
    assert [e.seq for e in writer.fold.events] == [1, 2, 3]
