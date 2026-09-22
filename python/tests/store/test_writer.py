"""The single writer: durable appends, lease fencing, validate_next on append, forks."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import Draft, ForkRequest, SqliteStore, Writer, verify_export

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
CHILD = BranchId("0192b000-0000-7000-8000-000000000002")
T0 = 1_790_000_000_000
TTL = 30_000
ALICE: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}
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
DONE = Draft("turn_completed", {"reason": "end_turn"})
SNAPSHOT = Draft(
    "snapshot",
    {
        "snapshot_id": "snap_01",
        "provider": "fake",
        "sandbox_id": "sbx_parent_01",
        "capture_class": "filesystem",
        "expires_at": None,
        "manifest_hash": "1" * 64,
        "quiesced": {"frozen": [], "stopped": [], "excluded": []},
    },
)


def user(text: str) -> Draft:
    return Draft("user_input", {"source": "api", "text": text}, actor=ALICE)


@dataclass
class Clock:
    now: int = T0

    def __call__(self) -> int:
        return self.now


def run(test: Callable[[SqliteStore], Awaitable[None]], path: str = ":memory:") -> None:
    async def main() -> None:
        opened = await SqliteStore.open(path)
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            await test(store)
        finally:
            await store.close()

    asyncio.run(main())


def tamper(path: str, statement: str) -> None:
    """Edits the database behind the store's back, as corruption or a bad actor would."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(statement)


async def started(store: SqliteStore, clock: Clock, holder: str = "a") -> Writer:
    assert await store.create(THREAD, ROOT, clock()) == Ok(None)
    writer = await store.acquire(ROOT, holder, clock)
    assert isinstance(writer, Ok)
    assert isinstance(await writer.value.append([STARTED]), Ok)
    return writer.value


def test_append_is_durable_across_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "threads.db")
    clock = Clock()

    async def write(store: SqliteStore) -> None:
        writer = await started(store, clock)
        appended = await writer.append([user("hi"), DONE])
        assert isinstance(appended, Ok)
        assert [e.seq for e in appended.value] == [2, 3]

    async def reread(store: SqliteStore) -> None:
        read = await store.read(ROOT, clock())
        assert isinstance(read, Ok)
        state = read.value.state
        assert (state.turns_completed, state.head.seq, state.epoch) == (1, 3, 1)

    run(write, path)
    run(reread, path)


def test_live_lease_makes_a_second_writer_busy() -> None:
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        await started(store, clock)
        second = await store.acquire(ROOT, "b", clock)
        assert isinstance(second, Err)
        assert second.error.code == "branch_busy"

    run(test)


def test_stale_writer_is_rejected_and_poisoned() -> None:
    """Invariant 2: after a takeover the old holder can't append, even once."""
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        old = await started(store, clock)
        clock.now += TTL + 1
        new = await store.acquire(ROOT, "b", clock)
        assert isinstance(new, Ok)
        assert new.value.epoch == old.epoch + 1
        stale = await old.append([user("late")])
        assert isinstance(stale, Err)
        assert stale.error.code == "stale_epoch"
        again = await old.append([user("late")])
        assert isinstance(again, Err)
        assert again.error.code == "writer_poisoned"
        assert isinstance(await old.renew(), Err)
        assert isinstance(await new.value.append([user("now")]), Ok)
        read = await store.read(ROOT, clock())
        assert isinstance(read, Ok)
        assert read.value.state.epoch == new.value.epoch

    run(test)


def test_expired_lease_is_not_renewed() -> None:
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        writer = await started(store, clock)
        assert await writer.renew() == Ok(None)
        clock.now += TTL
        renewed = await writer.renew()
        assert isinstance(renewed, Err)
        assert renewed.error.code == "stale_epoch"

    run(test)


def test_validate_next_rejects_a_batch_atomically() -> None:
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        writer = await started(store, clock)
        rejected = await writer.append([user("hi"), user("again")])
        assert isinstance(rejected, Err)
        assert (rejected.error.code, rejected.error.seq) == ("invalid_transition", 3)
        # Nothing of the batch was stored, and the writer still appends from the head.
        appended = await writer.append([user("hi"), DONE])
        assert isinstance(appended, Ok)
        assert [e.seq for e in appended.value] == [2, 3]

    run(test)


