"""Two stores on one database, each with its own connection and thread, as two machines are:
writers racing for one branch never leave a gap or a duplicate seq, budget reservations racing
for one limit never overspend, and stores racing to create one thread leave one root."""

import asyncio
from collections.abc import Iterator

import pytest
from pg_kit import Leg, need_postgres, schema_url
from store.test_writer import DONE, ROOT, T0, THREAD, TTL, user

from threads.log import BranchId, ThreadId
from threads.postgres.opening import open_postgres
from threads.result import Ok
from threads.store import SqliteStore, StoreError, verify_export
from threads.store.budgets import Cover

ROUNDS = 60
RACERS = 20
LIMIT = 10
REFUSALS = {"stale_epoch", "seq_conflict", "branch_busy", "writer_poisoned"}


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


async def _two(leg: Leg) -> tuple[SqliteStore, SqliteStore]:
    url = schema_url(leg.url, leg.schema())
    a = await open_postgres(url, "local", connect=leg.connect(url))
    b = await open_postgres(url, "local", connect=leg.connect(url))
    assert isinstance(a, Ok)
    assert isinstance(b, Ok)
    return a.value, b.value


async def _race(store: SqliteStore, holder: str) -> list[str]:
    """Rounds of take over, append, hand back; every lease is expired by the next round."""
    outcomes: list[str] = []
    for n in range(ROUNDS):
        now = T0 + n * (TTL + 1)
        taken = await store.acquire(ROOT, holder, lambda now=now: now)
        if not isinstance(taken, Ok):
            outcomes.append(taken.error.code)
            continue
        try:
            done = await taken.value.append([user(f"{holder}{n}"), DONE])
        except StoreError:
            outcomes.append("outage")
            continue
        outcomes.append("ok" if isinstance(done, Ok) else done.error.code)
    return outcomes


def test_two_writers_racing_for_one_branch_leave_no_gap_and_no_duplicate(leg: Leg) -> None:
    async def main() -> None:
        a, b = await _two(leg)
        assert await a.create(THREAD, ROOT, T0) == Ok(None)
        first, second = await asyncio.gather(_race(a, "a"), _race(b, "b"))
        outcomes = first + second
        assert set(outcomes) <= {"ok", "outage", *REFUSALS}
        exported = await a.export(ROOT)
        assert isinstance(exported, Ok)
        read = verify_export(exported.value, T0)  # the chain, seqs and epochs verify
        assert isinstance(read, Ok)
        events = [e for e, _ in read.value.segments[-1].events]
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert len(events) == 2 * outcomes.count("ok")
        epochs = [e.epoch for e in events]
        assert epochs == sorted(epochs)
        await a.close()
        await b.close()

    asyncio.run(main())


def test_reservations_racing_from_two_stores_never_overspend(leg: Leg) -> None:
    covers = [Cover("thread:t", {"max_model_requests": LIMIT})]

    async def reserve(store: SqliteStore, key: str) -> str:
        try:
            refused = await store.budgets.reserve(key, covers, {"max_model_requests": 1})
        except StoreError:
            return "outage"
        return "refused" if refused is not None else "reserved"

    async def main() -> None:
        a, b = await _two(leg)
        results = await asyncio.gather(
            *(reserve(a if i % 2 else b, f"k:{i}") for i in range(2 * RACERS))
        )
        spent = await a.budgets.spent("thread:t", "max_model_requests")
        assert spent == results.count("reserved")
        assert spent <= LIMIT
        await a.close()
        await b.close()

    asyncio.run(main())


def _ids(n: int) -> tuple[ThreadId, BranchId, BranchId]:
    """Round n's thread and the two branch ids its racers propose."""
    return (
        ThreadId(f"0192a000-0000-7000-8000-{n:012d}"),
        BranchId(f"0192b000-0000-7000-8000-{n:012d}"),
        BranchId(f"0192b000-0000-7000-9000-{n:012d}"),
    )


def test_two_stores_racing_to_create_one_thread_leave_one_root(leg: Leg) -> None:
    async def main() -> None:
        a, b = await _two(leg)
        for n in range(RACERS):
            thread, first, second = _ids(n)
            made = await asyncio.gather(a.create(thread, first, T0), b.create(thread, second, T0))
            codes = sorted("ok" if isinstance(m, Ok) else m.error.code for m in made)
            assert codes == ["invalid_transition", "ok"]
        for n in range(RACERS, 2 * RACERS):
            thread, first, second = _ids(n)
            found = await asyncio.gather(
                a.root_or_create(thread, first, T0), b.root_or_create(thread, second, T0)
            )
            assert found[0] == found[1]
        roots = await a.run(
            lambda c: c.execute(
                "SELECT thread_id, count(*) FROM branches WHERE parent_branch_id IS NULL"
                " GROUP BY thread_id HAVING count(*) > 1"
            ).fetchall()
        )
        assert roots == []
        await a.close()
        await b.close()

    asyncio.run(main())
