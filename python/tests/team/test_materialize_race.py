"""Materialize versus delete (design §4.15), with a barrier between materialize's prework (the
rebind) and its write transaction. Delete first: the row check inside the transaction finds the
member gone and opens nothing. Materialize first: the member's branch holds a live first lease, so
the delete is refused busy and writes nothing. Never an orphan branch. Mirrors TypeScript's
test/team/materialize-race.test.ts."""

import asyncio

from team.vectors import TEAM, seeded, vector_mint, vectors, world_logs

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.store.deletion import delete_thread
from threads.team.materialize import MaterializeOptions, Rebind, materialize

VECTOR = next(v for v in vectors() if v["name"] == "materialize-opens-branch")
MEMBER_BRANCH = BranchId("0192b000-0000-7000-8000-0000000000b2")


def _now() -> int:
    now = VECTOR["now"]
    assert isinstance(now, int)
    return now


NOW = _now()


def _clock() -> int:
    return NOW


def _lead() -> ThreadId:
    return ThreadId(str(world_logs(VECTOR)["lead"]["thread_id"]))


async def _count(store: SqliteStore, table: str) -> int:
    rows: list[tuple[int]] = await store.run(
        lambda c: c.execute(f"SELECT COUNT(*) FROM {table}").fetchall()  # noqa: S608 - fixed names
    )
    return rows[0][0]


def test_delete_first_the_row_check_finds_the_member_gone_and_nothing_is_opened() -> None:
    async def main() -> None:
        store = await seeded(VECTOR)

        async def rebind(_agent: str, _hash: str) -> Rebind:
            # The barrier: the lead is deleted after the prework, before the write transaction.
            deleted = await store.run(lambda c: delete_thread(c, "acme", _lead(), NOW))
            assert isinstance(deleted, Ok), deleted
            return Rebind("ok")

        o = MaterializeOptions(rebind, "worker", 30_000, _clock, vector_mint, MEMBER_BRANCH)
        got = await materialize(store, TEAM, "researcher-1", o)
        assert isinstance(got, Ok), got
        assert got.value.status == "not_starting"
        assert await _count(store, "branches") == 0
        read = await store.read(MEMBER_BRANCH, NOW)
        assert isinstance(read, Err)
        assert read.error.code == "branch_not_found"

    asyncio.run(main())


def test_materialize_first_the_members_live_lease_makes_the_delete_busy() -> None:
    async def main() -> None:
        store = await seeded(VECTOR)

        async def rebind(_agent: str, _hash: str) -> Rebind:
            return Rebind("ok")

        o = MaterializeOptions(rebind, "worker", 30_000, _clock, vector_mint, MEMBER_BRANCH)
        got = await materialize(store, TEAM, "researcher-1", o)
        assert isinstance(got, Ok), got
        assert got.value.status == "materialized"
        before = await _count(store, "events")
        deleted = await store.run(lambda c: delete_thread(c, "acme", _lead(), NOW))
        assert isinstance(deleted, Err)
        assert deleted.error.code == "busy"
        assert await _count(store, "events") == before
        assert isinstance(await store.read(MEMBER_BRANCH, NOW), Ok)

    asyncio.run(main())
