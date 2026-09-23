"""The resource ledger: fenced creation and release, and its state machine."""

import asyncio
from typing import get_args

from hypothesis import given, settings
from hypothesis import strategies as st

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import SqliteStore, Writer
from threads.store.lease import TTL_MS
from threads.store.resources import (
    MOVES,
    Answer,
    Move,
    ReleaseOutcome,
    Resource,
    State,
    next_state,
)

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
            live = await store.ledger.resolve(first.owner, row.value, "found", now[0], "sbx_1")
            assert isinstance(live, Ok)
            now[0] += TTL_MS + 1
            assert isinstance(await store.acquire(ROOT, "second", lambda: now[0]), Ok)
            for refused in (
                await store.ledger.pending(first.owner, "fake", "sandbox", now[0]),
                await store.ledger.release(first.owner, live.value, now[0]),
            ):
                assert isinstance(refused, Err)
                assert refused.error.code == "stale_epoch"
            assert [r.state for r in await store.ledger.rows()] == ["live"]
        finally:
            await store.close()

    asyncio.run(main())


def test_only_rows_of_a_dead_owner_are_orphaned() -> None:
    async def main() -> None:
        store, first = await owned_store([T0])
        try:
            row = await store.ledger.pending(first.owner, "fake", "sandbox", T0)
            assert isinstance(row, Ok)
            assert await store.ledger.orphaned(T0) == ()
            # gc can't resolve a row its live owner is still creating.
            assert await store.ledger.gc_resolve(row.value, "not_found", T0) is None
            assert await store.ledger.orphaned(T0 + TTL_MS) == (row.value,)
        finally:
            await store.close()

    asyncio.run(main())


STATES: tuple[State, ...] = get_args(State.__value__)
MOVE_NAMES: tuple[Move, ...] = (
    *get_args(Answer.__value__),
    *get_args(ReleaseOutcome.__value__),
    "release",
    "retry",
)


def test_released_is_final_and_nothing_returns_to_pending() -> None:
    for state in STATES:
        for move in MOVE_NAMES:
            to = next_state(state, move)
            assert to != "pending"
            assert state != "released" or to is None
    # Every state but released has a way forward (an operator's, at least).
    assert {s for s, _ in MOVES} == set(STATES) - {"released"}


type Step = tuple[str, Move]
"""A ledger path and the move it asks for."""

STEPS: tuple[Step, ...] = (
    *(("resolve", a) for a in get_args(Answer.__value__)),
    ("release", "release"),
    *(("settle", o) for o in get_args(ReleaseOutcome.__value__)),
    ("retry", "retry"),
)
SOURCE: dict[str, State] = {
    "resolve": "pending",
    "release": "live",
    "settle": "releasing",
    "retry": "release_failed",
}


async def take(store: SqliteStore, owner: Writer, row: Resource, step: Step) -> Resource | None:
    path, move = step
    ledger, at = store.ledger, T0 + 1
    match path, move:
        case "resolve", "found" | "not_found" | "unresolved":
            done = await ledger.resolve(owner.owner, row, move, at, "sbx_1")
            return done.value if isinstance(done, Ok) else None
        case "release", _:
            done = await ledger.release(owner.owner, row, at)
            return done.value if isinstance(done, Ok) else None
        case "settle", "released" | "not_found" | "release_error" | "unresolved":
            return await ledger.settle_release(row, move, at)
        case _:
            return await ledger.gc_retry(row, at)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(STEPS), max_size=12))
def test_each_path_moves_a_row_only_from_its_own_state(steps: list[Step]) -> None:
    """Whatever is learned in whatever order, a row moves only along MOVES and only by the
    path that owns its current state; any other call changes nothing. A released row records
    when and why."""

    async def main() -> None:
        store, owner = await owned_store([T0])
        try:
            created = await store.ledger.pending(owner.owner, "fake", "sandbox", T0)
            assert isinstance(created, Ok)
            row = created.value
            for step in steps:
                expected = next_state(row.state, step[1])
                if SOURCE[step[0]] != row.state:
                    expected = None
                moved = await take(store, owner, row, step)
                assert (moved is None) == (expected is None)
                if moved is not None:
                    assert moved.state == expected
                    row = moved
                if row.state == "released":
                    assert row.released_at is not None
                    assert row.release_outcome in ("not_found", "released")
            assert await store.ledger.rows() == (row,)
        finally:
            await store.close()

    asyncio.run(main())
