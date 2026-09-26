"""Importing a schedule thread rebuilds its indices (spec/schema/README.md, "Portable bundles"):
the schedule's identity and its decided occurrence rows come from the chain's own schedule events,
claimed again inside the row transaction so a scheduler can't lose or split a schedule."""

import asyncio

from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, verify_export
from threads.store.sql import LOCAL_TENANT

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
OTHER = ThreadId("0192a000-0000-7000-8000-0000000000ff")
T0 = 1_790_000_000_000
SCHEDULE = "daily_digest"
AT = 1_790_000_600_000
OCCURRENCE = f"{SCHEDULE}@2026-09-26T00:03:20.000Z"
SCHEDULER: dict[str, JsonValue] = {"kind": "scheduler"}
STARTED = Draft(
    "thread_started",
    {
        "agent_name": "demo",
        "config_hash": "0" * 64,
        "instructions": "You are a helpful agent.",
        "model": {"provider": "scripted", "name": "scripted-1"},
        "model_params": {"max_tokens": 1024},
        "adapter": {"name": "scripted", "version": "1", "settings": {}},
        "tools": [],
    },
)
FIRED = Draft(
    "schedule_fired",
    {
        "schedule_id": SCHEDULE,
        "occurrence_id": OCCURRENCE,
        "scheduled_for": AT,
        "timezone": "UTC",
    },
    actor=SCHEDULER,
)


async def _exported() -> bytes:
    """A schedule thread's export: thread_started, then one fired occurrence."""
    opened = await SqliteStore.open(":memory:")
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        assert await store.create(THREAD, ROOT, T0) == Ok(None)
        writer = await store.acquire(ROOT, "setup", lambda: T0)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([STARTED, FIRED]), Ok)
        await writer.value.release()
        lines = await store.export(ROOT)
        assert isinstance(lines, Ok)
        return lines.value
    finally:
        await store.close()


async def _imported(bytes_: bytes, store: SqliteStore) -> Ok[None] | Err[object]:
    verified = verify_export(bytes_, T0)
    assert isinstance(verified, Ok), verified
    return await store.import_log(verified.value)


def _code(result: Ok[None] | Err[object]) -> str:
    return "ok" if isinstance(result, Ok) else str(getattr(result.error, "code", "?"))


def test_import_rebuilds_a_schedules_identity_and_occurrence_row() -> None:
    async def main() -> None:
        data = await _exported()
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        into = opened.value
        try:
            assert isinstance(await _imported(data, into), Ok)
            identity = await into.run(
                lambda c: c.execute(
                    "SELECT schedule_id, thread_id, current FROM schedule_threads"
                ).fetchall(),
                read_only=True,
            )
            assert identity == [(SCHEDULE, THREAD, 1)]
            occurrences = await into.run(
                lambda c: c.execute(
                    "SELECT schedule_id, occurrence_at, state, reason, thread_id, logged_seq"
                    " FROM schedule_occurrences"
                ).fetchall(),
                read_only=True,
            )
            assert occurrences == [(SCHEDULE, AT, "fired", None, THREAD, 2)]
            # A second import of the same bundle changes nothing: the keys are already its own.
            assert isinstance(await _imported(data, into), Ok)
            assert (
                len(
                    await into.run(
                        lambda c: c.execute("SELECT thread_id FROM schedule_threads").fetchall(),
                        read_only=True,
                    )
                )
                == 1
            )
        finally:
            await into.close()

    asyncio.run(main())


def test_a_schedule_on_another_thread_here_is_schedule_conflict_and_stores_nothing() -> None:
    async def main() -> None:
        data = await _exported()
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        into = opened.value
        try:
            # A scheduler got there first with its own thread for the same schedule id.
            await into.run(
                lambda c: c.execute(
                    "INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, current,"
                    " created_at) VALUES (?, ?, ?, 1, ?)",
                    (LOCAL_TENANT, SCHEDULE, OTHER, T0),
                )
            )
            assert _code(await _imported(data, into)) == "schedule_conflict"
            assert (
                await into.run(
                    lambda c: c.execute("SELECT branch_id FROM branches").fetchall(),
                    read_only=True,
                )
                == []
            )
            assert (
                await into.run(
                    lambda c: c.execute(
                        "SELECT occurrence_at FROM schedule_occurrences"
                    ).fetchall(),
                    read_only=True,
                )
                == []
            )
        finally:
            await into.close()

    asyncio.run(main())


def test_an_occurrence_decided_otherwise_here_is_schedule_conflict() -> None:
    async def main() -> None:
        data = await _exported()
        opened = await SqliteStore.open(":memory:")
        assert isinstance(opened, Ok)
        into = opened.value
        try:
            await into.run(
                lambda c: c.execute(
                    "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at,"
                    " state, reason, thread_id, claimed_at)"
                    " VALUES (?, ?, ?, 'skipped', 'overlap', ?, ?)",
                    (LOCAL_TENANT, SCHEDULE, AT, THREAD, T0),
                )
            )
            assert _code(await _imported(data, into)) == "schedule_conflict"
            assert (
                await into.run(
                    lambda c: c.execute("SELECT branch_id FROM branches").fetchall(),
                    read_only=True,
                )
                == []
            )
        finally:
            await into.close()

    asyncio.run(main())
