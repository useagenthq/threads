"""A COMMIT whose outcome is unknown (lane 27, review 27-r2-2): the writer is poisoned and its
owner reloads it from the log, which shows the append exactly once or not at all; every other
write is keyed or conditional, so doing it again changes nothing."""

import asyncio
from collections.abc import Iterator, Sequence
from typing import Literal

import pytest
from pg_drill import Dropping, opened_with
from pg_kit import Leg, need_postgres
from store.test_writer import DONE, ROOT, T0, Clock, started, user

from threads.log import ParseError, ThreadId
from threads.result import Err, Ok
from threads.store import StoreError
from threads.store.budgets import Cover
from threads.store.companion import Companion
from threads.store.conn import CommitUnknownError, Conn
from threads.store.inbox import Item, consume
from threads.store.receipts import Key, insert
from threads.store.verify import StoredEvent

type When = Literal["before", "after"]


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


@pytest.mark.parametrize("when", ["before", "after"])
def test_an_append_whose_commit_is_unknown_poisons_and_the_reload_sees_the_truth(
    leg: Leg, when: When
) -> None:
    async def main() -> None:
        store, conn = await opened_with(leg, Dropping)
        clock = Clock()
        writer = await started(store, clock)
        conn.drop = when
        with pytest.raises(StoreError) as unknown:
            await writer.append([user("maybe"), DONE])
        assert isinstance(unknown.value, CommitUnknownError)
        poisoned = await writer.append([user("again")])
        assert isinstance(poisoned, Err)
        assert poisoned.error.code == "writer_poisoned"

        reloaded = await store.acquire(ROOT, "a", clock)  # recovery: the log says what committed
        assert isinstance(reloaded, Ok)
        texts = [getattr(e.data, "text", None) for e in reloaded.value.fold.events]
        assert texts.count("maybe") == (1 if when == "after" else 0)
        appended = await reloaded.value.append([user("next"), DONE])
        assert isinstance(appended, Ok)
        assert appended.value[0].seq == reloaded.value.fold.seq - 1
        await store.close()

    asyncio.run(main())


def test_a_budget_reservation_whose_commit_is_unknown_is_not_reserved_twice(leg: Leg) -> None:
    covers = [Cover("thread:t", {"max_model_requests": 1})]

    async def main() -> None:
        store, conn = await opened_with(leg, Dropping)
        conn.drop = "after"
        with pytest.raises(CommitUnknownError):
            await store.budgets.reserve("b:1", covers, {"max_model_requests": 1})
        assert await store.budgets.reserve("b:1", covers, {"max_model_requests": 1}) is None
        assert await store.budgets.spent("thread:t", "max_model_requests") == 1
        await store.close()

    asyncio.run(main())


def test_an_inbox_item_and_a_receipt_whose_commit_is_unknown_are_written_once(
    leg: Leg,
) -> None:
    item = Item("slack", "T1", "m-1", "d-1", "C1", b'{"text":"hi"}')
    key = Key("local", "runs", "idem-1", "api/local/alice", "0" * 64)

    async def main() -> None:
        store, conn = await opened_with(leg, Dropping)
        clock = Clock()
        writer = await started(store, clock)
        conn.drop = "after"
        with pytest.raises(CommitUnknownError):
            await store.tables.intake([item], T0, lambda: THREAD_A)
        await store.tables.intake([item], T0, lambda: THREAD_B)
        (row,) = await store.tables.inbox_rows()
        assert row.thread_id == THREAD_A

        conn.drop = "after"
        with pytest.raises(CommitUnknownError):
            await writer.append([user("hi"), DONE], _both(row.inbox_id, key))
        again = await store.acquire(ROOT, "a", clock)
        assert isinstance(again, Ok)
        # Consumed once: the owner's retry is refused, never applied twice.
        refused = await again.value.append([user("hi"), DONE], _both(row.inbox_id, key))
        assert isinstance(refused, Err)
        assert await store.tables.pending(row.thread_id) == ()
        assert await store.tables.receipt(key) is not None
        await store.close()

    asyncio.run(main())


THREAD_A = ThreadId("0192a000-0000-7000-8000-00000000000a")
THREAD_B = ThreadId("0192a000-0000-7000-8000-00000000000b")


def _both(inbox_id: int, key: Key) -> Companion:
    mark, receipt = consume(inbox_id), insert(key, T0)

    def run(conn: Conn, events: Sequence[StoredEvent]) -> ParseError | None:
        return mark(conn, events) or receipt(conn, events)

    return run
