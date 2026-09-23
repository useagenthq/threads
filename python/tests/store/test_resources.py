"""The resource ledger: fenced creation and release, and its state machine."""

import asyncio
from typing import get_args

from hypothesis import given, settings
from hypothesis import strategies as st

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import SqliteStore, Writer
from threads.store.lease import TTL_MS
from threads.store.resources import MOVES, Move, State, next_state

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000


async def owned_store(now: list[int]) -> tuple[SqliteStore, Writer]:
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    store = opened.value
    assert await store.create(THREAD, ROOT, now[0]) == Ok(None)
    taken = await store.acquire(ROOT, "first", lambda: now[0])
    assert isinstance(taken, Ok)
    return store, taken.value


def test_a_stale_owner_can_neither_create_nor_release() -> None:
    async def main() -> None:
        now = [T0]
        store, first = await owned_store(now)
        try:
            row = await store.ledger.pending(first.owner, "fake", "sandbox", now[0])
            assert isinstance(row, Ok)
            live = await store.ledger.move(row.value, "found", now[0], ref="sbx_1")
            assert live is not None
            now[0] += TTL_MS + 1
            assert isinstance(await store.acquire(ROOT, "second", lambda: now[0]), Ok)
            refused = await store.ledger.pending(first.owner, "fake", "sandbox", now[0])
            assert isinstance(refused, Err)
            assert refused.error.code == "stale_epoch"
            released = await store.ledger.release(first.owner, live, now[0])
            assert isinstance(released, Err)
            assert released.error.code == "stale_epoch"
            assert [r.state for r in await store.ledger.rows()] == ["live"]
        finally:
            await store.close()

    asyncio.run(main())


def test_only_rows_of_a_dead_owner_are_orphaned() -> None:
    async def main() -> None:
        now = [T0]
        store, first = await owned_store(now)
        try:
            assert isinstance(await store.ledger.pending(first.owner, "fake", "sandbox", T0), Ok)
            assert await store.ledger.orphaned(T0) == ()
            assert len(await store.ledger.orphaned(T0 + TTL_MS)) == 1
        finally:
            await store.close()

    asyncio.run(main())


STATES: tuple[State, ...] = get_args(State.__value__)
MOVE_NAMES: tuple[Move, ...] = get_args(Move.__value__)


def test_released_is_final_and_nothing_returns_to_pending() -> None:
    for state in STATES:
        for move in MOVE_NAMES:
            to = next_state(state, move)
            assert to != "pending"
            assert state != "released" or to is None
    # Every state but released has a way forward (an operator's, at least).
    assert {s for s, _ in MOVES} == set(STATES) - {"released"}


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(MOVE_NAMES), max_size=12))
def test_the_stored_row_follows_the_state_machine(moves: list[Move]) -> None:
    """Whatever is learned in whatever order, the row moves only along MOVES, an illegal move
    changes nothing, and a released row records when and why."""

    async def main() -> None:
        now = [T0]
        store, owner = await owned_store(now)
        try:
            created = await store.ledger.pending(owner.owner, "fake", "sandbox", T0)
            assert isinstance(created, Ok)
            row, expected = created.value, "pending"
            for i, move in enumerate(moves):
                moved = await store.ledger.move(row, move, T0 + i, ref="sbx_1")
                to = next_state(expected, move)
                assert (moved is None) == (to is None)
                if moved is not None and to is not None:
                    row, expected = moved, to
                assert row.state == expected
                if expected == "released":
                    assert row.released_at is not None
                    assert row.release_outcome in ("not_found", "released")
            (stored,) = await store.ledger.rows()
            assert stored == row
        finally:
            await store.close()

    asyncio.run(main())