def test_a_draft_that_fails_its_schema_is_never_stored() -> None:
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        writer = await started(store, clock)
        no_principal = Draft("user_input", {"source": "api", "text": "hi"})
        refused = await writer.append([no_principal])
        assert isinstance(refused, Err)
        assert refused.error.code == "invalid_line"
        nan = await writer.append([Draft("turn_completed", {"reason": float("nan")})])
        assert isinstance(nan, Err)
        assert nan.error.code == "invalid_line"
        read = await store.read(ROOT, clock())
        assert isinstance(read, Ok)
        assert read.value.head.seq == 1

    run(test)


def test_fork_references_parent_rows(tmp_path: Path) -> None:
    path = str(tmp_path / "threads.db")
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        parent = await started(store, clock)
        assert isinstance(await parent.append([user("hi"), DONE, SNAPSHOT]), Ok)
        before = await store.export(ROOT)
        data = {"reason": "snapshot", "sandbox_id": "sbx_child_01", "knowledge_policy": "pinned"}
        child = await store.fork(ForkRequest(ROOT, 4, CHILD, data), "c", clock)
        assert isinstance(child, Ok)
        assert child.value is not None
        assert child.value.epoch == parent.epoch + 1
        assert isinstance(await child.value.append([user("in the child")]), Ok)
        assert await store.export(ROOT) == before
        exported = await store.export(CHILD)
        assert exported.startswith(before[: before.rindex(b"\n", 0, -1) + 1])
        verified = verify_export(exported, clock())
        assert isinstance(verified, Ok)
        state = verified.value.state
        assert (state.branch_id, state.head.seq, state.status) == (CHILD, 6, "in_turn")
        assert [p.seq for p in state.fork_points] == [4]

    run(test, path)
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT seq FROM events WHERE branch_id = ? ORDER BY seq", (CHILD,))
        assert [seq for (seq,) in rows] == [5, 6]


def test_repair_child_is_inspection_only() -> None:
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        await started(store, clock)
        repair = await store.fork(ForkRequest(ROOT, 1, CHILD, {"reason": "repair"}), "c", clock)
        assert repair == Ok(None)
        refused = await store.acquire(CHILD, "c", clock)
        assert isinstance(refused, Err)
        assert (refused.error.code, refused.error.seq) == ("branch_not_runnable", 2)
        read = await store.read(CHILD, clock())
        assert isinstance(read, Ok)
        assert read.value.state.status == "inspection_only"

    run(test)


def test_a_changed_row_refuses_writable_opens(tmp_path: Path) -> None:
    path = str(tmp_path / "threads.db")
    clock = Clock()

    async def write(store: SqliteStore) -> None:
        writer = await started(store, clock)
        assert isinstance(await writer.append([user("hi"), DONE]), Ok)

    run(write, path)
    tamper(
        path,
        "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), 'hi', 'yo') AS BLOB)"
        " WHERE seq = 2",
    )

    async def reopen(store: SqliteStore) -> None:
        read = await store.read(ROOT, clock())
        assert isinstance(read, Err)
        assert (read.error.code, read.error.seq) == ("prev_hash_mismatch", 3)
        clock.now += TTL + 1
        refused = await store.acquire(ROOT, "b", clock)
        assert isinstance(refused, Err)
        assert (refused.error.code, refused.error.seq) == ("log_corrupt", 3)

    run(reopen, path)


def test_a_moved_head_checkpoint_is_detected(tmp_path: Path) -> None:
    path = str(tmp_path / "threads.db")
    clock = Clock()

    async def write(store: SqliteStore) -> None:
        writer = await started(store, clock)
        assert isinstance(await writer.append([user("hi"), DONE]), Ok)

    run(write, path)
    tamper(path, "DELETE FROM events WHERE seq = 3")

    async def reopen(store: SqliteStore) -> None:
        read = await store.read(ROOT, clock())
        assert isinstance(read, Err)
        assert (read.error.code, read.error.seq) == ("head_mismatch", 3)

    run(reopen, path)


def test_import_is_idempotent_and_refuses_other_lines() -> None:
    clock = Clock()

    async def test(store: SqliteStore) -> None:
        writer = await started(store, clock)
        assert isinstance(await writer.append([user("hi")]), Ok)
        export = await store.export(ROOT)
        verified = verify_export(export, clock())
        assert isinstance(verified, Ok)
        assert await store.import_log(verified.value) == Ok(None)
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        other = opened.value
        try:
            clash = await other.create(THREAD, ROOT, clock() + 1)
            assert clash == Ok(None)
            refused = await other.import_log(verified.value)
            assert isinstance(refused, Err)
            assert refused.error.code == "seq_conflict"
        finally:
            await other.close()

    run(test)
